# SPDX-License-Identifier: MIT
"""One-click account import: Plaid account → ERPNext Bank + Bank Account.

The transaction bridge (app/erpnext_bank.py) still assumes an operator has
hand-created the matching ERPNext Bank Account and mapped it. This module
removes that friction: given a linked Plaid account it will, idempotently,

  1. ensure the `plaid_account_id` + `last_4` custom fields exist on the
     ERPNext Bank Account doctype (first-push bootstrap),
  2. find-or-create the parent `Bank` (by bank_name),
  3. find-or-create the `Bank Account` (deduped on the `plaid_account_id`
     custom field),
  4. wire the result back onto the local PlaidAccount (erpnext_bank_account_name
     + sync_enabled) and stamp import_status.

Everything is find-or-create, so re-running never double-creates a Bank or a
Bank Account. The HTTP mechanics live in erpnext_client; this module owns the
doctype field mapping, the Plaid-subtype → ERPNext-type inference, the custom
field bootstrap, and the PlaidSyncLog audit line.

Supported vs unsupported: only account subtypes that map cleanly onto an
ERPNext Bank Account are offered a button. Loans, investments, brokerage,
retirement (401k/IRA/…), mortgages, HSAs etc. are NOT Bank Accounts in
ERPNext's model, so they're marked `unsupported` and skipped.
"""
from __future__ import annotations

import logging

from flask import current_app

from . import db
from . import erpnext_bank
from . import erpnext_settings
from .erpnext_client import (ERPNextAPIError, ERPNextClient, ERPNextConfigError,
                             ERPNextError)
from .models import PlaidAccount, PlaidItem, PlaidSyncLog

log = logging.getLogger('bankbridge.erpnext.accounts')

BANK_DT = 'Bank'
BANK_ACCOUNT_DT = 'Bank Account'
BANK_ACCOUNT_TYPE_DT = 'Bank Account Type'
CUSTOM_FIELD_DT = 'Custom Field'

# The Bank Account Type records the import flow references. Stock ERPNext ships
# without them, so an out-of-box instance would reject a Bank Account that links
# one — we provision them as part of the idempotent bootstrap.
DEFAULT_BANK_ACCOUNT_TYPES = ('Current', 'Credit')

# ── Plaid subtype → ERPNext support / typing ───────────────────────────────
#
# Subtypes that map cleanly onto an ERPNext Bank Account. Depository-style
# accounts become account_type "Current"; cards / credit lines become "Credit".
_CURRENT_SUBTYPES = {
    'checking', 'savings', 'cd', 'money market', 'cash management', 'paypal',
}
_CREDIT_SUBTYPES = {'credit card', 'line of credit'}
SUPPORTED_SUBTYPES = _CURRENT_SUBTYPES | _CREDIT_SUBTYPES

# Types / subtypes we explicitly never offer a button for — not Bank Accounts
# in ERPNext's model. (The subtype set is broader than strictly necessary given
# SUPPORTED_SUBTYPES already gates inclusion, but it documents intent and keeps
# the "not supported" note honest even if Plaid adds a new depository subtype.)
_UNSUPPORTED_TYPES = {'loan', 'investment', 'brokerage', 'other'}
_UNSUPPORTED_SUBTYPES = {
    'mortgage', 'student', 'auto', '401k', 'ira', 'roth', 'brokerage', 'hsa',
}

# The auto-provisioned custom fields on the Bank Account doctype. Idempotent:
# `plaid_account_id` is the dedup key (unique); `last_4` mirrors the mask.
_CUSTOM_FIELDS = (
    {'fieldname': 'plaid_account_id', 'label': 'Plaid Account ID',
     'fieldtype': 'Data', 'unique': 1, 'read_only': 1, 'no_copy': 1,
     'insert_after': 'bank_account_no'},
    {'fieldname': 'last_4', 'label': 'Last 4', 'fieldtype': 'Data',
     'read_only': 1, 'insert_after': 'plaid_account_id'},
)


def is_supported(account: PlaidAccount) -> bool:
    """True when this Plaid account maps onto an ERPNext Bank Account (and so
    gets a 'Create in ERPNext' button)."""
    t = (account.type or '').strip().lower()
    s = (account.subtype or '').strip().lower()
    if t in _UNSUPPORTED_TYPES or s in _UNSUPPORTED_SUBTYPES:
        return False
    return s in SUPPORTED_SUBTYPES


