# SPDX-License-Identifier: MIT
"""Bank-issued statements: the only numbers here the BANK asserts (v0.4.9).

Everything else Bank Bridge holds is either a transaction Plaid mirrored or a
figure this app derived from those transactions. A statement is different — it
is the institution's own monthly assertion of what an account opened and closed
at — and that makes it worth the trouble twice over:

  1. OPENING BALANCES WITHOUT ARITHMETIC. `opening_balance.estimate_opening_
     balance` works backwards from today's balance by subtracting every
     transaction it has mirrored, which is exact only if the mirror is complete.
     It says so itself, which is why the v0.4.4 backfill lands its entries in
     `pending_review` for an operator to check against a real statement. A
     statement removes the arithmetic AND the manual check: the bank already
     wrote the number down.

  2. RECONCILIATION. A closing balance is a monthly checkpoint the mirror can be
     measured against — opening + Σ(mirrored movement in the period) should land
     on closing. When it doesn't, the mirror has a gap, and /admin/statements
     shows that as a dollar delta without anyone opening ERPNext.

WHAT PLAID ACTUALLY GIVES US, which shapes this whole module: /statements/list
returns statement_id, month, year and a nullable date_posted. That is all. No
balances, no explicit period bounds, no currency. The balances exist ONLY inside
the PDF. So every number this module contributes is recovered by regex over text
extracted from a PDF whose layout is the bank's business, not ours — and the
honest consequence is that parsing FAILS ROUTINELY. A layout we can't read
yields NULL, never a guess.

That single fact drives the design: every consumer treats a missing statement,
an unparseable statement, and a disabled feature identically, and falls back to
v0.4.4 behaviour. Statements can only ever make an opening balance better; they
are structurally incapable of making one worse.

WHY THE IMPORT PATH DOESN'T ANCHOR ON A STATEMENT. At initial link there is no
estimate to improve on: `balances.current` from /accounts/get IS the bank's
number, and posting it as of today is exact. The estimate — and therefore the
problem statements solve — only exists for accounts linked before v0.4.4, which
is what `scripts/backfill_statements.py` and `book_opening_balance(...,
prefer_statement=True)` are for. Import still FETCHES statements (for the audit
trail and the reconciliation view); it just doesn't change the number it books.
Keeping that boundary means an upgrade cannot move an operator's balance sheet.

ANCHOR SAFETY is `choose_anchor_statement`, and it is the load-bearing check in
this module. Booking an opening balance dated a statement's period_start is only
correct if the mirror contains exactly the movement from that date forward —
transactions dated BEFORE the anchor would be counted twice, and a gap after it
would leave the position short. A statement lets us test that rather than assume
it: if opening + mirrored movement lands on closing, the mirror is demonstrably
complete for that period. A statement that fails its own reconciliation is never
anchored on.
"""
from __future__ import annotations

import calendar
import logging
import os
import re
import time
from datetime import date, datetime, timezone

from flask import current_app

from . import audit
from . import crypto
from . import db
from .models import BankTransaction, PlaidAccount, PlaidItem, PlaidStatement
from .plaid_client import PlaidError

log = logging.getLogger('bankbridge.statements')


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── configuration ───────────────────────────────────────────────────────────

def is_enabled() -> bool:
    """Whether statements are pulled at all (STATEMENTS_ENABLED, default true).
    Off means no listing, no download, no storage — and every opening balance
    path behaves exactly as it did in v0.4.4."""
    return bool(current_app.config.get('STATEMENTS_ENABLED', True))


def storage_root() -> str:
    """The directory statement PDFs are filed under (STATEMENTS_STORAGE_PATH,
    default {DATA_DIR}/statements). Derived from DATA_DIR rather than hardcoded
    to /data/statements so tests, and any install that moved its data volume,
    keep their PDFs beside the rest of their persistent state."""
    configured = (current_app.config.get('STATEMENTS_STORAGE_PATH') or '').strip()
    if configured:
        return configured
    return os.path.join(current_app.config.get('DATA_DIR', '/data'), 'statements')


def pull_interval_days() -> int | None:
    """The monthly job's cadence in days, or None when it is disabled (<= 0)."""
    try:
        days = int(current_app.config.get('STATEMENTS_PULL_INTERVAL_DAYS', 30))
    except (TypeError, ValueError):
        days = 30
    return days if days > 0 else None


def reconcile_tolerance() -> float:
    """How far a statement's closing balance may sit from the mirror's own
    arithmetic before the period counts as NOT reconciled."""
    try:
        return abs(float(current_app.config.get(
            'STATEMENTS_RECONCILE_TOLERANCE', 1.00)))
    except (TypeError, ValueError):
        return 1.00


def _download_attempts() -> int:
    try:
        return max(1, int(current_app.config.get(
            'STATEMENTS_DOWNLOAD_ATTEMPTS', 3)))
    except (TypeError, ValueError):
        return 3


# ── period helpers ──────────────────────────────────────────────────────────

def period_bounds(month, year) -> tuple[date | None, date | None]:
    """(first_day, last_day) for a Plaid statement's month + year, or (None,
    None) when either is missing or out of range.

    Plaid gives no explicit period bounds — month and year are the entire period
    description — so the calendar month IS the period as far as we can know. A
    bank whose statement cycle doesn't align to calendar months will therefore
    have period bounds that are approximate; that only affects which mirrored
    transactions fall inside the reconciliation window, and a cycle-misaligned
    statement simply fails to reconcile and is never anchored on."""
    try:
        m, y = int(month), int(year)
    except (TypeError, ValueError):
        return None, None
    if not (1 <= m <= 12) or not (1900 <= y <= 9999):
        return None, None
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def period_label(start: date | None) -> str:
    """'2026-07' — the key a statement's PDF is filed under."""
    return f'{start.year:04d}-{start.month:02d}' if start else 'unknown'


# ── PDF storage ─────────────────────────────────────────────────────────────

_UNSAFE = re.compile(r'[^A-Za-z0-9._-]')


def _safe(component: str) -> str:
    """One path component, reduced to characters that cannot escape the storage
    root. Plaid's ids are opaque tokens we never chose, so they are treated as
    untrusted input: every character outside [A-Za-z0-9._-] becomes '_', which
    collapses '..' and any separator into something inert."""
    cleaned = _UNSAFE.sub('_', (component or '').strip())
    # '.' survives the character filter (a legitimate part of a filename), so a
    # '..' can still be spelled even once every separator is gone. Collapsing it
    # here means no component can name a parent directory at all — belt and
    # braces alongside resolve_pdf_path's containment check, since this is the
    # half that runs BEFORE anything is written to disk.
    while '..' in cleaned:
        cleaned = cleaned.replace('..', '_')
    return cleaned.strip('.') or 'unknown'


def pdf_path_for(item_id: str, account_id: str, label: str,
                 statement_id: str = '') -> str:
    """Absolute path for one statement's PDF:
    {root}/{item_id}/{account_id}/{yyyy-mm}.pdf

    `statement_id` only matters when a DIFFERENT statement already occupies that
    filename — a re-issued or corrected statement for a month we already hold —
    in which case a short, stable suffix keeps both rather than silently
    overwriting the bank's earlier document."""
    base = os.path.join(storage_root(), _safe(item_id), _safe(account_id))
    path = os.path.join(base, f'{_safe(label)}.pdf')
    if statement_id:
        clash = PlaidStatement.query.filter(
            PlaidStatement.pdf_path == path,
            PlaidStatement.statement_id != statement_id).first()
        if clash is not None:
            path = os.path.join(
                base, f'{_safe(label)}-{_safe(statement_id)[:8]}.pdf')
    return path


def store_pdf(path: str, data: bytes) -> int:
    """Write the PDF, creating its directory. Returns the byte count.

    Writes to a temporary sibling and renames, so a crash mid-write can never
    leave a truncated PDF that later looks present-and-valid to the fetch path's
    "do we already have this file?" check."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.part'
    with open(tmp, 'wb') as fh:
        fh.write(data)
    os.replace(tmp, path)
    return len(data)


def pdf_exists(statement: PlaidStatement) -> bool:
    """Whether this row's PDF is actually on disk. A row can outlive its file (a
    wiped or restored-from-backup data volume), and the fetch path re-downloads
    on that case rather than trusting the row alone."""
    path = (statement.pdf_path or '').strip()
    return bool(path) and os.path.isfile(path)


def resolve_pdf_path(statement: PlaidStatement) -> str | None:
    """The on-disk path to serve for this statement, or None when there is
    nothing safe to serve.

    Re-checks containment under the storage root even though pdf_path was
    written by pdf_path_for: the value round-trips through the database, and a
    file-serving route must never trust a stored path to still be inside the
    directory it is allowed to read from."""
    path = (statement.pdf_path or '').strip()
    if not path or not os.path.isfile(path):
        return None
    root = os.path.realpath(storage_root())
    real = os.path.realpath(path)
    if not (real == root or real.startswith(root + os.sep)):
        log.warning('refusing to serve %s — outside the statement store', real)
        return None
    return real


# ── PDF text → structured statement metadata ────────────────────────────────
#
# The bank's layout is the bank's business, so this is deliberately a
# RECOGNIZER, not a parser: it looks for labels institutions actually print and
# takes the amount each one owns. Anything it doesn't recognize yields None,
# which every caller reads as "no bank-issued figure" and falls back on.
#
# v0.4.41 · TWO LESSONS FROM REAL STATEMENTS.
#
# 1. LINE STRUCTURE IS EVIDENCE. Up to v0.4.40 this worked on one flat,
#    whitespace-collapsed string and took the first amount within 120
#    characters of a label. That is safe on a one-column consumer statement and
#    actively WRONG on a Wells Fargo Advisors brokerage statement, whose "Cash
#    sweep activity" table extracts as
#
#      TRANSFER TO BANK DEPOSIT SWEEP ENDING BALANCE06/16 100,000.00 06/30 39,751.95
#
#    — so 'ending balance' claimed 100,000.00, a single sweep transfer, as the
#    month's closing balance. Every one of the 26 real statements on the live
#    install parsed a wrong closing figure that way, and a wrong bank-asserted
#    number is worse than none: it is the input to reconciliation AND to
#    choose_anchor_statement, i.e. to a posted opening balance. Matching is now
#    per LINE, and the run between a label and its amount must contain no
#    digits and stay short (see _amounts_on_line).
#
# 2. LABELS ONLY MEAN SOMETHING INSIDE THEIR SECTION. 'Interest', 'Total' and
#    'Other additions' appear many times in a 35-page brokerage statement, and
#    the one that matters is the one under a particular summary heading. Fields
#    are therefore looked up inside a named SECTION (see _section), which is
#    also what stops the twelve-page holdings table — whose every row begins
#    'Total …' — from answering a question about realized gains.
#
# WHAT IS AND IS NOT VERIFIED AGAINST REAL DOCUMENTS. The `wf_advisors` field
# table below was written against 26 production Wells Fargo Advisors statements
# and every field in it was checked on at least two of them. The `wf_deposit`
# and `wf_card` tables were written from the standard Wells Fargo consumer
# layouts and have NOT been checked against a real document, because the live
# install holds none — its deposit and card "statements" are Plaid sandbox
# mocks ('First Platypus Bank', 'Balance on XX/XX:') with no labelled totals at
# all. They are safe in the sense every recognizer here is safe — an
# unrecognized layout yields None, never a guess — but treat a figure they
# produce as unconfirmed until a real statement has been eyeballed against it.

# Bump when a change here could produce DIFFERENT numbers from the same PDF.
# Stored on every row (`parsed_metadata['parser_version']`), which is what lets
# an operator tell a stale parse from a current one and re-parse only what is
# behind — see reparse_stored.
PARSER_VERSION = '0.4.44'

# Ordered most- to least-specific. 'previous balance' must be tried before a
# bare 'balance', and the credit-card wording ('previous balance', 'new
# balance') sits alongside the depository wording because one account's
# statement uses one vocabulary or the other, never both.
_OPENING_LABELS = (
    'beginning balance', 'opening balance', 'previous balance',
    'balance forward', 'previous statement balance', 'starting balance',
    'balance last statement', 'opening value',
)
_CLOSING_LABELS = (
    'ending balance', 'closing balance', 'new balance', 'ending daily balance',
    'statement balance', 'new balance total', 'balance this statement',
    'closing value',
)

# A US-format currency amount: optional leading '-' or '(', optional '$',
# grouped digits, exactly two decimals, optional trailing ')'. The parenthesis
# and the minus both mean negative, which is how statements print an overdrawn
# depository account or a credit balance on a card.
_AMOUNT = r'\(?-?\s*\$?\s*(\d{1,3}(?:,\d{3})*|\d+)\.(\d{2})\s*\)?'
_AMOUNT_RE = re.compile(_AMOUNT)

# 'Beginning balance on 6/1', 'Ending balance on June 30, 2026' — the one thing
# banks routinely print BETWEEN a balance label and its amount. Stripped before
# the digit-free-gap rule below, which would otherwise reject the very layout
# (Wells Fargo's own consumer checking statement) it is meant to read.
_ON_DATE = re.compile(
    r'^\s*(?:as\s+of\s+|on\s+|through\s+|for\s+)'
    r'(?:\d{1,2}/\d{1,2}(?:/\d{2,4})?'
    r'|[a-z]{3,9}\.?\s+\d{1,2}(?:\s*,\s*\d{4})?)\s*[:.\-]?\s*',
    re.IGNORECASE)

# A line that OPENS with a MM/DD (optionally MM/DD/YY) is a transaction row.
# Wells Fargo's cash-sweep table prints '06/01 BEGINNING BALANCE $0.00' there —
# a per-day running total of a sub-account, never the statement's own figure.
_ROW_PREFIX = re.compile(r'^\d{1,2}/\d{1,2}(?:/\d{2,4})?\b')

# How much unlabelled text may sit between a label and the amount it owns.
# Generous enough for a dotted leader or a column header fragment, far too
# small to reach the next labelled figure.
_MAX_GAP = 60


def extract_pages(pdf_bytes: bytes) -> list[str]:
    """Per-page text, in page order, or [] when the PDF can't be read.

    [] is a first-class outcome, not an error: a scanned/image-only statement
    has no text layer at all, and a PDF that pypdf refuses is common enough in
    the wild that raising here would turn a routine parse miss into a failed
    fetch. The bytes are still stored either way — an operator can always open
    the document even when we can't read it."""
    if not pdf_bytes:
        return []
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        log.info('could not extract text from statement PDF: %s', e)
        return []
    pages = []
    for pageno, page in enumerate(reader.pages):
        try:
            pages.append(page.extract_text() or '')
        except Exception:
            log.debug('unreadable page %d in statement PDF', pageno)
            pages.append('')
    return pages


