# SPDX-License-Identifier: MIT
"""Intercompany transfer detection + paired Journal Entry generation (v0.4.1).

THE PROBLEM. When money moves between two ERPNext Companies the operator owns —
Farm checking → Personal checking — Plaid delivers TWO transactions, one on each
linked account, equal in magnitude and opposite in sign. Nothing in the rules
engine knows they are the same movement, so a generic rule books each leg on its
own:

    Farm     -$10,000  "Transfer to Personal"   → an expense on the Farm's P&L
    Personal +$10,000  "Transfer from Farm"     → income on Personal's P&L

Both sets of books then show revenue and expense that never happened. It is only
money changing pockets.

THE FIX, in three parts:

  1. DETECT (find_transfer_pair / detect_pairs). Match a transaction against
     candidate counterparties in OTHER Companies' linked accounts on amount,
     date, and description, and score the match 0.0-1.0 (see score_pair). At or
     above INTERCOMPANY_CONFIDENCE_THRESHOLD the two rows are recorded as an
     `IntercompanyTransferPair` and stamped with its id.

  2. DIVERT (categorization.categorize_after_push). A paired transaction is no
     longer eligible for rules that carry `ignore_for_paired` (the default), so
     the generic Transfer rule never fires on it.

  3. BOOK (generate_pair_journal_entries). Instead, ONE pair of Journal Entries
     is written, one per Company, through a counterparty control account:

       source Company:  Dr  Due from <target>      Cr  <source bank>
       target Company:  Dr  <target bank>          Cr  Due to <source>

     P&L untouched; the movement shows up on both balance sheets as a mutual
     receivable/payable that nets to zero across the two entities.

ATOMICITY. The two JEs are all-or-nothing (see generate_pair_journal_entries):
if the second create fails, the first is deleted in ERPNext before the failure is
recorded. Half-updated books are the one outcome worse than no entry at all.

BACKWARD COMPATIBILITY. Detection needs at least two Companies with linked
accounts, so on a single-Company install every pass finds nothing and every code
path here is inert. Nothing about a v0.4.0.9 database changes on upgrade.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from flask import current_app

from . import audit
from . import db
from . import erpnext_accounts
from .erpnext_client import ERPNextAPIError, ERPNextError
from .models import (BankTransaction, GeneratedJournalEntry,
                     IntercompanyTransferPair, PlaidAccount)

log = logging.getLogger('bankbridge.intercompany')

JOURNAL_ENTRY_DT = 'Journal Entry'

# Defaults for the three knobs, all overridable via config/env. They are module
# constants rather than inline literals so the tests can state the contract.
DEFAULT_DATE_TOLERANCE_DAYS = 3
DEFAULT_DESCRIPTION_THRESHOLD = 0.6
DEFAULT_CONFIDENCE_THRESHOLD = 0.75
# How far back a detection pass looks for counterparties. Generous, because the
# two legs of one transfer routinely arrive on different syncs (each Plaid Item
# pulls on its own cursor) — but bounded, so the pass stays a small query rather
# than a full-table scan that grows without limit.
DEFAULT_LOOKBACK_DAYS = 30

# Amounts are compared to the cent. Two legs of one transfer are the same money,
# so anything beyond float representation error is a different transaction.
AMOUNT_EPSILON = 0.005


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cfg_float(key: str, default: float) -> float:
    try:
        return float(current_app.config.get(key, default))
    except (TypeError, ValueError, RuntimeError):
        return default


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(current_app.config.get(key, default))
    except (TypeError, ValueError, RuntimeError):
        return default


def date_tolerance_days() -> int:
    """± window, in days, two legs of one transfer may be dated apart
    (INTERCOMPANY_DATE_TOLERANCE_DAYS, default 3). A same-bank move clears the
    same day; an ACH between institutions routinely takes two or three."""
    return max(0, _cfg_int('INTERCOMPANY_DATE_TOLERANCE_DAYS',
                           DEFAULT_DATE_TOLERANCE_DAYS))


def description_threshold() -> float:
    """Minimum description similarity (0.0-1.0) for a candidate to be considered
    at all (INTERCOMPANY_DESCRIPTION_THRESHOLD, default 0.6)."""
    return _cfg_float('INTERCOMPANY_DESCRIPTION_THRESHOLD',
                      DEFAULT_DESCRIPTION_THRESHOLD)


def confidence_threshold() -> float:
    """Minimum confidence for a detected pair to be recorded automatically
    (INTERCOMPANY_CONFIDENCE_THRESHOLD, default 0.75)."""
    return _cfg_float('INTERCOMPANY_CONFIDENCE_THRESHOLD',
                      DEFAULT_CONFIDENCE_THRESHOLD)


def lookback_days() -> int:
    """How many days back a detection pass considers
    (INTERCOMPANY_LOOKBACK_DAYS, default 30)."""
    return max(0, _cfg_int('INTERCOMPANY_LOOKBACK_DAYS', DEFAULT_LOOKBACK_DAYS))


# ── description similarity ─────────────────────────────────────────────

# Everything but letters, digits and spaces — reference numbers keep their
# digits, but punctuation and separators are flattened before comparison.
_NON_ALNUM_RE = re.compile(r'[^a-z0-9 ]+')


def normalize_description(text: str) -> str:
    """Fold a Plaid description to a comparison form: lowercased, punctuation
    replaced by spaces, runs of whitespace collapsed. So 'ONLINE TRANSFER TO
    CHECKING #1234' and 'Online Transfer to Checking 1234' compare identical."""
    s = _NON_ALNUM_RE.sub(' ', (text or '').lower())
    return ' '.join(s.split())


