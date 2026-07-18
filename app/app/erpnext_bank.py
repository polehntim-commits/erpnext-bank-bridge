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
from .models import (Customer, PlaidAccount, PlaidItem, PlaidSyncLog,
                     Supplier)

log = logging.getLogger('bankbridge.erpnext')

DOCTYPE = 'Bank Transaction'
SUPPLIER_DT = 'Supplier'
CUSTOMER_DT = 'Customer'


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


def list_companies(client: ERPNextClient | None = None) -> list[str]:
    """Every ERPNext Company docname, for the multi-entity owning-Company
    dropdowns (v0.4.0 L1). The REST equivalent of `frappe.get_all("Company")` —
    a plain list of names, ordered so the picker is tidy. Raises the usual
    ERPNext errors if the connection isn't configured / reachable; callers that
    render a dropdown swallow those and fall back to just the default Company."""
    client = client or get_client()
    rows = client.list_docs('Company', fields=['name'],
                            order_by='name asc', limit_page_length=0)
    return [r['name'] for r in rows if r.get('name')]


# Sentinel for list_accounts(company=…): distinguishes "caller didn't specify a
# company" (→ back-compat default-Company scoping) from "caller explicitly wants
# ALL Companies" (company=None/'' → no company filter).
_COMPANY_UNSET = object()


def list_accounts(client: ERPNextClient | None = None, *,
                  company=_COMPANY_UNSET) -> list[dict]:
    """Non-group ERPNext GL Accounts (Chart of Accounts leaves) for the rule
    offset-account dropdown. Ordered by name so the picker is tidy.

    v0.3.1: filters are scoped to real, usable posting accounts only —
    `is_group=0` (leaves, not the parent groups the auto-CoA import creates) and
    `disabled=0` — but deliberately NOT by account_type/root_type, so every leaf
    (Bank, Cash, Expense, Income, …), including the auto-created Bank Accounts
    under the '1200' group, is offered. `limit_page_length=0` returns every match
    (no 20-row default cap that would hide accounts).

    v0.4.0.2 — Company scoping is now explicit, to fix cross-Company posting:
      * `company` omitted  → back-compat: scope to the configured default
        Company (unchanged callers keep their old behaviour);
      * `company='Acme'`   → scope to that Company's chart only;
      * `company=None`/`''` → NO company filter — every Company's leaves, so the
        caller can render them all with their Company suffix and force a
        conscious choice.
    The `company` field is returned on every row so callers can group / label by
    owning Company and validate a chosen account against a target Company."""
    client = client or get_client()
    filters = [['is_group', '=', 0], ['disabled', '=', 0]]
    if company is _COMPANY_UNSET:
        company = (erpnext_settings.load().get('default_company') or '').strip()
    else:
        company = (company or '').strip()
    if company:
        filters.append(['company', '=', company])
    return client.list_docs(
        'Account', filters=filters,
        fields=['name', 'account_name', 'company', 'account_type', 'root_type'],
        order_by='name asc', limit_page_length=0)


def list_account_names(client: ERPNextClient | None = None, *,
                       company=None) -> list[str]:
    """Deduplicated LOGICAL GL account names — the `account_name` field, stripped
    of any number/Company suffix — across every Company (or one Company when
    `company` is given). Feeds the Mode B (Company-agnostic rule) offset-account
    dropdown (v0.4.0.3): the operator picks a logical name ('Meals &
    Entertainment') that resolves to each transaction's own Company chart at JE
    time. Sorted, unique, case-insensitively deduped (first spelling wins).

    `company=None`/'' → all Companies (the agnostic default); a real name → that
    Company only. Best-effort via list_accounts; raises the usual ERPNext errors,
    which the UI caller swallows to leave the field free-text."""
    rows = list_accounts(client, company=(company or None))
    seen: dict[str, str] = {}
    for r in rows:
        name = (r.get('account_name') or '').strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return sorted(seen.values(), key=lambda s: s.lower())


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
                              subject_id=None,
                              supplier_group: str | None = None) -> str:
    """Find-or-create the ERPNext Supplier for a normalized merchant name and
    return its docname. Searches by supplier_name first (reuse an existing
    Supplier), else creates one; on a link failure (unknown supplier_group /
    country) retries once with those optional fields dropped.

    v0.4.0.7 — `supplier_group` overrides the configured default group for a
    derived party (a bank institution → 'Financial Institutions', a payroll
    processor → 'Payroll Providers'); None keeps the default."""
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
        'supplier_group': (supplier_group or '').strip() or _default_supplier_group(),
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