def extract_lines(pdf_bytes: bytes) -> list[str]:
    """Every non-blank line of the PDF, lowercased, inner whitespace collapsed,
    in document order.

    Line structure is the whole point (see this section's header): it is what
    separates 'Closing value $7,196.73' — a summary the bank asserts — from a
    sweep-table row that merely happens to contain the word 'balance'."""
    out = []
    for page in extract_pages(pdf_bytes):
        for raw in page.splitlines():
            line = re.sub(r'\s+', ' ', raw).strip().lower()
            if line:
                out.append(line)
    return out


def extract_text(pdf_bytes: bytes) -> str:
    """All text in the PDF, lowercased and whitespace-collapsed, or '' when it
    can't be read. Retained as the flat-text fallback for layouts whose label
    and amount pypdf does not place on one line."""
    pages = extract_pages(pdf_bytes)
    if not pages:
        return ''
    return re.sub(r'\s+', ' ', ' '.join(pages)).strip().lower()


# ── amounts ─────────────────────────────────────────────────────────────────

def _to_float(match) -> float:
    """One _AMOUNT_RE match as a signed float. '(1,234.56)' and '-1,234.56' are
    the two ways a statement prints the same negative."""
    value = float(f"{match.group(1).replace(',', '')}.{match.group(2)}")
    matched = match.group(0)
    if matched.lstrip().startswith('(') or '-' in matched.split('$')[0]:
        value = -value
    return round(value, 2)


def _label_at(line: str, label: str) -> int:
    """Where `label` starts as a WHOLE WORD in `line`, or -1.

    A plain substring search is wrong for the short labels a statement prints.
    Wells Fargo's income summary has a row labelled just 'Other' — and 'other'
    is a substring of 'Other additions', 'Other subtractions' and any number of
    disclosure sentences. Requiring a non-letter before the match (and after,
    so 'interest' does not answer for 'interest-bearing') makes a bare label
    safe to use, which is what lets the field tables name one."""
    start = 0
    while True:
        idx = line.find(label, start)
        if idx < 0:
            return -1
        before = line[idx - 1] if idx else ' '
        after_pos = idx + len(label)
        after = line[after_pos] if after_pos < len(line) else ' '
        if not before.isalpha() and not after.isalpha():
            return idx
        start = idx + 1


def _amounts_on_line(line: str, label: str) -> list[float]:
    """Every amount `label` owns on this already-lowercased line, left to right.

    Ownership of the FIRST amount is decided by what sits between it and the
    label, which is the only signal available once a PDF's columns have been
    flattened into text. Three rules, each of which a real statement taught us:

      * a line that opens with a MM/DD is a transaction row, never a summary
      * the gap may carry an 'on <date>' clause ('Beginning balance on 6/1'),
        which is stripped before the next rule
      * whatever is left must contain NO digits and stay under _MAX_GAP. A digit
        in the gap means another column intervened — 'ending balance06/16
        100,000.00' is a sweep transfer, not a closing balance — and a long gap
        means the label and the figure are simply not related.

    Once the first amount qualifies, the rest of the line follows it: statement
    summaries are printed as THIS PERIOD then THIS YEAR then sometimes a chart
    label, and a caller asks for the column it wants by index. [] is the safe
    answer and the common one."""
    if _ROW_PREFIX.match(line):
        return []
    idx = _label_at(line, label)
    if idx < 0:
        return []
    rest = line[idx + len(label):]
    matches = list(_AMOUNT_RE.finditer(rest))
    if not matches:
        return []
    gap = _ON_DATE.sub('', rest[:matches[0].start()])
    if len(gap) > _MAX_GAP or any(c.isdigit() for c in gap):
        return []
    return [_to_float(m) for m in matches]


def _find_amounts(lines, labels, exclude: tuple = ()) -> list[float]:
    """The amounts owned by the first of `labels` that any line answers to,
    scanning labels most-specific first and, for each, the document top-down.

    `exclude` names substrings that disqualify a line for this lookup — it is
    what keeps the bare label 'opening value' from claiming the figure on
    'Opening value of cash and sweep balances', a DIFFERENT number on the same
    Wells Fargo statement."""
    for label in labels:
        for line in lines:
            if any(bad in line for bad in exclude):
                continue
            found = _amounts_on_line(line, label)
            if found:
                return found
    return []


def _find_label_amount(lines, labels, exclude: tuple = ()) -> float | None:
    """The first (this-period) amount any of `labels` owns, or None."""
    found = _find_amounts(lines, labels, exclude=exclude)
    return found[0] if found else None


# ── sections ────────────────────────────────────────────────────────────────
#
# A summary heading and the handful of lines beneath it. Scoping a lookup to a
# section is what makes generic words usable: 'Interest' means the income line
# under "Income summary" and nothing else, and 'Total' means the gain/loss
# roll-up rather than any of the ~90 holdings rows that also start with it.

# How many lines after a heading still count as part of its block. Every real
# Wells Fargo summary block fits inside 14; a larger window would start
# swallowing the block below it.
_SECTION_SPAN = 14


def _is_section_heading(line: str, header: str) -> bool:
    """Whether `line` is the HEADING of section `header` rather than prose that
    happens to mention it.

    Real statements print disclosure text like "Income summary: The Income
    summary displays all income as recorded in the tax system…", and a naive
    substring match would scope every income lookup to that paragraph. A real
    heading either carries its column headers ('… THIS PERIOD THIS YEAR') or is
    short — a table heading is a few words, an explanatory sentence is not."""
    if header not in line:
        return False
    return 'this period' in line or len(line) <= 60


def _section(lines, header: str, span: int = _SECTION_SPAN) -> list[str]:
    """The lines under every occurrence of section `header`, concatenated.

    Every occurrence, because a statement can restate a summary per sub-account
    and the first one is not always the one carrying the figure. Callers still
    take the first answering line, so an earlier section wins where both have
    the field."""
    out = []
    for i, line in enumerate(lines):
        if _is_section_heading(line, header):
            out.extend(lines[i + 1: i + 1 + span])
    return out


# ── field tables ────────────────────────────────────────────────────────────
#
# One row per extracted figure: (key, section, labels, column, exclude).
#
#   section  — the summary heading the label must sit under, '' for anywhere
#   labels   — tried in order, most specific first
#   column   — which amount on the line: 0 = THIS PERIOD, 1 = THIS YEAR, …
#   exclude  — substrings that disqualify a line
#
# SIGNS ARE THE BANK'S, NOT OURS. A field holds exactly what the statement
# printed: 'Cash withdrawn -20,047.16' is stored as -20047.16 and 'Cash
# deposited 10,000.00' as 10000.00. Normalising here would bury the one thing
# an audit wants — what the document says — under our own convention. The
# validation view does the normalising, once, where it is visible.

# Wells Fargo Advisors (brokerage). VERIFIED against 26 production statements.
#
# Two blocks carry balances and they are DIFFERENT NUMBERS: the Progress
# summary's 'Opening/Closing value' is total account value (cash plus
# securities at market), while the Cash flow summary's 'Opening/Closing value
# of cash and sweep balances' is the cash side alone. `opening_balance` and
# `closing_balance` take the CASH figure, because that is the one
# reconciliation can test — signed_movement sums cash events and has no record
# of market movement, so anchoring on total value would make every brokerage
# period fail its own reconciliation and never qualify in
# choose_anchor_statement.
_WF_ADVISORS_FIELDS = (
    # Progress summary — the account as a whole
    ('portfolio_opening', 'progress summary', ('opening value',), 0,
     ('of cash and sweep',)),
    ('portfolio_closing', 'progress summary', ('closing value',), 0,
     ('of cash and sweep',)),
    ('deposits_total', 'progress summary', ('cash deposited',), 0, ()),
    ('withdrawals_total', 'progress summary', ('cash withdrawn',), 0, ()),
    ('securities_deposited', 'progress summary', ('securities deposited',), 0, ()),
    ('securities_withdrawn', 'progress summary', ('securities withdrawn',), 0, ()),
    ('change_in_value', 'progress summary', ('change in value',), 0, ()),
    # Cash flow summary — the cash side, line by line
    ('cash_opening', 'cash flow summary',
     ('opening value of cash and sweep balances',), 0, ()),
    ('cash_closing', 'cash flow summary',
     ('closing value of cash and sweep balances',), 0, ()),
    ('income_distributions', 'cash flow summary',
     ('income and distributions',), 0, ()),
    ('securities_sold', 'cash flow summary',
     ('securities sold and redeemed',), 0, ()),
    ('securities_purchased', 'cash flow summary',
     ('securities purchased',), 0, ()),
    ('other_additions', 'cash flow summary', ('other additions',), 0, ()),
    ('other_subtractions', 'cash flow summary',
     ('other subtractions, transfers & charges', 'other subtractions'), 0, ()),
    ('advisory_fees', 'cash flow summary',
     ('advisory, manager and platform fees',), 0, ()),
    ('net_additions', 'cash flow summary',
     ('net additions to cash',), 0, ()),
    ('net_subtractions', 'cash flow summary',
     ('net subtractions from cash',), 0, ()),
    ('check_withdrawals', 'cash flow summary',
     ('withdrawals by check',), 0, ()),
    ('atm_activity', 'cash flow summary',
     ('atm and checkcard activity',), 0, ()),
    # NOTE 'Electronic funds transfers' is NOT here — it is printed twice, once
    # under additions and once under subtractions, and is split by position in
    # _extract_electronic_transfers.
    # Income summary
    ('interest_income', 'income summary', ('interest',), 0, ()),
    ('sweep_income', 'income summary',
     ('taxable money market/sweep funds',
      'money market/sweep funds'), 0, ()),
    ('ordinary_dividends', 'income summary',
     ('ordinary dividends and st capital gains',), 0, ()),
    ('qualified_dividends', 'income summary', ('qualified dividends',), 0, ()),
    # A bare label, safe only because _label_at matches whole words — 'other'
    # is a substring of 'Other additions' and of most disclosure sentences.
    ('other_income', 'income summary', ('other',), 0, ()),
    ('total_taxable_income', 'income summary',
     ('total taxable income',), 0, ()),
    ('tax_exempt_income', 'income summary',
     ('total federally tax-exempt income',), 0, ()),
    ('total_income', 'income summary', ('total income',), 0, ()),
    # Additional information — one printed line carries both fields.
    ('gross_proceeds', '', ('gross proceeds',), 0, ()),
    ('foreign_withholding', '', ('foreign withholding',), 0, ()),
    # NOTE the gain/loss summary is NOT here — it is the one block whose rows
    # each carry three meaningful columns, so it is extracted by
    # _extract_gainloss into a dict per row rather than flattened into nine
    # loose keys.
    # Anywhere
    ('available_funds', '', ('your total available funds',), 0, ()),
)

# The gain/loss summary, whose heading names three columns:
#
#   Gain/loss summary **   UNREALIZED | THIS PERIOD REALIZED | THIS YEAR REALIZED
#   Short term/Net lots          0.00 |                 0.00 |            0.00
#   Long term (L)                0.00 |                 0.00 |            0.00
#   Total                       $0.00 |                $0.00 |           $0.00
#
# Each row becomes one dict keyed by column, because the three numbers are one
# fact about one holding period and splitting them into `short_term_unrealized`
# / `short_term_realized` / `short_term_realized_ytd` would be nine keys
# describing a 3×3 table. The roll-up row is literally 'Total', which is why
# this MUST stay section-scoped: the holdings tables further on print ~90 more
# lines beginning with that same word.
#
# 'Short term/Net lots' is the wording on an account holding only sweep cash;
# 'Short term (S)' is what an account with securities prints. Both appear in
# production, so both are listed.
_GAINLOSS_ROWS = (
    ('short_term_gainloss', ('short term/net lots', 'short term (s)',
                             'short term')),
    ('long_term_gainloss', ('long term (l)', 'long term')),
    ('total_gainloss', ('total',)),
)
_GAINLOSS_COLUMNS = ('unrealized', 'realized_period', 'realized_ytd')

# Wells Fargo deposit (checking / savings / money market).
# UNVERIFIED — see this section's header. Written from the standard Wells Fargo
# "Activity summary" layout; the live install holds no real deposit statement
# to check it against.
_WF_DEPOSIT_FIELDS = (
    ('opening_balance', '',
     ('beginning balance on', 'beginning balance', 'previous balance'), 0, ()),
    ('closing_balance', '',
     ('ending balance on', 'ending balance', 'closing balance'), 0, ()),
    ('deposits_total', '',
     ('total deposits and other credits', 'deposits and other credits',
      'deposits/additions', 'deposits/credits'), 0, ()),
    ('withdrawals_total', '',
     ('total withdrawals and other debits', 'withdrawals and other debits',
      'withdrawals/subtractions', 'withdrawals/debits'), 0, ()),
    ('check_withdrawals', '', ('checks paid', 'total checks'), 0, ()),
    ('interest_earned', '',
     ('interest earned this statement period', 'interest paid this period',
      'interest earned', 'interest paid'), 0, ()),
    ('service_fees', '',
     ('total service fees', 'monthly service fee', 'service charges',
      'service fee'), 0, ()),
    ('average_ledger_balance', '',
     ('average ledger balance', 'average daily ledger balance'), 0, ()),
    ('average_collected_balance', '', ('average collected balance',), 0, ()),
)

# Wells Fargo credit card.
# UNVERIFIED — see this section's header.
_WF_CARD_FIELDS = (
    ('previous_balance', '', ('previous balance',), 0, ()),
    ('new_balance', '', ('new balance total', 'new balance'), 0, ()),
    ('payments_total', '',
     ('total payments and credits', 'payments and credits', 'payments'), 0, ()),
    ('purchases_total', '',
     ('total purchases and adjustments', 'purchases and adjustments',
      'total purchases', 'purchases'), 0, ()),
    ('cash_advances_total', '',
     ('total cash advances', 'cash advances'), 0, ()),
    ('fees_total', '',
     ('total fees charged for this period', 'total fees charged',
      'fees charged', 'total fees'), 0, ()),
    ('interest_charged', '',
     ('total interest charged for this period', 'total interest charged',
      'interest charged', 'total interest'), 0, ()),
    ('minimum_payment_due', '',
     ('minimum payment due', 'minimum payment'), 0, ()),
    ('credit_limit', '', ('total credit limit', 'credit limit'), 0, ()),
    ('available_credit', '', ('available credit',), 0, ()),
)

