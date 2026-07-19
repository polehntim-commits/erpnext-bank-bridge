# SPDX-License-Identifier: MIT
"""Opening balances: book what an account ALREADY HELD when you linked it.

Bank Bridge mirrors transactions. A real bank account, though, has a balance on
the day you link it — the sum of every transaction that happened before Plaid
ever handed us a cursor. Without booking that, the ERPNext balance sheet reports
"movement since Bank Bridge started tracking", not "what this account holds",
and an account whose recent activity is net-negative shows a negative asset:

    Wells Fargo Money Market   opening 17,600.00
                               transactions since link  -17,550.00
    ERPNext (pre-v0.4.4)                    -17,550.00   ← wrong, and alarming
    ERPNext (with opening balance)               50.00   ← the actual position

So at initial import (and, for accounts linked before this shipped, via
`scripts/backfill_opening_balances.py`) we generate ONE Journal Entry per
account against an `Opening Balance Equity` account, auto-created under the
owning Company's Equity root if the chart doesn't ship one.

DIRECTION is the whole subtlety, and it comes from two independent facts:

  1. WHICH SIDE the account's GL leaf sits on. Depository and investment leaves
     live under Assets; credit cards and lines of credit live under Current
     Liabilities (see erpnext_accounts._gl_side). An asset opens by DEBIT, a
     liability opens by CREDIT — that is just what those root types mean.
  2. PLAID'S SIGN. `balances.current` is positive-means-*more-of-the-account*
     in every case, but "more" differs per type: for depository/investment a
     positive number is money you HAVE; for credit a positive number is money
     you OWE. Plaid does not flip the sign for liabilities — the account type
     carries that meaning.

Read together those give one rule, not two: book the account's natural opening
side, and flip it when the balance is negative (an overdrawn checking account,
or a credit card you have overpaid into a credit balance). The JE itself always
carries positive debit/credit amounts, which is what ERPNext wants.

    depository / investment, +17,600  →  Dr Bank GL       Cr Opening Balance Equity
    depository,                 -120  →  Dr Equity        Cr Bank GL   (overdrawn)
    credit card,               +2,400  →  Dr Equity        Cr Card GL   (owed)
    credit card,                 -75   →  Dr Card GL       Cr Equity    (overpaid)

The entry is created as a Draft in state `pending_review`, so it flows through
the same approve/reject workflow every rules-engine JE uses (v0.4.0.5) — an
estimate the operator disagrees with is rejected, and nothing is posted.

Idempotency is structural, not a flag: the GeneratedJournalEntry table is UNIQUE
on `plaid_transaction_id`, and an opening balance claims the synthetic key
`opening-balance:<plaid account_id>`. Re-importing, re-running the backfill, or
double-clicking the manual button all find that row and stop. `PlaidAccount.
opening_balance_je_id` is the denormalized pointer the Accounts page reads so
rendering a status column costs no join.

No Party is ever set. An opening balance is an equity event, not a purchase from
anyone — there is no Supplier or Customer on either leg.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from flask import current_app

from . import audit
from . import db
from . import erpnext_accounts
from .erpnext_client import ERPNextAPIError, ERPNextError
from .models import BankTransaction, GeneratedJournalEntry, PlaidAccount

log = logging.getLogger('bankbridge.opening_balance')

JOURNAL_ENTRY_DT = 'Journal Entry'
ACCOUNT_DT = 'Account'

# The synthetic `plaid_transaction_id` an opening balance JE claims. Namespaced
# with a colon so it can never collide with a real Plaid transaction id (those
# are opaque base64-ish tokens with no colon), and prefix-matchable so the UI can
# tell an opening balance apart from a rules-engine entry without a join.
SYNTHETIC_PREFIX = 'opening-balance:'

# What `rule_name` reads as on the GeneratedJournalEntry row — these entries have
# no CategorizationRule behind them, and the column is the audit dashboard's
# human label. Mirrors intercompany's 'Intercompany transfer'.
RULE_LABEL = 'Opening balance'

# Reserved account_number for the auto-created Opening Balance Equity leaf, in
# the conventional 3000s Equity band (v0.3.9 numbering). Only applied when the
# chart numbers its accounts at all, and bumped past any collision — same
# contract as the investment groups' 1300/1310.
_EQUITY_ACCOUNT_NUMBER = 3000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── configuration ───────────────────────────────────────────────────────────

def auto_book_enabled() -> bool:
    """Whether initial account import books an opening balance automatically
    (AUTO_BOOK_OPENING_BALANCE, default true). Off means the operator books each
    one by hand from the Accounts page — the entry is identical either way."""
    return bool(current_app.config.get('AUTO_BOOK_OPENING_BALANCE', True))


def equity_account_name() -> str:
    """The account name the Opening Balance Equity leaf is found-or-created under
    (OPENING_BALANCE_EQUITY_ACCOUNT_NAME, default 'Opening Balance Equity')."""
    return (current_app.config.get('OPENING_BALANCE_EQUITY_ACCOUNT_NAME')
            or 'Opening Balance Equity').strip() or 'Opening Balance Equity'


def opening_balance_date() -> date:
    """The posting date for auto-booked opening balances (OPENING_BALANCE_DATE).

    Default — and the value for the literal string 'today' — is today, which is
    right when you link an account mid-year and want the position from now on.
    An ISO date backdates it instead, which is what you want when the account
    should open at the start of a fiscal year:

        OPENING_BALANCE_DATE=2026-01-01

    An unparseable value falls back to today with a warning rather than failing
    the import — a typo in an env var must not block linking a bank."""
    raw = (current_app.config.get('OPENING_BALANCE_DATE') or '').strip()
    if not raw or raw.lower() == 'today':
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        log.warning('OPENING_BALANCE_DATE=%r is not an ISO date (YYYY-MM-DD); '
                    'using today instead', raw)
        return date.today()


# ── sign convention ─────────────────────────────────────────────────────────

def opens_by_debit(account: PlaidAccount) -> bool:
    """Whether this account's GL leaf opens on the DEBIT side, ignoring the
    balance's sign — i.e. purely "is this an asset?".

    Assets (depository, investment) increase by debit; liabilities (credit cards,
    lines of credit, loans) increase by credit. Delegates the classification to
    erpnext_accounts._gl_side so this can never drift from the group the leaf was
    actually created under."""
    return erpnext_accounts._gl_side(account) not in ('credit', 'loan')


def opening_balance_direction(account: PlaidAccount, amount: float) -> bool:
    """Whether to DEBIT the account's own GL leaf (True) or credit it (False),
    for a Plaid `current` balance of `amount`.

    The account's natural opening side, flipped when the balance is negative.
    See the module docstring for the four cases this collapses; the point is that
    an overdrawn checking account and an overpaid credit card are the SAME
    situation viewed from either side, and both are handled by one flip rather
    than by special-casing each account type."""
    natural = opens_by_debit(account)
    return natural if (amount or 0.0) >= 0 else not natural


def estimate_opening_balance(account: PlaidAccount,
                             transactions: list | None = None) -> float:
    """What this account held BEFORE the first transaction Bank Bridge mirrored:
    its current Plaid balance minus everything we have seen move since.

    Used only by the backfill — an account linked from v0.4.4 on books its
    opening balance at import time, when the current balance IS the opening
    balance and no arithmetic is needed.

    Plaid's transaction `amount` is positive for money OUT of the account (see
    BankTransaction.amount), and the balance it moves is type-dependent in
    exactly the way the module docstring describes:

      * depository / investment — the balance is what you HAVE, so an outflow
        DECREASES it. balance += -amount, hence opening = current + Σamount.
      * credit — the balance is what you OWE, so a purchase (an outflow)
        INCREASES it. owed += +amount, hence opening = current - Σamount.

    Pending rows are excluded: they are provisional, may be restated or dropped
    entirely, and double-counting one would skew the estimate by its full value.
    Removed rows are excluded for the same reason — Plaid took them back.

    This is an ESTIMATE and the backfill says so: it is only exact if Bank Bridge
    has mirrored every transaction since the account's balance was last equal to
    its opening balance. That is why backfilled entries land in `pending_review`
    for the operator to check against a real statement."""
    current = float(account.balance_current or 0.0)
    if transactions is None:
        transactions = (BankTransaction.query
                        .filter_by(account_id=account.account_id).all())
    total = sum(float(t.amount or 0.0) for t in transactions
                if not t.pending and not t.removed)
    if opens_by_debit(account):
        return round(current + total, 2)
    return round(current - total, 2)


# ── Opening Balance Equity account ──────────────────────────────────────────

def _equity_root(client, company: str) -> str | None:
    """The company's root Equity group (top of the Equity branch). Prefers a
    root_type=Equity group with no parent_account; falls back to the first
    Equity-rooted group. None when the company has no Equity branch — the caller
    then reports that rather than inventing a root, because creating a root
    account is a chart-of-accounts decision Bank Bridge has no business making.

    The Assets/Liabilities twin of erpnext_accounts._asset_root/_liability_root."""
    groups = erpnext_accounts._find_accounts(client, company, is_group=1,
                                             root_type='Equity')
    if not groups:
        return None
    for g in groups:
        if not (g.get('parent_account') or ''):
            return g['name']
    return groups[0]['name']


def ensure_opening_balance_equity_account(client, company: str) -> str | None:
    """Find (or create) the Opening Balance Equity leaf for `company`; return its
    docname, or None when the company has no Equity branch to anchor to.

    Idempotent — an existing leaf with the same account_name under this company
    is reused, so every account's opening balance credits the SAME equity
    account and the chart never accumulates duplicates. Numbering follows the
    chart's own convention (skipped entirely on an unnumbered chart), preferring
    the conventional 3000 Equity slot."""
    name = equity_account_name()
    existing = client.list_docs(
        ACCOUNT_DT,
        filters=[['account_name', '=', name], ['company', '=', company],
                 ['is_group', '=', 0]],
        fields=['name'], limit_page_length=1)
    if existing:
        return existing[0]['name']
    root = _equity_root(client, company)
    if not root:
        log.info('no Equity branch for company %r; cannot provision %r',
                 company, name)
        return None
    doc = {'account_name': name, 'parent_account': root, 'company': company,
           'is_group': 0, 'account_type': 'Equity'}
    number = erpnext_accounts._reserved_group_number(client, company,
                                                     _EQUITY_ACCOUNT_NUMBER)
    if number:
        doc['account_number'] = number
    created = client.create_doc(ACCOUNT_DT, doc)
    docname = created.get('name') or name
    log.info("created Opening Balance Equity account '%s' under '%s'%s",
             docname, root, f' as #{number}' if number else '')
    audit.record('opening_balance_equity_account_created', subject_type='Account',
                 subject_id=docname,
                 notes=f'created {name} under {root}',
                 after={'account': docname, 'company': company,
                        'parent_account': root, 'account_number': number})
    return docname


# ── the Journal Entry ───────────────────────────────────────────────────────

def synthetic_transaction_id(account: PlaidAccount) -> str:
    """The GeneratedJournalEntry key this account's opening balance claims."""
    return f'{SYNTHETIC_PREFIX}{account.account_id}'


