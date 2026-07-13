# SPDX-License-Identifier: MIT
"""Test doubles for the Plaid client and the ERPNext REST client, so the sync
engine can be exercised end-to-end without the Plaid SDK or a live ERPNext."""
import json


class FakePlaidClient:
    """Same method surface the sync engine calls. `pages` is the scripted
    /transactions/sync response sequence (re-arm between syncs by reassigning
    `self.pages`). `accounts` is what get_accounts returns."""
    def __init__(self, accounts=None, pages=None):
        self.accounts = accounts or []
        self.pages = list(pages or [])
        self.calls = []

    def create_link_token(self, user_id, redirect_uri=None, webhook=None):
        self.calls.append(('create_link_token', user_id))
        return 'link-sandbox-test-token'

    def exchange_public_token(self, public_token):
        self.calls.append(('exchange_public_token', public_token))
        return 'access-sandbox-abc', 'item-abc'

    def get_item(self, access_token):
        self.calls.append(('get_item', access_token))
        return {'item_id': 'item-abc', 'institution_id': 'ins_1'}

    def get_institution_name(self, institution_id):
        return 'Wells Fargo'

    def get_institution_details(self, institution_id):
        self.calls.append(('get_institution_details', institution_id))
        return {'name': 'Wells Fargo', 'url': 'https://wellsfargo.com'}

    def get_accounts(self, access_token):
        self.calls.append(('get_accounts', access_token))
        return list(self.accounts)

    def transactions_sync(self, access_token, cursor=None, count=500):
        self.calls.append(('transactions_sync', cursor))
        if self.pages:
            return self.pages.pop(0)
        return {'added': [], 'modified': [], 'removed': [],
                'next_cursor': cursor or 'cursor-0', 'has_more': False}


def page(added=None, modified=None, removed=None, next_cursor='cursor-1',
         has_more=False):
    """Build one normalized transactions_sync page."""
    return {
        'added': added or [], 'modified': modified or [],
        'removed': [{'transaction_id': t} if isinstance(t, str) else t
                    for t in (removed or [])],
        'next_cursor': next_cursor, 'has_more': has_more,
    }


def txn(transaction_id, account_id, amount, name='TXN', merchant_name='',
        date='2026-07-10', category='', pending=False, iso='USD'):
    return {
        'transaction_id': transaction_id, 'account_id': account_id,
        'amount': amount, 'iso_currency_code': iso, 'date': date,
        'name': name, 'merchant_name': merchant_name, 'category': category,
        'pending': pending,
    }