# Which figures become the top-level opening_balance / closing_balance columns,
# per layout. Those two columns are what reconciliation, anchoring and the
# ERPNext Bank Statement record read, so the mapping is stated once, here,
# rather than implied by whatever the parser happened to fill in.
_HEADLINE_FIELDS = {
    'wf_advisors': ('cash_opening', 'cash_closing'),
    'wf_deposit': ('opening_balance', 'closing_balance'),
    'wf_card': ('previous_balance', 'new_balance'),
}

_LAYOUT_FIELDS = {
    'wf_advisors': _WF_ADVISORS_FIELDS,
    'wf_deposit': _WF_DEPOSIT_FIELDS,
    'wf_card': _WF_CARD_FIELDS,
}

# Layouts whose field table has been checked against a real document. Surfaced
# in the metadata so a validation screen can say so rather than implying every
# number carries the same weight.
_VERIFIED_LAYOUTS = frozenset({'wf_advisors'})


# ── statement period ────────────────────────────────────────────────────────

_MONTHS = {m: i for i, m in enumerate(
    ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
     'september', 'october', 'november', 'december'), start=1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})

_MONTH_NAME = r'(' + '|'.join(sorted(_MONTHS, key=len, reverse=True)) + r')'
# 'june 1, 2026 - june 30, 2026' — what Wells Fargo Advisors prints in the
# page footer, and the only place either bound appears in full.
_RANGE_WORDS = re.compile(
    _MONTH_NAME + r'\.?\s+(\d{1,2}),?\s+(\d{4})\s*(?:-|–|through|to)\s*'
    + _MONTH_NAME + r'\.?\s+(\d{1,2}),?\s+(\d{4})')
_RANGE_SLASH = re.compile(
    r'(\d{1,2})/(\d{1,2})/(\d{2,4})\s*(?:-|–|through|to)\s*'
    r'(\d{1,2})/(\d{1,2})/(\d{2,4})')


def _year(text: str) -> int:
    value = int(text)
    return value + 2000 if value < 100 else value


def parse_period(lines) -> tuple[date | None, date | None]:
    """(start, end) the statement states for itself, or (None, None).

    Worth recovering even though `period_bounds` already derives a period from
    Plaid's month + year: those are the CALENDAR month, and a bank whose cycle
    does not align to one has a real period this is the only record of. A
    mismatch between the two is itself a finding, which is why this is stored
    beside the derived bounds rather than overwriting them."""
    for line in lines:
        m = _RANGE_WORDS.search(line)
        if m:
            try:
                return (date(_year(m.group(3)), _MONTHS[m.group(1)],
                             int(m.group(2))),
                        date(_year(m.group(6)), _MONTHS[m.group(4)],
                             int(m.group(5))))
            except ValueError:
                continue
        m = _RANGE_SLASH.search(line)
        if m:
            try:
                return (date(_year(m.group(3)), int(m.group(1)),
                             int(m.group(2))),
                        date(_year(m.group(6)), int(m.group(4)),
                             int(m.group(5))))
            except ValueError:
                continue
    return None, None


_DUE_DATE = re.compile(
    r'payment due date[^0-9a-z]*(?:' + _MONTH_NAME
    + r'\.?\s+(\d{1,2}),?\s+(\d{4})|(\d{1,2})/(\d{1,2})/(\d{2,4}))')


def parse_due_date(lines) -> date | None:
    """A credit card's payment due date, or None. Card-only; no other statement
    prints one."""
    for line in lines:
        m = _DUE_DATE.search(line)
        if not m:
            continue
        try:
            if m.group(1):
                return date(_year(m.group(3)), _MONTHS[m.group(1)],
                            int(m.group(2)))
            return date(_year(m.group(6)), int(m.group(4)), int(m.group(5)))
        except (ValueError, TypeError):
            continue
    return None


# ── layout detection ────────────────────────────────────────────────────────

def detect_layout(lines) -> str:
    """Which field table to use: 'wf_advisors', 'wf_card', 'wf_deposit',
    'generic', or '' when there is no text at all.

    Keyed on the blocks a layout prints rather than on the institution name,
    because it is the block — not the brand — that the field tables know how to
    read. Ordered most- to least-distinctive: a brokerage statement also
    contains the word 'balance', and a card statement also contains 'previous
    balance', so the cheap generic tests have to come last."""
    if not lines:
        return ''
    joined = ' '.join(lines)
    if ('of cash and sweep balances' in joined
            or 'wells fargo advisors' in joined):
        return 'wf_advisors'
    if ('minimum payment due' in joined
            or ('previous balance' in joined and 'new balance' in joined)):
        return 'wf_card'
    if any(label in joined for label in
           ('beginning balance', 'ending balance', 'deposits and other credits',
            'withdrawals and other debits')):
        return 'wf_deposit'
    return 'generic'


# ── extraction ──────────────────────────────────────────────────────────────

def _extract_fields(lines, specs) -> tuple[dict, list[str]]:
    """Run one field table over the document.

    Each field is extracted INSIDE ITS OWN try/except and a failure records the
    key rather than propagating. That is deliberate and load-bearing: these
    tables will grow, most of them describe layouts nobody here has seen, and a
    single bad pattern must cost one figure — not the whole metadata blob for a
    statement whose balances parsed perfectly. Returns (values, failed_keys),
    and `failed` is stored so an operator can see WHICH field went wrong on
    WHICH statement instead of inferring it from a hole."""
    values, failed = {}, []
    for key, section, labels, column, exclude in specs:
        try:
            scope = _section(lines, section) if section else lines
            found = _find_amounts(scope, labels, exclude=exclude)
            if len(found) > column:
                values[key] = found[column]
        except Exception:
            log.warning('statement field %r failed to extract', key,
                        exc_info=True)
            failed.append(key)
    return values, failed


def _extract_gainloss(lines) -> dict:
    """The gain/loss summary as {row_key: {column: amount}}.

    Separate from the generic field table because this is the one block whose
    columns all carry meaning — see _GAINLOSS_ROWS. A row is only emitted when
    the line actually carried three amounts; a two-column variant would
    otherwise silently label the year-to-date figure as this period's."""
    scope = _section(lines, 'gain/loss summary')
    if not scope:
        return {}
    out = {}
    for key, labels in _GAINLOSS_ROWS:
        found = _find_amounts(scope, labels)
        if len(found) >= len(_GAINLOSS_COLUMNS):
            out[key] = dict(zip(_GAINLOSS_COLUMNS, found))
    return out


def _extract_electronic_transfers(lines) -> dict:
    """'Electronic funds transfers', which is printed TWICE.

    The cash flow summary lists it once under additions and once under
    subtractions, with identical wording — the sign lives in the position, not
    the label:

        Income and distributions            1,121.79
        Securities sold and redeemed       24,705.10
        Electronic funds transfers              0.00   <- money IN
        Other additions                         0.00
        Net additions to cash             $25,826.89
        Securities purchased              -19,587.94
        Electronic funds transfers              0.00   <- money OUT
        Advisory, manager and platform fees     0.00

    So the two are split on the 'Net additions to cash' line that separates the
    halves, and stored as two keys rather than one. A single key cannot
    represent two different lines, and picking whichever the recognizer saw
    first would silently report an inflow as an outflow on any month where they
    differ — which is exactly the month it would matter."""
    scope = _section(lines, 'cash flow summary')
    if not scope:
        return {}
    pivot = next((i for i, line in enumerate(scope)
                  if _label_at(line, 'net additions to cash') >= 0), None)
    if pivot is None:
        return {}
    label = ('electronic funds transfers',)
    out = {}
    inflow = _find_amounts(scope[:pivot], label)
    outflow = _find_amounts(scope[pivot + 1:], label)
    if inflow:
        out['electronic_transfers_in'] = inflow[0]
    if outflow:
        out['electronic_transfers_out'] = outflow[0]
    return out


# Descriptive (non-numeric) fields: what KIND of account this is. Their
# presence is the signal — a self-directed brokerage prints neither — which is
# what distinguishes a managed account from one the operator trades themselves.
_ADVISORY_PROGRAM = re.compile(
    r'your advisory program:\s*([a-z0-9 &/.\'-]{2,60})', re.IGNORECASE)
# 'Brokerage Cash Services number: 1234567890' — the bank's own statement of
# which checking account carries this brokerage account's cash. Its last four
# digits are that account's Plaid mask, which makes this the one authoritative
# pairing key a statement offers (see statements.autolink_cash_services).
_CASH_SERVICES = re.compile(
    r'brokerage cash services number:?\s*(\d{6,20})', re.IGNORECASE)
_FEE_RATE = re.compile(
    r'your effective fee rate:?\**\s*(\d{1,3}(?:\.\d{1,3})?\s*%)',
    re.IGNORECASE)


def _extract_advisory(lines) -> dict:
    """{'advisory_program', 'advisory_fee_rate', 'cash_services_number'}
    where the statement names them.

    Strings, deliberately: '1.00%' is a disclosed rate to reproduce verbatim on
    a report, not a number to compute with, and an account number is an
    identifier whose leading zeros matter."""
    out = {}
    for line in lines:
        if 'cash_services_number' not in out:
            m = _CASH_SERVICES.search(line)
            if m:
                out['cash_services_number'] = m.group(1)
        if 'advisory_program' not in out:
            m = _ADVISORY_PROGRAM.search(line)
            if m:
                out['advisory_program'] = m.group(1).strip().upper()
        if 'advisory_fee_rate' not in out:
            m = _FEE_RATE.search(line)
            if m:
                out['advisory_fee_rate'] = m.group(1).replace(' ', '')
    return out


def page_inventory(pages) -> list[dict]:
    """[{'page', 'lines', 'heading'}] — what is actually IN this PDF.

    Recorded because the summary block this parser reads is one page of nine on
    a real Wells Fargo Advisors statement, and the rest (holdings, transaction
    detail, realized-gain lots, fee schedules, dividend detail) is unmined. An
    inventory in the metadata is how a decision about mining any of it can be
    made from data rather than from opening fourteen PDFs by hand."""
    out = []
    for i, text in enumerate(pages, start=1):
        lines = [re.sub(r'\s+', ' ', l).strip()
                 for l in (text or '').splitlines() if l.strip()]
        if not lines:
            continue
        # The first line is the running header ('Page 2 of 9SNAPSHOT'); the
        # most useful label is whichever of the first few lines names a block.
        heading = next((l for l in lines[:4]
                        if 'summary' in l.lower() or 'activity' in l.lower()),
                       lines[0])
        out.append({'page': i, 'lines': len(lines), 'heading': heading[:80]})
    return out


def _derive(values: dict) -> None:
    """Figures the statement implies but doesn't print on a line of their own.

    Only sums of fields already recovered — nothing here reads the PDF again,
    so a derived value can never be more speculative than its inputs."""
    ordinary = values.get('ordinary_dividends')
    qualified = values.get('qualified_dividends')
    if ordinary is not None or qualified is not None:
        values['dividends_total'] = round((ordinary or 0.0)
                                          + (qualified or 0.0), 2)


def _blank_parse() -> dict:
    return {'opening': None, 'closing': None, 'portfolio_opening': None,
            'portfolio_closing': None, 'has_text': False, 'method': '',
            'metadata': {}}


def parse_statement(pdf_bytes: bytes) -> dict:
    """Everything one statement PDF asserts.

        {'opening', 'closing',                      # the reconcilable balances
         'portfolio_opening', 'portfolio_closing',  # total value, if stated
         'has_text', 'method', 'metadata'}

    `metadata` is the full blob persisted to `PlaidStatement.parsed_metadata`:
    every recovered field under its own key, plus

        parser_version  which build of this module produced it
        layout          which field table was used
        verified        whether that table has been checked against a real
                        document of this kind
        fields_failed   keys whose extraction raised (see _extract_fields)
        period_start / period_end   the period the STATEMENT states, ISO, when
                        it prints one — kept beside the bounds derived from
                        Plaid's month+year rather than replacing them
        pages           an inventory of what is in the document but unmined
                        (see page_inventory)

    Every balance is float-or-None and all-None is an ordinary result rather
    than a failure — see this module's docstring. Never raises."""
    pages = extract_pages(pdf_bytes)
    lines = []
    for page in pages:
        for raw in page.splitlines():
            line = re.sub(r'\s+', ' ', raw).strip().lower()
            if line:
                lines.append(line)
    if not lines:
        return _blank_parse()

    layout = detect_layout(lines)
    values, failed = _extract_fields(lines, _LAYOUT_FIELDS.get(layout, ()))
    if layout == 'wf_advisors':
        # Blocks the (key, section, labels, column) table cannot describe: a
        # 3×3 grid, a label printed twice with the sign carried by position,
        # and two non-numeric fields. Each in its own try/except, for the same
        # reason every other field is — see _extract_fields.
        for name, extractor in (('gainloss', _extract_gainloss),
                                ('electronic_transfers',
                                 _extract_electronic_transfers),
                                ('advisory', _extract_advisory)):
            try:
                values.update(extractor(lines))
            except Exception:
                log.warning('statement block %r failed to extract', name,
                            exc_info=True)
                failed.append(name)
    _derive(values)

    open_key, close_key = _HEADLINE_FIELDS.get(layout, ('', ''))
    opening = values.get(open_key)
    closing = values.get(close_key)

    if opening is None and closing is None:
        # No field table matched, or the one that did found no balance. Fall
        # back to the generic label search, then — only if THAT finds nothing —
        # to treating the document as a single long line, for a layout whose
        # label and amount pypdf did not place together. The gap rules apply
        # throughout, so each step relaxes WHERE a figure may sit, never how
        # convincingly it has to sit there.
        opening = _find_label_amount(lines, _OPENING_LABELS)
        closing = _find_label_amount(lines, _CLOSING_LABELS)
        layout = 'labels' if (opening is not None or closing is not None) \
            else layout
        if opening is None and closing is None:
            flat = ' '.join(lines)
            opening = _find_label_amount([flat], _OPENING_LABELS)
            closing = _find_label_amount([flat], _CLOSING_LABELS)
            layout = 'labels_flat' if (opening is not None
                                       or closing is not None) else ''
        if opening is not None:
            values.setdefault('opening_balance', opening)
        if closing is not None:
            values.setdefault('closing_balance', closing)

    start, end = parse_period(lines)
    due = parse_due_date(lines) if layout == 'wf_card' else None

    metadata = dict(values)
    metadata.update({
        'parser_version': PARSER_VERSION,
        'layout': layout,
        'verified': layout in _VERIFIED_LAYOUTS,
        'fields_failed': failed,
        'period_start': start.isoformat() if start else None,
        'period_end': end.isoformat() if end else None,
        'pages': page_inventory(pages),
    })
    if due is not None:
        metadata['payment_due_date'] = due.isoformat()
    if failed:
        log.warning('statement parse: %d field(s) failed on a %s layout: %s',
                    len(failed), layout or 'unknown', ', '.join(failed))

    return {'opening': opening, 'closing': closing,
            'portfolio_opening': values.get('portfolio_opening'),
            'portfolio_closing': values.get('portfolio_closing'),
            'has_text': True, 'method': layout, 'metadata': metadata}


