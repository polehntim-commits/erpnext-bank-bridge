# SPDX-License-Identifier: MIT
"""Finding and removing ERPNext Bank Accounts nothing is linked to (v0.4.15).

WHY THIS EXISTS. The v0.4.15 fingerprint (app/erpnext_accounts.py) stops a
re-link from creating duplicates, but it cannot clean up the ones earlier
versions already made. An operator who dry-ran the import against one Company,
then re-ran it against another, has Bank Accounts on their books that no Plaid
account points at and no import will ever adopt — the fingerprint deliberately
refuses to cross Company boundaries, so those records are inert forever.

This module lists exactly those records and offers to delete them. It is a
janitor, not a migration: nothing here runs automatically, and nothing here is
required for correctness.

WHAT COUNTS AS UNLINKED. A Bank Account is orphaned when no LIVE local
PlaidAccount claims it — by mapping pointer (`erpnext_bank_account_name`) or by
the `plaid_account_id` custom field. "Live" excludes accounts a v0.4.11 adoption
superseded and accounts whose Item the operator disconnected, which is the same
definition `erpnext_accounts._claimed_by_live_account` uses; the two must agree,
or this page would offer to delete a record the very next import would adopt.

WHY DELETION IS SAFE TO OFFER, AND WHERE IT STOPS. ERPNext refuses to delete a
Bank Account with linked documents, so a record that ever carried a transaction
fails the delete and is reported as such rather than silently mangled. That
refusal is the real guard rail — this module does not attempt to second-guess
it, and it never touches the GL Account behind the Bank Account, because that
leaf may carry ledger entries the Bank Account itself does not.

Deletion is idempotent: a record already gone reports 'gone', not an error, so
a double-submitted form is harmless.
"""
from __future__ import annotations

import logging

from . import audit
from .erpnext_accounts import (BANK_ACCOUNT_DT, _claimed_by_live_account,
                               get_client)
from .erpnext_client import ERPNextAPIError, ERPNextError
from .models import PlaidAccount

log = logging.getLogger('bankbridge.cleanup')

# What the cleanup list reads back per record.
CLEANUP_FIELDS = ['name', 'account_name', 'bank', 'company', 'last_4',
                  'account_subtype', 'account_type', 'plaid_account_id',
                  'is_company_account', 'modified']


class _Sentinel:
    """Stands in for 'no heir' when reusing `_claimed_by_live_account`, whose
    only use of the heir is to let an account claim its OWN record. Cleanup has
    no heir, so nothing should ever be excused on that basis."""
    account_id = '\x00-no-heir'


_SENTINEL = _Sentinel()


def _mapped_docnames() -> set:
    """Every ERPNext Bank Account docname a live local account points at.

    Read from the mapping pointer rather than the custom field because the
    pointer is the side this app actually syncs through — a record it names is
    in use even if the custom field on the ERPNext side was never provisioned."""
    rows = (PlaidAccount.query
            .filter(PlaidAccount.erpnext_bank_account_name.isnot(None),
                    PlaidAccount.superseded_by_account_id.is_(None))
            .all())
    return {(r.erpnext_bank_account_name or '').strip() for r in rows
            if (r.erpnext_bank_account_name or '').strip()}


def unlinked_bank_accounts(client=None) -> list[dict]:
    """Every Bank Account in ERPNext that no live Plaid account claims, newest
    first. Raises ERPNextError if ERPNext can't be reached — the page shows the
    error rather than an empty list, which would read as 'nothing to clean'."""
    client = client or get_client()
    rows = client.list_docs(BANK_ACCOUNT_DT, fields=CLEANUP_FIELDS) or []
    mapped = _mapped_docnames()
    out = []
    for row in rows:
        name = (row.get('name') or '').strip()
        if not name or name in mapped:
            continue
        # A record still claimed by a live account is in use even when the local
        # pointer has drifted — leave it alone.
        if _claimed_by_live_account(row.get('plaid_account_id') or '',
                                    _SENTINEL):
            continue
        out.append(dict(row))
    return sorted(out, key=lambda r: (str(r.get('modified') or ''),
                                      str(r.get('name') or '')), reverse=True)