def description_similarity(a: str, b: str) -> float:
    """Similarity of two descriptions, 0.0-1.0, via `difflib.SequenceMatcher`
    over their normalized forms.

    STDLIB ON PURPOSE. The obvious alternatives (rapidfuzz, fuzzywuzzy) are a new
    external dependency for a comparison that runs a few dozen times per sync,
    and this is a single-container Umbrel app where every added wheel is a
    packaging risk. SequenceMatcher's ratio is more than discriminating enough at
    the scale that matters here: the canonical 'Transfer to Personal' /
    'Transfer from Farm' pair scores 0.63 while two unrelated merchants score
    below 0.3, so the 0.6 threshold separates them with room to spare.

    Two blank descriptions score 0.0, not 1.0 — a bank that sends no description
    on either leg supplies no evidence, and treating absence as a perfect match
    would let amount alone pair unrelated transactions."""
    na, nb = normalize_description(a), normalize_description(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


# ── scoring ────────────────────────────────────────────────────────────

def score_pair(amount_a: float, amount_b: float, date_a, date_b,
               desc_a: str, desc_b: str) -> float:
    """Confidence, 0.0-1.0, that these two transactions are the two legs of one
    intercompany transfer. Returns 0.0 for anything that fails a hard gate.

    HARD GATES (all required — a failure is 0.0, not a low score):
      * equal magnitude, opposite sign. The same money left one account and
        arrived in the other; an unequal amount is simply a different movement.
      * dated within the ± tolerance window.
      * description similarity at or above the threshold.

    THE SCORE ITSELF, once the gates pass:

        0.50  base — amount and sign already match exactly, which is by far the
              strongest single signal, so a gate-passing candidate starts
              halfway rather than at zero.
      + 0.25 × date proximity, decaying linearly to 0 across the window
              (same day 1.0, one day 0.75, three days 0.25 at the default
              tolerance).
      + 0.25 × description similarity.

    So a same-day transfer whose two descriptions read 'Transfer to Personal' /
    'Transfer from Farm' (0.63 similar) scores 0.91 and auto-pairs, while the
    same pair of descriptions three days apart scores 0.72 and is surfaced for
    review instead of booked. That is the intended split: weak evidence gets a
    human, not a Journal Entry.

    The weights are deliberately blunt. Amount+sign does the real work; date and
    description are corroboration, and splitting the remaining half evenly
    between them avoids implying a precision this has no way to justify."""
    a, b = float(amount_a or 0.0), float(amount_b or 0.0)
    # Opposite sign, equal magnitude. One must be an outflow and one an inflow —
    # so their sum is zero and neither is zero itself.
    if abs(a) < AMOUNT_EPSILON or abs(a + b) > AMOUNT_EPSILON:
        return 0.0
    tol = date_tolerance_days()
    if date_a is None or date_b is None:
        return 0.0
    delta = abs((date_a - date_b).days)
    if delta > tol:
        return 0.0
    sim = description_similarity(desc_a, desc_b)
    if sim < description_threshold():
        return 0.0
    date_score = 1.0 - (delta / (tol + 1.0))
    return round(0.5 + 0.25 * date_score + 0.25 * sim, 4)


# ── candidate lookup ───────────────────────────────────────────────────

def _company_of(row: BankTransaction, cache: dict | None = None) -> str:
    """The ERPNext Company owning this transaction's linked account, memoized per
    account_id across a detection pass."""
    acct_id = getattr(row, 'account_id', None) or ''
    if cache is not None and acct_id in cache:
        return cache[acct_id]
    company = erpnext_accounts.owning_company_for_account_id(acct_id)
    if cache is not None:
        cache[acct_id] = company
    return company


def _suppressed_keys() -> set:
    """The unordered {transaction_id, transaction_id} pairs a REJECTED
    IntercompanyTransferPair already covers.

    Load-bearing for Unpair. Rejecting a pair clears both transactions'
    `intercompany_pair_id` so they return to normal categorization — but the very
    next detection pass would see two unpaired, perfectly-matching transactions
    and re-pair them, undoing the operator's decision on a loop. Keeping the
    rejected row as a suppression record is what makes Unpair stick."""
    rows = (IntercompanyTransferPair.query
            .filter(IntercompanyTransferPair.state == 'rejected')
            .with_entities(IntercompanyTransferPair.from_transaction_id,
                           IntercompanyTransferPair.to_transaction_id).all())
    return {frozenset((a, b)) for a, b in rows}


def _paired_ids() -> set:
    """Every plaid_transaction_id currently claimed by a non-rejected pair. A
    transaction belongs to at most one pair at a time."""
    rows = (IntercompanyTransferPair.query
            .filter(IntercompanyTransferPair.state != 'rejected')
            .with_entities(IntercompanyTransferPair.from_transaction_id,
                           IntercompanyTransferPair.to_transaction_id).all())
    out = set()
    for a, b in rows:
        out.add(a)
        out.add(b)
    return out


def multi_company_accounts() -> bool:
    """True when linked, sync-enabled accounts resolve to MORE THAN ONE ERPNext
    Company — the precondition for any intercompany transfer to exist.

    Every entry point checks this first, so a single-Company install pays one
    cheap local query per sync and nothing else. This is what makes the whole
    feature auto-activate: link a second Company's bank and detection starts;
    until then it is inert."""
    companies = set()
    for acct in PlaidAccount.query.all():
        if not acct.erpnext_bank_account_name or not acct.sync_enabled:
            continue
        company = erpnext_accounts.owning_company_for(acct)
        if company:
            companies.add(company)
        if len(companies) > 1:
            return True
    return False


def candidate_transactions(row: BankTransaction, company: str,
                           cache: dict | None = None) -> list:
    """Transactions that could be `row`'s counterparty: the opposite sign, the
    same magnitude, inside the date window, not removed, and belonging to a
    DIFFERENT Company. Cheap filters run in SQL; the Company test (which needs
    the account → Item → default resolution chain) runs in Python on the handful
    of rows that survive."""
    if row.date is None:
        return []
    tol = date_tolerance_days()
    lo, hi = row.date - timedelta(days=tol), row.date + timedelta(days=tol)
    amount = float(row.amount or 0.0)
    rows = (BankTransaction.query
            .filter(BankTransaction.plaid_transaction_id
                    != row.plaid_transaction_id,
                    BankTransaction.removed.is_(False),
                    BankTransaction.date >= lo, BankTransaction.date <= hi)
            .all())
    out = []
    for other in rows:
        if abs(float(other.amount or 0.0) + amount) > AMOUNT_EPSILON:
            continue
        other_company = _company_of(other, cache)
        # Different Companies is the definition of intercompany. Two accounts of
        # the SAME Company transferring between themselves is an ordinary
        # internal move that the existing skip_party transfer rules already
        # handle, and booking a Due-from/Due-to against yourself is meaningless.
        if not other_company or not company or other_company == company:
            continue
        out.append(other)
    return out


def find_transfer_pair(row: BankTransaction, *, cache: dict | None = None,
                       paired: set | None = None, suppressed: set | None = None):
    """The best counterparty for `row` in another Company's linked account, as
    `(counterparty_transaction, confidence)`, or None when there isn't one.

    Applies every criterion in score_pair, plus two the score can't see: neither
    transaction may already belong to a pair, and the combination must not be one
    an operator already rejected. `paired` / `suppressed` let a batch pass
    compute those sets once instead of per row.

    Returns the HIGHEST-scoring candidate. Ties break on the smaller date gap and
    then the lower id, so a repeated run over the same data always reaches the
    same answer — a detector that shuffled its own output would make the review
    queue impossible to reason about."""
    if row is None or row.removed:
        return None
    paired = _paired_ids() if paired is None else paired
    if row.plaid_transaction_id in paired:
        return None
    suppressed = _suppressed_keys() if suppressed is None else suppressed
    cache = {} if cache is None else cache
    company = _company_of(row, cache)
    if not company:
        return None
    best = None
    for other in candidate_transactions(row, company, cache):
        if other.plaid_transaction_id in paired:
            continue
        if frozenset((row.plaid_transaction_id,
                      other.plaid_transaction_id)) in suppressed:
            continue
        score = score_pair(row.amount, other.amount, row.date, other.date,
                           row.name or '', other.name or '')
        if score <= 0.0:
            continue
        delta = abs((row.date - other.date).days)
        key = (score, -delta, -(other.id or 0))
        if best is None or key > best[0]:
            best = (key, other, score)
    return (best[1], best[2]) if best else None


# ── recording a detected pair ──────────────────────────────────────────

def _orient(a: BankTransaction, b: BankTransaction):
    """(source, target) for the two legs — Plaid's convention is positive =
    money OUT of the account, so the positive-amount row is the source. Called
    before any pair row is created, because every consumer downstream reads
    direction off the row rather than re-deriving it."""
    return (a, b) if float(a.amount or 0.0) > 0 else (b, a)


def record_pair(row_a: BankTransaction, row_b: BankTransaction,
                confidence: float, cache: dict | None = None):
    """Create the IntercompanyTransferPair for two matched transactions, oriented
    source-first, and stamp both local rows with its id. Returns the pair.

    Committed here rather than by the caller: the stamp on the two transactions
    and the pair row have to land together or the detector would re-consider
    rows it had already claimed."""
    cache = {} if cache is None else cache
    source, target = _orient(row_a, row_b)
    pair = IntercompanyTransferPair(
        from_transaction_id=source.plaid_transaction_id,
        from_company=_company_of(source, cache),
        to_transaction_id=target.plaid_transaction_id,
        to_company=_company_of(target, cache),
        amount=round(abs(float(source.amount or 0.0)), 2),
        confidence=float(confidence), state='pending')
    db.session.add(pair)
    db.session.flush()          # assign pair.id before stamping the two rows
    source.intercompany_pair_id = pair.id
    target.intercompany_pair_id = pair.id
    db.session.commit()
    log.info('intercompany pair #%s detected: %s (%s) ↔ %s (%s) %.2f @ %.2f',
             pair.id, pair.from_transaction_id, pair.from_company,
             pair.to_transaction_id, pair.to_company, pair.amount,
             pair.confidence)
    audit.record('intercompany_pair_detected',
                 subject_type='IntercompanyTransferPair', subject_id=pair.id,
                 after=pair.to_dict(),
                 notes=(f'{pair.from_company} → {pair.to_company} '
                        f'{pair.amount:.2f} (confidence {pair.confidence:.2f})'))
    return pair


def detect_pairs(*, limit_transaction_ids=None) -> list:
    """Run a detection pass and record every pair that clears the confidence
    threshold. Returns the newly-created pairs.

    Scans unpaired, non-removed transactions inside the lookback window, newest
    first. `limit_transaction_ids` narrows the scan to a specific set (the sync
    path passes the rows it just pulled) — candidates are still drawn from the
    full window either way, which is what lets a leg pulled today pair with one
    that landed three days ago on the other Company's Item.

    Best-effort by construction: a pair below the threshold is simply not
    recorded, and the pass never raises into the sync loop."""
    if not multi_company_accounts():
        return []
    threshold = confidence_threshold()
    cutoff = (_now() - timedelta(days=lookback_days())).date()
    q = (BankTransaction.query
         .filter(BankTransaction.removed.is_(False),
                 BankTransaction.intercompany_pair_id.is_(None),
                 BankTransaction.date.isnot(None),
                 BankTransaction.date >= cutoff))
    if limit_transaction_ids is not None:
        ids = list(limit_transaction_ids)
        if not ids:
            return []
        q = q.filter(BankTransaction.plaid_transaction_id.in_(ids))
    rows = q.order_by(BankTransaction.date.desc(),
                      BankTransaction.id.desc()).all()
    cache: dict = {}
    paired = _paired_ids()
    suppressed = _suppressed_keys()
    created = []
    for row in rows:
        if row.plaid_transaction_id in paired:
            continue        # claimed by a pair made earlier in this same pass
        found = find_transfer_pair(row, cache=cache, paired=paired,
                                   suppressed=suppressed)
        if found is None:
            continue
        other, confidence = found
        if confidence < threshold:
            log.info('intercompany candidate below threshold (%.2f < %.2f): '
                     '%s ↔ %s', confidence, threshold,
                     row.plaid_transaction_id, other.plaid_transaction_id)
            continue
        pair = record_pair(row, other, confidence, cache)
        paired.add(row.plaid_transaction_id)
        paired.add(other.plaid_transaction_id)
        created.append(pair)
    return created


# ── Journal Entry generation ───────────────────────────────────────────

def _bank_gl_for(transaction_id: str) -> str:
    """The ERPNext GL Account of the Plaid account a transaction landed in — the
    bank side of that leg's Journal Entry. '' when the account is unmapped or the
    import fell back to a personal account (no GL link)."""
    row = BankTransaction.query.filter_by(
        plaid_transaction_id=transaction_id).first()
    if row is None:
        return ''
    acct = PlaidAccount.query.filter_by(account_id=row.account_id).first()
    return ((acct.erpnext_gl_account_name or '').strip() if acct else '')


def _leg_doc(company: str, debit_account: str, credit_account: str,
             amount: float, posting_date, remark: str,
             bank_transaction: str = '') -> dict:
    """One side of the pair as an ERPNext Journal Entry payload — a plain
    two-line Dr/Cr that balances by construction."""
    debit_line = {'account': debit_account,
                  'debit_in_account_currency': amount}
    credit_line = {'account': credit_account,
                   'credit_in_account_currency': amount}
    accounts = [debit_line, credit_line]
    if bank_transaction:
        for line in accounts:
            line['reference_type'] = 'Bank Transaction'
            line['reference_name'] = bank_transaction
    doc = {'doctype': JOURNAL_ENTRY_DT, 'voucher_type': 'Journal Entry',
           'company': company, 'user_remark': remark, 'accounts': accounts}
    if posting_date:
        doc['posting_date'] = posting_date.isoformat()
    return doc


def build_pair_documents(pair: IntercompanyTransferPair,
                         due_from_account: str, due_to_account: str) -> tuple:
    """The two Journal Entry payloads for a pair, as (source_doc, target_doc).

        source Company:  Dr  Due from <target>     Cr  <source bank>
        target Company:  Dr  <target bank>         Cr  Due to <source>

    Read together, the source records "the target Company now owes me this" while
    the target records "I now owe the source Company this", and the two
    receivable/payable balances cancel when the entities are consolidated.
    Neither entry touches an Income or Expense account, which is the entire point.

    Pure — no ERPNext calls, no writes — so the shape of both entries is
    assertable in a test without a live instance."""
    source_row = BankTransaction.query.filter_by(
        plaid_transaction_id=pair.from_transaction_id).first()
    target_row = BankTransaction.query.filter_by(
        plaid_transaction_id=pair.to_transaction_id).first()
    amount = round(abs(float(pair.amount or 0.0)), 2)
    source_remark = (f'Intercompany transfer to {pair.to_company} — '
                     f'{(source_row.name if source_row else "") or "transfer"}')
    target_remark = (f'Intercompany transfer from {pair.from_company} — '
                     f'{(target_row.name if target_row else "") or "transfer"}')
    source_doc = _leg_doc(
        pair.from_company, due_from_account,
        _bank_gl_for(pair.from_transaction_id), amount,
        source_row.date if source_row else None, source_remark,
        (source_row.erpnext_bank_transaction_id if source_row else '') or '')
    target_doc = _leg_doc(
        pair.to_company, _bank_gl_for(pair.to_transaction_id),
        due_to_account, amount,
        target_row.date if target_row else None, target_remark,
        (target_row.erpnext_bank_transaction_id if target_row else '') or '')
    return source_doc, target_doc


def _delete_draft(client, name: str) -> bool:
    """Delete a Draft Journal Entry in ERPNext. Used only to unwind the first leg
    when the second fails — a Draft has posted nothing, so deleting it is the
    clean undo. Returns whether it went; never raises, because it runs inside an
    error path that must still record the original failure."""
    try:
        client.call_method('frappe.client.delete', http_method='POST',
                           json_body={'doctype': JOURNAL_ENTRY_DT, 'name': name})
        return True
    except (ERPNextAPIError, ERPNextError):
        log.warning('could not delete half-created intercompany JE %s — it is '
                    'left as an unsubmitted Draft', name, exc_info=True)
        return False


def _record_leg(pair: IntercompanyTransferPair, transaction_id: str,
                je_name: str, doc: dict) -> None:
    """Upsert the GeneratedJournalEntry audit row for one leg. Reuses any
    existing row for the transaction — that table is UNIQUE on
    plaid_transaction_id, so when a pair supersedes a rules-engine draft the row
    is re-pointed rather than duplicated (the AuditEvent trail keeps the history
    of what it used to be)."""
    row = BankTransaction.query.filter_by(
        plaid_transaction_id=transaction_id).first()
    gje = GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=transaction_id).first()
    if gje is None:
        gje = GeneratedJournalEntry(plaid_transaction_id=transaction_id)
        db.session.add(gje)
    gje.rule_id = None
    gje.rule_name = 'Intercompany transfer'
    gje.intercompany_pair_id = pair.id
    gje.erpnext_journal_entry_name = je_name
    gje.state = 'pending_review'
    gje.amount = round(abs(float(pair.amount or 0.0)), 2)
    gje.merchant_name = ((row.merchant_name if row else '') or '')[:255]
    gje.description = (doc.get('user_remark') or '')
    gje.error_message = None
    gje.updated_at = _now()


