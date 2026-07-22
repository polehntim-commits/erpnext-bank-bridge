# SPDX-License-Identifier: MIT
"""Investment sync: pull Plaid /investments/holdings/get and
/investments/transactions/get into the local `securities`, `security_holdings`,
and `security_transactions` tables (v0.4.28 · Phase A step 2 of the v0.5.0
lot tracker).

Fail-soft by design, mirroring how loans.py handles liabilities:

  * An Item without the `investments` product returns {} from the Plaid
    wrappers; this module treats that as "no investment data on this Item"
    and skips silently. Bank Bridge's existing transaction sync continues
    to run.
  * Any individual holding or transaction that can't be upserted (e.g. its
    referenced account_id doesn't exist locally yet) is logged at INFO and
    skipped; the rest of the batch persists.
  * The whole `sync_investments_for_item` call is wrapped in an outer
    try/except at the caller (sync_engine.pull_item), so a bug in this
    module cannot fail an otherwise-successful transactions sync.

Idempotency:

  * Holdings are snapshot-per-sync — each successful call REPLACES the
    holding rows for a given (account_id, security_id) pair. This matches
    Plaid's endpoint semantics (holdings_get returns the CURRENT state,
    not a delta) and means we always reflect what Plaid last told us.
  * Investment transactions are upserted on
    `plaid_investment_transaction_id`, so a re-pull that overlaps the
    previous range is a no-op on the DB.
  * Securities are upserted on `security_id`, so the same AAPL row serves
    every account holding it across every sync.

Backfill window:

  * Investment transactions use offset-based pagination (unlike
    /transactions/sync's cursor model). The initial pull requests the
    Item's full history back to the account open date (or Plaid's
    institution limit, whichever comes first). Subsequent pulls narrow to
    a rolling window since the last sync's `investments_synced_at`
    timestamp, so a settled install costs a single small page per sync.
"""
import logging
from datetime import date, timedelta

from . import db
from .models import (PlaidAccount, PlaidItem, Security, SecurityHolding,
                     SecurityTransaction)
from .plaid_client import PlaidClient, PlaidError

log = logging.getLogger('bankbridge.investments')

# Maximum offset-paginated page size Plaid honors for
# /investments/transactions/get. Matches TRANSACTIONS_DAYS_REQUESTED's spirit —
# we ask for the max on each call so a big backfill takes as few paginated
# roundtrips as possible.
_TXNS_PAGE_SIZE = 500

# Cap on how far back the initial investment-transactions pull reaches when
# `investments_synced_at` is NULL (a fresh Item). Two years matches Plaid's
# TRANSACTIONS_DAYS_REQUESTED default for the transactions product, and mirrors
# the bank statement retention most brokerages actually provide. On subsequent
# syncs the floor is `investments_synced_at - 30 days` (a small overlap so any
# late-posting cash dividends or corporate action reclassifications catch).
_INITIAL_BACKFILL_DAYS = 730
_OVERLAP_DAYS = 30


def sync_investments_for_item(item: PlaidItem, plaid_client: PlaidClient,
                              access_token: str) -> dict:
    """Pull holdings + investment transactions for one Item, upsert locally,
    return stats. Never raises — every failure mode returns something.

    Stats: {'holdings': int, 'txns_added': int, 'txns_modified': int,
    'securities': int, 'skipped': str|None}. `skipped` is set when the
    whole call was a no-op (product unavailable, no investment accounts,
    etc.); zero-count with skipped=None means "ran successfully, nothing
    new to store"."""
    stats = {'holdings': 0, 'txns_added': 0, 'txns_modified': 0,
             'securities': 0, 'skipped': None}

    # Only Items that actually hold investment accounts are worth calling
    # for. Plaid returns 0-length holdings arrays on other Items, so the
    # skip is a cost optimization, not a correctness requirement.
    invest_accounts = (
        PlaidAccount.query
        .filter(PlaidAccount.item_id == item.item_id,
                PlaidAccount.type.in_(('investment', 'brokerage')))
        .all())
    if not invest_accounts:
        stats['skipped'] = 'no investment accounts on this Item'
        return stats

    # ── Holdings first: gives us the current position snapshot AND the
    # Security records the subsequent transactions will reference. Doing
    # holdings before transactions means the FK reference on
    # SecurityTransaction.security_id is guaranteed to resolve.
    holdings_payload = plaid_client.investments_holdings_get(access_token)
    if not holdings_payload:
        stats['skipped'] = ('investments product unavailable for this Item '
                            '(not enabled on Plaid application, or not '
                            'requested at Link time)')
        return stats

    securities_by_id = {}
    for s in holdings_payload.get('securities', []) or []:
        row = _upsert_security(s)
        if row is not None:
            securities_by_id[s.get('security_id')] = row
            stats['securities'] += 1

    for h in holdings_payload.get('holdings', []) or []:
        row = _upsert_holding(h)
        if row is not None:
            stats['holdings'] += 1

    # ── Transactions next: paginated backfill or delta pull depending on
    # whether this Item has been synced before.
    txn_stats = _pull_investment_transactions(item, plaid_client, access_token,
                                              securities_by_id)
    stats['txns_added'] += txn_stats['added']
    stats['txns_modified'] += txn_stats['modified']
    stats['securities'] += txn_stats['securities_upserted']

    # Stamp the successful-completion timestamp so the next sync's window
    # narrows appropriately.
    from datetime import datetime, timezone
    item.investments_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return stats


