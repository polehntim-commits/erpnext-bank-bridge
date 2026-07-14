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
import re
from datetime import datetime, timezone

from flask import current_app

from . import audit
from . import db
from . import erpnext_settings
from .erpnext_client import (ERPNextAPIError, ERPNextClient, ERPNextConfig,
                             ERPNextConfigError, ERPNextError)
from .models import PlaidSyncLog, Supplier

log = logging.getLogger('bankbridge.erpnext')

DOCTYPE = 'Bank Transaction'
SUPPLIER_DT = 'Supplier'


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def list_accounts(client: ERPNextClient | None = None) -> list[dict]:
    """Non-group ERPNext GL Accounts (Chart of Accounts leaves) for the rule
    debit/credit-account dropdowns. Ordered by name so the datalist is tidy.

    v0.3.1: filters are scoped to real, usable posting accounts only —
    `is_group=0` (leaves, not the parent groups the auto-CoA import creates) and
    `disabled=0` — but deliberately NOT by account_type/root_type, so every leaf
    (Bank, Cash, Expense, Income, …), including the auto-created Bank Accounts
    under the '1200' group, is offered. Scoped to the configured default company
    when one is set so a multi-company instance doesn't cross-list. `limit_page_
    length=0` returns every match (no 20-row default cap that would hide
    accounts)."""
    client = client or get_client()
    filters = [['is_group', '=', 0], ['disabled', '=', 0]]
    company = (erpnext_settings.load().get('default_company') or '').strip()
    if company:
        filters.append(['company', '=', company])
    return client.list_docs(
        'Account', filters=filters,
        fields=['name', 'account_type', 'root_type'],
        order_by='name asc', limit_page_length=0)


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


# ── v0.3.0: merchant → Supplier auto-create ────────────────────────────
#
# When a pushed Plaid transaction carries a merchant we've never seen, mint the
# matching ERPNext Supplier so the Bank Transaction is linkable during
# reconciliation. A local `Supplier` mirror caches merchant → ERPNext docname so
# the common path is a cheap DB lookup, not an ERPNext round-trip.

# Payment-processor / point-of-sale prefixes Plaid leaves on the raw `name`
# (and sometimes merchant_name). Stripped (case-insensitively, longest first)
# before title-casing so "SQ *STARBUCKS" and "STARBUCKS" collapse to one
# Supplier. Each entry is the literal leading token.
_STRIP_PREFIXES = (
    'paypal *', 'paypal*', 'pp *', 'pp*',   # PayPal
    'sq *', 'sq*',                          # Square
    'tst* ', 'tst*',                        # Toast
    'in *', 'in*',                          # Intuit
    'sp *', 'sp*',                          # Shopify
    'ci* ', 'ci*',                          # ci
    'pos ', 'pos debit ', 'pos purchase ',  # generic point-of-sale
    'dd *', 'dd*',                          # DoorDash processor prefix
    'chkcard ', 'chkcardpurchase ',         # bank card-purchase prefixes
    'ach debit ', 'ach credit ',
)

# Marketplace aliases: if the (lowercased) raw string CONTAINS the key, the
# Supplier collapses to the canonical brand regardless of the surrounding store
# id / marketplace suffix. Order matters — first hit wins.
_ALIASES = (
    ('amzn mktp', 'Amazon'), ('amazon mktp', 'Amazon'), ('amzn', 'Amazon'),
    ('amazon.com', 'Amazon'), ('amazon', 'Amazon'),
    ('wal-mart', 'Walmart'), ('walmart', 'Walmart'), ('wm supercenter', 'Walmart'),
    ('the home depot', 'The Home Depot'), ('home depot', 'The Home Depot'),
)

# Trailing store / location id: an optional '#', a digit, then any run of
# digits / dashes / spaces to end-of-string ("Chevron 0123456", "Costco #487").
_TRAILING_ID_RE = re.compile(r'\s*#?\s*\d[\d\-\s]*$')


def normalize_merchant_name(raw: str) -> str:
    """Clean a raw Plaid merchant/name string into a stable Supplier key.

    Rules (in order):
      1. Marketplace alias — a known brand anywhere in the string wins
         ("AMZN Mktp US*2X4…" → "Amazon").
      2. Strip a leading payment-processor / POS prefix ("SQ *STARBUCKS" →
         "STARBUCKS").
      3. Drop a trailing store / location id ("STARBUCKS 92104" → "STARBUCKS").
      4. Collapse leftover '*' and repeated whitespace.
      5. Title-case an ALL-CAPS string ("STARBUCKS" → "Starbucks") while leaving
         an already mixed-case name ("Blue Bottle") untouched.
    Returns '' for a blank input."""
    s = (raw or '').strip()
    if not s:
        return ''
    low = s.lower()
    for key, canonical in _ALIASES:
        if key in low:
            return canonical
    for p in _STRIP_PREFIXES:
        if low.startswith(p):
            s = s[len(p):].strip()
            break
    s = _TRAILING_ID_RE.sub('', s).strip()
    s = re.sub(r'\s+', ' ', s.replace('*', ' ')).strip()
    if not s:
        # The whole string was a prefix + id (e.g. "POS 12345"); fall back to
        # the pre-strip token so we never return an empty Supplier name.
        s = re.sub(r'\s+', ' ', (raw or '').replace('*', ' ')).strip()
    if s and not any(c.islower() for c in s):
        s = s.title()
    return s