def parse_balances(pdf_bytes: bytes) -> dict:
    """The two balances alone — `parse_statement` without the metadata.

    Kept because it is what every pre-v0.4.41 caller asks for and what the
    module's contract has always been: two numbers, either of which may be
    None."""
    return parse_statement(pdf_bytes)


# ── reading the metadata blob ───────────────────────────────────────────────

# Keys that are bookkeeping about the parse rather than figures the bank
# asserted. Separated so a UI can show "what the statement says" without
# 'parser_version' and 'fields_failed' sitting in the middle of the money.
_META_HOUSEKEEPING = frozenset({
    'parser_version', 'layout', 'verified', 'fields_failed',
    'period_start', 'period_end', 'payment_due_date', 'pages',
    'advisory_program', 'advisory_fee_rate', 'cash_services_number',
})

# How a gain/loss column reads in a table of figures.
_GAINLOSS_COLUMN_LABELS = {
    'unrealized': 'unrealized',
    'realized_period': 'realized, this period',
    'realized_ytd': 'realized, year to date',
}

# Presentation order and labels for the figures. A dict is unordered and
# alphabetical is meaningless here — 'Available funds' before 'Cash opening'
# tells a reader nothing — so the sequence a statement itself uses is
# reproduced: balances, then what moved, then income, then gains.
FIGURE_LABELS = (
    ('opening_balance', 'Opening balance'),
    ('closing_balance', 'Closing balance'),
    ('cash_opening', 'Cash & sweep — opening'),
    ('cash_closing', 'Cash & sweep — closing'),
    ('portfolio_opening', 'Total account value — opening'),
    ('portfolio_closing', 'Total account value — closing'),
    ('previous_balance', 'Previous balance'),
    ('new_balance', 'New balance'),
    ('available_funds', 'Available funds'),
    ('available_credit', 'Available credit'),
    ('credit_limit', 'Credit limit'),
    ('minimum_payment_due', 'Minimum payment due'),
    ('deposits_total', 'Deposits / cash in'),
    ('withdrawals_total', 'Withdrawals / cash out'),
    ('payments_total', 'Payments and credits'),
    ('purchases_total', 'Purchases'),
    ('cash_advances_total', 'Cash advances'),
    ('check_withdrawals', 'Withdrawals by check'),
    ('atm_activity', 'ATM and CheckCard activity'),
    ('other_additions', 'Other additions'),
    ('other_subtractions', 'Other subtractions and transfers'),
    ('net_additions', 'Net additions to cash'),
    ('net_subtractions', 'Net subtractions from cash'),
    ('securities_purchased', 'Securities purchased'),
    ('securities_sold', 'Securities sold and redeemed'),
    ('electronic_transfers_in', 'Electronic funds transfers in'),
    ('electronic_transfers_out', 'Electronic funds transfers out'),
    ('securities_deposited', 'Securities deposited'),
    ('securities_withdrawn', 'Securities withdrawn'),
    ('change_in_value', 'Change in market value'),
    ('income_distributions', 'Income and distributions'),
    ('interest_income', 'Interest'),
    ('interest_earned', 'Interest earned'),
    ('interest_charged', 'Interest charged'),
    ('sweep_income', 'Sweep / money-market income'),
    ('ordinary_dividends', 'Dividends — ordinary and short-term gains'),
    ('qualified_dividends', 'Dividends — qualified'),
    ('dividends_total', 'Dividends — total'),
    ('other_income', 'Other income'),
    ('total_taxable_income', 'Taxable income'),
    ('tax_exempt_income', 'Tax-exempt income'),
    ('total_income', 'Total income'),
    ('gross_proceeds', 'Gross proceeds'),
    ('foreign_withholding', 'Foreign withholding'),
    ('advisory_fees', 'Advisory, manager and platform fees'),
    ('fees_total', 'Fees'),
    ('service_fees', 'Service fees'),
    ('short_term_gainloss', 'Short term / net lots'),
    ('long_term_gainloss', 'Long term'),
    ('total_gainloss', 'Gain/loss — total'),
    ('average_ledger_balance', 'Average ledger balance'),
    ('average_collected_balance', 'Average collected balance'),
)

_FIGURE_LABEL = dict(FIGURE_LABELS)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _expand(key: str, label: str, value):
    """One metadata entry as [(key, label, amount)].

    A plain number is one row. A gain/loss dict is THREE — one per column —
    because a 3×3 table read as three opaque objects tells a reader nothing,
    and flattening it at storage time would have meant nine loose keys for
    what is one fact about one holding period."""
    if _is_number(value):
        return [(key, label, value)]
    if isinstance(value, dict):
        return [(f'{key}.{col}',
                 f'{label} — {_GAINLOSS_COLUMN_LABELS.get(col, col)}',
                 value[col])
                for col in _GAINLOSS_COLUMNS if _is_number(value.get(col))]
    return []


def metadata_figures(metadata: dict) -> list[tuple[str, str, float]]:
    """[(key, label, value)] for the money in one metadata blob, in the order
    FIGURE_LABELS declares. Unknown keys sort last under a humanised name
    rather than being dropped — a field added by a newer parser must still be
    visible on a page rendered by an older template."""
    metadata = metadata or {}
    known, seen = [], set()
    for key, label in FIGURE_LABELS:
        rows = _expand(key, label, metadata.get(key))
        if rows:
            known.extend(rows)
            seen.add(key)
    extra = []
    for key, value in sorted(metadata.items()):
        if key in seen or key in _META_HOUSEKEEPING:
            continue
        extra.extend(_expand(key, key.replace('_', ' ').capitalize(), value))
    return known + extra


# ── statement-anchored reconciliation (v0.4.43) ─────────────────────────────
#
# The durable, ERPNext-independent record of what each account actually held at
# each statement boundary. See StatementAnchor's docstring for why this is a
# table rather than a journal entry — in short, the accounts it matters most
# for belong to books that do not exist yet, and posting corrections into the
# wrong instance today is work to reverse tomorrow.
#
# Nothing here writes to ERPNext. That is a property of the release, not an
# oversight.

# How far a supersede chain is walked before we assume something is wrong.
# A real chain is one or two links (a bank re-link, occasionally two); ten is
# far past any legitimate history and exists so a cycle introduced by bad data
# cannot hang a page render.
_MAX_SUPERSEDE_DEPTH = 10


def supersede_chain(account_id: str) -> list:
    """Every account id that is the SAME REAL ACCOUNT as `account_id`.

    THE BUG THIS EXISTS FOR. When a bank is re-linked, Plaid mints a new
    account_id for the same physical account, and Bank Bridge records the
    relationship with `superseded_by_account_id`. What it does NOT do is move
    the transactions: the pre-relink history stays on the old row and
    everything after lands on the new one. So on the live install, ••3158 has
    two rows — one holding 2025-06 → 2026-03, the other 2026-04 onward — and
    any query filtering on a single account_id sees roughly half the history
    and silently reports the rest as missing money.

    Which of the two rows hygiene marked 'active' is arbitrary, so this walks
    the chain in BOTH directions — successors via `superseded_by_account_id`,
    predecessors via the rows pointing AT this one — and transitively, since a
    twice-relinked account is three rows deep. That makes the active/superseded
    designation cosmetic for reconciliation purposes: pair to either row and
    the same transactions are found.

    Breadth-first with a visited set, so a cycle in the data (which should be
    impossible and therefore will eventually happen) terminates instead of
    looping. Returns the ids including the one asked for."""
    account_id = (account_id or '').strip()
    if not account_id:
        return []
    seen = {account_id}
    frontier = [account_id]
    for _ in range(_MAX_SUPERSEDE_DEPTH):
        if not frontier:
            break
        # Successors: rows these point at. Predecessors: rows pointing at them.
        successors = {
            (a.superseded_by_account_id or '').strip()
            for a in PlaidAccount.query
            .filter(PlaidAccount.account_id.in_(tuple(frontier))).all()
            if (a.superseded_by_account_id or '').strip()}
        predecessors = {
            a.account_id for a in PlaidAccount.query
            .filter(PlaidAccount.superseded_by_account_id.in_(
                tuple(frontier))).all()}
        frontier = sorted((successors | predecessors) - seen)
        seen.update(frontier)
    if len(seen) > 1:
        log.debug('account %s resolves to a supersede chain of %d rows',
                  account_id, len(seen))
    return sorted(seen)


def _fingerprint(txn) -> tuple:
    """(date, amount, normalised name) — what makes two rows THE SAME REAL
    EVENT even though Plaid gave them different ids.

    Name is stripped and lowercased because the two sides of a re-link are two
    separate Plaid ingestions of the same feed and their capitalisation is not
    guaranteed to agree; a missing name folds to ''. Amount and date are used
    as-is: a cent or a day of difference means two events."""
    return (txn.date, round(float(txn.amount or 0.0), 2),
            (getattr(txn, 'name', '') or '').strip().lower())


def dedupe_across_accounts(rows) -> list:
    """Drop rows that a re-link caused to be recorded twice.

    THE BUG THIS EXISTS FOR. `supersede_chain` (v0.4.45) fixed history being
    SPLIT across two rows of a re-linked account. It exposed the opposite
    problem in the overlap: for the months on both sides of the re-link, Plaid
    ingested the same purchases into BOTH account_ids with DIFFERENT
    `plaid_transaction_id`s. On the live install every May 2026 purchase on
    ••3158 appears twice that way, and the anchor summed −$33,778 where the
    bank saw −$16,763. Id-based dedupe cannot catch it; the ids genuinely
    differ. What matches exactly is (date, amount, name).

    ONLY ACROSS ACCOUNTS, NEVER WITHIN ONE. Two identical charges on the same
    day from the same merchant on the SAME account are two real purchases —
    buying the same coffee twice is not a data error — so within a single
    account_id everything is kept. What is removed is the mirror image of a set
    on another account_id in the chain.

    Implemented as: for each fingerprint, keep the rows belonging to whichever
    single account_id holds the MOST copies of it. That preserves a genuine
    intra-account repeat (the account with two keeps both) while collapsing the
    cross-account duplicate (two accounts with one each keep one). Ties break
    on account_id so the result is stable rather than dependent on row order."""
    by_fingerprint: dict = {}
    for row in rows:
        by_fingerprint.setdefault(_fingerprint(row), {}).setdefault(
            row.account_id, []).append(row)
    kept = []
    for per_account in by_fingerprint.values():
        if len(per_account) == 1:
            kept.extend(next(iter(per_account.values())))
            continue
        winner = max(sorted(per_account), key=lambda a: len(per_account[a]))
        kept.extend(per_account[winner])
    return kept


# A PAIRED BROKERAGE ACCOUNT'S SECURITY TRANSACTIONS ARE NOT COUNTED AT ALL.
#
# The companion is a "Brokerage Cash Services" account, which is a CASH SWEEP
# ledger by construction: every event in the brokerage — a buy, a sell, an
# advisory fee, a dividend — sweeps cash to or from it, and the companion
# records that sweep as an 'Increase/Decrease from Brokerage activity' bank
# line. So the companion's BankTransactions already ARE the account's complete
# cash story. Adding the brokerage's SecurityTransactions on top double-counts
# every one of them.
#
# v0.4.47 tried to keep income (dividends, fees) and drop only trades, on the
# theory that the companion doesn't carry income. Verifying v0.4.48 against
# 26 months of production data disproved that theory outright:
#
#     ••9401, 26 periods, total variance
#       include all security txns (pre-v0.4.47) : whatever
#       keep income, drop trades (v0.4.47)      : −30,225.86
#       NEGATE security amounts (proposed)      : +30,225.86   (sign flip only)
#       EXCLUDE all security txns               :       0.00
#
# The proposed sign fix only flipped the error's sign — the fees were being
# counted, not mis-signed. Excluding them entirely, because the sweep on the
# companion is their real cash leg, is what reconciles. On ••6030 the three
# policies are identical (its kept-security sum was already ~0), leaving a
# genuine $152.74 residual to tag rather than a double-count to remove.
#
# UNPAIRED brokerage accounts are untouched: with no companion holding the
# sweep, their SecurityTransactions are the only cash record there is, so they
# are all kept exactly as before. The exclusion is a property of PAIRING.


def _bank_total(account_ids, start, end) -> float:
    """Σ Plaid amounts for settled BankTransactions across a set of account
    ids, in Plaid's own convention (positive = money out).

    Deduplicated across the chain (see dedupe_across_accounts) — a re-link
    leaves the same real purchase recorded on both account_ids."""
    if not account_ids or start is None or end is None:
        return 0.0
    rows = (BankTransaction.query
            .filter(BankTransaction.account_id.in_(tuple(account_ids)),
                    BankTransaction.date >= start,
                    BankTransaction.date <= end,
                    BankTransaction.pending.is_(False),
                    BankTransaction.removed.is_(False))
            .all())
    if len(account_ids) > 1:
        rows = dedupe_across_accounts(rows)
    return sum(float(t.amount or 0.0) for t in rows)


