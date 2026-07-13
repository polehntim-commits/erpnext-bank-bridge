# SPDX-License-Identifier: MIT
"""ERPNext Bank Transaction bridge — the orchestration above erpnext_client.

Maps a local Plaid `BankTransaction` → an ERPNext `Bank Transaction` doctype
row and keeps the two in sync:

  * create  → find-or-create by reference_number (the Plaid transaction id),
              then SUBMIT (docstatus 0 → 1) so it appears in ERPNext's Bank
              Reconciliation Tool. The returned docname is written back onto
              the local row.
  * modify  → Plaid's `modified` list. A submitted Bank Transaction is
              immutable in Frappe, so we CANCEL the existing doc (docstatus 2)
              and create+submit a fresh one, repointing the local row.
  * remove  → Plaid's `removed` list. CANCEL the existing doc (docstatus 2).

Idempotent: every create first checks ERPNext for an existing Bank Transaction
with the same reference_number, so a re-run (or a lost local pointer) never
double-posts.

The Bank Transaction doctype ships with ERPNext (Accounts module) — no custom
fields are required; `verify_doctype()` confirms it's reachable. The HTTP
mechanics live in erpnext_client; this module owns the field mapping, the
idempotency rules, and the deposit/withdrawal sign translation.

Plaid amount convention: POSITIVE = money out of the account (withdrawal);
NEGATIVE = money in (deposit). ERPNext wants both as positive numbers in
separate fields."""
from __future__ import annotations

import logging

from . import erpnext_settings
from .erpnext_client import (ERPNextAPIError, ERPNextClient, ERPNextConfig,
                             ERPNextConfigError, ERPNextError)

log = logging.getLogger('bankbridge.erpnext')

DOCTYPE = 'Bank Transaction'


# ── configuration / client ────────────────────────────────────────────

def get_config() -> ERPNextConfig:
    s = erpnext_settings.load()
    return ERPNextConfig(url=s['url'], api_key=s['api_key'],
                         api_secret=s['api_secret'],
                         default_company=s['default_company'])


def get_client(**kwargs) -> ERPNextClient:
    """Build a client from the merged (env + JSON) ERPNext settings. Raises
    ERPNextConfigError if the connection isn't configured yet."""
    return ERPNextClient(get_config(), **kwargs)


# ── probes ─────────────────────────────────────────────────────────────

def test_connection(client: ERPNextClient | None = None):
    """Probe GET frappe.auth.get_logged_user. Returns (ok, detail)."""
    try:
        client = client or get_client()
    except ERPNextConfigError as e:
        return False, str(e)
    try:
        user = client.get_logged_user()
        return True, user
    except ERPNextAPIError as e:
        return False, str(e)


def verify_doctype(client: ERPNextClient | None = None):
    """Confirm the Bank Transaction doctype is reachable (it's a stock ERPNext
    doctype; no custom fields are needed for our mapping). Returns (ok, detail).
    A 403 means the API user lacks read permission on the doctype."""
    try:
        client = client or get_client()
    except ERPNextConfigError as e:
        return False, str(e)
    try:
        client.list_docs(DOCTYPE, fields=['name'], limit_page_length=1)
        return True, 'Bank Transaction doctype reachable (no custom fields needed).'
    except ERPNextAPIError as e:
        return False, f'{e} — {e.response_body[:200]}'


def list_bank_accounts(client: ERPNextClient | None = None) -> list[dict]:
    """Enabled ERPNext Bank Accounts for the account-mapping dropdown."""
    client = client or get_client()
    return client.list_docs(
        'Bank Account', filters=[['disabled', '=', 0]],
        fields=['name', 'account_name', 'bank_account_no', 'bank'],
        limit_page_length=0)


# ── field mapping ──────────────────────────────────────────────────────

def _deposit_withdrawal(amount: float) -> tuple[float, float]:
    """(deposit, withdrawal) — both non-negative. Plaid positive = outflow."""
    amt = float(amount or 0.0)
    if amt > 0:
        return 0.0, round(amt, 2)      # money out → withdrawal
    if amt < 0:
        return round(-amt, 2), 0.0     # money in → deposit
    return 0.0, 0.0


def _description(txn) -> str:
    name = (txn.name or '').strip()
    merchant = (txn.merchant_name or '').strip()
    if merchant and merchant.lower() not in name.lower():
        return f'{name} — {merchant}' if name else merchant
    return name or merchant or '(no description)'


