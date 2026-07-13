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
ACCOUNT_SUBTYPE_DT = 'Account Subtype'
CUSTOM_FIELD_DT = 'Custom Field'

# ── unavailable-doctype registry ────────────────────────────────────────────
#
# Some ERPNext / Frappe instances don't ship (or have a broken) `Account
# Subtype` — Tim's returns HTTP 500 "No module named
# 'frappe.core.doctype.account_subtype'" for the existence probe. Bootstrap must
# not crash on that; instead it records the doctype as *unavailable* and the
# send-side drops the fields that link to it. The registry is a per-app set so
# it's process-local and resets on restart (bootstrap then re-discovers), and so
# tests — each of which builds a fresh app — start clean. When there's no app
# context (defensive), we fall back to a module-level set.
_UNAVAILABLE_KEY = 'bankbridge_unavailable_doctypes'
_fallback_unavailable: set[str] = set()


def _unavailable_registry() -> set:
    """The per-app set of ERPNext doctypes discovered to be unavailable."""
    try:
        store = current_app.extensions
    except RuntimeError:  # pragma: no cover - no app context (defensive)
        return _fallback_unavailable
    return store.setdefault(_UNAVAILABLE_KEY, set())


def _mark_doctype_unavailable(doctype: str) -> None:
    _unavailable_registry().add(doctype)


def _mark_doctype_available(doctype: str) -> None:
    """Clear a prior 'unavailable' mark — bootstrap re-probed and it's back."""
    _unavailable_registry().discard(doctype)


def is_doctype_unavailable(doctype: str) -> bool:
    return doctype in _unavailable_registry()


def unavailable_doctypes() -> set:
    """A copy of the set of doctypes bootstrap found unavailable in this
    ERPNext (for the admin partial-bootstrap banner)."""
    return set(_unavailable_registry())


def _is_missing_doctype_error(e: ERPNextAPIError) -> bool:
    """True when a Frappe error means the *doctype itself* isn't installed in
    this ERPNext (its Python module is missing) rather than a normal
    missing-document / permission error. Tim's instance returns HTTP 500 with
    'No module named …' / ImportError for the Account Subtype probe."""
    blob = ((e.response_body or '') + ' ' + str(e)).lower()
    return e.status_code == 500 and ('no module named' in blob
                                     or 'importerror' in blob)

# The Bank Account Type records the import flow references. Stock ERPNext ships
# without them, so an out-of-box instance would reject a Bank Account that links
# one — we provision them as part of the idempotent bootstrap.
DEFAULT_BANK_ACCOUNT_TYPES = ('Current', 'Credit')

# The Account Subtype records `Bank Account.account_subtype` links to. On Tim's
# instance that field is a Link (not a Select), so a Bank Account create fails
# with a LinkValidationError ("Could not find Account Subtype: savings") unless
# the target record exists. Same remedy as Bank Account Type: provision them in
# the idempotent bootstrap. Docnames are Title Case (Frappe convention), and the
# send-side (erpnext_account_subtype) matches — see build_bank_account_doc.
DEFAULT_ACCOUNT_SUBTYPES = (
    'Checking', 'Savings', 'Current', 'Other',
    'Credit Card', 'Cd', 'Money Market',
    'Cash Management', 'Paypal', 'Line Of Credit',
)

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
    """Normalized `account_subtype` for the Bank Account (Checking / Savings /
    Current / Other) — a coarser bucket than Plaid's subtype. Title Case, to
    match the Account Subtype link-target docnames provisioned in bootstrap."""
    s = (account.subtype or '').strip().lower()
    if s == 'checking':
        return 'Checking'
    if s == 'savings':
        return 'Savings'
    if s in _CURRENT_SUBTYPES:
        return 'Current'
    return 'Other'


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

