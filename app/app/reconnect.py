# SPDX-License-Identifier: MIT
"""Reconnecting a bank without losing what you configured (v0.4.11).

THE PROBLEM, in two parts.

PART ONE — banks expire. Plaid's ITEM_LOGIN_REQUIRED means the institution wants
the operator to authenticate again; until they do, every call for that Item
fails. Before this release nothing detected that state, so the poll loop kept
calling a dead Item on every cycle — a failed, billable request per poll,
forever, with the operator finding out only because transactions stopped
appearing. The fix has two halves: DETECT the state (from the ITEM webhook, or
from the error text of a failed sync, so neither is required) and then REPAIR
the Item in place using Plaid's update mode, which hands Link the existing
access_token and preserves both `item_id` and every `account_id`.

PART TWO — when repair isn't possible. If the Item was removed, or the operator
deliberately disconnected and re-linked, Plaid mints a genuinely new Item with
genuinely new account_ids. Every row this app keys on account_id then misses,
and the damage is not merely "re-map it by hand":

  * `erpnext_bank_account_name`, `erpnext_gl_account_name`, `owning_company`,
    `import_status`, `sync_enabled` and `opening_balance_je_id` all revert to
    their defaults on the new row.
  * The ERPNext Bank Account dedup key is the `plaid_account_id` custom field,
    so the import tries to CREATE a second Bank Account whose docname collides
    with the existing one — and errors out.
  * The opening-balance idempotency key is the synthetic
    `opening-balance:<account_id>`, so a SECOND opening balance becomes eligible
    for an account that already has one. That one silently double-counts.

So this module fingerprints the new accounts against the old ones and MOVES the
configuration across.

WHY MOVE, NOT COPY. If both rows named the same ERPNext Bank Account, both would
push their transactions into it and every transaction in the overlap would post
twice. The donor is retired in the same operation that hands over its mapping —
`superseded_by_account_id` records where its identity went.

WHY EXACT, UNAMBIGUOUS MATCHING ONLY. The fingerprint is
(institution, mask, type, subtype), and adoption happens ONLY when exactly one
donor matches. Zero matches, or two, means the operator maps it by hand. This is
the same reasoning already written into counterparty.pair_existing_parties:
fuzzy matching here would silently attach one real bank account's ledger history
to a different real bank account, which is a mistake nobody would catch until
the books were already wrong. Exact matching can only ever under-adopt, which is
the safe direction to be wrong in.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import current_app

from . import db
from .models import (GeneratedJournalEntry, PlaidAccount, PlaidAccountLink,
                     PlaidItem)

log = logging.getLogger('bankbridge.reconnect')

# A real chain is one or two hops (a re-link, occasionally two). Ten is far
# past any legitimate history and exists so a cycle introduced by bad data
# cannot hang a walk — which should be impossible and will therefore eventually
# happen. Moved here from statements.py in v1.0.2, along with the walk itself:
# one walk, one limit.
MAX_CHAIN_DEPTH = 10


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── re-auth state ───────────────────────────────────────────────────────────
#
# Plaid error codes that mean "a human must log in again". Matched as substrings
# against the error text because PlaidError carries a message, not a structured
# code — the SDK's ApiException stringifies to a body containing the code, which
# is the same shape erpnext_accounts._is_missing_doctype_error keys off.

# The operator must re-authenticate. Nothing retries its way out of these.
REAUTH_CODES = (
    'ITEM_LOGIN_REQUIRED',
    'PENDING_EXPIRATION',
    'ITEM_LOCKED',
    'USER_PERMISSION_REVOKED',
)

# ITEM webhook codes carrying the same meaning. PENDING_EXPIRATION is a WARNING
# — the Item still works — but it is surfaced the same way on purpose: it is the
# only advance notice Plaid gives, and an operator who reconnects during the
# warning window never has a failed sync at all.
REAUTH_WEBHOOK_CODES = {
    'ITEM_LOGIN_REQUIRED',
    'PENDING_EXPIRATION',
    'USER_PERMISSION_REVOKED',
    'NEW_ACCOUNTS_AVAILABLE',
}

# Human-readable cause per code, shown on the Accounts page.
REAUTH_HELP = {
    'ITEM_LOGIN_REQUIRED':
        'the bank needs you to sign in again. Reconnect to restore syncing.',
    'PENDING_EXPIRATION':
        'this connection expires soon. Reconnect now to avoid an interruption.',
    'ITEM_LOCKED':
        'the bank locked the account after too many sign-in attempts. '
        'Resolve it with the bank, then reconnect.',
    'USER_PERMISSION_REVOKED':
        'access was revoked at the bank. Reconnect to grant it again.',
    'NEW_ACCOUNTS_AVAILABLE':
        'this bank has accounts Bank Bridge has not seen. Reconnect to add '
        'them.',
}


def is_reauth_error(exc: Exception | str) -> str:
    """The re-auth code inside a Plaid error, or '' when it is an ordinary
    failure.

    Returning the CODE rather than a bool is what lets the caller record why the
    Item stopped — 'the bank locked you out' and 'sign in again' need different
    words in front of an operator."""
    blob = (exc if isinstance(exc, str) else str(exc)).upper()
    for code in REAUTH_CODES:
        if code in blob:
            return code
    return ''


def mark_needs_reauth(item: PlaidItem, code: str, *, source: str = '') -> bool:
    """Flag an Item as needing the operator to sign in again. Returns True when
    this CHANGED the row, so a caller can audit only real transitions rather
    than every poll that rediscovers the same dead link.

    Never raises: this is called from inside a sync error handler, where a
    second failure would mask the first."""
    if item is None:
        return False
    already = bool(item.needs_reauth) and (item.reauth_reason or '') == code
    if already:
        return False
    try:
        item.needs_reauth = True
        item.reauth_reason = code
        item.reauth_detected_at = _now()
        item.updated_at = _now()
        db.session.commit()
    except Exception:  # pragma: no cover - never mask the original failure
        db.session.rollback()
        log.warning('could not flag item %s as needing reauth',
                    item.item_id, exc_info=True)
        return False
    log.warning('item %s (%s) needs re-authentication [%s%s] — %s',
                item.item_id, item.institution_name or '?', code,
                f' via {source}' if source else '',
                REAUTH_HELP.get(code, ''))
    return True


def clear_reauth(item: PlaidItem) -> bool:
    """Clear the flag after a successful reconnect or sync. Returns True when it
    was actually set."""
    if item is None or not item.needs_reauth:
        return False
    item.needs_reauth = False
    item.reauth_reason = None
    item.reauth_detected_at = None
    item.updated_at = _now()
    db.session.commit()
    log.info('item %s reconnected — resuming sync', item.item_id)
    return True


def items_needing_reauth() -> list:
    """Connected Items waiting on the operator, newest problem first."""
    return (PlaidItem.query
            .filter(PlaidItem.needs_reauth.is_(True),
                    PlaidItem.disconnected.isnot(True))
            .order_by(PlaidItem.reauth_detected_at.desc().nullslast())
            .all())


# ── fingerprinting ──────────────────────────────────────────────────────────

def is_adoption_enabled() -> bool:
    """Master switch (RECONNECT_ADOPT_ENABLED, default on). Off → a re-linked
    account is a brand-new account with no configuration, exactly as it was
    before v0.4.11."""
    return bool(current_app.config.get('RECONNECT_ADOPT_ENABLED', True))


def fingerprint(account: PlaidAccount) -> tuple | None:
    """The identity of a real-world account, independent of the Plaid ids that
    change on re-link: (mask, type, subtype), normalized. None when the account
    cannot be fingerprinted safely.

    A BLANK MASK yields None, deliberately. The mask is the only component that
    distinguishes two accounts of the same kind at the same bank — without it,
    'depository/checking' would match every checking account the institution
    has, and adoption would attach one account's ledger to another. An account
    Plaid gave no mask for is simply never adopted."""
    mask = (account.mask or '').strip()
    if not mask:
        return None
    return (mask,
            (account.type or '').strip().lower(),
            (account.subtype or '').strip().lower())


def has_configuration(account: PlaidAccount) -> bool:
    """Whether an account carries anything worth moving. An account that was
    linked but never imported has nothing to hand over, so it is not a donor —
    which keeps a re-link from 'adopting' a blank row and reporting success."""
    return bool((account.erpnext_bank_account_name or '').strip()
                or (account.erpnext_gl_account_name or '').strip()
                or (account.owning_company or '').strip()
                or account.opening_balance_je_id is not None
                or (account.import_status or 'pending') != 'pending')


def donor_candidates(account: PlaidAccount, item: PlaidItem) -> list:
    """Every retired account that could be `account` under its previous Plaid
    identity.

    Scoped to the SAME INSTITUTION — matching a Wells Fargo checking ...1234
    against a Columbia Bank checking ...1234 is exactly the silent
    cross-contamination this design exists to prevent. Institution comes from
    the owning Item, so accounts are joined through it."""
    fp = fingerprint(account)
    if fp is None:
        return []
    institution = (item.institution_id or '').strip()
    if not institution:
        # Without an institution we cannot scope the match, and an unscoped
        # match is the dangerous one. Decline rather than guess.
        return []
    sibling_item_ids = [
        row.item_id for row in
        PlaidItem.query.filter(PlaidItem.institution_id == institution,
                               PlaidItem.item_id != item.item_id).all()]
    if not sibling_item_ids:
        return []
    candidates = (PlaidAccount.query
                  .filter(PlaidAccount.item_id.in_(sibling_item_ids),
                          PlaidAccount.account_id != account.account_id,
                          PlaidAccount.superseded_by_account_id.is_(None))
                  .all())
    return [c for c in candidates
            if fingerprint(c) == fp and has_configuration(c)]


# ── adoption ────────────────────────────────────────────────────────────────

# The configuration a re-linked account inherits. `balance_only` is absent on
# purpose: it is re-derived from the live type/subtype on every refresh, so
# copying it would just be overwritten.
ADOPTED_FIELDS = ('erpnext_bank_account_name', 'erpnext_gl_account_name',
                  'owning_company', 'import_status', 'sync_enabled',
                  'opening_balance_je_id')


def _rekey_opening_balance(donor: PlaidAccount, heir: PlaidAccount) -> bool:
    """Move the donor's opening-balance GeneratedJournalEntry onto the heir's
    synthetic key.

    THIS IS THE LOAD-BEARING STEP. `opening_balance_je_id` is only a
    denormalized pointer; the thing that actually prevents a second opening
    balance being booked is the UNIQUE synthetic id
    `opening-balance:<account_id>`. Leaving that row keyed to the dead
    account_id means the heir looks like an account that has never had an
    opening balance — and books a second one, double-counting the starting
    position. Re-keying is what makes adoption safe rather than merely tidy."""
    from . import opening_balance as obal
    old_key = f'{obal.SYNTHETIC_PREFIX}{donor.account_id}'
    new_key = f'{obal.SYNTHETIC_PREFIX}{heir.account_id}'
    row = GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=old_key).first()
    if row is None:
        return False
    # If the heir somehow already has one, leave both alone and say so — two
    # opening balances is a thing to report, never to silently resolve.
    clash = GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=new_key).first()
    if clash is not None:
        log.warning('account %s already has an opening balance; leaving %s '
                    'keyed to the retired account', heir.account_id, old_key)
        return False
    row.plaid_transaction_id = new_key
    return True


def adopt(heir: PlaidAccount, donor: PlaidAccount) -> dict:
    """Move `donor`'s configuration onto `heir` and retire the donor.

    Returns a report dict. Commits, because the caller is mid-refresh and a
    half-applied adoption is the one state that would leave two rows pointing at
    one ERPNext Bank Account."""
    moved = {}
    for field in ADOPTED_FIELDS:
        value = getattr(donor, field, None)
        if value is None or value == '':
            continue
        # Never stomp a value the new row already has: the operator may have
        # picked a Company on the Link page for this very reconnect, and their
        # explicit choice outranks the old row's.
        current = getattr(heir, field, None)
        if field == 'owning_company' and (current or '').strip():
            continue
        setattr(heir, field, value)
        moved[field] = value
    rekeyed = _rekey_opening_balance(donor, heir)

    # v1.0.2 · record the hop BEFORE the donor is stripped. `record_link`
    # snapshots the mask and the Bank Account docname, and the next four lines
    # are what make the donor's copy of both unreadable — so this is the last
    # moment the hop can be recorded in full.
    record_link(donor, heir, reason='plaid_relink')

    # Retire the donor in the SAME operation. Two rows naming one ERPNext Bank
    # Account would both push into it and duplicate every transaction.
    donor.superseded_by_account_id = heir.account_id
    donor.erpnext_bank_account_name = None
    donor.erpnext_gl_account_name = None
    donor.opening_balance_je_id = None
    donor.sync_enabled = False
    donor.import_status = 'superseded'
    donor.updated_at = _now()
    heir.updated_at = _now()
    db.session.commit()

    log.info('adopted configuration from retired account %s → %s '
             '(%s; opening balance re-keyed: %s)',
             donor.account_id, heir.account_id, ', '.join(moved) or 'nothing',
             rekeyed)
    return {'donor': donor.account_id, 'heir': heir.account_id,
            'moved': moved, 'opening_balance_rekeyed': rekeyed,
            'chain': chain_for(heir.account_id)}


def adopt_if_unambiguous(account: PlaidAccount, item: PlaidItem) -> dict | None:
    """Adopt a retired account's configuration onto a freshly-linked one, when
    exactly one candidate matches. Returns the adoption report, or None.

    Called from the account refresh for every NEWLY CREATED account. Never
    raises — a failed adoption must leave the operator with an unmapped account
    to fix by hand, never with a broken link."""
    if not is_adoption_enabled():
        return None
    try:
        candidates = donor_candidates(account, item)
    except Exception:  # pragma: no cover - defensive
        log.warning('adoption lookup failed for %s', account.account_id,
                    exc_info=True)
        return None
    if not candidates:
        return None
    if len(candidates) > 1:
        # Ambiguity is reported, never resolved by guessing. Two accounts at one
        # bank sharing a mask, type and subtype is rare enough that a human
        # should look at it.
        log.warning('account %s (%s ...%s) matches %d retired accounts — '
                    'not adopting; map it by hand on /admin/accounts',
                    account.account_id, account.subtype or account.type,
                    account.mask, len(candidates))
        return None
    try:
        report = adopt(account, candidates[0])
    except Exception:  # pragma: no cover - never break a link
        db.session.rollback()
        log.warning('adoption failed for %s', account.account_id, exc_info=True)
        return None
    try:
        from . import audit
        audit.record('account_configuration_adopted',
                     subject_type='PlaidAccount', subject_id=account.account_id,
                     after=report,
                     notes=(f"re-linked account inherited the mapping of "
                            f"retired {report['donor']}"))
    except Exception:  # pragma: no cover - auditing must not break the link
        log.debug('adoption audit failed', exc_info=True)
    return report


# ── the id chain ────────────────────────────────────────────────────────────
#
# v1.0.2. Everything above moves a re-linked account's CONFIGURATION. This
# section keeps its IDENTITY: the record that ...6030 was three different Plaid
# ids over eighteen months and is one bank account.
#
# Two sources, read together and never separately:
#
#   * `PlaidAccount.superseded_by_account_id` — the live pointer, written by
#     `adopt`. Authoritative while both rows exist, and only as durable as the
#     retired row it sits on.
#   * `PlaidAccountLink` — the durable event log, written here. Outlives both
#     rows because it holds no foreign key to either.
#
# Reading both is not belt-and-braces, it is correctness in each direction: an
# install that upgraded into v1.0.2 has hops only the column knows about (until
# the backfill runs), and an install that has since pruned has hops only the
# table knows about. Neither source is complete on its own, and their union is.


def record_link(donor: PlaidAccount, heir: PlaidAccount, *,
                reason: str = 'plaid_relink') -> PlaidAccountLink | None:
    """Record one hop of the id chain, durably and idempotently.

    Snapshots the mask and the ERPNext Bank Account docname AT THE HOP, which
    is the only moment both are knowable: `adopt` is about to null the donor's
    mapping (it must — two rows naming one Bank Account would double-post), so
    a minute later there is nothing left to read.

    RECORDED ONCE, THEN ONLY FILLED IN. A second call for the same hop — the
    hygiene sweep re-asserting what an adoption already wrote — updates only
    the fields that are still empty, and never `reason` or `detected_at`. This
    row says what was true AT the hop; a later pass knows less about that
    moment than the pass that was there for it, and overwriting a snapshot with
    a re-observation is how a history stops being one.

    Best-effort and never raises. The caller is mid-adoption and a failure to
    write history must not fail the adoption itself — an un-recorded hop is
    recoverable (the backfill reconstructs it from the column), a half-applied
    adoption is not."""
    if donor is None or heir is None:
        return None
    previous = (donor.account_id or '').strip()
    current = (heir.account_id or '').strip()
    if not previous or not current or previous == current:
        return None
    try:
        row = PlaidAccountLink.query.filter_by(
            previous_account_id=previous, account_id=current).first()
        if row is not None:
            _fill_blanks(row, donor, heir)
            db.session.commit()
            return row
        db.session.add(PlaidAccountLink(
            previous_account_id=previous, account_id=current,
            previous_item_id=donor.item_id, item_id=heir.item_id,
            # The heir's mask, falling back to the donor's: adoption only ever
            # matches on an identical mask, so these agree — but a
            # hygiene-marked hop is operator-driven and need not, and the
            # donor's is the one naming the account nobody can look up now.
            mask=(heir.mask or donor.mask or '') or None,
            erpnext_bank_account_name=(
                (heir.erpnext_bank_account_name
                 or donor.erpnext_bank_account_name or '') or None),
            reason=reason, detected_at=_now()))
        db.session.commit()
        return PlaidAccountLink.query.filter_by(
            previous_account_id=previous, account_id=current).first()
    except Exception:  # noqa: BLE001 - history must never break an adoption
        db.session.rollback()
        log.warning('could not record the account link %s → %s', previous,
                    current, exc_info=True)
        return None


def _fill_blanks(row: PlaidAccountLink, donor: PlaidAccount,
                 heir: PlaidAccount) -> None:
    """Complete a hop already on file, without overwriting it. The case this
    is for: the v1.0.2 backfill reconstructs a hop from the column alone and
    cannot know the donor's Bank Account (adopt nulled it releases ago), so a
    later pass that DOES know it should be able to say so — and nothing else."""
    if not row.previous_item_id:
        row.previous_item_id = donor.item_id
    if not row.item_id:
        row.item_id = heir.item_id
    if not row.mask:
        row.mask = (heir.mask or donor.mask or '') or None
    if not row.erpnext_bank_account_name:
        row.erpnext_bank_account_name = (
            (heir.erpnext_bank_account_name
             or donor.erpnext_bank_account_name or '') or None)


def _hops() -> list[tuple[str, str]]:
    """Every (previous, current) edge this install knows about, from both
    sources. One query each rather than one per node — a chain walk is on the
    push path and runs per account."""
    edges = [(row.previous_account_id, row.account_id)
             for row in PlaidAccountLink.query.all()
             if row.previous_account_id and row.account_id]
    edges += [(row.account_id, (row.superseded_by_account_id or '').strip())
              for row in PlaidAccount.query.filter(
                  PlaidAccount.superseded_by_account_id.isnot(None)).all()
              if row.account_id and (row.superseded_by_account_id or '').strip()]
    return [(a, b) for a, b in edges if a != b]


def chain_for(account_id: str) -> list:
    """Every Plaid id that is the SAME REAL ACCOUNT as `account_id`, oldest
    first.

    Walks BOTH directions — an id can be handed a chain in the middle of, and
    which end the caller happens to hold is arbitrary. Ordered by following the
    hops forward from whichever ids nothing points at; ids the ordering cannot
    place (a fork, a cycle, an orphan half of a pruned pair) are appended
    sorted rather than dropped, because a chain that silently omits an id is
    exactly the data loss this exists to prevent.

    Returns `[account_id]` for an account that has never been re-linked, which
    is every account on most installs — so callers need no special case."""
    account_id = (account_id or '').strip()
    if not account_id:
        return []
    try:
        edges = _hops()
    except Exception:  # noqa: BLE001 - a chain walk never breaks its caller
        db.session.rollback()
        log.warning('could not read the account id chain', exc_info=True)
        return [account_id]
    if not edges:
        return [account_id]
    forward: dict[str, set] = {}
    backward: dict[str, set] = {}
    for previous, current in edges:
        forward.setdefault(previous, set()).add(current)
        backward.setdefault(current, set()).add(previous)
    seen = {account_id}
    frontier = [account_id]
    for _ in range(MAX_CHAIN_DEPTH):
        if not frontier:
            break
        nxt = set()
        for node in frontier:
            nxt |= forward.get(node, set())
            nxt |= backward.get(node, set())
        frontier = sorted(nxt - seen)
        seen.update(frontier)
    if len(seen) == 1:
        return [account_id]
    # Order it: start from the ids in the chain nothing in the chain points at,
    # then follow the hops forward. `placed` is the cycle guard.
    heads = sorted(i for i in seen if not (backward.get(i, set()) & seen))
    ordered: list[str] = []
    placed = set()
    queue = heads or sorted(seen)
    while queue:
        node = queue.pop(0)
        if node in placed:
            continue
        placed.add(node)
        ordered.append(node)
        queue.extend(sorted(forward.get(node, set()) & seen))
    ordered.extend(sorted(seen - placed))
    return ordered


def current_account_id(account_id: str) -> str:
    """The id this real account goes by NOW — the last hop of its chain."""
    chain = chain_for(account_id)
    return chain[-1] if chain else (account_id or '')


def mapped_account_for(account_id: str) -> PlaidAccount | None:
    """The PlaidAccount row that currently CARRIES the ERPNext mapping for this
    Plaid id: itself, or the row that inherited the mapping on a re-link.

    THE BUG THIS EXISTS FOR. `adopt` nulls the donor's
    `erpnext_bank_account_name` — it has to, or two rows would push into one
    Bank Account and double every transaction in the overlap. But anchors,
    statements and transactions recorded BEFORE the re-link keep the dead
    account_id, so anything that resolves "which Bank Account does this row
    belong to" by loading that one row gets None, and the pre-relink half of
    the history silently never reaches ERPNext. It reads as an unmapped
    account, which is a mapping decision nobody made.

    Prefers the row asked for when it is itself mapped, so the ordinary case
    costs one query and the chain is only walked when there is a reason to."""
    account_id = (account_id or '').strip()
    if not account_id:
        return None
    account = PlaidAccount.query.filter_by(account_id=account_id).first()
    if account is not None and (account.erpnext_bank_account_name or '').strip():
        return account
    chain = chain_for(account_id)
    if len(chain) <= 1:
        return account
    # Newest first: a twice-relinked account's mapping is on the live row, and
    # asking for the OLDEST mapped row would resurrect a stale docname.
    for other_id in reversed(chain):
        if other_id == account_id:
            continue
        other = PlaidAccount.query.filter_by(account_id=other_id).first()
        if other is not None \
                and (other.erpnext_bank_account_name or '').strip():
            log.debug('plaid account %s resolves its ERPNext mapping through '
                      're-linked %s', account_id, other_id)
            return other
    return account


# ── repointing ERPNext ──────────────────────────────────────────────────────

def repoint_erpnext_bank_account(client, account: PlaidAccount) -> bool:
    """Rewrite the `plaid_account_id` custom field on the mapped ERPNext Bank
    Account to this account's CURRENT Plaid id.

    Without this the adoption is only half done. That custom field is the dedup
    key `_find_bank_account` filters on, so an adopted account whose ERPNext
    record still carries the dead id looks unmapped to ERPNext — and the next
    import tries to create a second Bank Account whose docname collides with the
    existing one, which fails the import outright.

    Best-effort by design: the local adoption is already committed and correct,
    and this is retried on the next sync if ERPNext is unreachable now."""
    from .erpnext_accounts import BANK_ACCOUNT_DT, CUSTOM_FIELD_DT
    from .erpnext_accounts import is_doctype_unavailable
    from .erpnext_client import ERPNextAPIError, ERPNextError
    name = (account.erpnext_bank_account_name or '').strip()
    if client is None or not name or is_doctype_unavailable(CUSTOM_FIELD_DT):
        return False
    try:
        existing = client.get_doc(BANK_ACCOUNT_DT, name)
    except (ERPNextAPIError, ERPNextError) as e:
        log.info('could not read Bank Account %s to repoint it: %s', name,
                 str(e)[:200])
        return False
    if existing is None:
        return False
    if (existing.get('plaid_account_id') or '') == account.account_id:
        return False   # already correct — no write
    try:
        client.update_doc(BANK_ACCOUNT_DT, name,
                          {'plaid_account_id': account.account_id})
    except (ERPNextAPIError, ERPNextError) as e:
        log.warning('could not repoint Bank Account %s at %s: %s', name,
                    account.account_id, str(e)[:200])
        return False
    log.info('repointed ERPNext Bank Account %s at re-linked account %s',
             name, account.account_id)
    return True


def repoint_adopted_accounts(client) -> int:
    """Repoint every adopted account whose ERPNext Bank Account still carries a
    dead Plaid id. Returns the count rewritten.

    Runs from the sync path so an adoption that happened while ERPNext was down
    still converges. Cheap in the steady state: one GET per adopted account, and
    only accounts that have ever superseded another are considered."""
    if client is None:
        return 0
    superseded_ids = [row.superseded_by_account_id for row in
                      PlaidAccount.query.filter(
                          PlaidAccount.superseded_by_account_id.isnot(None)).all()
                      if row.superseded_by_account_id]
    if not superseded_ids:
        return 0
    heirs = (PlaidAccount.query
             .filter(PlaidAccount.account_id.in_(superseded_ids),
                     PlaidAccount.erpnext_bank_account_name.isnot(None))
             .all())
    return sum(1 for heir in heirs
               if repoint_erpnext_bank_account(client, heir))
