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


# ── accounting dimensions (v0.8.3) ───────────────────────────────────────────
#
# THE BUG THIS FIXES. v0.8.2 wired the writer into the sync and 455 investment
# JEs landed — every line of every one carrying `cost_center = Main - OML`, the
# Company default, and no Member. The 44 categorization rules that DO name a
# cost center only ever applied to the bank-transaction path
# (categorization.build_je_doc); investment JEs are built here, and this module
# had no dimension wiring at all. Account routing was never the problem — EEIIX
# reached 1323 Mutual Funds, the advisory fee reached 5700 — the postings simply
# had no segment to be filed under.
#
# WHY THE ITEM CARRIES IT. See PlaidItem.invest_je_cost_center: one Item is one
# custodial relationship, and the two legs of a single trade must not be able to
# land in different segments.
#
# WHY EVERY LINE, where a rule tags only its offset. A rule's bank line is the
# operator's own checking account and the movement of cash belongs to no
# value-chain segment, so tagging it would attribute the payment rather than the
# cost. An investment JE has no such line: both sides are the SAME activity —
# securities on one side, the Cash Clearing bridge (or the brokerage's own leaf)
# on the other — and both are investment activity. Tagging one side only would
# leave half of every trade filed under the Company default, which is precisely
# the state this release exists to end.

# The extra keys a Journal Entry Account line may carry, as
# {PlaidItem attribute: JE Account line key}. `cost_center` is a stock ERPNext
# field; `member` is a Link CUSTOM FIELD (Journal Entry Account-member → Member)
# on Tim's install. Frappe applies a child-table dict's keys to the child doc by
# FIELDNAME, and a custom field is an ordinary field of the doctype by the time
# a document is instantiated — so `{'account': …, 'member': 'MEM-0001'}` sets it
# with no special handling, and an install WITHOUT that custom field would
# ignore the key rather than fail. Both are therefore written the same way.
_JE_TAG_FIELDS = (('invest_je_cost_center', 'cost_center'),
                  ('invest_je_member', 'member'))


def _member_exists(client, name: str) -> bool | None:
    """Whether `name` is a Member docname ERPNext will accept on a JE line.
    TRI-STATE like erpnext_bank.cost_center_exists: True / False (ERPNext
    answered and has no such doc) / None (no verdict — unreachable, or the
    Member doctype isn't installed, in which case nothing can be concluded)."""
    if client is None or not name:
        return None
    try:
        doc = client.get_doc('Member', name)
    except (ERPNextAPIError, ERPNextError):
        return None
    return bool(isinstance(doc, dict) and doc.get('name'))


def resolve_je_tags(client, account: PlaidAccount,
                    company: str = '') -> dict:
    """The dimension keys every JE line for `account` should carry — {} when the
    owning Item sets none, which restores the pre-v0.8.3 behaviour exactly.

    FAIL-SOFT, and only on a POSITIVE denial. A dimension ERPNext *answers* it
    does not have is dropped with a warning and the line falls back to the
    server-side default, because one stale docname must not cost an operator
    every JE in a 455-transaction backfill. A dimension it could not check —
    unreachable, an API error, a doctype this install lacks — is written
    ANYWAY: refusing valid input during a transient outage is the worse failure,
    and ERPNext rejects a genuinely bad link at create time regardless. Same
    "positive mismatch only" discipline as the party_type migration and the
    rule-editor's cost-center validation.

    Resolved ONCE per batch by post_investments_for_account and threaded down,
    so a 455-trade backfill costs two lookups rather than 910."""
    item = PlaidItem.query.filter_by(item_id=account.item_id).first()
    if item is None:
        return {}
    tags: dict = {}
    for attr, field in _JE_TAG_FIELDS:
        value = (getattr(item, attr, None) or '').strip()
        if not value:
            continue                      # unset → write no key at all
        if field == 'cost_center':
            from . import erpnext_bank
            verdict = erpnext_bank.cost_center_exists(client, value, company)
        else:
            verdict = _member_exists(client, value)
        if verdict is False:
            log.warning('item %s names %s=%r, which ERPNext does not have — '
                        'posting without it (ERPNext will apply its own '
                        'default)', item.item_id, attr, value)
            continue
        tags[field] = value
    return tags


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


