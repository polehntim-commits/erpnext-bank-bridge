# SPDX-License-Identifier: MIT
"""Loans: the debt the books were leaving out (v0.4.14).

THE GAP. Until now `is_supported` refused every loan account — a mortgage, an
equipment note, a student loan — because a loan is not a Bank Account in
ERPNext's sense. The consequence was not that loans were handled badly; it was
that they were absent. A farm with a $200,000 orchard mortgage had a balance
sheet overstating its net worth by $200,000, and the payments leaving the
chequing account had nowhere honest to go.

Worse, the natural workaround makes it wrong in a second way. Categorize the
whole $2,000 payment as an expense and you overstate expenses by the principal
portion AND still leave the debt off the balance sheet. Principal repayment is
not an expense; it is the settlement of a liability.

THE ACCOUNTING, and why it is two entries rather than one.

The textbook entry for a mortgage payment is three lines:

    Dr  Mortgage Liability      400      (principal — settles debt)
    Dr  Interest Expense      1,600      (interest — a real cost)
        Cr  Bank                   2,000 (cash out)

Nothing in this codebase can express a three-line entry: every Journal Entry
builder here — the rules engine, intercompany, opening balances, revaluation —
emits exactly two lines, and a CategorizationRule has a single `offset_account`
with one amount. Building N-line rules would mean a rule child table, an
allocation model, a rules-editor rewrite, and a decision about which line the
party attaches to.

It would also be the wrong shape. A mortgage's principal/interest split changes
every single month, so any allocation stored ON a rule is wrong from month two
onward.

So this decomposes into two entries that are each independently true, each two
lines, and each expressible with machinery that already exists:

    payment   Dr  Loan Liability   Cr  Bank            (a categorization rule)
    accrual   Dr  Interest Expense Cr  Loan Liability  (generated here)

Net effect on the ledger is identical to the three-line entry. It is arguably
more faithful, because it is what actually happens: interest accrues against the
loan continuously, and payments settle the balance. It also means the split is
never stored anywhere — the accrual is computed fresh from live lender data each
time, so a changing monthly ratio needs no special handling at all.

THE PAYMENT HALF NEEDS NO NEW CODE. Once a loan has a GL account, that account
appears in the rules editor's offset dropdown like any other, and a rule
pointing a mortgage payment at it produces `Dr Loan Liability / Cr Bank`
already. What this module adds is the accrual half, plus everything needed to
make a loan exist in ERPNext at all.

EXACT FIGURES ONLY, NEVER AN ESTIMATE. Interest comes from the lender's own
year-to-date `ytd_interest_paid`, differenced against what was last booked. An
amortization estimate (balance × rate ÷ 12) was considered and rejected for the
same reason statements refuse to guess an unparseable balance: it silently
diverges from the lender's real figure whenever there is an extra payment, an
escrow adjustment, a fee or a rate change, and nobody reconciles a number they
were never told was approximate. A lender that reports no year-to-date interest
gets no accrual, and the Accounts page says so.

LOANS ARE BALANCE-ONLY. Plaid does return transactions for loan accounts, and
posting them would double-count every payment — the money leaving chequing is
already mirrored on the chequing side, which is where the payment is booked
from. See sync_engine._eligible_account_map.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

from flask import current_app

from . import db
from . import erpnext_accounts
from .erpnext_client import ERPNextAPIError, ERPNextError
from .models import GeneratedJournalEntry, PlaidAccount, PlaidItem

log = logging.getLogger('bankbridge.loans')

JOURNAL_ENTRY_DT = 'Journal Entry'
ACCOUNT_DT = 'Account'

# The GeneratedJournalEntry key an interest accrual claims. Date-scoped like a
# revaluation — accruals recur — so a sync running twice in a day accrues once.
SYNTHETIC_PREFIX = 'loan-interest:'
RULE_LABEL = 'Loan interest'

# Reserved account_number for the auto-created Interest Expense leaf. 5000s is
# the conventional Expense band in the numbering scheme v0.3.9 established.
_INTEREST_ACCOUNT_NUMBER = 5300


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── configuration ───────────────────────────────────────────────────────────

def is_enabled() -> bool:
    """Whether loan accounts are imported and tracked at all (LOANS_ENABLED,
    default on). Off restores the pre-v0.4.14 behaviour exactly: loans are
    unsupported, no button, nothing created."""
    return bool(current_app.config.get('LOANS_ENABLED', True))


def interest_accrual_enabled() -> bool:
    """Whether interest accrual entries are generated
    (LOAN_INTEREST_ACCRUAL_ENABLED, default on). Off still imports the loan and
    tracks its balance — an operator who books interest by hand from the
    lender's statement wants exactly that."""
    return bool(current_app.config.get('LOAN_INTEREST_ACCRUAL_ENABLED', True))


