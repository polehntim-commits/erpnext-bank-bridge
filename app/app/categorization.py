# SPDX-License-Identifier: MIT
"""Categorization rules engine + Journal Entry generator (v0.3.0).

Given a local `BankTransaction` that's just been posted to ERPNext, walk the
active `CategorizationRule` rows in priority order (lower = higher priority) and
let the FIRST match generate an ERPNext Journal Entry:

  * the rule supplies only the OFFSET (categorized) account; the BANK side comes
    from the transaction's own linked Plaid account (v0.3.1 — rules are
    bank-account-agnostic, so one rule works across every account)
  * `offset_direction` decides which side the offset lands on: 'auto' infers it
    from the Plaid amount sign (withdrawal → offset debited; deposit/refund →
    offset credited), while 'always_debit' / 'always_credit' force it (rare)
  * pre-v0.3.1 rules that still carry a debit/credit pair keep working via a
    legacy branch (see build_journal_entry) for one release cycle.

Every generated JE is recorded in `GeneratedJournalEntry`, whose UNIQUE
`plaid_transaction_id` is the idempotency guard: a transaction generates at most
one JE, so re-running the sync (or a retry) never double-posts.

Design guarantees:
  * Non-destructive — a rule/JE failure is caught, logged, and recorded as an
    `error` GeneratedJournalEntry; it never propagates to abort the Bank
    Transaction sync (the caller has already committed that).
  * Opt-in — generation only runs when ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES is
    True. Auto-Supplier creation (ERPNEXT_AUTO_CREATE_SUPPLIERS, default True)
    runs independently so transactions stay linkable even with the JE engine off.
  * Draft by default — JEs insert as docstatus 0 for review unless
    ERPNEXT_JOURNAL_ENTRY_AUTO_SUBMIT is True.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from flask import current_app, g

from . import audit
from . import db
from . import erpnext_bank
from . import erpnext_settings
from . import je_dedup
from .erpnext_client import ERPNextAPIError, ERPNextError
from .models import (BankTransaction, CategorizationRule,
                     GeneratedJournalEntry, PlaidAccount)

log = logging.getLogger('bankbridge.categorization')

JOURNAL_ENTRY_DT = 'Journal Entry'

# match_type values the engine understands.
MATCH_TYPES = ('merchant_exact', 'merchant_contains', 'description_regex',
               'plaid_category_matches', 'amount_range')

# offset_direction values (v0.3.1). 'auto' infers debit/credit from the amount
# sign; the two 'always_*' overrides force the offset side (rare — reversals).
OFFSET_DIRECTIONS = ('auto', 'always_debit', 'always_credit')

# v0.3.2 · short, human names for the common Plaid categories, used to suggest a
# rule Name from the transaction's category (e.g. a Chevron txn categorized
# "Transportation > Gas Stations" suggests the name "Fuel — Chevron"). Keys are
# the hierarchical Plaid path; matching is lenient (see category_alias) so a raw
# PFC label like "GAS_STATIONS" resolves to the same alias.
CATEGORY_ALIASES = {
    'Transportation > Gas Stations': 'Fuel',
    'Food and Drink > Restaurants > Coffee Shop': 'Coffee',
    'Food and Drink > Restaurants': 'Meals',
    'Food and Drink > Groceries': 'Groceries',
    'Food and Drink': 'Meals',
    'Rent and Utilities > Rent': 'Rent',
    'Rent and Utilities > Utilities': 'Utilities',
    'General Merchandise': 'Supplies',
    'Travel > Airlines': 'Travel',
    'Transportation': 'Transportation',
    'Entertainment': 'Entertainment',
    'Loan Payments': 'Loan Payment',
    'Bank Fees': 'Bank Fees',
    'Interest': 'Interest Income',
    'Payroll': 'Payroll',
    'Transfer': 'Transfer',
}


def _norm_category(s: str) -> str:
    """Collapse a category label/segment to a comparison key: lowercased, with
    every non-alphanumeric character (spaces, '>', ',', '_') stripped. So
    "Gas Stations", "gas_stations" and "GAS_STATIONS" all key the same."""
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


# Built lazily: normalized-key → alias. Both the full path and each key's last
# segment are indexed (full path wins) so a stored PFC label matches by leaf.
_ALIAS_INDEX = None


def _alias_index() -> dict:
    global _ALIAS_INDEX
    if _ALIAS_INDEX is None:
        idx = {}
        for path, alias in CATEGORY_ALIASES.items():
            segs = [p.strip() for p in re.split(r'[>,]', path) if p.strip()]
            if segs:                      # last segment first (weaker key)
                idx.setdefault(_norm_category(segs[-1]), alias)
        for path, alias in CATEGORY_ALIASES.items():
            idx[_norm_category(path)] = alias   # full path wins
        _ALIAS_INDEX = idx
    return _ALIAS_INDEX


def category_alias(category: str) -> str:
    """Short human name for a Plaid category, or '' if we have no alias. Tries the
    full path, then each segment (leaf first), so both "Transportation > Gas
    Stations" and a raw "GAS_STATIONS" resolve to "Fuel"."""
    cat = (category or '').strip()
    if not cat:
        return ''
    idx = _alias_index()
    hit = idx.get(_norm_category(cat))
    if hit:
        return hit
    parts = [p.strip() for p in re.split(r'[>,]', cat) if p.strip()]
    for p in reversed(parts):
        hit = idx.get(_norm_category(p))
        if hit:
            return hit
    return ''


def suggest_rule_name(match_value: str, category: str = '') -> str:
    """Suggest a rule Name from the match value + the merchant's category:
    "<alias> — <match>" (e.g. "Fuel — Chevron"). Falls back to just the match
    value when no alias is known, or the alias alone when there's no match."""
    alias = category_alias(category)
    mv = (match_value or '').strip()
    if alias and mv:
        return f'{alias} — {mv}'
    return mv or alias


def _overlap_facets(match_type: str, match_value: str) -> dict:
    """A representative transaction (as rule_matches kwargs) that the given
    predicate is meant to catch — used to detect whether OTHER rules also fire on
    the same input (conflict detection)."""
    mv = (match_value or '').strip()
    facets = {'merchant_name': '', 'description': '', 'category': '', 'amount': 0.0}
    if match_type in ('merchant_exact', 'merchant_contains'):
        facets['merchant_name'] = mv
    elif match_type == 'plaid_category_matches':
        facets['category'] = mv
    elif match_type == 'description_regex':
        facets['description'] = mv
    elif match_type == 'amount_range':
        rng = _amount_range(match_value)
        facets['amount'] = (rng[0] + rng[1]) / 2.0 if rng else 0.0
    return facets


def conflicting_rules(match_type: str, match_value: str, priority: int,
                      exclude_id=None) -> list:
    """ACTIVE, non-archived rules at the SAME or HIGHER priority (lower number)
    that already match the same input a new rule targets. Because the engine is
    first-match-wins in priority order, any such rule would shadow the new one.
    Returned in priority order (the winner first) so the caller can warn."""
    facets = _overlap_facets(match_type, match_value)
    if not any((facets['merchant_name'], facets['description'],
                facets['category'])) and match_type != 'amount_range':
        return []
    rules = (CategorizationRule.query
             .filter(CategorizationRule.active.is_(True),
                     CategorizationRule.archived.is_(False))
             .order_by(CategorizationRule.priority.asc(),
                       CategorizationRule.id.asc()).all())
    out = []
    for r in rules:
        if exclude_id is not None and r.id == exclude_id:
            continue
        if (r.priority or 0) <= priority and rule_matches(r, **facets):
            out.append(r)
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── rule matching ──────────────────────────────────────────────────────

def _amount_range(match_value: str):
    """Parse a '[min, max]' JSON array → (min, max) floats, or None if invalid."""
    try:
        arr = json.loads(match_value)
        if isinstance(arr, (list, tuple)) and len(arr) == 2:
            return float(arr[0]), float(arr[1])
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def rule_matches(rule: CategorizationRule, *, merchant_name: str = '',
                 description: str = '', category: str = '',
                 amount: float = 0.0) -> bool:
    """True when `rule` matches the given transaction facets. Pure + total —
    a malformed pattern (bad regex, bad amount_range JSON) matches nothing
    rather than raising, so one broken rule can't wedge the engine."""
    mt = rule.match_type
    mv = (rule.match_value or '')
    merchant = (merchant_name or '')
    if mt == 'merchant_exact':
        return bool(merchant) and merchant.strip().lower() == mv.strip().lower()
    if mt == 'merchant_contains':
        return bool(mv.strip()) and mv.strip().lower() in merchant.lower()
    if mt == 'description_regex':
        if not mv:
            return False
        try:
            return re.search(mv, description or '', re.IGNORECASE) is not None
        except re.error:
            return False
    if mt == 'plaid_category_matches':
        if not mv.strip():
            return False
        needle = mv.strip().lower()
        cat = (category or '')
        # `category` is the stored string — a PFC detailed label
        # ("GENERAL_MERCHANDISE") or a legacy 'A > B' path. Match the needle
        # against any split part or as a substring of the whole.
        parts = [p.strip().lower() for p in re.split(r'[>,]', cat) if p.strip()]
        return needle in parts or needle in cat.lower()
    if mt == 'amount_range':
        rng = _amount_range(mv)
        if rng is None:
            return False
        lo, hi = rng
        return lo <= abs(float(amount or 0.0)) <= hi
    return False


def _rule_eligible_for_paired(rule, row) -> bool:
    """Whether a rule may fire on this transaction given its intercompany status
    (v0.4.1). A transaction the detector has paired is booked through the Due
    from / Due to entries instead, so a rule carrying `ignore_for_paired` (the
    default) is skipped for it — otherwise a generic "Transfer" rule would ALSO
    book one leg to P&L and the transfer would be counted twice.

    An unpaired transaction is always eligible, which is every transaction on a
    single-Company install."""
    if not getattr(row, 'intercompany_pair_id', None):
        return True
    return not getattr(rule, 'ignore_for_paired', True)


def _rule_applies_to_company(rule, row_company: str) -> bool:
    """Whether a company-scoped rule is in scope for this transaction. A rule
    with a blank `applies_to_company` is company-agnostic and always applies
    (v0.3.x behavior); a scoped rule applies only when the transaction's linked
    account resolves to that same Company."""
    scope = (getattr(rule, 'applies_to_company', None) or '').strip()
    return not scope or scope == row_company