def _pull_investment_transactions(item: PlaidItem, plaid_client: PlaidClient,
                                  access_token: str,
                                  known_securities: dict) -> dict:
    """Paginated pull of /investments/transactions/get. Returns
    {'added', 'modified', 'securities_upserted'}. Fail-soft: on any
    PlaidError mid-page the completed pages remain persisted."""
    stats = {'added': 0, 'modified': 0, 'securities_upserted': 0}
    end = date.today()
    last_synced = getattr(item, 'investments_synced_at', None)
    if last_synced is None:
        start = end - timedelta(days=_INITIAL_BACKFILL_DAYS)
    else:
        start = last_synced.date() - timedelta(days=_OVERLAP_DAYS)

    offset = 0
    total = None
    while total is None or offset < total:
        try:
            page = plaid_client.investments_transactions_get(
                access_token, start_date=start, end_date=end,
                count=_TXNS_PAGE_SIZE, offset=offset)
        except PlaidError as e:  # pragma: no cover - fail-soft
            log.warning('investments_transactions_get failed at offset=%d '
                        'for %s: %s', offset, item.item_id, e)
            break
        if not page:
            break
        # Every page ships the same securities list; upsert defensively so
        # newly-appearing securities land even if this run's holdings pull
        # was a no-op.
        for s in page.get('securities', []) or []:
            sid = s.get('security_id')
            if sid and sid not in known_securities:
                row = _upsert_security(s)
                if row is not None:
                    known_securities[sid] = row
                    stats['securities_upserted'] += 1
        rows = page.get('investment_transactions', []) or []
        for t in rows:
            added_or_modified = _upsert_investment_txn(t)
            if added_or_modified == 'added':
                stats['added'] += 1
            elif added_or_modified == 'modified':
                stats['modified'] += 1
        total = page.get('total_transactions', 0) or 0
        offset += len(rows) if rows else _TXNS_PAGE_SIZE
        # Belt: avoid an infinite loop if a defective page returns zero rows
        # but claims has_more.
        if not rows:
            break
        db.session.commit()
    return stats


def _upsert_security(payload: dict) -> Security | None:
    """Insert or update a Security row keyed on security_id. Returns the row
    (or None if the payload is missing security_id)."""
    sid = payload.get('security_id')
    if not sid:
        return None
    row = Security.query.filter_by(security_id=sid).first()
    if row is None:
        row = Security(security_id=sid)
        db.session.add(row)
    row.ticker_symbol = payload.get('ticker_symbol', '') or ''
    row.name = payload.get('name', '') or ''
    row.type = payload.get('type', '') or ''
    row.iso_currency_code = payload.get('iso_currency_code', 'USD') or 'USD'
    row.cusip = payload.get('cusip', '') or ''
    row.isin = payload.get('isin', '') or ''
    row.sedol = payload.get('sedol', '') or ''
    row.close_price = payload.get('close_price')
    row.close_price_as_of = _to_date(payload.get('close_price_as_of'))
    row.is_option = bool(payload.get('is_option'))
    row.option_contract_type = payload.get('option_contract_type')
    row.option_strike_price = payload.get('option_strike_price')
    row.option_expiration_date = _to_date(payload.get('option_expiration_date'))
    row.option_underlying_ticker = payload.get('option_underlying_ticker')
    return row


def _upsert_holding(payload: dict) -> SecurityHolding | None:
    """Insert or update the SecurityHolding row for (account_id, security_id).
    Returns None when the referenced account_id doesn't exist locally — a
    common race between /accounts/get and /investments/holdings/get on the
    first sync after a re-link. The row will land on the next sync when the
    account has been ingested."""
    aid = payload.get('account_id')
    sid = payload.get('security_id')
    if not aid or not sid:
        return None
    if PlaidAccount.query.filter_by(account_id=aid).first() is None:
        log.info('holding references unknown account_id=%s (security %s) '
                 '— skipping this sync, will land next time', aid, sid)
        return None
    row = (SecurityHolding.query
           .filter_by(account_id=aid, security_id=sid).first())
    if row is None:
        row = SecurityHolding(account_id=aid, security_id=sid)
        db.session.add(row)
    row.quantity = float(payload.get('quantity') or 0.0)
    row.cost_basis = payload.get('cost_basis')
    row.institution_price = payload.get('institution_price')
    row.institution_price_as_of = _to_date(
        payload.get('institution_price_as_of'))
    row.institution_value = payload.get('institution_value')
    row.iso_currency_code = payload.get('iso_currency_code', 'USD') or 'USD'
    return row


def _upsert_investment_txn(payload: dict) -> str | None:
    """Insert or update the SecurityTransaction row keyed on
    plaid_investment_transaction_id. Returns 'added' / 'modified' / None."""
    tid = payload.get('investment_transaction_id')
    if not tid:
        return None
    row = SecurityTransaction.query.filter_by(
        plaid_investment_transaction_id=tid).first()
    is_new = row is None
    if row is None:
        row = SecurityTransaction(plaid_investment_transaction_id=tid)
        db.session.add(row)
    row.account_id = payload.get('account_id')
    row.security_id = payload.get('security_id')
    row.date = _to_date(payload.get('date'))
    row.name = payload.get('name', '') or ''
    row.quantity = float(payload.get('quantity') or 0.0)
    row.amount = float(payload.get('amount') or 0.0)
    row.price = float(payload.get('price') or 0.0)
    row.fees = payload.get('fees')
    row.type = payload.get('type', '') or ''
    row.subtype = payload.get('subtype', '') or ''
    row.iso_currency_code = payload.get('iso_currency_code', 'USD') or 'USD'
    return 'added' if is_new else 'modified'


def _to_date(value):
    """Coerce an ISO 8601 date string, a datetime.date, or a datetime.datetime
    into a datetime.date (or None for anything else)."""
    if value is None or value == '':
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