def interest_account_name() -> str:
    """The expense leaf accruals debit (LOAN_INTEREST_ACCOUNT_NAME, default
    'Interest Expense')."""
    return ((current_app.config.get('LOAN_INTEREST_ACCOUNT_NAME')
             or 'Interest Expense').strip() or 'Interest Expense')


def min_accrual() -> float:
    """The smallest interest movement worth an entry
    (LOAN_MIN_ACCRUAL, default 1.00). Same reasoning as the revaluation
    threshold: nobody wants to approve a Journal Entry for eleven cents."""
    try:
        return abs(float(current_app.config.get('LOAN_MIN_ACCRUAL', 1.00)))
    except (TypeError, ValueError):
        return 1.00


# ── liability detail ────────────────────────────────────────────────────────

def is_loan_account(account: PlaidAccount) -> bool:
    return bool(account is not None
                and erpnext_accounts.is_loan_type(account.type,
                                                  account.subtype))


def loan_accounts() -> list:
    """Every linked loan account, mapped or not."""
    return [a for a in PlaidAccount.query.order_by(PlaidAccount.id).all()
            if is_loan_account(a)]


def detail_for(account: PlaidAccount) -> dict:
    """The stored liability detail for one account, or {}. Tolerates a corrupt
    or absent blob — this feeds a UI panel, and a bad JSON string must not 500 a
    page."""
    blob = (account.liability_detail or '').strip()
    if not blob:
        return {}
    try:
        parsed = json.loads(blob)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def store_detail(account: PlaidAccount, detail: dict) -> None:
    """Persist one account's liability detail verbatim."""
    account.liability_detail = json.dumps(detail, default=str)
    account.liability_refreshed_at = _now()
    account.updated_at = _now()


def refresh_liabilities(item: PlaidItem, plaid_client, access_token: str) -> int:
    """Pull /liabilities/get for one Item and store what it says about each loan
    account. Returns how many accounts were updated.

    Never raises, and treats an empty answer as ordinary: the `liabilities`
    product must be approved on the Plaid application AND requested at link time
    AND offered by the institution, so "no detail" is the common case on an
    install that has not enabled it. Everything downstream degrades to "balance
    tracked, interest not split"."""
    if not is_enabled() or item is None:
        return 0
    accounts = [a for a in PlaidAccount.query.filter_by(
        item_id=item.item_id).all() if is_loan_account(a)]
    if not accounts:
        return 0            # no loans on this Item — don't spend the call
    try:
        detail = plaid_client.liabilities_get(access_token)
    except Exception as e:  # the wrapper already swallows, this is belt+braces
        log.info('liabilities unavailable for item %s: %s', item.item_id, e)
        return 0
    if not detail:
        return 0
    updated = 0
    for account in accounts:
        row = detail.get(account.account_id)
        if not row:
            continue
        store_detail(account, row)
        updated += 1
    if updated:
        db.session.commit()
        log.info('refreshed liability detail for %d loan account(s) on %s',
                 updated, item.item_id)
    return updated


# ── the interest expense account ────────────────────────────────────────────

def _expense_root(client, company: str) -> str | None:
    """The company's root Expense group. Mirrors opening_balance._equity_root,
    including declining to invent one: creating a root account is a
    chart-of-accounts decision Bank Bridge has no business making."""
    groups = erpnext_accounts._find_accounts(client, company, is_group=1,
                                             root_type='Expense')
    if not groups:
        return None
    for g in groups:
        if not (g.get('parent_account') or ''):
            return g['name']
    return groups[0]['name']