def ensure_bank_account_types(client: ERPNextClient) -> bool:
    """Ensure ERPNext has the 'Current' and 'Credit' Bank Account Type records
    the import flow assigns. Stock ERPNext ships without them, so an out-of-box
    instance would error when a Bank Account references one. Idempotent: a record
    is only created when the GET returns 404 (no existing doc).

    Returns True if the doctype is available (provisioned or already present),
    False if this ERPNext doesn't have the Bank Account Type doctype at all — in
    which case we log-warn, mark it unavailable, and skip provisioning rather
    than crashing bootstrap."""
    for name in DEFAULT_BANK_ACCOUNT_TYPES:
        try:
            existing = client.get_doc(BANK_ACCOUNT_TYPE_DT, name)
        except ERPNextAPIError as e:
            if _is_missing_doctype_error(e):
                log.warning('Bank Account Type doctype unavailable in this '
                            'ERPNext; skipping provisioning (%s)', str(e)[:200])
                _mark_doctype_unavailable(BANK_ACCOUNT_TYPE_DT)
                return False
            raise
        if existing is not None:
            continue
        # Bank Account Type autonames from its `account_type` field, so the
        # created record's docname is exactly `name`.
        client.create_doc(BANK_ACCOUNT_TYPE_DT, {'account_type': name})
        log.info("created Bank Account Type '%s'", name)
    _mark_doctype_available(BANK_ACCOUNT_TYPE_DT)
    return True


def ensure_account_subtypes(client: ERPNextClient) -> bool:
    """Ensure ERPNext has the Account Subtype records the import flow links from
    `Bank Account.account_subtype` (Checking, Savings, Current, …). Where that
    field is a Link, an out-of-box instance has no matching target and rejects
    the Bank Account create with a LinkValidationError. Idempotent: a record is
    only created when the GET returns 404. Docnames are Title Case, matching the
    values erpnext_account_subtype sends.

    Returns True if available, False if this ERPNext lacks the Account Subtype
    doctype entirely (Tim's instance returns HTTP 500 'No module named …'). In
    that case we log-warn, mark it unavailable, and skip — the send-side then
    drops `account_subtype` so the import still succeeds."""
    for name in DEFAULT_ACCOUNT_SUBTYPES:
        try:
            existing = client.get_doc(ACCOUNT_SUBTYPE_DT, name)
        except ERPNextAPIError as e:
            if _is_missing_doctype_error(e):
                log.warning('Account Subtype doctype unavailable in this '
                            'ERPNext; skipping provisioning (%s)', str(e)[:200])
                _mark_doctype_unavailable(ACCOUNT_SUBTYPE_DT)
                return False
            raise
        if existing is not None:
            continue
        # Mirror Bank Account Type: the master autonames from its titling field,
        # so the created record's docname is exactly `name`.
        client.create_doc(ACCOUNT_SUBTYPE_DT, {'account_subtype': name})
        log.info("created Account Subtype '%s'", name)
    _mark_doctype_available(ACCOUNT_SUBTYPE_DT)
    return True


def ensure_custom_fields(client: ERPNextClient) -> bool:
    """Idempotently provision the Bank Account custom fields we rely on
    (`plaid_account_id`, `last_4`). A no-op once they exist — same pattern the
    transaction bridge uses for its first push.

    Returns True if available, False if this ERPNext lacks the Custom Field
    doctype (very unlikely — it's Frappe core — but handled for parity). When
    unavailable the send-side drops the custom fields, degrading to a
    no-dedup-key import rather than crashing bootstrap."""
    for spec in _CUSTOM_FIELDS:
        try:
            existing = client.list_docs(
                CUSTOM_FIELD_DT,
                filters=[['dt', '=', BANK_ACCOUNT_DT],
                         ['fieldname', '=', spec['fieldname']]],
                fields=['name'], limit_page_length=1)
        except ERPNextAPIError as e:
            if _is_missing_doctype_error(e):
                log.warning('Custom Field doctype unavailable in this ERPNext; '
                            'skipping provisioning (%s)', str(e)[:200])
                _mark_doctype_unavailable(CUSTOM_FIELD_DT)
                return False
            raise
        if existing:
            continue
        doc = {'dt': BANK_ACCOUNT_DT}
        doc.update(spec)
        client.create_doc(CUSTOM_FIELD_DT, doc)
        log.info('provisioned Bank Account custom field %s', spec['fieldname'])
    _mark_doctype_available(CUSTOM_FIELD_DT)
    return True


