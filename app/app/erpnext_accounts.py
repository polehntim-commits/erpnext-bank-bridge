# SPDX-License-Identifier: MIT
"""One-click account import: Plaid account → ERPNext Bank + Bank Account.

The transaction bridge (app/erpnext_bank.py) still assumes an operator has
hand-created the matching ERPNext Bank Account and mapped it. This module
removes that friction: given a linked Plaid account it will, idempotently,

  1. ensure the `plaid_account_id` + `last_4` custom fields exist on the
     ERPNext Bank Account doctype (first-push bootstrap),
  2. find-or-create the parent `Bank` (by bank_name),
  3. auto-create the matching GL `Account` in the company's Chart of Accounts
     (v0.2.0) — a Bank-typed leaf under the "Bank Accounts" group — so a company
     Bank Account can link a real `account` and keep is_company_account = 1,
  4. find-or-create the `Bank Account` (deduped on the `plaid_account_id`
     custom field), linking the GL account,
  5. wire the result back onto the local PlaidAccount (erpnext_bank_account_name,
     erpnext_gl_account_name + sync_enabled) and stamp import_status.

The GL auto-create is best-effort: if the Chart of Accounts can't be walked or
created, the import degrades to the v0.1.5 personal-account fallback (retry with
is_company_account = 0) rather than failing.

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
import re
from difflib import SequenceMatcher

from flask import current_app

from . import audit
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
# The doctype `Bank Account.account_subtype` links to. In ERPNext v15 this is
# named "Bank Account Subtype" (module Accounts, autoname field:account_subtype);
# earlier Bank Bridge builds probed the non-existent name "Account Subtype",
# which Frappe answers with an ImportError ("Module import failed for Account
# Subtype") — mis-read as an unavailable doctype, so the subtype was dropped from
# every import. Using the real name lets bootstrap provision the records and the
# Link on Bank Account resolve. (Fixed in v0.3.8.)
ACCOUNT_SUBTYPE_DT = 'Bank Account Subtype'
CUSTOM_FIELD_DT = 'Custom Field'
# The Chart-of-Accounts doctype the GL auto-create (v0.2.0) walks and creates in.
ACCOUNT_DT = 'Account'

# ── unavailable-doctype registry ────────────────────────────────────────────
#
# Some ERPNext / Frappe instances genuinely don't ship (or have a broken) linked
# doctype — the probe then returns HTTP 500 with an ImportError / "No module
# named …". Bootstrap must not crash on that; instead it records the doctype as
# *unavailable* and the
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
    missing-document / permission error. A truly-absent doctype answers the
    probe with HTTP 500 and 'No module named …' / ImportError."""
    blob = ((e.response_body or '') + ' ' + str(e)).lower()
    return e.status_code == 500 and ('no module named' in blob
                                     or 'importerror' in blob)

# The Bank Account Type records the import flow references. Stock ERPNext ships
# without them, so an out-of-box instance would reject a Bank Account that links
# one — we provision them as part of the idempotent bootstrap.
DEFAULT_BANK_ACCOUNT_TYPES = ('Current', 'Credit')

# The Bank Account Subtype records `Bank Account.account_subtype` links to. That
# field is a Link (ERPNext v15: options "Bank Account Subtype"), so a Bank Account
# create fails with a LinkValidationError ("Could not find Account Subtype:
# savings" — the error names the field label, not the doctype) unless the target
# record exists. Same remedy as Bank Account Type: provision them in the
# idempotent bootstrap. Docnames are Title Case (Frappe convention), and the
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


# Plaid subtype → precise ERPNext Bank Account Subtype docname (Title Case, to
# match the Bank Account Subtype link targets provisioned in bootstrap). v0.3.9
# replaces the old coarse buckets (cd/money market/… → 'Current', credit card →
# 'Other') with a 1:1 mapping onto the 10 provisioned masters. Anything unmapped
# still falls back to 'Other'.
_SUBTYPE_MAP = {
    'checking': 'Checking',
    'savings': 'Savings',
    'cd': 'Cd',
    'money market': 'Money Market',
    'cash management': 'Cash Management',
    'paypal': 'Paypal',
    'credit card': 'Credit Card',
    'line of credit': 'Line Of Credit',
}


def erpnext_account_subtype(account: PlaidAccount) -> str:
    """Precise `account_subtype` docname for the Bank Account, mapped 1:1 from
    the Plaid subtype (v0.3.9). Title Case, to match the Bank Account Subtype
    link-target docnames provisioned in bootstrap. A `credit`-type account with
    no recognized subtype still reads as 'Credit Card'; everything else unmapped
    falls back to 'Other'."""
    s = (account.subtype or '').strip().lower()
    if s in _SUBTYPE_MAP:
        return _SUBTYPE_MAP[s]
    if (account.type or '').strip().lower() == 'credit':
        return 'Credit Card'
    return 'Other'


# ── client / config ────────────────────────────────────────────────────────

def get_client(**kwargs) -> ERPNextClient:
    """An ERPNext client from the merged settings (reuses erpnext_bank's
    builder). Raises ERPNextConfigError if the connection isn't configured."""
    return erpnext_bank.get_client(**kwargs)


def _default_company() -> str:
    return (erpnext_settings.load().get('default_company') or '').strip()


def _default_is_company_account() -> int:
    """1 to mark imported Bank Accounts as company accounts, else 0. Driven by
    ERPNEXT_DEFAULT_IS_COMPANY_ACCOUNT (default True). When False we send 0 up
    front, so ERPNext's "Company Account is mandatory" check never fires and the
    create-side retry isn't needed."""
    return 1 if current_app.config.get('ERPNEXT_DEFAULT_IS_COMPANY_ACCOUNT', True) else 0