def _existing_rule_entry(transaction_id: str):
    """A rules-engine GeneratedJournalEntry already booked for this transaction —
    i.e. one carrying an ERPNext JE that is NOT part of this feature. None when
    the leg is clean."""
    gje = GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=transaction_id).first()
    if gje is None or not gje.erpnext_journal_entry_name:
        return None
    if gje.intercompany_pair_id:
        return None
    return gje


def _supersede_rule_entries(client, pair: IntercompanyTransferPair) -> str:
    """Clear the way for a pair's own entries when a normal rule already booked
    one of its legs. Returns '' when the pair may proceed, or the operator-facing
    reason it may not.

    WHY THIS EXISTS. The two legs of a transfer usually arrive on DIFFERENT sync
    runs, because each Plaid Item advances its own cursor. So the first leg can
    be pushed, categorized by a generic Transfer rule, and given a Draft JE
    minutes before its counterparty ever lands. Only when the second leg arrives
    does the pair become visible — by which point one entity's books already
    carry a rules-engine entry for money that was never revenue or expense.

    A Draft has posted nothing to the ledger, so it is deleted and the pair's own
    entry replaces it. A SUBMITTED entry is a different matter: it is real
    posted activity, and silently cancelling it behind the operator's back would
    be a worse failure than the double-count. Those are refused with an
    explanation, leaving the pair in the review queue with a Retry."""
    blocked = []
    drafts = []
    for tid in (pair.from_transaction_id, pair.to_transaction_id):
        gje = _existing_rule_entry(tid)
        if gje is None:
            continue
        if gje.state == 'pending_review':
            drafts.append(gje)
        elif gje.state in ('approved', 'reversed'):
            blocked.append(gje)
    if blocked:
        names = ', '.join(g.erpnext_journal_entry_name for g in blocked)
        return (f'A rules-engine Journal Entry ({names}) was already submitted '
                f'for one side of this transfer. Reverse or reject it on the '
                f'Generated JEs page, then Retry — Bank Bridge will not cancel '
                f'a posted entry on its own.')
    for gje in drafts:
        name = gje.erpnext_journal_entry_name
        _delete_draft(client, name)
        log.info('superseding rules-engine Draft %s (rule “%s”) with '
                 'intercompany pair #%s', name, gje.rule_name or '', pair.id)
        audit.record('intercompany_superseded_rule_entry',
                     subject_type='GeneratedJournalEntry', subject_id=gje.id,
                     before=gje.to_dict(),
                     after={'pair_id': pair.id},
                     notes=(f'Draft {name} from rule “{gje.rule_name or ""}” '
                            f'abandoned — transaction is one leg of '
                            f'intercompany pair #{pair.id}'))
    return ''