def is_opening_balance_entry(entry: GeneratedJournalEntry) -> bool:
    """Whether a GeneratedJournalEntry row is an opening balance rather than a
    rules-engine or intercompany entry. Keyed off the synthetic id prefix, which
    is structural — a row cannot claim that key without being one."""
    return (entry.plaid_transaction_id or '').startswith(SYNTHETIC_PREFIX)


def build_opening_balance_document(account: PlaidAccount, gl_account: str,
                                   equity_account: str, company: str,
                                   amount: float, posting_date,
                                   statement=None) -> dict:
    """The ERPNext Journal Entry payload for one account's opening balance, as a
    plain two-line Dr/Cr that balances by construction.

    Pure — no ERPNext calls, no writes — so the direction rule this module exists
    to get right is assertable in a test without a live instance. `amount` is the
    raw (signed) Plaid balance; the returned lines always carry its magnitude,
    with the SIGN expressed as which account is debited.

    No `party_type` / `party` on either line, and no `reference_type` — an
    opening balance answers to no transaction and no counterparty.

    `statement` (v0.4.9), when given, is the PlaidStatement the amount was taken
    from; it only changes the human remark, so a reviewer approving the Draft can
    tell a bank-issued figure from a derived one without leaving ERPNext."""
    magnitude = round(abs(float(amount or 0.0)), 2)
    debit_first = opening_balance_direction(account, amount)
    debit_account = gl_account if debit_first else equity_account
    credit_account = equity_account if debit_first else gl_account
    label = (account.name or account.official_name or account.mask
             or account.account_id)
    if statement is not None:
        remark = (f'Opening Balance for {label} from bank statement '
                  f'{statement.period_label()} (Plaid statement '
                  f'{statement.statement_id})')
    else:
        remark = f'Opening Balance for {label} at initial link'
    doc = {
        'doctype': JOURNAL_ENTRY_DT,
        'voucher_type': 'Journal Entry',
        'company': company,
        'user_remark': remark,
        'accounts': [
            {'account': debit_account, 'debit_in_account_currency': magnitude},
            {'account': credit_account, 'credit_in_account_currency': magnitude},
        ],
    }
    if posting_date:
        doc['posting_date'] = (posting_date.isoformat()
                               if hasattr(posting_date, 'isoformat')
                               else str(posting_date))
    return doc


