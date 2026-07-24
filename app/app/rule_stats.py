# SPDX-License-Identifier: MIT
"""Rule-authoring support: transaction rule-state filtering, unmatched grouping,
and the cached per-rule match count (v0.4.6).

All of this is LOCAL — it reads the transaction mirror and the
GeneratedJournalEntry audit table, never ERPNext. That is deliberate: the whole
point of the guided authoring workflow is that a new operator can sit on the
Transactions tab immediately after the first sync and see which merchants still
have no rule, without a round trip to ERPNext for every page load.

Three pieces:

  * `apply_state_filter` — the Transactions tab's rule-state filter (unmatched /
    matched / JE error / JE cancelled). Orthogonal to the existing `status`
    filter, which is about the Plaid→ERPNext *sync* of the Bank Transaction; this
    one is about whether the rules engine did anything with it afterwards.
  * `group_unmatched` — collapses an unmatched list into per-merchant groups so
    "12 from Uber Eats" is one rule to write instead of twelve.
  * `rollup_match_counts` — the daily job behind the Rules list's Match Count
    column.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

from sqlalchemy.orm.attributes import flag_modified

from . import db
from .models import BankTransaction, CategorizationRule, GeneratedJournalEntry

log = logging.getLogger('bankbridge.rule_stats')


# ── rule-state filter ───────────────────────────────────────────────────────

# GeneratedJournalEntry.state, partitioned for the Transactions filter. Together
# with "no GeneratedJournalEntry row at all" (= unmatched) these cover every
# state the JE lifecycle can produce, so the four filters partition the eligible
# transactions rather than overlapping.
JE_LIVE_STATES = ('pending_review', 'approved')
JE_ERROR_STATES = ('error', 'blocked', 'skipped_missing_account')
JE_CANCELLED_STATES = ('rejected', 'reversed')

# The filter values the Transactions tab accepts, with their labels. '' = no
# filter (every transaction, eligible or not) and stays the default.
STATE_FILTERS = (
    ('', '(all)'),
    ('matched', 'Rule matched'),
    ('unmatched', 'Unmatched'),
    ('je_error', 'JE error'),
    ('je_cancelled', 'JE cancelled'),
)
STATE_FILTER_VALUES = tuple(v for v, _ in STATE_FILTERS if v)


def is_state_filter(state: str) -> bool:
    """True when `state` is one of the four real filters (blank is not — it means
    no filtering, which callers handle by skipping apply_state_filter)."""
    return (state or '').strip() in STATE_FILTER_VALUES


def eligible_filter():
    """The SQLAlchemy criteria for a transaction the rules engine would actually
    consider: posted to ERPNext and not removed.

    Deliberately the SAME predicate as the "Rerun rules" button
    (admin_ui.rerun_rules), because the two have to agree: a row listed as
    Unmatched must be one that a Rerun would genuinely re-evaluate. A pending or
    removed transaction has never been offered to the engine, so calling it
    "unmatched" would send the operator off writing rules for rows no rule can
    fire on yet."""
    return (BankTransaction.posted_at.isnot(None),
            BankTransaction.removed.is_(False))


def _tx_ids_in_je_states(states):
    """SUBQUERY of the plaid_transaction_ids whose GeneratedJournalEntry sits in
    `states`.

    Deliberately a subquery and not a materialized list: an install with tens of
    thousands of generated entries would otherwise build an equally large SQL
    `IN (...)` list, which Postgres caps (~32k bind parameters) and which is slow
    long before it errors."""
    return (db.session.query(GeneratedJournalEntry.plaid_transaction_id)
            .filter(GeneratedJournalEntry.state.in_(tuple(states))))


def tx_ids_with_je():
    """SUBQUERY of every plaid_transaction_id that has a GeneratedJournalEntry
    row at all — whatever its state. The complement of this set, within the
    eligible rows, is what "unmatched" means: the engine looked and no rule
    fired."""
    return db.session.query(GeneratedJournalEntry.plaid_transaction_id)


def tx_ids_with_je_among(transaction_ids) -> set:
    """The subset of `transaction_ids` that already have a GeneratedJournalEntry.

    For the per-row "+ Rule" decision on a single rendered page: bounded by the
    page's own row count rather than by the size of the whole entry table."""
    ids = list(transaction_ids)
    if not ids:
        return set()
    return {r[0] for r in
            db.session.query(GeneratedJournalEntry.plaid_transaction_id)
            .filter(GeneratedJournalEntry.plaid_transaction_id.in_(ids))}