def ensure_interest_account(client, company: str) -> str | None:
    """Find (or create) the Interest Expense leaf for `company`, or None when
    the chart has no Expense branch.

    Idempotent — an existing leaf of the same name under this company is reused,
    so every loan's interest lands in ONE account and the chart never
    accumulates duplicates. A chart that already has an 'Interest Expense' (most
    stock ERPNext charts do, under Indirect Expenses) is adopted rather than
    duplicated, which is the point of matching on name."""
    name = interest_account_name()
    try:
        existing = client.list_docs(
            ACCOUNT_DT,
            filters=[['account_name', '=', name], ['company', '=', company],
                     ['is_group', '=', 0]],
            fields=['name'], limit_page_length=1)
    except (ERPNextAPIError, ERPNextError) as e:
        log.warning('could not look up %r for %s: %s', name, company,
                    str(e)[:200])
        return None
    if existing:
        return existing[0]['name']
    root = _expense_root(client, company)
    if not root:
        log.info('no Expense branch for company %r; cannot provision %r — '
                 'loan interest accrual is unavailable for this company',
                 company, name)
        return None
    doc = {'account_name': name, 'parent_account': root, 'company': company,
           'is_group': 0, 'account_type': 'Expense Account'}
    number = erpnext_accounts._reserved_group_number(client, company,
                                                     _INTEREST_ACCOUNT_NUMBER)
    if number:
        doc['account_number'] = number
    try:
        created = client.create_doc(ACCOUNT_DT, doc)
    except (ERPNextAPIError, ERPNextError) as e:
        log.warning('could not create %r under %r: %s', name, root, str(e)[:200])
        return None
    docname = (created or {}).get('name')
    log.info('created expense account %s for loan interest', docname)
    return docname


# ── the accrual ─────────────────────────────────────────────────────────────

def synthetic_transaction_id(account: PlaidAccount, when: date) -> str:
    return f'{SYNTHETIC_PREFIX}{account.account_id}:{when.isoformat()}'


def is_interest_entry(entry: GeneratedJournalEntry) -> bool:
    """Whether a row is a loan interest accrual rather than an opening balance,
    a revaluation, an intercompany leg or a rules-engine entry."""
    return (entry.plaid_transaction_id or '').startswith(SYNTHETIC_PREFIX)


def existing_entry(account: PlaidAccount, when: date):
    return GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=synthetic_transaction_id(account, when)).first()


def interest_delta(account: PlaidAccount) -> tuple[float | None, str]:
    """(interest accrued since the last booking, source) for one loan.

    None means "cannot be established, post nothing" — the caller seeds instead.
    Sources: 'reported' (a real delta to book), 'seed' (first sight of this
    lender's counter), 'rollover' (the year turned), 'none' (no data).

    THE YEAR ROLLOVER is the subtle one. `ytd_interest_paid` resets to zero each
    January, so a naive difference would produce a large NEGATIVE accrual — a
    credit to Interest Expense wiping out the year's costs. When the new figure
    is BELOW what was booked, the year has turned and the new value IS the
    delta: everything reported so far this year is unbooked."""
    detail = detail_for(account)
    reported = detail.get('ytd_interest_paid')
    if reported is None:
        return None, 'none'
    try:
        reported = round(float(reported), 2)
    except (TypeError, ValueError):
        return None, 'none'
    booked = account.loan_ytd_interest_booked
    if booked is None:
        # First sight. Booking `reported` now would post the whole year to date
        # as one entry for interest that accrued before Bank Bridge was
        # watching — and, on an upgrade, for a loan that was never on the books
        # at all. Seed instead.
        return None, 'seed'
    booked = round(float(booked), 2)
    if reported < booked:
        return reported, 'rollover'
    return round(reported - booked, 2), 'reported'


def build_interest_document(account: PlaidAccount, loan_account: str,
                            interest_account: str, company: str,
                            amount: float, posting_date) -> dict:
    """The Journal Entry payload for one interest accrual: a two-line Dr/Cr that
    balances by construction.

    Pure — no ERPNext calls, no writes. Direction is never in question: interest
    is a cost incurred (debit the expense) that increases what is owed (credit
    the liability). Unlike an opening balance there is no sign to reason about,
    because a negative accrual is not a thing — the caller refuses one."""
    magnitude = round(abs(float(amount or 0.0)), 2)
    label = (account.name or account.official_name or account.mask
             or account.account_id)
    doc = {
        'doctype': JOURNAL_ENTRY_DT,
        'voucher_type': 'Journal Entry',
        'company': company,
        'user_remark': (f'Interest accrued on {label} '
                        f'({magnitude:,.2f}), per lender year-to-date figures'),
        'accounts': [
            {'account': interest_account,
             'debit_in_account_currency': magnitude},
            {'account': loan_account,
             'credit_in_account_currency': magnitude},
        ],
    }
    if posting_date:
        doc['posting_date'] = (posting_date.isoformat()
                               if hasattr(posting_date, 'isoformat')
                               else str(posting_date))
    return doc