# ── v0.4.0.7: party derivation + auto-Supplier for NON-merchant txns ───
#
# Root cause this section fixes: pre-v0.4.0.7 the auto-Supplier only ever fired
# for a transaction Plaid gave a `merchant_name` (Uber, Starbucks). A rule that
# names a Party for a DESCRIPTION-only transaction ("INTRST PYMNT", "ACH
# Electronic CreditGUSTO PAY", "CREDIT CARD 3333 PAYMENT") therefore put a Party
# on the JE that had no matching ERPNext Supplier, and the Journal Entry create
# came back 417 LinkValidationError ("Could not find Row #1: Party: Wells
# Fargo"). Every party NAME — whoever derived it — now ensures its Supplier
# exists before the JE is built (see categorization.resolve_party).

# Payroll processors, matched case-insensitively as a substring of the raw
# description. Value is the canonical Supplier name. Ordered most-specific
# first so 'intuit payroll' wins over a bare 'intuit'.
_PAYROLL_PROCESSORS = (
    ('intuit payroll', 'Intuit Payroll'),
    ('quickbooks payroll', 'QuickBooks Payroll'),
    ('square payroll', 'Square Payroll'),
    ('wave payroll', 'Wave Payroll'),
    ('gusto', 'Gusto'),
    ('adp', 'ADP'),
    ('paychex', 'Paychex'),
    ('paylocity', 'Paylocity'),
    ('paycom', 'Paycom'),
    ('paycor', 'Paycor'),
    ('trinet', 'TriNet'),
    ('justworks', 'Justworks'),
    ('rippling', 'Rippling'),
    ('zenefits', 'Zenefits'),
    ('bamboohr', 'BambooHR'),
    ('insperity', 'Insperity'),
)

# Supplier Groups the auto-create files a derived party under, by source. A
# merchant keeps the configured default group (unchanged pre-v0.4.0.7
# behaviour) — only the two derived sources get an opinionated bucket.
SUPPLIER_GROUP_BY_SOURCE = {
    'institution': 'Financial Institutions',
    'payroll': 'Payroll Providers',
}

SUPPLIER_GROUP_DT = 'Supplier Group'


def institution_name_for_account_id(account_id: str | None) -> str:
    """The linked Plaid Item's institution name for a transaction's account
    ('Wells Fargo'). '' when the account is unknown or its Item carries no
    institution name — the caller then has no party to derive."""
    if not account_id:
        return ''
    acct = PlaidAccount.query.filter_by(account_id=account_id).first()
    if acct is None:
        return ''
    item = PlaidItem.query.filter_by(item_id=acct.item_id).first()
    return ((item.institution_name or '').strip() if item else '')


def payroll_processor_in(text: str) -> str:
    """The canonical payroll-processor name mentioned in `text`, or ''. Matches
    a bare substring because Plaid concatenates the processor into a run-on
    description ('ACH Electronic CreditGUSTO PAY 123456' → 'Gusto')."""
    low = (text or '').lower()
    for key, canonical in _PAYROLL_PROCESSORS:
        if key in low:
            return canonical
    return ''


