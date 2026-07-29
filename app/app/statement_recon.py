# SPDX-License-Identifier: MIT
"""Does what the STATEMENT says happened match what Bank Bridge BOOKED?
(v0.9.0)

THE CLASS OF BUG THIS EXISTS FOR. v0.8.4 shipped a settlement leg that never
posted, and the ledger ran to -$1,011,119.41 on Cash Clearing before a human
noticed. v0.8.5 added two guards and neither of them would have caught it any
earlier:

  * `StatementAnchor.variance` reconciles CASH. It asks whether the transactions
    Plaid mirrored explain the balance the bank asserted. A leg that Bank Bridge
    failed to POST is not a transaction Plaid failed to MIRROR — the cash
    identity balances perfectly while the books are missing half the entry.
  * The Bank-Transaction-reference dedup catches EXACT re-emission: the same
    entry written twice, recognised by its reference. A period that booked 24 of
    26 dividends re-emits nothing at all.

So this compares PER CATEGORY. Dividends against dividends, buys against buys,
fees against fees. A balance can be right while its composition is wrong, and a
category is the smallest unit where that is legible.

    statement says $12,480.19 of dividends · books hold $11,502.31 · Δ 7.8%

**KAIROS OVER CHRONOS.** Nothing here is scheduled, and the reason is stronger
than style. A reconciled period's delta is SETTLED — the statement is a document
that will not change, so the answer computed today is the answer forever. There
is nothing for a nightly job to discover. `report()` is a pure read, answered
whenever someone asks. The ACTION lives in `observe()`, and it fires on the
appearance of a NEW drifted observation or on a settled one CHANGING verdict —
`StatementReconSample.fired_at` is what makes a second read silent. The clock
(the statement PDF poll, the Plaid sync) gathers the observations; the state
decides whether anything happens. Same shape as `draft_health.observe`.

**THE GATE IS STATE, NOT TIME.** A period produces a recon row only when all
three are true: the statement has ARRIVED (a PlaidStatement with a parsed
period), it is RECONCILED (a StatementAnchor whose cash variance is inside
tolerance), and BOOKED ACTIVITY IS AVAILABLE (the period is in the past and its
transactions have been through the JE pipeline). A period failing any of them is
reported as `skipped` WITH THE REASON rather than omitted — an operator who
cannot see why a month is absent will assume the month is fine.

**DATA DRIVEN, NOT HAND-CODED.** The drift line is the P95 of this install's own
prior |delta_pct| for the same (account, category), because "how much does our
dividend booking normally differ from the statement's" is a question only this
install's history can answer. `DEFAULT_THRESHOLD_PCT` (5%) applies until there
are `MIN_SAMPLES_FOR_BASELINE` observations, and during that window every
non-zero delta is flagged — starting strict and loosening as evidence arrives is
the honest direction, since the alternative trains an operator to ignore the
report before it knows anything.

**MAGNITUDES, NOT SIGNS.** Both sides are compared as absolute values, and that
is a decision rather than a shortcut. Statement figures carry THE BANK'S signs
verbatim (see statements.py: 'Cash withdrawn -20,047.16' is stored negative,
'securities purchased' likewise), while a Journal Entry carries unsigned debits
and credits whose direction lives in which line they sit on. Reconciling those
two conventions per category would mean encoding a sign rule per field per
layout, and every one of them would be a hand-coded guess this module exists to
avoid. The question being asked is "is the same amount of activity in both
places", and |x| answers it without inventing a convention.

**CATEGORIES ARE NOT ADDITIVE.** They are drawn from three different statement
blocks that overlap by design: `dividends` and `interest` are both inside the
cash-flow summary's `income_distributions`, and `deposits`/`withdrawals` come
from the progress summary which also contains the electronic transfers. Do NOT
sum a column of this report and expect the period's cash movement — that
identity is `StatementAnchor`'s job and it already holds. Each category is an
independent probe.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone

from . import db
from .erpnext_client import ERPNextAPIError, ERPNextError
from .models import (BankTransaction, GeneratedJournalEntry, PlaidAccount,
                     PlaidStatement, SecurityTransaction, StatementAnchor,
                     StatementReconSample)

log = logging.getLogger('bankbridge.statement_recon')

JOURNAL_ENTRY_DT = 'Journal Entry'

# The drift line before there is any history to learn from. 5% is Tim's starting
# number and is deliberately untuned — tuning a constant is the thing this
# module exists to stop doing. See learned_threshold.
DEFAULT_THRESHOLD_PCT = 0.05

# How many settled observations of the same (account, category) before the P95
# is trusted. Below this the percentile of a handful of periods says more about
# which periods happened to be reconciled first than about the install.
MIN_SAMPLES_FOR_BASELINE = 20

# Only the most recent N clean samples feed the baseline, so it tracks the
# account as it grows rather than averaging in its first year forever.
BASELINE_WINDOW = 200

# Headroom above the observed P95. A category that normally lands within 3%
# should not fire at 3.1%; it should fire when something is happening that has
# not happened before.
BASELINE_SLACK = 1.25

# The floor a learned threshold cannot fall below. An account whose history is a
# flat 0% would otherwise compute a P95 of 0 and treat a one-cent rounding
# difference as a finding, which trains the operator to ignore the report.
MIN_LEARNED_THRESHOLD_PCT = 0.005

# Below this, a delta is rounding and not a finding — half a cent on either
# side of a two-decimal figure. Applied in absolute dollars, because a 100%
# delta_pct on a $0.01 category is not worth anyone's morning.
ABSOLUTE_TOLERANCE = 0.01

# Statuses. `unexplained` is deliberately distinct from `drifted`: a drift is a
# delta bigger than this install's own history, while unexplained is activity on
# one side and NOTHING on the other — a category the statement asserts and the
# books have never heard of, or vice versa. The second is the v0.8.4 shape.
STATUS_MATCHED = 'matched'
STATUS_DRIFTED = 'drifted'
STATUS_UNEXPLAINED = 'unexplained'

# FAIL FORWARD · every non-matched row carries one of these. A categorized
# reason is the difference between a report an operator acts on and a column of
# numbers they learn to scroll past.
DRIFT_REASONS = {
    'nothing_booked': 'the statement reports activity in this category and the '
                      'books hold none at all',
    'nothing_on_statement': 'the books hold activity the statement does not '
                            'report in this category',
    'drafts_would_match': 'the posted total is short, but the staged DRAFT '
                          'entries would close the gap — a submit backlog, not '
                          'a drift',
    'over_threshold': 'the delta exceeds this account and category\'s own '
                      'historical P95',
    'no_baseline_yet': 'any non-zero delta is flagged until there are enough '
                       'observations to learn a threshold from',
    'not_booked_by_design': 'this category has no booked counterpart by design '
                            '— shown for visibility, not as something to chase',
}

# Why a period produced no comparison. Reported, never silently omitted.
SKIP_REASONS = {
    'no_anchor': 'the statement has not been anchored yet — run Rebuild '
                 'statement anchors',
    'not_reconciled': 'the period\'s cash variance is outside tolerance, so a '
                      'category comparison would be measured against a '
                      'baseline that is already known to be wrong',
    'chain_gap': 'a statement is missing before this one, so this period\'s '
                 'opening balance does not meet the prior closing',
    'no_statement_figures': 'the parser recovered no category figures from this '
                            'statement layout',
}


# ── the category table ──────────────────────────────────────────────────────
#
# One entry per probe. `statement_keys` are read from
# PlaidStatement.parsed_metadata (the figures statements.py recovered from the
# PDF); `anchor_attr` reads a StatementAnchor column instead, for the one
# category no statement line states directly.
#
# `security_match` decides which of the account's Plaid INVESTMENT transactions
# belong to the category, from Plaid's own (type, subtype) — the vocabulary
# SecurityTransaction preserves verbatim for exactly this kind of question. A
# category with no `security_match` has no investment-side booking to compare.
#
# WHY (type, subtype) AND NOT THE OFFSET ACCOUNT. The offset account is a
# consequence of the chart, which an operator can restructure at any time; the
# Plaid classification is a property of the event itself. Keying on the event
# means this report keeps working after a chart edit, which is precisely when a
# drift detector is most needed.

class Category:
    """One category's definition — the statement side and the booked side."""

    def __init__(self, key, label, *, statement_keys=(), anchor_attr='',
                 security_types=(), subtype_contains=(),
                 subtype_excludes=(), note='', informational=False):
        self.key = key
        self.label = label
        self.statement_keys = tuple(statement_keys)
        self.anchor_attr = anchor_attr
        self.security_types = tuple(security_types)
        self.subtype_contains = tuple(subtype_contains)
        self.subtype_excludes = tuple(subtype_excludes)
        self.note = note
        # INFORMATIONAL · this category has no booked counterpart BY DESIGN, so
        # an empty books side is the expected state rather than a finding. See
        # classify()'s `informational` branch for why that distinction has to
        # exist: without it, mark-to-market would report `unexplained` on every
        # brokerage period forever, and a report that always shows the same
        # finding is a report an operator stops reading.
        self.informational = informational

    def matches_security(self, txn) -> bool:
        """Whether one Plaid investment transaction belongs to this category."""
        if not self.security_types and not self.subtype_contains:
            return False
        ttype = (getattr(txn, 'type', '') or '').strip().lower()
        subtype = (getattr(txn, 'subtype', '') or '').strip().lower()
        if self.security_types and ttype not in self.security_types:
            return False
        if any(bad in subtype for bad in self.subtype_excludes):
            return False
        if self.subtype_contains:
            return any(good in subtype for good in self.subtype_contains)
        return True