def evaluate_rules(row):
    """Walk the ACTIVE, non-archived rules in priority order and return
    (winner_or_None, trace). `trace` is the ordered list of every rule
    considered — {rule_id, rule_name, priority, matched} — up to and including
    the winner, so the audit log captures exactly what was evaluated and why the
    winner won. Evaluation stops at the first match (first-match-wins).

    v0.4.0.1: a rule scoped to an owning Company (`applies_to_company`) is only
    eligible when the transaction's account resolves to that Company. The row's
    Company is resolved once (not per rule) to keep this cheap.

    v0.4.1: a rule carrying `ignore_for_paired` (the default) is skipped
    entirely for a transaction the intercompany detector has paired — that
    transfer is booked through its Due from / Due to entries instead. The trace
    still records the rule as considered-and-unmatched, so the audit log shows
    exactly why an otherwise-matching rule didn't win."""
    from . import erpnext_accounts
    row_company = erpnext_accounts.owning_company_for_account_id(
        getattr(row, 'account_id', None))
    rules = (CategorizationRule.query
             .filter(CategorizationRule.active.is_(True),
                     CategorizationRule.archived.is_(False))
             .order_by(CategorizationRule.priority.asc(),
                       CategorizationRule.id.asc()).all())
    trace = []
    for rule in rules:
        matched = (_rule_eligible_for_paired(rule, row)
                   and _rule_applies_to_company(rule, row_company)
                   and rule_matches(
                       rule, merchant_name=row.merchant_name,
                       description=(row.name or ''), category=(row.category or ''),
                       amount=row.amount))
        trace.append({'rule_id': rule.id, 'rule_name': rule.name,
                      'priority': rule.priority, 'matched': matched})
        if matched:
            return rule, trace
    return None, trace


def find_matching_rule(row) -> CategorizationRule | None:
    """The first ACTIVE, non-archived rule (priority ascending, id as tiebreak)
    that matches the transaction, or None. Thin wrapper over evaluate_rules()
    for callers that don't need the evaluation trace (e.g. the test sandbox)."""
    return evaluate_rules(row)[0]


# ── Bank-Bridge-internal attribution tags (v0.4.49) ────────────────────────

def internal_tag_for(row, rule: CategorizationRule | None = None) -> str:
    """The internal attribution tag the winning rule assigns this transaction,
    or '' — a PURE read, no writes.

    Separate from JE generation on purpose: the tag is a property of which rule
    MATCHED, not of whether a Journal Entry was successfully built, so it can be
    recomputed for a whole account's history (the backfill) without touching JE
    state. Passing `rule` skips re-evaluation when the caller already has the
    winner."""
    if rule is None:
        rule = evaluate_rules(row)[0]
    if rule is None:
        return ''
    return (getattr(rule, 'bb_internal_tag', '') or '').strip()


def apply_internal_tag(row, rule: CategorizationRule | None = None) -> str:
    """Stamp the matched rule's internal tag onto `row` (no commit). Returns
    the tag written.

    Only writes when the tag actually changes, so this never dirties a row —
    and therefore never bumps `updated_at` — for a no-op. A row that matches a
    rule carrying no tag is CLEARED to '', which is what keeps the backfill a
    pure function of the current rule set: re-running it, or running it after a
    rule's tag is removed, converges rather than leaving a stale tag behind."""
    tag = internal_tag_for(row, rule)
    if (row.bb_internal_tag or '') != tag:
        row.bb_internal_tag = tag
    return tag


def backfill_internal_tags(account_id: str | None = None) -> dict:
    """Recompute `bb_internal_tag` on every stored transaction from the current
    rules — the retroactive path, so adding a tag to a rule can label history.

    TOUCHES ONLY THE TAG COLUMN. It never builds, posts, or alters a Journal
    Entry: tagging and JE generation are independent, and a rule added purely to
    attribute variance must not retro-post accounting entries for a year of old
    transactions. Idempotent — the tag is a pure function of the rules, so a
    second run changes nothing. Never raises; returns
    {'examined', 'tagged', 'cleared', 'unchanged'}."""
    stats = {'examined': 0, 'tagged': 0, 'cleared': 0, 'unchanged': 0}
    q = BankTransaction.query.filter(BankTransaction.removed.is_(False))
    if account_id:
        q = q.filter(BankTransaction.account_id == account_id)
    for row in q.all():
        stats['examined'] += 1
        before = row.bb_internal_tag or ''
        after = apply_internal_tag(row)
        if before == after:
            stats['unchanged'] += 1
        elif after:
            stats['tagged'] += 1
        else:
            stats['cleared'] += 1
    db.session.commit()
    return stats


# ── two-mode offset accounts (v0.4.0.3) ────────────────────────────────
#
# A rule's offset is interpreted per its scope (see CategorizationRule):
#   * SCOPED rule  (applies_to_company set) → Mode A: offset_account is a
#     specific, fully-qualified GL docname, used verbatim.
#   * AGNOSTIC rule (applies_to_company NULL) → Mode B: offset_account is a
#     LOGICAL account name (the ERPNext `account_name`, sans number + Company
#     suffix); at JE time it's resolved to the transaction's own Company's chart
#     (erpnext_accounts.resolve_logical_account), so one rule books to each
#     Company's own Meals/Fuel/… account.
#
# `logical_account_name` reduces a fully-qualified docname to that logical name.
# It's used both to convert a legacy agnostic rule's pinned offset on upgrade
# (app/migrations._migrate_agnostic_offset_to_logical) and as a resolve-time
# fallback. The trailing Company suffix is only stripped when the last ` - X`
# segment looks like a Company ABBREVIATION (uppercase letters/digits), so a real
# account_name that legitimately contains ` - ` (e.g. 'Owner - Draws') is left
# intact — which also makes the reduction idempotent.

# Trailing ' - <ABBR>' where ABBR is 1-10 uppercase letters/digits (ERPNext
# autoname suffix, e.g. ' - BBT'). Case-sensitive on purpose: 'Owner - Draws'
# ends in ' - Draws' but 'Draws' isn't all-caps, so it's not mistaken for a suffix.
_COMPANY_ABBR_SUFFIX_RE = re.compile(r'\s+-\s+[A-Z0-9]{1,10}$')
# Leading '<number> - ' account-number prefix ERPNext prepends in a numbered
# chart (e.g. '5100 - Fuel Expense' → 'Fuel Expense'); account_name has no number.
_LEADING_ACCOUNT_NUMBER_RE = re.compile(r'^\d+\s+-\s+')


def logical_account_name(name: str) -> str:
    """Reduce an ERPNext GL account docname to its LOGICAL account name (the
    `account_name` field): strip a trailing ' - <ABBR>' Company suffix and a
    leading '<number> - ' account number. 'Meals & Entertainment - BBT' and
    '5100 - Fuel Expense - EC' both reduce to their bare name; an already-logical
    name ('Meals & Entertainment') is returned unchanged. Idempotent:
    logical_account_name(logical_account_name(x)) == logical_account_name(x)."""
    s = (name or '').strip()
    if not s:
        return ''
    s2 = _COMPANY_ABBR_SUFFIX_RE.sub('', s).strip()
    s2 = _LEADING_ACCOUNT_NUMBER_RE.sub('', s2).strip()
    return s2 or s


# ── Description templates (v0.4.0.4) ───────────────────────────────────
#
# A rule's `description_template` is a small string with `{{variable}}`
# placeholders (whitespace-tolerant) that render into the JE's user_remark at
# generation time. The Rules editor auto-fills a sensible default per match type
# (default_description_template) when the operator picks an Offset Account, and
# `render_description_template` resolves the placeholders against a transaction.
#
# Deliberately NOT a full templating engine (v0.4.0.3 used Jinja): plain variable
# substitution keeps the surface tiny + predictable, and lets us COMPACT the
# separators a missing variable would otherwise leave behind ("A -  - C" → "A -
# C", leading/trailing " - " trimmed) — the property that makes the same template
# read cleanly whether or not every variable resolves.

# Per-match-type default templates. `{{offset_short}}` is a build-time token
# baked in by default_description_template (the offset account's logical name);
# every other `{{...}}` is a render-time transaction variable.
_DEFAULT_TEMPLATES = {
    'merchant_exact': '{{merchant_name}} - {{offset_short}}',
    'merchant_contains': '{{merchant_name}} - {{offset_short}}',
    'description_regex': '{{offset_short}} - {{amount}}',
    'plaid_category_matches': '{{plaid_category}} - {{offset_short}} - {{merchant_name}}',
    'amount_range': '{{offset_short}} - {{merchant_name}} - {{amount}}',
}
_DEFAULT_TEMPLATE_FALLBACK = '{{merchant_name}} - {{offset_short}}'

# One `{{ variable }}` placeholder — a bare identifier, any surrounding spaces.
_TEMPLATE_VAR_RE = re.compile(r'\{\{\s*(\w+)\s*\}\}')
# Two ' - ' separators with nothing between (what an empty variable leaves).
_DOUBLE_SEP = ' -  - '
_SEP = ' - '


def default_description_template(match_type: str, offset_account: str) -> str:
    """The auto-fill Description Template for a (match_type, offset_account) pair
    — the per-type pattern with `{{offset_short}}` replaced by the offset
    account's LOGICAL name (number + Company suffix stripped; see
    logical_account_name). The remaining `{{...}}` stay as render-time variables.
    An unknown match type falls back to the merchant/offset pattern."""
    pattern = _DEFAULT_TEMPLATES.get(match_type or '', _DEFAULT_TEMPLATE_FALLBACK)
    offset_short = logical_account_name(offset_account or '')
    return pattern.replace('{{offset_short}}', offset_short)


def _primary_category(category: str) -> str:
    """The primary Plaid category — the first segment of a stored 'A > B > C'
    path (or the whole label for a raw PFC string like 'GAS_STATIONS')."""
    cat = (category or '').strip()
    if not cat:
        return ''
    parts = [p.strip() for p in re.split(r'[>,]', cat) if p.strip()]
    return parts[0] if parts else cat


def _format_amount(amount, currency: str) -> str:
    """Signed amount as '123.45 USD' — keeps Plaid's sign (positive = outflow),
    two-decimal, with the transaction's currency (default USD)."""
    try:
        val = float(amount or 0.0)
    except (TypeError, ValueError):
        val = 0.0
    cur = (currency or 'USD').strip() or 'USD'
    return f'{val:.2f} {cur}'