def group_by_bank(rows: list[dict]) -> list[dict]:
    """The unlinked records grouped for display, banks in alphabetical order and
    records newest-first inside each. A blank `bank` groups under 'No bank' so a
    record with a broken link is still shown rather than dropped."""
    buckets: dict = {}
    for row in rows:
        buckets.setdefault((row.get('bank') or '').strip() or 'No bank',
                           []).append(row)
    return [{'bank': bank, 'accounts': buckets[bank], 'count': len(buckets[bank])}
            for bank in sorted(buckets)]


def delete_bank_account(docname: str, *, client=None) -> dict:
    """Delete one unlinked Bank Account. Returns
    {'name', 'status': deleted|gone|in_use|refused|linked, 'message'}.

    Re-checks that the record is still unlinked before deleting, because the
    page the operator submitted from may be minutes old and an import may have
    adopted the record since it rendered. Never raises: the caller renders a
    per-record outcome, and one stubborn record must not sink the rest."""
    docname = (docname or '').strip()
    if not docname:
        return {'name': docname, 'status': 'refused',
                'message': 'no record named'}
    client = client or get_client()
    if docname in _mapped_docnames():
        return {'name': docname, 'status': 'linked',
                'message': 'now mapped to a Plaid account — left alone'}
    try:
        existing = client.get_doc(BANK_ACCOUNT_DT, docname)
    except (ERPNextAPIError, ERPNextError):
        existing = None
    if existing is None:
        # Idempotent by design: a second submit of the same form is a no-op.
        return {'name': docname, 'status': 'gone',
                'message': 'already deleted'}
    if _claimed_by_live_account(existing.get('plaid_account_id') or '',
                                _SENTINEL):
        return {'name': docname, 'status': 'linked',
                'message': 'now claimed by a live Plaid account — left alone'}
    try:
        client.call_method('frappe.client.delete', http_method='POST',
                           json_body={'doctype': BANK_ACCOUNT_DT,
                                      'name': docname})
    except (ERPNextAPIError, ERPNextError) as e:
        # The expected failure: ERPNext refuses to delete a record other
        # documents link to. That is the guard rail working, so it is reported
        # as an outcome rather than an error.
        log.info('ERPNext refused to delete Bank Account %s: %s', docname,
                 str(e)[:200])
        return {'name': docname, 'status': 'in_use',
                'message': ('ERPNext would not delete this — it still has '
                            'linked documents. Cancel or remove those first.')}
    audit.record('erpnext_bank_account_deleted',
                 subject_type='BankAccount', subject_id=docname,
                 before={k: existing.get(k) for k in CLEANUP_FIELDS
                         if k in existing},
                 notes='deleted from the unlinked-accounts cleanup page')
    log.info('deleted unlinked ERPNext Bank Account %s', docname)
    return {'name': docname, 'status': 'deleted', 'message': 'deleted'}


def delete_many(docnames, *, client=None) -> dict:
    """Delete several unlinked Bank Accounts, one at a time. Returns
    {'results': [...], 'deleted': n, 'skipped': n} — the summary the page shows.

    One client is resolved for the whole batch so a 40-record clean-up is not 40
    fresh connections; a failure to build it is reported once, not per record."""
    names = [str(n).strip() for n in (docnames or []) if str(n).strip()]
    if not names:
        return {'results': [], 'deleted': 0, 'skipped': 0}
    client = client or get_client()
    results = [delete_bank_account(n, client=client) for n in names]
    deleted = sum(1 for r in results if r['status'] == 'deleted')
    return {'results': results, 'deleted': deleted,
            'skipped': len(results) - deleted}