def existing_entry(account: PlaidAccount):
    """The GeneratedJournalEntry row holding this account's opening balance, or
    None. Looked up by the synthetic key rather than by the denormalized
    `opening_balance_je_id` so it stays correct even if that pointer was never
    written (a pre-v0.4.4 row, or a crash between the two writes)."""
    return GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=synthetic_transaction_id(account)).first()


def opening_balance_status(account: PlaidAccount) -> str:
    """This account's opening balance state for the Accounts page: 'booked' (the
    JE is submitted in ERPNext), 'pending' (a Draft awaiting review), 'none' (no
    entry), or the row's own state for the rejected / error cases."""
    entry = existing_entry(account)
    if entry is None:
        return 'none'
    return {'approved': 'booked', 'pending_review': 'pending'}.get(
        entry.state, entry.state or 'none')


def _record_entry(account: PlaidAccount, je_name: str, doc: dict,
                  amount: float, state: str = 'pending_review',
                  error_message: str | None = None) -> GeneratedJournalEntry:
    """Upsert the GeneratedJournalEntry audit row for this account's opening
    balance, and point the PlaidAccount at it. Upsert rather than insert because
    the synthetic key is UNIQUE — a re-book after a rejection must re-point the
    existing row, exactly as the intercompany path re-points a superseded one."""
    entry = existing_entry(account)
    if entry is None:
        entry = GeneratedJournalEntry(
            plaid_transaction_id=synthetic_transaction_id(account))
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
    db.session.flush()      # so entry.id exists for the pointer below
    account.opening_balance_je_id = entry.id
    return entry