def _fail_pair(pair: IntercompanyTransferPair, message: str) -> None:
    """Record a generation failure on the pair without changing its state: it
    stays `pending`, so the operator sees it in the review queue with the reason
    and a Retry, rather than it vanishing or silently booking half an entry."""
    pair.note = message[:2000]
    pair.updated_at = _now()
    db.session.commit()
    log.warning('intercompany pair #%s could not be booked: %s', pair.id, message)
    audit.record('intercompany_pair_failed',
                 subject_type='IntercompanyTransferPair', subject_id=pair.id,
                 after={'pair': pair.to_dict(), 'error': message[:2000]},
                 notes=message[:500])


def generate_pair_journal_entries(client, pair: IntercompanyTransferPair):
    """Book both halves of one intercompany transfer in ERPNext. Returns
    (source_je_name, target_je_name), or None when nothing was booked.

    ATOMIC ACROSS TWO COMPANIES, which is the hard part and the reason this
    doesn't just call the rules engine twice. Two Journal Entries in two
    different Companies cannot share a database transaction in ERPNext, so
    atomicity is enforced by ordering and compensation:

      1. resolve BOTH control accounts and BOTH bank accounts up front, and
         refuse the whole pair if any is missing. Most failures are configuration
         problems, and this catches them before anything is written.
      2. create the source leg as a Draft.
      3. create the target leg. If THAT fails, delete the source Draft before
         recording the failure — a Draft has posted nothing to the ledger, so
         the books are exactly as they were.

    The result is that the operator's books are never half-updated: either both
    entries exist as reviewable Drafts, or neither does and the pair carries the
    reason why. Idempotent — a pair that already names both JEs returns them
    untouched, so a retry or a re-run never double-books."""
    if pair is None:
        return None
    if pair.from_journal_entry and pair.to_journal_entry:
        return pair.from_journal_entry, pair.to_journal_entry
    if pair.state == 'rejected':
        return None
    if client is None:
        _fail_pair(pair, 'ERPNext is not configured — cannot book the transfer')
        return None
    if not pair.from_company or not pair.to_company:
        _fail_pair(pair, 'Both Companies must be resolvable to book an '
                         'intercompany transfer')
        return None

    # 1 · everything both legs need, resolved before anything is written.
    try:
        due_from, due_to = erpnext_accounts.ensure_intercompany_accounts(
            client, pair.from_company, pair.to_company)
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        pair = db.session.get(IntercompanyTransferPair, pair.id)
        if pair is not None:
            _fail_pair(pair, f'Could not provision the intercompany accounts: {e}')
        return None
    if not due_from or not due_to:
        _fail_pair(pair,
                   f'Could not provision “Due from {pair.to_company}” under '
                   f'{pair.from_company} and “Due to {pair.from_company}” under '
                   f'{pair.to_company}. Check that both Companies have a Chart '
                   f'of Accounts.')
        return None
    # A rules-engine entry may already exist for one leg (the two legs routinely
    # arrive on different sync runs) — abandon a Draft, refuse on a submitted one.
    blocked = _supersede_rule_entries(client, pair)
    if blocked:
        _fail_pair(pair, blocked)
        return None
    source_doc, target_doc = build_pair_documents(pair, due_from, due_to)
    missing = [d['company'] for d in (source_doc, target_doc)
               if not all((ln.get('account') or '').strip()
                          for ln in d['accounts'])]
    if missing:
        _fail_pair(pair,
                   'Both bank accounts must be linked to a GL Account before an '
                   'intercompany transfer can be booked; missing on '
                   + ', '.join(missing) + '. Import the account on the Accounts '
                   'page, then Retry.')
        return None

    # 2 · the source leg.
    try:
        created = client.create_doc(JOURNAL_ENTRY_DT, source_doc)
        source_name = created.get('name') if isinstance(created, dict) else None
        if not source_name:
            raise ERPNextAPIError('ERPNext returned no Journal Entry name',
                                  status_code=None)
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        pair = db.session.get(IntercompanyTransferPair, pair.id)
        if pair is not None:
            _fail_pair(pair, f'{pair.from_company} entry failed: {e}')
        return None

    # 3 · the target leg — and unwind the source if it doesn't land.
    try:
        created = client.create_doc(JOURNAL_ENTRY_DT, target_doc)
        target_name = created.get('name') if isinstance(created, dict) else None
        if not target_name:
            raise ERPNextAPIError('ERPNext returned no Journal Entry name',
                                  status_code=None)
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        removed = _delete_draft(client, source_name)
        pair = db.session.get(IntercompanyTransferPair, pair.id)
        if pair is not None:
            _fail_pair(pair,
                       f'{pair.to_company} entry failed: {e} — rolled back the '
                       f'{pair.from_company} entry'
                       + ('' if removed else f' (Draft {source_name} could not '
                                             f'be deleted; cancel it manually)'))
        return None

    pair.from_journal_entry = source_name
    pair.to_journal_entry = target_name
    pair.note = None
    pair.updated_at = _now()
    _record_leg(pair, pair.from_transaction_id, source_name, source_doc)
    _record_leg(pair, pair.to_transaction_id, target_name, target_doc)
    db.session.commit()
    log.info('intercompany pair #%s booked: %s (%s) / %s (%s)', pair.id,
             source_name, pair.from_company, target_name, pair.to_company)
    audit.record('intercompany_journal_entries_generated',
                 subject_type='IntercompanyTransferPair', subject_id=pair.id,
                 after={'pair': pair.to_dict(), 'source_doc': source_doc,
                        'target_doc': target_doc},
                 notes=f'{source_name} + {target_name}')
    return source_name, target_name