def _template_context(row, supplier_name=None, rule_name=None) -> dict:
    """Render-time values for the template variables, read off a transaction
    (BankTransaction or any object exposing the same attributes). Missing values
    resolve to '' (compacted away later). `merchant_name` falls back to the raw
    description when the merchant is missing. `name`/`category`/`supplier_name`/
    `rule_name` are legacy aliases (pre-v0.4.0.4 templates) kept resolving."""
    merchant = (getattr(row, 'merchant_name', '') or '').strip()
    description = (getattr(row, 'name', '') or '').strip()
    d = getattr(row, 'date', None)
    return {
        'merchant_name': merchant or description,
        'description': description,
        'name': description,                       # legacy alias
        'amount': _format_amount(getattr(row, 'amount', 0.0),
                                 getattr(row, 'iso_currency_code', 'USD')),
        'plaid_category': _primary_category(getattr(row, 'category', '')),
        'category': (getattr(row, 'category', '') or ''),   # legacy alias (full)
        'date': d.isoformat() if d else '',
        'supplier_name': supplier_name or '',      # legacy alias
        'rule_name': rule_name or '',              # legacy alias
    }


def _compact_separators(s: str) -> str:
    """Collapse ' -  - ' chains left by empty variables to a single ' - ', then
    trim any leading/trailing separator. Leaves separators INSIDE a resolved
    value untouched (e.g. an account name like 'Owner - Draws')."""
    s = s or ''
    while _DOUBLE_SEP in s:
        s = s.replace(_DOUBLE_SEP, _SEP)
    # Trim only ' - ' style separators (dash with surrounding whitespace) at the
    # ends — never a bare leading '-', which would eat a negative amount's sign.
    s = re.sub(r'^(\s+-\s+)+', '', s)
    s = re.sub(r'(\s+-\s+)+$', '', s)
    return s.strip()


def render_description_template(template: str, transaction,
                               supplier_name=None, rule_name=None) -> str:
    """Render a `description_template` against a transaction: substitute each
    `{{variable}}` with its value ('' when the variable is unknown or the data is
    missing), then compact the separators. Returns '' for a blank template. Pure
    + total — never raises (a template is operator input, not code)."""
    tmpl = template or ''
    if not tmpl.strip():
        return ''
    ctx = _template_context(transaction, supplier_name=supplier_name,
                            rule_name=rule_name)

    def _sub(m):
        return str(ctx.get(m.group(1), ''))

    return _compact_separators(_TEMPLATE_VAR_RE.sub(_sub, tmpl))


# ── Journal Entry construction ─────────────────────────────────────────

def render_description(rule: CategorizationRule, row, supplier_name=None) -> str:
    """The JE user_remark for a matched transaction: the rule's rendered
    `description_template`, or a sensible default when the template is blank (or
    renders to nothing). A bad/empty template must never block generation."""
    default = (f'{rule.name or "Auto"} — '
               f'{row.merchant_name or row.name or "transaction"} '
               f'{row.date.isoformat() if row.date else ""}').strip()
    tmpl = (rule.description_template or '').strip()
    if not tmpl:
        return default
    rendered = render_description_template(tmpl, row, supplier_name=supplier_name,
                                           rule_name=rule.name or '')
    return rendered or default


def bank_gl_account_for(row) -> str:
    """The ERPNext GL Account (Chart-of-Accounts leaf) for the transaction's
    linked Plaid account — the BANK side of a v0.3.1 bank-agnostic JE. Empty
    string when the account is unmapped, has no GL link (import fell back to a
    personal account), or the row carries no account_id."""
    account_id = getattr(row, 'account_id', None)
    if not account_id:
        return ''
    acct = PlaidAccount.query.filter_by(account_id=account_id).first()
    return ((acct.erpnext_gl_account_name or '').strip() if acct else '')


# v0.5.15 · the two internal tags whose JE cash leg is routed to Cash Clearing
# (external owner capital in/out of a brokerage) rather than the account's own
# bank GL.
CONTRIBUTION_TAGS = ('owner_contribution', 'member_distribution')


def _contribution_bank_leg(client, row, company: str) -> str | None:
    """Cash Clearing docname when this transaction is a tagged owner
    contribution / member distribution on a paired-brokerage companion account,
    else None (v0.5.15). None leaves build_journal_entry on its normal bank GL."""
    tag = (getattr(row, 'bb_internal_tag', '') or '').strip()
    if tag not in CONTRIBUTION_TAGS:
        return None
    account_id = getattr(row, 'account_id', None)
    if not account_id:
        return None
    acct = PlaidAccount.query.filter_by(account_id=account_id).first()
    if acct is None:
        return None
    # Its own account is a brokerage with a cash companion, OR it IS the
    # companion of some paired brokerage — either way a Cash Clearing bridge is
    # the right cash leg.
    is_companion = (
        (acct.paired_account_id or '').strip()
        or PlaidAccount.query.filter_by(
            paired_account_id=acct.account_id).first() is not None)
    if not is_companion:
        return None
    from . import invest_je
    return invest_je.cash_clearing_account(client, company)


# ── v0.8.5: rule-level accounting dimensions, stamped onto BOTH legs ─────────

# The `bank_cost_center` sentinel meaning "write NO cost center key on the bank
# leg". A real Cost Center docname is always Company-suffixed ('Harvest - OML'),
# so this can never collide with one. See CategorizationRule.bank_cost_center.
BANK_LEG_NO_COST_CENTER = '(none)'


def bank_leg_cost_center(rule) -> str:
    """The Cost Center docname the BANK line should carry, '' for "write no key
    at all" (v0.8.5).

      * `bank_cost_center` unset      → MIRROR the rule's own `cost_center`
      * `bank_cost_center` = '(none)' → '' (ERPNext's own default fills it)
      * anything else                 → that docname, verbatim

    A rule with no `cost_center` at all returns '' under every branch except an
    explicit override: there is nothing to mirror."""
    offset_cc = (getattr(rule, 'cost_center', None) or '').strip()
    override = (getattr(rule, 'bank_cost_center', None) or '').strip()
    if not override:
        return offset_cc
    if override == BANK_LEG_NO_COST_CENTER:
        return ''
    return override


def apply_rule_dimensions(rule, offset_line: dict, bank_line: dict, *,
                          party_type: str = '', party: str = '') -> None:
    """Stamp this rule's accounting dimensions onto the JE lines, in place.

    THE ONE PLACE rule-level metadata reaches a Journal Entry line, and the
    reason it is a function rather than four lines inline: Sprint 5's per-rule
    Party wiring lands here too, so "which legs does a rule's metadata reach"
    is answered once per dimension instead of once per caller.

    v0.9.0 · Party arrived, as promised. It comes in as an ALREADY-RESOLVED
    (party_type, party) pair rather than being read off the rule, because
    resolving it is not a stamping concern: it needs an ERPNext client, it
    creates records, and it commits (see resolve_party). This function stays what
    it was — pure, synchronous, no I/O — and the leg-eligibility question it owns
    is answered for Party exactly as it is for Cost Center.

    Each dimension decides its own leg eligibility, because the legs are not
    interchangeable and ERPNext does not treat them so:

      * COST CENTER → BOTH legs (v0.8.5). Through v0.8.4 only the offset line
        got one, and ERPNext's server-side fallback then stamped the bank line
        with the COMPANY DEFAULT — 'Main - OML' on a Sorren bill whose expense
        half sat in '310 - G and A Administration - OML'. The cost landed in
        one segment and the cash that paid it in another, so a Cost-Center-wise
        report never balanced and 'Main' silently absorbed the cash side of
        every segment in the chart. A dimension only reads correctly when both
        halves of a double entry carry it.

      * PARTY → offset leg ONLY, and that is ERPNext's rule, not ours. The
        enforcement is `erpnext/accounts/party.py::validate_account_party_type`,
        reached from GL Entry.validate_party — so it fires at SUBMIT, and it
        refuses a Party on any account whose account_type is set to something
        other than Receivable, Payable or Equity. A BANK LINE IS ALWAYS
        account_type 'Bank', which is never in that set, so the bank leg can
        never carry a party no matter how the rule is configured. That is the
        asymmetry with Cost Center: a cost center BELONGS on both halves of a
        double entry, a party belongs only on the half that has a counterparty.

    An UNSET cost_center writes NO key on EITHER leg, which is the whole
    fallback chain: ERPNext then applies the Account's default cost center, or
    failing that the Company's, server-side. Writing a guessed value would
    OVERRIDE those defaults — "leave it blank" is not a gap in the chain, it is
    how the rest of the chain gets to run.

    An unset party writes no key either, for a different reason: a JE line with
    no party is valid and posts, and ERPNext has no party default to fall back
    to. Blank simply means "no counterparty recorded"."""
    cost_center = (getattr(rule, 'cost_center', None) or '').strip()
    if cost_center:
        offset_line['cost_center'] = cost_center
    bank_cc = bank_leg_cost_center(rule)
    if bank_cc:
        bank_line['cost_center'] = bank_cc
    # PARTY · offset leg only, and only when BOTH halves are present — ERPNext
    # treats a party_type with no party as an incomplete row, not a partial one.
    ptype = (party_type or '').strip()
    pname = (party or '').strip()
    if ptype and pname and not getattr(rule, 'skip_party', False):
        offset_line['party_type'] = ptype
        offset_line['party'] = pname