def anchor_transaction_sum(account: PlaidAccount, start: date | None,
                           end: date | None) -> float:
    """Everything Plaid mirrored in [start, end], normalised so CASH IN IS
    POSITIVE — the convention that makes `opening + sum = closing` read the way
    a bank statement does.

    `signed_movement` answers the same question in PLAID's convention, where a
    positive amount means money LEFT the account, so this is its negation. The
    flip lives here, once, rather than at each call site: an anchor chain whose
    sign convention is decided per-caller is a chain that will eventually
    disagree with itself.

    v0.4.44 · A PAIRED ACCOUNT'S CASH SIDE IS INCLUDED. Wells Fargo Advisors
    splits one economic account across two Plaid accounts: the brokerage side
    holds the statements and the securities activity but ZERO
    BankTransactions, while its "Brokerage Cash Services" companion holds every
    cash movement and no statements at all. Anchoring the brokerage account
    alone therefore compares the bank's closing balance against a transaction
    feed that is structurally empty, and reports the entire month as
    unexplained — a variance that says nothing about the books.

    Only the companion's BANK transactions are added. Its SecurityTransactions
    are not, because `signed_movement` already sums those for the brokerage
    account itself and counting them twice would replace one wrong answer with
    another.

    v0.4.45 · BOTH SIDES WALK THEIR SUPERSEDE CHAIN. A re-linked account has
    its history split across two rows and neither one alone is the account —
    see `supersede_chain`. Symmetric on purpose: the brokerage side can have
    been re-linked just as easily as the cash side, and fixing only the
    companion would leave the same bug on the other half of the identity."""
    total = -signed_movement(account, start, end)
    partner_id = (account.paired_account_id or '').strip()
    if partner_id and start is not None and end is not None:
        # The partner's whole chain, minus anything already counted as part of
        # this account's own identity — a pairing that happens to point into
        # the same chain must not double-count it.
        own = set(supersede_chain(account.account_id))
        partner_ids = [aid for aid in supersede_chain(partner_id)
                       if aid not in own]
        total += -_bank_total(partner_ids, start, end)
    return round(total, 2)


# ── pairing a brokerage account with its cash-services companion ────────────

def _mask_of(number: str) -> str:
    """The last four digits of a cash-services account number — which is
    exactly what Plaid reports as the companion account's `mask`."""
    digits = re.sub(r'\D', '', number or '')
    return digits[-4:] if len(digits) >= 4 else ''


def cash_services_numbers(account_id: str) -> list:
    """Every distinct cash-services number this account's statements printed.

    A list rather than one value because disagreement is meaningful: if the
    statements name two different companions, the account was re-numbered
    mid-history and guessing which one is current would be worse than
    declining to pair."""
    seen = []
    for st in (PlaidStatement.query
               .filter(PlaidStatement.plaid_account_id == account_id,
                       PlaidStatement.cash_services_account_number.isnot(None))
               .order_by(PlaidStatement.period_start.desc().nullslast())
               .all()):
        number = (st.cash_services_account_number or '').strip()
        if number and number not in seen:
            seen.append(number)
    return seen


def _same_company(a: PlaidAccount, b: PlaidAccount, items: dict) -> bool:
    """Whether two accounts resolve to the same owning Company (per-account
    override → Item). Pairing across entities is never right — it would fold
    one company's cash into another's reconciliation."""
    def owner(acct):
        return ((acct.owning_company or '').strip()
                or items.get(acct.item_id, ''))
    return owner(a) == owner(b)


def pair_candidates(account: PlaidAccount, accounts: list = None,
                    items: dict = None) -> list:
    """The accounts `account` may legitimately be paired with.

    v0.4.45 · SCOPED TO THE PLAID ITEM. This used to be "any depository under
    the same Company", which is far too wide: two brokerage accounts under one
    entity have two different companions, and offering all four made it
    possible — easy, even — to pair an account to the wrong one. A cash-services
    companion is by construction part of the same Plaid Link connection as the
    brokerage account it serves, so the Item IS the correct scope. Company is
    kept as a second constraint rather than replaced, because folding one
    entity's cash into another's reconciliation is never right even within one
    connection.

    Four rules, each cutting out a way to be wrong:

      * SAME ITEM — companions always arrive together
      * SAME COMPANY — never fold one entity's cash into another's
      * COMPATIBLE TYPE — only a depository account can be a brokerage
        account's cash side; two brokerages are not companions
      * NOT SUPERSEDED — a retired row is half an account (see
        supersede_chain), and pairing to one is a trap the UI should not set.
        Note the chain walk means pairing to a superseded row still WORKS; this
        is about not inviting it.
      * NOT ALREADY SOMEONE ELSE'S CASH SIDE (v0.4.46) — a cash-services
        account serves exactly one brokerage account. Once ••3158 is paired to
        ••6030 it is not a candidate for ••9401, and offering it there invites
        an operator to move one account's entire transaction history into
        another's reconciliation with a single click. The CURRENT selection is
        always still offered, so opening the dropdown on an already-paired
        account shows what it is paired to rather than a blank.

    Returns [] for a non-investment account: pairing is a property of the
    brokerage side, which is where `paired_account_id` lives."""
    if (account.type or '').lower() not in ('investment', 'brokerage'):
        return []
    if accounts is None:
        accounts = PlaidAccount.query.order_by(PlaidAccount.name).all()
    if items is None:
        items = {it.item_id: (it.owning_company or '').strip()
                 for it in PlaidItem.query.all()}
    claimed = {(a.paired_account_id or '').strip() for a in accounts
               if a.account_id != account.account_id
               and (a.paired_account_id or '').strip()}
    return [c for c in accounts
            if c.account_id != account.account_id
            and c.item_id == account.item_id
            and (c.type or '').lower() == 'depository'
            and not (c.superseded_by_account_id or '').strip()
            and (c.account_id not in claimed
                 or c.account_id == (account.paired_account_id or '').strip())
            and _same_company(account, c, items)]


# Retained name for the auto-linker, which wants exactly the same rule — the
# UI and the detector disagreeing about what a valid pairing is would be a bug
# in itself.
_candidate_partners = pair_candidates


def autolink_cash_services() -> dict:
    """Pair each brokerage account with its cash-services companion.

    Two strategies, strongest first:

      1. THE STATEMENT SAYS SO. A Wells Fargo Advisors statement prints
         'Brokerage Cash Services number: 1234567890', whose last four digits
         are the companion's Plaid mask. This is the bank's own assertion of
         the relationship and needs no inference.
      2. THE NAMES SAY SO. Failing that, an account named '… BROKERAGE …6030'
         alongside exactly one '… BROKERAGE CASH …' under the same Company is
         the same relationship spelled differently. Weaker, so it only runs
         when the statement is silent.

    A pairing is only made when EXACTLY ONE candidate matches. Two candidates
    is ambiguity, and pairing the wrong cash account would silently move
    another account's transactions into this one's reconciliation — much worse
    than leaving it unpaired and visibly unexplained.

    NEVER overwrites an existing pairing: a value already set was either put
    there by an operator who can see things the PDF does not say, or by a
    previous run that reached the same conclusion. Returns
    {'paired', 'already', 'ambiguous', 'unmatched'}."""
    stats = {'paired': 0, 'already': 0, 'ambiguous': 0, 'unmatched': 0}
    accounts = PlaidAccount.query.all()
    items = {it.item_id: (it.owning_company or '').strip()
             for it in PlaidItem.query.all()}
    for account in accounts:
        if (account.type or '').lower() != 'investment':
            continue
        if (account.paired_account_id or '').strip():
            stats['already'] += 1
            continue
        candidates = _candidate_partners(account, accounts, items)
        if not candidates:
            stats['unmatched'] += 1
            continue

        partner = None
        for number in cash_services_numbers(account.account_id):
            mask = _mask_of(number)
            if not mask:
                continue
            matches = [c for c in candidates if (c.mask or '') == mask]
            if len(matches) == 1:
                partner = matches[0]
                break
            if len(matches) > 1:
                log.info('account %s names cash-services mask %s, but %d '
                         'accounts share it — declining to pair',
                         account.account_id, mask, len(matches))
                stats['ambiguous'] += 1
                partner = None
                break

        if partner is None and not cash_services_numbers(account.account_id):
            partner = _partner_by_name(account, candidates)
            if partner is False:                      # ambiguous by name
                stats['ambiguous'] += 1
                continue

        if not partner:
            stats['unmatched'] += 1
            continue
        account.paired_account_id = partner.account_id
        stats['paired'] += 1
        log.info('paired brokerage %s (••%s) with cash-services %s (••%s)',
                 account.account_id, account.mask or '????',
                 partner.account_id, partner.mask or '????')
    if stats['paired']:
        db.session.commit()
    return stats


_BROKERAGE_CASH = re.compile(r'brokerage\s+cash', re.IGNORECASE)
_BROKERAGE = re.compile(r'brokerage', re.IGNORECASE)


def _partner_by_name(account: PlaidAccount, candidates: list):
    """The name-pattern fallback: exactly one '… BROKERAGE CASH …' companion,
    or None (no match) / False (ambiguous).

    Only consulted when no statement named a cash-services number, because a
    name is a convention and the statement is a fact."""
    if not _BROKERAGE.search(account.name or ''):
        return None
    named = [c for c in candidates
             if _BROKERAGE_CASH.search(f'{c.name or ""} {c.official_name or ""}')]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        log.info('account %s has %d "brokerage cash" candidates — declining '
                 'to pair by name', account.account_id, len(named))
        return False
    return None


def _anchor_values(statement: PlaidStatement, account: PlaidAccount) -> dict:
    """The arithmetic for one statement, before it is compared to its
    predecessor."""
    metadata = statement.metadata_dict()
    # The CASH side, not total account value. On a brokerage statement those
    # are different numbers and only cash can be reconciled against a
    # transaction feed — the mirror has no record of market movement. Falls
    # back to the promoted columns for a row parsed before v0.4.41 named them.
    opening = metadata.get('cash_opening')
    if opening is None:
        opening = statement.opening_balance
    closing = metadata.get('cash_closing')
    if closing is None:
        closing = statement.closing_balance
    txn_sum = anchor_transaction_sum(account, statement.period_start,
                                     statement.period_end)
    computed = (round(float(opening) + txn_sum, 2)
                if opening is not None else None)
    variance = (round(float(closing) - computed, 2)
                if closing is not None and computed is not None else None)
    return {'anchored_opening': opening, 'anchored_closing': closing,
            'transaction_sum': txn_sum, 'computed_closing': computed,
            'variance': variance,
            'parser_version': statement.parser_version()}


def rebuild_statement_anchors(account_id: str | None = None) -> dict:
    """Build (or refresh) the anchor chain for one account, or every account.

    Idempotent by construction: `StatementAnchor.statement_id` is unique, so a
    re-run updates the existing row rather than appending a second version of
    the same period's truth. That is what makes it safe to call after every
    parser upgrade — and why it IS called there (see reparse_stale), because an
    anchor built from figures a later recognizer corrected is exactly as stale
    as the figures were.

    Statements with no readable opening AND no readable closing are skipped
    rather than anchored to nulls: an anchor that asserts nothing is worse than
    an absent one, because it makes the chain look continuous where it isn't.

    Company-agnostic on purpose — see StatementAnchor. Never raises; returns
    {'accounts', 'written', 'skipped', 'gaps', 'variances'}."""
    from .models import StatementAnchor
    stats = {'accounts': 0, 'written': 0, 'skipped': 0, 'gaps': 0,
             'variances': 0}
    accounts = ([PlaidAccount.query.filter_by(account_id=account_id).first()]
                if account_id else PlaidAccount.query.all())
    for account in [a for a in accounts if a is not None]:
        rows = (PlaidStatement.query
                .filter(PlaidStatement.plaid_account_id == account.account_id,
                        PlaidStatement.period_start.isnot(None))
                .order_by(PlaidStatement.period_start.asc(),
                          PlaidStatement.id.asc())
                .all())
        if not rows:
            continue
        stats['accounts'] += 1
        prior_closing = None
        for st in rows:
            values = _anchor_values(st, account)
            if (values['anchored_opening'] is None
                    and values['anchored_closing'] is None):
                stats['skipped'] += 1
                continue
            anchor = (StatementAnchor.query
                      .filter_by(statement_id=st.id).first())
            if anchor is None:
                anchor = StatementAnchor(statement_id=st.id)
                db.session.add(anchor)
            anchor.account_id = account.account_id
            anchor.period_start = st.period_start
            anchor.period_end = st.period_end
            for key, value in values.items():
                setattr(anchor, key, value)
            # The chain test. Compared against the PREVIOUS ANCHORED period
            # rather than the previous calendar month, because a missing month
            # is precisely what this is meant to catch — if July's opening
            # doesn't meet June's closing, the statement in between was never
            # fetched, and every variance after it is measured from the wrong
            # baseline.
            gap = False
            if prior_closing is not None and values['anchored_opening'] is not None:
                gap = abs(float(values['anchored_opening'])
                          - float(prior_closing)) > 0.005
            anchor.chain_gap_from_prior = gap
            anchor.updated_at = _now()
            stats['written'] += 1
            if gap:
                stats['gaps'] += 1
            if values['variance'] is not None and abs(values['variance']) > 0.005:
                stats['variances'] += 1
            if values['anchored_closing'] is not None:
                prior_closing = values['anchored_closing']
    db.session.commit()
    return stats


def anchors_for_account(account_id: str) -> list:
    """One account's anchor chain, oldest period first — the order the chain
    has to be read in for a gap to mean anything."""
    from .models import StatementAnchor
    return (StatementAnchor.query
            .filter(StatementAnchor.account_id == account_id)
            .order_by(StatementAnchor.period_start.asc().nullslast(),
                      StatementAnchor.id.asc())
            .all())


def anchor_variance_total(account_id: str, year: int | None = None) -> float:
    """Σ variance for one account, optionally restricted to a calendar year.

    THE HEADLINE NUMBER: how much money the bank saw over the period that Plaid
    never reported. Summed rather than averaged because the question it answers
    — 'is this account's Plaid data telling the whole story?' — is about total
    unexplained movement, and a +$5,000 month cancelling a -$5,000 month is
    still two events nobody has accounted for. (Which is why the UI shows the
    count alongside it.)"""
    total = 0.0
    for anchor in anchors_for_account(account_id):
        if anchor.variance is None:
            continue
        if year is not None and (anchor.period_end is None
                                 or anchor.period_end.year != year):
            continue
        total += float(anchor.variance)
    return round(total, 2)