def bootstrap(client: ERPNextClient) -> dict:
    """Provision everything the import flow depends on, idempotently: the
    Current/Credit Bank Account Type records, the Account Subtype records, and
    the Bank Account custom fields. The link-target masters come first so a Bank
    Account create can't fail on a missing link target.

    Resilient to instances that lack one of these linked doctypes: a missing
    doctype is logged, recorded in the unavailable registry, and skipped — never
    raised — so one broken doctype can't sink the whole bootstrap (or poison the
    request that triggered it). Returns a per-doctype availability dict, e.g.
    {'Bank Account Type': True, 'Account Subtype': False, 'Custom Field': True,
    'partial': True}."""
    status = {
        BANK_ACCOUNT_TYPE_DT: ensure_bank_account_types(client),
        ACCOUNT_SUBTYPE_DT: ensure_account_subtypes(client),
        CUSTOM_FIELD_DT: ensure_custom_fields(client),
    }
    status['partial'] = not all(v for k, v in status.items() if k != 'partial')
    return status


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


# The Bank Account fields we will never drop on a retry — without them the doc
# is meaningless. account_name + bank are always essential. account_type is
# essential ONLY when bootstrap has proved the Bank Account Type doctype exists;
# if that doctype is unavailable the field is dropped up-front (see
# _prune_unavailable_fields) and must not be treated as un-droppable, or every
# import on such an instance would fail. Everything else (account_subtype,
# is_company_account, company, the custom fields) is "preferred": send it if
# ERPNext accepts it, drop it if ERPNext rejects it as unknown.
_BASE_ESSENTIAL_BANK_ACCOUNT_FIELDS = {'account_name', 'bank'}

# Which Bank Account payload fields link to which ERPNext doctype. If bootstrap
# proved a doctype unavailable in this instance, the dependent field is dropped
# from the POST — sending it would fail the whole import (missing link target or
# unknown custom field), and we'd rather import a slightly leaner record.
_FIELD_DOCTYPE_DEPS = {
    'account_subtype': ACCOUNT_SUBTYPE_DT,
    'account_type': BANK_ACCOUNT_TYPE_DT,
    'plaid_account_id': CUSTOM_FIELD_DT,
    'last_4': CUSTOM_FIELD_DT,
}


def _essential_bank_account_fields() -> set:
    """Essential (never-dropped-on-retry) Bank Account fields, computed against
    the unavailable registry. account_type is essential only while its Bank
    Account Type doctype is available."""
    fields = set(_BASE_ESSENTIAL_BANK_ACCOUNT_FIELDS)
    if not is_doctype_unavailable(BANK_ACCOUNT_TYPE_DT):
        fields.add('account_type')
    return fields


def _prune_unavailable_fields(doc: dict) -> dict:
    """Drop payload fields whose linked doctype bootstrap found unavailable, so
    a broken/missing ERPNext doctype degrades the import instead of failing it.
    Mutates and returns `doc`."""
    for field, doctype in _FIELD_DOCTYPE_DEPS.items():
        if field in doc and is_doctype_unavailable(doctype):
            doc.pop(field)
            log.info('dropping Bank Account field %r (%s unavailable in this '
                     'ERPNext)', field, doctype)
    return doc

# Substrings that mark a Frappe "you sent a field I don't have" rejection. When
# one of these appears in a 417/422 body we retry once with the named field(s)
# stripped, rather than failing the whole import.
_UNKNOWN_FIELD_HINTS = (
    'not a valid field', 'unknown field', 'does not have field',
    'has no field', 'no field named',
)