def _result(status: str, message: str, entry=None) -> dict:
    return {'status': status, 'message': message,
            'journal_entry': (entry.erpnext_journal_entry_name if entry else None),
            'entry_id': (entry.id if entry else None)}


def statement_anchor(account: PlaidAccount):
    """(amount, posting_date, statement) from a bank-issued statement it is safe
    to open this account at, or None (v0.4.9).

    Thin, deliberately: every judgement about WHICH statement qualifies lives in
    statements.choose_anchor_statement, next to the reconciliation arithmetic it
    depends on. This wrapper exists so the booking path can consult statements
    without importing them at module scope — app/statements.py imports THIS
    module for opens_by_debit, and a top-level import either way would be
    circular."""
    try:
        from . import statements
        return statements.anchor_for(account)
    except Exception:  # pragma: no cover - a statement must never block booking
        log.warning('statement anchor lookup failed for %s',
                    account.account_id, exc_info=True)
        return None


def book_opening_balance(client, account: PlaidAccount, *,
                         amount: float | None = None,
                         posting_date=None, force: bool = False,
                         prefer_statement: bool = False) -> dict:
    """Book one account's opening balance as a Draft Journal Entry in ERPNext.

    Returns {'status': booked|skipped|error, 'message', 'journal_entry',
    'entry_id'}. NEVER raises — this runs inside the account-import path, where a
    chart-of-accounts problem must degrade to "no opening balance yet" rather
    than sink the import that just linked the operator's bank.

    `amount` overrides the account's cached Plaid balance (the manual endpoint
    and the backfill's estimate both pass one); `posting_date` overrides the
    configured date. `force` re-books over a previously rejected entry, which is
    the only way an already-decided opening balance is ever revisited.

    `prefer_statement` (v0.4.9) consults Plaid Statements for a BANK-ISSUED
    opening balance first, and books that — dated the statement's period start —
    when one qualifies. It defaults OFF, and the asymmetry is the point:

      * the IMPORT path leaves it off, because at link time `balances.current`
        is already the bank's own number and posting it as of today is exact.
        There is no estimate there to improve on, so switching an import to a
        statement anchor would move an operator's balance sheet on upgrade for
        no accuracy gained.
      * the BACKFILL path turns it on, because that is where the estimate
        actually lives — current-balance-minus-mirrored-movement, exact only if
        the mirror is complete, which is why those entries land in
        `pending_review` for a human to check against a statement. A statement
        that reconciles replaces both the arithmetic and the manual check.

    An explicit `amount` always wins: a caller naming a figure has more context
    than any heuristic here."""
    if client is None:
        return _result('skipped', 'ERPNext is not configured')

    entry = existing_entry(account)
    if entry is not None and not (force and entry.state == 'rejected'):
        if entry.state == 'rejected':
            return _result('skipped', 'Opening balance was rejected — book a '
                                      'new one explicitly to override', entry)
        return _result('skipped', 'Opening balance already booked', entry)

    gl_account = (account.erpnext_gl_account_name or '').strip()
    if not gl_account:
        return _result('skipped',
                       'No GL Account linked — an opening balance needs a real '
                       'Chart-of-Accounts leaf to book against')

    # v0.4.9 · a bank-issued statement beats anything computed here, but only
    # when the caller hasn't named its own amount and only when a statement
    # actually qualifies (see statements.choose_anchor_statement — it rejects
    # any statement the mirror cannot reproduce). `source` rides through to the
    # entry's remark so an operator reviewing the Draft can see which it was.
    source = 'estimate' if amount is not None else 'plaid_balance'
    anchor = None
    if prefer_statement and amount is None:
        anchor = statement_anchor(account)
    if anchor is not None:
        amount, anchored_date, statement = anchor[0], anchor[1], anchor[2]
        source = 'statement'
        if posting_date is None:
            posting_date = anchored_date
        log.info('opening balance for %s anchored on bank statement %s '
                 '(period starting %s): %.2f', account.account_id,
                 statement.statement_id, anchored_date, amount)

    value = float(account.balance_current or 0.0) if amount is None \
        else float(amount)
    if round(abs(value), 2) < 0.005:
        return _result('skipped', 'Opening balance is zero — nothing to book')

    company = erpnext_accounts.owning_company_for(account)
    if not company:
        return _result('skipped', 'No owning Company resolved for this account')

    when = posting_date or opening_balance_date()
    try:
        equity = ensure_opening_balance_equity_account(client, company)
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        log.warning('could not provision the opening balance equity account for '
                    '%s: %s', company, e)
        return _result('error', f'Could not provision “{equity_account_name()}” '
                                f'under {company}: {e}')
    if not equity:
        return _result(
            'skipped',
            f'Company “{company}” has no Equity branch to create '
            f'“{equity_account_name()}” under — check its Chart of Accounts')

    doc = build_opening_balance_document(account, gl_account, equity, company,
                                         value, when,
                                         statement=(anchor[2] if anchor else None))
    try:
        created = client.create_doc(JOURNAL_ENTRY_DT, doc)
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        account = db.session.get(PlaidAccount, account.id)
        if account is None:
            return _result('error', f'ERPNext refused the opening balance: {e}')
        entry = _record_entry(account, '', doc, value, state='error',
                              error_message=str(e)[:2000])
        db.session.commit()
        log.warning('opening balance JE failed for %s: %s',
                    account.account_id, e)
        return _result('error', f'ERPNext refused the opening balance: {e}',
                       entry)

    je_name = created.get('name') or ''
    entry = _record_entry(account, je_name, doc, value)
    audit.record('opening_balance_booked', subject_type='GeneratedJournalEntry',
                 subject_id=entry.id,
                 after={'account_id': account.account_id, 'company': company,
                        'journal_entry': je_name, 'amount': entry.amount,
                        'debit_account': doc['accounts'][0]['account'],
                        'credit_account': doc['accounts'][1]['account'],
                        'posting_date': doc.get('posting_date'),
                        'source': source,
                        'statement_id': (anchor[2].statement_id
                                         if anchor else None)},
                 notes=f'opening balance {entry.amount:.2f} for '
                       f'{account.name or account.account_id}', commit=False)
    db.session.commit()
    log.info('opening balance %s booked for %s as %s (source: %s)',
             entry.amount, account.account_id, je_name, source)
    detail = (f' from bank statement {anchor[2].period_label()}'
              if anchor else '')
    return _result('booked',
                   f'Opening balance {entry.amount:.2f}{detail} booked as '
                   f'{je_name} — pending review', entry)


def book_if_enabled(client, account: PlaidAccount) -> dict | None:
    """The import-path hook: book this account's opening balance when
    AUTO_BOOK_OPENING_BALANCE is on. Returns the result dict, or None when the
    feature is off. Best-effort by construction — book_opening_balance never
    raises, so a failure here leaves a linked, working account with no opening
    balance rather than an aborted import."""
    if not auto_book_enabled():
        return None
    return book_opening_balance(client, account)