def erpnext_account_type(account: PlaidAccount) -> str:
    """The ERPNext Bank Account `account_type` for this Plaid account. A global
    ERPNEXT_DEFAULT_BANK_ACCOUNT_TYPE override wins; otherwise infer Credit for
    cards/credit lines, Current for everything else supported."""
    override = (current_app.config.get('ERPNEXT_DEFAULT_BANK_ACCOUNT_TYPE') or '').strip()
    if override:
        return override
    s = (account.subtype or '').strip().lower()
    if s in _CREDIT_SUBTYPES or (account.type or '').strip().lower() == 'credit':
        return 'Credit'
    return 'Current'


def erpnext_account_subtype(account: PlaidAccount) -> str:
    """Normalized `account_subtype` for the Bank Account (checking / savings /
    current / other) — a coarser bucket than Plaid's subtype."""
    s = (account.subtype or '').strip().lower()
    if s == 'checking':
        return 'checking'
    if s == 'savings':
        return 'savings'
    if s in _CURRENT_SUBTYPES:
        return 'current'
    return 'other'


# ── client / config ────────────────────────────────────────────────────────

def get_client(**kwargs) -> ERPNextClient:
    """An ERPNext client from the merged settings (reuses erpnext_bank's
    builder). Raises ERPNextConfigError if the connection isn't configured."""
    return erpnext_bank.get_client(**kwargs)


def _default_company() -> str:
    return (erpnext_settings.load().get('default_company') or '').strip()


def _log(item_id: str, count: int, status: str, message: str = '') -> None:
    """Persist one PlaidSyncLog row for an account-import batch. Best-effort —
    never masks the real outcome."""
    try:
        db.session.add(PlaidSyncLog(
            item_id=(item_id or '')[:120], direction='erpnext_account_import',
            count=count, status=status, error_message=(message or None)))
        db.session.commit()
    except Exception:  # pragma: no cover - defensive
        db.session.rollback()
        log.warning('failed to write account-import PlaidSyncLog row', exc_info=True)


# ── bootstrap (Bank Account Type records + custom fields) ───────────────────

def ensure_bank_account_types(client: ERPNextClient) -> None:
    """Ensure ERPNext has the 'Current' and 'Credit' Bank Account Type records
    the import flow assigns. Stock ERPNext ships without them, so an out-of-box
    instance would error when a Bank Account references one. Idempotent: a record
    is only created when the GET returns 404 (no existing doc)."""
    for name in DEFAULT_BANK_ACCOUNT_TYPES:
        if client.get_doc(BANK_ACCOUNT_TYPE_DT, name) is not None:
            continue
        # Bank Account Type autonames from its `account_type` field, so the
        # created record's docname is exactly `name`.
        client.create_doc(BANK_ACCOUNT_TYPE_DT, {'account_type': name})
        log.info("created Bank Account Type '%s'", name)


def ensure_custom_fields(client: ERPNextClient) -> None:
    """Idempotently provision the Bank Account custom fields we rely on
    (`plaid_account_id`, `last_4`). A no-op once they exist — same pattern the
    transaction bridge uses for its first push."""
    for spec in _CUSTOM_FIELDS:
        existing = client.list_docs(
            CUSTOM_FIELD_DT,
            filters=[['dt', '=', BANK_ACCOUNT_DT],
                     ['fieldname', '=', spec['fieldname']]],
            fields=['name'], limit_page_length=1)
        if existing:
            continue
        doc = {'dt': BANK_ACCOUNT_DT}
        doc.update(spec)
        client.create_doc(CUSTOM_FIELD_DT, doc)
        log.info('provisioned Bank Account custom field %s', spec['fieldname'])


def bootstrap(client: ERPNextClient) -> None:
    """Provision everything the import flow depends on, idempotently: the
    Current/Credit Bank Account Type records and the Bank Account custom fields.
    Bank Account Types come first so a Bank Account create can't fail on a
    missing link target."""
    ensure_bank_account_types(client)
    ensure_custom_fields(client)


# ── Bank find-or-create ─────────────────────────────────────────────────────