# Substrings that mark a Frappe Link-target rejection — the field IS valid, but
# the *value* we sent has no matching link record (e.g. "Could not find Account
# Subtype: savings"). Same remedy as an unknown field: drop the offending
# preferred field and retry once, so a missing master doesn't sink the import.
_LINK_VALIDATION_HINTS = ('could not find', 'linkvalidationerror')

# A missing *required* field. We can't know which value to supply, so this is
# logged and surfaced — never auto-retried (dropping a field can't fix it).
_MANDATORY_HINTS = ('mandatory',)


def _unknown_fields_in(body: str, doc: dict) -> list[str]:
    """The preferred (droppable) doc fields named in an ERPNext rejection body.
    Matches both the snake_case fieldname ('account_subtype', from 'not a valid
    field' errors) and its spaced label ('account subtype', from LinkValidation
    'Could not find Account Subtype: …' errors). Essential fields are never
    returned — if ERPNext rejects one of those the import genuinely can't
    succeed and should surface."""
    low = (body or '').lower()
    essential = _essential_bank_account_fields()
    out = []
    for k in doc:
        if k in essential:
            continue
        if k.lower() in low or k.lower().replace('_', ' ') in low:
            out.append(k)
    return out


def _create_bank_account_defensive(client: ERPNextClient, doc: dict, *,
                                   item_id: str = '') -> tuple[dict, bool]:
    """POST a Bank Account, and if ERPNext rejects a preferred field — either as
    unknown ('not a valid field') or as a broken Link ('Could not find … ' /
    LinkValidationError) — retry ONCE with that field (or fields) stripped. The
    retry decision is written to the sync log so the fallback is traceable.

    Returns (created_doc, retried). A mandatory-field error is logged and
    re-raised (no field to drop fixes it); any other non-field error, or a
    field-error naming only essential fields, is re-raised unchanged (its
    response body rides along on the exception)."""
    try:
        return client.create_doc(BANK_ACCOUNT_DT, doc), False
    except ERPNextAPIError as e:
        body = e.response_body or ''
        low = body.lower()
        if e.status_code not in (417, 422):
            raise
        # A missing required field can't be repaired by dropping something —
        # log it plainly and surface, per spec (don't auto-retry).
        if any(h in low for h in _MANDATORY_HINTS):
            log.warning('Bank Account create failed on a mandatory field; not '
                        'retrying: %s', str(e)[:300])
            raise
        is_link = any(h in low for h in _LINK_VALIDATION_HINTS)
        if not (is_link or any(h in low for h in _UNKNOWN_FIELD_HINTS)):
            raise
        drop = _unknown_fields_in(body, doc)
        if not drop:
            raise
        retry_doc = {k: v for k, v in doc.items() if k not in drop}
        reason = 'LinkValidationError' if is_link else 'unknown field'
        msg = (f"Retry: dropped {', '.join(drop)} due to {reason}. "
               f'Original error: {str(e)[:400]}')
        log.warning('Bank Account create rejected %s (%s); retrying without it',
                    drop, reason)
        _log(item_id, 0, 'retry', msg)
        return client.create_doc(BANK_ACCOUNT_DT, retry_doc), True


# ── Bank Account find-or-create ─────────────────────────────────────────────

def _find_bank_account(client: ERPNextClient, account: PlaidAccount) -> str | None:
    """Existing Bank Account docname for this Plaid account, deduped on the
    `plaid_account_id` custom field. None if not yet created. When the custom
    field couldn't be provisioned (Custom Field doctype unavailable) there's no
    dedup key to filter on, so we skip the lookup — the local mapping pointer is
    then the only dedup guard."""
    if is_doctype_unavailable(CUSTOM_FIELD_DT):
        return None
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
    # Drop any field whose linked doctype bootstrap found unavailable in this
    # ERPNext (e.g. account_subtype when Tim's instance has no Account Subtype).
    return _prune_unavailable_fields(doc)