def _bank_account_group_name() -> str:
    """The Chart-of-Accounts group the per-account depository GL Accounts are
    created under (ERPNEXT_BANK_ACCOUNT_GROUP_NAME, default 'Bank Accounts')."""
    return (current_app.config.get('ERPNEXT_BANK_ACCOUNT_GROUP_NAME')
            or 'Bank Accounts').strip() or 'Bank Accounts'


def _credit_card_group_name() -> str:
    """The Chart-of-Accounts group credit-card GL Accounts are created under, on
    the Liabilities side (ERPNEXT_CREDIT_CARD_GROUP_NAME, default 'Credit
    Cards')."""
    return (current_app.config.get('ERPNEXT_CREDIT_CARD_GROUP_NAME')
            or 'Credit Cards').strip() or 'Credit Cards'


def _loan_group_name() -> str:
    """The Chart-of-Accounts group loan GL Accounts are created under, on the
    Liabilities side (ERPNEXT_LOAN_GROUP_NAME, default 'Loans')."""
    return (current_app.config.get('ERPNEXT_LOAN_GROUP_NAME')
            or 'Loans').strip() or 'Loans'


def _current_liabilities_group_name() -> str:
    """The parent group credit-card / current-loan groups anchor under
    (ERPNEXT_CURRENT_LIABILITIES_GROUP_NAME, default 'Current Liabilities')."""
    return (current_app.config.get('ERPNEXT_CURRENT_LIABILITIES_GROUP_NAME')
            or 'Current Liabilities').strip() or 'Current Liabilities'


def _longterm_liabilities_group_name() -> str:
    """The parent group long-term loans anchor under
    (ERPNEXT_LONGTERM_LIABILITIES_GROUP_NAME, default 'Long-term Liabilities').
    Stock ERPNext charts don't always ship this — the loan resolver falls back to
    Current Liabilities when it's absent."""
    return (current_app.config.get('ERPNEXT_LONGTERM_LIABILITIES_GROUP_NAME')
            or 'Long-term Liabilities').strip() or 'Long-term Liabilities'


def _gl_side(account: PlaidAccount) -> str:
    """Which side of the Chart of Accounts this Plaid account's GL leaf belongs
    on: 'credit' (cards / lines of credit → Current Liabilities), 'loan' (→
    Loans, on the Liabilities side), or 'depository' (checking / savings / … →
    Assets, the existing Bank Accounts group). Drives the GL parent choice
    (v0.3.9); the leaf's own account_type stays 'Bank' regardless, so ERPNext's
    Bank Reconciliation Tool still operates on it."""
    t = (account.type or '').strip().lower()
    s = (account.subtype or '').strip().lower()
    if t == 'credit' or s in _CREDIT_SUBTYPES:
        return 'credit'
    if t == 'loan':
        return 'loan'
    return 'depository'