CATEGORIES = (
    Category(
        'dividends', 'Dividends',
        # ORDINARY dividends only. 'qualified_dividends' is a TAX
        # CHARACTERISATION of a subset of the same money, not additional money —
        # adding the two would double-count every qualified dividend.
        statement_keys=('ordinary_dividends',),
        security_types=('cash',), subtype_contains=('dividend',)),
    Category(
        'interest', 'Interest',
        # Two disjoint income-summary lines: coupon/credit interest and the
        # sweep fund's yield. They are printed separately and never overlap.
        statement_keys=('interest_income', 'sweep_income'),
        security_types=('cash',), subtype_contains=('interest',)),
    Category(
        'buys', 'Securities purchased',
        statement_keys=('securities_purchased',),
        security_types=('buy',)),
    Category(
        'sells', 'Securities sold',
        statement_keys=('securities_sold',),
        security_types=('sell',)),
    Category(
        'fees', 'Advisory and manager fees',
        statement_keys=('advisory_fees',),
        security_types=('fee',)),
    Category(
        'deposits', 'Cash deposited',
        statement_keys=('deposits_total',),
        security_types=('transfer', 'cash'), subtype_contains=('deposit',)),
    Category(
        'withdrawals', 'Cash withdrawn',
        statement_keys=('withdrawals_total',),
        security_types=('transfer', 'cash'), subtype_contains=('withdrawal',)),
    Category(
        'mark_to_market', 'Mark to market',
        # The one category with no statement LINE — it is the residual
        # StatementAnchor already computes (portfolio delta less cash flow less
        # security flow). Its booked side is whatever revaluation posted, which
        # for an install that has never run a revaluation is legitimately zero;
        # that reads as `unexplained` and is the correct finding, because
        # unrealized movement genuinely is absent from the books.
        anchor_attr='mark_to_market_delta',
        informational=True,
        note='price movement, which no Plaid transaction corresponds to and '
             'which Bank Bridge deliberately does NOT book — the cash '
             'reconciliation is cash-only by design. Shown so the period\'s '
             'total-value change is legible, not as something to chase'),
)