def member_contributions_account(client, company) -> str | None:
    """`Member Contributions` (Equity, 3200) — the owner's capital paid INTO the
    brokerage to fund net securities purchases (v0.5.15). Distributions OUT use
    the pre-existing `Member Distribution` (3201); this only creates the
    contribution side. Credit-normal equity, found-or-created under the Equity
    root."""
    return ensure_leaf(client, company, 'Member Contributions', 'Equity',
                       account_number='3200')


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

def _buy_cost_per_unit(buy_transaction_id: str | None) -> float | None:
    """The cost per unit of a BUY, as amount ÷ quantity (v0.5.14) — the universal
    per-unit basis that is exact for equities AND bonds. None when the buy row is
    absent or carries no quantity, so the caller can fall back."""
    if not buy_transaction_id:
        return None
    buy = (SecurityTransaction.query
           .filter_by(plaid_investment_transaction_id=buy_transaction_id).first())
    if buy is None:
        return None
    qty = abs(float(buy.quantity or 0.0))
    if qty == 0.0:
        return None
    return abs(float(buy.amount or 0.0)) / qty


def cost_basis_for_sell(txn: SecurityTransaction, sold_qty: float) -> tuple:
    """(cost_basis, method, plan) for a sell of `sold_qty` shares. PURE — reads
    the lots but does NOT mutate them; `plan` is the FIFO consumption to apply
    only AFTER the JE actually posts (see _apply_lot_plan), so an ERPNext
    failure never leaves inventory consumed for a sale that didn't book.

    SPECIFIC IDENTIFICATION first: when a TradedCycle names this sell, the cost
    is the matched BUY's cost-per-unit times the quantity sold.

    v0.5.14 · COST-PER-UNIT IS amount ÷ quantity, NEVER price. Plaid quotes an
    equity price per share (so price == amount/qty) but a BOND price per $100 of
    face while quantity is in face DOLLARS — so `price × qty` overstates a bond's
    basis ~100× (a $10,931 T-bill sale matched to a lot priced 98.78 booked
    $1,086,567 basis and a $1.08M phantom loss). Deriving the per-unit cost from
    the buy's own amount and quantity — both reliable cash figures — is exact for
    every instrument with no special-casing (Tim's universal rule).

    FIFO FALLBACK against RetainedLot rows, oldest purchase first (their
    cost_basis_per_share is likewise amount/qty, see strategy_tracker). Shares no
    lot covers are valued at PROCEEDS per share (zero gain on that portion),
    marked 'fifo_incomplete'.

    `method` is 'specific_id', 'fifo', 'fifo_incomplete', or 'no_basis'."""
    cycle = (TradedCycle.query
             .filter_by(sell_transaction_id=txn.plaid_investment_transaction_id)
             .first())
    if cycle is not None:
        cpu = _buy_cost_per_unit(cycle.buy_transaction_id)
        if cpu is not None:
            return round(cpu * sold_qty, 2), 'specific_id', []
        # else: the matched buy is gone/zero-qty — fall through to FIFO.

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
    # v0.5.12 · the fall-back basis for shares NO lot covers is valued at
    # PROCEEDS-per-share (amount / sold_qty), NOT txn.price. For the brokerage's
    # cash-sweep / money-market shares Plaid reports a price scaled ~100× off the
    # cash (e.g. qty 40000 × "price" 99.60 = $3.98M against $39.8k of actual
    # proceeds), so the old `price × sold_qty` fabricated a $3.94M basis and a
    # $3.94M phantom loss. Proceeds are the reliable figure; valuing the
    # uncovered shares at proceeds/share yields zero realized gain on them,
    # which is the correct conservative treatment when cost basis is unknown.
    proceeds = abs(float(txn.amount or 0.0))
    proceeds_per_share = (proceeds / sold_qty) if sold_qty else 0.0
    if remaining > 0.0000001:
        if basis == 0.0:
            return round(proceeds, 2), 'no_basis', []
        basis += remaining * proceeds_per_share
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
                        security: Security | None,
                        tags: dict | None = None) -> tuple:
    """(doc, lot_plan) for one SecurityTransaction — doc is None when the type
    is not posted (transfer, cancel, unrecognized), and lot_plan is the FIFO
    consumption to apply only after a successful post (empty except on a
    FIFO-costed sell).

    Never mutates ERPNext state or the local lots — only reads, and creates GL
    accounts. The cash leg is resolved by `cash_side_account` (clearing for
    paired, bank for unpaired).

    `tags` (v0.8.3) are the accounting dimensions to stamp on every line. None
    means "resolve them here" — correct but one lookup per call, so a batch
    caller resolves once with `resolve_je_tags` and passes the result in."""
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
        # v0.5.14 · a FIXED-INCOME position (T-bill / note) is bought at a
        # discount and redeemed at face; the positive difference is ACCRETED
        # DISCOUNT — Interest Income under §1.1275, not a capital gain. Equity
        # gains stay Realized Capital Gains. A fixed-income sale BELOW cost
        # (sold early into higher rates) is still a capital loss.
        is_fixed_income = bool(security
                               and 'fixed' in (security.type or '').lower())
        if gain > 0:
            income = (interest_income_account(client, company) if is_fixed_income
                      else realized_gains_account(client, company))
            lines.append(_cr(income, gain))
        elif gain < 0:
            lines.append(_dr(realized_losses_account(client, company), -gain))
        accounts = lines
    elif kind == 'fee':
        accounts = [_dr(advisory_fees_account(client, company), amount),
                    _cr(cash, amount)]
    elif kind == 'cash':
        # v0.5.12 · ONLY genuine income posts. A `cash` SecurityTransaction is a
        # dividend or interest RECEIVED — BUT Plaid also types the brokerage
        # CASH-SWEEP movements ('deposit'/'withdrawal', the "BANK DEPOSIT SWEEP"
        # in and out of the sweep fund) as `cash`. Those are the sweep itself,
        # already carried by the companion BankTransaction; the pre-v0.5.12 code
        # booked EVERY non-interest cash row as Dividend Income, which on the
        # live data was hundreds of deposits + withdrawals totalling millions of
        # phantom dividend income against a single genuine dividend. Post only
        # rows whose subtype actually names dividend or interest; skip the rest.
        sub = (txn.subtype or '').lower()
        if 'interest' in sub:
            income = interest_income_account(client, company)
        elif 'dividend' in sub:
            income = dividend_income_account(client, company)
        else:
            return None, []   # deposit / withdrawal / sweep — not income
        accounts = [_dr(cash, amount), _cr(income, amount)]
    else:
        return None, []  # transfer, cancel, unrecognized → not posted

    if accounts is None or any(a.get('account') is None for a in accounts):
        return None, []
    # v0.8.3 · the dimensions, on EVERY line (see the module note above). Applied
    # here rather than inside _dr/_cr so there is ONE place a line can acquire
    # one, and so a future line type cannot be added without them.
    line_tags = resolve_je_tags(client, account, company) if tags is None else tags
    if line_tags:
        for line in accounts:
            line.update(line_tags)
    doc = {'doctype': JOURNAL_ENTRY_DT, 'voucher_type': 'Journal Entry',
           'company': company, 'user_remark': _remark(txn, security),
           'accounts': accounts}
    if txn.date:
        doc['posting_date'] = txn.date.isoformat()
    return doc, plan