def build_journal_entry(rule: CategorizationRule, row, company: str, *,
                        supplier_name=None, remark: str = '',
                        bank_account: str | None = None,
                        offset_account_override: str | None = None,
                        party_override: str | None = None,
                        party_type_override: str | None = None) -> dict:
    """Assemble the ERPNext Journal Entry payload for a matched transaction.

    Two lines — the OFFSET (categorized) side and the BANK side:

      * v0.3.1 bank-agnostic path (rule has `offset_account`): the rule supplies
        only the offset account; the bank account comes from the transaction's
        linked Plaid account (`bank_account`, else resolved from the row). Which
        side the offset lands on is decided by `offset_direction`:
          - 'always_debit'  → offset debited, bank credited;
          - 'always_credit' → bank debited, offset credited;
          - 'auto'          → Plaid sign: amount > 0 (withdrawal) debits the
                              offset; amount ≤ 0 (deposit/refund) credits it.
      * Legacy path (no `offset_account`, deprecated debit/credit pair): the old
        behaviour — debit the rule's debit_account, credit its credit_account,
        reversing on an inflow — kept for backwards compatibility.

    v0.4.0.3 — `offset_account_override` supplies the already-resolved offset for
    a Mode B (Company-agnostic) rule, whose `offset_account` is a logical name the
    caller has resolved to a specific account under `company` (see
    generate_journal_entry). When None, the rule's own `offset_account` is used
    (Mode A / legacy), unchanged.

    The optional party rides the offset line — `party_override` wins (the
    v0.4.0.7 already-ensured Supplier docname from generate_journal_entry), then
    `rule.party_name`, then the auto-created Supplier for this merchant.

    v0.4.0.7 — a rule with `skip_party` set puts NO party on the JE at all
    (transfers between two accounts you own have no counterparty to book)."""
    amt = round(abs(float(row.amount or 0.0)), 2)
    offset_account = (offset_account_override
                      if offset_account_override is not None
                      else (rule.offset_account or '')).strip()

    if offset_account:
        bank = (bank_account if bank_account is not None
                else bank_gl_account_for(row)) or rule.credit_account or ''
        direction = (rule.offset_direction or 'auto').strip() or 'auto'
        if direction == 'always_debit':
            offset_is_debit = True
        elif direction == 'always_credit':
            offset_is_debit = False
        else:  # auto — Plaid: positive = outflow (spending)
            offset_is_debit = float(row.amount or 0.0) > 0
        offset_line = {'account': offset_account}
        bank_line = {'account': bank}
        if offset_is_debit:
            offset_line['debit_in_account_currency'] = amt
            bank_line['credit_in_account_currency'] = amt
            accounts = [offset_line, bank_line]
        else:
            offset_line['credit_in_account_currency'] = amt
            bank_line['debit_in_account_currency'] = amt
            accounts = [bank_line, offset_line]
        party_line = offset_line
    else:
        # Deprecated pre-v0.3.1 pair (both accounts on the rule).
        outflow = float(row.amount or 0.0) >= 0
        party_line = {'account': rule.debit_account}
        bank_line = {'account': rule.credit_account}
        if outflow:
            party_line['debit_in_account_currency'] = amt
            bank_line['credit_in_account_currency'] = amt
        else:
            party_line['credit_in_account_currency'] = amt
            bank_line['debit_in_account_currency'] = amt
        accounts = [party_line, bank_line]

    # `party_override` follows the same convention as offset_account_override:
    # None means "the caller didn't resolve a party, use the legacy precedence",
    # while ANY string — including '' — is authoritative. That distinction is
    # load-bearing: generate_journal_entry passes '' when resolve_party declined
    # (skip_party, or a Supplier that couldn't be created), and falling back to
    # rule.party_name there would put back the very unbacked party whose
    # LinkValidationError v0.4.0.7 exists to fix.
    party = (party_override if party_override is not None
             else (rule.party_name or supplier_name))
    # v0.4.0.8 · the SIDE follows the same override convention. generate_
    # journal_entry passes the already-derived 'Supplier'/'Customer' (or '' to
    # decline), so a party_type='Auto' rule lands the right way round. Without
    # an override we fall back to the rule's own literal value — and a bare
    # 'Auto' is NOT a doctype ERPNext knows, so it books no party rather than a
    # party the server would reject.
    party_type = (party_type_override if party_type_override is not None
                  else (rule.party_type or ''))
    if (party_type or '').strip().lower() == 'auto':
        party_type = ''

    # v0.8.5 · the rule's accounting dimensions, onto BOTH legs. `party_line` is
    # the categorized side (the offset line in the v0.3.1 path, the non-bank
    # debit_account line in the deprecated one) and `bank_line` is the cash
    # side. See apply_rule_dimensions for the tri-state and the reasoning.
    #
    # v0.9.0 · the party rides through here too rather than being stamped a few
    # lines below, so there is ONE function that decides which leg any rule-level
    # dimension reaches. Resolution stays out here (it needs a client and it
    # commits); only the stamping moved in.
    apply_rule_dimensions(rule, party_line, bank_line,
                          party_type=(party_type or ''), party=(party or ''))

    if row.erpnext_bank_transaction_id:
        for ln in accounts:
            ln['reference_type'] = 'Bank Transaction'
            ln['reference_name'] = row.erpnext_bank_transaction_id

    doc = {
        'doctype': JOURNAL_ENTRY_DT,
        'voucher_type': 'Journal Entry',
        'company': company,
        'user_remark': remark,
        'accounts': accounts,
    }
    if row.date:
        doc['posting_date'] = row.date.isoformat()
    return doc


# ── v0.4.0.7: party resolution + auto-Supplier for every party source ──

# The rule's party_type values. '' (NULL) means NO party — that has been the
# behaviour since v0.3.0 and is deliberately preserved, so an existing rule that
# never named a party keeps not naming one.
#
# v0.9.0 · Employee and Shareholder join the list. Both are real ERPNext Party
# Types on this install (`tabParty Type`: Supplier / Employee / Shareholder all
# map to account_type Payable, Customer to Receivable), and both answer a
# question the ledger could not answer before: an owner draw booked to
# '3201 - Member Distribution' is a SHAREHOLDER transaction, and a reimbursement
# booked to '1610 - Employee Advances' is an EMPLOYEE one.
PARTY_TYPES = ('', 'Supplier', 'Customer', 'Employee', 'Shareholder', 'Auto')

# The party types Bank Bridge is willing to CREATE when one is missing (the
# v0.9.0 `auto_create_party` rule flag). The split is not arbitrary — it is what
# each DocType requires:
#
#   * Supplier / Customer — named after the party itself, no other mandatory
#     field. Minted since v0.4.0.8.
#   * Shareholder — mandatory fields are `company` + `title`, both of which we
#     have. Safe to mint.
#   * Employee — mandatory fields are `date_of_birth`, `date_of_joining`,
#     `gender`, `first_name` and `status`. We know exactly one of those. An
#     auto-created Employee would carry a FABRICATED birth date and joining
#     date in an HR record that payroll and leave accrual read, so this one is
#     never minted: a rule naming an Employee that does not exist declines the
#     party and says so (see resolve_party → PARTY_NOT_FOUND).
AUTO_CREATABLE_PARTY_TYPES = ('Supplier', 'Customer', 'Shareholder')

# v0.4.0.8 · the party_type='Auto' derivation, keyed on the offset account's
# root_type alone. RETAINED FOR REFERENCE ONLY — v0.4.0.9 supersedes it with
# PARTY_TYPE_MATRIX below, because root_type is not the field ERPNext validates
# against. See party_type_for_account_types for the bug this cost us.
ROOT_TYPE_PARTY_TYPE = {
    'Income': 'Customer',
    'Expense': 'Supplier',
}

# The ERPNext account_type values that a Party may legally be attached to. This
# is not our policy — it is ERPNext's, enforced in JournalEntry.validate_party:
#
#     Party Type and Party can only be set for Receivable / Payable account
#
# A Receivable account is an AR ledger, so its counterparty is a Customer; a
# Payable account is AP, so its counterparty is a Supplier.
PARTY_ACCOUNT_TYPES = {
    'Receivable': 'Customer',
    'Payable': 'Supplier',
}

# ── v0.9.0 · ERPNext's ACTUAL party rule, read off the running install ───────
#
# Everything above this line was written from the error message ERPNext prints,
# and the error message is not the rule. The rule is
# `erpnext/accounts/party.py::validate_account_party_type`, reached from
# `GL Entry.validate_party` — which is why it fires at SUBMIT (GL Entries are
# what a submit writes), not at insert:
#
#     def validate_account_party_type(self):
#         if self.is_cancelled: return
#         if self.party_type and self.party:
#             account_type = frappe.get_cached_value("Account", self.account,
#                                                    "account_type")
#             if account_type and (account_type not in
#                                  ["Receivable", "Payable", "Equity"]):
#                 frappe.throw("Party Type and Party can only be set for "
#                              "Receivable / Payable account ...")
#
# Two clauses in there that PARTY_TYPE_MATRIX (v0.4.0.9) missed, and both cost
# us party coverage on real accounts:
#
#   1. **EQUITY IS ALLOWED.** Not just Receivable/Payable. That is the whole
#      reason a Shareholder party type exists, and on this install it is 12
#      already-submitted bank-transaction JE lines sitting on Equity accounts
#      ('3201 - Member Distribution', '3200 - Member Contributions') with an
#      empty party that ERPNext would have accepted all along.
#
#   2. **A BLANK account_type IS ALLOWED.** The `account_type and` guard means
#      an Account whose account_type field is simply not set skips the check
#      entirely. This is not an edge case: 29 of OML's ~60 leaf accounts are
#      blank, including most of the 52xx expense accounts. v0.4.0.9 declined a
#      party on every one of them.
#
# What is still genuinely refused is an account with an EXPLICIT, non-eligible
# account_type — 'Expense Account', 'Income Account', 'Bank', 'Cash', 'Tax'.
# That is why ACC-JV-2026-02312 (Sorren, $2,030) cannot carry a Supplier as
# booked: its offset is '6400 - Professional Services - OML', account_type
# 'Expense Account', set explicitly. No amount of rule configuration changes
# that — see party_eligibility for what an operator is told instead.
PARTY_ELIGIBLE_ACCOUNT_TYPES = frozenset({'Receivable', 'Payable', 'Equity'})

# Each Party Type's own `account_type`, from `tabParty Type` on this install.
# JournalEntry.validate_party compares this against the ACCOUNT's account_type
# and refuses a mismatch — but ONLY when the account is Receivable/Payable, and
# with a standing exception for Employee ("since they can be both payable and
# receivable"). An Equity or blank-account_type account reaches neither clause,
# so any party type is accepted there.
PARTY_TYPE_ACCOUNT_TYPES = {
    'Customer': 'Receivable',
    'Supplier': 'Payable',
    'Employee': 'Payable',
    'Shareholder': 'Payable',
}