def anchor_summary(account_id: str, year: int | None = None) -> dict:
    """{'variance', 'unexplained', 'gaps', 'periods', 'year'} — the one-line
    verdict for an account, as /admin/accounts shows it."""
    anchors = anchors_for_account(account_id)
    if year is not None:
        anchors = [a for a in anchors
                   if a.period_end is not None and a.period_end.year == year]
    return {
        'variance': round(sum(float(a.variance or 0.0) for a in anchors), 2),
        'unexplained': sum(1 for a in anchors if not a.reconciles()),
        'gaps': sum(1 for a in anchors if a.chain_gap_from_prior),
        'periods': len(anchors),
        'year': year,
    }


def accounts_with_anchors() -> list:
    """Every account that actually HAS an anchor chain, by name.

    v0.4.47 · keyed on StatementAnchor rather than on PlaidStatement. The
    difference matters because of pairing: a Wells Fargo Advisors setup has
    four Plaid accounts but only TWO reconciliations. The brokerage accounts
    hold the statements and therefore the anchors; their cash-services
    companions hold neither — every transaction, but no statement to measure it
    against. Offering all four in a picker presents four choices for two
    answers, and two of them open an empty page.

    Also excludes a brokerage account whose statements have not been anchored
    yet, which is the honest thing to show: the page's empty state tells you to
    rebuild, and listing an account with nothing behind it does not.

    Deliberately NOT filtered by ERPNext Company: the accounts this feature
    exists for are the ones whose books do not exist yet."""
    from .models import StatementAnchor
    ids = {a.account_id for a in
           StatementAnchor.query.with_entities(
               StatementAnchor.account_id).distinct()}
    ids.discard(None)
    if not ids:
        return []
    return (PlaidAccount.query
            .filter(PlaidAccount.account_id.in_(tuple(ids)))
            .order_by(PlaidAccount.name).all())


def brokerage_for_partner(account_id: str) -> PlaidAccount | None:
    """The brokerage account whose cash side is `account_id`, or None.

    The inverse of `paired_account_id`, and what lets a link or a bookmark
    pointing at the cash-services account land somewhere useful: that account
    has no reconciliation of its own, but it is half of one."""
    account_id = (account_id or '').strip()
    if not account_id:
        return None
    return (PlaidAccount.query
            .filter(PlaidAccount.paired_account_id == account_id)
            .order_by(PlaidAccount.name).first())


def account_label(account: PlaidAccount, partner: PlaidAccount = None) -> str:
    """'BUSINESS BROKERAGE ••6030 ⇄ ••3158' — one economic account, named as
    such.

    The pair is shown at pick-time because it changes what the numbers MEAN:
    this reconciliation aggregates both Plaid accounts, and a reader who
    doesn't know that will wonder why a brokerage account with zero
    BankTransactions has a transaction sum."""
    name = account.name or account.official_name or account.account_id
    if account.mask:
        name = f'{name} ••{account.mask}'
    partner_id = (account.paired_account_id or '').strip()
    if partner_id:
        if partner is None:
            partner = PlaidAccount.query.filter_by(
                account_id=partner_id).first()
        if partner is not None:
            name = f'{name} ⇄ ••{partner.mask or "????"}'
    return name


# ── validating a statement against everything else we know ──────────────────
#
# WHY THIS EXISTS. Bank Bridge holds three independent accounts of the same
# month, and until now nothing put them next to each other:
#
#   STATEMENT  what the institution wrote down and mailed. Authoritative, but
#              recovered by regex from a PDF, so it can be MISREAD.
#   PLAID      what the API reports for the account right now. Live, but it is
#              a current snapshot, so it only speaks to the newest period.
#   MIRROR     what Bank Bridge computes from the transactions it stored.
#              Complete arithmetic over a feed that may itself have gaps.
#
# Any two agreeing is ordinary. Any two DISAGREEING is the finding: a misparse,
# a gap in the transaction feed, or something that moved the real balance and
# never reached Plaid. Each has a different fix, and none of them is visible
# from one column alone.
#
# Nothing here writes anything or gates anything. It is a report — its whole
# job is to put the three numbers in one row so the disagreement is impossible
# to miss.

def variance_thresholds() -> tuple[float, float]:
    """(absolute dollars, fractional) beyond which a difference is flagged.
    A row must exceed BOTH to flag, so neither a rounding cent on a large
    balance nor a fixed percentage of a tiny one raises noise."""
    try:
        dollars = abs(float(current_app.config.get(
            'STATEMENTS_VARIANCE_DOLLARS', 1.00)))
    except (TypeError, ValueError):
        dollars = 1.00
    try:
        pct = abs(float(current_app.config.get(
            'STATEMENTS_VARIANCE_PCT', 0.001)))
    except (TypeError, ValueError):
        pct = 0.001
    return dollars, pct


def _flagged(a, b) -> tuple[float | None, float | None, bool]:
    """(delta, fraction, flagged) for one pair, or (None, None, False) when
    either side is absent. `fraction` is relative to the larger magnitude, so
    the answer does not change depending on which column is called the
    baseline."""
    if a is None or b is None:
        return None, None, False
    delta = round(float(a) - float(b), 2)
    scale = max(abs(float(a)), abs(float(b)))
    fraction = (abs(delta) / scale) if scale else 0.0
    dollars, pct = variance_thresholds()
    return delta, fraction, (abs(delta) > dollars and fraction > pct)


def _movement_totals(account, start, end) -> dict:
    """What the transaction mirror says moved in [start, end], normalised so
    that CASH IN IS POSITIVE.

    Plaid's own convention is the opposite — its `amount` is positive when
    money LEAVES the account — and a statement prints deposits positive and
    withdrawals negative. Flipping once, here, is what lets a validation row
    subtract one column from the other and have the answer mean something."""
    out = {'deposits_total': 0.0, 'withdrawals_total': 0.0,
           'dividends_total': 0.0, 'interest_income': 0.0, 'advisory_fees': 0.0,
           'securities_purchased': 0.0, 'securities_sold': 0.0}
    if account is None or start is None or end is None:
        return out
    rows = (BankTransaction.query
            .filter(BankTransaction.account_id == account.account_id,
                    BankTransaction.date >= start,
                    BankTransaction.date <= end,
                    BankTransaction.pending.is_(False),
                    BankTransaction.removed.is_(False))
            .all())
    for t in rows:
        cash = -float(t.amount or 0.0)
        if cash >= 0:
            out['deposits_total'] += cash
        else:
            out['withdrawals_total'] += cash

    if (account.type or '').lower() in ('investment', 'brokerage'):
        from .models import SecurityTransaction
        sec = (SecurityTransaction.query
               .filter(SecurityTransaction.account_id == account.account_id,
                       SecurityTransaction.date >= start,
                       SecurityTransaction.date <= end)
               .all())
        for t in sec:
            cash = -float(t.amount or 0.0)
            kind = (t.type or '').lower()
            sub = (t.subtype or '').lower()
            if 'dividend' in sub:
                out['dividends_total'] += cash
            elif 'interest' in sub:
                out['interest_income'] += cash
            elif kind == 'fee' or 'fee' in sub:
                out['advisory_fees'] += cash
            elif kind == 'buy':
                out['securities_purchased'] += cash
            elif kind == 'sell':
                out['securities_sold'] += cash
    return {k: round(v, 2) for k, v in out.items()}


# Which statement figures the mirror can be asked the same question about.
# Both sides are already cash-in-positive — Wells Fargo prints withdrawals and
# purchases negative, which is the convention _movement_totals normalises to —
# so these compare directly with no per-field sign handling.
_COMPARABLE = (
    'deposits_total', 'withdrawals_total', 'dividends_total',
    'interest_income', 'advisory_fees',
    'securities_purchased', 'securities_sold',
)


def validate_statement(statement: PlaidStatement,
                       account: PlaidAccount | None = None) -> dict:
    """Every figure this statement asserts, beside the other accounts of the
    same month.

    Returns {'rows', 'flagged', 'thresholds', 'layout', 'verified',
    'fields_failed', 'period_matches'} where each row is

        {'key', 'label', 'statement', 'plaid', 'computed',
         'delta', 'pct', 'flagged', 'note'}

    `plaid` is populated only where Plaid actually reports something
    comparable, which in practice means the CURRENT balance on the most recent
    statement — Plaid's balance is a snapshot of today, and lining it up
    against a period that closed four months ago would manufacture a variance
    that means nothing. Every other cell is an honest None.

    Never raises: this is a report, and a report that 500s on a surprising row
    is worse than one that omits it."""
    account = account or (PlaidAccount.query.filter_by(
        account_id=statement.plaid_account_id).first()
        if statement.plaid_account_id else None)
    metadata = statement.parsed_metadata or {}
    dollars, pct = variance_thresholds()
    out = {'rows': [], 'flagged': 0, 'thresholds': (dollars, pct),
           'layout': metadata.get('layout') or (statement.parse_method or ''),
           'verified': bool(metadata.get('verified')),
           'fields_failed': list(metadata.get('fields_failed') or []),
           'advisory_program': metadata.get('advisory_program') or '',
           'advisory_fee_rate': metadata.get('advisory_fee_rate') or '',
           'period_matches': None}

    # Does the period the STATEMENT prints agree with the one derived from
    # Plaid's month + year? A bank whose cycle isn't a calendar month makes
    # every reconciliation in that period suspect, and this is the only place
    # that discrepancy is visible.
    stated_start = metadata.get('period_start')
    stated_end = metadata.get('period_end')
    if stated_start and stated_end and statement.period_start \
            and statement.period_end:
        out['period_matches'] = (
            stated_start == statement.period_start.isoformat()
            and stated_end == statement.period_end.isoformat())
    out['stated_period'] = (stated_start, stated_end)

    from . import computed_balances as cb
    try:
        computed_open, computed_close = cb.opening_and_closing_for_period(
            account, statement.period_start, statement.period_end)
    except Exception:  # pragma: no cover - computed_balances is already total
        log.warning('computed balances failed for statement %s',
                    statement.statement_id, exc_info=True)
        computed_open = computed_close = None

    # Plaid's live balance speaks only to the newest period — see the docstring.
    plaid_close = None
    if account is not None and _is_latest_statement(statement):
        plaid_close = account.balance_current

    def _row(key, label, stmt_value, plaid_value, computed_value, note=''):
        # Prefer statement-vs-mirror for the headline delta (both describe the
        # same closed period); fall back to statement-vs-Plaid when the mirror
        # has nothing to say.
        delta, fraction, flag = _flagged(stmt_value, computed_value)
        if delta is None:
            delta, fraction, flag = _flagged(stmt_value, plaid_value)
        if flag:
            out['flagged'] += 1
        out['rows'].append({
            'key': key, 'label': label, 'statement': stmt_value,
            'plaid': plaid_value, 'computed': computed_value,
            'delta': delta, 'pct': fraction, 'flagged': flag, 'note': note})

    _row('opening_balance', 'Opening balance', statement.opening_balance,
         None, computed_open)
    _row('closing_balance', 'Closing balance', statement.closing_balance,
         plaid_close, computed_close,
         'Plaid reports a live balance, compared here only on the newest '
         'statement' if plaid_close is not None else '')

    if statement.portfolio_closing_value is not None:
        _row('portfolio_closing', 'Total account value — closing',
             statement.portfolio_closing_value,
             plaid_close if (account is not None
                             and (account.type or '').lower() == 'investment')
             else None, None,
             'Cash plus securities at market. The mirror cannot reproduce it — '
             'it has no record of market movement — so there is no computed '
             'column to compare.')

    movement = _movement_totals(account, statement.period_start,
                                statement.period_end)
    for key in _COMPARABLE:
        stmt_value = metadata.get(key)
        if stmt_value is None:
            continue
        _row(key, _FIGURE_LABEL.get(key, key), stmt_value, None,
             movement.get(key))

    # Everything else the statement stated, with no counterpart to check it
    # against. Shown anyway: an unmatched figure is still the bank's own
    # assertion, and it is what a journal entry cites.
    compared = {r['key'] for r in out['rows']}
    for key, label, value in metadata_figures(metadata):
        if key in compared:
            continue
        _row(key, label, value, None, None)
    return out


def _is_latest_statement(statement: PlaidStatement) -> bool:
    """Whether this is the newest period held for its account — the only one
    Plaid's current balance can fairly be compared against."""
    if not statement.period_end:
        return False
    newer = (PlaidStatement.query
             .filter(PlaidStatement.plaid_account_id == statement.plaid_account_id,
                     PlaidStatement.period_end > statement.period_end)
             .first())
    return newer is None


# ── persisting a parse ──────────────────────────────────────────────────────

def apply_parse(record: PlaidStatement, parsed: dict) -> dict:
    """Copy one `parse_statement` result onto a row (no commit). Returns the
    parse, so a caller can go on inspecting it.

    `parsed_metadata` is the whole blob; the scalar columns beside it are
    PROMOTIONS of fields inside it, not independent values. Keeping them is
    what lets SQL ask "which months don't reconcile" without unpacking JSON on
    every row, and deriving them here — in one place — is what stops the two
    representations from ever disagreeing."""
    record.opening_balance = parsed['opening']
    record.closing_balance = parsed['closing']
    record.portfolio_opening_value = parsed.get('portfolio_opening')
    record.portfolio_closing_value = parsed.get('portfolio_closing')
    record.parse_method = parsed.get('method') or ''
    metadata = parsed.get('metadata') or {}
    record.parsed_metadata = metadata
    record.cash_services_account_number = metadata.get('cash_services_number')
    record.updated_at = _now()
    return parsed


def previous_statement(statement: PlaidStatement) -> PlaidStatement | None:
    """The statement covering the month immediately before this one, for the
    same account, or None.

    IMMEDIATELY before is the point — a statement two months back says nothing
    about whether this one's opening balance is right, because a whole month of
    unseen movement sits between them."""
    if not statement.period_start:
        return None
    prior = (PlaidStatement.query
             .filter(PlaidStatement.plaid_account_id == statement.plaid_account_id,
                     PlaidStatement.id != statement.id,
                     PlaidStatement.period_end.isnot(None),
                     PlaidStatement.period_end < statement.period_start)
             .order_by(PlaidStatement.period_end.desc())
             .first())
    if prior is None or prior.period_end is None:
        return None
    gap = (statement.period_start - prior.period_end).days
    return prior if gap == 1 else None