def derive_party_from_transaction(row) -> tuple[str, str]:
    """Derive a party NAME (and the source that produced it) for a transaction
    whose rule wants a Party. Returns `(name, source)`; `('', '')` when nothing
    can be derived.

    Order — most specific first:
      1. `merchant` — Plaid gave a merchant_name (normalized as before).
      2. `payroll`  — a known payroll processor appears in the raw description
         ('ACH Electronic CreditGUSTO PAY 123456' → 'Gusto').
      3. `institution` — fall back to the account's own institution, which is
         the right counterparty for the bank's own postings ('INTRST PYMNT',
         'CREDIT CARD 3333 PAYMENT' → 'Wells Fargo').

    `source` selects the Supplier Group the auto-create files the party under
    (see SUPPLIER_GROUP_BY_SOURCE)."""
    merchant = normalize_merchant_name(getattr(row, 'merchant_name', '') or '')
    if merchant:
        return merchant, 'merchant'
    payroll = payroll_processor_in(getattr(row, 'name', '') or '')
    if payroll:
        return payroll, 'payroll'
    institution = institution_name_for_account_id(
        getattr(row, 'account_id', None))
    if institution:
        return institution, 'institution'
    return '', ''


def ensure_supplier_group(client: ERPNextClient, group: str) -> str:
    """Find-or-create the ERPNext Supplier Group `group` and return its docname,
    or '' when it can't be provisioned. Best-effort by design: a Supplier Group
    is a nicety, and failing to make one must never cost us the Supplier (the
    caller then falls back to the configured default group)."""
    group = (group or '').strip()
    if not group:
        return ''
    try:
        if client.get_doc(SUPPLIER_GROUP_DT, group):
            return group
        created = client.create_doc(SUPPLIER_GROUP_DT, {
            'supplier_group_name': group,
            'parent_supplier_group': 'All Supplier Groups',
            'is_group': 0,
        })
        return (created or {}).get('name') or group
    except (ERPNextAPIError, ERPNextError):
        log.info('could not ensure Supplier Group %r; using the default', group)
        return ''


def ensure_supplier(client: ERPNextClient | None, party_name: str, *,
                    source: str = '') -> str | None:
    """Find-or-create the ERPNext Supplier for an ALREADY-RESOLVED party name and
    return its docname (v0.4.0.7).

    Unlike get_or_create_supplier this takes the name verbatim — no merchant
    normalization — because the name has already been settled by whoever derived
    it (a rule's Party name, a payroll processor, an institution). Normalizing
    again would turn an operator's literal "GUSTO" into "Gusto" and mint a
    duplicate alongside the Supplier they already have.

    Idempotent on three levels: the local `Supplier` mirror short-circuits a
    repeat call, `_resolve_erpnext_supplier` reuses an existing ERPNext Supplier
    by supplier_name before creating, and the Supplier Group provisioning is
    itself find-or-create. Returns None for a blank name or when the ERPNext
    resolve fails (the caller then must not put a Party on the JE)."""
    name = (party_name or '').strip()
    if not name or client is None:
        return None
    row = Supplier.query.filter_by(normalized_name=name[:255]).first()
    if row is not None and row.erpnext_supplier_name:
        return row.erpnext_supplier_name
    group = ''
    wanted = SUPPLIER_GROUP_BY_SOURCE.get(source, '')
    if wanted:
        group = ensure_supplier_group(client, wanted)
    is_new = row is None
    if is_new:
        row = Supplier(merchant_name=name[:255], normalized_name=name[:255],
                       first_seen_at=_now(), transaction_count=0,
                       total_amount=0.0)
        db.session.add(row)
        db.session.flush()      # assign row.id for the audit / log cross-link
    try:
        row.erpnext_supplier_name = _resolve_erpnext_supplier(
            client, name, subject_id=row.id, supplier_group=group or None)
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        _supplier_log('erpnext_supplier_auto_create', 'failed', str(e))
        log.warning('party supplier resolve failed for %r: %s', name, e)
        return None
    row.updated_at = _now()
    db.session.commit()
    if is_new:
        audit.record('supplier_auto_created', subject_type='Supplier',
                     subject_id=row.id, after=row.to_dict(),
                     notes=f'party={name!r} (source={source or "rule"}) → '
                           f'{row.erpnext_supplier_name!r}')
    return row.erpnext_supplier_name


# ── v0.4.0.8: the sell side — Customers, and dual-role parties ─────────

