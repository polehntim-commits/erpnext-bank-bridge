# SPDX-License-Identifier: MIT
"""Mark-to-market for investment accounts (v0.4.12).

THE GAP. v0.4.0 brought investment accounts in as BALANCE-ONLY: Bank Bridge
creates the Bank Account and the GL leaf, books an opening balance, and then
mirrors each refreshed balance onto an informational `plaid_balance` custom
field on the Bank Account. That field is not a posting. So the GL leaf keeps the
value it opened at, forever — a brokerage that went from $50,000 to $65,000
reads $50,000 on the balance sheet, with the real number visible only to someone
who opens the Bank Account form and knows to look.

THE SHAPE. Post the DIFFERENCE as a Journal Entry, against an equity account:

    value went UP      Dr  <investment leaf>      Cr  Unrealized Gain/Loss
    value went DOWN    Dr  Unrealized Gain/Loss   Cr  <investment leaf>

WHY EQUITY AND NOT INCOME. An unrealized gain is a paper movement — the market
moved, the farm did nothing, and no cash exists. Routing it to an income account
would put market noise straight into the operating result, so a quarter where
the orchard did poorly but the brokerage rallied would report a profit that
nobody can spend. Booking it to equity keeps the balance sheet honest (the asset
is worth what it is worth) while leaving the income statement to say what the
FARM did. This mirrors how OCI treats available-for-sale securities, and it is
the reversible choice: an operator whose accountant wants it in income can point
UNREALIZED_GAIN_ACCOUNT_NAME at an income account and every future entry follows.

THE DELTA IS AGAINST THE LEDGER, NOT AGAINST YESTERDAY. `last_revalued_balance`
records what the GL leaf currently reflects, and each entry posts only the gap
between that and the live balance. Entries therefore COMPOSE: three revaluations
of +5k, -2k and +1k leave the leaf at opening +4k, which is what a running
ledger has to mean. Keying off "the last balance Plaid reported" instead would
double-count every time a pass was retried.

THE SEEDING RULE, which is the one that stops this being dangerous. A NULL
baseline means "we do not know what the ledger reflects". Posting a revaluation
in that state would book the account's entire value as a fictional one-off gain
— catastrophic on an upgrade, where every existing investment account has a NULL
baseline. So the first pass over an account SEEDS the baseline and posts
nothing. The baseline comes from the booked opening balance when there is one
(the value the leaf actually holds); otherwise from the current balance, which
means revaluation tracks change from the moment the feature was switched on.
Either way it never invents a gain that did not happen.

NOTHING POSTS UNREVIEWED. Entries land as `pending_review` GeneratedJournalEntry
rows, exactly like opening balances, so a human approves every one.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from flask import current_app

from . import db
from . import erpnext_accounts
from .erpnext_client import ERPNextAPIError, ERPNextError
from .models import GeneratedJournalEntry, PlaidAccount

log = logging.getLogger('bankbridge.revaluation')

JOURNAL_ENTRY_DT = 'Journal Entry'
ACCOUNT_DT = 'Account'

# The GeneratedJournalEntry key a revaluation claims. UNLIKE an opening balance
# — one per account, forever — revaluations recur, so the key carries the
# posting date as a discriminator. That makes a pass idempotent PER DAY: running
# the sync twice on Tuesday revalues once, and Wednesday gets its own entry.
SYNTHETIC_PREFIX = 'investment-revaluation:'

# The human label on the GeneratedJournalEntry row, mirroring
# opening_balance.RULE_LABEL and intercompany's.
RULE_LABEL = 'Investment revaluation'

# Reserved account_number for the auto-created equity leaf, in the conventional
# 3000s Equity band — the slot after Opening Balance Equity's 3000. Only applied
# when the chart numbers its accounts at all, and bumped past any collision.
_EQUITY_ACCOUNT_NUMBER = 3100


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── configuration ───────────────────────────────────────────────────────────

def is_enabled() -> bool:
    """Whether investment balances are marked to market
    (INVESTMENT_REVALUATION_ENABLED, default on). Off → the GL leaf keeps its
    opening value and v0.4.0's informational `plaid_balance` field is the only
    place the live number appears, exactly as before v0.4.12."""
    return bool(current_app.config.get('INVESTMENT_REVALUATION_ENABLED', True))


def unrealized_account_name() -> str:
    """The equity leaf revaluations post against
    (UNREALIZED_GAIN_ACCOUNT_NAME, default 'Unrealized Gain/Loss on
    Investments').

    Pointing this at an INCOME account is a supported choice, not a
    misconfiguration — see the module docstring on why equity is the default.
    Nothing here inspects the account's root type; ERPNext validates the link."""
    return ((current_app.config.get('UNREALIZED_GAIN_ACCOUNT_NAME')
             or 'Unrealized Gain/Loss on Investments').strip()
            or 'Unrealized Gain/Loss on Investments')


def min_delta() -> float:
    """The smallest movement worth a Journal Entry
    (INVESTMENT_REVALUATION_MIN_DELTA, default 1.00).

    A brokerage moves every day. Without a floor, a daily sync would file a JE
    for eleven cents and bury the entries that matter under entries nobody would
    ever want to read — and each one costs a human an approval click."""
    try:
        return abs(float(current_app.config.get(
            'INVESTMENT_REVALUATION_MIN_DELTA', 1.00)))
    except (TypeError, ValueError):
        return 1.00


# ── keys and lookups ────────────────────────────────────────────────────────

def synthetic_transaction_id(account: PlaidAccount, when: date) -> str:
    """The GeneratedJournalEntry key a revaluation of `account` on `when`
    claims. Date-scoped, so re-running a pass on the same day is a no-op rather
    than a second entry."""
    return f'{SYNTHETIC_PREFIX}{account.account_id}:{when.isoformat()}'


def is_revaluation_entry(entry: GeneratedJournalEntry) -> bool:
    """Whether a row is a revaluation rather than an opening balance, an
    intercompany leg or a rules-engine entry. Keyed off the prefix, which is
    structural — a row cannot claim that key without being one."""
    return (entry.plaid_transaction_id or '').startswith(SYNTHETIC_PREFIX)


def existing_entry(account: PlaidAccount, when: date):
    """This account's revaluation entry for `when`, or None."""
    return GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=synthetic_transaction_id(account, when)).first()