# ── generation, idempotent + gated ───────────────────────────────────────────

def generate_investment_je(client: ERPNextClient, txn: SecurityTransaction, *,
                           force: bool = False,
                           tags: dict | None = None
                           ) -> GeneratedJournalEntry | None:
    """Post one SecurityTransaction as a Journal Entry, or return the existing
    GeneratedJournalEntry when it is already posted.

    Idempotent on `plaid_investment_transaction_id`: a re-sync of the same
    trade recognizes the prior row and generates nothing. Gated by the
    per-Item kill switch — returns None without touching ERPNext when posting
    is disabled. Never raises; a failure is recorded on an 'error' row.

    `force=True` (v0.5.13) treats a CANCELLED GJE as if it were absent and
    regenerates a fresh draft over it. Without this, a SecurityTransaction whose
    JE was bulk-cancelled in ERPNext is blocked forever — its GJE still carries a
    (now-cancelled) `erpnext_journal_entry_name`, which the idempotency check
    reads as "already handled". pending_review and approved states are STILL
    protected regardless of force, so force never disturbs live drafts or
    approved entries.

    Cost-basis lot decrements (FIFO) and the GJE row commit together, so an
    ERPNext failure rolls the lot consumption back with it.

    `tags` (v0.8.3) is the pre-resolved dimension set — see
    build_investment_je. None resolves it per call."""
    itx = txn.plaid_investment_transaction_id
    existing = (GeneratedJournalEntry.query
                .filter_by(plaid_investment_transaction_id=itx).first())
    if existing is not None and existing.erpnext_journal_entry_name:
        if not (force and (existing.state or '') == 'cancelled'):
            return existing
        # force + cancelled → fall through and rebuild over this row (the code
        # below reuses `existing`, overwriting its JE name and state).
        existing.erpnext_journal_entry_name = None

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
                                            security, tags)
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
    reset = reset_investment_drafts(client)
    stats['drafts_deleted'] = reset['drafts_deleted']
    if reset['aborted']:
        stats['aborted'] = True
        stats['reason'] = reset['reason']
        return stats

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


