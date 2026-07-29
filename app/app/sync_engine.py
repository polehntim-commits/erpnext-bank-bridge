# SPDX-License-Identifier: MIT
"""The sync engine: Plaid /transactions/sync → local mirror → ERPNext.

Two phases per Item, each independently logged to PlaidSyncLog:

  1. PULL  (direction='plaid_pull')
     Loop /transactions/sync while has_more, applying Plaid's added / modified /
     removed lists to the local BankTransaction table and advancing the Item's
     cursor. Cursor-based, so each poll only pulls the delta. Also refreshes the
     Item's accounts (balances) so the dashboard stays current — but only at most
     once per ACCOUNT_REFRESH_INTERVAL_HOURS, since that billable /accounts/get
     powers the dashboard only (ERPNext reconciles on amounts, not balances).

  2. PUSH  (direction='erpnext_push')
     Post every settled local row to ERPNext (see app/erpnext_bank.py).

Row state machine (uses existing columns, no extra flags):
  * posted_at IS NULL                       → pending ERPNext work (the queue)
  * removed=True                            → Plaid removed it → cancel the doc
  * erpnext_bank_transaction_id set + NULL  → a MODIFIED repost: cancel old,
    posted_at + not removed                   create+submit a replacement
  * no erpnext id + NULL posted_at          → a fresh create+submit
Once the ERPNext state is settled, posted_at is stamped so the row leaves the
queue. A row whose account isn't mapped/enabled stays pending (posted_at NULL)
and posts automatically on a later run once it's mapped — no error, no dup.

Everything is idempotent: re-applying the same Plaid transaction updates one
local row (unique on plaid_transaction_id), and every ERPNext create first
looks for an existing Bank Transaction with the same reference_number.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

from flask import current_app

from . import audit
from . import db
from . import categorization
from . import erpnext_accounts
from . import erpnext_bank
from . import erpnext_settings
from . import intercompany
from . import loans
from . import plaid_settings
from . import reconnect
from . import revaluation
from . import crypto
from .erpnext_client import ERPNextAPIError, ERPNextConfigError, ERPNextError
from .models import (BankTransaction, PlaidAccount, PlaidItem, PlaidSyncLog)
from .plaid_client import PlaidClient, PlaidConfigError, PlaidError

log = logging.getLogger('bankbridge.sync')


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(s):
    if not s:
        return None
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(str(s)[:10]).date()
    except (ValueError, TypeError):
        return None


def _plaid_calls_today(item_id: str) -> int:
    """Count today's (UTC) real Plaid pull actions logged for one Item — the
    basis for the optional per-Item daily call brake. `skipped` markers (rows
    the brake itself wrote) are excluded so they never inflate the count."""
    # Naive UTC midnight — PlaidSyncLog.at is stored naive-UTC on both SQLite
    # and Postgres, so compare against a matching naive value.
    today0 = datetime.now(timezone.utc).replace(
        tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    return (PlaidSyncLog.query
            .filter(PlaidSyncLog.direction == 'plaid_pull',
                    PlaidSyncLog.item_id == item_id,
                    PlaidSyncLog.status != 'skipped',
                    PlaidSyncLog.at >= today0)
            .count())


def _daily_call_brake_hit(item_id: str) -> bool:
    """True when PLAID_MAX_CALLS_PER_DAY is set and this Item has reached it
    today. Off (always False) when the limit is 0/unset. Logs + records a
    `skipped` sync-log row on the transition so the operator sees why a poll was
    held back."""
    try:
        limit = int(current_app.config.get('PLAID_MAX_CALLS_PER_DAY', 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return False
    if _plaid_calls_today(item_id) < limit:
        return False
    log.warning('[brake] item %s reached PLAID_MAX_CALLS_PER_DAY=%d — '
                'skipping this pull', item_id, limit)
    _log(item_id, 'plaid_pull', 0, 'skipped',
         f'daily Plaid call brake hit (limit={limit})')
    return True


def _log(item_id: str, direction: str, count: int, status: str,
         error_message: str = '') -> None:
    """Persist one PlaidSyncLog row. Best-effort — never masks the real outcome."""
    try:
        db.session.add(PlaidSyncLog(
            item_id=(item_id or '')[:120], direction=direction, count=count,
            status=status, error_message=(error_message or None)))
        db.session.commit()
    except Exception:  # pragma: no cover - defensive
        db.session.rollback()
        log.warning('failed to write PlaidSyncLog row', exc_info=True)


# ── structured failure codes (v0.8.5) ──────────────────────────────────
#
# Every failure a sync can surface gets a CODE and a REMEDY, not just a
# message. The reason is operational: a sync that half-failed is answered by
# retrying the account that broke, and "which account, and is retrying even the
# right move?" is not a question a free-text string can be relied on to answer.
# A code is also something the next release can act on — a `plaid_error` that
# keeps recurring on one Item is a stale token, an `invest_je_failed` that keeps
# recurring on one brokerage is a chart-of-accounts gap. The failure teaches, or
# it was wasted.
#
# Codes are stable strings; callers may switch on them. New codes get appended,
# existing ones are not renamed.
SYNC_ERROR_REMEDIES = {
    'plaid_not_configured':
        'Set the Plaid client id and secret on /admin/plaid_settings.',
    'plaid_error':
        'A transient Plaid failure clears on the next run; a persistent one is '
        "on this Item — see its last error on /admin/accounts.",
    'item_needs_reauth':
        'The bank wants a human to sign in again. Use Reconnect on '
        '/admin/accounts; nothing here can clear it, and polling meanwhile just '
        'spends billable calls.',
    'item_disconnected':
        "This Item's access token was removed at Plaid — re-link the bank.",
    'daily_call_brake':
        'PLAID_MAX_CALLS_PER_DAY is spent for this Item today. It resumes at '
        'UTC midnight, or raise the limit.',
    'erpnext_not_configured':
        'Nothing can post until ERPNext is configured — set the URL, API key '
        'and secret on /admin/erpnext_settings (or with set_erpnext_config). '
        'Transactions still mirror locally and post on a later run.',
    'account_not_found':
        'Check the account_id against get_account_topology.',
    'account_unmapped':
        'Map the Plaid account to an ERPNext Bank Account on /admin/accounts. '
        'Its mirrored rows post automatically on the next run once mapped.',
    'company_drift':
        "The mapped Bank Account lives under a different ERPNext Company than "
        "the account's owning Company. Nothing posted; correct one of the two.",
    'bank_transaction_push_failed':
        'The row keeps posted_at NULL and retries on the next run — the '
        "message is ERPNext's own.",
    'invest_je_posting_disabled':
        'Investment JE posting is off for this Item; turn it on with '
        'enable_je_posting (or on /admin/accounts) and re-run.',
    'invest_je_failed':
        'Usually a chart-of-accounts gap on this brokerage. Scope a retry to '
        'this account rather than re-running the whole sweep.',
    'revaluation_failed':
        'Mark-to-market is best-effort and never blocks a sync; the mirrored '
        'transactions are correct regardless.',
    'loan_accrual_failed':
        'Interest accrual is best-effort; the principal half is booked from '
        'the chequing side by an ordinary rule.',
    'intercompany_failed':
        'Cross-Item transfer pairing is best-effort and re-runs on the next '
        'sync — no transfer is lost, only unpaired for now.',
}

# How many per-run error entries a single bucket keeps. A brokerage whose chart
# of accounts is missing can fail every one of 4,000 trades; returning 4,000
# identical objects would bury the ONE fact the operator needs. The count is
# never truncated, only the detail.
MAX_ERRORS_REPORTED = 10


def _err(code: str, message, **fields) -> dict:
    """One structured failure: code, message, remedy, plus whatever identifiers
    the caller has (item_id, account_id, transaction_id)."""
    out = {'code': code, 'message': str(message)[:2000],
           'remedy': SYNC_ERROR_REMEDIES.get(code, '')}
    out.update({k: v for k, v in fields.items() if v})
    return out


def _collect(errors: list, entry: dict) -> list:
    """Append one error, capped at MAX_ERRORS_REPORTED. The cap is announced
    rather than silent — a truncated list that looked complete would be worse
    than no list."""
    if len(errors) < MAX_ERRORS_REPORTED:
        errors.append(entry)
    elif len(errors) == MAX_ERRORS_REPORTED:
        errors.append({'code': 'errors_truncated',
                       'message': f'only the first {MAX_ERRORS_REPORTED} '
                                  'errors are detailed; the counts above are '
                                  'complete',
                       'remedy': ''})
    return errors


# ── client construction ────────────────────────────────────────────────

def get_plaid_client() -> PlaidClient:
    """Build a Plaid client from the merged settings + active secret."""
    return PlaidClient.from_settings(plaid_settings.load(),
                                     plaid_settings.active_secret())


def get_erp_client_or_none():
    """An ERPNext client if the connection is configured, else None (so a pull
    can still run and mirror locally when ERPNext isn't wired yet)."""
    if not erpnext_settings.is_configured():
        return None
    try:
        return erpnext_bank.get_client()
    except ERPNextConfigError:
        return None


# ── account refresh ────────────────────────────────────────────────────

def _as_utc(dt: datetime) -> datetime:
    """PlaidAccount.updated_at round-trips through the DB as naive UTC while
    _now() is tz-aware; coerce a naive value to UTC so the two can be
    subtracted without raising TypeError."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _should_refresh_accounts(item: PlaidItem) -> bool:
    """Balances are dashboard-only, so skip the billable /accounts/get on most
    polls. Refresh at most once per ACCOUNT_REFRESH_INTERVAL_HOURS (default 24),
    and always when the Item has no cached accounts yet (first sync / new acct).
    Set the interval to 0 to opt back into every-poll refresh."""
    try:
        interval = max(0, int(
            current_app.config.get('ACCOUNT_REFRESH_INTERVAL_HOURS', 24) or 0))
    except (TypeError, ValueError):
        interval = 24
    if interval == 0:
        return True
    newest = (db.session.query(db.func.max(PlaidAccount.updated_at))
              .filter(PlaidAccount.item_id == item.item_id).scalar())
    if newest is None:
        return True
    return (_now() - _as_utc(newest)).total_seconds() / 3600.0 >= interval


def refresh_accounts(item: PlaidItem, plaid_client: PlaidClient,
                     access_token: str) -> int:
    """Upsert the Item's accounts (idempotent on account_id); refresh cached
    balances. Preserves operator-set erpnext mapping + sync toggle. Returns the
    account count."""
    accounts = plaid_client.get_accounts(access_token)
    adopted: list = []
    for a in accounts:
        acct_id = a.get('account_id')
        if not acct_id:
            continue
        acct = PlaidAccount.query.filter_by(account_id=acct_id).first()
        fresh = acct is None
        if acct is None:
            acct = PlaidAccount(account_id=acct_id, item_id=item.item_id)
            db.session.add(acct)
            # v0.4.0: a newly-seen account inherits its Item's owning Company at
            # link time. Existing accounts keep any per-account override across
            # refreshes (we never clobber a correction).
            if item.owning_company and not acct.owning_company:
                acct.owning_company = item.owning_company
        acct.name = a.get('name', '') or ''
        acct.official_name = a.get('official_name', '') or ''
        acct.mask = a.get('mask', '') or ''
        acct.type = a.get('type', '') or ''
        acct.subtype = a.get('subtype', '') or ''
        acct.balance_available = a.get('balance_available')
        acct.balance_current = a.get('balance_current')
        cur = a.get('iso_currency_code', 'USD') or 'USD'
        acct.iso_currency_code = cur
        acct.currency = cur
        # v0.4.0: re-derive balance-only (investment) each refresh from the live
        # type/subtype, so a reclassification flips the flag either way.
        # v0.4.14 · loans join investments as balance-only. The reason differs
        # and matters: an investment has no transactions to fetch, whereas a
        # loan HAS them and they must not be posted — the payment is booked from
        # the chequing side where the money actually moved, and posting the
        # loan's own copy of the same event would double-count every payment.
        acct.balance_only = (
            erpnext_accounts.is_investment_type(acct.type, acct.subtype)
            or erpnext_accounts.is_loan_type(acct.type, acct.subtype))
        acct.updated_at = _now()
        # v0.4.11 · a newly-seen account at a bank we have linked BEFORE is
        # usually the same real account under a new Plaid id — a re-link, not a
        # new account. Adopt the retired row's mapping when exactly one
        # candidate matches, so the operator doesn't re-map by hand (and, more
        # importantly, so a second opening balance can't be booked). Deferred
        # until after the fields above are set: the fingerprint reads mask,
        # type and subtype. See app/reconnect.py.
        if fresh:
            adopted.append(acct)
    db.session.commit()
    for acct in adopted:
        reconnect.adopt_if_unambiguous(acct, item)
    return len(accounts)


# ── local upserts from a Plaid sync page ───────────────────────────────

def _upsert_txn(t: dict, is_modification: bool) -> BankTransaction:
    """Insert or update one local BankTransaction from a normalized Plaid txn.

    Idempotency hinges on the source list:
      * `added` (is_modification=False) — a NEW transaction. A re-delivery of an
        already-seen `added` id (cursor replay) must NOT re-post, so we leave a
        posted row's posted_at intact. A fresh row is born with posted_at NULL
        and gets pushed.
      * `modified` (is_modification=True) — Plaid corrected the transaction, so
        we re-queue it (posted_at=NULL) while keeping any existing ERPNext
        docname; the push phase then cancels the stale doc and posts a
        replacement.
    """
    tid = t.get('transaction_id')
    row = BankTransaction.query.filter_by(plaid_transaction_id=tid).first()
    is_new = row is None
    if is_new:
        row = BankTransaction(plaid_transaction_id=tid,
                              account_id=t.get('account_id'))
        db.session.add(row)
    row.account_id = t.get('account_id') or row.account_id
    row.amount = float(t.get('amount', 0.0) or 0.0)
    row.iso_currency_code = t.get('iso_currency_code', 'USD') or 'USD'
    row.date = _parse_date(t.get('date'))
    row.name = (t.get('name', '') or '')[:500]
    row.merchant_name = (t.get('merchant_name', '') or '')[:255]
    row.category = (t.get('category', '') or '')[:255]
    row.pending = bool(t.get('pending', False))
    row.removed = False
    if is_new or is_modification:
        # Re-queue for the ERPNext push phase. Keep any existing
        # erpnext_bank_transaction_id so push knows to cancel+replace.
        row.posted_at = None
        row.sync_error = None
    row.updated_at = _now()
    return row


def _mark_removed(tid: str) -> str:
    """Mark one mirrored row removed. Returns the account it belonged to (or ''
    when the id is unknown) so the caller can attribute the removal per account
    — a removal carries no account_id of its own in Plaid's payload."""
    row = BankTransaction.query.filter_by(plaid_transaction_id=tid).first()
    if row is None:
        return ''
    row.removed = True
    row.posted_at = None       # re-queue for the cancel path
    row.updated_at = _now()
    return row.account_id or ''


def _item_all_balance_only(item: PlaidItem) -> bool:
    """True when the Item has at least one account and every one is balance-only
    (an investment). Such an Item has no transactions to pull, so the sync loop
    skips /transactions/sync for it and just refreshes balances."""
    accts = PlaidAccount.query.filter_by(item_id=item.item_id).all()
    return bool(accts) and all(a.balance_only for a in accts)


def _pull_bucket(by_account: dict, account_id: str) -> dict:
    """The per-account counter bucket in a pull's `by_account` breakdown.

    v0.8.5 · the pull is per ITEM (one Plaid cursor per Item), but an operator
    asking `sync_now` what a run did asks per ACCOUNT — an Item carrying a
    chequing account beside a brokerage tells them nothing at the Item level.
    Every transaction already names its account, so the breakdown costs a dict
    write and nothing else."""
    return by_account.setdefault(account_id or '',
                                 {'added': 0, 'modified': 0, 'removed': 0})


def pull_item(item: PlaidItem, plaid_client: PlaidClient) -> dict:
    """Pull the delta for one Item, applying added/modified/removed locally and
    advancing the cursor. Returns a stats dict — Item totals plus a per-account
    `by_account` breakdown (v0.8.5). Logs one plaid_pull row."""
    access_token = crypto.decrypt(item.access_token_encrypted)
    stats = {'added': 0, 'modified': 0, 'removed': 0, 'accounts': 0,
             'by_account': {}}
    if _should_refresh_accounts(item):
        try:
            stats['accounts'] = refresh_accounts(item, plaid_client, access_token)
        except PlaidError as e:
            log.warning('account refresh failed for %s: %s', item.item_id, e)
        # v0.4.14 · pull the lender's own loan figures alongside the balance.
        # Only spends a call when this Item actually has a loan account, and
        # answers {} when the `liabilities` product isn't approved — in which
        # case the loan still imports and its balance is still tracked, only
        # the interest split is unavailable. Never raises.
        try:
            loans.refresh_liabilities(item, plaid_client, access_token)
        except Exception:  # pragma: no cover - refresh already swallows
            db.session.rollback()
            log.warning('liability refresh failed for %s', item.item_id,
                        exc_info=True)

    # v0.4.30 · investment holdings + transactions run on EVERY sync tick,
    # NOT gated by _should_refresh_accounts. The account-refresh throttle
    # (default 24h) is designed to save on /accounts/get calls whose
    # balance data is dashboard-only; investments data drives the v0.5.0
    # lot tracker, which needs current holdings + fresh trade history on
    # every sync so the strategy dashboard reflects the latest state.
    # Same fail-soft posture as liabilities: an Item without the
    # `investments` product answers {} and the sync continues.
    try:
        from . import investments
        investments.sync_investments_for_item(
            item, plaid_client, access_token)
    except Exception:  # pragma: no cover - defensive
        db.session.rollback()
        log.warning('investment sync failed for %s', item.item_id,
                    exc_info=True)

    # v0.4.0: an Item whose every account is balance-only (e.g. a 401k-only
    # institution) has no transactions to fetch — Plaid returns none without the
    # `investments` product — so skip the billable /transactions/sync entirely.
    if _item_all_balance_only(item):
        item.status = 'active'
        item.last_synced_at = _now()
        item.last_error = None
        item.updated_at = _now()
        db.session.commit()
        _log(item.item_id, 'plaid_pull', 0, 'success',
             f"balance-only item — skipped /transactions/sync "
             f"(accounts={stats['accounts']})")
        return stats

    cursor = item.cursor or None
    try:
        has_more = True
        while has_more:
            page = plaid_client.transactions_sync(access_token, cursor=cursor)
            for t in page['added']:
                _upsert_txn(t, is_modification=False)
                stats['added'] += 1
                _pull_bucket(stats['by_account'], t.get('account_id'))['added'] += 1
            for t in page['modified']:
                _upsert_txn(t, is_modification=True)
                stats['modified'] += 1
                _pull_bucket(stats['by_account'],
                             t.get('account_id'))['modified'] += 1
            for t in page['removed']:
                gone = _mark_removed(t.get('transaction_id'))
                stats['removed'] += 1
                _pull_bucket(stats['by_account'], gone)['removed'] += 1
            cursor = page['next_cursor'] or cursor
            has_more = page['has_more']
            # Persist cursor progress after each page so a mid-loop crash
            # doesn't re-pull everything.
            item.cursor = cursor
            db.session.commit()
        item.status = 'active'
        item.last_synced_at = _now()
        item.last_error = None
        item.updated_at = _now()
        db.session.commit()
        # v0.4.11 · a sync that completed is proof the link works, whatever a
        # stale webhook or an earlier failure claimed. Self-healing matters
        # here: a PENDING_EXPIRATION warning that the bank later resolves on
        # its own must not park the Item permanently.
        reconnect.clear_reauth(item)
    except PlaidError as e:
        db.session.rollback()
        item = db.session.get(PlaidItem, item.id)
        if item is not None:
            item.status = 'error'
            item.last_error = str(e)[:2000]
            item.updated_at = _now()
            db.session.commit()
            # v0.4.11 · classify the failure. ITEM_LOGIN_REQUIRED and friends
            # need a human, not a retry, so park the Item rather than paying to
            # rediscover this every poll. Deriving it from the error text here
            # — as well as from the ITEM webhook — is what makes the feature
            # work on an install with no public webhook URL, which is most of
            # them.
            code = reconnect.is_reauth_error(e)
            if code:
                reconnect.mark_needs_reauth(item, code, source='sync error')
        _log(item.item_id if item else '', 'plaid_pull', 0, 'failed', str(e))
        raise
    _log(item.item_id, 'plaid_pull',
         stats['added'] + stats['modified'] + stats['removed'], 'success',
         f"added={stats['added']} modified={stats['modified']} "
         f"removed={stats['removed']} accounts={stats['accounts']}")
    return stats


# ── ERPNext push ───────────────────────────────────────────────────────

def _eligible_account_map() -> dict:
    """account_id → PlaidAccount for accounts whose transactions should be
    posted to ERPNext: mapped, sync-enabled, and not balance-only.

    v0.4.14 adds the balance-only exclusion, and it is load-bearing rather than
    tidy. Investments never returned transactions, so their presence here was
    harmless; LOANS do return them. A mortgage payment is booked from the
    chequing side, where the money actually left — posting the loan account's
    own copy of the same event would double-count every payment. Balance-only
    accounts carry their position through their balance (see app/loans.py and
    app/revaluation.py), never through their transactions."""
    out = {}
    for a in PlaidAccount.query.all():
        if a.erpnext_bank_account_name and a.sync_enabled and not a.balance_only:
            out[a.account_id] = a
    return out


def _never_posts_account_ids() -> list:
    """Accounts whose transactions are mirrored but INTENTIONALLY never posted:
    the balance-only ones (investments, and loans since v0.4.14).

    Held apart from "waiting on the operator" because the two look identical in
    the data and mean opposite things. A loan's transactions are not stuck —
    the payment is booked from the chequing side by design — and telling an
    operator that 40 mortgage rows are "waiting" would send them looking for a
    problem that isn't there."""
    return [a.account_id for a in
            PlaidAccount.query.filter(PlaidAccount.balance_only.is_(True)).all()]


def unpostable_pending_count() -> int:
    """How many mirrored transactions cannot post because their account isn't
    mapped + sync-enabled (v0.4.13).

    A COUNT, never a load. These rows are legitimate data — the transactions
    really happened, and they post the moment the account is mapped — but they
    can accumulate without bound, and before this release the push path LOADED
    every one of them on every run just to skip it.

    Reported so the situation is visible rather than silent. The realistic
    source is an account Plaid returns but ERPNext has no home for: a mortgage
    or student loan sharing an Item with a checking account, where `is_supported`
    refuses the loan (it is not a Bank Account in ERPNext's model) while
    /transactions/sync keeps returning its payments."""
    eligible = list(_eligible_account_map().keys())
    q = BankTransaction.query.filter(BankTransaction.posted_at.is_(None),
                                     BankTransaction.removed.is_(False))
    if eligible:
        q = q.filter(BankTransaction.account_id.notin_(eligible))
    # v0.4.14 · balance-only accounts are excluded: their rows are not waiting
    # on anything (see _never_posts_account_ids).
    never = _never_posts_account_ids()
    if never:
        q = q.filter(BankTransaction.account_id.notin_(never))
    return q.count()


def unpostable_by_account() -> dict:
    """account_id → count of mirrored-but-unpostable transactions, in ONE
    grouped query. Feeds the Accounts page hint, so an operator can see that a
    linked-but-unmapped account has history waiting rather than wondering where
    its transactions went."""
    eligible = list(_eligible_account_map().keys())
    q = (db.session.query(BankTransaction.account_id,
                          db.func.count(BankTransaction.id))
         .filter(BankTransaction.posted_at.is_(None),
                 BankTransaction.removed.is_(False)))
    if eligible:
        q = q.filter(BankTransaction.account_id.notin_(eligible))
    never = _never_posts_account_ids()
    if never:
        q = q.filter(BankTransaction.account_id.notin_(never))
    return {account_id: count
            for account_id, count in q.group_by(BankTransaction.account_id).all()}


def _push_row(erp_client, row: BankTransaction, account: PlaidAccount) -> bool:
    """Bring one local row's ERPNext doc to its intended state. Returns True if
    an ERPNext operation was performed (and posted_at stamped)."""
    bank_account = account.erpnext_bank_account_name
    if row.removed:
        if row.erpnext_bank_transaction_id:
            erpnext_bank.cancel_bank_transaction(
                erp_client, row.erpnext_bank_transaction_id)
        row.posted_at = _now()
        row.sync_error = None
        return True

    if row.erpnext_bank_transaction_id:
        # Modified repost: cancel the stale doc, create a fresh submitted one.
        erpnext_bank.cancel_bank_transaction(
            erp_client, row.erpnext_bank_transaction_id)
    name = erpnext_bank.create_bank_transaction(erp_client, row, bank_account)
    row.erpnext_bank_transaction_id = name
    row.posted_at = _now()
    row.sync_error = None
    return True


def _push_bucket(by_account: dict, account_id: str) -> dict:
    return by_account.setdefault(account_id or '',
                                 {'posted': 0, 'cancelled': 0, 'failed': 0,
                                  'drift': 0, 'dedup_skipped': 0})


def push_pending(erp_client, item_id: str = '',
                 only_account_ids: list | None = None) -> dict:
    """Post every pending local row (posted_at IS NULL) whose account is mapped
    + enabled. Rows for unmapped accounts are left pending (no error). Logs one
    erpnext_push row. `item_id` scopes the log line (optional).

    `only_account_ids` (v0.8.5) narrows the push to specific Plaid accounts —
    what `run_sync(account_id=...)` needs to keep a scoped run scoped. A push
    that quietly posted the other 40 accounts' backlog because the operator
    asked about one brokerage would be doing work nobody asked for, which is
    exactly the failure `sync_now` is meant not to have. None means "every
    eligible account", the pre-v0.8.5 behaviour and what the sweep uses."""
    stats = {'posted': 0, 'cancelled': 0, 'failed': 0, 'skipped': 0, 'drift': 0,
             'paired': 0, 'pairs_booked': 0, 'unpostable': 0,
             # v0.8.5 · bank-side JEs the categorization pipeline declined to
             # write because ERPNext already carried one for the same Bank
             # Transaction (app/je_dedup.py). Reported rather than swallowed:
             # a silent skip and a silent success look identical in a summary,
             # and the whole point of the guard is that we get to see it work.
             'dedup_skipped': 0,
             'by_account': {}, 'errors': []}
    if erp_client is None:
        return stats
    eligible = _eligible_account_map()
    if only_account_ids is not None:
        scope = set(only_account_ids)
        eligible = {k: v for k, v in eligible.items() if k in scope}
    q = BankTransaction.query.filter(BankTransaction.posted_at.is_(None))
    # v0.4.13 · restrict the scan to accounts that can actually receive a
    # posting. This used to load EVERY pending row and skip the ineligible ones
    # in Python, which quietly got more expensive forever: a Plaid Item carrying
    # a mortgage alongside a checking account keeps returning loan transactions,
    # `is_supported` refuses to give a loan an ERPNext Bank Account, and so those
    # rows could never post — yet every push re-loaded all of them, fed their ids
    # into the intercompany pair detector, and incremented `skipped`.
    #
    # Filtering here rather than flagging the rows keeps the behaviour
    # SELF-HEALING: map the account (or re-enable sync) and its backlog becomes
    # eligible on the very next push, with no migration and no state to unwind.
    # The rows themselves are untouched — they are real mirrored transactions,
    # and dropping them would lose history the account's own import would want.
    if eligible:
        q = q.filter(BankTransaction.account_id.in_(list(eligible.keys())))
    else:
        # Nothing is mapped yet: there is provably nothing to post, and an
        # unbounded `IN ()` is not worth constructing.
        q = q.filter(db.false())
    pending = q.order_by(BankTransaction.date.asc()).all()
    # v0.4.1 · pair BEFORE categorizing. A transfer between two Companies the
    # operator owns must not be seen by the ordinary rules engine at all — so the
    # detection pass has to run ahead of the categorize_after_push calls below,
    # not after them. Detection is inert on a single-Company install and never
    # raises (see intercompany.run_detection).
    try:
        stats['paired'] = len(intercompany.detect_pairs(
            limit_transaction_ids=[r.plaid_transaction_id for r in pending]))
    except Exception:  # noqa: BLE001 — never fail a push on pair detection
        db.session.rollback()
        log.warning('intercompany detection failed before push', exc_info=True)
    # v0.4.0 multi-entity drift guard: only for accounts with an explicit owning
    # Company. Probed once per account per run (cached), and refused rows never
    # touch ERPNext, so a mis-set Company can't post into the wrong entity.
    drift_cache: dict = {}
    drift_audited: set = set()
    for row in pending:
        account = eligible.get(row.account_id)
        if account is None:
            stats['skipped'] += 1
            continue
        drift = _company_drift(erp_client, account, drift_cache)
        if drift is not None:
            _refuse_on_drift(row, account, drift, drift_audited)
            stats['drift'] += 1
            _push_bucket(stats['by_account'], row.account_id)['drift'] += 1
            _collect(stats['errors'], _err(
                'company_drift',
                f'owning Company {drift[0]!r} ≠ ERPNext Company {drift[1]!r} '
                f'on Bank Account {account.erpnext_bank_account_name!r}',
                account_id=row.account_id))
            continue
        try:
            was_removed = row.removed
            _push_row(erp_client, row, account)
            db.session.commit()
            if was_removed:
                stats['cancelled'] += 1
                _push_bucket(stats['by_account'],
                             row.account_id)['cancelled'] += 1
            else:
                stats['posted'] += 1
                _push_bucket(stats['by_account'], row.account_id)['posted'] += 1
                audit.record('bank_transaction_synced',
                             subject_type='BankTransaction',
                             subject_id=row.plaid_transaction_id,
                             after={'erpnext_bank_transaction_id':
                                    row.erpnext_bank_transaction_id,
                                    'amount': row.amount,
                                    'merchant_name': row.merchant_name,
                                    'account_id': row.account_id})
                # v0.3.0: auto-Supplier + rules-based JE. Best-effort and
                # self-guarding — a failure here never unwinds the posted row.
                gje = categorization.categorize_after_push(erp_client, row)
                if gje is not None and (gje.state or '') == 'dedup_skipped':
                    stats['dedup_skipped'] += 1
                    _push_bucket(stats['by_account'],
                                 row.account_id)['dedup_skipped'] += 1
        except (ERPNextAPIError, ERPNextError) as e:
            db.session.rollback()
            row_id, account_id = row.id, row.account_id
            row = db.session.get(BankTransaction, row_id)
            if row is not None:
                row.sync_error = str(e)[:2000]
                row.updated_at = _now()
                db.session.commit()
            stats['failed'] += 1
            _push_bucket(stats['by_account'], account_id)['failed'] += 1
            _collect(stats['errors'], _err(
                'bank_transaction_push_failed', e, account_id=account_id,
                transaction_id=(row.plaid_transaction_id if row else '')))
            log.warning('ERPNext push failed for %s: %s',
                        row.plaid_transaction_id if row else '?', e)
    # v0.4.1 · book the paired transfers AFTER the push, so each leg's Journal
    # Entry can reference the ERPNext Bank Transaction that was just created for
    # it (the reference_name on both lines).
    try:
        stats['pairs_booked'] = intercompany.generate_pending_journal_entries(
            erp_client)
    except Exception:  # noqa: BLE001 — never fail a push on pair booking
        db.session.rollback()
        log.warning('intercompany JE generation failed after push', exc_info=True)
    handled = stats['posted'] + stats['cancelled']
    status = 'failed' if stats['failed'] and not handled else 'success'
    # v0.4.13 · say how much is mirrored-but-unpostable. The rows are no longer
    # scanned (see the query above), so without this the situation would be
    # invisible instead of merely expensive — and "where did my mortgage
    # payments go?" deserves an answer in the log rather than silence.
    stats['unpostable'] = unpostable_pending_count()
    if stats['unpostable']:
        log.info('[push] %d mirrored transaction(s) cannot post yet — their '
                 'Plaid account is not mapped to an ERPNext Bank Account (or '
                 'has sync disabled). They post automatically once it is; see '
                 '/admin/accounts.', stats['unpostable'])
    _log(item_id, 'erpnext_push', handled, status,
         f"posted={stats['posted']} cancelled={stats['cancelled']} "
         f"failed={stats['failed']} skipped={stats['skipped']} "
         f"unpostable={stats['unpostable']} "
         f"drift={stats['drift']} paired={stats['paired']} "
         f"pairs_booked={stats['pairs_booked']}")
    return stats


def _company_drift(erp_client, account: PlaidAccount, cache: dict):
    """(expected, actual) owning-Company mismatch for a mapped account, or None.
    Caches the ERPNext-side probe per Bank Account name so a push run costs at
    most one extra GET per drifted-or-multi-entity account, and zero for an
    install that never chose an owning Company (explicit_owning_company == '')."""
    if not erpnext_accounts.explicit_owning_company(account):
        return None
    name = account.erpnext_bank_account_name or ''
    if name not in cache:
        cache[name] = erpnext_accounts.company_drift(erp_client, account)
    return cache[name]


def _refuse_on_drift(row: BankTransaction, account: PlaidAccount, drift: tuple,
                     audited: set) -> None:
    """Refuse to post one transaction whose Bank Account's ERPNext Company
    disagrees with the chosen owning Company. Stamps the row's sync_error (but
    leaves posted_at NULL so it retries once the drift is corrected) and records
    a company_drift_detected AuditEvent — once per drifted account per run."""
    expected, actual = drift
    row.sync_error = (f'company drift: owning Company is {expected!r} but ERPNext '
                      f'Bank Account {account.erpnext_bank_account_name!r} is under '
                      f'{actual!r}; refusing to post until corrected')[:2000]
    row.updated_at = _now()
    db.session.commit()
    key = account.account_id
    if key not in audited:
        audited.add(key)
        audit.record('company_drift_detected', subject_type='PlaidAccount',
                     subject_id=account.account_id,
                     after={'account_id': account.account_id,
                            'erpnext_bank_account': account.erpnext_bank_account_name,
                            'expected_company': expected, 'erpnext_company': actual},
                     notes=(f'refusing to push — owning Company {expected!r} ≠ '
                            f'ERPNext Company {actual!r}'))
    log.warning('company drift for %s: expected %r, ERPNext has %r — refusing push',
                account.account_id, expected, actual)


def retry_row(row_id: int) -> tuple[bool, str]:
    """Re-attempt the ERPNext push for a single local row (the per-row Retry
    button). Returns (ok, message)."""
    row = db.session.get(BankTransaction, row_id)
    if row is None:
        return False, 'transaction not found'
    account = PlaidAccount.query.filter_by(account_id=row.account_id).first()
    if account is None or not account.erpnext_bank_account_name:
        return False, 'account not mapped to an ERPNext Bank Account'
    if not account.sync_enabled:
        return False, 'sync is disabled for this account'
    erp_client = get_erp_client_or_none()
    if erp_client is None:
        return False, 'ERPNext not configured'
    try:
        row.posted_at = None
        _push_row(erp_client, row, account)
        db.session.commit()
        if not row.removed:
            categorization.categorize_after_push(erp_client, row)
        return True, f'posted as {row.erpnext_bank_transaction_id}'
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        row = db.session.get(BankTransaction, row_id)
        if row is not None:
            row.sync_error = str(e)[:2000]
            db.session.commit()
        return False, str(e)


# ── investment Journal Entries ─────────────────────────────────────────

def post_investment_jes(item: PlaidItem, erp_client,
                        only_account_id: str = '') -> dict:
    """v0.8.2 · emit Journal Entries for the investment transactions this Item's
    brokerage accounts have already mirrored locally. Returns aggregate
    {'posted', 'skipped', 'skipped_duplicate', 'failed'} plus a per-account
    `by_account` breakdown and structured `errors` (v0.8.5); never raises.

    `only_account_id` (v0.8.5) scopes the pass to one brokerage — what a scoped
    `run_sync` needs, and what makes a per-account retry after a partial failure
    cost one account's work instead of the Item's.

    THIS IS THE CALL THAT WAS MISSING. `invest_je.post_investments_for_account`
    has been complete since v0.5.1 and the operator-facing kill switch has been
    on /admin/accounts just as long, but no production code path ever invoked
    the writer — only the unit tests did. An Item with
    `invest_je_posting_enabled=True` therefore mirrored every trade into
    `security_transactions` and posted exactly nothing to ERPNext, which is
    indistinguishable from "there were no trades" unless you go read the table.

    Placement is deliberate. It runs here rather than beside the
    `investments.sync_investments_for_item` pull in `pull_item`, because that
    function has no ERPNext client — `pull_item(item, plaid_client)` mirrors
    Plaid locally and nothing more, by design. `sync_item` is the first frame
    where both halves exist: the transactions are in the DB (pull_item has
    returned) and `erp_client` is in hand. It runs BEFORE
    `revaluation.revalue_all` because the two are the same decision seen from
    opposite sides — revaluation skips mark-to-market on exactly the accounts
    this posts per-security entries for (see revaluation.revalue_account), and
    posting the real trades first keeps that ordering readable.

    Fail-soft, twice over: per-account so one brokerage's chart-of-accounts
    problem can't silence its sibling, and again around the whole pass so no JE
    failure can fail a sync that has already mirrored transactions correctly."""
    stats = {'posted': 0, 'skipped': 0, 'skipped_duplicate': 0,
             'dedup_skipped': 0, 'failed': 0, 'by_account': {}, 'errors': [],
             'notices': [], 'posting_enabled': False}
    if erp_client is None:
        return stats
    try:
        from . import invest_je
        if not invest_je.posting_enabled(item):
            # A NOTICE, not an error. Almost every Item has this switch off —
            # it is the designed default on a feature that writes real
            # accounting entries — so calling it a failure would mark every
            # healthy sweep on a normal install 'partial', and a status that is
            # always 'partial' tells the operator nothing when it matters. It is
            # still SAID, because "the investment JEs didn't post" needs an
            # answer, and this is usually the answer.
            stats['notices'].append(_err(
                'invest_je_posting_disabled',
                f'investment JE posting is off for item {item.item_id}',
                item_id=item.item_id))
            return stats
        stats['posting_enabled'] = True
        q = (PlaidAccount.query
             .filter(PlaidAccount.item_id == item.item_id,
                     PlaidAccount.type.in_(('investment', 'brokerage'))))
        if only_account_id:
            q = q.filter(PlaidAccount.account_id == only_account_id)
        for account in q.all():
            try:
                res = invest_je.post_investments_for_account(
                    erp_client, account.account_id)
            except Exception as e:  # noqa: BLE001 - per-account isolation
                db.session.rollback()
                log.warning('invest_je posting failed for %s (item=%s)',
                            account.mask or account.account_id, item.item_id,
                            exc_info=True)
                _collect(stats['errors'], _err(
                    'invest_je_failed', f'{type(e).__name__}: {e}',
                    item_id=item.item_id, account_id=account.account_id))
                continue
            res = res or {}
            for k in ('posted', 'skipped', 'skipped_duplicate',
                      'dedup_skipped', 'failed'):
                stats[k] += res.get(k, 0)
            stats['by_account'][account.account_id] = {
                k: res.get(k, 0) for k in
                ('posted', 'skipped', 'skipped_duplicate', 'dedup_skipped',
                 'failed')}
            if res.get('failed'):
                _collect(stats['errors'], _err(
                    'invest_je_failed',
                    f"{res['failed']} investment Journal Entr"
                    f"{'y' if res['failed'] == 1 else 'ies'} could not be "
                    'written for this account',
                    item_id=item.item_id, account_id=account.account_id))
    except Exception as e:  # noqa: BLE001 - outer guard: never fail a sync here
        db.session.rollback()
        log.warning('invest_je posting pass failed for %s', item.item_id,
                    exc_info=True)
        _collect(stats['errors'], _err('invest_je_failed',
                                       f'{type(e).__name__}: {e}',
                                       item_id=item.item_id))
    return stats


# ── top-level orchestration ────────────────────────────────────────────

def sync_item(item: PlaidItem, plaid_client: PlaidClient = None,
              erp_client=None, *, only_account_id: str = '',
              include_investments: bool = True) -> dict:
    """Pull + push one Item. Pull failures propagate; push failures are logged
    per-row and don't abort the pull's local mirror. The optional per-Item daily
    call brake (PLAID_MAX_CALLS_PER_DAY) short-circuits the pull before any Plaid
    call when the Item has hit its limit for the day.

    v0.8.5 · `only_account_id` narrows the ERPNext-facing work to one Plaid
    account (the pull is per-Item by construction — one cursor — so it is not
    narrowed, only its reporting is). `include_investments=False` skips the
    investment JE post and the mark-to-market revaluation that goes with it,
    leaving the bank-transaction sync exactly as it was; the investments PULL
    still runs, because it only mirrors and costs the same call either way."""
    started = time.monotonic()
    errors: list = []
    if _daily_call_brake_hit(item.item_id):
        return {'item_id': item.item_id, 'skipped': 'max_calls_per_day',
                'pull': {}, 'push': {}, 'elapsed_seconds': 0.0,
                'errors': [_err('daily_call_brake',
                                'per-Item daily Plaid call limit reached',
                                item_id=item.item_id)]}
    plaid_client = plaid_client or get_plaid_client()
    pull_stats = pull_item(item, plaid_client)
    if erp_client is None:
        erp_client = get_erp_client_or_none()
    scope = [only_account_id] if only_account_id else None
    push_stats = (push_pending(erp_client, item.item_id, only_account_ids=scope)
                  if erp_client else {})
    errors.extend(push_stats.get('errors', []))
    if erp_client is None:
        errors.append(_err('erpnext_not_configured',
                           'ERPNext is not configured — transactions were '
                           'mirrored locally but nothing was posted',
                           item_id=item.item_id))
    notices: list = []
    invest_stats = {'posted': 0, 'skipped': 0, 'skipped_duplicate': 0,
                    'failed': 0, 'by_account': {}, 'errors': [], 'notices': []}
    # v0.4.0: mirror refreshed balances onto balance-only investment accounts'
    # ERPNext Bank Accounts. Best-effort — never fails the sync.
    if erp_client is not None:
        try:
            erpnext_accounts.refresh_investment_balances(erp_client)
        except (ERPNextAPIError, ERPNextError):
            log.warning('investment balance refresh failed for %s',
                        item.item_id, exc_info=True)
        # v0.4.11 · finish any adoption whose ERPNext half couldn't be done at
        # the time (ERPNext down, or not configured yet). Until the Bank
        # Account's `plaid_account_id` names the CURRENT account, ERPNext's own
        # dedup can't see it — and the next import would try to create a
        # duplicate Bank Account and fail. Cheap: only ever-superseded accounts
        # are considered, and a correct one costs a GET and no write.
        try:
            reconnect.repoint_adopted_accounts(erp_client)
        except (ERPNextAPIError, ERPNextError):
            log.warning('repointing adopted accounts failed for %s',
                        item.item_id, exc_info=True)
        # v0.8.2 · post the investment transactions this Item just mirrored as
        # per-security Journal Entries, when the operator has opted the Item in.
        # Gated, idempotent and total — see post_investment_jes above for why it
        # lives here and not next to the investments pull.
        if include_investments:
            invest_started = time.monotonic()
            invest_stats = post_investment_jes(
                item, erp_client, only_account_id=only_account_id)
            invest_stats['elapsed_seconds'] = round(
                time.monotonic() - invest_started, 3)
            errors.extend(invest_stats.get('errors', []))
            notices.extend(invest_stats.get('notices', []))
            # v0.4.12 · mark investment accounts to market. Runs right after the
            # balance refresh above, because it measures against the balance
            # that refresh just cached. Posts a Draft for the DELTA only, and
            # only when a baseline is known — see app/revaluation.py.
            # Best-effort: a chart-of-accounts problem must never fail a sync
            # that has already mirrored transactions correctly.
            try:
                revaluation.revalue_all(erp_client)
            except Exception as e:  # pragma: no cover - revalue_all is total
                log.warning('investment revaluation failed for %s',
                            item.item_id, exc_info=True)
                errors.append(_err('revaluation_failed',
                                   f'{type(e).__name__}: {e}',
                                   item_id=item.item_id))
        # v0.4.14 · book the interest each loan has accrued since last time.
        # The other half of a loan payment — the principal — is booked from the
        # chequing side by an ordinary categorization rule; see app/loans.py for
        # why the two halves are separate entries.
        try:
            loans.accrue_all(erp_client)
        except Exception as e:  # pragma: no cover - accrue_all is already total
            log.warning('loan interest accrual failed for %s', item.item_id,
                        exc_info=True)
            errors.append(_err('loan_accrual_failed', f'{type(e).__name__}: {e}',
                               item_id=item.item_id))
    return {'item_id': item.item_id, 'pull': pull_stats, 'push': push_stats,
            'invest_je': invest_stats, 'errors': errors, 'notices': notices,
            'elapsed_seconds': round(time.monotonic() - started, 3)}


class SyncScopeError(Exception):
    """The caller asked to sync something that does not exist (v0.8.5). Raised
    BEFORE any Plaid call, so a typo costs nothing and is answerable — the one
    failure here that is the caller's to fix rather than the operator's."""


def _syncable_items():
    """Every Item a sweep may poll, and the ones deliberately held back.

    v0.4.7 · a disconnected Item's access_token was invalidated at Plaid by
    /item/remove, so every call for it would fail — skip it here rather than
    burn a request and record an error per poll. `isnot(True)` (not
    `.is_(False)`) so a row that predates the column and somehow read back NULL
    still counts as connected; the migration backfills false, this is
    belt-and-braces.

    v0.4.11 · an Item the bank wants re-authenticated is skipped for exactly the
    reason above: every call for it fails until a HUMAN signs in again, so
    polling it just burns a billable request and writes an error row per cycle.
    The operator sees it on /admin/accounts with a Reconnect button; nothing
    here can clear the state, so nothing here should keep paying to rediscover
    it."""
    items = (PlaidItem.query
             .filter(PlaidItem.status != 'revoked')
             .filter(PlaidItem.disconnected.isnot(True))
             .filter(PlaidItem.needs_reauth.isnot(True)).all())
    parked = (PlaidItem.query
              .filter(PlaidItem.needs_reauth.is_(True),
                      PlaidItem.disconnected.isnot(True)).all())
    return items, parked


def _account_summary(account: PlaidAccount) -> dict:
    """The per-account row every `run_sync` result carries, zeroed."""
    return {'account_id': account.account_id, 'mask': account.mask or '',
            'name': account.name or '', 'item_id': account.item_id,
            'type': account.type or '', 'subtype': account.subtype or '',
            'erpnext_bank_account': account.erpnext_bank_account_name or '',
            'sync_enabled': bool(account.sync_enabled),
            'balance_only': bool(account.balance_only),
            'transactions_fetched': {'added': 0, 'modified': 0, 'removed': 0,
                                     'total': 0},
            'bank_transactions_posted': 0, 'bank_transactions_cancelled': 0,
            'bank_transactions_failed': 0, 'bank_transactions_refused': 0,
            # v0.8.5 · the two dedup counters, kept APART on purpose. "3 skipped"
            # is not actionable; "3 bank-side, 0 investment-side" names the
            # pipeline that tried to write a duplicate, which is the first thing
            # the v0.8.4 cleanup had to work out by hand.
            'bank_transactions_dedup_skipped': 0,
            'investment_jes_drafted': 0, 'investment_jes_skipped_duplicate': 0,
            'investment_jes_dedup_skipped': 0,
            'investment_jes_skipped': 0, 'investment_jes_failed': 0,
            # The elapsed time of the ITEM sync this account's work rode on,
            # named for what it is. The Plaid pull is per-Item — one cursor —
            # so there is no honest per-account slice of it, and a made-up
            # division would be a worse answer than a labelled shared one.
            'item_elapsed_seconds': 0.0,
            'errors': []}


def _dry_run(accounts_by_id: dict, items: list, parked: list,
             include_investments: bool, erp_client) -> dict:
    """What a real run WOULD do, computed entirely from the local mirror.

    Deliberately spends NO Plaid call, which is why it cannot answer "how many
    new transactions are at the bank" — nothing can, without paying for the
    answer, and a preview that quietly bills the operator is not a preview. What
    it CAN answer is everything the run would do with what is already here: the
    queue that would post, the removals that would cancel, the trades that would
    become Journal Entries, and which Items would be polled at all."""
    from .models import GeneratedJournalEntry, SecurityTransaction
    eligible = _eligible_account_map()
    out = []
    for account in accounts_by_id.values():
        row = _account_summary(account)
        base = BankTransaction.query.filter(
            BankTransaction.account_id == account.account_id,
            BankTransaction.posted_at.is_(None))
        row['would_post'] = base.filter(
            BankTransaction.removed.is_(False)).count() \
            if account.account_id in eligible else 0
        row['would_cancel'] = base.filter(
            BankTransaction.removed.is_(True)).count() \
            if account.account_id in eligible else 0
        row['unpostable_pending'] = 0 if account.account_id in eligible else \
            base.filter(BankTransaction.removed.is_(False)).count()
        if row['unpostable_pending'] and not account.balance_only:
            _collect(row['errors'], _err(
                'account_unmapped',
                f"{row['unpostable_pending']} mirrored transaction(s) cannot "
                'post — this account is not mapped to an ERPNext Bank Account '
                '(or sync is off for it)',
                account_id=account.account_id))
        row['would_draft_investment_jes'] = 0
        if include_investments and (account.type or '') in ('investment',
                                                            'brokerage'):
            posted = {itx for (itx,) in db.session.query(
                GeneratedJournalEntry.plaid_investment_transaction_id)
                .filter(GeneratedJournalEntry
                        .erpnext_journal_entry_name.isnot(None)).all() if itx}
            total = (SecurityTransaction.query
                     .filter_by(account_id=account.account_id).count())
            done = (SecurityTransaction.query
                    .filter(SecurityTransaction.account_id == account.account_id,
                            SecurityTransaction.plaid_investment_transaction_id
                            .in_(posted)).count() if posted else 0)
            row['would_draft_investment_jes'] = max(0, total - done)
        out.append(row)
    result = {
        'dry_run': True,
        'plaid_contacted': False,
        'items_that_would_sync': [i.item_id for i in items],
        'items_held_back': [{'item_id': i.item_id,
                             'institution': i.institution_name or '',
                             'reason': 'needs_reauth'} for i in parked],
        'accounts': out,
        'erpnext_configured': erp_client is not None,
        'note': 'Nothing was fetched, written or posted. Counts are what the '
                'local mirror already holds; how many NEW transactions Plaid '
                'would return cannot be known without spending the call.',
    }
    return result


def run_sync(*, account_id: str = '', include_investments: bool = True,
             dry_run: bool = False, plaid_client: PlaidClient = None,
             erp_client=None, actor: str = '') -> dict:
    """THE sync. One function, called by the /admin Sync Now button, the
    `sync_now` MCP tool and the scheduled poll alike (v0.8.5).

    It exists because there was no way to trigger a sync except a human at a
    browser. During the Cash Clearing cleanup on 2026-07-29, 77 settlement legs
    needed posting and every MCP path around it — trigger_reparse,
    reset_investment_drafts — either did the wrong work or refused; the only
    thing that would post them was the button. A $1M ledger correction stalled
    at the last mile on a click. That is the relay tax this server exists to
    remove, so the button's own logic moved here and both surfaces call it.

    KAIROS, NOT CHRONOS. This is a manual override: it does exactly what the
    caller asks, once, and decides nothing on a clock. It does not reschedule,
    does not extend its own scope, and does not "while we're here" any extra
    work. The scheduled poll's cadence stays where it is (services/scheduler.py)
    — an operator calling this has decided that NOW is the moment, and the
    function's job is to honour that decision and no more.

    FAILS FORWARD. One account's or one Item's failure never stops the others,
    and each surfaces as a structured entry with a code and a remedy (see
    SYNC_ERROR_REMEDIES) rather than a bare string, so a partial run is retried
    where it broke instead of from the top.

      account_id           scope to ONE Plaid account (its Item is polled, and
                           only that account's rows post). '' = every account.
      include_investments  also run the investment JE post + mark-to-market.
                           False leaves the bank-transaction sync untouched.
      dry_run              report what WOULD happen; contacts nothing.

    Returns a per-account summary, aggregate totals, structured errors and
    elapsed time. Never raises except SyncScopeError (unknown account_id)."""
    started = time.monotonic()
    account_id = (account_id or '').strip()
    errors: list = []
    # NOTICES are things worth saying that are not failures — an Item with
    # investment posting deliberately switched off is the standing example. They
    # are held apart from `errors` because `status` is computed from errors, and
    # a status that reads 'partial' on every healthy install is a status nobody
    # reads on the day it matters.
    notices: list = []

    # ── scope, resolved before anything is spent ────────────────────────────
    scoped_account = None
    if account_id:
        scoped_account = PlaidAccount.query.filter_by(
            account_id=account_id).first()
        if scoped_account is None:
            raise SyncScopeError(
                f'no Plaid account {account_id!r} — check get_account_topology')

    if erp_client is None:
        erp_client = get_erp_client_or_none()

    all_items, parked = _syncable_items()
    if scoped_account is not None:
        items = [i for i in all_items if i.item_id == scoped_account.item_id]
        parked = [i for i in parked if i.item_id == scoped_account.item_id]
        accounts = {scoped_account.account_id: scoped_account}
        if not items:
            # The account exists but its Item is held back. Say which, and why:
            # "0 accounts synced" with no reason is the answer that sends an
            # operator hunting through logs.
            held = db.session.query(PlaidItem).filter_by(
                item_id=scoped_account.item_id).first()
            code = ('item_needs_reauth' if held is not None and held.needs_reauth
                    else 'item_disconnected')
            errors.append(_err(code, f'the Item behind account {account_id} is '
                                     'not being polled', item_id=(
                                         held.item_id if held else ''),
                               account_id=account_id))
    else:
        items = all_items
        item_ids = {i.item_id for i in items}
        accounts = {a.account_id: a for a in PlaidAccount.query.all()
                    if a.item_id in item_ids}
    for held in parked:
        errors.append(_err('item_needs_reauth',
                           f'{held.institution_name or held.item_id} is parked '
                           'awaiting re-authentication and was not polled',
                           item_id=held.item_id))

    if dry_run:
        result = _dry_run(accounts, items, parked, include_investments,
                          erp_client)
        result.update({'scope': account_id or 'all',
                       'include_investments': include_investments,
                       'errors': _dedupe(errors), 'notices': _dedupe(notices),
                       'elapsed_seconds': round(time.monotonic() - started, 3)})
        return result

    # ── the real thing ─────────────────────────────────────────────────────
    # A CALLER-SUPPLIED client is authoritative: the scheduler, the API surface
    # and the tests all hand one in, and refusing them because the settings file
    # says "unconfigured" would break every one of those paths. The settings
    # check exists for the surfaces that pass nothing — the button and the tool
    # — where "Plaid is not configured" is the whole answer and going on to
    # construct a client would only turn it into a stack trace.
    if plaid_client is None:
        if not plaid_settings.is_configured():
            return _finish(started, account_id, include_investments, accounts,
                           [], errors + [_err('plaid_not_configured',
                                              'Plaid is not configured')], 0)
        try:
            plaid_client = get_plaid_client()
        except PlaidConfigError as e:
            return _finish(started, account_id, include_investments, accounts,
                           [], errors + [_err('plaid_not_configured', e)], 0)
    if erp_client is None and not items:
        # With Items to sync, each reports this for itself (and _dedupe collapses
        # the repeats); with none, nothing else would say it at all.
        errors.append(_err('erpnext_not_configured',
                           'ERPNext is not configured — this run mirrors Plaid '
                           'locally and posts nothing'))

    audit.record('sync_run_started', subject_type=None,
                 after={'items': len(items), 'scope': account_id or 'all',
                        'include_investments': include_investments},
                 notes=(f'{actor or "manual"} sync across {len(items)} item(s)'
                        + (f' scoped to {account_id}' if account_id else '')))

    summaries = {aid: _account_summary(a) for aid, a in accounts.items()}
    item_results = []
    for item in items:
        try:
            res = sync_item(item, plaid_client, erp_client,
                            only_account_id=account_id,
                            include_investments=include_investments)
        except (PlaidError, PlaidConfigError) as e:
            # Fail fast on THIS Item, keep going with the rest — one bank's
            # outage is not the other banks' problem, and the operator gets a
            # retriable scope rather than an all-or-nothing failure.
            log.warning('sync failed for item %s: %s', item.item_id, e)
            entry = _err('plaid_error', e, item_id=item.item_id)
            errors.append(entry)
            item_results.append({'item_id': item.item_id, 'error': str(e),
                                 'errors': [entry]})
            for row in summaries.values():
                if row['item_id'] == item.item_id:
                    row['errors'].append(entry)
            continue
        item_results.append(res)
        errors.extend(res.get('errors', []))
        notices.extend(res.get('notices', []))
        _attribute(summaries, res, item, scoped=bool(account_id))

    # v0.4.1 · a final cross-Item detection pass. Each Item pulls on its own
    # cursor, so the two legs of one transfer very often arrive in DIFFERENT
    # iterations of the loop above — the second Company's leg simply did not
    # exist yet when the first was pushed. This pass runs once every Item has
    # landed, which is the first moment both halves are visible together.
    #
    # SKIPPED on a scoped run, deliberately: detection is install-wide and would
    # book Journal Entries for accounts the caller did not ask about. A scoped
    # sync that quietly touched the other entity's books would be the exact
    # surprise `account_id` exists to prevent; the next full sweep pairs them.
    pairs = {'detected': 0, 'booked': 0}
    if erp_client is not None and not account_id:
        try:
            pairs = intercompany.run_detection(erp_client)
        except Exception as e:  # noqa: BLE001 - never fail a sync on pairing
            db.session.rollback()
            log.warning('cross-item pair detection failed', exc_info=True)
            errors.append(_err('intercompany_failed', f'{type(e).__name__}: {e}'))
    # v0.8.5 · THE CLOCK GATHERS; THE STATE DECIDES. A sync is a chronological
    # job, so it is the natural place to take the draft-JE reading — but nothing
    # here fires on the schedule. `observe_quietly` records the observation and
    # raises an alert ONLY on the transition into (or out of) a breached count.
    # That is the whole Kairos/Chronos split, in one call: the poll is the
    # heartbeat, the crossing is the event. It runs LAST, after every JE this
    # run intends to write has been written, so the number it records is the
    # queue the operator would actually see. Never raises.
    draft_health = None
    if erp_client is not None:
        from . import draft_health as draft_health_mod
        draft_health = draft_health_mod.observe_quietly(erp_client)
    return _finish(started, account_id, include_investments, summaries,
                   item_results, errors, len(items), pairs=pairs,
                   draft_health=draft_health, notices=notices)


def _attribute(summaries: dict, res: dict, item: PlaidItem,
               scoped: bool = False) -> None:
    """Fold one Item's result into the per-account summaries.

    `scoped` is a run pinned to one account_id: it must never grow a row for a
    different account, so an unknown id is dropped rather than looked up. On an
    UNSCOPED run the opposite is true — the accounts were read before the pull,
    and a FIRST sync creates them DURING it (refresh_accounts runs inside
    pull_item), so dropping unknown ids would report a brand-new bank's first
    fifty transactions as zero."""
    def row_for(aid):
        row = summaries.get(aid)
        if row is not None or scoped or not aid:
            return row
        account = PlaidAccount.query.filter_by(account_id=aid).first()
        if account is None:
            return None
        summaries[aid] = _account_summary(account)
        return summaries[aid]

    elapsed = float(res.get('elapsed_seconds') or 0.0)
    for row in summaries.values():
        if row['item_id'] == item.item_id:
            row['item_elapsed_seconds'] = elapsed
    for aid, counts in (res.get('pull', {}).get('by_account') or {}).items():
        row = row_for(aid)
        if row is None:
            continue
        row['item_elapsed_seconds'] = elapsed
        fetched = row['transactions_fetched']
        for k in ('added', 'modified', 'removed'):
            fetched[k] += counts.get(k, 0)
        fetched['total'] = fetched['added'] + fetched['modified'] + \
            fetched['removed']
    for aid, counts in (res.get('push', {}).get('by_account') or {}).items():
        row = row_for(aid)
        if row is None:
            continue
        row['bank_transactions_posted'] += counts.get('posted', 0)
        row['bank_transactions_cancelled'] += counts.get('cancelled', 0)
        row['bank_transactions_failed'] += counts.get('failed', 0)
        row['bank_transactions_refused'] += counts.get('drift', 0)
        row['bank_transactions_dedup_skipped'] += counts.get('dedup_skipped', 0)
    for aid, counts in (res.get('invest_je', {}).get('by_account') or {}).items():
        row = row_for(aid)
        if row is None:
            continue
        row['investment_jes_drafted'] += counts.get('posted', 0)
        row['investment_jes_skipped_duplicate'] += counts.get(
            'skipped_duplicate', 0)
        row['investment_jes_dedup_skipped'] += counts.get('dedup_skipped', 0)
        row['investment_jes_skipped'] += counts.get('skipped', 0)
        row['investment_jes_failed'] += counts.get('failed', 0)
    # Errors carrying an account_id land on that account; the rest stay at the
    # top level. An operator retrying "the account that broke" needs the first,
    # and an Item-wide failure genuinely belongs to no single account.
    for entry in res.get('errors', []):
        row = row_for(entry.get('account_id') or '')
        if row is not None:
            row['errors'].append(entry)
        elif entry.get('item_id') == item.item_id:
            for row in summaries.values():
                if row['item_id'] == item.item_id:
                    row['errors'].append(entry)


def _dedupe(entries: list) -> list:
    """Collapse identical entries, keeping order. `erpnext_not_configured` is
    one fact about the install, not one fact per Item — reported once per Item
    it would make an unconfigured ERPNext look like forty separate problems."""
    seen, out = set(), []
    for e in entries:
        key = (e.get('code'), e.get('message'), e.get('item_id', ''),
               e.get('account_id', ''))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _finish(started: float, account_id: str, include_investments: bool,
            summaries, item_results: list, errors: list, item_count: int,
            pairs: dict = None, draft_health: dict = None,
            notices: list = None) -> dict:
    """Assemble the result: per-account rows, totals, status, elapsed.

    `draft_health` (v0.8.5) is the post-sync draft-JE reading, or None when the
    run had no ERPNext client (an early return) or the ledger could not be read.
    It rides the result rather than only the log because the operator's next
    question after "what did this sync do?" is "and what is the queue now?"."""
    rows = [r if isinstance(r, dict) and 'transactions_fetched' in r
            else _account_summary(r) for r in summaries.values()]
    totals = {
        'transactions_fetched': sum(
            r['transactions_fetched']['total'] for r in rows),
        'bank_transactions_posted': sum(
            r['bank_transactions_posted'] for r in rows),
        'bank_transactions_cancelled': sum(
            r['bank_transactions_cancelled'] for r in rows),
        'bank_transactions_failed': sum(
            r['bank_transactions_failed'] for r in rows),
        'investment_jes_drafted': sum(r['investment_jes_drafted'] for r in rows),
        'investment_jes_skipped_duplicate': sum(
            r['investment_jes_skipped_duplicate'] for r in rows),
        'investment_jes_failed': sum(r['investment_jes_failed'] for r in rows),
        # v0.8.5 · the headline number Tim asked the sync summary to carry: how
        # many Journal Entries this run declined to write because ERPNext
        # already held one. Zero is the expected reading; anything else is a
        # place the local tracker and the ledger had drifted apart.
        'bank_transactions_dedup_skipped': sum(
            r['bank_transactions_dedup_skipped'] for r in rows),
        'investment_jes_dedup_skipped': sum(
            r['investment_jes_dedup_skipped'] for r in rows),
        'dedup_skipped': sum(r['bank_transactions_dedup_skipped']
                             + r['investment_jes_dedup_skipped']
                             for r in rows),
        'transfers_paired': (pairs or {}).get('detected', 0),
        'transfers_booked': (pairs or {}).get('booked', 0),
    }
    worked = any(totals[k] for k in ('transactions_fetched',
                                     'bank_transactions_posted',
                                     'bank_transactions_cancelled',
                                     'investment_jes_drafted'))
    errors = _dedupe(errors)
    # 'partial' is not a softer 'failed' — it means SOME work landed and some
    # did not, which is the only status that tells an operator to retry a scope
    # rather than the whole sweep.
    status = 'ok' if not errors else ('partial' if worked else 'failed')
    result = {'dry_run': False, 'status': status,
              'scope': account_id or 'all',
              'include_investments': include_investments,
              'items': item_count, 'accounts': rows, 'totals': totals,
              'errors': errors, 'notices': _dedupe(notices or []),
              'draft_health': draft_health,
              'elapsed_seconds': round(time.monotonic() - started, 3)}
    audit.record('sync_run_completed', subject_type=None,
                 after={**totals, 'status': status, 'items': item_count},
                 notes=(f"items={item_count} status={status} "
                        f"fetched={totals['transactions_fetched']} "
                        f"posted={totals['bank_transactions_posted']} "
                        f"invest_jes={totals['investment_jes_drafted']} "
                        f'errors={len(errors)}'))
    result['item_results'] = item_results
    return result


def sync_all(plaid_client: PlaidClient = None, erp_client=None) -> dict:
    """Sync every active Item. Returns aggregate stats. One Item's failure
    doesn't stop the others.

    v0.8.5 · a thin wrapper over `run_sync`, kept because the scheduler, the
    /api surface and a dozen tests call it by this name and its return shape.
    New callers should prefer run_sync, which reports per account."""
    res = run_sync(plaid_client=plaid_client, erp_client=erp_client,
                   actor='scheduled')
    return {'items': res['items'], 'results': res['item_results'],
            'status': res['status'], 'totals': res['totals'],
            'errors': res['errors']}