def generate_pending_journal_entries(client) -> int:
    """Book every detected-but-unbooked pair. Returns how many were booked.
    Each pair is independent — one failure records itself on that pair and the
    pass moves on."""
    if client is None:
        return 0
    pairs = (IntercompanyTransferPair.query
             .filter(IntercompanyTransferPair.state == 'pending',
                     IntercompanyTransferPair.from_journal_entry.is_(None))
             .order_by(IntercompanyTransferPair.id).all())
    booked = 0
    for pair in pairs:
        try:
            if generate_pair_journal_entries(client, pair):
                booked += 1
        except Exception:  # noqa: BLE001 — never fail a sync on one bad pair
            db.session.rollback()
            log.warning('intercompany JE generation failed for pair #%s',
                        pair.id, exc_info=True)
    return booked


def run_detection(client=None, *, limit_transaction_ids=None) -> dict:
    """The sync-path entry point: detect pairs, then book the ones that need it.
    Returns {'detected': n, 'booked': n}. Never raises — intercompany handling is
    an enhancement to the sync, never a reason for one to fail."""
    stats = {'detected': 0, 'booked': 0}
    try:
        detected = detect_pairs(limit_transaction_ids=limit_transaction_ids)
        stats['detected'] = len(detected)
    except Exception:  # noqa: BLE001 — defensive
        db.session.rollback()
        log.warning('intercompany detection pass failed', exc_info=True)
        return stats
    try:
        stats['booked'] = generate_pending_journal_entries(client)
    except Exception:  # noqa: BLE001 — defensive
        db.session.rollback()
        log.warning('intercompany JE generation pass failed', exc_info=True)
    return stats