class FakeERPClient:
    """Programmable stand-in for ERPNextClient covering the surface
    erpnext_bank uses: list_docs, create_doc, call_method, get_logged_user.
    Tracks created docs, submits, and cancels; enforces reference_number
    idempotency the way the real Frappe list filter would."""
    def __init__(self, bank_accounts=None, fail_create=False,
                 fail_bank_account=False, existing_types=None):
        self.docs = {}          # name -> doc
        self.by_ref = {}        # reference_number -> name
        self.submitted = set()
        self.cancelled = set()
        self.calls = []
        self._counter = 0
        self.bank_accounts = bank_accounts or []   # preset dropdown list
        self.fail_create = fail_create             # fail Bank Transaction create
        self.fail_bank_account = fail_bank_account  # fail Bank Account create
        # Bank Account Type records that already exist in ERPNext (get_doc hits).
        self.existing_types = set(existing_types or ())
        # Records created by the one-click account import, keyed by doctype.
        self.created = {'Bank': {}, 'Bank Account': {}, 'Custom Field': {},
                        'Bank Account Type': {}}

    def get_logged_user(self):
        return 'admin@example.com'

    def get_doc(self, doctype, name):
        """Return a doc dict, or None on 'not found' (the real client's 404)."""
        self.calls.append(('get_doc', doctype, name))
        if doctype == 'Bank Account Type':
            if name in self.existing_types or name in self.created['Bank Account Type']:
                return {'name': name}
            return None
        return self.docs.get(name)

    @staticmethod
    def _matches(doc, filters):
        """True when doc satisfies every [field, '=', value] filter."""
        for f in (filters or []):
            field, op, value = f[0], f[1], f[2]
            if op == '=' and str(doc.get(field, '')) != str(value):
                return False
        return True

    def list_docs(self, doctype, filters=None, fields=None,
                  limit_page_length=0, order_by=None):
        self.calls.append(('list_docs', doctype, filters))
        if doctype == 'Bank Transaction' and filters:
            for f in filters:
                if f[0] == 'reference_number' and f[1] == '=':
                    name = self.by_ref.get(f[2])
                    # Mirror ERPNext's docstatus<2 filter: a cancelled doc
                    # keeps its reference_number but must not match.
                    if name and name in self.cancelled:
                        return []
                    return [{'name': name}] if name else []
            return []
        if doctype in ('Bank', 'Custom Field'):
            return [{'name': n} for n, d in self.created[doctype].items()
                    if self._matches(d, filters)]
        if doctype == 'Bank Account':
            # A plaid_account_id (or bank_account_no) filter is a dedup lookup
            # against created accounts; an unfiltered / disabled filter is the
            # mapping dropdown, which reads the preset list.
            if filters and any(f[0] in ('plaid_account_id', 'bank_account_no')
                               for f in filters):
                return [{'name': n} for n, d in self.created['Bank Account'].items()
                        if self._matches(d, filters)]
            return list(self.bank_accounts)
        return []

    def create_doc(self, doctype, doc):
        self.calls.append(('create_doc', doctype, doc))
        if doctype == 'Bank Transaction':
            if self.fail_create:
                from app.erpnext_client import ERPNextAPIError
                raise ERPNextAPIError('bad', status_code=417,
                                      response_body='{"exc":"ValidationError"}')
            self._counter += 1
            name = f'ACC-BTN-{self._counter:04d}'
            self.docs[name] = dict(doc)
            ref = doc.get('reference_number')
            if ref:
                self.by_ref[ref] = name
            return {'name': name}
        if doctype == 'Bank':
            name = doc.get('bank_name')       # ERPNext Bank autonames on bank_name
            self.created['Bank'][name] = dict(doc)
            return {'name': name}
        if doctype == 'Bank Account':
            if self.fail_bank_account:
                from app.erpnext_client import ERPNextAPIError
                raise ERPNextAPIError('bad', status_code=417,
                                      response_body='{"exc":"ValidationError"}')
            self._counter += 1
            name = f"{doc.get('account_name')} - {doc.get('bank')}"
            self.created['Bank Account'][name] = dict(doc)
            return {'name': name}
        if doctype == 'Bank Account Type':
            name = doc.get('account_type')   # autonames on account_type
            self.created['Bank Account Type'][name] = dict(doc)
            return {'name': name}
        if doctype == 'Custom Field':
            name = f"{doc.get('dt')}-{doc.get('fieldname')}"
            self.created['Custom Field'][name] = dict(doc)
            return {'name': name}
        # Unknown doctype — mimic a generic create.
        self._counter += 1
        name = f'DOC-{self._counter:04d}'
        return {'name': name}

    def call_method(self, method, params=None, http_method='GET', json_body=None):
        self.calls.append(('call_method', method, json_body))
        if method == 'frappe.client.submit':
            inner = json.loads((json_body or {}).get('doc', '{}'))
            if inner.get('name'):
                self.submitted.add(inner['name'])
        elif method == 'frappe.client.cancel':
            name = (json_body or {}).get('name')
            if name:
                self.cancelled.add(name)
        return {}

    # count helpers for assertions
    def creates_of(self, doctype='Bank Transaction'):
        return [c for c in self.calls
                if c[0] == 'create_doc' and c[1] == doctype]
