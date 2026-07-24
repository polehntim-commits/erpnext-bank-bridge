# SPDX-License-Identifier: MIT
"""Post investment transactions as ERPNext Journal Entries (v0.5.1, Phase D).

Every SecurityTransaction on a brokerage account can be booked as a Journal
Entry with security-level detail — a buy moves cost into Marketable Securities,
a sell realizes gain or loss, an advisory fee hits an expense line, a dividend
an income line. This is the accounting side of the investment pipeline Phases A
and B built.

THE DOUBLE-BOOKING HAZARD, and the Cash Clearing account that resolves it.
==========================================================================
The reconciliation subsystem (v0.4.48) established that for a PAIRED brokerage,
the cash leg of every trade is ALREADY on the companion depository account, as
an 'Increase/Decrease from Brokerage activity' BankTransaction — and that
BankTransaction is itself posted to ERPNext by the ordinary rules engine. If
the SecurityTransaction JE also credited/debited the sweep cash account, every
trade would be booked twice.

So a paired brokerage's investment JEs settle against a per-Company **Cash
Clearing - Brokerage** account, never the bank account directly:

    Buy $10k:   DR Marketable Securities  10,000   CR Cash Clearing 10,000
    companion:  DR Cash Clearing          10,000   CR Bank         10,000   (rules)
                -> Marketable +10k, Bank -10k, Clearing nets to ZERO

The clearing account is a bridge that must always net to zero. A non-zero
balance means a SecurityTransaction has no matching companion BankTransaction
(or vice versa) — a mismatched pair worth surfacing (see clearing_imbalance).

An UNPAIRED brokerage has no companion double-post, so its investment JEs
settle against its own bank GL leaf directly; clearing is a paired-account
concept only.

WHAT NEVER HAPPENS HERE. Unrealized gains are not posted (Marketable Securities
sits at cost until a sell). Nothing posts at all until the operator flips the
per-Item kill switch (posting_enabled, default FALSE) — these are real P&L
entries and an upgrade must not auto-post them. And every line carries
`company = plaid_account.owning_company`, so Orchard Meadow's JEs stay separable
from any other entity's and move by export/import with nothing to unwind.
"""
from __future__ import annotations

import logging

from flask import current_app

from . import audit
from . import db
from .erpnext_accounts import (ACCOUNT_DT, _asset_root, _create_group_account,
                               _find_accounts, owning_company_for_account_id)
from .erpnext_client import ERPNextAPIError, ERPNextError, ERPNextClient
from .models import (GeneratedJournalEntry, PlaidAccount, PlaidItem, RetainedLot,
                     Security, SecurityTransaction, TradedCycle)

log = logging.getLogger('bankbridge.invest_je')

JOURNAL_ENTRY_DT = 'Journal Entry'


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── the kill switch ──────────────────────────────────────────────────────────

def posting_enabled(account_or_item) -> bool:
    """Whether investment JEs may be posted for this account/Item.

    Defaults FALSE and stays FALSE until an operator opts the Item in on
    /admin/accounts — the load-bearing safety on a feature that writes real
    accounting entries. Accepts a PlaidAccount (resolves its Item) or a
    PlaidItem."""
    item = account_or_item
    if isinstance(account_or_item, PlaidAccount):
        item = PlaidItem.query.filter_by(
            item_id=account_or_item.item_id).first()
    return bool(item and item.invest_je_posting_enabled)


# ── GL account taxonomy ──────────────────────────────────────────────────────
#
# Each category names the leaf account and the root branch it belongs under.
# Accounts are created on first use, idempotently, under the company's root of
# the right type — an Asset for securities and clearing, Income for gains and
# premium, Expense for losses and fees.

def _root_group(client: ERPNextClient, company: str, root_type: str) -> str | None:
    """The company's top group of `root_type` (Asset/Income/Expense/Liability),
    or None when the Chart of Accounts has none yet."""
    if root_type == 'Asset':
        return _asset_root(client, company)
    groups = _find_accounts(client, company, is_group=1, root_type=root_type)
    if not groups:
        return None
    for g in groups:
        if not (g.get('parent_account') or ''):
            return g['name']
    return groups[0]['name']