def _institution_extras(plaid_client, institution_id: str) -> dict:
    """Best-effort SWIFT / website for a Bank record, from Plaid institution
    metadata. Optional (section 3): returns {} on any miss or without a
    plaid_client. Plaid exposes an institution `url` (website) but no SWIFT, so
    swift_number is only set if a future metadata source provides it."""
    if plaid_client is None or not institution_id:
        return {}
    try:
        details = plaid_client.get_institution_details(institution_id)
    except Exception:  # pragma: no cover - enrichment must never fail an import
        log.warning('institution enrichment failed for %s', institution_id,
                    exc_info=True)
        return {}
    extras = {}
    if details.get('url'):
        extras['website'] = details['url']
    if details.get('swift'):
        extras['swift_number'] = details['swift']
    return extras


def find_or_create_bank(client: ERPNextClient, bank_name: str, *,
                        extras: dict | None = None) -> str:
    """Find-or-create an ERPNext Bank by bank_name; return its docname. Dedup
    matches the spec's filter: [["bank_name","=",<name>]]."""
    bank_name = (bank_name or '').strip() or 'Bank'
    matches = client.list_docs(
        BANK_DT, filters=[['bank_name', '=', bank_name]],
        fields=['name'], limit_page_length=1)
    if matches:
        return matches[0]['name']
    doc = {'bank_name': bank_name}
    if extras:
        doc.update({k: v for k, v in extras.items() if v})
    created = client.create_doc(BANK_DT, doc)
    return created.get('name') or bank_name


# ── Bank Account find-or-create ─────────────────────────────────────────────

def _find_bank_account(client: ERPNextClient, account: PlaidAccount) -> str | None:
    """Existing Bank Account docname for this Plaid account, deduped on the
    `plaid_account_id` custom field. None if not yet created."""
    matches = client.list_docs(
        BANK_ACCOUNT_DT,
        filters=[['plaid_account_id', '=', account.account_id]],
        fields=['name'], limit_page_length=1)
    return matches[0]['name'] if matches else None


def build_bank_account_doc(account: PlaidAccount, bank_name: str,
                           institution: str) -> dict:
    """Assemble the ERPNext Bank Account payload. account_name follows
    '<Institution> <TitleCasedSubtype> - <mask>' (e.g. 'Wells Fargo Checking -
    0000'). iban / bank_account_no are left blank (Plaid doesn't expose them)."""
    subtype_title = (account.subtype or account.type or 'Account').strip().title()
    mask = (account.mask or '0000').strip()
    account_name = f'{institution} {subtype_title} - {mask}'.strip()
    doc = {
        'account_name': account_name,
        'bank': bank_name,
        'account_type': erpnext_account_type(account),
        'account_subtype': erpnext_account_subtype(account),
        'is_company_account': 1,
        # Auto-provisioned custom fields (dedup key + mask mirror).
        'plaid_account_id': account.account_id,
        'last_4': account.mask or '',
    }
    company = _default_company()
    if company:
        doc['company'] = company
    return doc


def find_or_create_bank_account(client: ERPNextClient, account: PlaidAccount,
                                bank_name: str, institution: str) -> tuple[str, bool]:
    """Find-or-create the Bank Account for this Plaid account. Returns
    (docname, created)."""
    existing = _find_bank_account(client, account)
    if existing:
        return existing, False
    doc = build_bank_account_doc(account, bank_name, institution)
    created = client.create_doc(BANK_ACCOUNT_DT, doc)
    name = created.get('name')
    if not name:
        raise ERPNextAPIError('ERPNext returned no Bank Account name',
                              status_code=None)
    return name, True


# ── per-account import ──────────────────────────────────────────────────────