CUSTOMER_GROUP_DT = 'Customer Group'
CUSTOMER_GROUP_BY_SOURCE = {
    'institution': 'Financial Institutions',
}


def _default_customer_group() -> str:
    return (current_app.config.get('ERPNEXT_DEFAULT_CUSTOMER_GROUP')
            or 'All Customer Groups').strip() or 'All Customer Groups'


def _default_territory() -> str:
    return (current_app.config.get('ERPNEXT_DEFAULT_TERRITORY')
            or 'All Territories').strip() or 'All Territories'


def ensure_customer_group(client: ERPNextClient, group: str) -> str:
    """Find-or-create the ERPNext Customer Group `group` and return its docname,
    or '' when it can't be provisioned. Best-effort for the same reason
    ensure_supplier_group is: the group is a filing nicety and must never cost
    us the Customer itself (the caller falls back to the configured default)."""
    group = (group or '').strip()
    if not group:
        return ''
    try:
        if client.get_doc(CUSTOMER_GROUP_DT, group):
            return group
        created = client.create_doc(CUSTOMER_GROUP_DT, {
            'customer_group_name': group,
            'parent_customer_group': 'All Customer Groups',
            'is_group': 0,
        })
        return (created or {}).get('name') or group
    except (ERPNextAPIError, ERPNextError):
        log.info('could not ensure Customer Group %r; using the default', group)
        return ''


def _resolve_erpnext_customer(client: ERPNextClient, normalized: str,
                              subject_id=None,
                              customer_group: str | None = None) -> str:
    """Find-or-create the ERPNext Customer for a party name and return its
    docname. The AR-side mirror of _resolve_erpnext_supplier, including the
    stripped-down retry when this ERPNext rejects the optional
    customer_group / territory links."""
    existing = client.list_docs(
        CUSTOMER_DT, filters=[['customer_name', '=', normalized]],
        fields=['name'], limit_page_length=1)
    if existing:
        _supplier_log('erpnext_customer_auto_create', 'success',
                      f'matched existing Customer {existing[0]["name"]}',
                      subject_id=subject_id)
        return existing[0]['name']
    doc = {
        'customer_name': normalized,
        'customer_type': 'Company',
        'customer_group': (customer_group or '').strip() or _default_customer_group(),
        'territory': _default_territory(),
    }
    try:
        created = client.create_doc(CUSTOMER_DT, doc)
    except ERPNextAPIError:
        created = client.create_doc(
            CUSTOMER_DT, {'customer_name': normalized, 'customer_type': 'Company'})
    name = created.get('name') or normalized
    _supplier_log('erpnext_customer_auto_create', 'success',
                  f'created Customer {name}', subject_id=subject_id)
    return name


def ensure_customer(client: ERPNextClient | None, party_name: str, *,
                    source: str = '') -> str | None:
    """Find-or-create the ERPNext Customer for an ALREADY-RESOLVED party name and
    return its docname (v0.4.0.8) — the exact AR-side twin of ensure_supplier.

    THE GAP THIS FILLS: everything through v0.4.0.7 was Accounts Payable. A farm
    also takes money IN — fruit buyers, USDA/FSA payments, grants, lease revenue,
    direct-to-consumer deposits — and booking those against an auto-created
    SUPPLIER is wrong twice over: it points the JE at the AP ledger instead of
    AR, and it seeds the 1099-NEC vendor list with people who are actually
    customers.

    Takes the name verbatim (no merchant re-normalization) and is idempotent on
    the same three levels as ensure_supplier: the local `Customer` mirror, the
    ERPNext search-by-customer_name, and find-or-create on the group. Returns
    None for a blank name or a failed resolve — the caller must then put NO party
    on the JE rather than one ERPNext will reject with a LinkValidationError."""
    name = (party_name or '').strip()
    if not name or client is None:
        return None
    row = Customer.query.filter_by(normalized_name=name[:255]).first()
    if row is not None and row.erpnext_customer_name:
        return row.erpnext_customer_name
    group = ''
    wanted = CUSTOMER_GROUP_BY_SOURCE.get(source, '')
    if wanted:
        group = ensure_customer_group(client, wanted)
    is_new = row is None
    if is_new:
        row = Customer(merchant_name=name[:255], normalized_name=name[:255],
                       first_seen_at=_now(), transaction_count=0,
                       total_amount=0.0)
        db.session.add(row)
        db.session.flush()      # assign row.id for the audit / log cross-link
    try:
        row.erpnext_customer_name = _resolve_erpnext_customer(
            client, name, subject_id=row.id, customer_group=group or None)
    except (ERPNextAPIError, ERPNextError) as e:
        db.session.rollback()
        _supplier_log('erpnext_customer_auto_create', 'failed', str(e))
        log.warning('party customer resolve failed for %r: %s', name, e)
        return None
    row.updated_at = _now()
    db.session.commit()
    if is_new:
        audit.record('customer_auto_created', subject_type='Customer',
                     subject_id=row.id, after=row.to_dict(),
                     notes=f'party={name!r} (source={source or "rule"}) → '
                           f'{row.erpnext_customer_name!r}')
    return row.erpnext_customer_name