def ensure_leaf(client: ERPNextClient, company: str, account_name: str,
                root_type: str, *, account_number: str = '',
                parent_account: str | None = None) -> str | None:
    """Find (or create) a posting leaf named `account_name`; return its docname,
    or None when there is no anchor group to create it under.

    `parent_account` (v0.5.11) is the group the leaf hangs under. When given it
    is used verbatim — NEVER falling back to the root — so a leaf can be nested
    exactly (e.g. the 181X asset-type leaves under 1810 - Marketable Securities).
    When omitted, the leaf lands under the company's `root_type` root, the
    pre-v0.5.11 behaviour. The caller is responsible for the parent existing;
    marketable_securities_account builds the 1800→1810 chain first.

    Idempotent: an existing leaf with the same account_name + company is reused,
    so a re-run never duplicates. The created docname is read back from the
    create response (Frappe autonames '<account_name> - <abbr>')."""
    existing = _find_accounts(client, company, account_name=account_name,
                              is_group=0)
    if existing:
        return existing[0]['name']
    root = parent_account or _root_group(client, company, root_type)
    if not root:
        log.info('company %r has no anchor group for %r — cannot create',
                 company, account_name)
        return None
    doc = {'account_name': account_name, 'parent_account': root,
           'company': company, 'root_type': root_type, 'is_group': 0}
    if account_number:
        doc['account_number'] = account_number
    try:
        created = client.create_doc(ACCOUNT_DT, doc)
    except ERPNextError as e:
        # v0.5.10 · the create can still fail after the name check above for two
        # reasons, and neither should abort a backfill:
        #   1. A CONCURRENT sync created the same-named leaf between our check
        #      and our create (a check-then-create race).
        #   2. The account_NUMBER is already used by ANOTHER leaf. Frappe
        #      enforces per-company number uniqueness, so a caller that reuses a
        #      number across distinct account_names (the pre-v0.5.10 bug where
        #      every 'Marketable Securities - <ticker>' asked for 1320.1) hits
        #      'Account Number NNNN already used' on the second name onward.
        # Re-check by name first — if it exists now, a race made it and we reuse
        # it. Otherwise retry ONCE without the colliding number: the leaf is
        # still creatable, the number simply belongs to a sibling.
        raced = _find_accounts(client, company, account_name=account_name,
                               is_group=0)
        if raced:
            log.info("GL Account '%s' already present on retry — reusing '%s'",
                     account_name, raced[0]['name'])
            return raced[0]['name']
        if account_number:
            log.warning("account_number %s already in use — creating '%s' "
                        "without a number (%s)", account_number, account_name, e)
            doc.pop('account_number', None)
            created = client.create_doc(ACCOUNT_DT, doc)
        else:
            raise
    name = created.get('name')
    log.info("created GL Account '%s' under '%s'", name or account_name, root)
    audit.record('invest_gl_account_created', subject_type='Account',
                 subject_id=name or account_name,
                 after={'account': name or account_name, 'root_type': root_type,
                        'company': company},
                 notes=f'auto-created for investment posting')
    return name


def ensure_group(client, company, account_name: str, parent_account: str, *,
                 account_number: str | int | None = None) -> str | None:
    """Find (or create) an is_group=1 Account named `account_name` DIRECTLY under
    `parent_account`; return its docname (v0.5.11).

    Matches on (account_name, parent_account) rather than name alone, so a group
    of the same name at a DIFFERENT place in the tree (the stale, wrongly-parented
    '1320 - Marketable Securities' from v0.5.10) is never mistaken for this one.
    Tolerant of a create race the same way ensure_leaf is."""
    for g in _find_accounts(client, company, account_name=account_name,
                            is_group=1):
        if (g.get('parent_account') or '') == parent_account:
            return g['name']
    try:
        return _create_group_account(client, account_name, parent_account,
                                     company, account_number=account_number)
    except ERPNextError:
        for g in _find_accounts(client, company, account_name=account_name,
                                is_group=1):
            if (g.get('parent_account') or '') == parent_account:
                return g['name']
        raise


def _investments_group(client, company) -> str | None:
    """The '1800 - Investments' asset group, creating it under the Asset root if
    a chart somehow lacks it. None when there is no Chart of Accounts at all."""
    existing = _find_accounts(client, company, account_name='Investments',
                              is_group=1)
    if existing:
        return existing[0]['name']
    root = _asset_root(client, company)
    if not root:
        return None
    return ensure_group(client, company, 'Investments', root,
                        account_number='1800')