CATEGORIES_BY_KEY = {c.key: c for c in CATEGORIES}


# ── the learned threshold ───────────────────────────────────────────────────

def _percentile(values: list, fraction: float) -> float:
    """Nearest-rank percentile. No numpy, no interpolation — with a few dozen
    observations, interpolating between two of them invents precision the sample
    size does not support. Same helper, same reasoning, as draft_health."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(fraction * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def learned_threshold(account_id: str, category: str) -> tuple:
    """(threshold_pct, source, sample_count) for one (account, category).

    `source` is 'baseline_p95' once there are MIN_SAMPLES_FOR_BASELINE settled,
    non-drifted observations and 'default' before that. `sample_count` rides
    along everywhere the threshold does, so an operator can always see whether
    the number is measured or assumed.

    DRIFTED samples are excluded. A threshold learned from its own excursions
    ratchets upward until it never fires again — the standard way an adaptive
    alarm dies with nobody noticing."""
    rows = (StatementReconSample.query
            .filter(StatementReconSample.account_id == account_id,
                    StatementReconSample.category == category,
                    StatementReconSample.drifted.is_(False),
                    StatementReconSample.delta_pct.isnot(None))
            .order_by(StatementReconSample.period_start.desc(),
                      StatementReconSample.id.desc())
            .limit(BASELINE_WINDOW).all())
    observed = [abs(float(r.delta_pct)) for r in rows
                if r.delta_pct is not None]
    if len(observed) < MIN_SAMPLES_FOR_BASELINE:
        return DEFAULT_THRESHOLD_PCT, 'default', len(observed)
    learned = _percentile(observed, 0.95) * BASELINE_SLACK
    return (max(MIN_LEARNED_THRESHOLD_PCT, learned), 'baseline_p95',
            len(observed))


# ── the statement side ──────────────────────────────────────────────────────

def statement_amount(statement: PlaidStatement, anchor: StatementAnchor,
                     category: Category) -> float | None:
    """What this statement asserts for one category, as a MAGNITUDE, or None
    when the statement states nothing for it.

    None and 0.0 are different findings and must not be conflated: None means
    the parser recovered no such figure from this layout (a depository statement
    has no 'securities purchased' line at all), while 0.0 means the statement
    printed the line and it was zero. The first cannot be compared; the second
    can, and a booked amount against it is a real discrepancy."""
    if category.anchor_attr:
        value = getattr(anchor, category.anchor_attr, None)
        return None if value is None else abs(float(value))
    meta = statement.parsed_metadata or {}
    total = None
    for key in category.statement_keys:
        raw = meta.get(key)
        if raw is None or isinstance(raw, (dict, list, bool)):
            continue
        try:
            total = (total or 0.0) + abs(float(raw))
        except (TypeError, ValueError):
            continue
    return total


# ── the booked side ─────────────────────────────────────────────────────────

def _period_account_ids(account: PlaidAccount) -> list:
    """Every Plaid account id whose activity belongs to this reconciliation.

    An account's own supersede chain (a re-link mints a new Plaid id for the
    same real account) plus a paired brokerage's cash companion — the SAME set
    `statements.anchor_transaction_sum` uses. That alignment is the point: a
    comparison against a statement must draw on exactly the transactions the
    anchor drew on, or the two guards disagree about what a period even is."""
    from . import statements as stmts
    # `supersede_chain` returns account IDs, and it already includes the one it
    # was asked for.
    ids = set(stmts.supersede_chain(account.account_id))
    ids.add(account.account_id)
    partner_id = (account.paired_account_id or '').strip()
    if partner_id:
        ids.update(stmts.supersede_chain(partner_id))
        ids.add(partner_id)
    return sorted(i for i in ids if i)


def booked_journal_entries(account: PlaidAccount, start: date,
                           end: date) -> dict:
    """{category_key: [GeneratedJournalEntry]} for one period.

    Grouped from the LOCAL tables, because attribution is local knowledge —
    only Bank Bridge knows which rule or which Plaid classification produced an
    entry. The AMOUNTS are not taken from here; see `booked_amounts`. That split
    is deliberate: local rows say WHY an entry exists, ERPNext says WHAT it
    holds, and conflating the two is how a diagnostic ends up reporting a
    projection as a ledger fact."""
    account_ids = _period_account_ids(account)
    if not account_ids:
        return {}
    out: dict = {c.key: [] for c in CATEGORIES}

    # Investment side · Plaid's own (type, subtype) decides the category.
    sec_rows = (db.session.query(SecurityTransaction, GeneratedJournalEntry)
                .join(GeneratedJournalEntry,
                      GeneratedJournalEntry.plaid_investment_transaction_id
                      == SecurityTransaction.plaid_investment_transaction_id)
                .filter(SecurityTransaction.account_id.in_(tuple(account_ids)),
                        SecurityTransaction.date >= start,
                        SecurityTransaction.date <= end,
                        GeneratedJournalEntry.erpnext_journal_entry_name
                        .isnot(None))
                .all())
    for txn, gje in sec_rows:
        for category in CATEGORIES:
            if category.matches_security(txn):
                out[category.key].append(gje)
                break          # first match wins; the table is disjoint

    # Bank side · a depository statement's deposits/withdrawals are ordinary
    # Bank Transactions, categorized by the rules engine. Only the two
    # direction-shaped categories can be filled this way — a bank transaction
    # carries no notion of a dividend or a buy.
    #
    # ONE SIDE OR THE OTHER, NEVER BOTH, and the choice is made from the data
    # rather than from the account's `type` field. A period that produced
    # investment transactions is described by them; its cash movements are
    # ALREADY counted above as `transfer/deposit` and `transfer/withdrawal`.
    # Adding the bank side too would double-count, and it would double-count
    # worst on exactly the account this feature exists for: a paired brokerage
    # pulls in its cash-services companion (see _period_account_ids), whose Bank
    # Transactions ARE those same settlement flows under another Plaid id. The
    # result would be a permanent false drift on deposits and withdrawals for
    # every WFA period — a detector that cries wolf on the account it was built
    # to watch.
    if any(entries for entries in out.values()):
        return out
    bank_rows = (db.session.query(BankTransaction, GeneratedJournalEntry)
                 .join(GeneratedJournalEntry,
                       GeneratedJournalEntry.plaid_transaction_id
                       == BankTransaction.plaid_transaction_id)
                 .filter(BankTransaction.account_id.in_(tuple(account_ids)),
                         BankTransaction.date >= start,
                         BankTransaction.date <= end,
                         BankTransaction.removed.is_(False),
                         GeneratedJournalEntry.erpnext_journal_entry_name
                         .isnot(None))
                 .all())
    for txn, gje in bank_rows:
        # Plaid's sign convention on a bank transaction: positive = money out.
        key = 'withdrawals' if float(txn.amount or 0.0) > 0 else 'deposits'
        out[key].append(gje)
    return out


# How many docnames go into one ERPNext list filter. A period can hold hundreds
# of settlement legs, and a single unbounded `['name','in',[...]]` becomes a URL
# no proxy will forward.
_FETCH_CHUNK = 100


def fetch_je_amounts(client, names) -> dict:
    """{docname: (amount, docstatus)} read FROM ERPNEXT, in batches.

    THE AMOUNT COMES FROM THE LEDGER, NOT FROM OUR MIRROR, and that is why this
    talks to ERPNext at all instead of summing `GeneratedJournalEntry.amount`.
    The local column is what Bank Bridge INTENDED to post; a report whose job is
    detecting that the books diverged from reality cannot take our own intention
    as evidence of what the books hold. (Every prior 'balanced' diagnostic that
    projected from local tables and then disagreed with the GL is this lesson.)

    BATCHED, and that matters more than it looks: the first cut fetched one
    document per Journal Entry, which on an install with ~900 investment JEs
    across twenty periods is ~900 round-trips every time the admin page loads —
    and the sync calls this too. One filtered list per hundred docnames turns
    that into single digits.

    A name ERPNext does not return is simply absent from the map, and the caller
    treats absent as contributing nothing. That biases the booked total DOWNWARD,
    which biases toward reporting a drift that may not exist — the safe direction
    for a detector. A whole chunk that fails is logged rather than raised, for the
    same reason."""
    wanted = sorted({(n or '').strip() for n in names if (n or '').strip()})
    out: dict = {}
    for i in range(0, len(wanted), _FETCH_CHUNK):
        chunk = wanted[i:i + _FETCH_CHUNK]
        try:
            rows = client.list_docs(
                JOURNAL_ENTRY_DT, filters=[['name', 'in', chunk]],
                fields=['name', 'total_debit', 'docstatus'],
                limit_page_length=0) or []
        except (ERPNextAPIError, ERPNextError):
            log.warning('statement recon: could not read %d Journal Entry(ies)',
                        len(chunk))
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = (row.get('name') or '').strip()
            if not name:
                continue
            try:
                amount = abs(float(row.get('total_debit') or 0.0))
            except (TypeError, ValueError):
                continue
            try:
                status = int(row.get('docstatus') or 0)
            except (TypeError, ValueError):
                status = 0
            out[name] = (amount, status)
    return out


def booked_amounts(entries: list, amounts: dict) -> tuple:
    """(submitted_total, draft_total) for a list of GeneratedJournalEntry rows,
    given the ledger amounts `fetch_je_amounts` already read.

    Pure — no I/O — so the split rule is testable without an ERPNext. Split by
    docstatus because submitted and draft are different findings: a category that
    only matches once drafts are counted is a submit backlog, and calling that a
    drift sends an operator hunting for a transaction that is sitting in the
    approval queue.

    docstatus 2 (cancelled) counts toward NEITHER — a cancelled entry is not in
    the books and is not waiting to be."""
    submitted = 0.0
    draft = 0.0
    for gje in entries:
        name = (gje.erpnext_journal_entry_name or '').strip()
        found = amounts.get(name)
        if not name or found is None:
            continue
        amount, status = found
        if status == 1:
            submitted += amount
        elif status == 0:
            draft += amount
    return round(submitted, 2), round(draft, 2)


# ── the comparison ──────────────────────────────────────────────────────────

def classify(statement_value: float | None, submitted: float, drafts: float,
             threshold_pct: float, threshold_source: str, *,
             informational: bool = False) -> tuple:
    """(status, reason, delta, delta_pct) for one category comparison.

    Pure and total — no I/O, no ORM — so the decision rule is testable on its
    own, which matters because it is the one place a judgement is made."""
    stated = 0.0 if statement_value is None else float(statement_value)
    delta = round(submitted - stated, 2)
    within_absolute = abs(delta) <= ABSOLUTE_TOLERANCE
    delta_pct = None if not stated else round(delta / stated, 6)

    if within_absolute:
        return STATUS_MATCHED, '', delta, delta_pct

    # An INFORMATIONAL category with nothing booked is in its EXPECTED state,
    # not a finding. Mark-to-market is the case: unrealized price movement is
    # deliberately absent from the books (the cash reconciliation is cash-only
    # by design — folding market movement into it would manufacture variance on
    # a period that reconciles perfectly), so "statement says $28k, books say
    # nothing" is the design working. Reporting it as `unexplained` would put an
    # identical, permanent, unactionable finding on every brokerage period, and
    # a report that always shows the same finding is one an operator stops
    # reading — which would cost them the findings that DO matter.
    #
    # If a revaluation HAS booked something, the normal comparison resumes: at
    # that point the two numbers genuinely should agree.
    if informational and not submitted and not drafts:
        return STATUS_MATCHED, 'not_booked_by_design', delta, delta_pct

    # One side empty and the other not — the v0.8.4 shape, and a different
    # finding from "the numbers differ by more than usual".
    if stated and not submitted:
        if drafts and abs(round(submitted + drafts - stated, 2)) <= ABSOLUTE_TOLERANCE:
            return STATUS_DRIFTED, 'drafts_would_match', delta, delta_pct
        return STATUS_UNEXPLAINED, 'nothing_booked', delta, delta_pct
    if submitted and not stated:
        return STATUS_UNEXPLAINED, 'nothing_on_statement', delta, delta_pct

    # Both sides have something. Would the drafts close the gap?
    if drafts and abs(round(submitted + drafts - stated, 2)) <= ABSOLUTE_TOLERANCE:
        return STATUS_DRIFTED, 'drafts_would_match', delta, delta_pct

    if delta_pct is None:
        return STATUS_MATCHED, '', delta, delta_pct
    if abs(delta_pct) <= threshold_pct:
        return STATUS_MATCHED, '', delta, delta_pct
    # Before there is a baseline, say so — the operator should read an early
    # flag as "no history yet", not as "this install normally does better".
    reason = ('no_baseline_yet' if threshold_source == 'default'
              else 'over_threshold')
    return STATUS_DRIFTED, reason, delta, delta_pct


def _tolerance() -> float:
    from . import statements as stmts
    try:
        return float(stmts.reconcile_tolerance())
    except Exception:  # noqa: BLE001 — a settings read must not fail the report
        return 0.005


def _period_gate(anchor: StatementAnchor) -> str:
    """'' when this period may be compared, else the SKIP_REASONS key.

    The kairotic gate, and every clause is a state test rather than a date
    test. An unreconciled period is excluded because its cash baseline is
    already known to be wrong, so a category delta measured against it would
    describe the known cash gap a second time instead of finding anything new."""
    if anchor is None:
        return 'no_anchor'
    if anchor.chain_gap_from_prior:
        return 'chain_gap'
    if not anchor.reconciles(_tolerance()):
        return 'not_reconciled'
    return ''


def _mask(account: PlaidAccount) -> str:
    return (getattr(account, 'mask', '') or '')[-4:]


def report(client, account_id: str = '', *,
           include_skipped: bool = True) -> dict:
    """The full statement-to-books comparison. A PURE READ — persists nothing
    and fires nothing (that is `observe`).

    One entry per (account, reconciled period, category). `include_skipped`
    keeps the periods that could not be compared, WITH their reason; turning it
    off is for a caller that only wants findings, never for hiding gaps.

    Raises the usual ERPNext errors when the ledger cannot be reached at all: a
    caller handed a report full of zero booked amounts would read it as "the
    books are empty", which is the one wrong answer this function could give."""
    from . import statements as stmts
    accounts = ([PlaidAccount.query.filter_by(account_id=account_id).first()]
                if account_id else stmts.accounts_with_anchors())
    accounts = [a for a in accounts if a is not None]

    rows: list = []
    skipped: list = []
    for account in accounts:
        anchors = (StatementAnchor.query
                   .filter_by(account_id=account.account_id)
                   .order_by(StatementAnchor.period_start.asc()).all())
        for anchor in anchors:
            statement = db.session.get(PlaidStatement, anchor.statement_id)
            period_label = (anchor.period_start.isoformat()[:7]
                            if anchor.period_start else '')
            reason = _period_gate(anchor)
            if not reason and statement is None:
                reason = 'no_statement_figures'
            if reason:
                if include_skipped:
                    skipped.append({
                        'account_id': account.account_id,
                        'account_mask': _mask(account),
                        'period': period_label,
                        'reason': reason,
                        'detail': SKIP_REASONS.get(reason, reason)})
                continue
            if not anchor.period_start or not anchor.period_end:
                if include_skipped:
                    skipped.append({
                        'account_id': account.account_id,
                        'account_mask': _mask(account),
                        'period': period_label,
                        'reason': 'no_statement_figures',
                        'detail': SKIP_REASONS['no_statement_figures']})
                continue

            booked = booked_journal_entries(account, anchor.period_start,
                                           anchor.period_end)
            # ONE batched ledger read for the whole period, before any category
            # is scored — see fetch_je_amounts on why this is not per entry.
            amounts = fetch_je_amounts(
                client, [gje.erpnext_journal_entry_name
                         for entries in booked.values() for gje in entries])
            for category in CATEGORIES:
                stated = statement_amount(statement, anchor, category)
                entries = booked.get(category.key) or []
                if stated is None and not entries:
                    # The statement says nothing and the books did nothing.
                    # Not a finding, and not worth a row.
                    continue
                submitted, drafts = booked_amounts(entries, amounts)
                threshold, source, samples = learned_threshold(
                    account.account_id, category.key)
                status, drift_reason, delta, delta_pct = classify(
                    stated, submitted, drafts, threshold, source,
                    informational=category.informational)
                rows.append({
                    'account_id': account.account_id,
                    'account_mask': _mask(account),
                    'account_label': stmts.account_label(account),
                    'period': period_label,
                    'period_start': anchor.period_start.isoformat(),
                    'period_end': anchor.period_end.isoformat(),
                    'statement_id': (statement.statement_id
                                     if statement else ''),
                    'category': category.key,
                    'category_label': category.label,
                    'statement_amount': (None if stated is None
                                         else round(stated, 2)),
                    'booked_amount': submitted,
                    'booked_draft_amount': drafts,
                    'journal_entries': len(entries),
                    'delta': delta,
                    'delta_pct': delta_pct,
                    'status': status,
                    'reason': drift_reason,
                    'reason_detail': DRIFT_REASONS.get(drift_reason, ''),
                    'threshold_pct': round(threshold, 6),
                    'threshold_source': source,
                    'baseline_samples': samples,
                    'note': category.note,
                })

    findings = [r for r in rows if r['status'] != STATUS_MATCHED]
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'accounts': len(accounts),
        'rows': rows,
        'skipped': skipped,
        'findings': findings,
        'matched': len(rows) - len(findings),
        'drifted': len([r for r in findings
                        if r['status'] == STATUS_DRIFTED]),
        'unexplained': len([r for r in findings
                            if r['status'] == STATUS_UNEXPLAINED]),
        'status': 'warn' if findings else 'ok',
        'headline': (
            f'{len(findings)} of {len(rows)} category comparison(s) diverge '
            f'from the statement across {len(accounts)} account(s)'
            if findings else
            f'all {len(rows)} category comparison(s) match the statements '
            f'across {len(accounts)} account(s)'),
        # Non-fatal and worth stating: the thresholds behind an early report
        # rest on a constant, not on this install's own behaviour.
        'baseline_ready': all(r['threshold_source'] != 'default'
                              for r in rows) if rows else False,
        'categories_are_not_additive': True,
    }