# A party name containing one of these (as a whole word) is assumed to trade
# with you in BOTH directions — see is_dual_role_party. Deliberately short and
# conservative: a false positive costs one unused ERPNext party record, but the
# operator can still tune both ends via config (see is_dual_role_party).
DUAL_ROLE_KEYWORDS = (
    'bank', 'banks', 'banking', 'bancorp', 'bancshares', 'credit union',
    'trust', 'financial', 'federal', 'savings',
)

# Institutions with no give-away keyword in the name. Matched on the whole
# normalized name or its first word, never as a bare substring, so "Schwab
# Landscaping" doesn't get mistaken for the brokerage.
DUAL_ROLE_NAMES = frozenset({
    'wells fargo', 'chase', 'jpmorgan', 'jpmorgan chase', 'citi', 'citibank',
    'citigroup', 'amex', 'american express', 'discover', 'capital one',
    'usaa', 'ally', 'pnc', 'truist', 'synchrony', 'barclays',
    'fidelity', 'schwab', 'charles schwab', 'vanguard', 'robinhood',
    'etrade', 'e*trade', 'merrill', 'merrill lynch', 'edward jones',
    'coinbase', 'kraken', 'gemini', 'binance',
})

# The multi-word entries above, which also match as a phrase inside a longer
# name ('Wells Fargo Clearing Services'). Single-word entries deliberately do
# NOT match that way — 'gemini' or 'discover' as a bare substring would sweep up
# far too much.
_DUAL_ROLE_PHRASES = tuple(n for n in DUAL_ROLE_NAMES if ' ' in n)

_WORDS = re.compile(r"[a-z0-9*']+")


def is_dual_role_party(party_name: str, *, source: str = '') -> bool:
    """True when this party plausibly trades with you in BOTH directions, so
    both an ERPNext Customer AND a Supplier should exist for it (v0.4.0.8).

    The motivating case is a bank: Wells Fargo pays you interest (it is a
    Customer on that JE) and charges you account fees (it is a Supplier on that
    one). Whichever transaction lands first would otherwise mint only one side,
    and the second transaction's JE would 417 on the missing party — or, worse,
    quietly book AR activity against an AP party.

    Three independent signals, any of which is sufficient:
      1. `source='institution'` — the name was derived from a linked Plaid
         Item's institution, so it IS a bank by construction.
      2. a whole-word DUAL_ROLE_KEYWORDS hit ('… Bank', 'Credit Union',
         '… Federal Savings').
      3. a DUAL_ROLE_NAMES hit on the whole name or its first word — the
         brokerages and exchanges whose names carry no such keyword.

    Config escapes both ways, for a chart that disagrees with the heuristic:
    `BANKBRIDGE_DUAL_ROLE_PARTIES` force-adds names, and
    `BANKBRIDGE_SINGLE_ROLE_PARTIES` force-excludes them (it wins, so a
    "Federal Express" or a "Trust Fruit Co" can be pinned to one role)."""
    name = (party_name or '').strip()
    if not name:
        return False
    low = name.lower()
    forced_single = {s.strip().lower()
                     for s in (current_app.config.get(
                         'BANKBRIDGE_SINGLE_ROLE_PARTIES') or ()) if s.strip()}
    if low in forced_single:
        return False
    forced_dual = {s.strip().lower()
                   for s in (current_app.config.get(
                       'BANKBRIDGE_DUAL_ROLE_PARTIES') or ()) if s.strip()}
    if low in forced_dual:
        return True
    if source == 'institution':
        return True
    if low in DUAL_ROLE_NAMES:
        return True
    if any(phrase in low for phrase in _DUAL_ROLE_PHRASES):
        return True
    words = _WORDS.findall(low)
    if words and words[0] in DUAL_ROLE_NAMES:
        return True
    wordset = set(words)
    for kw in DUAL_ROLE_KEYWORDS:
        if ' ' in kw:
            if kw in low:
                return True
        elif kw in wordset:
            return True
    return False