def apply_state_filter(q, state: str):
    """Narrow a BankTransaction query to one rule-state. Returns `q` untouched
    for an unrecognized/blank state, so a hand-typed query string degrades to
    "no filter" rather than to an error page.

    Every branch also applies `eligible_filter()`: all four states are
    statements about what the rules engine did with a transaction, which is only
    meaningful once it has been posted."""
    st = (state or '').strip()
    if not is_state_filter(st):
        return q
    q = q.filter(*eligible_filter())
    if st == 'unmatched':
        return q.filter(BankTransaction.plaid_transaction_id.notin_(
            tx_ids_with_je()))
    if st == 'matched':
        return q.filter(BankTransaction.plaid_transaction_id.in_(
            _tx_ids_in_je_states(JE_LIVE_STATES)))
    if st == 'je_error':
        return q.filter(BankTransaction.plaid_transaction_id.in_(
            _tx_ids_in_je_states(JE_ERROR_STATES)))
    # je_cancelled
    return q.filter(BankTransaction.plaid_transaction_id.in_(
        _tx_ids_in_je_states(JE_CANCELLED_STATES)))


# ── unmatched grouping ──────────────────────────────────────────────────────

# Tokens stripped from a description before it is used as a grouping key: card
# and transaction reference numbers, dates and store numbers are exactly what
# makes two visits to the same merchant look like two different merchants.
_DESC_NOISE = re.compile(r'[0-9]+|[^A-Za-z ]+')
_DESC_TOKENS = 3
# A group has to be worth a rule. A single transaction under a description
# signature is a one-off (a cheque, a one-time transfer), and burying it in a
# collapsed group of one would hide it — those go to the ungrouped list.
MIN_GROUP_SIZE = 2


def description_signature(description: str, tokens: int = _DESC_TOKENS) -> str:
    """A coarse grouping key for a transaction with no merchant name: the first
    few alphabetic words of the description, uppercased.

    'SQ *BLUE BOTTLE 4471 SEATTLE WA' and 'SQ *BLUE BOTTLE 8890 PORTLAND OR'
    both reduce to 'SQ BLUE BOTTLE', which is the substring a
    description-based rule would want to match on."""
    cleaned = _DESC_NOISE.sub(' ', description or '')
    parts = [p for p in cleaned.split() if p]
    return ' '.join(parts[:tokens]).upper()


def group_key(row) -> tuple:
    """(kind, key, label) for one transaction, where kind is 'merchant' when
    Plaid gave us a merchant name and 'description' when we had to fall back to
    the description signature. Returns ('', '', '') for a row with neither,
    which the caller sends straight to the ungrouped list."""
    merchant = (getattr(row, 'merchant_name', '') or '').strip()
    if merchant:
        return ('merchant', merchant.lower(), merchant)
    sig = description_signature(getattr(row, 'name', '') or '')
    if sig:
        return ('description', sig.lower(), sig)
    return ('', '', '')


def group_unmatched(rows, min_size: int = MIN_GROUP_SIZE) -> tuple:
    """Collapse unmatched transactions into (groups, ungrouped).

    `groups` is a list of dicts — {kind, key, label, count, total, rows} —
    ordered by count descending then label, so the merchant costing the operator
    the most repeated manual work is the first rule they are invited to write.
    Anything below `min_size`, and anything with neither a merchant nor a usable
    description, falls through to `ungrouped` in the caller's original order."""
    buckets: dict = {}
    order: list = []
    loose: list = []
    for row in rows:
        kind, key, label = group_key(row)
        if not key:
            loose.append(row)
            continue
        if key not in buckets:
            buckets[key] = {'kind': kind, 'key': key, 'label': label,
                            'count': 0, 'total': 0.0, 'rows': []}
            order.append(key)
        b = buckets[key]
        b['count'] += 1
        b['total'] += abs(float(getattr(row, 'amount', 0.0) or 0.0))
        b['rows'].append(row)
    groups = []
    for key in order:
        b = buckets[key]
        if b['count'] >= min_size:
            groups.append(b)
        else:
            loose.extend(b['rows'])
    groups.sort(key=lambda g: (-g['count'], g['label'].lower()))
    # Restore the caller's ordering for the leftovers — they arrived sorted by
    # date and the grouping pass shouldn't shuffle them.
    seen = {id(r): i for i, r in enumerate(rows)}
    loose.sort(key=lambda r: seen.get(id(r), 0))
    return groups, loose


def prefill_for(row) -> dict:
    """The Rules-editor prefill for "create a rule from THIS transaction": an
    exact merchant match when Plaid identified the merchant, else a regex on the
    description signature.

    merchant_exact (not _contains) is the single-transaction default on purpose —
    it is the narrowest rule that certainly covers the row in front of the
    operator, and narrowing is easier to reason about than accidentally catching
    a neighbour. The GROUP button (prefill_for_group) is where _contains belongs,
    because there the operator has already seen the whole set it will catch."""
    merchant = (getattr(row, 'merchant_name', '') or '').strip()
    if merchant:
        return {'match_type': 'merchant_exact', 'match_value': merchant,
                'name': merchant}
    sig = description_signature(getattr(row, 'name', '') or '')
    if sig:
        return {'match_type': 'description_regex', 'match_value': re.escape(sig),
                'name': sig.title()}
    return {'match_type': 'description_regex', 'match_value': '', 'name': ''}


