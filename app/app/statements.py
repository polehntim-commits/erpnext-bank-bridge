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


# ── PDF text → balances ─────────────────────────────────────────────────────
#
# The bank's layout is the bank's business, so this is deliberately a
# recognizer, not a parser: it looks for the handful of labels US institutions
# actually print and takes the first currency amount that follows one. Anything
# it doesn't recognize yields None, which every caller reads as "no bank-issued
# figure" and falls back on.

# Ordered most- to least-specific. 'previous balance' must be tried before a
# bare 'balance', and the credit-card wording ('previous balance', 'new
# balance') sits alongside the depository wording because one account's
# statement uses one vocabulary or the other, never both.
_OPENING_LABELS = (
    'beginning balance', 'opening balance', 'previous balance',
    'balance forward', 'previous statement balance', 'starting balance',
    'beginning balance on', 'balance last statement',
)
_CLOSING_LABELS = (
    'ending balance', 'closing balance', 'new balance', 'ending daily balance',
    'statement balance', 'new balance total', 'balance this statement',
)

# A US-format currency amount: optional leading '-' or '(', optional '$',
# grouped digits, exactly two decimals, optional trailing ')'. The parenthesis
# and the minus both mean negative, which is how statements print an overdrawn
# depository account or a credit balance on a card.
_AMOUNT = r'\(?-?\s*\$?\s*(\d{1,3}(?:,\d{3})*|\d+)\.(\d{2})\s*\)?'
_AMOUNT_RE = re.compile(_AMOUNT)


def extract_text(pdf_bytes: bytes) -> str:
    """All text in the PDF, lowercased and whitespace-collapsed, or '' when it
    can't be read.

    '' is a first-class outcome, not an error: a scanned/image-only statement
    has no text layer at all, and a PDF that pypdf refuses is common enough in
    the wild that raising here would turn a routine parse miss into a failed
    fetch. The bytes are still stored either way — an operator can always open
    the document even when we can't read it."""
    if not pdf_bytes:
        return ''
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for pageno, page in enumerate(reader.pages):
            try:
                parts.append(page.extract_text() or '')
            except Exception:
                log.debug('unreadable page %d in statement PDF', pageno)
        text = ' '.join(parts)
    except Exception as e:
        log.info('could not extract text from statement PDF: %s', e)
        return ''
    return re.sub(r'\s+', ' ', text).strip().lower()


def _amount_after(text: str, label: str) -> float | None:
    """The first currency amount printed after `label`, or None.

    Bounded to a 120-character window because statements are laid out in columns
    and the extracted text runs them together: without a bound, 'ending balance'
    on one line could pick up an amount belonging to a table three rows down.
    120 comfortably covers 'Beginning balance on July 1 ... $1,234.56' while
    stopping well short of the next labelled figure."""
    idx = text.find(label)
    if idx < 0:
        return None
    window = text[idx + len(label): idx + len(label) + 120]
    m = _AMOUNT_RE.search(window)
    if not m:
        return None
    whole = m.group(1).replace(',', '')
    try:
        value = float(f'{whole}.{m.group(2)}')
    except ValueError:  # pragma: no cover - the regex guarantees the shape
        return None
    matched = m.group(0)
    if matched.lstrip().startswith('(') or '-' in matched.split('$')[0]:
        value = -value
    return round(value, 2)


def _first_label_amount(text: str, labels) -> float | None:
    for label in labels:
        value = _amount_after(text, label)
        if value is not None:
            return value
    return None


def parse_balances(pdf_bytes: bytes) -> dict:
    """{'opening': float|None, 'closing': float|None, 'has_text': bool} for one
    statement PDF. Both None is an ordinary result, not a failure."""
    text = extract_text(pdf_bytes)
    if not text:
        return {'opening': None, 'closing': None, 'has_text': False}
    return {
        'opening': _first_label_amount(text, _OPENING_LABELS),
        'closing': _first_label_amount(text, _CLOSING_LABELS),
        'has_text': True,
    }


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
    balances = parse_balances(data)
    record.opening_balance = balances['opening']
    record.closing_balance = balances['closing']
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
    the same exclusion, for the same reason, as estimate_opening_balance."""
    if start is None or end is None:
        return 0.0
    rows = (BankTransaction.query
            .filter(BankTransaction.account_id == account.account_id,
                    BankTransaction.date >= start,
                    BankTransaction.date <= end,
                    BankTransaction.pending.is_(False),
                    BankTransaction.removed.is_(False))
            .all())
    return round(sum(float(t.amount or 0.0) for t in rows), 2)


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
    'txn_count'}. `status` is one of:

      * 'ok'        — the mirror's arithmetic lands on the bank's closing
                      balance, within tolerance
      * 'mismatch'  — it doesn't, by `delta`; the mirror has a gap for this
                      period (or the bank's cycle isn't a calendar month)
      * 'no_data'   — the statement's balances couldn't be parsed, so there is
                      nothing to measure against

    'no_data' is deliberately NOT a mismatch. An unparseable PDF says nothing
    about whether the books agree, and flagging it as a discrepancy would train
    an operator to ignore the one signal on this page that means something."""
    blank = {'status': 'no_data', 'expected_closing': None, 'closing': None,
             'delta': None, 'movement': 0.0, 'txn_count': 0}
    if statement.opening_balance is None or statement.closing_balance is None:
        return blank
    account = account or PlaidAccount.query.filter_by(
        account_id=statement.plaid_account_id).first()
    if account is None:
        return blank
    movement = signed_movement(account, statement.period_start,
                               statement.period_end)
    expected = apply_movement(account, statement.opening_balance, movement)
    delta = round(expected - float(statement.closing_balance), 2)
    count = (BankTransaction.query
             .filter(BankTransaction.account_id == account.account_id,
                     BankTransaction.date >= statement.period_start,
                     BankTransaction.date <= statement.period_end,
                     BankTransaction.pending.is_(False),
                     BankTransaction.removed.is_(False))
             .count()) if statement.period_start and statement.period_end else 0
    return {
        'status': 'ok' if abs(delta) <= reconcile_tolerance() else 'mismatch',
        'expected_closing': expected,
        'closing': round(float(statement.closing_balance), 2),
        'delta': delta, 'movement': movement, 'txn_count': count,
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