def _marketable_securities_group(client, company) -> str | None:
    """The '1320 - Marketable Securities' group under 1800 - Investments
    (v0.5.11). Tim's chart already has this group holding the brokerage balance,
    so ensure_group MATCHES it by (name, parent=Investments) and reuses it — the
    six 1321-1326 asset-type leaves hang under it. Only created (as 1320) if a
    chart somehow lacks it."""
    inv = _investments_group(client, company)
    if not inv:
        return None
    return ensure_group(client, company, 'Marketable Securities', inv,
                        account_number='1320')


# security_type (Plaid Security.type) → (account_number, leaf name) under the
# existing 1320 - Marketable Securities group. Plaid prints multi-word types
# with spaces ('mutual fund', 'fixed income'); the lookup normalises spaces to
# underscores so both spellings map. Anything unrecognised (including a blank
# type) buckets into Stocks — the safe default that keeps a real holding on the
# balance sheet rather than dropping it.
_SECURITY_TYPE_ACCOUNTS = {
    'equity': ('1321', 'Stocks'),
    'etf': ('1322', 'ETFs'),
    'mutual_fund': ('1323', 'Mutual Funds'),
    'fixed_income': ('1324', 'Fixed Income'),
    'preferred': ('1325', 'Preferreds'),
    'derivative': ('1326', 'Options'),
    'option': ('1326', 'Options'),
}
_DEFAULT_SECURITY_ACCOUNT = ('1321', 'Stocks')


def _norm_security_type(security_type: str | None) -> str:
    return (security_type or '').strip().lower().replace(' ', '_')


def marketable_securities_account(client, company, ticker,
                                  security_type=None) -> str | None:
    """The asset-TYPE Marketable Securities leaf a holding belongs on (v0.5.11).

    v0.5.10 created one leaf PER TICKER — 103 of them on the live install, all
    stranded at the Chart-of-Accounts root. v0.5.11 buckets by `security_type`
    into six leaves (1321 Stocks / 1322 ETFs / 1323 Mutual Funds / 1324 Fixed
    Income / 1325 Preferreds / 1326 Options) under the EXISTING 1320 - Marketable
    Securities group (itself under 1800 - Investments), so the balance sheet
    stays clean. `ticker` is retained for signature compatibility but no longer
    selects the account. Returns None when the chart has no Investments anchor to
    nest under."""
    group = _marketable_securities_group(client, company)
    if not group:
        return None
    number, leaf_name = _SECURITY_TYPE_ACCOUNTS.get(
        _norm_security_type(security_type), _DEFAULT_SECURITY_ACCOUNT)
    return ensure_leaf(client, company, leaf_name, 'Asset',
                       account_number=number, parent_account=group)


def cash_clearing_account(client, company) -> str | None:
    """`Cash Clearing - Brokerage` (Asset) — the bridge that nets to zero
    between a SecurityTransaction JE and its companion BankTransaction JE."""
    return ensure_leaf(client, company, 'Cash Clearing - Brokerage', 'Asset',
                       account_number='1099')


def realized_gains_account(client, company) -> str | None:
    return ensure_leaf(client, company, 'Realized Capital Gains', 'Income',
                       account_number='4200')


def realized_losses_account(client, company) -> str | None:
    return ensure_leaf(client, company, 'Realized Capital Losses', 'Expense',
                       account_number='5710')


def options_premium_income_account(client, company) -> str | None:
    return ensure_leaf(client, company, 'Options Premium Income', 'Income',
                       account_number='4210')


def options_premium_losses_account(client, company) -> str | None:
    return ensure_leaf(client, company, 'Options Premium Losses', 'Expense',
                       account_number='5715')


def advisory_fees_account(client, company) -> str | None:
    return ensure_leaf(client, company, 'Advisory & Management Fees', 'Expense',
                       account_number='5700')


def dividend_income_account(client, company) -> str | None:
    return ensure_leaf(client, company, 'Dividend Income', 'Income',
                       account_number='4230')


def interest_income_account(client, company) -> str | None:
    return ensure_leaf(client, company, 'Interest Income', 'Income',
                       account_number='4240')


def cash_side_account(client, account: PlaidAccount, company: str) -> str | None:
    """The account a SecurityTransaction's cash leg settles against: the Cash
    Clearing bridge for a PAIRED brokerage (its companion posts the other
    clearing leg), or the account's own bank GL leaf when unpaired (no
    companion, so no double-post to guard against)."""
    if (account.paired_account_id or '').strip():
        return cash_clearing_account(client, company)
    return (account.erpnext_gl_account_name or '').strip() or None