def reset_investment_drafts(client: ERPNextClient,
                            account_id: str | None = None) -> dict:
    """Delete every DRAFT investment Journal Entry — in ERPNext and the GJE row
    that names it — so the next sync re-posts them from scratch (v0.8.3).

    This is the repair path for a batch that posted with the wrong shape rather
    than the wrong data: v0.8.2's 455 drafts are correct in account, amount and
    date, and wrong only in carrying the Company's default cost center. A JE's
    dimensions cannot be edited in bulk, and the GJE row makes the trade
    idempotently "already handled" — so clearing both is what lets
    post_investments_for_account rebuild them with the dimensions set.

    SAFETY, identical to rebuild_investment_accounts (which now calls this):
    only state='pending_review' rows are candidates, and each is re-checked in
    ERPNext — the first SUBMITTED docstatus aborts the whole pass with nothing
    further deleted, because a submitted entry is real ledger history. approved,
    error and cancelled rows are never touched (`reset_cancelled_gjes` owns the
    last of those). Idempotent: a second run finds nothing and reports zeros.

    `account_id` scopes it to one brokerage; omitted, it covers every account.

    Returns {'drafts_deleted', 'aborted', 'reason'}."""
    stats = {'drafts_deleted': 0, 'aborted': False, 'reason': ''}
    q = GeneratedJournalEntry.query.filter(
        GeneratedJournalEntry.plaid_investment_transaction_id.isnot(None),
        GeneratedJournalEntry.erpnext_journal_entry_name.isnot(None),
        GeneratedJournalEntry.state == 'pending_review')
    if account_id:
        itxs = tuple(
            t.plaid_investment_transaction_id for t in
            SecurityTransaction.query.filter_by(account_id=account_id).all())
        q = q.filter(GeneratedJournalEntry.plaid_investment_transaction_id
                     .in_(itxs or ('',)))
    for gje in q.all():
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
    return stats