def _record_entry(account: PlaidAccount, when: date, je_name: str, doc: dict,
                  amount: float, state: str = 'pending_review',
                  error_message: str | None = None) -> GeneratedJournalEntry:
    entry = existing_entry(account, when)
    if entry is None:
        entry = GeneratedJournalEntry(
            plaid_transaction_id=synthetic_transaction_id(account, when))
        db.session.add(entry)
    entry.rule_id = None
    entry.rule_name = RULE_LABEL
    entry.erpnext_journal_entry_name = je_name or None
    entry.state = state
    entry.amount = round(abs(float(amount or 0.0)), 2)
    entry.merchant_name = ''
    entry.description = (doc.get('user_remark') or '')
    entry.error_message = error_message
    entry.updated_at = _now()
    db.session.flush()
    return entry


def _seed(account: PlaidAccount, reported, reason: str) -> dict:
    """Record the lender's counters without posting anything."""
    detail = detail_for(account)
    account.loan_ytd_interest_booked = reported
    principal = detail.get('ytd_principal_paid')
    try:
        account.loan_ytd_principal_seen = (round(float(principal), 2)
                                           if principal is not None else None)
    except (TypeError, ValueError):
        account.loan_ytd_principal_seen = None
    account.updated_at = _now()
    db.session.commit()
    log.info('loan interest baseline seeded for %s at %s (%s) — no entry '
             'posted', account.account_id, reported, reason)
    return {'status': 'seeded', 'amount': 0.0, 'message': reason,
            'journal_entry': None, 'entry_id': None}


def _result(status: str, message: str, *, amount: float = 0.0,
            entry=None) -> dict:
    return {'status': status, 'message': message, 'amount': amount,
            'journal_entry': (entry.erpnext_journal_entry_name if entry else None),
            'entry_id': (entry.id if entry else None)}


def accrue_interest(client, account: PlaidAccount, *,
                    posting_date: date | None = None) -> dict:
    """Book the interest one loan has accrued since the last accrual.

    Returns {'status', 'message', 'amount', 'journal_entry', 'entry_id'} where
    status is 'posted', 'seeded', 'unchanged', 'skipped' or 'error'.

    NEVER raises — this runs inside the sync path."""
    if not is_enabled() or not interest_accrual_enabled():
        return _result('skipped', 'loan interest accrual is disabled')
    if not is_loan_account(account):
        return _result('skipped', 'not a loan account')
    loan_gl = (account.erpnext_gl_account_name or '').strip()
    if not loan_gl:
        return _result('skipped', 'no ERPNext GL account for this loan')
    if client is None:
        return _result('skipped', 'ERPNext is not configured')

    amount, source = interest_delta(account)
    if source == 'none':
        return _result(
            'skipped',
            'this lender does not report year-to-date interest through Plaid, '
            'so interest cannot be split automatically')
    detail = detail_for(account)
    reported = detail.get('ytd_interest_paid')
    if amount is None:
        return _seed(account, round(float(reported), 2),
                     'first sight of this lender’s year-to-date figures')
    if amount < min_accrual():
        return _result('unchanged',
                       f'{amount:.2f} of new interest is under the '
                       f'{min_accrual():.2f} threshold', amount=amount)

    when = posting_date or date.today()
    already = existing_entry(account, when)
    if already is not None and already.state != 'rejected':
        return _result('unchanged', f'already accrued on {when.isoformat()}',
                       amount=amount, entry=already)

    company = erpnext_accounts.owning_company_for(account)
    if not company:
        return _result('skipped', 'no ERPNext Company for this loan')
    interest_gl = ensure_interest_account(client, company)
    if not interest_gl:
        return _result('skipped',
                       f'no Interest Expense account available under {company}')

    doc = build_interest_document(account, loan_gl, interest_gl, company,
                                  amount, when)
    try:
        created = client.create_doc(JOURNAL_ENTRY_DT, doc)
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        detail_msg = str(e)[:500]
        entry = _record_entry(account, when, '', doc, amount, state='error',
                              error_message=detail_msg)
        db.session.commit()
        log.warning('interest accrual failed for %s: %s', account.account_id,
                    detail_msg)
        return _result('error', detail_msg, amount=amount, entry=entry)

    je_name = (created or {}).get('name') or ''
    entry = _record_entry(account, when, je_name, doc, amount)
    # Advance the booked counter only once the entry exists, so a failed write
    # is retried rather than silently swallowing a month of interest.
    account.loan_ytd_interest_booked = round(float(reported), 2)
    principal = detail.get('ytd_principal_paid')
    try:
        account.loan_ytd_principal_seen = (round(float(principal), 2)
                                           if principal is not None else None)
    except (TypeError, ValueError):
        pass
    account.updated_at = _now()
    db.session.commit()
    log.info('accrued %.2f interest on %s as %s (%s)', amount,
             account.account_id, je_name, source)
    try:
        from . import audit
        audit.record('loan_interest_accrued', subject_type='PlaidAccount',
                     subject_id=account.account_id,
                     after={'amount': amount, 'source': source,
                            'journal_entry': je_name},
                     notes=f'interest accrual of {amount:,.2f}')
    except Exception:  # pragma: no cover - auditing must not fail the sync
        log.debug('accrual audit failed', exc_info=True)
    return _result('posted', f'accrued {amount:,.2f}', amount=amount,
                   entry=entry)