def build_doc(txn, bank_account_name: str) -> dict:
    """Assemble the ERPNext Bank Transaction payload for a local BankTransaction.
    `reference_number` carries the Plaid transaction id — the idempotency key."""
    deposit, withdrawal = _deposit_withdrawal(txn.amount)
    doc = {
        'date': txn.date.isoformat() if txn.date else None,
        'bank_account': bank_account_name,
        'deposit': deposit,
        'withdrawal': withdrawal,
        'description': _description(txn),
        'reference_number': txn.plaid_transaction_id,
        'currency': txn.iso_currency_code or 'USD',
    }
    return {k: v for k, v in doc.items() if v is not None}


# ── low-level submit / cancel ──────────────────────────────────────────

def _submit(client: ERPNextClient, name: str) -> None:
    """Submit a doc (docstatus 0 → 1) so it's available for reconciliation."""
    import json
    client.call_method('frappe.client.submit', http_method='POST',
                       json_body={'doc': json.dumps(
                           {'doctype': DOCTYPE, 'name': name})})


def _cancel(client: ERPNextClient, name: str) -> None:
    """Cancel a submitted doc (docstatus 1 → 2)."""
    client.call_method('frappe.client.cancel', http_method='POST',
                       json_body={'doctype': DOCTYPE, 'name': name})


def _find_existing(client: ERPNextClient, reference_number: str):
    """Return the docname of an existing NON-cancelled Bank Transaction with
    this Plaid id, or None. Makes create idempotent even if the local pointer
    was lost. The docstatus<2 filter is essential for the modified-repost path:
    a cancelled doc keeps its reference_number, and without this filter
    find-or-create would resurrect the cancelled doc instead of creating a
    fresh submitted replacement."""
    if not reference_number:
        return None
    matches = client.list_docs(
        DOCTYPE, filters=[['reference_number', '=', reference_number],
                          ['docstatus', '<', 2]],
        fields=['name'], limit_page_length=1)
    return matches[0]['name'] if matches else None


# ── public operations ──────────────────────────────────────────────────

def create_bank_transaction(client: ERPNextClient, txn, bank_account_name: str,
                            *, submit: bool = True) -> str:
    """Find-or-create the ERPNext Bank Transaction for `txn`, submit it, and
    return its docname. Idempotent on reference_number (Plaid id)."""
    existing = _find_existing(client, txn.plaid_transaction_id)
    if existing:
        return existing
    doc = build_doc(txn, bank_account_name)
    created = client.create_doc(DOCTYPE, doc)
    name = created.get('name')
    if not name:
        raise ERPNextAPIError('ERPNext returned no Bank Transaction name',
                              status_code=None)
    if submit:
        try:
            _submit(client, name)
        except ERPNextAPIError:
            # Leave it as a draft rather than failing the whole sync — the
            # doc exists and reconciliation still works after a manual submit.
            log.warning('submit failed for %s; left as draft', name)
    return name


# ── Phase 2 scaffold: merchant → Supplier auto-suggest ─────────────────
#
# NOT IMPLEMENTED — intentional stub so the next feature has a seam to grow
# into. The plan: when a transaction is an outflow (a payment), look at its
# merchant_name / normalized name and suggest (or auto-create + link) a matching
# ERPNext Supplier, so a Bank Transaction can be reconciled straight against a
# Purchase Invoice / Payment Entry instead of being categorized by hand. This is
# where auto-categorization (Plaid personal_finance_category → ERPNext expense
# account / party) will also hang.
#
# Deliberately does nothing today: no ERPNext writes, no suggestions surfaced.
# The push path does not call it yet.

def _maybe_suggest_supplier(transaction):  # noqa: ARG001 - Phase 2 placeholder
    """(Phase 2 — scaffold only.) Suggest an ERPNext Supplier for a transaction.

    `transaction` is a local BankTransaction. A future implementation will
    fuzzy-match transaction.merchant_name (falling back to transaction.name)
    against existing ERPNext Suppliers and return a suggestion — or, gated
    behind an opt-in setting, find-or-create the Supplier and attach it to the
    Bank Transaction / a draft Payment Entry.

    Returns None today (no-op). See the module note above for the full intent.
    """
    # TODO(phase-2): implement merchant → Supplier match + auto-categorization.
    return None


def cancel_bank_transaction(client: ERPNextClient, name: str) -> None:
    """Cancel a posted Bank Transaction (Plaid removed / superseded). Tolerates
    an already-cancelled or missing doc."""
    if not name:
        return
    try:
        _cancel(client, name)
    except ERPNextAPIError as e:
        # 417/409 → already cancelled or not submittable; 404 → gone. All are
        # acceptable end-states for a cancel.
        if e.status_code in (404, 409, 417):
            log.info('cancel of %s no-op (%s)', name, e.status_code)
            return
        raise