# ── cost basis ───────────────────────────────────────────────────────────────

def cost_basis_for_sell(txn: SecurityTransaction, sold_qty: float) -> tuple:
    """(cost_basis, method, plan) for a sell of `sold_qty` shares. PURE — reads
    the lots but does NOT mutate them; `plan` is the FIFO consumption to apply
    only AFTER the JE actually posts (see _apply_lot_plan), so an ERPNext
    failure never leaves inventory consumed for a sale that didn't book.

    SPECIFIC IDENTIFICATION first: when a TradedCycle names this sell, the cost
    is the cycle's buy price times the quantity sold — the 5:4 strategy's every
    sell is a matched cycle, so this is the ordinary path (empty plan).

    FIFO FALLBACK against RetainedLot rows, oldest purchase first. When the lots
    cannot cover the whole sale, the uncovered shares take the sale price as
    their basis (zero realized gain on that portion), marked 'fifo_incomplete'
    so the gap is visible rather than silently booked as pure gain.

    `method` is 'specific_id', 'fifo', 'fifo_incomplete', or 'no_basis'."""
    cycle = (TradedCycle.query
             .filter_by(sell_transaction_id=txn.plaid_investment_transaction_id)
             .first())
    if cycle is not None and cycle.buy_price is not None:
        return round(float(cycle.buy_price) * sold_qty, 2), 'specific_id', []

    lots = (RetainedLot.query
            .filter(RetainedLot.security_id == txn.security_id,
                    RetainedLot.shares_remaining > 0)
            .order_by(RetainedLot.purchase_date.asc(), RetainedLot.id.asc())
            .all())
    remaining = sold_qty
    basis = 0.0
    plan = []
    for lot in lots:
        if remaining <= 0:
            break
        take = min(float(lot.shares_remaining), remaining)
        basis += take * float(lot.cost_basis_per_share)
        plan.append((lot.id, round(take, 6)))
        remaining -= take
    if remaining > 0.0000001:
        if basis == 0.0:
            return round(float(txn.price or 0.0) * sold_qty, 2), 'no_basis', []
        basis += remaining * float(txn.price or 0.0)
        return round(basis, 2), 'fifo_incomplete', plan
    return round(basis, 2), 'fifo', plan


def _apply_lot_plan(plan) -> None:
    """Decrement RetainedLot.shares_remaining per a plan from
    cost_basis_for_sell. No commit — the caller commits with the GJE row so the
    two are one transaction."""
    for lot_id, take in plan:
        lot = db.session.get(RetainedLot, lot_id)
        if lot is not None:
            lot.shares_remaining = round(float(lot.shares_remaining) - take, 6)


# ── JE construction ──────────────────────────────────────────────────────────

def _dr(account, amount):
    return {'account': account, 'debit_in_account_currency': round(amount, 2)}


def _cr(account, amount):
    return {'account': account, 'credit_in_account_currency': round(amount, 2)}


def _security_label(security: Security | None) -> str:
    if security is None:
        return 'security'
    return (security.ticker_symbol or '').strip() or (security.name
                                                       or 'security')


def _remark(txn: SecurityTransaction, security: Security | None) -> str:
    """'Bought 100 TEST-AAPL at $150.00 = $15,000.00' — the security-level
    detail the CPA reads on the voucher."""
    label = _security_label(security)
    qty = abs(float(txn.quantity or 0.0))
    price = float(txn.price or 0.0)
    total = abs(float(txn.amount or 0.0))
    verb = {'buy': 'Bought', 'sell': 'Sold', 'fee': 'Fee',
            'cash': 'Cash'}.get((txn.type or '').lower(), (txn.type or 'Txn').title())
    if (txn.type or '').lower() in ('buy', 'sell'):
        return (f'{verb} {qty:g} {label} at ${price:,.2f} = ${total:,.2f}')
    return f'{verb}: {label} ${total:,.2f} — {txn.name or txn.subtype or ""}'.strip()


