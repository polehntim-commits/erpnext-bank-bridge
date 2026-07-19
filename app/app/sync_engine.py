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


def _mark_removed(tid: str) -> None:
    row = BankTransaction.query.filter_by(plaid_transaction_id=tid).first()
    if row is None:
        return
    row.removed = True
    row.posted_at = None       # re-queue for the cancel path
    row.updated_at = _now()


def _item_all_balance_only(item: PlaidItem) -> bool:
    """True when the Item has at least one account and every one is balance-only
    (an investment). Such an Item has no transactions to pull, so the sync loop
    skips /transactions/sync for it and just refreshes balances."""
    accts = PlaidAccount.query.filter_by(item_id=item.item_id).all()
    return bool(accts) and all(a.balance_only for a in accts)


def pull_item(item: PlaidItem, plaid_client: PlaidClient) -> dict:
    """Pull the delta for one Item, applying added/modified/removed locally and
    advancing the cursor. Returns a stats dict. Logs one plaid_pull row."""
    access_token = crypto.decrypt(item.access_token_encrypted)
    stats = {'added': 0, 'modified': 0, 'removed': 0, 'accounts': 0}
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
            for t in page['modified']:
                _upsert_txn(t, is_modification=True)
                stats['modified'] += 1
            for t in page['removed']:
                _mark_removed(t.get('transaction_id'))
                stats['removed'] += 1
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


def push_pending(erp_client, item_id: str = '') -> dict:
    """Post every pending local row (posted_at IS NULL) whose account is mapped
    + enabled. Rows for unmapped accounts are left pending (no error). Logs one
    erpnext_push row. `item_id` scopes the log line (optional)."""
    stats = {'posted': 0, 'cancelled': 0, 'failed': 0, 'skipped': 0, 'drift': 0,
             'paired': 0, 'pairs_booked': 0, 'unpostable': 0}
    if erp_client is None:
        return stats
    eligible = _eligible_account_map()
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
            continue
        try:
            was_removed = row.removed
            _push_row(erp_client, row, account)
            db.session.commit()
            if was_removed:
                stats['cancelled'] += 1
            else:
                stats['posted'] += 1
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
                categorization.categorize_after_push(erp_client, row)
        except (ERPNextAPIError, ERPNextError) as e:
            db.session.rollback()
            row = db.session.get(BankTransaction, row.id)
            if row is not None:
                row.sync_error = str(e)[:2000]
                row.updated_at = _now()
                db.session.commit()
            stats['failed'] += 1
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


# ── top-level orchestration ────────────────────────────────────────────

def sync_item(item: PlaidItem, plaid_client: PlaidClient = None,
              erp_client=None) -> dict:
    """Pull + push one Item. Pull failures propagate; push failures are logged
    per-row and don't abort the pull's local mirror. The optional per-Item daily
    call brake (PLAID_MAX_CALLS_PER_DAY) short-circuits the pull before any Plaid
    call when the Item has hit its limit for the day."""
    if _daily_call_brake_hit(item.item_id):
        return {'item_id': item.item_id, 'skipped': 'max_calls_per_day',
                'pull': {}, 'push': {}}
    plaid_client = plaid_client or get_plaid_client()
    pull_stats = pull_item(item, plaid_client)
    if erp_client is None:
        erp_client = get_erp_client_or_none()
    push_stats = push_pending(erp_client, item.item_id) if erp_client else {}
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
        # v0.4.12 · mark investment accounts to market. Runs right after the
        # balance refresh above, because it measures against the balance that
        # refresh just cached. Posts a Draft for the DELTA only, and only when
        # a baseline is known — see app/revaluation.py. Best-effort: a
        # chart-of-accounts problem must never fail a sync that has already
        # mirrored transactions correctly.
        try:
            revaluation.revalue_all(erp_client)
        except Exception:  # pragma: no cover - revalue_all is already total
            log.warning('investment revaluation failed for %s', item.item_id,
                        exc_info=True)
        # v0.4.14 · book the interest each loan has accrued since last time.
        # The other half of a loan payment — the principal — is booked from the
        # chequing side by an ordinary categorization rule; see app/loans.py for
        # why the two halves are separate entries.
        try:
            loans.accrue_all(erp_client)
        except Exception:  # pragma: no cover - accrue_all is already total
            log.warning('loan interest accrual failed for %s', item.item_id,
                        exc_info=True)
    return {'item_id': item.item_id, 'pull': pull_stats, 'push': push_stats}


def sync_all(plaid_client: PlaidClient = None, erp_client=None) -> dict:
    """Sync every active Item. Returns aggregate stats. One Item's failure
    doesn't stop the others."""
    plaid_client = plaid_client or get_plaid_client()
    if erp_client is None:
        erp_client = get_erp_client_or_none()
    # v0.4.7 · a disconnected Item's access_token was invalidated at Plaid by
    # /item/remove, so every call for it would fail — skip it here rather than
    # burn a request and record an error per poll. `isnot(True)` (not
    # `.is_(False)`) so a row that predates the column and somehow read back
    # NULL still counts as connected; the migration backfills false, this is
    # belt-and-braces.
    # v0.4.11 · an Item the bank wants re-authenticated is skipped for exactly
    # the reason above: every call for it fails until a HUMAN signs in again, so
    # polling it just burns a billable request and writes an error row per
    # cycle. Before this release that happened on every poll, forever. The
    # operator sees it on /admin/accounts with a Reconnect button; nothing here
    # can clear the state, so nothing here should keep paying to rediscover it.
    items = (PlaidItem.query
             .filter(PlaidItem.status != 'revoked')
             .filter(PlaidItem.disconnected.isnot(True))
             .filter(PlaidItem.needs_reauth.isnot(True)).all())
    parked = (PlaidItem.query
              .filter(PlaidItem.needs_reauth.is_(True),
                      PlaidItem.disconnected.isnot(True)).count())
    if parked:
        log.info('[sync] %d item(s) parked awaiting re-authentication — not '
                 'polled. Reconnect them on /admin/accounts.', parked)
    audit.record('sync_run_started', subject_type=None,
                 after={'items': len(items)},
                 notes=f'sync across {len(items)} item(s)')
    results = []
    agg = {'added': 0, 'modified': 0, 'removed': 0, 'posted': 0,
           'cancelled': 0, 'failed': 0, 'paired': 0, 'pairs_booked': 0}
    for item in items:
        try:
            res = sync_item(item, plaid_client, erp_client)
            results.append(res)
            for k in ('added', 'modified', 'removed'):
                agg[k] += res.get('pull', {}).get(k, 0)
            for k in ('posted', 'cancelled', 'failed', 'paired', 'pairs_booked'):
                agg[k] += res.get('push', {}).get(k, 0)
        except (PlaidError, PlaidConfigError) as e:
            log.warning('sync failed for item %s: %s', item.item_id, e)
            results.append({'item_id': item.item_id, 'error': str(e)})
    # v0.4.1 · a final cross-Item detection pass. Each Item pulls on its own
    # cursor, so the two legs of one transfer very often arrive in DIFFERENT
    # iterations of the loop above — the second Company's leg simply did not
    # exist yet when the first was pushed. This pass runs once every Item has
    # landed, which is the first moment both halves are visible together.
    if erp_client is not None:
        final = intercompany.run_detection(erp_client)
        agg['paired'] += final['detected']
        agg['pairs_booked'] += final['booked']
    audit.record('sync_run_completed', subject_type=None, after=agg,
                 notes=(f"items={len(items)} added={agg['added']} "
                        f"posted={agg['posted']} failed={agg['failed']} "
                        f"paired={agg['paired']}"))
    return {'items': len(items), 'results': results}