# ── the action, which is kairotic ───────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def observe(client, account_id: str = '') -> dict:
    """Compute the report, persist each comparison, and fire on a NEW or CHANGED
    verdict.

    Returns the report with three extra keys: `recorded` (how many rows were
    written or updated), `fired` (the comparisons that raised an alert on THIS
    call) and `already_known` (drifted comparisons that had already fired).

    WHY THE FIRING RULE IS WHAT IT IS. A reconciled period is settled — the
    statement will not change, so unlike a draft queue there is no "still
    breached" state to re-alert on. The meaningful moments are exactly two: a
    drifted comparison APPEARING for the first time, and a settled one CHANGING
    verdict because the books moved under it (someone submitted the drafts, or
    posted the missing leg). `fired_at` records that we have spoken, so the
    admin page and the MCP tool can be read as often as anyone likes without
    manufacturing a second alert. Re-deciding a settled question on every read
    is chronos.

    Best-effort on the WRITE half: a comparison that cannot be persisted is
    still returned. Losing a history row costs a little baseline precision;
    failing the caller costs them the answer."""
    result = report(client, account_id)
    fired: list = []
    already: list = []
    recovered: list = []
    recorded = 0

    for row in result['rows']:
        try:
            sample = (StatementReconSample.query
                      .filter_by(account_id=row['account_id'],
                                 period_start=date.fromisoformat(
                                     row['period_start']),
                                 category=row['category']).first())
            is_new = sample is None
            previous_status = None if is_new else (sample.status or '')
            if is_new:
                sample = StatementReconSample(
                    account_id=row['account_id'],
                    period_start=date.fromisoformat(row['period_start']),
                    category=row['category'])
                db.session.add(sample)
            sample.account_mask = row['account_mask']
            sample.period_end = date.fromisoformat(row['period_end'])
            sample.statement_ref = row['statement_id']
            sample.statement_amount = (row['statement_amount'] or 0.0)
            sample.booked_amount = row['booked_amount']
            sample.booked_draft_amount = row['booked_draft_amount']
            sample.delta = row['delta']
            sample.delta_pct = row['delta_pct']
            sample.status = row['status']
            sample.reason = row['reason']
            sample.threshold_pct = row['threshold_pct']
            sample.threshold_source = row['threshold_source']
            sample.baseline_samples = row['baseline_samples']
            sample.drifted = row['status'] != STATUS_MATCHED
            sample.updated_at = _now()

            # THE TRANSITION, and there are exactly three of them.
            #
            #   1. a finding APPEARS (new row, or a row that used to read
            #      differently) → fire.
            #   2. a finding RESOLVES — it had fired, and now matches → fire a
            #      recovery, once. Closing the loop is what makes the audit
            #      trail answer "and was it fixed?", which is the question after
            #      "what was wrong?". Same shape as draft_health_recovered.
            #   3. anything else, including a repeat read of an unchanged
            #      finding → say nothing. That silence is the kairotic property:
            #      a settled verdict re-alerting on every page load is chronos,
            #      and it is how an operator learns to filter the alert out.
            is_finding = row['status'] != STATUS_MATCHED
            changed = (previous_status is not None
                       and previous_status != row['status'])
            had_fired = sample.fired_at is not None
            if is_finding and (not had_fired or changed):
                sample.fired_at = _now()
                fired.append(row)
            elif is_finding:
                already.append(row)
            elif had_fired:
                # Resolved. Clearing `fired_at` is what keeps this a one-shot:
                # the next read sees a matched row that has never fired and
                # stays quiet.
                sample.fired_at = None
                recovered.append(row)
            db.session.commit()
            recorded += 1
        except Exception:  # noqa: BLE001 — see the docstring
            db.session.rollback()
            log.warning('could not persist recon sample for %s %s/%s',
                        row.get('account_mask'), row.get('period'),
                        row.get('category'), exc_info=True)

    for row in fired:
        _fire('statement_recon_drift', row)
    for row in recovered:
        _fire('statement_recon_resolved', row)

    result['recorded'] = recorded
    result['fired'] = fired
    result['already_known'] = already
    result['recovered'] = recovered
    return result