def flag_parse_continuity(statement: PlaidStatement) -> bool:
    """Set (or clear) `parse_suspect` by testing this statement against the one
    before it. Returns the flag. No commit.

    THE CHECK: statements are a chain. Whatever an account closed at on June 30
    is what it opened at on July 1, so `opening == previous.closing` is a test
    the bank's own documents have to pass — and one that costs nothing, needs no
    ERPNext round trip, and does not care whether Bank Bridge mirrored a single
    transaction in between. It is the cheapest available evidence that a
    recognizer read the right number off the page rather than a plausible one
    printed near it, which is precisely the failure v0.4.41 was written to fix
    (the v0.4.40 parser read a sweep-table transfer as a closing balance, and
    every consecutive pair of the 26 real statements on the live install
    disagreed by hundreds of thousands of dollars without anything noticing).

    Deliberately SILENT when it cannot run — no prior month held, or either side
    unparsed. An unknown is not a suspicion, and flagging every first-ever
    statement would make the flag mean nothing.

    A flag is advisory. It is surfaced on /admin/statements and nothing else
    consults it: choose_anchor_statement's reconciliation test is the check that
    actually gates a posted number, and it is strictly stronger."""
    prior = previous_statement(statement)
    suspect = False
    if (prior is not None and prior.closing_balance is not None
            and statement.opening_balance is not None):
        drift = abs(float(statement.opening_balance)
                    - float(prior.closing_balance))
        suspect = drift > reconcile_tolerance()
        if suspect:
            log.info('statement %s opens at %.2f but %s closed at %.2f — '
                     'flagging the parse as suspect',
                     statement.statement_id, statement.opening_balance,
                     prior.statement_id, prior.closing_balance)
    statement.parse_suspect = suspect
    return suspect


def is_stale(statement: PlaidStatement) -> bool:
    """Whether this row's figures came from an older parser than the one now
    running. A row with no PDF on disk is never stale — there is nothing to
    re-read, so its figures are the best that will ever be had."""
    return (pdf_exists(statement)
            and statement.parser_version() != PARSER_VERSION)


def stale_statements(account_id: str = '') -> list:
    """Every held statement whose figures predate the running parser."""
    q = PlaidStatement.query
    if account_id:
        q = q.filter(PlaidStatement.plaid_account_id == account_id)
    return [s for s in q.all() if is_stale(s)]


def reparse_stale() -> dict:
    """Re-read every statement whose figures predate the running parser.

    THIS IS THE FIX FOR THE FAILURE v0.4.42 WAS WRITTEN AFTER. v0.4.41 shipped
    a parser that corrected a wrong closing balance on all 26 production
    statements — and then nothing changed, because `_store_one` skips any
    statement whose PDF it already holds and the correction only reached the
    database when an operator pressed a button nobody knew to press. The
    install ran the new code over the old numbers for as long as it took
    someone to notice, which is exactly the shape of bug an audit trail is
    supposed to prevent.

    So a parser bump now heals itself: this runs on the same schedule as the
    statement pull, and a row is re-read when its `parser_version` is not the
    running one. It is idempotent and cheap on a settled install — every row
    already carries the current version, so the pass costs one query and reads
    no PDFs at all."""
    stale = stale_statements()
    if not stale:
        return {'examined': 0, 'changed': 0, 'unreadable': 0, 'suspect': 0,
                'fields': 0, 'failed_fields': 0, 'anchors': 0, 'paired': 0}
    log.info('%d statement(s) parsed by an older recognizer — re-reading',
             len(stale))
    stats = reparse_stored(only_stale=True)
    # v0.4.43 · anchors are DERIVED from the figures that just changed, so a
    # chain left standing after a re-parse asserts the old numbers as this
    # account's balance truth. Rebuilding here is what keeps the two from ever
    # disagreeing — the same lesson as reparse_stale itself, one layer up.
    # v0.4.44 · pairing runs BEFORE the rebuild and after the re-parse, which
    # is the only order that works: the cash-services number it keys on was
    # just recovered by the re-parse, and the anchor sums it feeds depend on
    # the pairing being set.
    try:
        stats['paired'] = autolink_cash_services()['paired']
    except Exception:  # pragma: no cover - detection must not break a re-parse
        db.session.rollback()
        log.warning('cash-services autolink after re-parse failed',
                    exc_info=True)
        stats['paired'] = 0
    try:
        stats['anchors'] = rebuild_statement_anchors()['written']
    except Exception:  # pragma: no cover - a report must not break a re-parse
        db.session.rollback()
        log.warning('anchor rebuild after re-parse failed', exc_info=True)
        stats['anchors'] = 0
    return stats


def reparse_stored(account_id: str = '', *, only_stale: bool = False) -> dict:
    """Re-run the parser over PDFs already on disk, without re-downloading.

    This is how an install picks up a parser improvement. `_store_one` skips any
    statement whose PDF it already holds — correctly, since re-downloading an
    unchanged document is pure waste — which means a better recognizer reaches
    NOTHING already stored until something re-reads those bytes. That is what
    this does, and it is the reason /admin/statements has a "Re-parse stored
    PDFs" button: on the install this was written for, all 26 real Wells Fargo
    statements were on disk with wrong closing balances recovered by v0.4.40.

    Rows whose PDF has gone missing are left exactly as they are — a statement
    we can no longer read is not evidence that its recorded figures are wrong.
    Never raises; returns {'examined', 'changed', 'unreadable', 'suspect',
    'fields', 'failed_fields'}."""
    stats = {'examined': 0, 'changed': 0, 'unreadable': 0, 'suspect': 0,
             'fields': 0, 'failed_fields': 0}
    q = PlaidStatement.query
    if account_id:
        q = q.filter(PlaidStatement.plaid_account_id == account_id)
    rows = q.order_by(PlaidStatement.plaid_account_id,
                      PlaidStatement.period_start.asc().nullsfirst()).all()
    if only_stale:
        rows = [r for r in rows if is_stale(r)]
    for row in rows:
        path = resolve_pdf_path(row)
        if not path:
            stats['unreadable'] += 1
            continue
        stats['examined'] += 1
        before = (row.opening_balance, row.closing_balance,
                  row.portfolio_opening_value, row.portfolio_closing_value)
        try:
            with open(path, 'rb') as fh:
                parsed = parse_statement(fh.read())
        except OSError as e:
            log.info('could not re-read %s: %s', path, e)
            stats['unreadable'] += 1
            continue
        apply_parse(row, parsed)
        after = (row.opening_balance, row.closing_balance,
                 row.portfolio_opening_value, row.portfolio_closing_value)
        if before != after:
            stats['changed'] += 1
        meta = parsed.get('metadata') or {}
        stats['fields'] += len(metadata_figures(meta))
        stats['failed_fields'] += len(meta.get('fields_failed') or [])
    # Continuity runs in a second pass, after every row has its new figures:
    # a statement is checked against the month before it, and that month may
    # itself have been re-parsed a moment ago.
    db.session.flush()
    for row in rows:
        if flag_parse_continuity(row):
            stats['suspect'] += 1
    db.session.commit()
    return stats


# ── download, with backoff ──────────────────────────────────────────────────

def backoff_delay(attempt: int) -> float:
    """Seconds to wait before retry `attempt` (1-based): 0.5, 1, 2, 4 … capped
    at 8. Pure, so the schedule is assertable without actually sleeping."""
    return min(8.0, 0.5 * (2 ** max(0, attempt - 1)))


def _sleep(seconds: float) -> None:
    """Indirection so tests can run the retry path at full speed."""
    time.sleep(seconds)  # pragma: no cover - patched in tests


def download_with_retry(plaid_client, access_token: str,
                        statement_id: str) -> bytes | None:
    """One statement's PDF bytes, retried with exponential backoff, or None when
    every attempt failed.

    Returns None rather than raising because a single unavailable statement must
    not abandon the rest of the pull — the caller records the row without a PDF,
    and the next scheduled pull retries it (pdf_exists is False, so it is picked
    up again automatically)."""
    attempts = _download_attempts()
    for attempt in range(1, attempts + 1):
        try:
            return plaid_client.statements_download(access_token, statement_id)
        except PlaidError as e:
            if attempt >= attempts:
                log.warning('statement %s download failed after %d attempt(s): '
                            '%s', statement_id, attempts, e)
                return None
            delay = backoff_delay(attempt)
            log.info('statement %s download attempt %d/%d failed (%s) — '
                     'retrying in %.1fs', statement_id, attempt, attempts, e,
                     delay)
            _sleep(delay)
    return None  # pragma: no cover - the loop always returns


# ── fetching ────────────────────────────────────────────────────────────────

def _plaid_client_or_none(plaid_client=None):
    """The caller's Plaid client, else one built from settings, else None.

    The import path reaches this without a client (admin_ui calls
    import_plaid_account_to_erpnext with no plaid_client), so building one here
    is what makes statement fetch work on a one-click import at all. None means
    Plaid isn't configured, which is a skip, not a failure."""
    if plaid_client is not None:
        return plaid_client
    from . import plaid_settings
    from .sync_engine import get_plaid_client
    if not plaid_settings.is_configured():
        return None
    try:
        return get_plaid_client()
    except Exception as e:
        log.info('Plaid client unavailable for statements: %s', e)
        return None


def _blank_stats() -> dict:
    return {'listed': 0, 'stored': 0, 'skipped_existing': 0, 'parsed': 0,
            'failed': 0, 'items': 0, 'errors': []}


def fetch_statements_for_item(item: PlaidItem, *, plaid_client=None,
                              account_ids=None, limit: int | None = None,
                              oldest_first: bool = True) -> dict:
    """List and download every statement for one Item that isn't already stored.

    `account_ids` restricts the pull to specific Plaid accounts (the import path
    passes the one account it just linked). `limit` caps how many statements are
    DOWNLOADED in this pass — the import path takes just the oldest, since that
    is the one carrying the earliest opening balance, while the monthly job
    takes everything new.

    Idempotent on two independent levels: a statement whose row already exists
    AND whose PDF is on disk is skipped without a download, and a row whose PDF
    has gone missing is re-downloaded into the same row. Never raises."""
    stats = _blank_stats()
    if not is_enabled():
        return stats
    if item is None or item.disconnected:
        return stats
    client = _plaid_client_or_none(plaid_client)
    if client is None:
        return stats
    try:
        access_token = crypto.decrypt(item.access_token_encrypted)
    except Exception as e:
        log.warning('cannot decrypt access token for item %s: %s',
                    item.item_id, e)
        stats['errors'].append(f'{item.item_id}: token unreadable')
        return stats

    # statements_list already swallows Plaid's own errors and returns [], but it
    # can still raise before it gets that far — the SDK's request models are
    # imported lazily inside it, so a missing/partial `plaid` wheel surfaces as
    # an ImportError here rather than as an API failure. Either way this is a
    # best-effort read that must degrade to "no statements".
    try:
        listed = client.statements_list(access_token)
    except Exception as e:
        log.info('statements unavailable for item %s: %s', item.item_id, e)
        stats['errors'].append(f'{item.item_id}: {e}')
        return stats
    stats['items'] = 1
    wanted = set(account_ids) if account_ids else None
    rows = [s for s in listed
            if not wanted or s.get('account_id') in wanted]
    stats['listed'] = len(rows)
    if not rows:
        return stats

    # Oldest first: the earliest statement carries the earliest opening balance,
    # which is the one an anchor wants. Statements with no usable period sort
    # last rather than being dropped — the PDF is still worth holding.
    def _sort_key(s):
        start, _ = period_bounds(s.get('month'), s.get('year'))
        return (start is None, start or date.max)

    rows.sort(key=_sort_key, reverse=not oldest_first)

    downloaded = 0
    for row in rows:
        if limit is not None and downloaded >= limit:
            break
        try:
            result = _store_one(item, row, client, access_token)
        except Exception as e:  # never let one statement sink the pull
            db.session.rollback()
            log.warning('statement %s failed: %s', row.get('statement_id'), e)
            stats['failed'] += 1
            stats['errors'].append(f"{row.get('statement_id')}: {e}")
            continue
        if result == 'skipped':
            stats['skipped_existing'] += 1
            continue
        downloaded += 1
        if result == 'stored':
            stats['stored'] += 1
        elif result == 'parsed':
            stats['stored'] += 1
            stats['parsed'] += 1
        else:
            stats['failed'] += 1
    return stats


def _store_one(item: PlaidItem, row: dict, client, access_token: str) -> str:
    """Download + persist one listed statement. Returns 'skipped' (already
    held), 'parsed' (stored, balances recovered), 'stored' (stored, balances
    not parseable) or 'failed' (no PDF)."""
    statement_id = row['statement_id']
    account_id = row.get('account_id') or ''
    existing = PlaidStatement.query.filter_by(statement_id=statement_id).first()
    if existing is not None and pdf_exists(existing):
        return 'skipped'

    start, end = period_bounds(row.get('month'), row.get('year'))
    label = period_label(start)
    data = download_with_retry(client, access_token, statement_id)

    record = existing or PlaidStatement(statement_id=statement_id)
    record.plaid_item_id = item.item_id
    record.plaid_account_id = account_id
    record.period_start = start
    record.period_end = end
    record.fetched_at = _now()
    record.updated_at = _now()
    if existing is None:
        db.session.add(record)

    if not data:
        # Keep the row: it records that Plaid says this statement exists, and
        # the next pull retries it because pdf_exists() is still False.
        record.pdf_path = record.pdf_path or ''
        record.pdf_bytes = 0
        db.session.commit()
        return 'failed'

    path = pdf_path_for(item.item_id, account_id, label, statement_id)
    record.pdf_bytes = store_pdf(path, data)
    record.pdf_path = path
    balances = apply_parse(record, parse_balances(data))
    db.session.commit()
    flag_parse_continuity(record)
    db.session.commit()

    if balances['opening'] is None and balances['closing'] is None:
        log.info('statement %s (%s) stored, but no balances could be parsed '
                 'from its PDF — opening balances fall back to the estimate',
                 statement_id, label)
        return 'stored'
    return 'parsed'