# The party type `Auto` derives for an account, keyed on account_type FIRST
# (the field ERPNext validates) and falling back to root_type when account_type
# is blank — where ERPNext validates nothing, so root_type's coarser answer is
# both safe and the only signal available.
_AUTO_BY_ACCOUNT_TYPE = {
    'Receivable': 'Customer',
    'Payable': 'Supplier',
    'Equity': 'Shareholder',
}
_AUTO_BY_ROOT_TYPE = {
    'Income': 'Customer',
    'Expense': 'Supplier',
    'Equity': 'Shareholder',
}

# party_eligibility verdicts. `unknown` is NOT `allowed` on purpose: a blank
# account_type that came back from a FAILED ERPNext read looks identical to a
# genuinely blank one, and guessing "allowed" there is how an unsubmittable JE
# gets written during an ERPNext blip. Fail Safe — decline, and say why.
ELIGIBILITY_ALLOWED = 'allowed'
ELIGIBILITY_BLOCKED = 'blocked'
ELIGIBILITY_UNKNOWN = 'unknown'


def party_allowed_on_account_type(account_type: str,
                                  party_type: str = '') -> bool:
    """Whether ERPNext would accept `party_type` on a line whose account carries
    `account_type` — the pure predicate, no ERPNext round-trip (v0.9.0).

    A BLANK `account_type` returns True, because that is what ERPNext does (see
    PARTY_ELIGIBLE_ACCOUNT_TYPES clause 2). Callers that cannot distinguish
    "blank" from "could not read it" must use `party_eligibility` instead, which
    keeps the two apart."""
    acct = (account_type or '').strip()
    if not acct:
        return True                       # ERPNext skips the check entirely
    if acct not in PARTY_ELIGIBLE_ACCOUNT_TYPES:
        return False
    # Receivable/Payable additionally demand a matching Party Type — except
    # Employee, which ERPNext exempts explicitly.
    declared = (party_type or '').strip()
    if acct in ('Receivable', 'Payable') and declared:
        if declared == 'Employee':
            return True
        want = PARTY_TYPE_ACCOUNT_TYPES.get(declared)
        return want is None or want == acct
    return True


def auto_party_type_for(root_type: str, account_type: str) -> str:
    """The side `party_type='Auto'` picks for an account (v0.9.0).

    account_type decides when it is set — it is the field ERPNext enforces. When
    it is blank, root_type decides, which is safe precisely BECAUSE blank means
    ERPNext validates nothing. '' when neither says anything useful (an Asset or
    Liability root with no account_type — typically a transfer between accounts
    you own, which wants no party at all)."""
    acct = (account_type or '').strip()
    if acct:
        return _AUTO_BY_ACCOUNT_TYPE.get(acct, '')
    return _AUTO_BY_ROOT_TYPE.get((root_type or '').strip(), '')


def _offset_account_types(client, offset_account: str,
                          company: str = '') -> tuple[str, str]:
    """`erpnext_bank.account_types_for_account`, memoized per (offset, company)
    for the life of the app context (v0.9.0).

    THE ONE FETCH both party questions share. The JE path runs once PER
    TRANSACTION (sync_engine loops categorize_after_push over every row), and
    v0.9.0 asks about the offset account twice per transaction — once to derive
    an 'Auto' side, once to check eligibility. Without a shared cache a sync of
    300 transactions through one rule made 600 identical ERPNext calls; the
    memo makes it one. `g` scopes it to a single sync run / request, so a chart
    edit is picked up on the next run rather than needing a restart."""
    key = ((offset_account or '').strip(), (company or '').strip())
    try:
        cache = g._bb_offset_account_types
    except AttributeError:
        cache = g._bb_offset_account_types = {}
    except RuntimeError:            # no app context (direct call in a test)
        cache = None
    if cache is not None and key in cache:
        return cache[key]
    types = erpnext_bank.account_types_for_account(client, offset_account,
                                                  company)
    if cache is not None:
        cache[key] = types
    return types


def party_eligibility(client, offset_account: str, company: str = '',
                      party_type: str = '') -> tuple[str, str]:
    """(verdict, account_type) for hanging a party off `offset_account`.

    verdict is one of ELIGIBILITY_ALLOWED / _BLOCKED / _UNKNOWN. The third is
    the one that earns this function its existence: `account_types_for_account`
    answers ('', '') both for an account whose account_type is genuinely blank
    (party ALLOWED) and for an ERPNext read that failed (party must be
    DECLINED). Those are opposite answers, so this asks ERPNext whether it knows
    the account at all — a resolvable account with a blank account_type is
    `allowed`, an unresolvable one is `unknown`.

    Never raises. An ERPNext that cannot be reached yields `unknown` — and note
    that `unknown` is NOT a decline at the JE path (see resolve_party): only a
    POSITIVE block is acted on there, so a network blip cannot cost a party."""
    root, acct_type = _offset_account_types(client, offset_account, company)
    if not root and not acct_type:
        # Nothing came back at all. Either ERPNext is unreachable or the account
        # does not resolve — either way we do not know what we are writing onto.
        return ELIGIBILITY_UNKNOWN, ''
    if party_allowed_on_account_type(acct_type, party_type):
        return ELIGIBILITY_ALLOWED, acct_type
    return ELIGIBILITY_BLOCKED, acct_type


# v0.4.0.9 · the party_type='Auto' derivation, now keyed on BOTH the offset
# account's root_type AND its account_type.
#
# THE BUG THIS FIXES: v0.4.0.8 mapped root_type alone — Income → Customer,
# Expense → Supplier. But a garden-variety Income account has root_type=Income
# and account_type='Income Account', NOT 'Receivable', so hanging a Customer off
# it produces a JE that CREATES fine and then fails at submit:
#
#     ValidationError: Party Type and Party can only be set for Receivable /
#     Payable account Interest Income - BBT
#
# (ERPNext validates the party at submit, not at insert, which is why the
# breakage surfaced only once an operator went to approve the entries.) An
# Income root with a Receivable account_type — a real AR ledger booked under
# Income — is the only Income shape that legitimately carries a Customer, and
# likewise Expense + Payable for a Supplier. Every other pair, including the
# Asset / Liability / Equity roots that were already partyless in v0.4.0.8, gets
# NO party: a JE with no party still posts, and posting beats being right.
PARTY_TYPE_MATRIX = {
    ('Income', 'Receivable'): 'Customer',
    ('Expense', 'Payable'): 'Supplier',
}


def party_type_for_account_types(root_type: str, account_type: str) -> str:
    """The party side an offset account with this (root_type, account_type) pair
    may legally carry, or '' for "no party".

    v0.9.0 · now ERPNext's real rule rather than PARTY_TYPE_MATRIX's two-entry
    approximation of it. The eligibility test and the side derivation are
    separate questions and are answered separately: an account must be
    party-ELIGIBLE (party_allowed_on_account_type) *and* imply a side
    (auto_party_type_for). PARTY_TYPE_MATRIX is retained above as the record of
    what v0.4.0.9 believed, because two of the four bank-transaction JE
    populations on this install were declined a party by it."""
    if not party_allowed_on_account_type(account_type):
        return ''
    return auto_party_type_for(root_type, account_type)


def party_type_for_offset(client, offset_account: str, company: str = '') -> str:
    """The party side ('Customer' | 'Supplier' | '') implied by an offset
    account — the `party_type='Auto'` derivation (v0.4.0.8, refined v0.4.0.9 to
    consult account_type as well as root_type; see PARTY_TYPE_MATRIX).

    '' means "no party": the pair says transfer, says a non-party ledger like an
    Income Account, or couldn't be determined at all. All three are the safe
    answer, since a JE with no party still posts.

    Memoized on (offset_account, company) for the life of the app context. The
    JE path runs once PER TRANSACTION (sync_engine loops categorize_after_push
    over every row), and a sync that pulls hundreds of transactions through a
    handful of Auto rules would otherwise re-ask ERPNext for the same few
    accounts' types hundreds of times. `g` scopes the cache to one sync run /
    one request, so a chart edit is picked up on the next run rather than
    needing a restart. Both types come from one fetch, so v0.4.0.9's extra
    precision costs no extra round-trips."""
    root, acct_type = _offset_account_types(client, offset_account, company)
    return party_type_for_account_types(root, acct_type)


def effective_party_type(client, rule: CategorizationRule,
                         company: str = '') -> str:
    """The party side this rule wants for a JE booked under `company` — one of
    PARTY_TYPES minus 'Auto', or '' for no party — resolving 'Auto' against the
    offset account (v0.4.0.8). Precedence, highest first:

      1. `skip_party` — the v0.4.0.7 override, always wins → ''.
      2. a literal party type on the rule ('Supplier' / 'Customer' / and since
         v0.9.0 'Employee' / 'Shareholder') — the operator's explicit choice
         beats any derivation, for a chart that doesn't follow the usual
         root_type convention.
      3. 'Auto' — derived from the offset account's types.
      4. '' / NULL — no party, unchanged since v0.3.0.

    v0.4.0.9 deliberately does NOT second-guess an explicit side here, even
    though ERPNext will reject one on a non-Receivable/Payable account. The
    incompatible-explicit case is handled where it can be handled well: the
    Rules editor refuses to save a new one (party_type_conflict, below) and a
    boot migration flips the ones already in the database
    (migrations._migrate_incompatible_party_types). Doing it a third time at JE
    time would mean silently dropping a party mid-sync on a transient ERPNext
    read failure, which is a worse failure than the one it prevents."""
    if getattr(rule, 'skip_party', False):
        return ''
    declared = (rule.party_type or '').strip()
    # Every literal party type ERPNext knows, not just the two v0.4.0.8 had.
    if declared in PARTY_TYPE_ACCOUNT_TYPES:
        return declared
    if declared.lower() == 'auto':
        return party_type_for_offset(client, (rule.offset_account or ''), company)
    return ''