# ── review actions ─────────────────────────────────────────────────────

def _submit(client, name: str) -> None:
    """Submit a Draft Journal Entry by name. Delegates to the categorization
    module's submitter, which fetches the stored document first — handing Frappe
    a bare {doctype, name} stub asks it to submit an EMPTY entry, which is the
    v0.4.0.4 'Approve does nothing' bug."""
    from . import categorization
    categorization._submit_je(client, name)


def _cancel(client, name: str) -> None:
    client.call_method('frappe.client.cancel', http_method='POST',
                       json_body={'doctype': JOURNAL_ENTRY_DT, 'name': name})


def _sync_leg_states(pair: IntercompanyTransferPair, state: str) -> None:
    """Mirror a pair-level decision onto both legs' GeneratedJournalEntry rows,
    so the Generated JEs page never disagrees with the Intercompany page."""
    for gje in GeneratedJournalEntry.query.filter_by(
            intercompany_pair_id=pair.id).all():
        gje.state = state
        gje.updated_at = _now()


def approve_pair(client, pair: IntercompanyTransferPair) -> tuple:
    """Submit BOTH Journal Entries in ERPNext and move the pair to `approved`.
    Returns (ok, message).

    Atomic in the same compensating sense as generation: if the second submit
    fails, the first is cancelled so the two entities' books stay in step. A
    submitted-then-cancelled JE is visible in ERPNext (cancellation is not
    deletion), which is the correct audit trail for a partial failure — the
    alternative, leaving one entity's transfer posted and the other's not, is a
    reconciliation problem that surfaces months later."""
    if pair is None:
        return False, 'Pair not found'
    if pair.state == 'approved':
        return True, 'Already approved'
    if pair.state == 'rejected':
        return False, 'Cannot approve a rejected (unpaired) transfer'
    if not pair.from_journal_entry or not pair.to_journal_entry:
        return False, ('Both Journal Entries must be generated before the '
                       'transfer can be approved')
    if client is None:
        return False, 'ERPNext is not configured — check the connection'
    try:
        _submit(client, pair.from_journal_entry)
    except (ERPNextAPIError, ERPNextError) as e:
        return False, f'ERPNext refused the {pair.from_company} entry: {e}'
    try:
        _submit(client, pair.to_journal_entry)
    except (ERPNextAPIError, ERPNextError) as e:
        undone = True
        try:
            _cancel(client, pair.from_journal_entry)
        except (ERPNextAPIError, ERPNextError):
            undone = False
            log.warning('could not cancel %s after its counterpart failed to '
                        'submit', pair.from_journal_entry, exc_info=True)
        return False, (f'ERPNext refused the {pair.to_company} entry: {e}'
                       + (f' — cancelled the {pair.from_company} entry'
                          if undone else
                          f' — and the {pair.from_company} entry '
                          f'({pair.from_journal_entry}) could NOT be cancelled; '
                          f'cancel it manually'))
    before = pair.to_dict()
    pair.state = 'approved'
    pair.note = None
    pair.updated_at = _now()
    _sync_leg_states(pair, 'approved')
    audit.record('intercompany_pair_approved',
                 subject_type='IntercompanyTransferPair', subject_id=pair.id,
                 before=before, after=pair.to_dict(),
                 notes=(f'submitted {pair.from_journal_entry} + '
                        f'{pair.to_journal_entry} in ERPNext'), commit=False)
    return True, 'Approved — both Journal Entries submitted in ERPNext'