def _supplier_log(direction: str, status: str, message: str = '',
                  subject_id=None) -> None:
    """Persist one PlaidSyncLog row for a supplier auto-create. Best-effort.
    `subject_id` cross-links this HTTP-level line to the AuditEvent for the same
    Supplier (see /admin/audit detail view)."""
    try:
        db.session.add(PlaidSyncLog(
            item_id='', direction=direction, count=1, status=status,
            error_message=(message or None),
            subject_id=(str(subject_id) if subject_id is not None else None)))
        db.session.commit()
    except Exception:  # pragma: no cover - logging must never crash a push
        db.session.rollback()
        log.warning('failed to write supplier PlaidSyncLog row', exc_info=True)


def _default_supplier_group() -> str:
    return (current_app.config.get('ERPNEXT_DEFAULT_SUPPLIER_GROUP')
            or 'All Supplier Groups').strip() or 'All Supplier Groups'


def _default_supplier_country() -> str:
    return (current_app.config.get('ERPNEXT_DEFAULT_SUPPLIER_COUNTRY')
            or 'United States').strip() or 'United States'


def _resolve_erpnext_supplier(client: ERPNextClient, normalized: str,
                              subject_id=None) -> str:
    """Find-or-create the ERPNext Supplier for a normalized merchant name and
    return its docname. Searches by supplier_name first (reuse an existing
    Supplier), else creates one; on a link failure (unknown supplier_group /
    country) retries once with those optional fields dropped."""
    existing = client.list_docs(
        SUPPLIER_DT, filters=[['supplier_name', '=', normalized]],
        fields=['name'], limit_page_length=1)
    if existing:
        _supplier_log('erpnext_supplier_auto_create', 'success',
                      f'matched existing Supplier {existing[0]["name"]}',
                      subject_id=subject_id)
        return existing[0]['name']
    doc = {
        'supplier_name': normalized,
        'supplier_type': 'Company',
        'supplier_group': _default_supplier_group(),
        'country': _default_supplier_country(),
    }
    try:
        created = client.create_doc(SUPPLIER_DT, doc)
    except ERPNextAPIError:
        # A stripped-down retry so a non-default supplier_group / country name
        # on this ERPNext doesn't block the Supplier create entirely.
        created = client.create_doc(
            SUPPLIER_DT, {'supplier_name': normalized, 'supplier_type': 'Company'})
    name = created.get('name') or normalized
    _supplier_log('erpnext_supplier_auto_create', 'success',
                  f'created Supplier {name}', subject_id=subject_id)
    return name


def get_or_create_supplier(client: ERPNextClient | None, merchant_name: str, *,
                           amount: float = 0.0, txn_date=None) -> str | None:
    """Find-or-create the Supplier for a merchant and return its ERPNext docname.

    Consults the local `Supplier` mirror first (keyed on the normalized name);
    on a miss it searches / creates the ERPNext Supplier and caches the result.
    Also rolls the transaction into the mirror's running tally (count / total /
    last-seen) for the /admin/suppliers dashboard. Returns None for a blank
    merchant name. A row is always cached locally even when `client` is None or
    the ERPNext resolve fails — `erpnext_supplier_name` just stays NULL until a
    later run resolves it."""
    normalized = normalize_merchant_name(merchant_name)
    if not normalized:
        return None
    row = Supplier.query.filter_by(normalized_name=normalized).first()
    is_new = row is None
    if is_new:
        row = Supplier(merchant_name=(merchant_name or '')[:255],
                       normalized_name=normalized[:255],
                       first_seen_at=_now(), transaction_count=0,
                       total_amount=0.0)
        db.session.add(row)
        db.session.flush()          # assign row.id for the audit / log cross-link
    row.transaction_count = (row.transaction_count or 0) + 1
    row.total_amount = round((row.total_amount or 0.0)
                             + abs(float(amount or 0.0)), 2)
    if txn_date is not None:
        try:
            row.last_transaction_at = datetime(
                txn_date.year, txn_date.month, txn_date.day, tzinfo=timezone.utc)
        except (AttributeError, TypeError, ValueError):
            pass
    row.updated_at = _now()
    if not row.erpnext_supplier_name and client is not None:
        try:
            row.erpnext_supplier_name = _resolve_erpnext_supplier(
                client, normalized, subject_id=row.id)
        except (ERPNextAPIError, ERPNextError) as e:
            _supplier_log('erpnext_supplier_auto_create', 'failed', str(e),
                          subject_id=row.id)
            log.warning('supplier resolve failed for %r: %s', normalized, e)
    db.session.commit()
    if is_new:
        # Permanent audit line for the first sighting of this merchant.
        audit.record('supplier_auto_created', subject_type='Supplier',
                     subject_id=row.id, after=row.to_dict(),
                     notes=f'merchant_name={merchant_name!r} → {normalized!r}')
    return row.erpnext_supplier_name


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