def blank_stats() -> dict:
    return {'scanned': 0, 'posted': 0, 'seeded': 0, 'unchanged': 0,
            'skipped': 0, 'failed': 0, 'total_interest': 0.0, 'errors': []}


def accrue_all(client, *, posting_date: date | None = None) -> dict:
    """Accrue interest on every mapped loan. Never raises."""
    stats = blank_stats()
    if not is_enabled() or not interest_accrual_enabled() or client is None:
        return stats
    for account in loan_accounts():
        if not (account.erpnext_gl_account_name or '').strip():
            continue        # not imported yet — nothing to post against
        if account.superseded_by_account_id:
            continue        # v0.4.11: its identity moved to a re-linked heir
        stats['scanned'] += 1
        try:
            result = accrue_interest(client, account, posting_date=posting_date)
        except Exception as e:  # pragma: no cover - accrue_interest is total
            db.session.rollback()
            log.warning('interest accrual crashed for %s: %s',
                        account.account_id, e, exc_info=True)
            stats['failed'] += 1
            stats['errors'].append(f'{account.account_id}: {e}')
            continue
        status = result['status']
        if status == 'posted':
            stats['posted'] += 1
            stats['total_interest'] = round(
                stats['total_interest'] + result['amount'], 2)
        elif status == 'error':
            stats['failed'] += 1
            stats['errors'].append(f"{account.account_id}: {result['message']}")
        elif status in ('seeded', 'unchanged', 'skipped'):
            stats[status] += 1
    if stats['posted']:
        log.info('[loans] %s', {k: v for k, v in stats.items() if k != 'errors'})
    return stats


# ── operator-facing summary ─────────────────────────────────────────────────

def summary(account: PlaidAccount) -> dict:
    """What the Accounts page shows for one loan: the lender's own figures plus
    an honest statement of whether interest can be split.

    `interest_split_available` is the one an operator needs: without year-to-date
    figures from the lender, Bank Bridge books the balance and leaves the
    interest to them, and saying so beats a silently missing accrual."""
    detail = detail_for(account)
    return {
        'account_id': account.account_id,
        'label': (account.name or account.official_name
                  or account.account_id),
        'liability_type': detail.get('liability_type') or '',
        'balance': account.balance_current,
        'interest_rate': detail.get('interest_rate'),
        'next_payment_due_date': detail.get('next_payment_due_date') or '',
        'last_payment_amount': detail.get('last_payment_amount'),
        'last_payment_date': detail.get('last_payment_date') or '',
        'origination_principal_amount': detail.get(
            'origination_principal_amount'),
        'maturity_date': detail.get('maturity_date') or '',
        'minimum_payment_amount': detail.get('minimum_payment_amount'),
        'ytd_interest_paid': detail.get('ytd_interest_paid'),
        'ytd_principal_paid': detail.get('ytd_principal_paid'),
        'ytd_interest_booked': account.loan_ytd_interest_booked,
        'interest_split_available': detail.get('ytd_interest_paid') is not None,
        'refreshed_at': (account.liability_refreshed_at.isoformat()
                         if account.liability_refreshed_at else None),
        'gl_account': account.erpnext_gl_account_name or '',
    }


def all_summaries() -> list:
    """Every linked loan, for the Accounts page panel."""
    return [summary(a) for a in loan_accounts()
            if not a.superseded_by_account_id]
