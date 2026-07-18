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

from flask import current_app

from . import audit
from . import db
from . import erpnext_bank
from . import erpnext_settings
from .erpnext_client import ERPNextAPIError, ERPNextError
from .models import CategorizationRule, GeneratedJournalEntry, PlaidAccount

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
    Company is resolved once (not per rule) to keep this cheap."""
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
        matched = (_rule_applies_to_company(rule, row_company)
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


def build_journal_entry(rule: CategorizationRule, row, company: str, *,
                        supplier_name=None, remark: str = '',
                        bank_account: str | None = None,
                        offset_account_override: str | None = None) -> dict:
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

    The optional party rides the offset line — `rule.party_name` wins, else the
    auto-created Supplier for this merchant."""
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

    party = rule.party_name or supplier_name
    if rule.party_type and party:
        party_line['party_type'] = rule.party_type
        party_line['party'] = party

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

    # v0.4.0 multi-entity: the JE books to the Company that owns the
    # transaction's Bank Account (per-account/Item choice → default).
    from . import erpnext_accounts
    company = erpnext_accounts.owning_company_for_account_id(
        getattr(row, 'account_id', None))
    remark = render_description(rule, row, supplier_name=supplier_name)

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
        doc = build_journal_entry(rule, row, company,
                                  supplier_name=supplier_name, remark=remark,
                                  offset_account_override=offset_override)
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


def categorize_after_push(erp_client, row) -> None:
    """The sync-path hook, called right after a Bank Transaction is posted +
    committed. Best-effort and self-contained: auto-creates the Supplier (when
    enabled) and, when the JE engine is enabled, generates the Journal Entry.
    Catches everything — categorization must never fail the transaction sync."""
    if erp_client is None or row is None or row.removed:
        return
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
    if not cfg.get('ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES', False):
        return
    try:
        generate_journal_entry(erp_client, row, supplier_name=supplier_name)
    except Exception:  # noqa: BLE001 - defensive; generate_* already guards
        db.session.rollback()
        log.warning('rules engine failed for %s', row.plaid_transaction_id,
                    exc_info=True)