def build_investment_je(client: ERPNextClient, txn: SecurityTransaction,
                        account: PlaidAccount, company: str,
                        security: Security | None) -> tuple:
    """(doc, lot_plan) for one SecurityTransaction — doc is None when the type
    is not posted (transfer, cancel, unrecognized), and lot_plan is the FIFO
    consumption to apply only after a successful post (empty except on a
    FIFO-costed sell).

    Never mutates ERPNext state or the local lots — only reads, and creates GL
    accounts. The cash leg is resolved by `cash_side_account` (clearing for
    paired, bank for unpaired)."""
    kind = (txn.type or '').lower()
    is_option = bool(security and security.is_option)
    amount = abs(float(txn.amount or 0.0))
    qty = abs(float(txn.quantity or 0.0))
    if amount == 0.0:
        return None, []
    cash = cash_side_account(client, account, company)
    if not cash:
        log.warning('no cash-side account for %s — cannot post', account.account_id)
        return None, []

    plan = []
    accounts = None
    if is_option:
        premium = options_premium_income_account(client, company)
        loss = options_premium_losses_account(client, company)
        if kind == 'sell':               # sell-to-open: premium received
            accounts = [_dr(cash, amount), _cr(premium, amount)]
        elif kind == 'buy':              # buy-to-close: cost of closing
            accounts = [_dr(loss, amount), _cr(cash, amount)]
    elif kind == 'buy':
        ms = marketable_securities_account(
            client, company, security and security.ticker_symbol,
            security and security.type)
        accounts = [_dr(ms, amount), _cr(cash, amount)]
    elif kind == 'sell':
        ms = marketable_securities_account(
            client, company, security and security.ticker_symbol,
            security and security.type)
        basis, _method, plan = cost_basis_for_sell(txn, qty)
        gain = round(amount - basis, 2)
        lines = [_dr(cash, amount), _cr(ms, basis)]
        if gain > 0:
            lines.append(_cr(realized_gains_account(client, company), gain))
        elif gain < 0:
            lines.append(_dr(realized_losses_account(client, company), -gain))
        accounts = lines
    elif kind == 'fee':
        accounts = [_dr(advisory_fees_account(client, company), amount),
                    _cr(cash, amount)]
    elif kind == 'cash':
        # A dividend or interest RECEIVED (Plaid amount negative = cash in).
        sub = (txn.subtype or '').lower()
        income = (interest_income_account(client, company)
                  if 'interest' in sub
                  else dividend_income_account(client, company))
        accounts = [_dr(cash, amount), _cr(income, amount)]
    else:
        return None, []  # transfer, cancel, unrecognized → not posted

    if accounts is None or any(a.get('account') is None for a in accounts):
        return None, []
    doc = {'doctype': JOURNAL_ENTRY_DT, 'voucher_type': 'Journal Entry',
           'company': company, 'user_remark': _remark(txn, security),
           'accounts': accounts}
    if txn.date:
        doc['posting_date'] = txn.date.isoformat()
    return doc, plan


# ── generation, idempotent + gated ───────────────────────────────────────────

def generate_investment_je(client: ERPNextClient,
                           txn: SecurityTransaction) -> GeneratedJournalEntry | None:
    """Post one SecurityTransaction as a Journal Entry, or return the existing
    GeneratedJournalEntry when it is already posted.

    Idempotent on `plaid_investment_transaction_id`: a re-sync of the same
    trade recognizes the prior row and generates nothing. Gated by the
    per-Item kill switch — returns None without touching ERPNext when posting
    is disabled. Never raises; a failure is recorded on an 'error' row.

    Cost-basis lot decrements (FIFO) and the GJE row commit together, so an
    ERPNext failure rolls the lot consumption back with it."""
    itx = txn.plaid_investment_transaction_id
    existing = (GeneratedJournalEntry.query
                .filter_by(plaid_investment_transaction_id=itx).first())
    if existing is not None and existing.erpnext_journal_entry_name:
        return existing

    account = PlaidAccount.query.filter_by(account_id=txn.account_id).first()
    if account is None or not posting_enabled(account):
        return None
    company = owning_company_for_account_id(txn.account_id)
    if not company:
        log.info('no owning company for %s — not posting investment JE',
                 txn.account_id)
        return None
    security = (Security.query.filter_by(security_id=txn.security_id).first()
                if txn.security_id else None)

    try:
        doc, lot_plan = build_investment_je(client, txn, account, company,
                                            security)
    except (ERPNextAPIError, ERPNextError):
        db.session.rollback()
        log.warning('failed to build investment JE for %s', itx, exc_info=True)
        return _record_error(txn, 'could not build JE (GL account create failed)')
    if doc is None:
        return None  # a type we don't post

    gje = existing or GeneratedJournalEntry(
        plaid_transaction_id=f'inv:{itx}',
        plaid_investment_transaction_id=itx)
    if existing is None:
        db.session.add(gje)
    gje.amount = round(abs(float(txn.amount or 0.0)), 2)
    gje.merchant_name = _security_label(security)[:255]
    gje.description = doc['user_remark'][:2000]
    gje.rule_name = 'investment'
    gje.updated_at = _now()
    try:
        created = client.create_doc(JOURNAL_ENTRY_DT, doc)
        name = created.get('name')
        if not name:
            raise ERPNextAPIError('ERPNext returned no Journal Entry name',
                                  status_code=None)
        gje.erpnext_journal_entry_name = name
        # The JE posted — NOW consume the FIFO lots, in the same transaction as
        # the GJE row, so the two commit or roll back together.
        _apply_lot_plan(lot_plan)
        cfg = current_app.config
        if cfg.get('ERPNEXT_JOURNAL_ENTRY_AUTO_SUBMIT', False):
            from .categorization import _submit_je
            _submit_je(client, name)
            gje.state = 'approved'
        else:
            gje.state = cfg.get('ERPNEXT_JOURNAL_ENTRY_REVIEW_STATE',
                                'pending_review') or 'pending_review'
        gje.error_message = None
        db.session.commit()
        log.info('posted investment JE %s for %s', name, itx)
        audit.record('investment_journal_entry_generated',
                     subject_type='GeneratedJournalEntry', subject_id=gje.id,
                     after={'journal_entry': name, 'state': gje.state,
                            'plaid_investment_transaction_id': itx, 'doc': doc},
                     notes=f'investment {txn.type} → {name}')
        return gje
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        return _record_error(txn, str(e)[:2000])