def entries_for(account: PlaidAccount) -> list:
    """Every revaluation entry for one account, newest first — what the Accounts
    page shows when an operator asks where a number came from."""
    return (GeneratedJournalEntry.query
            .filter(GeneratedJournalEntry.plaid_transaction_id.like(
                f'{SYNTHETIC_PREFIX}{account.account_id}:%'))
            .order_by(GeneratedJournalEntry.id.desc())
            .all())


# ── the equity leaf ─────────────────────────────────────────────────────────

def ensure_unrealized_account(client, company: str) -> str | None:
    """Find (or create) the unrealized gain/loss leaf for `company`; return its
    docname, or None when the company has no Equity branch to anchor to.

    Idempotent — an existing leaf with the same account_name under this company
    is reused, so every account's revaluations hit the SAME leaf and the chart
    never accumulates duplicates. Deliberately the same shape as
    opening_balance.ensure_opening_balance_equity_account, including declining to
    invent a root: creating a root account is a chart-of-accounts decision Bank
    Bridge has no business making."""
    from . import opening_balance as obal
    name = unrealized_account_name()
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
    root = obal._equity_root(client, company)
    if not root:
        log.info('no Equity branch for company %r; cannot provision %r — '
                 'investment revaluation is unavailable for this company',
                 company, name)
        return None
    doc = {'account_name': name, 'parent_account': root, 'company': company,
           'is_group': 0, 'account_type': 'Equity'}
    number = erpnext_accounts._reserved_group_number(client, company,
                                                     _EQUITY_ACCOUNT_NUMBER)
    if number:
        doc['account_number'] = number
    try:
        created = client.create_doc(ACCOUNT_DT, doc)
    except (ERPNextAPIError, ERPNextError) as e:
        log.warning('could not create %r under %r: %s', name, root, str(e)[:200])
        return None
    docname = (created or {}).get('name')
    log.info('created equity account %s for investment revaluation', docname)
    return docname


# ── the baseline ────────────────────────────────────────────────────────────

def booked_opening_amount(account: PlaidAccount) -> float | None:
    """The opening-balance value this account's GL leaf actually holds, or None.

    Only an APPROVED entry counts. A Draft awaiting review has not moved the
    ledger yet, and a rejected one never will — treating either as booked would
    make the first revaluation post a delta against a value the leaf does not
    hold."""
    from . import opening_balance as obal
    entry = obal.existing_entry(account)
    if entry is None or entry.state != 'approved':
        return None
    return round(float(entry.amount or 0.0), 2)


def baseline_for(account: PlaidAccount) -> tuple[float | None, str]:
    """(baseline, source) — what the ledger currently reflects for this account.

    `baseline` is None when it cannot be established, which the caller must
    treat as "seed, do not post". Sources:

      * 'tracked'  — a previous revaluation recorded it. The normal path.
      * 'opening'  — no revaluation yet, but the opening balance is booked, so
                     the leaf holds that value.
      * 'seed'     — neither. Revaluation starts tracking from the current
                     balance and posts nothing, so an account whose opening
                     balance was never approved cannot have its whole value
                     booked as a fictitious gain."""
    if account.last_revalued_balance is not None:
        return round(float(account.last_revalued_balance), 2), 'tracked'
    opening = booked_opening_amount(account)
    if opening is not None:
        return opening, 'opening'
    return None, 'seed'