def find_or_create_bank_account(client: ERPNextClient, account: PlaidAccount,
                                bank_name: str, institution: str
                                ) -> tuple[str, bool, bool]:
    """Find-or-create the Bank Account for this Plaid account. Returns
    (docname, created, retried) — retried is True when the create succeeded only
    after the defensive path dropped a rejected field."""
    existing = _find_bank_account(client, account)
    if existing:
        return existing, False, False
    doc = build_bank_account_doc(account, bank_name, institution)
    created, retried = _create_bank_account_defensive(
        client, doc, item_id=account.item_id)
    name = created.get('name')
    if not name:
        raise ERPNextAPIError('ERPNext returned no Bank Account name',
                              status_code=None)
    return name, True, retried


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
                'retried': False, 'message': 'already mapped'}

    if not is_supported(account):
        account.import_status = 'unsupported'
        db.session.commit()
        return {'status': 'unsupported', 'bank_account': None, 'bank': None,
                'created_account': False, 'created_bank': False,
                'retried': False,
                'message': f'{account.type}/{account.subtype} not supported'}

    client = client or get_client()
    if ensure_fields:
        # First step, idempotent: guarantee the Bank Account Type records +
        # custom fields exist so the create below can't fail on a missing link.
        # A missing doctype is handled inside bootstrap (marked unavailable, not
        # raised); any *other* bootstrap error is logged but must not sink the
        # import — the create's own defensive retry is the backstop.
        try:
            bootstrap(client)
        except ERPNextError:
            log.warning('bootstrap failed before import; continuing on the '
                        'create-side defensive path', exc_info=True)

    item = PlaidItem.query.filter_by(item_id=account.item_id).first()
    institution = ((item.institution_name if item else '') or '').strip() or 'Bank'

    # Bank (find-or-create), enriched best-effort with website/SWIFT.
    banks_before = _bank_exists(client, institution)
    extras = _institution_extras(plaid_client,
                                 item.institution_id if item else '')
    bank_name = find_or_create_bank(client, institution, extras=extras)
    created_bank = not banks_before

    # Bank Account (find-or-create, deduped on plaid_account_id).
    docname, created_account, retried = find_or_create_bank_account(
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
            'retried': retried, 'message': msg}


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
    # Bank Account Types + Account Subtypes + custom fields, once for the batch.
    # Missing doctypes are marked unavailable inside bootstrap (not raised); any
    # other bootstrap error is logged and the batch proceeds on the create-side
    # defensive path rather than failing every account.
    try:
        bootstrap(client)
    except ERPNextError:
        log.warning('bootstrap failed before bulk import; continuing',
                    exc_info=True)

    stats = {'created': 0, 'unsupported': 0, 'skipped_mapped': 0,
             'retried': 0, 'failed': 0, 'considered': 0, 'errors': []}
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
                if result.get('retried'):
                    stats['retried'] += 1
        except (ERPNextAPIError, ERPNextError) as e:
            db.session.rollback()
            stats['failed'] += 1
            detail = f'{account.name or account.mask}: {e}'
            stats['errors'].append(detail)
            log.warning('bulk import failed for %s: %s', account.account_id, e)
            # One sync-log row per failure carrying the actual Frappe body (via
            # str(e)), so the operator can diagnose each rejection at /admin/sync_log.
            _log(account.item_id, 0, 'failed', detail)

    total = stats['created'] + stats['unsupported'] + stats['failed']
    parts = [f"Created {stats['created']}/{total} accounts"]
    if stats['unsupported']:
        parts.append(f"skipped {stats['unsupported']} unsupported")
    if stats['retried']:
        parts.append(f"{stats['retried']} retried successfully")
    if stats['failed']:
        parts.append(f"{stats['failed']} failed")
    stats['summary'] = ', '.join(parts) + '.'
    # The final batch row folds in the individual errors so the summary line is
    # self-contained for triage (error_message is TEXT — the detail fits).
    summary_msg = stats['summary']
    if stats['errors']:
        summary_msg += ' | ' + ' ; '.join(stats['errors'][:10])
    _log('', stats['created'], 'failed' if stats['failed'] and not stats['created']
         else 'success', summary_msg)
    return stats
