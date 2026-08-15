# SPDX-License-Identifier: MIT
"""The outward leg of the ERPNext consolidation (v1.0.0, verified in v1.0.2).

WHAT CHANGED. Through v0.9.1 Bank Bridge held the reconciliation truth: the
statement-anchor chain, the account pairings, the categorization rules and the
advisory terms all lived in this app's Postgres and were readable only through
this app's MCP server. ERPNext held Bank Transactions and could not answer
"does ••6030 reconcile in October?" without asking a sidecar Flask app. Two
systems of record, two schemas, two places to break.

From v1.0.0 ERPNext is the authority and Bank Bridge is the pipe. This module is
the pipe's discharge: every authoritative fact Bank Bridge produces — an anchor,
a pairing, a variance explanation, an account's Plaid metadata — is pushed here,
through one choke point, to a whitelisted Frappe method.

FOUR PROPERTIES THIS MODULE EXISTS TO GUARANTEE:

  1. A PUSH NEVER FAILS ITS CALLER. Parsing a statement, rebuilding the chain
     and pairing two accounts are things that must keep working when ERPNext is
     down — they are how Bank Bridge earns its keep. So every entry point here
     is fail-soft and returns a verdict rather than raising.

  2. A FAILED PUSH IS NOT A LOST FACT. It is queued (see
     models.ErpnextPushQueue) with exponential backoff and drained on the next
     sync. That is what makes "ERPNext is the source of truth" survive an
     Umbrel restart instead of quietly becoming "ERPNext is the source of
     whatever arrived while it happened to be up".

  3. AN UNCHANGED FACT IS NOT PUSHED. Every push carries a fingerprint of its
     own payload, recorded on the source row. `rebuild_statement_anchors` runs
     after every reparse and every pairing change; without this, each run would
     re-write 27 identical periods to a live ERPNext. A write that says nothing
     is not free — it is a row in somebody's audit trail.

  4. "UNCHANGED" MEANS ERPNEXT CONFIRMED IT, NOT THAT WE SENT IT (v1.0.2). The
     fingerprint in (3) is what makes a fact stop being pushed, so stamping it
     on anything less than proof is how a fact stops being pushed WITHOUT ever
     having arrived — and the symptom is a sync reporting `unchanged: 4` over
     four ERPNext Bank Accounts whose Plaid metadata is null, which is what
     v1.0.1 produced in the field.

     SEVERAL THINGS PRODUCE A 200 THAT WRITES NOTHING, and v1.0.1 could tell
     none of them from a success: Frappe hands a whitelisted method only the
     kwargs it DECLARES and silently drops the rest (so pushing
     `plaid_account_mask` to an erpnext_mcp build that predates the field
     writes nothing); the custom field may be absent from the doctype; a
     validation may rewrite the value after the write. Which one the live
     install hit is not recoverable after the fact, and that opacity is the
     defect. (The `tolerance` → `variance_tolerance` fix one release earlier is
     the same class, caught by hand.) HTTP 200 answers "did Frappe route the
     call", which is never the question being asked.

     So every push now reads ERPNext's own reply — for metadata, the Bank
     Account record it echoes back — and the fingerprint is stamped only when
     the reply CONFIRMS the fields that were sent. See `_ack_metadata`.

WHERE THE BACKOFF LIVES, AND WHY IT IS NOT IN THE INNER LOOP. ERPNextClient
already retries connection errors and 5xx internally (1s/3s/9s), so by the time
a failure reaches this module the transport has spent ~13 seconds establishing
that ERPNext is not answering. Spinning a second exponential retry on top of
that would make one unreachable ERPNext cost 27 anchors × ~40s inside a sync.
So the exponential backoff here is on the QUEUE — `next_attempt_at` doubles per
attempt up to an hour — and a batch that hits a transport failure trips a
circuit breaker (`PushSession.broken`) so the REST of the batch queues
immediately without re-proving the same outage. Retry that costs a minute of
wall clock is not resilience; it is the same failure, slower.

WHAT IS NOT HERE. Nothing in this module reads Plaid, parses a PDF or writes a
Journal Entry — those stay where they are. And nothing here deletes a local
row: the internal tables are the rollback path (see
erpnext_settings.SOURCE_FIELDS), and a consolidation that burned its own
fallback on the way out would be a migration, not a consolidation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from . import db
from .erpnext_client import ERPNextAPIError, ERPNextError

log = logging.getLogger('bankbridge.erpnext_push')


# ── the ERPNext surface ─────────────────────────────────────────────────────
#
# Whitelisted Frappe methods in the erpnext_mcp app. Named as constants and not
# inlined so a rename on the ERPNext side is one edit here plus one line in the
# README's migration section — and so a queued row can record the method it was
# destined for at the time it was queued (see ErpnextPushQueue.method).
ANCHOR_METHOD = 'erpnext_mcp.bank.push_statement_anchor'
PAIRING_METHOD = 'erpnext_mcp.bank.push_account_pairing'
ANCHOR_CHAIN_METHOD = 'erpnext_mcp.bank.get_statement_anchor_chain'
RULES_METHOD = 'erpnext_mcp.bank.list_bank_categorization_rules'
ADVISORY_METHOD = 'erpnext_mcp.bank.get_advisory_agreement_summary'

KIND_ANCHOR = 'statement_anchor'
KIND_PAIRING = 'account_pairing'
KIND_METADATA = 'plaid_metadata'

# Queue backoff: attempt N waits BASE × 2^(N-1), capped. One minute after the
# first failure, an hour once ERPNext has been unreachable for six attempts.
QUEUE_BACKOFF_BASE_SECONDS = 60
QUEUE_BACKOFF_CAP_SECONDS = 3600

# How many queued rows one drain will attempt. A cap so a sync that finds a
# thousand-row queue (an ERPNext that was down for a week) spends bounded time
# on it and drains the rest on the next pass, rather than turning one sync into
# an outage of its own.
DRAIN_LIMIT = 200

# The in-call retry ladder, applied ONLY to a 5xx and never to a connection
# failure. That split is the whole reason this exists at its current length:
#
#   * A 5xx is Frappe answering badly — a worker restarting mid-request, a
#     migration holding a lock. Two seconds later it is very often fine, and
#     retrying saves a fact a 60-second queue round-trip.
#   * A connection failure is ERPNext not being there. Retrying two seconds
#     later re-proves the same thing, and the queue (which retries at 60s, then
#     120s, then 240s…) is the correct instrument for it. Sleeping here would
#     just make every push during an outage two seconds slower for nothing.
#
# See the module docstring for why the ladder is short rather than exponential:
# the exponential part lives on the queue, where the waiting is free.
PUSH_RETRY_BACKOFFS = (2,)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sleep(seconds: float) -> None:  # patched out in tests
    time.sleep(seconds)


def fingerprint(payload: dict) -> str:
    """A stable hash of a push payload — the "has this actually changed?" test.

    Canonical JSON (sorted keys, no whitespace) so two dicts that differ only in
    construction order hash the same; if they didn't, every rebuild would look
    like a change and the whole point of the fingerprint would be lost."""
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


# Bumped when a release changes what a STAMP MEANS, as opposed to what the
# payload contains. v1.0.2 is exactly that: before it, a stamp meant "Frappe
# returned 200"; after it, "ERPNext's reply showed the fields". Every stamp on
# a live database was written under the old meaning and cannot be trusted under
# the new one — the four accounts reporting `unchanged` against four empty Bank
# Accounts are what that looks like from outside.
#
# Folding the version into the HASH INPUT rather than expiring old rows by
# timestamp is what makes the invalidation exact. A date cutoff has to guess
# when the deploy happened and re-clears anything stamped near the boundary;
# this simply cannot collide with a v1.0.1 hash, needs no clock, no migration
# and no state, and self-resolves on the first verified push of each row.
#
# It never reaches ERPNext: it is an argument to the hash, not a payload key.
PUSH_FINGERPRINT_CONTRACT = '2'


def push_fingerprint(payload: dict) -> str:
    """The fingerprint as STORED on a source row — the payload hash, bound to
    the contract version above. Use this everywhere a fingerprint is written or
    compared; `fingerprint` is the plain payload hash and stays public for
    callers that want to compare two payloads to each other."""
    return fingerprint({'contract': PUSH_FINGERPRINT_CONTRACT,
                        'payload': payload})


# ── payload builders ────────────────────────────────────────────────────────
#
# Each returns the exact body ERPNext receives. Kept as pure functions with no
# session access so they are trivially assertable in a test and so the queue can
# store their output verbatim.

def anchor_payload(anchor, account=None) -> dict:
    """One StatementAnchor as ERPNext's Statement Anchor doctype wants it.

    `bank_account` is the ERPNext Bank Account docname — the Link that gives the
    record its Company scope for free (see the plan, §4.1). It can be empty when
    the Plaid account was never mapped, and `push_anchor` refuses in that case
    rather than pushing an unscoped row: an anchor ERPNext cannot attribute to a
    company is worse than an absent one, for the same reason
    rebuild_statement_anchors skips a statement it could read no balance from.

    The Plaid identifiers ride along regardless, so ERPNext can resolve or
    re-resolve the account itself, and so a mask is greppable in its logs.

    SIGN CONVENTION IS CARRIED UNCHANGED (plan §7): positive is money INTO the
    account, `computed_closing = anchored_opening + transaction_sum`,
    `variance = anchored_closing - computed_closing`. ERPNext's Bank Transaction
    `amount_signed` already agrees, so there is nothing to flip and this is the
    one place a flip could be introduced by accident."""
    from . import statements as stmts
    account_id = anchor.account_id or (account.account_id if account else '')
    tolerance = stmts.reconcile_tolerance()
    return {
        'bank_account': ((account.erpnext_bank_account_name or '')
                         if account is not None else ''),
        'company': ((account.owning_company or '') if account is not None
                    else ''),
        'plaid_account_id': account_id,
        'plaid_account_mask': ((account.mask or '') if account is not None
                               else ''),
        'period_start': (anchor.period_start.isoformat()
                         if anchor.period_start else None),
        'period_end': (anchor.period_end.isoformat()
                       if anchor.period_end else None),
        'anchored_opening': anchor.anchored_opening,
        'anchored_closing': anchor.anchored_closing,
        'transaction_sum': anchor.transaction_sum,
        'computed_closing': anchor.computed_closing,
        'variance': anchor.variance,
        'variance_reason': anchor.variance_reason or '',
        'chain_gap_from_prior': bool(anchor.chain_gap_from_prior),
        'parser_version': anchor.parser_version or '',
        # The portfolio bridge (v0.8.0). Emitted always, NULL on a depository
        # statement — "this account has no portfolio figures" and "this build
        # doesn't send them" are different facts.
        'portfolio_opening_value': anchor.portfolio_opening,
        'portfolio_closing_value': anchor.portfolio_closing,
        'security_flow_sum': anchor.security_flow_sum or 0.0,
        'mark_to_market_delta': anchor.mark_to_market_delta,
        # Migration traceability, per the plan: the local id that produced this
        # row, so a backfill can be reconciled against its source.
        'source_statement_id': anchor.statement_id,
        'source_anchor_id': anchor.id,
        'reconciled': bool(anchor.reconciles(tolerance)),
        # `variance_tolerance` is ERPNext's fieldname, and the name has to be
        # exact: Frappe drops kwargs a whitelisted method doesn't declare, so a
        # payload saying `tolerance` arrives as no tolerance at all and the
        # doctype recomputes `reconciled` against its own 0.01 default. The
        # push looks clean and the answer is a different one than BB's.
        'variance_tolerance': tolerance,
    }


def metadata_payload(account, partner=None) -> dict:
    """One PlaidAccount's Bank Account fields — pairing plus Plaid metadata.

    ONE PAYLOAD FOR BOTH because they are one record. The plan splits §5.1.2's
    items 2 and 4 into "push pairings" and "push Plaid metadata", but both land
    on the same ERPNext Bank Account, and sending them separately would mean two
    writes racing on one document with no ordering guarantee between them. So
    `pair_accounts` and the per-sync metadata refresh call the same endpoint
    with the same shape; the only difference is which side triggers it.

    `pairing_type` follows the account's own Plaid type rather than being passed
    in: the brokerage is the account that HOLDS the pairing link (see
    PlaidAccount.paired_account_id), and its companion is the cash-services
    side. Deriving it here keeps the two records from disagreeing about which is
    which."""
    paired_id = (account.paired_account_id or '').strip()
    if paired_id and partner is None:
        from .models import PlaidAccount
        partner = PlaidAccount.query.filter_by(account_id=paired_id).first()
    return {
        'bank_account': account.erpnext_bank_account_name or '',
        'company': account.owning_company or '',
        'plaid_account_id': account.account_id,
        'plaid_account_mask': account.mask or '',
        'plaid_account_type': account.type or '',
        'plaid_account_subtype': account.subtype or '',
        'sync_enabled': bool(account.sync_enabled),
        'paired_bank_account': ((partner.erpnext_bank_account_name or '')
                                if partner is not None else ''),
        'paired_plaid_account_id': paired_id,
        'paired_plaid_account_mask': ((partner.mask or '')
                                      if partner is not None else ''),
        'pairing_type': _pairing_type(account, paired_id),
    }


def _pairing_type(account, paired_id: str) -> str:
    """'Brokerage' for the account carrying the pairing link, 'Cash Services'
    for the one carried, '' for an ordinary unpaired account.

    The reverse lookup is what makes the companion's own record correct: it
    holds no `paired_account_id` of its own (the link is one-directional by
    design), so without asking who points AT it, every cash-services account
    would push a blank pairing_type and ERPNext would show the relationship from
    one side only."""
    if paired_id:
        return 'Brokerage'
    from .models import PlaidAccount
    pointed_at = PlaidAccount.query.filter_by(
        paired_account_id=account.account_id).first()
    return 'Cash Services' if pointed_at is not None else ''


# ── acknowledgement: did the write actually land? ───────────────────────────
#
# v1.0.2. A 200 from Frappe means the request was routed and the method did not
# raise. It does NOT mean the fields in the payload were written, and the gap
# between those two is not academic: `frappe.call` passes a whitelisted method
# only the kwargs its signature DECLARES and drops the rest without a word. A
# pipe that sends six metadata fields to a method that declares three gets the
# same 200 as one that sends nothing, and the three it dropped are exactly the
# ones an operator then finds empty in ERPNext.
#
# Both push endpoints already answer the real question in their reply —
# `push_account_pairing` echoes the Bank Account AS IT NOW READS, and
# `push_statement_anchor` returns created/updated counts — so the fix is to
# read the reply instead of the status line.
#
# THREE OUTCOMES, NOT TWO, and the third is the one that keeps this safe:
#
#   CONFIRMED     ERPNext's reply shows the fields we sent. Stamp and stop.
#   CONTRADICTED  the reply is one we understand and it says they are not
#                 there. Treated as a FAILED push: queued, retried on the
#                 backoff, and `last_error` names the fields. That is what
#                 turns "silently null forever" into a queue depth an operator
#                 can see, and it drains itself the moment the ERPNext side
#                 gains the fields.
#   UNRECOGNIZED  the reply is a shape this build does not know — a leaner
#                 build, a renamed envelope, a method that returns nothing.
#                 NOT queued (a re-push would meet the same silence and the
#                 queue would never drain) and NOT stamped (we cannot claim it
#                 landed). The fact is simply pushed again next sync, which is
#                 cheap because both endpoints are idempotent upserts.
#
# The asymmetry is deliberate. Treating an unknown shape as failure would let
# one envelope rename on the ERPNext side stall every push behind a queue that
# can never drain; treating it as success is the bug this section exists to
# end. Re-sending an idempotent upsert is the only cost that is bounded in both
# directions.

ACK_CONFIRMED = 'confirmed'
ACK_CONTRADICTED = 'contradicted'
ACK_UNRECOGNIZED = 'unrecognized'

# The metadata fields ERPNext writes onto a Bank Account, as (payload key,
# comparator). The ERPNext fieldnames are identical to the payload keys, which
# is what lets the `updated_fields` fallback below work on names alone.
_METADATA_TEXT_FIELDS = ('plaid_account_id', 'plaid_account_mask',
                         'plaid_account_type', 'plaid_account_subtype')


def _ack_metadata(response, payload) -> tuple[str, str]:
    """Whether ERPNext's reply shows the metadata we just pushed.

    Compares against the ECHOED RECORD (`updated[i]['account']`, which
    `upsert_pairing` reads back after writing) rather than any claim of
    success, because the echo is the only part of the reply that cannot be
    right while the Bank Account is wrong.

    ONLY FIELDS WE ACTUALLY SENT ARE CHECKED. ERPNext writes a key only when
    its value is non-empty — a pipe that knows the mask and not the subtype
    must not blank a subtype somebody typed — so demanding an echo for a field
    we sent as '' would report every ordinary account as contradicted.
    `sync_enabled` is the exception and is always checked: False is a value,
    not an absence, and "this account is no longer synced" is precisely the
    fact that must not be lost in transit."""
    rows = _ack_rows(response)
    if rows is None:
        return ACK_UNRECOGNIZED, ('ERPNext accepted the push but its reply '
                                  'did not say what it wrote')
    wanted = (payload.get('bank_account') or '').strip()
    row = _ack_row_for(rows, wanted)
    if row is None:
        return ACK_CONTRADICTED, (
            f'ERPNext acknowledged no write for Bank Account {wanted!r}'
            + (f' — {_ack_failures(response)}' if _ack_failures(response)
               else ''))
    echoed = row.get('account')
    if isinstance(echoed, dict):
        wrong = []
        for key in _METADATA_TEXT_FIELDS:
            sent = str(payload.get(key) or '').strip()
            if not sent:
                continue          # not sent → ERPNext correctly left it alone
            got = str(echoed.get(key) or '').strip()
            if got != sent:
                wrong.append(f'{key}={got or "empty"} (sent {sent})')
        if bool(echoed.get('sync_enabled')) != bool(payload.get('sync_enabled')):
            wrong.append(f'sync_enabled={bool(echoed.get("sync_enabled"))} '
                         f'(sent {bool(payload.get("sync_enabled"))})')
        if wrong:
            return ACK_CONTRADICTED, (
                'ERPNext returned 200 but its Bank Account still reads: '
                + '; '.join(wrong)
                + ' — usually an erpnext_mcp build older than these fields '
                  '(Frappe drops kwargs a whitelisted method does not '
                  'declare); otherwise the custom field is missing from the '
                  'Bank Account doctype, or something on that site rewrote '
                  'the value after the push')
        return ACK_CONFIRMED, ''
    # No echoed record. Fall back to the list of fieldnames it says it wrote —
    # weaker (it is ERPNext's claim, not its state) but still a real answer.
    written = row.get('updated_fields')
    if not isinstance(written, list):
        return ACK_UNRECOGNIZED, ('ERPNext accepted the push but echoed '
                                  'neither the record nor the fields written')
    expected = [k for k in _METADATA_TEXT_FIELDS
                if str(payload.get(k) or '').strip()] + ['sync_enabled']
    missing = [k for k in expected if k not in written]
    if missing:
        return ACK_CONTRADICTED, (
            'ERPNext returned 200 but reports it did not write: '
            + ', '.join(missing))
    return ACK_CONFIRMED, ''


def _ack_anchor(response, payload) -> tuple[str, str]:
    """Whether ERPNext's reply shows the anchor landed.

    Deliberately weaker than the metadata check and for a good reason: the
    Statement Anchor doctype RECOMPUTES every derived value from the three
    anchored numbers, so comparing what came back against what was sent would
    flag the endpoint's whole purpose as a contradiction. What is checked is
    the one thing a silent drop would break — that a row was created or
    updated at all."""
    if not isinstance(response, dict):
        return ACK_UNRECOGNIZED, ('ERPNext accepted the anchor but its reply '
                                  'did not say what it wrote')
    created, updated = response.get('created_count'), response.get('updated_count')
    if created is None and updated is None:
        return ACK_UNRECOGNIZED, ('ERPNext accepted the anchor but returned no '
                                  'created/updated count')
    try:
        landed = int(created or 0) + int(updated or 0)
    except (TypeError, ValueError):
        return ACK_UNRECOGNIZED, 'ERPNext returned counts this build cannot read'
    if landed >= 1:
        return ACK_CONFIRMED, ''
    return ACK_CONTRADICTED, (
        'ERPNext returned 200 but created and updated no anchor'
        + (f' — {_ack_failures(response)}' if _ack_failures(response) else ''))


def _ack_rows(response):
    """The per-record acknowledgements out of ERPNext's envelope, or None when
    the reply is a shape this build does not recognize. Tolerant of the same
    three envelopes `_anchor_rows` accepts, for the same reason: the ERPNext
    side is a sibling codebase under active development."""
    if isinstance(response, list):
        return response if all(isinstance(r, dict) for r in response) else None
    if not isinstance(response, dict):
        return None
    found = None
    for key in ('updated', 'created', 'accounts', 'data'):
        value = response.get(key)
        if not isinstance(value, list) \
                or not all(isinstance(r, dict) for r in value):
            continue
        if value:
            return value
        # An EMPTY list is still an answer — "I wrote nothing" — but only the
        # last resort: an envelope carrying both `updated: []` and
        # `created: [row]` (which a create-only push produces) must resolve to
        # the row, not to the empty half that happened to be checked first.
        found = value
    return found


def _ack_row_for(rows, bank_account: str):
    """The acknowledgement for one Bank Account. Falls back to a single
    unnamed row — a lean reply that acknowledges "the one thing you sent" is
    unambiguous, and refusing it would queue a push that plainly worked."""
    for row in rows:
        if (row.get('bank_account') or '').strip() == bank_account:
            return row
        # `upsert_pairing` names the record under `account.name`, not
        # `bank_account`, when it echoes the whole doc.
        account = row.get('account')
        if isinstance(account, dict) \
                and (account.get('name') or '').strip() == bank_account:
            return row
    if len(rows) == 1 and not (rows[0].get('bank_account') or '').strip():
        return rows[0]
    return None


def _ack_failures(response) -> str:
    """ERPNext's own reason(s) for refusing, joined — the sentence that goes in
    `last_error` and is the whole difference between a queue an operator can
    act on and a number that only goes up."""
    if not isinstance(response, dict):
        return ''
    failed = response.get('failed')
    if not isinstance(failed, list):
        return ''
    reasons = [str(f.get('error') or '') for f in failed
               if isinstance(f, dict) and f.get('error')]
    return '; '.join(reasons)[:500]


# ── the push session ────────────────────────────────────────────────────────

class PushSession:
    """One batch of pushes sharing a client and a circuit breaker.

    THE BREAKER IS THE POINT. A rebuild pushes up to 27 anchors and a sync
    pushes one metadata row per account. When ERPNext is unreachable, the first
    push already spent the transport's full retry ladder proving it; the other
    26 would each spend it again, turning a 13-second outage into a six-minute
    sync. So the first TRANSPORT failure trips `broken` and every subsequent
    push in the batch queues immediately, unattempted.

    A 4xx does NOT trip it: a rejection is about that document (a Link that
    doesn't resolve, a validation ERPNext enforces), and the next one may well
    succeed. Only "we could not reach ERPNext at all" generalizes — which is
    exactly the distinction ERPNextAPIError.status_code already carries."""

    def __init__(self, client=None):
        self._client = client
        self._looked_up = client is not None
        self.broken = False
        # `unconfirmed` (v1.0.2) is its own counter and not folded into
        # `pushed`, because the two need different reactions: `pushed` is
        # finished, `unconfirmed` means ERPNext took the write and would not
        # say what it did with it — nothing is queued and nothing is stamped,
        # so a steady non-zero number here is the signal that the ERPNext side
        # is answering in a shape this build cannot verify.
        self.stats = {'pushed': 0, 'queued': 0, 'skipped': 0, 'unchanged': 0,
                      'unconfirmed': 0}

    @property
    def client(self):
        """The ERPNext client, or None when this install isn't configured.
        Resolved lazily and once — a batch where nothing needs pushing should
        not construct a client at all."""
        if not self._looked_up:
            self._looked_up = True
            self._client = _client_or_none()
        return self._client

    def call(self, method: str, payload: dict) -> tuple[bool, str, bool, object]:
        """POST one payload. Returns (ok, error, transient, response).

        `transient` distinguishes "ERPNext did not answer" (retry the whole
        batch later, and stop trying now) from "ERPNext answered no" (queue this
        one, keep going).

        `response` is ERPNext's decoded reply, carried out rather than
        discarded so `push` can ask it what was actually written — see the
        acknowledgement section above for why a 200 is not that answer."""
        client = self.client
        if client is None:
            return False, 'ERPNext is not configured', False, None
        attempts = len(PUSH_RETRY_BACKOFFS) + 1
        for attempt in range(attempts):
            try:
                out = client.call_method(method, http_method='POST',
                                         json_body=payload)
                return True, '', False, out
            except ERPNextAPIError as e:
                # `transient` trips the batch breaker: "we could not reach
                # ERPNext" generalizes to the next push, "ERPNext said no" does
                # not. `retryable` is narrower — see PUSH_RETRY_BACKOFFS.
                transient = e.status_code is None or e.status_code >= 500
                retryable = e.status_code is not None and e.status_code >= 500
                if retryable and attempt < attempts - 1:
                    wait = PUSH_RETRY_BACKOFFS[attempt]
                    log.warning('erpnext push %s failed (%s) — one retry in %ss',
                                method, e.status_code, wait)
                    _sleep(wait)
                    continue
                return False, str(e), transient, None
            except ERPNextError as e:
                return False, str(e), False, None
            except Exception as e:  # noqa: BLE001 - a push never raises upward
                log.warning('erpnext push %s raised', method, exc_info=True)
                return False, f'{type(e).__name__}: {e}', False, None
        return False, 'unreachable', True, None  # pragma: no cover - defensive

    def push(self, kind: str, dedupe_key: str, method: str,
             payload: dict, *, ack=None) -> dict:
        """Push one payload, queueing it on any failure. Never raises.

        Returns {'status', 'error'} where status is 'pushed' | 'unconfirmed' |
        'queued' | 'skipped'. 'skipped' means the push was not attempted and
        not queued — today that is only "ERPNext is not configured", which is a
        state an operator chose rather than a failure to recover from.

        `ack` is the reply-reader for this kind of push: given ERPNext's
        response and the payload, it says whether the write is confirmed,
        contradicted or unverifiable. Optional so a kind with no meaningful
        reply keeps the old behaviour explicitly, rather than by omission."""
        if self.broken:
            _enqueue(kind, dedupe_key, method, payload,
                     'ERPNext unreachable earlier in this batch')
            self.stats['queued'] += 1
            return {'status': 'queued', 'error': 'ERPNext unreachable'}
        if self.client is None:
            self.stats['skipped'] += 1
            log.debug('erpnext push %s %s skipped — not configured',
                      kind, dedupe_key)
            return {'status': 'skipped', 'error': 'ERPNext is not configured'}
        ok, error, transient, response = self.call(method, payload)
        if ok:
            verdict, detail = (ack(response, payload) if ack
                               else (ACK_CONFIRMED, ''))
            if verdict == ACK_CONFIRMED:
                _dequeue(dedupe_key)
                self.stats['pushed'] += 1
                log.info('erpnext push %s %s -> ok', kind, dedupe_key)
                return {'status': 'pushed', 'error': ''}
            if verdict == ACK_UNRECOGNIZED:
                # Sent and accepted, but unverifiable. Not queued — a retry
                # meets the same silence — and not stamped, so the next sync
                # sends it again against an idempotent upsert.
                self.stats['unconfirmed'] += 1
                log.info('erpnext push %s %s -> accepted but unconfirmed: %s',
                         kind, dedupe_key, detail)
                return {'status': 'unconfirmed', 'error': detail}
            # CONTRADICTED: ERPNext answered, and its answer is that the fields
            # are not there. That is a failed push wearing a 200, and it is
            # queued as one — with the reason, so the queue names its own cure.
            error = detail
        if transient:
            self.broken = True
        _enqueue(kind, dedupe_key, method, payload, error)
        self.stats['queued'] += 1
        log.warning('erpnext push %s %s -> queued: %s', kind, dedupe_key,
                    error[:300])
        return {'status': 'queued', 'error': error}


def _client_or_none():
    """The ERPNext client when this install is configured and reachable enough
    to construct one, else None. Never raises — an unconfigured install is a
    normal state, not an error, and a push is never the thing that reports it."""
    from . import erpnext_bank
    from . import erpnext_settings
    if not erpnext_settings.is_configured():
        return None
    try:
        # NO transport retry ladder, on purpose — see ERPNextClient's docstring.
        # Both things this client is used for have somewhere to land when
        # ERPNext does not answer: a push falls into the queue (and is retried
        # on an exponential schedule from there), a read falls back to the local
        # chain. Retrying inside the call as well would spend thirteen seconds
        # re-proving an outage that the very next line already handles, and the
        # one in-call retry that IS worth having lives a layer up, in
        # PushSession.call, where it can tell a transient failure from a
        # rejection.
        return erpnext_bank.get_client(retry_backoffs=(), timeout=15)
    except Exception:  # noqa: BLE001 - config error, bad URL, anything
        log.warning('could not construct an ERPNext client for a push',
                    exc_info=True)
        return None


# ── the queue ───────────────────────────────────────────────────────────────

def _enqueue(kind: str, dedupe_key: str, method: str, payload: dict,
             error: str) -> None:
    """Record (or refresh) one pending write. Best-effort: a queue-write failure
    must never propagate into the caller that was only trying to push."""
    from .models import ErpnextPushQueue
    try:
        row = ErpnextPushQueue.query.filter_by(dedupe_key=dedupe_key).first()
        if row is None:
            row = ErpnextPushQueue(dedupe_key=dedupe_key, kind=kind,
                                   attempts=0)
            db.session.add(row)
        row.kind = kind
        row.method = method
        row.payload = json.dumps(payload, default=str)
        row.attempts = int(row.attempts or 0) + 1
        row.last_error = (error or '')[:4000]
        row.last_attempt_at = _now()
        row.next_attempt_at = _now() + timedelta(
            seconds=_backoff_seconds(row.attempts))
        row.updated_at = _now()
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.warning('could not queue erpnext push %s', dedupe_key, exc_info=True)


def _backoff_seconds(attempts: int) -> int:
    """Exponential, capped. attempt 1 → 60s, 2 → 120s, … 7+ → 3600s."""
    n = max(1, int(attempts or 1))
    if n > 20:                       # 2**20 overflows nothing but wastes work
        return QUEUE_BACKOFF_CAP_SECONDS
    return min(QUEUE_BACKOFF_CAP_SECONDS,
               QUEUE_BACKOFF_BASE_SECONDS * (2 ** (n - 1)))


def _dequeue(dedupe_key: str) -> None:
    """Drop a pending write that has now landed. Idempotent."""
    from .models import ErpnextPushQueue
    try:
        row = ErpnextPushQueue.query.filter_by(dedupe_key=dedupe_key).first()
        if row is not None:
            db.session.delete(row)
            db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()


def queue_depth() -> int:
    """How many authoritative writes ERPNext has not received. The number an
    operator (or an AI) should be able to see without reading a log."""
    from .models import ErpnextPushQueue
    try:
        return int(ErpnextPushQueue.query.count())
    except Exception:  # noqa: BLE001 - a diagnostic never breaks a caller
        db.session.rollback()
        return 0


def drain(client=None, *, limit: int = DRAIN_LIMIT, force: bool = False) -> dict:
    """Replay every queued write whose backoff has elapsed. Called by each sync.

    `force=True` ignores `next_attempt_at` — the operator-triggered "try now"
    that an MCP caller or an admin button wants, as distinct from the sync's
    scheduled pass which must honour the backoff or the backoff means nothing.

    Returns {'attempted', 'pushed', 'unconfirmed', 'failed', 'remaining'}.
    Never raises."""
    from .models import ErpnextPushQueue
    out = {'attempted': 0, 'pushed': 0, 'unconfirmed': 0, 'failed': 0,
           'remaining': 0}
    try:
        q = ErpnextPushQueue.query
        if not force:
            now = _now()
            q = q.filter(db.or_(ErpnextPushQueue.next_attempt_at.is_(None),
                                ErpnextPushQueue.next_attempt_at <= now))
        rows = (q.order_by(ErpnextPushQueue.next_attempt_at.asc().nullsfirst(),
                           ErpnextPushQueue.id.asc()).limit(limit).all())
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.warning('could not read the erpnext push queue', exc_info=True)
        return out
    session = PushSession(client)
    for row in rows:
        # A row whose payload no longer parses is dropped rather than retried
        # forever: it can never succeed, and a permanently-stuck head would
        # starve everything queued behind it.
        try:
            payload = json.loads(row.payload or '{}')
        except (ValueError, TypeError):
            log.warning('dropping unparseable queued push %s', row.dedupe_key)
            _dequeue(row.dedupe_key)
            continue
        out['attempted'] += 1
        verdict = session.push(row.kind, row.dedupe_key,
                               row.method or _method_for(row.kind), payload,
                               ack=_ack_for(row.kind))
        if verdict['status'] == 'pushed':
            out['pushed'] += 1
            _stamp_source(row.kind, payload)
        elif verdict['status'] == 'unconfirmed':
            # Accepted but unverifiable, so the source row was not stamped and
            # this is neither a push nor a failure — reporting it as pushed
            # would claim a landing nobody witnessed. The row stays queued and
            # its backoff is ADVANCED: on the direct path an unconfirmed push
            # simply repeats next sync, but a queued row has no such clock of
            # its own, and one that never advanced would be re-sent by every
            # single drain forever. That is the "write that says nothing" this
            # module exists to avoid, wearing a different hat.
            out['unconfirmed'] += 1
            _enqueue(row.kind, row.dedupe_key,
                     row.method or _method_for(row.kind), payload,
                     verdict.get('error') or 'accepted but unconfirmed')
        else:
            out['failed'] += 1
        if session.broken:
            # Everything after this would queue unattempted anyway; stopping
            # keeps the drain's own counters honest about what it tried.
            break
    out['remaining'] = queue_depth()
    if out['attempted']:
        log.info('erpnext push queue drained: %d attempted, %d pushed, '
                 '%d still queued', out['attempted'], out['pushed'],
                 out['remaining'])
    return out


def _method_for(kind: str) -> str:
    """The current method for a kind — the fallback for a queued row written by
    a build that predates ErpnextPushQueue.method carrying a value."""
    return {KIND_ANCHOR: ANCHOR_METHOD}.get(kind, PAIRING_METHOD)


def _ack_for(kind: str):
    """The reply-reader for a kind. Keyed the same way as `_method_for` and for
    the same reason: a drained row carries only its kind, and the check that
    proves it landed has to be recoverable from that alone."""
    return {KIND_ANCHOR: _ack_anchor}.get(kind, _ack_metadata)


def _stamp_source(kind: str, payload: dict) -> None:
    """Record on the SOURCE ROW that a drained write has now landed.

    Without this a queued anchor would push successfully and still read as
    never-pushed, so the next rebuild would push it again — the queue would
    drain and immediately refill. Best-effort: the write landed either way, and
    a failed bookkeeping update must not be reported as a failed push."""
    from .models import PlaidAccount, StatementAnchor
    try:
        if kind == KIND_ANCHOR and payload.get('source_anchor_id'):
            anchor = db.session.get(StatementAnchor,
                                    int(payload['source_anchor_id']))
            if anchor is not None:
                anchor.erpnext_push_fingerprint = push_fingerprint(payload)
                anchor.erpnext_pushed_at = _now()
                db.session.commit()
        elif kind in (KIND_PAIRING, KIND_METADATA) \
                and payload.get('plaid_account_id'):
            account = PlaidAccount.query.filter_by(
                account_id=payload['plaid_account_id']).first()
            if account is not None:
                account.erpnext_metadata_fingerprint = push_fingerprint(payload)
                account.erpnext_metadata_pushed_at = _now()
                db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.warning('could not stamp a drained push onto its source row',
                    exc_info=True)


# ── the entry points callers use ────────────────────────────────────────────

def push_anchor(anchor, account=None, *, session: PushSession = None,
                force: bool = False) -> dict:
    """Push one StatementAnchor. Never raises.

    Returns {'status', 'error'} where status is one of:

      pushed       ERPNext has it, and said so.
      unchanged    the fingerprint matches a push that already landed — the
                   no-op that keeps a rebuild from re-writing every period.
      unconfirmed  ERPNext accepted it without saying what it wrote; not
                   stamped, so the next rebuild sends it again.
      queued       the push failed and is parked for the next drain.
      skipped      not attempted and not queued: the account is unmapped, or
                   ERPNext is not configured. Neither is a failure to retry.

    `force` pushes even when the fingerprint says nothing changed — what
    `set_variance_tag` wants after writing a reason that IS the change, and what
    an operator's "resend" wants.

    THE ACCOUNT IS RESOLVED THROUGH THE RE-LINK CHAIN (v1.0.2). An anchor built
    before a Plaid re-link carries the dead account_id, and `reconnect.adopt`
    strips the retired row's Bank Account mapping on purpose — so loading that
    one row finds nothing and the whole pre-relink history reads as an unmapped
    account and never reaches ERPNext. `mapped_account_for` walks forward to
    the row that inherited the mapping, which is the same real account and the
    same Bank Account docname. The anchor's period, not its Plaid id, is what
    ERPNext keys on, so the two halves of the history land in one chain."""
    own = session is None
    session = session or PushSession()
    if account is None:
        from . import reconnect
        account = reconnect.mapped_account_for(anchor.account_id)
    if account is None or not (account.erpnext_bank_account_name or '').strip():
        session.stats['skipped'] += 1
        return {'status': 'skipped',
                'error': 'no ERPNext Bank Account is mapped to Plaid account '
                         f'{anchor.account_id} (or to any account it was '
                         're-linked from) — map it on /admin/accounts before '
                         'its reconciliation can reach ERPNext'}
    payload = anchor_payload(anchor, account)
    fp = push_fingerprint(payload)
    if not force and anchor.erpnext_push_fingerprint == fp \
            and anchor.erpnext_pushed_at is not None:
        session.stats['unchanged'] += 1
        return {'status': 'unchanged', 'error': ''}
    verdict = session.push(KIND_ANCHOR, f'anchor:{anchor.id}', ANCHOR_METHOD,
                           payload, ack=_ack_anchor)
    if verdict['status'] == 'pushed':
        _record_anchor_push(anchor, fp)
    if own:
        verdict['stats'] = dict(session.stats)
    return verdict


def _record_anchor_push(anchor, fp: str) -> None:
    try:
        anchor.erpnext_push_fingerprint = fp
        anchor.erpnext_pushed_at = _now()
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()


def push_anchors(anchors, *, client=None, force: bool = False) -> dict:
    """Push a batch of anchors under one session (and one circuit breaker).

    Returns the session's counters — {'pushed', 'queued', 'skipped',
    'unchanged'} — which is what `rebuild_statement_anchors` folds into its own
    result so an operator sees, in one line, both what was rebuilt and what
    ERPNext now knows about it."""
    session = PushSession(client)
    for anchor in anchors:
        push_anchor(anchor, session=session, force=force)
    return dict(session.stats)


def push_account_metadata(account, *, session: PushSession = None,
                          force: bool = False) -> dict:
    """Push one account's pairing + Plaid metadata onto its ERPNext Bank
    Account. Skipped (not queued) when the account is unmapped: there is no
    Bank Account to write onto, and that is a mapping decision an operator makes
    on /admin/accounts, not a failure to retry.

    THIS IS ALSO THE REPOINT (v1.0.2). `plaid_account_id` rides in the payload
    and ERPNext writes it keyed by the Bank Account DOCNAME, so a sync after a
    re-link is what corrects an ERPNext record still holding the dead Plaid id
    — the docname is the stable system identifier and the Plaid id is not. That
    only works if the push is verified, which is the other half of this
    release: an unverified push stamped a fingerprint, and the stale id then
    read as up to date forever."""
    own = session is None
    session = session or PushSession()
    if not (account.erpnext_bank_account_name or '').strip():
        session.stats['skipped'] += 1
        return {'status': 'skipped',
                'error': f'Plaid account {account.mask or account.account_id} '
                         'is not mapped to an ERPNext Bank Account'}
    payload = metadata_payload(account)
    fp = push_fingerprint(payload)
    if not force and account.erpnext_metadata_fingerprint == fp \
            and account.erpnext_metadata_pushed_at is not None:
        session.stats['unchanged'] += 1
        return {'status': 'unchanged', 'error': ''}
    verdict = session.push(KIND_METADATA, f'account:{account.account_id}',
                           PAIRING_METHOD, payload, ack=_ack_metadata)
    if verdict['status'] == 'pushed':
        try:
            account.erpnext_metadata_fingerprint = fp
            account.erpnext_metadata_pushed_at = _now()
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
    if own:
        verdict['stats'] = dict(session.stats)
    return verdict


def push_pairing(brokerage, cash, *, client=None) -> dict:
    """Push BOTH sides of a pairing, forced.

    Both, because a pairing is a relationship and ERPNext renders it from each
    Bank Account; pushing only the brokerage would leave the companion's own
    record claiming it is unpaired. Forced, because the operator just changed
    the pairing — the fingerprint check exists to suppress no-op rebuilds, not
    to second-guess a deliberate act."""
    session = PushSession(client)
    out = {'brokerage': push_account_metadata(brokerage, session=session,
                                              force=True)}
    if cash is not None and cash.account_id != brokerage.account_id:
        out['cash_services'] = push_account_metadata(cash, session=session,
                                                     force=True)
    out['stats'] = dict(session.stats)
    return out


def push_metadata_for(accounts, *, client=None, force: bool = False) -> dict:
    """Per-sync metadata refresh across a set of accounts, under one session."""
    session = PushSession(client)
    for account in accounts:
        push_account_metadata(account, session=session, force=force)
    return dict(session.stats)


# ── the inward leg: reads, when ERPNext is the source ───────────────────────

def fetch_anchor_chain(account, *, client=None):
    """The anchor chain for one account AS ERPNEXT HOLDS IT, or None.

    None means "could not be read" — unconfigured, unreachable, the doctype not
    deployed yet, or a shape this build doesn't recognize — and every caller
    treats it as "fall back to the local chain and SAY SO". That fallback is
    not a nicety: through the migration window the ERPNext side may not exist,
    and a reconciliation tool that answered "no anchors" because a method 404'd
    would report a reconciled account as unreconciled."""
    if client is None:
        client = _client_or_none()
    if client is None:
        return None
    params = {'plaid_account_id': account.account_id}
    if (account.erpnext_bank_account_name or '').strip():
        params['bank_account'] = account.erpnext_bank_account_name
    if (account.mask or '').strip():
        params['plaid_account_mask'] = account.mask
    try:
        out = client.call_method(ANCHOR_CHAIN_METHOD, params=params)
    except (ERPNextAPIError, ERPNextError) as e:
        log.info('anchor chain read from ERPNext failed (%s) — using the local '
                 'chain', e)
        return None
    except Exception:  # noqa: BLE001
        log.warning('anchor chain read from ERPNext raised', exc_info=True)
        return None
    return _anchor_rows(out)


def _anchor_rows(out):
    """The anchor list out of whatever envelope ERPNext returned. Tolerant of
    three shapes — a bare list, {'anchors': [...]}, {'data': [...]} — because
    the ERPNext side is a sibling codebase under active development and a read
    that hard-failed on an envelope rename would take the reconciliation tools
    down with it."""
    if isinstance(out, list):
        return out
    if isinstance(out, dict):
        for key in ('anchors', 'data', 'rows', 'message'):
            value = out.get(key)
            if isinstance(value, list):
                return value
    return None