def reset_cancelled_gjes(account_id: str | None = None) -> int:
    """Delete every CANCELLED investment GeneratedJournalEntry row (v0.5.13),
    optionally scoped to one account. Returns the count deleted.

    A bulk-cancel in ERPNext leaves the GJE rows behind carrying a cancelled
    `erpnext_journal_entry_name`, which blocks their SecurityTransactions from
    ever being regenerated. Clearing those rows lets a plain
    post_investments_for_account rebuild them from scratch. Touches ONLY
    state='cancelled' rows — pending_review, approved and error rows are never
    deleted. Idempotent: a second run finds nothing and returns 0."""
    q = GeneratedJournalEntry.query.filter(
        GeneratedJournalEntry.plaid_investment_transaction_id.isnot(None),
        GeneratedJournalEntry.state == 'cancelled')
    if account_id:
        itxs = tuple(
            t.plaid_investment_transaction_id for t in
            SecurityTransaction.query.filter_by(account_id=account_id).all())
        q = q.filter(GeneratedJournalEntry.plaid_investment_transaction_id
                     .in_(itxs or ('',)))
    n = 0
    for gje in q.all():
        db.session.delete(gje)
        n += 1
    db.session.commit()
    return n


def post_investments_for_account(client: ERPNextClient, account_id: str, *,
                                 force: bool = False) -> dict:
    """Post every not-yet-posted SecurityTransaction for one account. Never
    raises; returns {'posted', 'skipped', 'failed'}.

    `force=True` (v0.5.13) regenerates over CANCELLED GJEs — see
    generate_investment_je. pending_review/approved rows stay protected.

    v0.8.3 · the Item's accounting dimensions are resolved ONCE here and handed
    to every JE, so a backfill validates the cost center and the member a single
    time rather than once per trade."""
    stats = {'posted': 0, 'skipped': 0, 'failed': 0}
    account = PlaidAccount.query.filter_by(account_id=account_id).first()
    if account is None or not posting_enabled(account):
        return stats
    company = owning_company_for_account_id(account_id) or ''
    try:
        tags = resolve_je_tags(client, account, company)
    except (ERPNextAPIError, ERPNextError):
        # Resolution is already fail-soft per-dimension; this catches a client
        # that cannot answer at all. Posting with the server-side defaults beats
        # posting nothing — the same trade-off resolve_je_tags itself makes.
        log.warning('could not resolve investment JE dimensions for %s — '
                    'posting with ERPNext defaults', account_id, exc_info=True)
        tags = {}
    rows = (SecurityTransaction.query
            .filter_by(account_id=account_id)
            .order_by(SecurityTransaction.date.asc(),
                      SecurityTransaction.id.asc()).all())
    for txn in rows:
        try:
            gje = generate_investment_je(client, txn, force=force, tags=tags)
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
    dollars (v0.5.1, corrected v0.8.1).

    Each half of a trade posts to clearing from the opposite side, so a complete
    pair nets to zero and only the UNPAIRED remainder is left sitting there.
    This projects that remainder WITHOUT reading ERPNext: Σ over the legs
    `trade_pairing` could not match, cash-in-positive. Zero means every trade
    Bank Bridge can see has both its legs; a non-zero result is a worklist, and
    `list_unpaired_trades` is that worklist itemised.

    0.0 for an unpaired account (no clearing account, nothing to balance).

    v0.8.1 · DELEGATED to `trade_pairing.projected_clearing_imbalance`, which
    computes it from the same source tables by the same leg-pairing rules the
    itemisation uses. Until v0.8.1 this function had its own arithmetic —
    every clearing-typed security row's cash-in, minus every companion row's —
    and against Wells Fargo Advisors that arithmetic was wrong twice over. WFA
    puts BOTH halves of a trade on the brokerage account (a `buy` row and a
    `cash` settlement row, same security, same amount), so the security-side sum
    counted every trade twice; and the companion is an ordinary checking account
    whose debit-card traffic has no security leg by nature, so subtracting all
    of it charged the cardholder's groceries to Cash Clearing. ••9401's
    -$985,015.56 was mostly those two artifacts.

    Keeping the sum here and pairing over there would have left the scalar and
    its own itemisation disagreeing, with no way for an operator to know which
    to believe. One calculation, in the module that owns the pairing rules, is
    the only arrangement where they cannot drift."""
    from . import trade_pairing
    return trade_pairing.projected_clearing_imbalance(account_id)