def fetch_for_account(account: PlaidAccount, *, plaid_client=None,
                      limit: int | None = 1) -> dict:
    """Pull statements for ONE account — the import path's entry point.

    Defaults to `limit=1`, i.e. the oldest available statement, because that is
    the one carrying the earliest opening balance and a one-click import should
    not sit through a dozen PDF downloads. The monthly job backfills the rest."""
    stats = _blank_stats()
    if not is_enabled() or account is None:
        return stats
    item = PlaidItem.query.filter_by(item_id=account.item_id).first()
    if item is None:
        return stats
    return fetch_statements_for_item(
        item, plaid_client=plaid_client, account_ids=[account.account_id],
        limit=limit)


def fetch_all(*, plaid_client=None, limit_per_item: int | None = None) -> dict:
    """Pull new statements for every connected Item — the monthly job and the
    backfill script's entry point. Aggregates per-Item stats; never raises."""
    totals = _blank_stats()
    if not is_enabled():
        totals['errors'].append('statements are disabled (STATEMENTS_ENABLED)')
        return totals
    items = PlaidItem.query.filter(
        PlaidItem.disconnected.is_(False)).order_by(PlaidItem.id).all()
    for item in items:
        stats = fetch_statements_for_item(item, plaid_client=plaid_client,
                                          limit=limit_per_item)
        for key in ('listed', 'stored', 'skipped_existing', 'parsed', 'failed',
                    'items'):
            totals[key] += stats[key]
        totals['errors'].extend(stats['errors'])
    return totals


# ── reconciliation ──────────────────────────────────────────────────────────

def signed_movement(account: PlaidAccount, start: date | None,
                    end: date | None) -> float:
    """Σ of Plaid amounts for this account's mirrored transactions in
    [start, end], excluding pending and removed rows.

    Pending rows are provisional and may be restated; removed rows Plaid took
    back. Counting either would move the number by its full value and produce a
    reconciliation delta that reflects our bookkeeping rather than the bank's —
    the same exclusion, for the same reason, as estimate_opening_balance.

    v0.4.38 · investment/brokerage accounts include SecurityTransaction cash
    flows too. /transactions/sync only ships plain bank cash events; the
    investment side (buys, sells, dividends, options premium, transfers) lives
    in SecurityTransaction from /investments/transactions/get. Before this
    fix, brokerage-account reconciliation always showed movement=0 because
    BankTransaction was empty for those accounts, producing massive spurious
    deltas whenever the statement's actual closing reflected investment
    activity. Both models use the same Plaid sign convention (positive =
    money out of account) so the sums combine directly.

    v0.4.45 · sums the account's whole SUPERSEDE CHAIN, not one row of it. A
    re-linked account keeps its pre-relink transactions on the old row, so
    filtering on a single account_id sees only part of the history and reports
    the rest as unexplained. See `supersede_chain` for why walking both
    directions is what makes the active/superseded designation cosmetic.

    v0.4.48 · when the account is PAIRED, its SecurityTransactions are skipped
    ENTIRELY — the companion is a cash-sweep ledger that already records every
    security event's cash leg, so counting them here double-counts. Unpaired
    accounts keep everything: with no companion holding the sweep, that
    security feed is the only cash record there is. See _bank_total's comment
    for the 26-month verification that settled this."""
    if start is None or end is None:
        return 0.0
    account_ids = supersede_chain(account.account_id)
    bank_total = _bank_total(account_ids, start, end)
    # v0.4.38: brokerage/investment accounts have SecurityTransaction rows
    # that also move cash. Include them so reconciliation reflects the
    # complete picture (buys reduce cash, sells add cash, dividends add
    # cash, etc.).
    if (account.type or '').lower() in ('investment', 'brokerage'):
        # v0.4.48 · a PAIRED brokerage's cash is entirely on its sweep
        # companion (see _bank_total's neighbouring comment). Its own
        # SecurityTransactions would double-count that, so they are skipped and
        # only the bank side — the brokerage's own chain plus, via
        # anchor_transaction_sum, the companion's — is summed.
        if (account.paired_account_id or '').strip():
            return round(bank_total, 2)
        from .models import SecurityTransaction
        sec_rows = (SecurityTransaction.query
                    .filter(SecurityTransaction.account_id.in_(
                        tuple(account_ids)),
                            SecurityTransaction.date >= start,
                            SecurityTransaction.date <= end)
                    .all())
        # Deduped for the same reason the bank rows are, defensively: the live
        # data doesn't show doubled security transactions today, but a re-link
        # overlap is a property of the re-link, not of the table.
        if len(account_ids) > 1:
            sec_rows = dedupe_across_accounts(sec_rows)
        sec_total = sum(float(t.amount or 0.0) for t in sec_rows)
        return round(bank_total + sec_total, 2)
    return round(bank_total, 2)


def apply_movement(account: PlaidAccount, opening: float,
                   movement: float) -> float:
    """The balance an account reaches from `opening` after Σamount `movement`.

    Plaid's amount is positive for money LEAVING the account, and what that does
    to the balance is type-dependent in exactly the way opening_balance's module
    docstring lays out: for a depository/investment account the balance is what
    you HAVE, so an outflow decreases it; for a credit account the balance is
    what you OWE, so a purchase increases it. Delegating the asset/liability
    question to opens_by_debit keeps this from drifting away from the side the
    GL leaf was actually created under."""
    from . import opening_balance as obal
    if obal.opens_by_debit(account):
        return round(float(opening) - float(movement), 2)
    return round(float(opening) + float(movement), 2)


def reconcile_statement(statement: PlaidStatement,
                        account: PlaidAccount | None = None) -> dict:
    """Measure one statement against the mirror.

    Returns {'status', 'expected_closing', 'closing', 'delta', 'movement',
    'txn_count', 'opening', 'opening_source', 'closing_source'}. `status` is
    one of:

      * 'ok'        — the mirror's arithmetic lands on the bank's (or
                      computed) closing balance, within tolerance
      * 'mismatch'  — it doesn't, by `delta`; the mirror has a gap for this
                      period (or the bank's cycle isn't a calendar month)
      * 'computed'  — the PDF was unparseable but the mirror knows the balance
                      for this period; opening/closing are transaction-derived
                      and the reconciliation is trivially exact (both sides
                      come from the same mirror) — the row still surfaces
                      opening + closing + movement so an operator can eyeball
                      the period rather than see a blank line
      * 'no_data'   — no statement fields to work with AND the mirror doesn't
                      cover this period either; nothing worth reporting

    v0.4.20: when the PDF parser returned None for opening or closing, the
    computed_balances module fills them from the transaction mirror. Sources
    are surfaced explicitly (`bank` vs `computed`) so the UI can show which
    figure came from where — a bank-issued number gets the same authority it
    always did; a computed one is honest about being derived from the mirror,
    not asserted by the institution.

    'no_data' is deliberately NOT a mismatch. An unparseable PDF over an
    unmirrored period says nothing about whether the books agree, and
    flagging it as a discrepancy would train an operator to ignore the one
    signal on this page that means something."""
    blank = {'status': 'no_data', 'expected_closing': None, 'closing': None,
             'delta': None, 'movement': 0.0, 'txn_count': 0,
             'opening': None,
             'opening_source': None, 'closing_source': None}
    account = account or (PlaidAccount.query.filter_by(
        account_id=statement.plaid_account_id).first()
        if statement.plaid_account_id else None)
    if account is None:
        return blank

    # v0.4.20 · fall back to the mirror when the PDF parser missed either
    # side. The parser is silent-optional by design (see the module
    # docstring); the mirror is authoritative arithmetic on top of a
    # transaction feed Plaid guarantees is complete.
    from . import computed_balances as cb
    opening = statement.opening_balance
    opening_source = 'bank' if opening is not None else None
    closing = statement.closing_balance
    closing_source = 'bank' if closing is not None else None
    if opening is None or closing is None:
        c_open, c_close = cb.opening_and_closing_for_period(
            account, statement.period_start, statement.period_end)
        if opening is None and c_open is not None:
            opening = c_open
            opening_source = 'computed'
        if closing is None and c_close is not None:
            closing = c_close
            closing_source = 'computed'

    if opening is None or closing is None:
        return blank

    movement = signed_movement(account, statement.period_start,
                               statement.period_end)
    expected = apply_movement(account, opening, movement)
    delta = round(expected - float(closing), 2)
    count = (BankTransaction.query
             .filter(BankTransaction.account_id == account.account_id,
                     BankTransaction.date >= statement.period_start,
                     BankTransaction.date <= statement.period_end,
                     BankTransaction.pending.is_(False),
                     BankTransaction.removed.is_(False))
             .count()) if statement.period_start and statement.period_end else 0

    # v0.4.39 · manual reconciliation adjustments. When the operator has
    # attributed portions of the delta to specific offset accounts (tax
    # payment, member distribution, off-platform wire, etc.), the residual
    # delta is what's LEFT after adjustments. A statement whose delta is
    # fully explained by adjustments reconciles cleanly at status='reconciled'.
    from .models import StatementAdjustment
    adjustments = (StatementAdjustment.query
                   .filter_by(statement_id=statement.id).all())
    adjustment_total = round(sum(a.amount for a in adjustments), 2)
    adjusted_delta = round(delta - adjustment_total, 2)

    # When BOTH sides came from the mirror, the reconciliation identity is
    # trivial — expected and closing are two ways of computing the same
    # arithmetic. Report status='computed' so an operator knows the equality
    # is definitional here, not a bank cross-check.
    if opening_source == 'computed' and closing_source == 'computed':
        status = 'computed'
    elif abs(adjusted_delta) <= reconcile_tolerance():
        status = 'reconciled' if adjustment_total else 'ok'
    else:
        status = 'mismatch'
    return {
        'status': status,
        'expected_closing': expected,
        'closing': round(float(closing), 2),
        'delta': delta, 'movement': movement, 'txn_count': count,
        'opening': round(float(opening), 2),
        'opening_source': opening_source, 'closing_source': closing_source,
        'adjustment_total': adjustment_total,
        'adjusted_delta': adjusted_delta,
        'adjustments': [a.to_dict() for a in adjustments],
    }


def statements_for_account(account_id: str) -> list:
    """This account's statements, newest period first (how the admin list and
    any operator reads them)."""
    return (PlaidStatement.query
            .filter_by(plaid_account_id=account_id)
            .order_by(PlaidStatement.period_start.desc().nullslast(),
                      PlaidStatement.id.desc())
            .all())


def reconcile(account: PlaidAccount) -> list:
    """Every statement for one account, each with its reconciliation verdict:
    [{'statement': PlaidStatement, 'to_dict': {...}, **verdict}]."""
    out = []
    for st in statements_for_account(account.account_id):
        verdict = reconcile_statement(st, account)
        out.append({'statement': st, 'row': st.to_dict(), **verdict})
    return out


# ── anchoring an opening balance ────────────────────────────────────────────

def earliest_transaction_date(account: PlaidAccount) -> date | None:
    """The oldest non-pending, non-removed mirrored transaction date, or None
    when the mirror is empty."""
    return (db.session.query(db.func.min(BankTransaction.date))
            .filter(BankTransaction.account_id == account.account_id,
                    BankTransaction.pending.is_(False),
                    BankTransaction.removed.is_(False))
            .scalar())


def choose_anchor_statement(account: PlaidAccount) -> PlaidStatement | None:
    """The oldest statement it is SAFE to book this account's opening balance
    from, or None.

    This is the check that makes statement-anchored opening balances trustworthy
    rather than merely automatic. Booking `opening_balance` dated `period_start`
    asserts that the ledger from that date forward is exactly the mirrored
    transactions — so two things have to hold, and both are tested here rather
    than assumed:

      1. NO PRE-ANCHOR MOVEMENT. If the mirror holds transactions dated before
         period_start, they sit underneath an entry that already accounts for
         them and would be counted twice. A statement older than the mirror is
         therefore rejected outright.

      2. THE PERIOD RECONCILES. opening + mirrored movement must land on the
         bank's own closing balance. This is the strong signal: it is precisely
         the statement that a gap in the mirror would fail. A period with no
         activity reconciles trivially (opening == closing), which is correct —
         nothing happened, so nothing can be missing.

    Oldest-qualifying-first, because the earliest safe anchor books the longest
    stretch of real history. None means fall back to the v0.4.4 estimate, which
    is always a legitimate answer."""
    if not is_enabled():
        return None
    earliest_txn = earliest_transaction_date(account)
    candidates = (PlaidStatement.query
                  .filter(PlaidStatement.plaid_account_id == account.account_id,
                          PlaidStatement.opening_balance.isnot(None),
                          PlaidStatement.closing_balance.isnot(None),
                          PlaidStatement.period_start.isnot(None))
                  .order_by(PlaidStatement.period_start.asc())
                  .all())
    for st in candidates:
        if earliest_txn is not None and earliest_txn < st.period_start:
            continue  # movement predates the anchor — it would double count
        if reconcile_statement(st, account)['status'] != 'ok':
            continue  # the mirror can't reproduce the bank's own closing figure
        return st
    return None


def anchor_for(account: PlaidAccount) -> tuple[float, date, PlaidStatement] | None:
    """(opening_balance, posting_date, statement) to book for this account from a
    bank-issued statement, or None when no statement qualifies."""
    st = choose_anchor_statement(account)
    if st is None:
        return None
    return float(st.opening_balance), st.period_start, st


# ── import-path hook ────────────────────────────────────────────────────────

def fetch_on_import(account: PlaidAccount, *, plaid_client=None) -> dict | None:
    """Pull this account's oldest statement right after it is linked.

    Best-effort and deliberately quiet: this runs inside the account-import path,
    where a Plaid hiccup must degrade to "no statement yet" rather than unwind an
    import that has already created ERPNext records. Returns the stats dict, or
    None when statements are off.

    NOTE that this does NOT change the opening balance the import then books.
    At link time `balances.current` is the bank's own number and posting it as of
    today is exact — there is no estimate here to improve on. The statement is
    fetched for the audit trail and for /admin/statements; anchoring is for the
    backfill path, where an estimate genuinely exists (see the module
    docstring)."""
    if not is_enabled():
        return None
    try:
        stats = fetch_for_account(account, plaid_client=plaid_client, limit=1)
    except Exception:  # pragma: no cover - the fetch already swallows its own
        db.session.rollback()
        log.warning('statement fetch failed for %s', account.account_id,
                    exc_info=True)
        return None
    if stats['stored']:
        audit.record('statement_fetched', subject_type='PlaidAccount',
                     subject_id=account.account_id, after=stats,
                     notes=f"fetched {stats['stored']} statement(s) at import")
    return stats
