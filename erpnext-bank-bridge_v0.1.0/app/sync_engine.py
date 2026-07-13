# SPDX-License-Identifier: MIT
"""The sync engine: Plaid /transactions/sync → local mirror → ERPNext.

Two phases per Item, each independently logged to PlaidSyncLog:

  1. PULL  (direction='plaid_pull')
     Loop /transactions/sync while has_more, applying Plaid's added / modified /
     removed lists to the local BankTransaction table and advancing the Item's
     cursor. Cursor-based, so each poll only pulls the delta. Also refreshes the
     Item's accounts (balances) so the dashboard stays current.

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

from . import db
from . import erpnext_bank
from . import erpnext_settings
from . import plaid_settings
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

def refresh_accounts(item: PlaidItem, plaid_client: PlaidClient,
                     access_token: str) -> int:
    """Upsert the Item's accounts (idempotent on account_id); refresh cached
    balances. Preserves operator-set erpnext mapping + sync toggle. Returns the
    account count."""
    accounts = plaid_client.get_accounts(access_token)
    for a in accounts:
        acct_id = a.get('account_id')
        if not acct_id:
            continue
        acct = PlaidAccount.query.filter_by(account_id=acct_id).first()
        if acct is None:
            acct = PlaidAccount(account_id=acct_id, item_id=item.item_id)
            db.session.add(acct)
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
        acct.updated_at = _now()
    db.session.commit()
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


def pull_item(item: PlaidItem, plaid_client: PlaidClient) -> dict:
    """Pull the delta for one Item, applying added/modified/removed locally and
    advancing the cursor. Returns a stats dict. Logs one plaid_pull row."""
    access_token = crypto.decrypt(item.access_token_encrypted)
    stats = {'added': 0, 'modified': 0, 'removed': 0, 'accounts': 0}
    try:
        stats['accounts'] = refresh_accounts(item, plaid_client, access_token)
    except PlaidError as e:
        log.warning('account refresh failed for %s: %s', item.item_id, e)

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
    except PlaidError as e:
        db.session.rollback()
        item = db.session.get(PlaidItem, item.id)
        if item is not None:
            item.status = 'error'
            item.last_error = str(e)[:2000]
            item.updated_at = _now()
            db.session.commit()
        _log(item.item_id if item else '', 'plaid_pull', 0, 'failed', str(e))
        raise
    _log(item.item_id, 'plaid_pull',
         stats['added'] + stats['modified'] + stats['removed'], 'success',
         f"added={stats['added']} modified={stats['modified']} "
         f"removed={stats['removed']} accounts={stats['accounts']}")
    return stats


# ── ERPNext push ───────────────────────────────────────────────────────

def _eligible_account_map() -> dict:
    """account_id → PlaidAccount for accounts that are mapped + sync-enabled."""
    out = {}
    for a in PlaidAccount.query.all():
        if a.erpnext_bank_account_name and a.sync_enabled:
            out[a.account_id] = a
    return out


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
    stats = {'posted': 0, 'cancelled': 0, 'failed': 0, 'skipped': 0}
    if erp_client is None:
        return stats
    eligible = _eligible_account_map()
    q = BankTransaction.query.filter(BankTransaction.posted_at.is_(None))
    pending = q.order_by(BankTransaction.date.asc()).all()
    for row in pending:
        account = eligible.get(row.account_id)
        if account is None:
            stats['skipped'] += 1
            continue
        try:
            was_removed = row.removed
            _push_row(erp_client, row, account)
            db.session.commit()
            if was_removed:
                stats['cancelled'] += 1
            else:
                stats['posted'] += 1
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
    handled = stats['posted'] + stats['cancelled']
    status = 'failed' if stats['failed'] and not handled else 'success'
    _log(item_id, 'erpnext_push', handled, status,
         f"posted={stats['posted']} cancelled={stats['cancelled']} "
         f"failed={stats['failed']} skipped={stats['skipped']}")
    return stats


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
    per-row and don't abort the pull's local mirror."""
    plaid_client = plaid_client or get_plaid_client()
    pull_stats = pull_item(item, plaid_client)
    if erp_client is None:
        erp_client = get_erp_client_or_none()
    push_stats = push_pending(erp_client, item.item_id) if erp_client else {}
    return {'item_id': item.item_id, 'pull': pull_stats, 'push': push_stats}


def sync_all(plaid_client: PlaidClient = None, erp_client=None) -> dict:
    """Sync every active Item. Returns aggregate stats. One Item's failure
    doesn't stop the others."""
    plaid_client = plaid_client or get_plaid_client()
    if erp_client is None:
        erp_client = get_erp_client_or_none()
    items = PlaidItem.query.filter(PlaidItem.status != 'revoked').all()
    results = []
    for item in items:
        try:
            results.append(sync_item(item, plaid_client, erp_client))
        except (PlaidError, PlaidConfigError) as e:
            log.warning('sync failed for item %s: %s', item.item_id, e)
            results.append({'item_id': item.item_id, 'error': str(e)})
    return {'items': len(items), 'results': results}
