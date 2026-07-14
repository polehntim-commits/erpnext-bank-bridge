# SPDX-License-Identifier: MIT
"""Categorization rules engine + Journal Entry generator (v0.3.0).

Given a local `BankTransaction` that's just been posted to ERPNext, walk the
active `CategorizationRule` rows in priority order (lower = higher priority) and
let the FIRST match generate an ERPNext Journal Entry:

  * debit the rule's `debit_account` (the expense / party side)
  * credit the rule's `credit_account` (usually the Bank Account)
  * for an INFLOW (a Plaid deposit / refund, amount < 0) the two sides swap so
    money lands in the bank — the rule is authored for the common outflow case.

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
from jinja2.sandbox import SandboxedEnvironment

from . import db
from . import erpnext_bank
from . import erpnext_settings
from .erpnext_client import ERPNextAPIError, ERPNextError
from .models import CategorizationRule, GeneratedJournalEntry

log = logging.getLogger('bankbridge.categorization')

JOURNAL_ENTRY_DT = 'Journal Entry'

# match_type values the engine understands.
MATCH_TYPES = ('merchant_exact', 'merchant_contains', 'description_regex',
               'plaid_category_matches', 'amount_range')

_jinja = SandboxedEnvironment(autoescape=False)


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


def find_matching_rule(row) -> CategorizationRule | None:
    """The first ACTIVE rule (priority ascending, id as tiebreak) that matches
    the transaction, or None."""
    rules = (CategorizationRule.query
             .filter(CategorizationRule.active.is_(True))
             .order_by(CategorizationRule.priority.asc(),
                       CategorizationRule.id.asc()).all())
    for rule in rules:
        if rule_matches(rule, merchant_name=row.merchant_name,
                        description=(row.name or ''), category=(row.category or ''),
                        amount=row.amount):
            return rule
    return None


# ── Journal Entry construction ─────────────────────────────────────────

def render_description(rule: CategorizationRule, row, supplier_name=None) -> str:
    """Render the rule's Jinja `description_template` against the transaction.
    Falls back to a sensible default remark when the template is blank or errors
    (a bad template must never block generation)."""
    default = (f'{rule.name or "Auto"} — '
               f'{row.merchant_name or row.name or "transaction"} '
               f'{row.date.isoformat() if row.date else ""}').strip()
    tmpl = (rule.description_template or '').strip()
    if not tmpl:
        return default
    ctx = {
        'merchant_name': row.merchant_name or '',
        'name': row.name or '',
        'description': row.name or '',
        'date': row.date.isoformat() if row.date else '',
        'amount': abs(float(row.amount or 0.0)),
        'category': row.category or '',
        'supplier_name': supplier_name or '',
        'rule_name': rule.name or '',
    }
    try:
        return _jinja.from_string(tmpl).render(**ctx)
    except Exception:  # noqa: BLE001 - any Jinja error → default remark
        log.warning('description_template render failed for rule %s; using default',
                    rule.id, exc_info=True)
        return default


def build_journal_entry(rule: CategorizationRule, row, company: str, *,
                        supplier_name=None, remark: str = '') -> dict:
    """Assemble the ERPNext Journal Entry payload for a matched transaction.

    Two lines: the rule's debit_account (expense/party side) and credit_account
    (bank side). Plaid's sign convention (positive = outflow) decides which side
    carries the debit vs credit; for an inflow the sides reverse. The optional
    party rides the expense/party line — `rule.party_name` wins, else the
    auto-created Supplier for this merchant."""
    amt = round(abs(float(row.amount or 0.0)), 2)
    outflow = float(row.amount or 0.0) >= 0

    party_line = {'account': rule.debit_account}
    bank_line = {'account': rule.credit_account}
    if outflow:
        party_line['debit_in_account_currency'] = amt
        bank_line['credit_in_account_currency'] = amt
    else:
        party_line['credit_in_account_currency'] = amt
        bank_line['debit_in_account_currency'] = amt

    party = rule.party_name or supplier_name
    if rule.party_type and party:
        party_line['party_type'] = rule.party_type
        party_line['party'] = party

    if row.erpnext_bank_transaction_id:
        for ln in (party_line, bank_line):
            ln['reference_type'] = 'Bank Transaction'
            ln['reference_name'] = row.erpnext_bank_transaction_id

    doc = {
        'doctype': JOURNAL_ENTRY_DT,
        'voucher_type': 'Journal Entry',
        'company': company,
        'user_remark': remark,
        'accounts': [party_line, bank_line],
    }
    if row.date:
        doc['posting_date'] = row.date.isoformat()
    return doc


# ── generation (the write path) ────────────────────────────────────────

def _submit_je(client, name: str) -> None:
    client.call_method('frappe.client.submit', http_method='POST',
                       json_body={'doc': json.dumps(
                           {'doctype': JOURNAL_ENTRY_DT, 'name': name})})


def _default_company() -> str:
    return (erpnext_settings.load().get('default_company') or '').strip()


def generate_journal_entry(client, row, *, supplier_name=None,
                           rule: CategorizationRule | None = None):
    """Run the rules engine for one transaction and, on a match, create the
    ERPNext Journal Entry + record a GeneratedJournalEntry. Idempotent on the
    transaction id. Returns the GeneratedJournalEntry row, or None when nothing
    matched / it was already generated. Never raises — failures are recorded on
    an `error` audit row."""
    tid = row.plaid_transaction_id
    # Idempotency: one JE per transaction. A prior success (has a JE docname)
    # short-circuits; a prior `error` row is allowed to retry.
    audit = GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=tid).first()
    if audit is not None and audit.erpnext_journal_entry_name:
        return audit

    rule = rule or find_matching_rule(row)
    if rule is None:
        return None  # no rule matched → leave for manual reconciliation

    company = _default_company()
    remark = render_description(rule, row, supplier_name=supplier_name)

    if audit is None:
        audit = GeneratedJournalEntry(plaid_transaction_id=tid)
        db.session.add(audit)
    audit.rule_id = rule.id
    audit.rule_name = (rule.name or '')[:255]
    audit.amount = abs(float(row.amount or 0.0))
    audit.merchant_name = (row.merchant_name or '')[:255]
    audit.description = remark
    audit.updated_at = _now()

    cfg = current_app.config
    try:
        doc = build_journal_entry(rule, row, company,
                                  supplier_name=supplier_name, remark=remark)
        created = client.create_doc(JOURNAL_ENTRY_DT, doc)
        name = created.get('name')
        if not name:
            raise ERPNextAPIError('ERPNext returned no Journal Entry name',
                                  status_code=None)
        audit.erpnext_journal_entry_name = name
        if cfg.get('ERPNEXT_JOURNAL_ENTRY_AUTO_SUBMIT', False):
            _submit_je(client, name)
            audit.state = 'approved'
        else:
            audit.state = cfg.get('ERPNEXT_JOURNAL_ENTRY_REVIEW_STATE',
                                  'pending_review') or 'pending_review'
        audit.error_message = None
        db.session.commit()
        log.info('generated Journal Entry %s for %s (rule %s)',
                 name, tid, rule.id)
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        # Re-load the audit row (rollback detached it) and record the failure.
        audit = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id=tid).first()
        if audit is None:
            audit = GeneratedJournalEntry(plaid_transaction_id=tid,
                                          rule_id=rule.id)
            db.session.add(audit)
        audit.state = 'error'
        audit.rule_name = (rule.name or '')[:255]
        audit.amount = abs(float(row.amount or 0.0))
        audit.merchant_name = (row.merchant_name or '')[:255]
        audit.error_message = str(e)[:2000]
        audit.updated_at = _now()
        db.session.commit()
        log.warning('Journal Entry generation failed for %s: %s', tid, e)
    return audit


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