def party_type_conflict(client, party_type: str, offset_account: str,
                        company: str = '') -> tuple[str, str]:
    """Would ERPNext reject `party_type` on `offset_account`? Returns a
    (severity, message) pair for the Rules editor to act on (v0.4.0.9):

      ('',      '')    — compatible, or not knowable, so don't stand in the way.
      ('block', msg)   — a definite conflict on a definite account. Refuse the
                         save; the rule could only ever produce unsubmittable
                         Journal Entries.
      ('warn',  msg)   — the offset is a LOGICAL name (a Mode B Company-agnostic
                         rule) that resolves incompatibly under at least one
                         Company but not all. The operator may well know which
                         Companies the rule will actually fire under, so this is
                         a confirmation, not a refusal.

    Only a LITERAL party type can conflict. 'Auto' derives a legal side per
    transaction by construction, and '' / NULL books no party at all — both
    always return ('', ''), which is what makes "set it to None or Auto" honest
    advice in the message.

    v0.9.0 · the compatibility test is now `party_allowed_on_account_type`, so
    an Equity account and an account with a BLANK account_type both stop being
    refused. They were never ERPNext's objection; they were ours. See
    PARTY_ELIGIBLE_ACCOUNT_TYPES.

    Silent on anything it cannot determine (ERPNext down, unresolvable account):
    a save-time check that blocks on a network blip would be worse than the
    submit-time failure it is trying to pre-empt."""
    declared = (party_type or '').strip()
    offset = (offset_account or '').strip()
    if declared not in PARTY_TYPE_ACCOUNT_TYPES or not offset:
        return '', ''
    scope = (company or '').strip()
    if scope:
        # Mode A · a fully-qualified offset under one Company. One answer, and
        # a wrong one is definitive — block.
        acct_type = erpnext_bank.account_types_for_account(
            client, offset, scope)[1]
        if not acct_type or party_allowed_on_account_type(acct_type, declared):
            return '', ''
        return 'block', _party_conflict_message(offset, acct_type, declared)
    # Mode B · a logical name resolving across every Company. Collect the
    # DISTINCT account_types it maps to and warn if any of them is incompatible.
    try:
        rows = erpnext_bank.list_accounts(client, company=None)
    except Exception:
        return '', ''
    bad: dict[str, str] = {}            # account_type → an example docname
    for r in rows:
        if (r.get('account_name') or '').strip().lower() != offset.lower():
            continue
        acct_type = (r.get('account_type') or '').strip()
        if acct_type and not party_allowed_on_account_type(acct_type, declared):
            bad.setdefault(acct_type, (r.get('name') or '').strip() or offset)
    if not bad:
        return '', ''
    acct_type, example = sorted(bad.items())[0]
    return 'warn', _party_conflict_message(example, acct_type, declared)


def _party_conflict_message(account: str, acct_type: str,
                            declared: str) -> str:
    """The operator-facing sentence for a party_type/offset conflict. Names the
    account, what it actually is, what ERPNext does allow, and the ways out —
    the fix has to be obvious from the message alone, since it is the only thing
    the operator sees when a save is refused.

    v0.9.0 · the advice now states the real eligible set (Receivable, Payable,
    Equity, or an account with no account_type set) instead of implying only the
    first two, and names clearing the account's account_type as the third way
    out — on this chart that is the difference between a rule that can carry a
    party and one that never will."""
    want = PARTY_TYPE_ACCOUNT_TYPES.get(declared, 'Receivable or Payable')
    return (f'{account} is a{"n" if acct_type[:1] in "AEIOU" else ""} '
            f'{acct_type}, not a {want} account. ERPNext only allows a Party on '
            f'an account whose type is Receivable, Payable or Equity — or that '
            f'has no account type set at all — so Bank Bridge cannot attach a '
            f'{declared} party to it. Set Party Type to “— none —” or “Auto”, '
            f'pick a different offset account, or clear the account type on '
            f'{account} in ERPNext if it should not have been classified.')


def resolve_party(client, rule: CategorizationRule, row, supplier_name=None,
                  company: str = '', offset_account_override: str | None = None):
    """The (party_type, party docname) to put on this JE's offset line, with the
    ERPNext party guaranteed to exist first. Returns None when the JE should
    carry no party (v0.4.0.7; tuple-valued since v0.4.0.8).

    THE BUG THIS FIXES: pre-v0.4.0.7 only a Plaid `merchant_name` ever triggered
    the auto-Supplier (see categorize_after_push), but a rule may name a Party
    for a transaction that HAS no merchant — an interest payment, a card
    payment, a payroll ACH. That party went onto the JE with no matching
    Supplier and ERPNext refused the whole document:

        LinkValidationError: Could not find Row #1: Party: Wells Fargo
        POST /api/resource/Journal Entry -> 417

    So the ensure now hangs off the PARTY, not off the merchant field. The SIDE
    comes from effective_party_type (skip_party / explicit / Auto); the NAME is
    then resolved in this order:

      1. no side at all → None (nothing to ensure).
      2. `rule.party_name` — the operator's literal name, used verbatim.
      3. the merchant Supplier already resolved by the sync path.
      4. derived from the transaction — payroll processor in the description,
         else the account's institution (erpnext_bank.derive_party_from_transaction).

    v0.4.0.8 — BOTH sides are now auto-created. A Customer party is minted just
    like a Supplier (erpnext_bank.ensure_party), which is what makes the sell
    side work at all: fruit-buyer deposits, USDA/FSA payments, grants and lease
    revenue all need an AR party, and booking them against an auto-created
    Supplier put them on the wrong ledger and polluted the 1099-NEC vendor list.
    A dual-role name (a bank, a brokerage) additionally gets its opposite side
    provisioned — see erpnext_bank.is_dual_role_party.

    If the party can't be resolved we return None rather than a name we know
    ERPNext will reject — a JE with no party beats no JE at all.

    v0.9.0 · three changes, all of them about not being silent.

      * ELIGIBILITY IS CHECKED HERE, once, against the offset account — but
        ONLY a positive block is acted on. v0.4.0.9 checked it only for 'Auto'
        (inside the derivation) and only at rule-save time for an explicit side,
        which left the JE path able to write a party ERPNext refuses at submit —
        producing a draft nobody can approve, the exact state
        _migrate_incompatible_party_types exists to clean up.

        `ELIGIBILITY_UNKNOWN` deliberately does NOT decline. That is v0.4.0.9's
        own objection to checking here, and it was right: dropping a party
        because one ERPNext read failed mid-sync loses it permanently, since the
        GeneratedJournalEntry row means the transaction is never reconsidered.
        So an unreadable account keeps pre-v0.9.0 behaviour (write the party,
        let ERPNext be the authority) and only a DEFINITE conflict on a DEFINITE
        account declines. Same stance as party_type_conflict and
        _migrate_incompatible_party_types: silent on what it cannot determine.
      * `auto_create_party` on the rule overrides the global
        ERPNEXT_AUTO_CREATE_SUPPLIERS gate. NULL inherits, so no existing rule
        changes behaviour on upgrade.
      * FAIL FORWARD — every decline is recorded via `_note_party_decline`, so a
        rule naming "Sorren" against an install with no Sorren shows up in the
        audit log instead of quietly producing a partyless JE forever.

    `offset_account_override` pins the account the eligibility read tests. It is
    normally unnecessary — `erpnext_bank.account_types_for_account` resolves a
    bare LOGICAL name (a Mode B rule's offset) via its account_name fallback
    scoped to `company`, so the rule's own `offset_account` gives the right
    answer in both offset modes. It exists for the backfill planner, which knows
    the exact docname a historical JE used and should test that one."""
    party_type = effective_party_type(client, rule, company)
    if not party_type:
        return None
    offset = ((offset_account_override
               if offset_account_override is not None
               else (rule.offset_account or '')) or '').strip()
    if offset:
        verdict, acct_type = party_eligibility(client, offset, company,
                                              party_type)
        if verdict == ELIGIBILITY_BLOCKED:
            _note_party_decline(
                rule, row, reason='offset_account_ineligible',
                detail=_party_conflict_message(offset, acct_type, party_type),
                party_type=party_type, offset_account=offset)
            return None
    name, source = ((rule.party_name or '').strip(), 'rule')
    if not name and supplier_name:
        name, source = supplier_name, 'merchant'
    if not name:
        name, source = erpnext_bank.derive_party_from_transaction(row)
    if not name:
        _note_party_decline(rule, row, reason='no_party_name',
                            detail=(f'the rule wants a {party_type} party but '
                                    'names none, the transaction has no '
                                    'merchant, and none could be derived from '
                                    'it'),
                            party_type=party_type, offset_account=offset)
        return None
    if not _auto_create_party_enabled(rule):
        # Trusted verbatim, exactly as pre-v0.9.0: with creation off, the
        # operator is asserting the party already exists.
        return party_type, name
    resolved = erpnext_bank.ensure_party(client, name, party_type, source=source,
                                        company=company)
    if not resolved:
        _note_party_decline(
            rule, row, reason='party_not_found',
            detail=(f'{party_type} “{name}” does not exist in ERPNext and '
                    'could not be created'
                    + ('' if party_type in AUTO_CREATABLE_PARTY_TYPES else
                       f' — Bank Bridge never auto-creates a {party_type} '
                       f'(see AUTO_CREATABLE_PARTY_TYPES). Create it in '
                       f'ERPNext, then re-run the rules.')),
            party_type=party_type, offset_account=offset, party_name=name)
        return None
    return party_type, resolved


def _auto_create_party_enabled(rule) -> bool:
    """Whether a missing party should be CREATED for this rule (v0.9.0).

    Tri-state by design, and the tri-state is what makes the flag additive:
    `auto_create_party` NULL means "inherit ERPNEXT_AUTO_CREATE_SUPPLIERS", which
    is what every pre-v0.9.0 rule holds, so an upgrade changes nothing. True and
    False are per-rule overrides — the kill switch is PRESENT (a rule can refuse
    to mint parties) but OFF by default, per Customer First → Safety Third."""
    flag = getattr(rule, 'auto_create_party', None)
    if flag is None:
        return bool(current_app.config.get('ERPNEXT_AUTO_CREATE_SUPPLIERS', True))
    return bool(flag)


# The reasons a party was wanted and not written. Each names something an
# operator can ACT on, which is the whole point of recording them — see the
# Fail Forward note in resolve_party.
PARTY_DECLINE_REASONS = {
    'offset_account_ineligible': "ERPNext refuses a Party on this offset "
                                 "account's account_type",
    'no_party_name': 'a party side was chosen but no name could be resolved',
    'party_not_found': 'the named party does not exist in ERPNext and was not '
                       'created',
}