def _account_currency(account: PlaidAccount) -> str:
    """Currency for a created GL Account: the Plaid account's iso_currency_code
    (or its cached currency), else ERPNEXT_BANK_ACCOUNT_CURRENCY (default USD)."""
    for c in (account.iso_currency_code, account.currency):
        if (c or '').strip():
            return c.strip()
    return (current_app.config.get('ERPNEXT_BANK_ACCOUNT_CURRENCY')
            or 'USD').strip() or 'USD'


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
    """Ensure ERPNext has the Bank Account Subtype records the import flow links from
    `Bank Account.account_subtype` (Checking, Savings, Current, …). Where that
    field is a Link, an out-of-box instance has no matching target and rejects
    the Bank Account create with a LinkValidationError. Idempotent: a record is
    only created when the GET returns 404. Docnames are Title Case, matching the
    values erpnext_account_subtype sends.

    Returns True if available, False if this ERPNext lacks the Bank Account
    Subtype doctype entirely (a truly-absent doctype answers HTTP 500 'No module
    named …'). In that case we log-warn, mark it unavailable, and skip — the
    send-side then drops `account_subtype` so the import still succeeds."""
    for name in DEFAULT_ACCOUNT_SUBTYPES:
        try:
            existing = client.get_doc(ACCOUNT_SUBTYPE_DT, name)
        except ERPNextAPIError as e:
            if _is_missing_doctype_error(e):
                log.warning('Bank Account Subtype doctype unavailable in this '
                            'ERPNext; skipping provisioning (%s)', str(e)[:200])
                _mark_doctype_unavailable(ACCOUNT_SUBTYPE_DT)
                return False
            raise
        if existing is not None:
            continue
        # Mirror Bank Account Type: the master autonames from its titling field,
        # so the created record's docname is exactly `name`.
        client.create_doc(ACCOUNT_SUBTYPE_DT, {'account_subtype': name})
        log.info("created Bank Account Subtype '%s'", name)
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
    Current/Credit Bank Account Type records, the Bank Account Subtype records, and
    the Bank Account custom fields. The link-target masters come first so a Bank
    Account create can't fail on a missing link target.

    Resilient to instances that lack one of these linked doctypes: a missing
    doctype is logged, recorded in the unavailable registry, and skipped — never
    raised — so one broken doctype can't sink the whole bootstrap (or poison the
    request that triggered it). Returns a per-doctype availability dict, e.g.
    {'Bank Account Type': True, 'Bank Account Subtype': False, 'Custom Field': True,
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


# ── GL Account (Chart of Accounts) auto-create — v0.2.0 ──────────────────────
#
# A company Bank Account (`is_company_account = 1`) is required by ERPNext to
# link a specific GL `account` of type Bank from the company's Chart of
# Accounts. v0.1.5 dropped is_company_account to 0 when that link was missing;
# v0.2.0 does it properly: find (or create) a "Bank Accounts" group under
# Assets → Current Assets, create a per-account leaf GL Account under it, and
# link that on the Bank Account so is_company_account stays 1.
#
# Everything here is best-effort and idempotent. If any step fails (broken CoA,
# permission error, unusual template) the caller degrades to the v0.1.5
# personal-account fallback — an import never fails because the GL path didn't
# work.


def _find_accounts(client: ERPNextClient, company: str, *, account_name=None,
                   is_group=None, root_type=None) -> list[dict]:
    """List Chart-of-Accounts records for `company` matching the given facets.
    Returns the raw dicts (name / account_name / parent_account / root_type)."""
    filters = [['company', '=', company]]
    if account_name is not None:
        filters.append(['account_name', '=', account_name])
    if is_group is not None:
        filters.append(['is_group', '=', is_group])
    if root_type is not None:
        filters.append(['root_type', '=', root_type])
    return client.list_docs(
        ACCOUNT_DT, filters=filters,
        fields=['name', 'account_name', 'parent_account', 'root_type', 'is_group'],
        limit_page_length=0)


def _create_group_account(client: ERPNextClient, account_name: str,
                          parent_account: str, company: str, *,
                          account_type: str | None = None) -> str:
    """Create an is_group=1 Account under `parent_account`; return its docname.
    root_type is inherited from the parent by ERPNext, so we don't set it.

    v0.3.9: when the parent group uses account numbering, the new group also gets
    an account_number in the parent's range (e.g. Current Liabilities siblings
    2100/2200/2300/2400 → new group 2500). Skipped silently when the chart
    doesn't number its accounts."""
    doc = {'account_name': account_name, 'parent_account': parent_account,
           'company': company, 'is_group': 1}
    if account_type:
        doc['account_type'] = account_type
    number = _next_account_number(client, company, parent_account, is_group=True)
    if number:
        doc['account_number'] = number
    created = client.create_doc(ACCOUNT_DT, doc)
    name = created.get('name')
    log.info("created Chart-of-Accounts group '%s' under '%s'%s",
             name or account_name, parent_account,
             f' as #{number}' if number else '')
    return name or account_name


def _asset_root(client: ERPNextClient, company: str) -> str | None:
    """The company's root Asset group (the top of the Assets branch). Prefers a
    root_type=Asset group with no parent_account; falls back to the first
    Asset-rooted group. None if the company has no Chart of Accounts yet."""
    groups = _find_accounts(client, company, is_group=1, root_type='Asset')
    if not groups:
        return None
    for g in groups:
        if not (g.get('parent_account') or ''):
            return g['name']
    return groups[0]['name']


def ensure_bank_account_group(client: ERPNextClient, company: str) -> str | None:
    """Find (or create) the group Account the per-account Bank GL Accounts live
    under, for `company`; return its docname. The conventional path is
    Assets → Current Assets → Bank Accounts, which stock ERPNext ships.

    Walks down, creating only what's missing:
      1. the configured group name (default 'Bank Accounts', is_group, Bank) —
         reuse if present;
      2. else find 'Current Assets' and create the group under it;
      3. else find the Asset root, create 'Current Assets' under it, then the
         group under that.
    Returns None when the company has no Chart of Accounts to anchor to (fresh
    install) — the caller then falls back to the personal-account path."""
    group_name = _bank_account_group_name()
    existing = _find_accounts(client, company, account_name=group_name, is_group=1)
    if existing:
        return existing[0]['name']

    current_assets = _find_accounts(client, company, account_name='Current Assets',
                                    is_group=1)
    if current_assets:
        return _create_group_account(client, group_name, current_assets[0]['name'],
                                     company, account_type='Bank')

    root = _asset_root(client, company)
    if root:
        current_assets_name = _create_group_account(client, 'Current Assets', root,
                                                     company)
        return _create_group_account(client, group_name, current_assets_name,
                                     company, account_type='Bank')

    log.info('no Chart of Accounts anchor (Bank group / Current Assets / Asset '
             'root) found for company %r; skipping GL auto-create', company)
    return None


# ── v0.3.9: liability-side groups (credit cards, loans) ─────────────────────
#
# Credit cards and loans are NOT assets — a credit card is a current liability,
# a loan a (usually long-term) liability. Before v0.3.9 every imported account,
# cards included, was created as a Bank leaf under the Assets-side "Bank
# Accounts" group, overstating assets and understating liabilities. Now the GL
# parent is chosen by Plaid `type` (see _gl_side): depository stays on Assets,
# credit lands under Current Liabilities → Credit Cards, loan under Loans. The
# leaf's account_type is still 'Bank' so Bank Reconciliation keeps working.


def _liability_root(client: ERPNextClient, company: str) -> str | None:
    """The company's root Liability group (top of the Liabilities branch).
    Prefers a root_type=Liability group with no parent_account; falls back to the
    first Liability-rooted group. None if the company has no Chart of Accounts."""
    groups = _find_accounts(client, company, is_group=1, root_type='Liability')
    if not groups:
        return None
    for g in groups:
        if not (g.get('parent_account') or ''):
            return g['name']
    return groups[0]['name']


def _ensure_current_liabilities(client: ERPNextClient, company: str) -> str | None:
    """Find (or create) the 'Current Liabilities' group for `company`; return its
    docname. Falls back to creating it under the Liability root when the chart
    doesn't ship it. None when there's no Liability branch to anchor to."""
    name = _current_liabilities_group_name()
    existing = _find_accounts(client, company, account_name=name, is_group=1)
    if existing:
        return existing[0]['name']
    root = _liability_root(client, company)
    if root:
        return _create_group_account(client, name, root, company)
    return None


def ensure_credit_card_group(client: ERPNextClient, company: str) -> str | None:
    """Find (or create) the group credit-card GL Accounts live under, for
    `company`; return its docname. The conventional path is Liabilities →
    Current Liabilities → Credit Cards.

    Walks down, creating only what's missing:
      1. the configured group name (default 'Credit Cards', is_group) — reuse if
         present;
      2. else find/create 'Current Liabilities' and create the group under it.
    Returns None when the company has no Liability branch to anchor to — the
    caller then degrades to the personal-account path (same as the Assets side)."""
    group_name = _credit_card_group_name()
    existing = _find_accounts(client, company, account_name=group_name, is_group=1)
    if existing:
        return existing[0]['name']
    current_liabilities = _ensure_current_liabilities(client, company)
    if current_liabilities:
        return _create_group_account(client, group_name, current_liabilities,
                                     company)
    log.info('no Current Liabilities anchor for company %r; skipping credit-card '
             'GL group', company)
    return None


def ensure_loan_group(client: ERPNextClient, company: str) -> str | None:
    """Find (or create) the group loan GL Accounts live under, for `company`;
    return its docname. Prefers Long-term Liabilities → Loans, falling back to
    Current Liabilities → Loans when the chart has no long-term branch (stock
    ERPNext charts often don't).

    Walks down, creating only what's missing:
      1. an existing 'Loans' (or stock 'Loans (Liabilities)') group — reuse;
      2. else create 'Loans' under Long-term Liabilities if that group exists;
      3. else create 'Loans' under Current Liabilities;
      4. else create it directly under the Liability root.
    Returns None when the company has no Liability branch at all."""
    group_name = _loan_group_name()
    for candidate in (group_name, 'Loans (Liabilities)'):
        existing = _find_accounts(client, company, account_name=candidate,
                                  is_group=1)
        if existing:
            return existing[0]['name']
    for parent_name in (_longterm_liabilities_group_name(),
                        _current_liabilities_group_name()):
        parent = _find_accounts(client, company, account_name=parent_name,
                                is_group=1)
        if parent:
            return _create_group_account(client, group_name, parent[0]['name'],
                                         company)
    root = _liability_root(client, company)
    if root:
        return _create_group_account(client, group_name, root, company)
    log.info('no Liability anchor for company %r; skipping loan GL group', company)
    return None


def resolve_gl_parent(client: ERPNextClient, account: PlaidAccount,
                      company: str) -> str | None:
    """The Chart-of-Accounts group a Plaid account's GL leaf belongs under,
    chosen by _gl_side (v0.3.9): credit → Credit Cards (Current Liabilities),
    loan → Loans, depository → Bank Accounts (Assets). Returns the group docname
    or None when the relevant branch can't be found/created."""
    side = _gl_side(account)
    if side == 'credit':
        return ensure_credit_card_group(client, company)
    if side == 'loan':
        return ensure_loan_group(client, company)
    return ensure_bank_account_group(client, company)


# ── v0.3.1: child account numbering + fuzzy dedup ──────────────────────────
#
# Two polish behaviours on the auto-CoA create:
#   * children of a numbered parent group (e.g. '1200 - Bank Accounts') get a
#     sequential account_number ('1201', '1202', …) so they sit tidily in the
#     Chart-of-Accounts hierarchy instead of being number-less leaves;
#   * before creating a leaf we fuzzy-match the intended account_name against the
#     company's existing leaf Accounts (stdlib difflib + last-4 mask signal) and
#     REUSE a close-enough one rather than making a near-duplicate.
# Both degrade to the pre-v0.3.1 behaviour (no number / create new) on any read
# error or when the signal is absent, so neither can block an import.

_MASK_SUFFIX_RE = re.compile(r'\s*-\s*[\w*]+\s*$')


def _fuzzy_threshold() -> int:
    """Similarity percentage (0-100) at/above which an existing GL Account is a
    match for reuse. Driven by ERPNEXT_FUZZY_MATCH_THRESHOLD (default 85)."""
    try:
        return int(current_app.config.get('ERPNEXT_FUZZY_MATCH_THRESHOLD', 85))
    except (TypeError, ValueError):
        return 85


def _norm(s: str) -> str:
    return ' '.join((s or '').lower().split())


def _strip_mask_suffix(name: str) -> str:
    """Drop a trailing ' - <mask>' so 'Wells Fargo Checking - 0000' compares
    equal to 'Wells Fargo Checking'."""
    return _MASK_SUFFIX_RE.sub('', (name or '')).strip()


def _similarity(a: str, b: str) -> float:
    """Percentage name similarity (0-100) via stdlib difflib. Scored both
    verbatim and with any trailing mask suffix stripped; the higher wins, so a
    masked name still matches its unmasked twin."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    whole = SequenceMatcher(None, na, nb).ratio()
    bare = SequenceMatcher(None, _norm(_strip_mask_suffix(a)),
                           _norm(_strip_mask_suffix(b))).ratio()
    return max(whole, bare) * 100.0


def _fuzzy_match_gl_account(client: ERPNextClient, company: str,
                            account_name: str, mask: str | None) -> dict | None:
    """Best existing leaf GL Account in `company` that closely matches
    `account_name` (or shares the last-4 `mask`), for reuse instead of a
    near-duplicate create. Returns {'name', 'account_name', 'score'} at/above the
    configured threshold, else None. Best-effort — any read error yields None."""
    threshold = _fuzzy_threshold()
    try:
        existing = client.list_docs(
            ACCOUNT_DT,
            filters=[['company', '=', company], ['is_group', '=', 0]],
            fields=['name', 'account_name'], limit_page_length=0)
    except (ERPNextAPIError, ERPNextError):
        return None
    mask = (mask or '').strip()
    best = None
    for row in existing or []:
        docname = row.get('name')
        if not docname:
            continue
        cand_name = row.get('account_name') or docname
        score = _similarity(account_name, cand_name)
        # Last-4 mask signal: an existing account carrying this account's mask,
        # with a plausibly-similar base name, qualifies even if the raw ratio is
        # a shade under the threshold.
        if mask and mask in cand_name:
            base = _similarity(_strip_mask_suffix(account_name),
                               _strip_mask_suffix(cand_name))
            if base >= 60:
                score = max(score, float(threshold))
        if score >= threshold and (best is None or score > best['score']):
            best = {'name': docname, 'account_name': cand_name,
                    'score': round(score, 1)}
    return best


_LEADING_INT_RE = re.compile(r'^(\d+)')


def _leading_int(value) -> int | None:
    """The leading integer of an account_number string, tolerating the range
    form some group numbers take ('2100-2400' → 2100, '1200' → 1200). None when
    there's no leading digit ('' , 'Test' → None)."""
    m = _LEADING_INT_RE.match(str(value or '').strip())
    return int(m.group(1)) if m else None


def _company_account_numbers(client: ERPNextClient, company: str) -> set[int]:
    """Every integer account_number in use across `company` (leading-int of the
    range form counts). Used to guarantee a freshly-assigned number is unique —
    ERPNext rejects a duplicate account_number in a company. Best-effort: any
    read error yields an empty set (the caller then skips the uniqueness bump)."""
    try:
        rows = client.list_docs(
            ACCOUNT_DT, filters=[['company', '=', company]],
            fields=['account_number'], limit_page_length=0)
    except (ERPNextAPIError, ERPNextError):
        return set()
    out = set()
    for r in (rows or []):
        n = _leading_int(r.get('account_number'))
        if n is not None:
            out.add(n)
    return out


# ── v0.3.9: liquidity-ordered leaf numbering ───────────────────────────────
#
# Standard balance-sheet convention: within a group, the most-liquid account
# gets the lowest number, the least-liquid the highest. The rank is keyed on
# Plaid subtype and shared by the Assets (depository) and Liabilities (credit)
# sides — the two never share a group, so the overlapping rank values (Cash
# Management and Credit Card are both rank 1) can't collide. Unmapped subtypes
# fall to the end (rank 99). Rank 5 (Bonds) is reserved for a future
# investment-side extension — Plaid `investment` accounts stay skipped today, so
# nothing maps to it yet. Extend the map, not the call sites.
LIQUIDITY_RANK = {
    # depository (Assets side), most→least liquid
    'cash management': 1,   # Cash — daily access, market-rate
    'paypal': 1,            # cash-equivalent balance
    'checking': 2,          # immediate access, primary transaction account
    'savings': 3,           # immediate access, usually held as reserves
    'money market': 3,      # alongside savings (slots after it by name)
    'cd': 4,                # least liquid — locked for a term
    # 5 reserved: Bonds (least liquid cash-equivalent) — not a Plaid Bank type
    # credit (Liabilities side), most→least current-due
    'credit card': 1,       # revolving, monthly due
    'line of credit': 2,
}
DEFAULT_LIQUIDITY_RANK = 99

# Finer within-rank ordering, so Money Market sorts *after* Savings even though
# they share rank 3 and 'Money Market' would otherwise sort first alphabetically.
_SUBTYPE_ORDER = {'savings': 0, 'money market': 1}

# Numbers reserved per liquidity rank inside a group's range. A group is
# hundred-spaced (e.g. 1200 → children 1201-1299), so 10 per rank leaves room
# for up to 10 accounts of each liquidity tier before spilling into the next.
_LIQUIDITY_BAND_SIZE = 10


def liquidity_rank(account: PlaidAccount) -> int:
    """The liquidity rank (lower = more liquid) for a Plaid account, from its
    subtype. Unmapped subtypes get DEFAULT_LIQUIDITY_RANK (sorted last)."""
    return LIQUIDITY_RANK.get((account.subtype or '').strip().lower(),
                              DEFAULT_LIQUIDITY_RANK)


def _liquidity_sort_key(subtype: str, account_name: str) -> tuple:
    """Sort key placing accounts most-liquid first: (rank, within-rank order,
    name). Used by the retroactive backfill and mirrored by the band layout."""
    s = (subtype or '').strip().lower()
    return (LIQUIDITY_RANK.get(s, DEFAULT_LIQUIDITY_RANK),
            _SUBTYPE_ORDER.get(s, 0), account_name or '')


def _band_slot(rank: int) -> int:
    """0-based band slot for a liquidity rank within the group's range: rank 1 →
    slot 0, … capped at 8, with the unmapped tail (rank ≥ 90) in slot 9."""
    return 9 if rank >= 90 else min(max(rank, 1) - 1, 8)


def _band_start(base: int, rank: int) -> int:
    """First number of a rank's reserved band under a group numbered `base`
    (e.g. base 1200: rank 1 → 1201, rank 2 → 1211, rank 4 → 1231)."""
    return base + 1 + _band_slot(rank) * _LIQUIDITY_BAND_SIZE


def _next_leaf_account_number(client: ERPNextClient, company: str,
                              parent_group: str,
                              account: PlaidAccount) -> str | None:
    """account_number for a new leaf, slotted into its liquidity band under
    `parent_group` (v0.3.9): the leaf takes the next free number in its rank's
    reserved band (rank from the Plaid subtype), so a new checking always lands in
    the checking band — after existing checkings, before less-liquid accounts —
    without disturbing any existing account. Falls back to a plain append when the
    parent group has no number but its leaves do, and returns None (no numbering)
    when neither the parent nor any sibling is numbered, or on a read error."""
    try:
        parent_rows = client.list_docs(
            ACCOUNT_DT, filters=[['name', '=', parent_group]],
            fields=['name', 'account_number'], limit_page_length=1)
    except (ERPNextAPIError, ERPNextError):
        return None
    base = _leading_int(parent_rows[0].get('account_number')) if parent_rows else None
    try:
        siblings = client.list_docs(
            ACCOUNT_DT,
            filters=[['parent_account', '=', parent_group],
                     ['company', '=', company], ['is_group', '=', 0]],
            fields=['name', 'account_number'], limit_page_length=0)
    except (ERPNextAPIError, ERPNextError):
        siblings = []
    sib_strs = [str(s.get('account_number') or '').strip() for s in (siblings or [])]
    sib_nums = [int(s) for s in sib_strs if s.isdigit()]
    width = max((len(s) for s in sib_strs if s.isdigit()),
                default=(len(str(base)) if base else 0))

    if base is None:
        if not sib_nums:
            log.info("parent group %r has no numeric account_number and no "
                     "numbered siblings; skipping leaf auto-numbering", parent_group)
            return None
        candidate = max(sib_nums) + 1   # can't band without a base → append
    else:
        rank = liquidity_rank(account)
        band_start = _band_start(base, rank)
        band_end = band_start + _LIQUIDITY_BAND_SIZE - 1
        in_band = [n for n in sib_nums if band_start <= n <= band_end]
        candidate = (max(in_band) + 1) if in_band else band_start

    used = _company_account_numbers(client, company)
    while candidate in used:
        candidate += 1
    return str(candidate).zfill(width)


def _next_account_number(client: ERPNextClient, company: str, parent_group: str,
                         *, is_group: bool = False) -> str | None:
    """Next account_number for a new child under `parent_group`, matching the
    chart's existing numbering convention (v0.3.9). None — so the create omits the
    field — when the chart doesn't number its accounts (parent unnumbered AND no
    numbered siblings) or on any read error.

    Used for GROUP accounts (is_group=True); leaves are numbered by liquidity via
    _next_leaf_account_number.

    Scheme, discovered by inspecting siblings of the same kind (group vs leaf):
      * with numbered siblings → max sibling number + step, where step is 1 for a
        leaf and the siblings' spacing (default 100) for a group. So consecutive
        leaves 1201/1202 → 1203, and hundred-spaced groups 2100/2200/2300/2400 →
        2500;
      * no numbered siblings yet → the parent's own (leading) number + step, e.g.
        parent '1200' → first leaf '1201'.
    The result is zero-padded to the siblings' width and bumped past any number
    already used in the company, so it can't collide. Monotonic — gaps are never
    backfilled."""
    try:
        siblings = client.list_docs(
            ACCOUNT_DT,
            filters=[['parent_account', '=', parent_group],
                     ['company', '=', company],
                     ['is_group', '=', 1 if is_group else 0]],
            fields=['name', 'account_number'], limit_page_length=0)
    except (ERPNextAPIError, ERPNextError):
        return None
    sib_strs = [str(c.get('account_number') or '').strip() for c in (siblings or [])]
    nums = sorted(int(s) for s in sib_strs if s.isdigit())
    width = max((len(s) for s in sib_strs if s.isdigit()), default=0)

    if nums:
        if is_group:
            gaps = [b - a for a, b in zip(nums, nums[1:]) if b - a > 0]
            step = min(gaps) if gaps else 100
        else:
            step = 1
        candidate = nums[-1] + step
    else:
        try:
            parent_rows = client.list_docs(
                ACCOUNT_DT, filters=[['name', '=', parent_group]],
                fields=['name', 'account_number'], limit_page_length=1)
        except (ERPNextAPIError, ERPNextError):
            return None
        base = _leading_int(parent_rows[0].get('account_number')) if parent_rows else None
        if base is None:
            log.info("parent group %r has no numeric account_number and no "
                     "numbered siblings; skipping auto-numbering", parent_group)
            return None
        step = 100 if is_group else 1
        candidate = base + step
        width = width or len(str(base))

    used = _company_account_numbers(client, company)
    bump = step if step else 1
    while candidate in used:
        candidate += bump
    return str(candidate).zfill(width)


def find_or_create_gl_account_for(client: ERPNextClient, account: PlaidAccount,
                                  parent_group: str, company: str,
                                  account_name: str, *,
                                  skip_fuzzy: bool = False) -> str | None:
    """Find (or create) the leaf Bank GL Account for one Plaid account under
    `parent_group`; return its docname. Idempotent: an existing leaf with the
    same account_name + company is reused, so re-import never duplicates. The
    created docname follows Frappe autonaming ('<account_name> - <company_abbr>')
    and is read back from the create response rather than computed.

    v0.3.1: when no exact leaf exists, a fuzzy-similar one is reused instead of
    creating a near-duplicate (unless `skip_fuzzy`, the UI "create new anyway"
    path). v0.3.9: a freshly-created leaf under a numbered parent gets an
    account_number slotted into its liquidity band (most-liquid lowest). Both are
    audited."""
    existing = client.list_docs(
        ACCOUNT_DT,
        filters=[['account_name', '=', account_name], ['company', '=', company],
                 ['is_group', '=', 0]],
        fields=['name'], limit_page_length=1)
    if existing:
        return existing[0]['name']
    if not skip_fuzzy:
        match = _fuzzy_match_gl_account(client, company, account_name, account.mask)
        if match:
            log.info("reusing GL Account %r (%.1f%% match) instead of creating "
                     "%r", match['name'], match['score'], account_name)
            audit.record(
                'fuzzy_match_found', subject_type='Account',
                subject_id=match['name'],
                notes=(f"reused '{match['account_name']}' for intended "
                       f"'{account_name}' ({match['score']}% similar)"),
                after={'intended_account_name': account_name,
                       'matched_account': match['name'], 'score': match['score']})
            return match['name']
    doc = {
        'account_name': account_name,
        'parent_account': parent_group,
        'company': company,
        'account_type': 'Bank',
        'account_currency': _account_currency(account),
        'is_group': 0,
    }
    number = _next_leaf_account_number(client, company, parent_group, account)
    if number:
        doc['account_number'] = number
    created = client.create_doc(ACCOUNT_DT, doc)
    name = created.get('name')
    log.info("created Bank GL Account '%s' under '%s'%s", name or account_name,
             parent_group, f' as #{number}' if number else '')
    if number:
        audit.record(
            'gl_account_number_assigned', subject_type='Account',
            subject_id=name or account_name,
            notes=f'assigned account_number {number} under {parent_group}',
            after={'account': name or account_name, 'account_number': number,
                   'parent_account': parent_group})
    return name


def _resolve_gl_account(client: ERPNextClient, account: PlaidAccount,
                        company: str, account_name: str, *,
                        skip_fuzzy: bool = False) -> str | None:
    """Best-effort: resolve the group and per-account leaf GL Account, returning
    the leaf docname to link on the Bank Account. Returns None (logging a warning)
    on ANY failure so the caller degrades to the v0.1.5 personal-account path —
    the proper company-account link is best-effort, never a hard requirement."""
    if not company:
        return None
    try:
        # v0.3.9: parent chosen by Plaid type — depository → Assets/Bank
        # Accounts, credit → Current Liabilities/Credit Cards, loan → Loans.
        group = resolve_gl_parent(client, account, company)
        if not group:
            return None
        return find_or_create_gl_account_for(client, account, group, company,
                                             account_name, skip_fuzzy=skip_fuzzy)
    except (ERPNextAPIError, ERPNextError):
        log.warning('GL Account auto-create failed for %s; falling back to the '
                    'personal-account path', account.account_id, exc_info=True)
        return None


# The Bank Account fields we will never drop on a retry — without them the doc
# is meaningless. account_name + bank are always essential. account_type is
# essential ONLY when bootstrap has proved the Bank Account Type doctype exists;
# if that doctype is unavailable the field is dropped up-front (see
# _prune_unavailable_fields) and must not be treated as un-droppable, or every
# import on such an instance would fail. `account` (the auto-created GL link) is
# essential too: it must never be caught by the generic field-drop path — its
# name is a substring of every other 'account…' field, so a LinkValidationError
# on account_subtype would otherwise strip the GL link and leave a company
# account with no `account` (an unrecoverable "Company Account is mandatory").
# Dropping it is only ever right paired with is_company_account → 0, which the
# dedicated company-mandatory retry does explicitly. Everything else
# (account_subtype, is_company_account, company, the custom fields) is
# "preferred": send it if ERPNext accepts it, drop it if ERPNext rejects it.
_BASE_ESSENTIAL_BANK_ACCOUNT_FIELDS = {'account_name', 'bank', 'account'}

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


def _is_company_account_mandatory(body_low: str) -> bool:
    """True when a Frappe rejection body means a *company* Bank Account needs a
    linked GL account we can't supply — i.e. "Company Account is mandatory", or
    (defensively) any body naming both "company account" and "mandatory". This is
    the one mandatory-field error we CAN auto-repair: retry as a personal account
    (is_company_account = 0), which needs no GL link."""
    if 'company account is mandatory' in body_low:
        return True
    return 'company account' in body_low and 'mandatory' in body_low


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
        # The one mandatory-field error we CAN repair: a company Bank Account
        # requires a linked GL account we don't have. Retry ONCE as a personal
        # account (is_company_account = 0, no `account` link). Checked before the
        # generic mandatory surface below so it isn't swallowed by it.
        if _is_company_account_mandatory(low):
            retry_doc = {k: v for k, v in doc.items() if k != 'account'}
            retry_doc['is_company_account'] = 0
            msg = ('Retry: dropped is_company_account=1 → 0 due to missing GL '
                   'account link. Bank Account created as personal — promote to '
                   'company manually in ERPNext when your Chart of Accounts is '
                   f'set up. Original error: {str(e)[:400]}')
            log.warning('Bank Account create rejected (Company Account '
                        'mandatory); retrying as a personal account')
            _log(item_id, 0, 'retry', msg)
            try:
                return client.create_doc(BANK_ACCOUNT_DT, retry_doc), True
            except ERPNextAPIError as e2:
                # One retry max — don't chain. Log the actual error with the
                # retried payload so the still-failing create is diagnosable.
                _log(item_id, 0, 'failed',
                     'Retry as personal account still failed: '
                     f'{str(e2)[:400]} | payload fields: {sorted(retry_doc)}')
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


def _bank_account_name(account: PlaidAccount, institution: str) -> str:
    """The Bank Account (and matching GL Account) account_name — follows
    '<Institution> <TitleCasedSubtype> - <mask>' (e.g. 'Wells Fargo Checking -
    0000')."""
    subtype_title = (account.subtype or account.type or 'Account').strip().title()
    mask = (account.mask or '0000').strip()
    return f'{institution} {subtype_title} - {mask}'.strip()


def build_bank_account_doc(account: PlaidAccount, bank_name: str,
                           institution: str, *, account_name: str | None = None,
                           gl_account: str | None = None) -> dict:
    """Assemble the ERPNext Bank Account payload. account_name follows
    '<Institution> <TitleCasedSubtype> - <mask>' (e.g. 'Wells Fargo Checking -
    0000'). iban / bank_account_no are left blank (Plaid doesn't expose them).

    When `gl_account` is supplied (the auto-created Chart-of-Accounts leaf) and
    this is a company account, it is linked via `account` so is_company_account
    stays 1 — the v0.2.0 proper path. Without it the doc carries no `account`,
    and a company-account create relies on the v0.1.5 personal-account retry."""
    account_name = account_name or _bank_account_name(account, institution)
    doc = {
        'account_name': account_name,
        'bank': bank_name,
        'account_type': erpnext_account_type(account),
        'account_subtype': erpnext_account_subtype(account),
        'is_company_account': _default_is_company_account(),
        # Auto-provisioned custom fields (dedup key + mask mirror).
        'plaid_account_id': account.account_id,
        'last_4': account.mask or '',
    }
    company = _default_company()
    if company:
        doc['company'] = company
    # Link the auto-created GL Account only for a company account — the `account`
    # link is meaningless (and rejected) on a personal Bank Account.
    if gl_account and doc.get('is_company_account'):
        doc['account'] = gl_account
    # Drop any field whose linked doctype bootstrap found unavailable in this
    # ERPNext (e.g. account_subtype if this instance lacks Bank Account Subtype).
    return _prune_unavailable_fields(doc)


def find_or_create_bank_account(client: ERPNextClient, account: PlaidAccount,
                                bank_name: str, institution: str, *,
                                company: str | None = None,
                                skip_fuzzy: bool = False
                                ) -> tuple[str, bool, bool, str | None]:
    """Find-or-create the Bank Account for this Plaid account. Returns
    (docname, created, retried, gl_account) — retried is True when the create
    succeeded only after the defensive path dropped a rejected field; gl_account
    is the auto-created (or fuzzy-reused) Chart-of-Accounts leaf that was linked
    (None if the GL path was skipped or fell back).

    `skip_fuzzy` forwards the UI "create new anyway" decision to the GL resolver.

    The GL Account is resolved (and only then created) after the existing-Bank-
    Account check, so a re-import that finds an existing account never creates an
    orphan GL Account."""
    existing = _find_bank_account(client, account)
    if existing:
        return existing, False, False, None
    account_name = _bank_account_name(account, institution)
    # Best-effort GL Account for a company import; None degrades to v0.1.5.
    gl_account = None
    if company and _default_is_company_account():
        gl_account = _resolve_gl_account(client, account, company, account_name,
                                         skip_fuzzy=skip_fuzzy)
    doc = build_bank_account_doc(account, bank_name, institution,
                                 account_name=account_name, gl_account=gl_account)
    created, retried = _create_bank_account_defensive(
        client, doc, item_id=account.item_id)
    name = created.get('name')
    if not name:
        raise ERPNextAPIError('ERPNext returned no Bank Account name',
                              status_code=None)
    return name, True, retried, gl_account


# ── per-account import ──────────────────────────────────────────────────────

def import_plaid_account_to_erpnext(plaid_account_id: str, *,
                                    client: ERPNextClient | None = None,
                                    plaid_client=None,
                                    ensure_fields: bool = True,
                                    fuzzy_decision: str | None = None) -> dict:
    """Create (or find) the ERPNext Bank + Bank Account for one Plaid account
    and wire the mapping back. Idempotent — a second call finds the existing
    records and re-links without creating duplicates.

    `fuzzy_decision` carries the operator's answer to a fuzzy-match prompt:
    'create_new' skips GL-Account fuzzy dedup (create a fresh leaf); None/'reuse'
    leave the default auto-reuse-on-match behaviour in place.

    Returns a result dict: {'status': imported|skipped|unsupported,
    'bank_account': <docname or None>, 'bank': <bank docname or None>,
    'created_account': bool, 'created_bank': bool, 'message': str}.
    """
    skip_fuzzy = fuzzy_decision == 'create_new'
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

    # Bank Account (find-or-create, deduped on plaid_account_id). The company
    # drives the v0.2.0 GL Account auto-create (best-effort; falls back to the
    # v0.1.5 personal path if the Chart of Accounts can't be walked/created).
    docname, created_account, retried, gl_account = find_or_create_bank_account(
        client, account, bank_name, institution, company=_default_company(),
        skip_fuzzy=skip_fuzzy)

    account.erpnext_bank_account_name = docname
    if gl_account:
        account.erpnext_gl_account_name = gl_account
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


def probe_fuzzy_gl_match(plaid_account_id: str, *,
                         client: ERPNextClient | None = None) -> dict | None:
    """UI helper (v0.3.1): would importing this Plaid account reuse an existing
    GL Account via fuzzy match? Returns the candidate {'name', 'account_name',
    'score'} or None. Read-only — creates nothing. Best-effort: returns None
    whenever the account is already mapped/unsupported, the GL path wouldn't run
    (no company / personal-account mode), an exact leaf already exists (plain
    dedup, not a prompt-worthy fuzzy case), or ERPNext can't be reached."""
    account = PlaidAccount.query.filter_by(account_id=plaid_account_id).first()
    if account is None or account.erpnext_bank_account_name:
        return None
    if not is_supported(account):
        return None
    company = _default_company()
    if not company or not _default_is_company_account():
        return None
    try:
        client = client or get_client()
    except (ERPNextConfigError, ERPNextError):
        return None
    item = PlaidItem.query.filter_by(item_id=account.item_id).first()
    institution = ((item.institution_name if item else '') or '').strip() or 'Bank'
    account_name = _bank_account_name(account, institution)
    try:
        exact = client.list_docs(
            ACCOUNT_DT,
            filters=[['account_name', '=', account_name], ['company', '=', company],
                     ['is_group', '=', 0]],
            fields=['name'], limit_page_length=1)
        if exact:
            return None
        return _fuzzy_match_gl_account(client, company, account_name, account.mask)
    except (ERPNextAPIError, ERPNextError):
        return None


# ── bulk import ─────────────────────────────────────────────────────────────

def import_all_supported_accounts(*, client: ERPNextClient | None = None,
                                  plaid_client=None) -> dict:
    """Run the create flow for every unmapped supported account across all
    linked items. Already-mapped rows are silently skipped; unsupported rows are
    marked and counted. Returns aggregate stats plus a human summary."""
    client = client or get_client()
    # Bank Account Types + Bank Account Subtypes + custom fields, once for the batch.
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