def reject_pair(client, pair: IntercompanyTransferPair) -> tuple:
    """Unpair: cancel whatever was booked, move the pair to `rejected`, and
    return both transactions to ordinary categorization. Returns (ok, message).

    The reversal for a detection that was WRONG — two unrelated transactions of
    the same size a few days apart. So it undoes everything: a submitted JE is
    cancelled in ERPNext, a Draft is abandoned, both legs' audit rows go to
    `rejected`, and both transactions have their `intercompany_pair_id` cleared
    so the normal rules can categorize them on the next Rerun.

    The pair ROW is kept, not deleted, and that is deliberate — it is the record
    that stops the detector re-pairing the same two transactions on the very next
    sync (see _suppressed_keys)."""
    if pair is None:
        return False, 'Pair not found'
    if pair.state == 'rejected':
        return True, 'Already unpaired'
    cancelled = []
    if pair.state == 'approved':
        if client is None:
            return False, ('ERPNext is not configured — cannot cancel the '
                           'submitted Journal Entries')
        for name in (pair.from_journal_entry, pair.to_journal_entry):
            if not name:
                continue
            try:
                _cancel(client, name)
            except (ERPNextAPIError, ERPNextError) as e:
                return False, f'ERPNext refused to cancel {name}: {e}'
            cancelled.append(name)
    before = pair.to_dict()
    pair.state = 'rejected'
    pair.updated_at = _now()
    _sync_leg_states(pair, 'rejected')
    for tid in (pair.from_transaction_id, pair.to_transaction_id):
        row = BankTransaction.query.filter_by(plaid_transaction_id=tid).first()
        if row is not None:
            row.intercompany_pair_id = None
            row.updated_at = _now()
    audit.record('intercompany_pair_rejected',
                 subject_type='IntercompanyTransferPair', subject_id=pair.id,
                 before=before, after=pair.to_dict(),
                 notes=('unpaired'
                        + (f' — cancelled {", ".join(cancelled)}'
                           if cancelled else ' — Drafts abandoned')),
                 commit=False)
    return True, ('Unpaired — both transactions are eligible for normal rules'
                  + (f' · cancelled {len(cancelled)} Journal Entr'
                     f'{"y" if len(cancelled) == 1 else "ies"}'
                     if cancelled else ''))


def retry_pair(client, pair: IntercompanyTransferPair) -> tuple:
    """Re-attempt generation for a detected pair that failed to book (the
    operator has presumably fixed the missing account / mapping). Returns
    (ok, message)."""
    if pair is None:
        return False, 'Pair not found'
    if pair.state == 'rejected':
        return False, 'Cannot retry an unpaired transfer'
    if pair.from_journal_entry and pair.to_journal_entry:
        return True, 'Already booked'
    if client is None:
        return False, 'ERPNext is not configured — check the connection'
    result = generate_pair_journal_entries(client, pair)
    if result:
        return True, 'Booked — both Journal Entries generated, pending review'
    return False, (pair.note or 'Still could not book this transfer')