def _seed(account: PlaidAccount, value: float, reason: str) -> dict:
    """Record the baseline without posting anything."""
    account.last_revalued_balance = round(float(value), 2)
    account.last_revalued_at = _now()
    account.updated_at = _now()
    db.session.commit()
    log.info('investment revaluation baseline seeded for %s at %.2f (%s) — '
             'no entry posted', account.account_id, value, reason)
    return {'status': 'seeded', 'delta': 0.0, 'baseline': account.last_revalued_balance,
            'message': reason, 'entry_id': None}


# ── the Journal Entry ───────────────────────────────────────────────────────

def build_revaluation_document(account: PlaidAccount, gl_account: str,
                               equity_account: str, company: str,
                               delta: float, posting_date) -> dict:
    """The ERPNext Journal Entry payload for one revaluation, as a plain two-line
    Dr/Cr that balances by construction.

    Pure — no ERPNext calls, no writes — so the direction rule is assertable
    without a live instance. `delta` is SIGNED (positive = the account is worth
    more than the ledger says); the lines carry its magnitude, with the sign
    expressed as which account is debited.

    An investment account is an ASSET regardless of subtype, so a gain always
    debits the leaf. There is no credit-side twin here the way there is for
    opening balances — a 401k is never a liability."""
    magnitude = round(abs(float(delta or 0.0)), 2)
    gain = float(delta or 0.0) > 0
    debit_account = gl_account if gain else equity_account
    credit_account = equity_account if gain else gl_account
    label = (account.name or account.official_name or account.mask
             or account.account_id)
    direction = 'gain' if gain else 'loss'
    remark = (f'Unrealized {direction} on {label} — market value revaluation '
              f'({magnitude:,.2f})')
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


def _record_entry(account: PlaidAccount, when: date, je_name: str, doc: dict,
                  delta: float, state: str = 'pending_review',
                  error_message: str | None = None) -> GeneratedJournalEntry:
    """Upsert the GeneratedJournalEntry audit row for this revaluation. Upsert
    rather than insert because the synthetic key is UNIQUE — a retry after a
    failed ERPNext write must re-point the existing row."""
    entry = existing_entry(account, when)
    if entry is None:
        entry = GeneratedJournalEntry(
            plaid_transaction_id=synthetic_transaction_id(account, when))
        db.session.add(entry)
    entry.rule_id = None
    entry.rule_name = RULE_LABEL
    entry.erpnext_journal_entry_name = je_name or None
    entry.state = state
    entry.amount = round(abs(float(delta or 0.0)), 2)
    entry.merchant_name = ''
    entry.description = (doc.get('user_remark') or '')
    entry.error_message = error_message
    entry.updated_at = _now()
    db.session.flush()
    return entry


# ── revaluing one account ───────────────────────────────────────────────────

def _result(status: str, message: str, *, delta: float = 0.0,
            baseline=None, entry=None) -> dict:
    return {'status': status, 'message': message, 'delta': delta,
            'baseline': baseline,
            'journal_entry': (entry.erpnext_journal_entry_name if entry else None),
            'entry_id': (entry.id if entry else None)}