def _record_error(txn: SecurityTransaction, message: str) -> GeneratedJournalEntry:
    itx = txn.plaid_investment_transaction_id
    gje = (GeneratedJournalEntry.query
           .filter_by(plaid_investment_transaction_id=itx).first())
    if gje is None:
        gje = GeneratedJournalEntry(plaid_transaction_id=f'inv:{itx}',
                                    plaid_investment_transaction_id=itx)
        db.session.add(gje)
    gje.state = 'error'
    gje.rule_name = 'investment'
    gje.error_message = message
    gje.updated_at = _now()
    db.session.commit()
    log.warning('investment JE failed for %s: %s', itx, message)
    return gje


GL_ENTRY_DT = 'GL Entry'


def _account_is_empty(client, account_docname: str) -> bool:
    """True when an Account has NO posted GL activity — safe to delete. A draft
    (docstatus 0) Journal Entry produces no GL Entry, so the per-ticker leaves,
    whose JEs were all drafts, read empty; a submitted entry would leave a GL
    row and this returns False, protecting real balances."""
    gl = client.list_docs(GL_ENTRY_DT,
                          filters=[['account', '=', account_docname]],
                          fields=['name'], limit_page_length=1)
    return not gl


def rebuild_investment_accounts(client: ERPNextClient,
                                company: str | None = None) -> dict:
    """One-shot cleanup of the v0.5.10 per-ticker sprawl (v0.5.11).

    Deletes the DRAFT investment Journal Entries and the 103 stranded
    'Marketable Securities - <ticker>' leaves (plus the 'Other' leaf), so a
    re-run of post_investments_for_account rebuilds everything against the six
    1321-1326 asset-type leaves under the EXISTING 1320 group. The 1320 group
    itself — which holds the real brokerage balance — is NEVER touched.

    SAFETY. Touches ONLY draft (docstatus 0) JEs and ONLY empty accounts. If it
    encounters a SUBMITTED investment JE it aborts immediately without deleting
    anything further — submitted entries are real ledger history. An account
    with any GL activity is skipped, not deleted. Idempotent: a second run finds
    nothing left and reports zeros.

    Returns {'drafts_deleted','accounts_deleted','groups_deleted',
    'skipped_nonzero','aborted','reason'}."""
    stats = {'drafts_deleted': 0, 'accounts_deleted': 0, 'groups_deleted': 0,
             'skipped_nonzero': 0, 'aborted': False, 'reason': ''}
    companies = ([company] if company else sorted({
        owning_company_for_account_id(a.account_id)
        for a in PlaidAccount.query.filter(
            PlaidAccount.type == 'investment').all()} - {None, ''}))
    if not companies:
        return stats

    # 1. Draft investment JEs → cancel/delete in ERPNext, drop the GJE row.
    drafts = (GeneratedJournalEntry.query
              .filter(GeneratedJournalEntry.plaid_investment_transaction_id
                      .isnot(None),
                      GeneratedJournalEntry.erpnext_journal_entry_name
                      .isnot(None),
                      GeneratedJournalEntry.state == 'pending_review').all())
    for gje in drafts:
        je = client.get_doc(JOURNAL_ENTRY_DT, gje.erpnext_journal_entry_name)
        if je is not None and int(je.get('docstatus') or 0) == 1:
            stats['aborted'] = True
            stats['reason'] = (f'submitted JE {gje.erpnext_journal_entry_name} '
                               '— aborted, deleted nothing further')
            db.session.rollback()
            return stats
        if je is not None:
            client.delete_doc(JOURNAL_ENTRY_DT, gje.erpnext_journal_entry_name)
        db.session.delete(gje)
        stats['drafts_deleted'] += 1
    db.session.commit()

    # 2. The orphan per-ticker LEAVES (+ 'Other'), balance-checked. The filter
    #    is is_group=0, so the 1320 GROUP that holds the real balance can never
    #    match here — it is deliberately never a deletion candidate.
    for comp in companies:
        for a in _find_accounts(client, comp, is_group=0):
            if not (a.get('account_name') or '').startswith(
                    'Marketable Securities - '):
                continue
            if not _account_is_empty(client, a['name']):
                stats['skipped_nonzero'] += 1
                continue
            client.delete_doc(ACCOUNT_DT, a['name'])
            stats['accounts_deleted'] += 1
    return stats