def _note_party_decline(rule, row, *, reason: str, detail: str,
                        party_type: str = '', offset_account: str = '',
                        party_name: str = '') -> None:
    """Record that a party was wanted and not written — one log line and one
    permanent AuditEvent (v0.9.0).

    FAIL FORWARD, and deliberately not a raise: a JE with no party still posts,
    and the transaction is still categorized correctly. What must not happen is
    the silence — pre-v0.9.0 an ineligible offset account produced a partyless
    JE with nothing anywhere saying a party had been intended, which is exactly
    how ACC-JV-2026-02312 sat unnoticed. Never raises; an audit write that fails
    must not cost the Journal Entry."""
    log.info('[party] declined on %s: %s (%s)',
             getattr(row, 'plaid_transaction_id', '?'), reason, detail)
    try:
        audit.record('journal_entry_party_declined',
                     subject_type='BankTransaction',
                     subject_id=getattr(row, 'plaid_transaction_id', None),
                     after={'reason': reason, 'detail': detail,
                            'party_type': party_type,
                            'party_name': party_name,
                            'offset_account': offset_account,
                            'rule_id': getattr(rule, 'id', None),
                            'rule_name': getattr(rule, 'name', '')},
                     notes=f'party declined — {reason}')
    except Exception:  # noqa: BLE001 — never break a JE on an audit write
        log.warning('could not audit party decline', exc_info=True)


def suggest_skip_party(offset_account: str, company: str = '') -> bool:
    """True when `offset_account` looks like ANOTHER Bank Account you own under
    `company` — i.e. the rule books a transfer (credit-card payment, deposit,
    inter-account move) rather than a purchase, so it wants no Party (v0.4.0.7).

    Answered entirely from local data: an imported Plaid account carries the GL
    account it was linked to (`erpnext_gl_account_name`), so "is the offset a
    bank account of mine?" is a set membership test — no ERPNext round-trip, and
    it works for both offset modes. A Mode B (Company-agnostic) rule names a
    LOGICAL account, so both sides are compared logically too. A blank `company`
    considers every Company's bank accounts.

    Advisory only — this pre-checks the Rules editor's checkbox; the stored
    `skip_party` is whatever the operator actually saved."""
    offset = (offset_account or '').strip()
    if not offset:
        return False
    want = (company or '').strip()
    from . import erpnext_accounts
    for acct in PlaidAccount.query.filter(
            PlaidAccount.erpnext_gl_account_name.isnot(None)).all():
        gl = (acct.erpnext_gl_account_name or '').strip()
        if not gl:
            continue
        if want and erpnext_accounts.owning_company_for(acct) != want:
            continue
        if gl == offset or logical_account_name(gl) == logical_account_name(offset):
            return True
    return False


# ── generation (the write path) ────────────────────────────────────────

def _submit_je(client, name: str) -> None:
    """Submit an existing Draft Journal Entry, by name, in ERPNext.

    `frappe.client.submit` submits the *document object it is handed* — it does
    NOT reload the record from the database. Handing it a bare
    ``{doctype, name}`` stub therefore asks Frappe to submit an empty Journal
    Entry: no accounts, nothing that balances. Frappe rejects that, so the real
    JE silently stays Draft and the local row never leaves ``pending_review``.
    That stub was the root cause of the v0.4.0.4 "Approve does nothing" bug.

    Fetch the stored document first, then submit *that* full payload so the
    accounts, company and totals Frappe validates are the ones already on the
    record."""
    doc = client.get_doc(JOURNAL_ENTRY_DT, name)
    if not doc:
        raise ERPNextAPIError(
            f'Journal Entry {name} not found in ERPNext', status_code=404)
    # A freshly-fetched doc already carries its `name`; set it defensively so
    # the submit is unambiguous even if a caller passed a trimmed dict.
    doc = {**doc, 'name': name, 'doctype': JOURNAL_ENTRY_DT}
    client.call_method('frappe.client.submit', http_method='POST',
                       json_body={'doc': json.dumps(doc)})


def _reverse_je(client, name: str):
    """Book a reversing Journal Entry for an already-submitted JE and return the
    new reverse JE's name (or None). Uses ERPNext's own reversal helper so the
    reverse mirrors the original's accounts/party with debits and credits
    swapped, then inserts the returned draft. This is the `approved → reversed`
    "undo" — the original submitted JE is left intact for the audit trail."""
    rev = client.call_method(
        'erpnext.accounts.doctype.journal_entry.journal_entry.'
        'make_reverse_journal_entry', http_method='POST',
        json_body={'source_name': name})
    if not isinstance(rev, dict):
        raise ERPNextAPIError(
            f'ERPNext returned no reversing entry for {name}', status_code=None)
    rev.pop('name', None)          # let ERPNext autoname the fresh draft
    rev['doctype'] = JOURNAL_ENTRY_DT
    created = client.create_doc(JOURNAL_ENTRY_DT, rev)
    return created.get('name') if isinstance(created, dict) else None


def _default_company() -> str:
    return (erpnext_settings.load().get('default_company') or '').strip()


def generate_journal_entry(client, row, *, supplier_name=None,
                           rule: CategorizationRule | None = None):
    """Run the rules engine for one transaction and, on a match, create the
    ERPNext Journal Entry + record a GeneratedJournalEntry. Idempotent on the
    transaction id. Returns the GeneratedJournalEntry row, or None when nothing
    matched / it was already generated. Never raises — failures are recorded on
    an `error` audit row. Emits AuditEvents (rule_matched, journal_entry_*)."""
    tid = row.plaid_transaction_id
    # Idempotency: one JE per transaction. A prior success (has a JE docname)
    # short-circuits; a prior `error` row is allowed to retry.
    gje = GeneratedJournalEntry.query.filter_by(plaid_transaction_id=tid).first()
    if gje is not None and gje.erpnext_journal_entry_name:
        return gje

    if rule is None:
        rule, trace = evaluate_rules(row)
    else:
        trace = [{'rule_id': rule.id, 'rule_name': rule.name,
                  'priority': rule.priority, 'matched': True}]
    # Permanent record of what the engine evaluated and which rule won — the
    # basis for reconstructing any past auto-JE decision.
    audit.record('rule_matched', subject_type='BankTransaction', subject_id=tid,
                 after={'winner': (rule.id if rule else None),
                        'winner_name': (rule.name if rule else None),
                        'merchant_name': row.merchant_name,
                        'amount': row.amount, 'evaluated': trace},
                 notes=f'{len(trace)} rule(s) evaluated'
                       + ('' if rule else ' — no match'))
    if rule is None:
        return None  # no rule matched → leave for manual reconciliation

    # v0.4.49 · stamp the internal attribution tag the moment the rule is known
    # to match — BEFORE any of the JE machinery below, because a rule matching
    # is what earns the tag and JE generation is a separate concern that may
    # legitimately skip or fail. Bank-Bridge-internal; it goes nowhere near the
    # JE payload built later in this function.
    apply_internal_tag(row, rule)

    # v0.4.0 multi-entity: the JE books to the Company that owns the
    # transaction's Bank Account (per-account/Item choice → default).
    from . import erpnext_accounts
    company = erpnext_accounts.owning_company_for_account_id(
        getattr(row, 'account_id', None))
    remark = render_description(rule, row, supplier_name=supplier_name)

    # v0.4.0.7 · settle the party FIRST — before the GeneratedJournalEntry row
    # is staged — so whatever name lands on the offset line is one ERPNext
    # already has a Supplier for. This is the single choke point every caller
    # funnels through (the sync path, "Rerun rules", the per-row Retry), so a
    # description-derived party can no longer 417 the JE create.
    #
    # The ORDER here is load-bearing, not stylistic: resolve_party ends in a
    # Supplier auto-create, which commits (on success) or rolls the session back
    # (on failure). Either would clobber pending, uncommitted changes to `gje` —
    # a rollback expunges a never-flushed row outright, so the JE would post to
    # ERPNext while the local row that guarantees one-JE-per-transaction
    # silently vanished, and the next "Rerun rules" would double-post. Staging
    # `gje` only after the Supplier work is done keeps the two apart.
    resolved_party = resolve_party(client, rule, row,
                                   supplier_name=supplier_name, company=company)
    party_type, party = resolved_party if resolved_party else ('', '')

    if gje is None:
        gje = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id=tid).first()
    if gje is None:
        gje = GeneratedJournalEntry(plaid_transaction_id=tid)
        db.session.add(gje)
    gje.rule_id = rule.id
    gje.rule_name = (rule.name or '')[:255]
    gje.amount = abs(float(row.amount or 0.0))
    gje.merchant_name = (row.merchant_name or '')[:255]
    gje.description = remark
    gje.updated_at = _now()

    cfg = current_app.config
    try:
        # v0.4.0.3 · two-mode offset. A SCOPED rule (applies_to_company) uses its
        # offset_account verbatim (Mode A). An AGNOSTIC rule is Mode B ONLY when
        # its offset is a bare LOGICAL name ('Meals & Entertainment'): that name
        # is resolved to an account under THIS transaction's Company, and a
        # Company lacking one is skipped (no JE, no auto-created account) and
        # surfaced for the operator. An agnostic rule whose offset is still
        # fully-qualified ('Meals & Entertainment - BBT' — a legacy value, a
        # single-Company install, or one not yet auto-migrated) is used verbatim,
        # exactly like pre-.3; the push-time guard remains its cross-Company
        # backstop. The shape test (logical_account_name is a fixed point on an
        # already-logical name) is what distinguishes the two.
        offset_override = None
        is_agnostic = not (getattr(rule, 'applies_to_company', None) or '').strip()
        offset = (rule.offset_account or '').strip()
        logical = offset if logical_account_name(offset) == offset else ''
        if is_agnostic and logical:
            offset_override = erpnext_accounts.resolve_logical_account(
                client, logical, company)
            if offset_override is None:
                msg = (f"Skipped: Company “{company}” has no account named "
                       f"“{logical}”. Create it (or map this transaction's "
                       "account to a Company that has it), then re-run the rules.")
                gje.state = 'skipped_missing_account'
                gje.error_message = msg[:2000]
                gje.updated_at = _now()
                db.session.commit()
                log.warning('Journal Entry SKIPPED (missing account) for %s: '
                            'no “%s” under %s', tid, logical, company)
                audit.record('journal_entry_skipped_missing_account',
                             subject_type='GeneratedJournalEntry',
                             subject_id=gje.id,
                             after={'plaid_transaction_id': tid,
                                    'rule_id': rule.id, 'rule_name': rule.name,
                                    'company': company,
                                    'logical_account': logical},
                             notes=f'rule “{rule.name}” — no “{logical}” under '
                                   f'{company}')
                return gje
        # v0.5.15 (Option A) · OWNER CONTRIBUTION / MEMBER DISTRIBUTION routing.
        # A transaction Tim tags 'owner_contribution' or 'member_distribution'
        # (via a rule's bb_internal_tag) is external capital moving in/out of the
        # brokerage — its cash leg belongs on 1099 Cash Clearing (netting the
        # sec-side that debited/credited Clearing on the trades that capital
        # funded), NOT on the account's own bank GL. The offset stays the rule's
        # equity account (3200 Member Contributions / 3201 Member Distribution)
        # and the operator's offset_direction decides the sign. Only overridden
        # for a paired-brokerage companion, where a Cash Clearing bridge exists.
        bank_override = _contribution_bank_leg(client, row, company)
        doc = build_journal_entry(rule, row, company,
                                  supplier_name=supplier_name, remark=remark,
                                  offset_account_override=offset_override,
                                  bank_account=bank_override,
                                  party_override=(party or ''),
                                  party_type_override=(party_type or ''))
        # v0.4.0.2 retroactive guard: refuse to post a JE that references a GL
        # account from a different Company than the target (belt-and-suspenders
        # behind the scoped Offset Account dropdown). A mismatch is a blocked,
        # not a failed, JE — it's a configuration error, not a transient one.
        mismatches = erpnext_accounts.je_company_mismatches(client, doc)
        if mismatches:
            detail = '; '.join(
                f"{m['account']} belongs to {m['account_company']}, "
                f"not {m['expected']}" for m in mismatches)
            msg = ('Blocked: cross-Company account reference — ' + detail
                   + '. Re-scope the rule (Applies to Company) or pick an '
                   'Offset Account under the transaction\'s Company.')
            gje.state = 'blocked'
            gje.error_message = msg[:2000]
            gje.updated_at = _now()
            db.session.commit()
            log.warning('Journal Entry BLOCKED (cross-Company) for %s: %s',
                        tid, detail)
            audit.record('journal_entry_blocked_cross_company',
                         subject_type='GeneratedJournalEntry', subject_id=gje.id,
                         after={'plaid_transaction_id': tid, 'rule_id': rule.id,
                                'rule_name': rule.name, 'company': company,
                                'mismatches': mismatches},
                         notes=f'rule “{rule.name}” blocked — {detail}')
            return gje
        # v0.8.5 · LAST GATE BEFORE THE WRITE — does ERPNext already carry a JE
        # for this Bank Transaction? The local GeneratedJournalEntry row above is
        # the primary idempotency guard; this is the one that still works after
        # that row has been deleted, restored from a stale volume, or never
        # written because a migration marker arrived late. See app/je_dedup.py.
        #
        # Fail Safe by construction: the lookup answers None on every difficulty,
        # and None means "create it". A dedup check can cost us a duplicate
        # draft; it must never cost us the entry.
        existing = je_dedup.find_by_bank_transaction(
            client, row.erpnext_bank_transaction_id, company=company)
        if existing is not None:
            reason = je_dedup.record_skip(
                'categorization', f'Bank Transaction '
                f'{row.erpnext_bank_transaction_id}', existing,
                subject_id=gje.id,
                extra={'plaid_transaction_id': tid, 'rule_id': rule.id,
                       'rule_name': rule.name, 'company': company})
            gje.state = 'dedup_skipped'
            gje.error_message = reason[:2000]
            gje.updated_at = _now()
            db.session.commit()
            return gje
        created = client.create_doc(JOURNAL_ENTRY_DT, doc)
        name = created.get('name')
        if not name:
            raise ERPNextAPIError('ERPNext returned no Journal Entry name',
                                  status_code=None)
        gje.erpnext_journal_entry_name = name
        submitted = cfg.get('ERPNEXT_JOURNAL_ENTRY_AUTO_SUBMIT', False)
        if submitted:
            _submit_je(client, name)
            gje.state = 'approved'
        else:
            gje.state = cfg.get('ERPNEXT_JOURNAL_ENTRY_REVIEW_STATE',
                                'pending_review') or 'pending_review'
        gje.error_message = None
        db.session.commit()
        log.info('generated Journal Entry %s for %s (rule %s)', name, tid, rule.id)
        audit.record('journal_entry_generated',
                     subject_type='GeneratedJournalEntry', subject_id=gje.id,
                     after={'journal_entry': name, 'state': gje.state,
                            'rule_id': rule.id, 'rule_name': rule.name,
                            'plaid_transaction_id': tid, 'doc': doc},
                     notes=f'rule “{rule.name}” → {name}')
        if submitted:
            audit.record('journal_entry_submitted_to_erpnext',
                         subject_type='GeneratedJournalEntry', subject_id=gje.id,
                         after={'journal_entry': name, 'auto_submit': True},
                         notes='auto-submitted on generation')
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        # Re-load the row (rollback detached it) and record the failure.
        gje = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id=tid).first()
        if gje is None:
            gje = GeneratedJournalEntry(plaid_transaction_id=tid, rule_id=rule.id)
            db.session.add(gje)
        gje.state = 'error'
        gje.rule_id = rule.id
        gje.rule_name = (rule.name or '')[:255]
        gje.amount = abs(float(row.amount or 0.0))
        gje.merchant_name = (row.merchant_name or '')[:255]
        gje.error_message = str(e)[:2000]
        gje.updated_at = _now()
        db.session.commit()
        log.warning('Journal Entry generation failed for %s: %s', tid, e)
        audit.record('journal_entry_failed',
                     subject_type='GeneratedJournalEntry', subject_id=gje.id,
                     after={'plaid_transaction_id': tid, 'rule_id': rule.id,
                            'error': str(e)[:2000]},
                     notes=f'rule “{rule.name}” failed')
    return gje