def prefill_for_group(group: dict) -> dict:
    """The Rules-editor prefill for "create a rule from this GROUP" — a
    merchant_contains rule (or a description regex for a description-keyed
    group) broad enough to clear every row the operator just expanded."""
    label = (group or {}).get('label') or ''
    if (group or {}).get('kind') == 'description':
        return {'match_type': 'description_regex',
                'match_value': re.escape(label), 'name': label.title()}
    return {'match_type': 'merchant_contains', 'match_value': label,
            'name': label}


# ── match-count rollup ──────────────────────────────────────────────────────

def _current_version(rule_id: int, by_id: dict) -> int | None:
    """Walk `superseded_by` forward from `rule_id` to the live rule that replaced
    it, so an archived version's matches are credited to the rule the operator
    is actually looking at. Returns None when the id names no rule at all.

    Cycle-guarded: superseded_by is written by save_rule and should always point
    strictly forward, but a loop here would hang the rollup thread, and a rollup
    is never worth a hung scheduler."""
    seen = set()
    cur = rule_id
    while cur is not None and cur not in seen:
        seen.add(cur)
        rule = by_id.get(cur)
        if rule is None:
            return None if cur == rule_id else cur
        nxt = rule.superseded_by
        if nxt is None or nxt == cur:
            return cur
        cur = nxt
    return cur


def match_counts() -> dict:
    """{rule_id: match_count} computed live from the GeneratedJournalEntry table,
    with each archived version's matches folded into its live successor. Pure
    read — `rollup_match_counts` is what persists it."""
    raw: dict = defaultdict(int)
    for (rule_id,) in (db.session.query(GeneratedJournalEntry.rule_id)
                       .filter(GeneratedJournalEntry.rule_id.isnot(None))):
        raw[rule_id] += 1
    by_id = {r.id: r for r in CategorizationRule.query.all()}
    resolved: dict = defaultdict(int)
    for rule_id, n in raw.items():
        head = _current_version(rule_id, by_id)
        if head is not None and head in by_id:
            resolved[head] += n
    return dict(resolved)


def active_match_counts() -> dict:
    """{head_rule_id: matches generated SINCE that rule was last switched ON} —
    the "currently active" count beside the lifetime `match_counts` (v0.5.9).

    A JE counts only when its winning rule (resolved to its live head) is ON
    *and* the JE was generated at or after the head's `activated_at`. A rule
    that is OFF, or ON but has generated nothing since it was last activated,
    reports 0 — which is why the number reads as "is this rule doing anything
    now". Version-resolved and cycle-guarded exactly like `match_counts`; the
    per-JE timestamp comparison is why this iterates rows rather than counting
    in SQL. Pure read."""
    by_id = {r.id: r for r in CategorizationRule.query.all()}
    resolved: dict = defaultdict(int)
    rows = (db.session.query(GeneratedJournalEntry.rule_id,
                             GeneratedJournalEntry.created_at)
            .filter(GeneratedJournalEntry.rule_id.isnot(None)))
    for rule_id, created_at in rows:
        head = _current_version(rule_id, by_id)
        if head is None or head not in by_id:
            continue
        rule = by_id[head]
        if not rule.active:
            continue
        since = rule.activated_at or rule.created_at
        created = _naive(created_at)
        since = _naive(since)
        if created is not None and since is not None and created >= since:
            resolved[head] += 1
    return dict(resolved)


def _naive(dt):
    """Drop tzinfo so a DB-read naive timestamp and an in-session tz-aware one
    (SQLite returns naive; a just-set attribute is aware) compare without
    raising. Both are UTC by construction (models._now)."""
    return dt.replace(tzinfo=None) if (dt is not None and dt.tzinfo) else dt


def rollup_match_counts() -> dict:
    """Refresh `CategorizationRule.match_count` for every rule from the local
    GeneratedJournalEntry table.

    Same shape as the v0.4.5 Counterparty rollup: one read, then a write only
    for the rules whose number actually moved — so the steady state on an
    unchanged ledger is zero writes. Local-only and cheap enough to also run
    inline right after "Rerun rules", which is where the count would otherwise
    be visibly stale.

    Returns {'scanned', 'updated', 'skipped'}. Never raises."""
    result = {'scanned': 0, 'updated': 0, 'skipped': 0}
    counts = match_counts()
    changed = False
    for rule in CategorizationRule.query.all():
        result['scanned'] += 1
        wanted = int(counts.get(rule.id, 0))
        if int(rule.match_count or 0) == wanted:
            result['skipped'] += 1
            continue
        # Preserve updated_at across the write. It carries "when did the operator
        # last change this rule" for the archived-rules list and the audit trail,
        # and a background counter refresh is not an edit — without this the
        # column's onupdate would restamp every rule the first time the rollup
        # ran.
        #
        # flag_modified is required, not decoration: re-assigning the attribute
        # its OWN value records no change history, so SQLAlchemy would leave
        # updated_at out of the UPDATE's SET clause and onupdate would fill it in
        # anyway. Flagging it dirty forces the explicit value into the statement.
        rule.match_count = wanted
        flag_modified(rule, 'updated_at')
        changed = True
        result['updated'] += 1
    if changed:
        db.session.commit()
    log.info('[rule_stats] match-count rollup: %s', result)
    return result