def revalue_account(client, account: PlaidAccount, *,
                    posting_date: date | None = None) -> dict:
    """Mark one investment account to market. Returns
    {'status', 'message', 'delta', 'baseline', 'journal_entry', 'entry_id'}
    where `status` is one of:

      * 'posted'    — a Draft Journal Entry was created for the delta
      * 'seeded'    — baseline recorded, nothing posted (first pass)
      * 'unchanged' — the movement is under the threshold
      * 'skipped'   — not eligible (not an investment, unmapped, no balance…)
      * 'error'     — ERPNext refused

    NEVER raises. This runs inside the sync path, where a chart-of-accounts
    problem must degrade to "not revalued yet" rather than sink the sync."""
    if not is_enabled():
        return _result('skipped', 'investment revaluation is disabled')
    if account is None or not account.balance_only:
        return _result('skipped', 'not a balance-only investment account')
    gl_account = (account.erpnext_gl_account_name or '').strip()
    if not gl_account:
        # Nothing to revalue against. An investment whose import fell back to a
        # personal Bank Account has no GL leaf of its own.
        return _result('skipped', 'no ERPNext GL account for this investment')
    if account.balance_current is None:
        return _result('skipped', 'no cached balance to revalue against')
    if client is None:
        return _result('skipped', 'ERPNext is not configured')

    current = round(float(account.balance_current), 2)
    baseline, source = baseline_for(account)
    if baseline is None:
        return _seed(account, current,
                     'no booked opening balance to measure against — '
                     'revaluation tracks changes from now on')

    delta = round(current - baseline, 2)
    if abs(delta) < min_delta():
        return _result('unchanged',
                       f'movement of {delta:+.2f} is under the '
                       f'{min_delta():.2f} threshold',
                       delta=delta, baseline=baseline)

    when = posting_date or date.today()
    already = existing_entry(account, when)
    if already is not None and already.state != 'rejected':
        return _result('unchanged',
                       f'already revalued on {when.isoformat()}',
                       delta=delta, baseline=baseline, entry=already)

    company = erpnext_accounts.owning_company_for(account)
    if not company:
        return _result('skipped', 'no ERPNext Company for this account')
    equity_account = ensure_unrealized_account(client, company)
    if not equity_account:
        return _result('skipped',
                       f'no equity account available under {company}')

    doc = build_revaluation_document(account, gl_account, equity_account,
                                     company, delta, when)
    try:
        created = client.create_doc(JOURNAL_ENTRY_DT, doc)
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        detail = str(e)[:500]
        entry = _record_entry(account, when, '', doc, delta, state='error',
                              error_message=detail)
        db.session.commit()
        log.warning('revaluation failed for %s: %s', account.account_id, detail)
        return _result('error', detail, delta=delta, baseline=baseline,
                       entry=entry)

    je_name = (created or {}).get('name') or ''
    entry = _record_entry(account, when, je_name, doc, delta)
    # Advance the baseline ONLY once the entry exists. The ledger now reflects
    # `current`, so the next pass measures from there — which is what makes a
    # sequence of revaluations compose instead of re-posting the same gain.
    account.last_revalued_balance = current
    account.last_revalued_at = _now()
    account.updated_at = _now()
    db.session.commit()
    log.info('revalued %s by %+.2f (%.2f → %.2f) as %s', account.account_id,
             delta, baseline, current, je_name)
    try:
        from . import audit
        audit.record('investment_revalued', subject_type='PlaidAccount',
                     subject_id=account.account_id,
                     after={'delta': delta, 'from': baseline, 'to': current,
                            'journal_entry': je_name},
                     notes=f'unrealized {"gain" if delta > 0 else "loss"} '
                           f'of {abs(delta):,.2f}')
    except Exception:  # pragma: no cover - auditing must not fail the sync
        log.debug('revaluation audit failed', exc_info=True)
    return _result('posted', f'revalued by {delta:+.2f}', delta=delta,
                   baseline=baseline, entry=entry)


# ── revaluing everything ────────────────────────────────────────────────────

def eligible_accounts() -> list:
    """Balance-only investment accounts with a GL leaf to revalue. A superseded
    account (v0.4.11) is excluded — its mapping now belongs to its heir, and
    revaluing both would post the same movement twice."""
    return (PlaidAccount.query
            .filter(PlaidAccount.balance_only.is_(True),
                    PlaidAccount.erpnext_gl_account_name.isnot(None),
                    PlaidAccount.superseded_by_account_id.is_(None))
            .order_by(PlaidAccount.id)
            .all())


def revalue_all(client, *, posting_date: date | None = None) -> dict:
    """Mark every eligible investment account to market. Returns
    {'scanned', 'posted', 'seeded', 'unchanged', 'skipped', 'failed', 'total_delta',
    'errors'}. Never raises — one account's failure doesn't stop the rest."""
    stats = {'scanned': 0, 'posted': 0, 'seeded': 0, 'unchanged': 0,
             'skipped': 0, 'failed': 0, 'total_delta': 0.0, 'errors': []}
    if not is_enabled() or client is None:
        return stats
    for account in eligible_accounts():
        stats['scanned'] += 1
        try:
            result = revalue_account(client, account, posting_date=posting_date)
        except Exception as e:  # pragma: no cover - revalue_account is total
            db.session.rollback()
            log.warning('revaluation crashed for %s: %s', account.account_id, e,
                        exc_info=True)
            stats['failed'] += 1
            stats['errors'].append(f'{account.account_id}: {e}')
            continue
        status = result['status']
        if status == 'posted':
            stats['posted'] += 1
            stats['total_delta'] = round(stats['total_delta'] + result['delta'], 2)
        elif status == 'error':
            stats['failed'] += 1
            stats['errors'].append(f"{account.account_id}: {result['message']}")
        elif status in ('seeded', 'unchanged', 'skipped'):
            stats[status] += 1
    if stats['posted']:
        log.info('[revaluation] %s', {k: v for k, v in stats.items()
                                      if k != 'errors'})
    return stats