class JournalEntryGateOff(Exception):
    """Raised by `rerun_rules` when JE generation is switched off. A rerun that
    silently generated nothing would look identical to a rerun that found
    nothing to do, and an operator would go hunting through the rules."""


def rerun_rules(erp_client) -> dict:
    """Re-run the CURRENT rules over posted, non-removed transactions that have
    no generated Journal Entry yet. Returns {'considered', 'matched',
    'generated', 'dedup_skipped'}.

    Rule edits never re-run retroactively on their own — this is the deliberate
    opt-in path, and it is idempotent: a transaction that already produced a JE
    is skipped by name, so running it twice generates nothing the second time.

    v0.8.4 · lifted out of the admin route so the MCP tool and the button run
    the SAME code rather than two copies that drift. The gate check came with
    it: the route never had one, so a rerun would post JEs while
    /admin/erpnext_settings said generation was off."""
    if erp_client is None:
        raise JournalEntryGateOff(
            'ERPNext is not configured — cannot generate Journal Entries.')
    if not erpnext_settings.je_generation_enabled():
        raise JournalEntryGateOff(
            'Journal Entry generation is OFF — turn it on under ERPNext '
            'settings before rerunning rules, or nothing will be posted.')
    # v0.8.5 · a `dedup_skipped` row counts as DONE. It carries no JE docname
    # (Bank Bridge did not create the entry ERPNext already holds), so without
    # this clause every rerun would re-ask ERPNext about the same transaction
    # forever and re-audit the same skip. The decision is settled; re-deciding
    # it is chronos, not kairos.
    done = {row.plaid_transaction_id for row in
            db.session.query(GeneratedJournalEntry.plaid_transaction_id)
            .filter(db.or_(
                GeneratedJournalEntry.erpnext_journal_entry_name.isnot(None),
                GeneratedJournalEntry.state == 'dedup_skipped'))}
    eligible = (BankTransaction.query
                .filter(BankTransaction.posted_at.isnot(None),
                        BankTransaction.removed.is_(False)).all())
    stats = {'considered': 0, 'matched': 0, 'generated': 0, 'dedup_skipped': 0}
    for row in eligible:
        if row.plaid_transaction_id in done:
            continue
        stats['considered'] += 1
        gje = generate_journal_entry(erp_client, row)
        if gje is not None:
            stats['matched'] += 1
            if gje.erpnext_journal_entry_name:
                stats['generated'] += 1
            elif (gje.state or '') == 'dedup_skipped':
                stats['dedup_skipped'] += 1
    # v0.4.6 · a Rerun is the one moment match counts change in bulk, and the
    # operator's next stop is the Rules tab to see what stuck. Rolling up inline
    # (a local read + a write per changed rule) beats showing them a column that
    # is a day out of date at exactly the moment they're relying on it.
    from . import rule_stats
    rule_stats.rollup_match_counts()
    audit.record('rules_rerun', subject_type=None, after=stats,
                 notes=(f"reran current rules on {stats['considered']} eligible "
                        f"transaction(s) → {stats['generated']} JE(s)"))
    return stats


def categorize_after_push(erp_client, row):
    """The sync-path hook, called right after a Bank Transaction is posted +
    committed. Best-effort and self-contained: auto-creates the Supplier (when
    enabled) and, when the JE engine is enabled, generates the Journal Entry.
    Catches everything — categorization must never fail the transaction sync.

    v0.8.5 · returns the GeneratedJournalEntry row (or None) so the push loop can
    count a `dedup_skipped` outcome in its summary. Every pre-v0.8.5 caller
    ignored the return, so handing one back changes nothing for them."""
    if erp_client is None or row is None or row.removed:
        return None
    cfg = current_app.config
    supplier_name = None
    if cfg.get('ERPNEXT_AUTO_CREATE_SUPPLIERS', True) and row.merchant_name:
        try:
            supplier_name = erpnext_bank.get_or_create_supplier(
                erp_client, row.merchant_name, amount=row.amount,
                txn_date=row.date)
        except Exception:  # noqa: BLE001 - never fail the sync on supplier work
            db.session.rollback()
            log.warning('auto-supplier failed for %s', row.plaid_transaction_id,
                        exc_info=True)
    # v0.8.4 · the persisted toggle, which the env var only seeds. Reading the
    # raw config key here would let /admin/erpnext_settings show the gate OFF
    # while the sync went on posting.
    if not erpnext_settings.je_generation_enabled():
        return None
    try:
        return generate_journal_entry(erp_client, row,
                                      supplier_name=supplier_name)
    except Exception:  # noqa: BLE001 - defensive; generate_* already guards
        db.session.rollback()
        log.warning('rules engine failed for %s', row.plaid_transaction_id,
                    exc_info=True)
    return None