def _fire(event_type: str, row: dict) -> None:
    """The kairotic notification: one log line and one permanent AuditEvent, at
    the moment a finding appeared, changed or resolved, and at no other moment."""
    from . import audit
    resolved = event_type == 'statement_recon_resolved'
    message = (
        f"statement recon {'RESOLVED' if resolved else 'drift'} · "
        f"{row['account_mask']} {row['period']} {row['category']}: "
        f"statement {row['statement_amount']}, books {row['booked_amount']}, "
        f"delta {row['delta']}"
        + (f" ({row['delta_pct']:.1%})" if row['delta_pct'] is not None else '')
        + ('' if resolved else f" — {row['reason']}"))
    if resolved:
        log.info('[statement-recon] %s', message)
    else:
        log.warning('[statement-recon] %s', message)
    try:
        audit.record(event_type, subject_type='StatementAnchor',
                     subject_id=row.get('statement_id') or None,
                     after={k: row.get(k) for k in (
                         'account_id', 'account_mask', 'period',
                         'statement_id', 'category', 'statement_amount',
                         'booked_amount', 'booked_draft_amount', 'delta',
                         'delta_pct', 'status', 'reason', 'threshold_pct',
                         'threshold_source', 'baseline_samples')},
                     notes=message)
    except Exception:  # noqa: BLE001 — never break the caller on an audit write
        log.warning('could not audit %s', event_type, exc_info=True)


def observe_quietly(client, account_id: str = '') -> dict | None:
    """`observe`, but an ERPNext that cannot answer costs nothing.

    THE KAIROTIC TRIGGER, called from the statement reconcile path once a
    statement has arrived AND been anchored — not from a schedule. A recon read
    that failed must not turn a successful statement import into a failed one.
    Returns None when the comparison could not be made."""
    if client is None:
        return None
    try:
        return observe(client, account_id)
    except (ERPNextAPIError, ERPNextError):
        log.warning('statement recon skipped — ERPNext unreadable',
                    exc_info=True)
        return None
    except Exception:  # noqa: BLE001 — defensive; this is a reporting read
        db.session.rollback()
        log.warning('statement recon failed', exc_info=True)
        return None