def ensure_party(client: ERPNextClient | None, party_name: str,
                 party_type: str, *, source: str = '') -> str | None:
    """Find-or-create the ERPNext party of `party_type` for `party_name` and
    return its docname — the single entry point the JE path uses (v0.4.0.8).

    Returns the docname for the REQUESTED side. When the name looks dual-role
    (is_dual_role_party) the OTHER side is provisioned too, best-effort: a bank
    that pays interest today will charge a fee next month, and having both
    records already in place is what keeps that second JE from failing. The
    counterpart create can never fail this call — its exceptions are swallowed
    and logged, because the party we were actually asked for is what the JE
    needs to post."""
    name = (party_name or '').strip()
    ptype = (party_type or '').strip()
    if not name or ptype not in (SUPPLIER_DT, CUSTOMER_DT):
        return None
    primary = (ensure_customer(client, name, source=source)
               if ptype == CUSTOMER_DT
               else ensure_supplier(client, name, source=source))
    if primary and is_dual_role_party(name, source=source):
        try:
            if ptype == CUSTOMER_DT:
                ensure_supplier(client, name, source=source)
            else:
                ensure_customer(client, name, source=source)
        except Exception:   # pragma: no cover - never cost us the primary party
            log.warning('dual-role counterpart for %r (%s) failed', name, ptype,
                        exc_info=True)
    return primary


def root_type_for_account(client: ERPNextClient | None, account: str,
                          company: str = '') -> str:
    """The ERPNext root_type ('Income' | 'Expense' | 'Asset' | 'Liability' |
    'Equity') of a GL account, or '' when it can't be determined (v0.4.0.8).

    Feeds the `party_type='Auto'` derivation. `account` may be a fully-qualified
    docname ('Fruit Sales - BBT', a Mode A rule) or a bare LOGICAL name ('Fruit
    Sales', a Mode B Company-agnostic rule) — both resolve, because the lookup
    falls back from an exact docname match to an account_name match scoped to
    `company`. Returns '' rather than raising on any ERPNext trouble: an
    undeterminable root_type means "don't guess a party", not "fail the JE"."""
    acct = (account or '').strip()
    if not acct or client is None:
        return ''
    try:
        doc = client.get_doc('Account', acct)
        if isinstance(doc, dict) and doc.get('root_type'):
            return (doc.get('root_type') or '').strip()
    except (ERPNextAPIError, ERPNextError):
        pass                      # fall through to the account_name lookup
    try:
        filters = [['account_name', '=', acct], ['is_group', '=', 0]]
        want = (company or '').strip()
        if want:
            filters.append(['company', '=', want])
        rows = client.list_docs('Account', filters=filters,
                                fields=['name', 'root_type'],
                                limit_page_length=1)
    except (ERPNextAPIError, ERPNextError):
        return ''
    if rows:
        return (rows[0].get('root_type') or '').strip()
    return ''


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