def import_plaid_account_to_erpnext(plaid_account_id: str, *,
                                    client: ERPNextClient | None = None,
                                    plaid_client=None,
                                    ensure_fields: bool = True) -> dict:
    """Create (or find) the ERPNext Bank + Bank Account for one Plaid account
    and wire the mapping back. Idempotent — a second call finds the existing
    records and re-links without creating duplicates.

    Returns a result dict: {'status': imported|skipped|unsupported,
    'bank_account': <docname or None>, 'bank': <bank docname or None>,
    'created_account': bool, 'created_bank': bool, 'message': str}.
    """
    account = PlaidAccount.query.filter_by(account_id=plaid_account_id).first()
    if account is None:
        raise ERPNextError(f'unknown Plaid account {plaid_account_id!r}')

    if account.erpnext_bank_account_name:
        return {'status': 'skipped', 'bank_account': account.erpnext_bank_account_name,
                'bank': None, 'created_account': False, 'created_bank': False,
                'message': 'already mapped'}

    if not is_supported(account):
        account.import_status = 'unsupported'
        db.session.commit()
        return {'status': 'unsupported', 'bank_account': None, 'bank': None,
                'created_account': False, 'created_bank': False,
                'message': f'{account.type}/{account.subtype} not supported'}

    client = client or get_client()
    if ensure_fields:
        # First step, idempotent: guarantee the Bank Account Type records +
        # custom fields exist so the create below can't fail on a missing link.
        bootstrap(client)

    item = PlaidItem.query.filter_by(item_id=account.item_id).first()
    institution = ((item.institution_name if item else '') or '').strip() or 'Bank'

    # Bank (find-or-create), enriched best-effort with website/SWIFT.
    banks_before = _bank_exists(client, institution)
    extras = _institution_extras(plaid_client,
                                 item.institution_id if item else '')
    bank_name = find_or_create_bank(client, institution, extras=extras)
    created_bank = not banks_before

    # Bank Account (find-or-create, deduped on plaid_account_id).
    docname, created_account = find_or_create_bank_account(
        client, account, bank_name, institution)

    account.erpnext_bank_account_name = docname
    account.sync_enabled = True
    account.import_status = 'imported'
    db.session.commit()

    verb = 'created' if created_account else 'linked existing'
    msg = f'{verb} Bank Account {docname} under Bank {bank_name}'
    _log(account.item_id, 1, 'success', msg)
    log.info('import: %s → %s', account.account_id, docname)
    return {'status': 'imported', 'bank_account': docname, 'bank': bank_name,
            'created_account': created_account, 'created_bank': created_bank,
            'message': msg}


def _bank_exists(client: ERPNextClient, bank_name: str) -> bool:
    """Whether a Bank with this name already exists (to report created_bank)."""
    if not (bank_name or '').strip():
        return False
    matches = client.list_docs(
        BANK_DT, filters=[['bank_name', '=', bank_name.strip()]],
        fields=['name'], limit_page_length=1)
    return bool(matches)


# ── bulk import ─────────────────────────────────────────────────────────────

def import_all_supported_accounts(*, client: ERPNextClient | None = None,
                                  plaid_client=None) -> dict:
    """Run the create flow for every unmapped supported account across all
    linked items. Already-mapped rows are silently skipped; unsupported rows are
    marked and counted. Returns aggregate stats plus a human summary."""
    client = client or get_client()
    bootstrap(client)  # Bank Account Types + custom fields, once for the batch

    stats = {'created': 0, 'unsupported': 0, 'skipped_mapped': 0,
             'failed': 0, 'considered': 0, 'errors': []}
    for account in PlaidAccount.query.order_by(PlaidAccount.name).all():
        if account.erpnext_bank_account_name:
            stats['skipped_mapped'] += 1
            continue
        stats['considered'] += 1
        if not is_supported(account):
            account.import_status = 'unsupported'
            db.session.commit()
            stats['unsupported'] += 1
            continue
        try:
            result = import_plaid_account_to_erpnext(
                account.account_id, client=client, plaid_client=plaid_client,
                ensure_fields=False)
            if result['status'] == 'imported':
                stats['created'] += 1
        except (ERPNextAPIError, ERPNextError) as e:
            db.session.rollback()
            stats['failed'] += 1
            stats['errors'].append(f'{account.name or account.mask}: {e}')
            log.warning('bulk import failed for %s: %s', account.account_id, e)

    total = stats['created'] + stats['unsupported'] + stats['failed']
    parts = [f"Created {stats['created']}/{total} accounts"]
    if stats['unsupported']:
        parts.append(f"skipped {stats['unsupported']} unsupported types")
    if stats['failed']:
        parts.append(f"{stats['failed']} failed")
    stats['summary'] = ', '.join(parts) + '.'
    _log('', stats['created'], 'failed' if stats['failed'] and not stats['created']
         else 'success', stats['summary'])
    return stats