def post_investments_for_account(client: ERPNextClient, account_id: str) -> dict:
    """Post every not-yet-posted SecurityTransaction for one account. Never
    raises; returns {'posted', 'skipped', 'failed'}."""
    stats = {'posted': 0, 'skipped': 0, 'failed': 0}
    account = PlaidAccount.query.filter_by(account_id=account_id).first()
    if account is None or not posting_enabled(account):
        return stats
    rows = (SecurityTransaction.query
            .filter_by(account_id=account_id)
            .order_by(SecurityTransaction.date.asc(),
                      SecurityTransaction.id.asc()).all())
    for txn in rows:
        try:
            gje = generate_investment_je(client, txn)
        except Exception:  # pragma: no cover - generate already swallows
            db.session.rollback()
            stats['failed'] += 1
            continue
        if gje is None:
            stats['skipped'] += 1
        elif gje.state == 'error':
            stats['failed'] += 1
        else:
            stats['posted'] += 1
    return stats


# ── clearing balance check ───────────────────────────────────────────────────

def clearing_imbalance(account_id: str) -> float:
    """How far a paired brokerage's Cash Clearing account is from zero, in
    dollars (v0.5.1).

    Every SecurityTransaction's cash leg posts to clearing, and every companion
    'Brokerage activity' BankTransaction posts the OTHER clearing leg — so a
    consistent set nets to zero. This projects that net WITHOUT reading ERPNext:
    the security side's cash-in-positive total minus the companion's
    cash-in-positive total. Zero means every trade has its matching companion
    movement; a non-zero result is a mismatched pair worth investigating.

    0.0 for an unpaired account (no clearing account, nothing to balance)."""
    from .models import BankTransaction
    from . import statements as stmts
    account = PlaidAccount.query.filter_by(account_id=account_id).first()
    partner_id = (account.paired_account_id or '').strip() if account else ''
    if not partner_id:
        return 0.0
    # Security side: cash-in-positive is -amount (Plaid amount positive = out).
    # Only the types we actually post to clearing count.
    posted_types = ('buy', 'sell', 'fee', 'cash')
    sec = (SecurityTransaction.query
           .filter(SecurityTransaction.account_id.in_(
               tuple(stmts.supersede_chain(account_id))),
                   SecurityTransaction.type.in_(posted_types))
           .all())
    security_cash_in = -sum(float(t.amount or 0.0) for t in sec)
    partner_ids = stmts.supersede_chain(partner_id)
    companion = (BankTransaction.query
                 .filter(BankTransaction.account_id.in_(tuple(partner_ids)),
                         BankTransaction.pending.is_(False),
                         BankTransaction.removed.is_(False))
                 .all())
    companion_cash_in = -sum(float(t.amount or 0.0) for t in companion)
    return round(security_cash_in - companion_cash_in, 2)
