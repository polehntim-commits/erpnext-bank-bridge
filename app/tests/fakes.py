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
                 fail_bank_account=False, existing_types=None,
                 reject_fields=None, bank_account_error=None,
                 existing_subtypes=None, link_reject_fields=None,
                 missing_doctypes=None, company_account_mandatory=False,
                 chart_accounts=None, fail_account_create=False,
                 company_abbr='EC', existing_suppliers=None,
                 fail_supplier_create=False, fail_je_create=False,
                 companies=None, existing_supplier_groups=None,
                 fail_supplier_group_create=False, existing_customers=None,
                 fail_customer_create=False, existing_customer_groups=None,
                 fail_customer_group_create=False):
        self.docs = {}          # name -> doc
        self.by_ref = {}        # reference_number -> name
        self.submitted = set()
        self.cancelled = set()
        self.calls = []
        self._counter = 0
        self.bank_accounts = bank_accounts or []   # preset dropdown list
        self.fail_create = fail_create             # fail Bank Transaction create
        self.fail_bank_account = fail_bank_account  # fail Bank Account create
        # Fields ERPNext will reject as "not a valid field" on a Bank Account
        # create: any create whose doc still carries one of these raises a 417
        # with a Frappe-style body naming it. A retry with the field stripped
        # then succeeds — the defensive-drop path under test.
        self.reject_fields = set(reject_fields or ())
        # Fields ERPNext rejects with a *LinkValidationError* ("Could not find
        # <Label>: <value>") — the link target doesn't exist. The defensive path
        # must drop the field and retry, same as an unknown field.
        self.link_reject_fields = set(link_reject_fields or ())
        # Optional (status_code, body) for an *unrelated* Bank Account failure —
        # a rejection the defensive path must NOT retry.
        self.bank_account_error = bank_account_error
        # Doctypes this ERPNext doesn't have installed at all — the existence
        # probe (get_doc / list_docs) raises the exact 500 ImportError Frappe
        # returns for a genuinely-missing module, e.g.:
        #   {"exception":"Error: No module named
        #    'frappe.core.doctype.account_subtype'","exc_type":"ImportError"}
        self.missing_doctypes = set(missing_doctypes or ())
        # Mirror ERPNext instances that enforce a GL link on company Bank
        # Accounts: a create with is_company_account truthy and no `account`
        # link raises "Company Account is mandatory". A retry with
        # is_company_account=0 (personal) then succeeds — the fallback under test.
        self.company_account_mandatory = company_account_mandatory
        # Bank Account Type records that already exist in ERPNext (get_doc hits).
        self.existing_types = set(existing_types or ())
        # Bank Account Subtype records that already exist in ERPNext (get_doc hits).
        self.existing_subtypes = set(existing_subtypes or ())
        # Company abbreviation Frappe appends when autonaming an Account
        # ('<account_name> - <abbr>'). Also the abbr for preset chart names.
        self.company_abbr = company_abbr
        # A preset Chart of Accounts (v0.2.0 GL auto-create). Each entry is an
        # Account dict; account_name is required, is_group defaults to 0, and the
        # docname is autonamed unless given. A `company` of '' matches any
        # company filter (a wildcard, so tests don't have to restate it).
        self.chart_accounts = {}    # name -> Account doc
        for a in (chart_accounts or []):
            d = dict(a)
            d.setdefault('is_group', 0)
            d.setdefault('company', '')
            d.setdefault('disabled', 0)
            name = d.get('name') or f"{d['account_name']} - {company_abbr}"
            d['name'] = name
            self.chart_accounts[name] = d
        # When True, every Account create (group or leaf) fails — exercises the
        # graceful fall-through to the v0.1.5 personal-account path.
        self.fail_account_create = fail_account_create
        # Suppliers that already exist in ERPNext (list_docs by supplier_name).
        self.existing_suppliers = set(existing_suppliers or ())
        # Supplier Groups that already exist in ERPNext (get_doc hits). The
        # v0.4.0.7 derived-party auto-create find-or-creates its group first.
        self.existing_supplier_groups = set(existing_supplier_groups
                                            or ('All Supplier Groups',))
        # When True, every Supplier Group create fails — the auto-create must
        # then fall back to the configured default group, not lose the Supplier.
        self.fail_supplier_group_create = fail_supplier_group_create
        # When True, every Supplier create fails (both attempts) so the
        # best-effort resolve path leaves erpnext_supplier_name NULL.
        self.fail_supplier_create = fail_supplier_create
        # v0.4.0.8 · the AR-side mirrors of the four Supplier knobs above. The
        # sell-side auto-create (erpnext_bank.ensure_customer) walks the same
        # find-or-create path, so the fake models it the same way.
        self.existing_customers = set(existing_customers or ())
        self.existing_customer_groups = set(existing_customer_groups
                                            or ('All Customer Groups',))
        self.fail_customer_group_create = fail_customer_group_create
        self.fail_customer_create = fail_customer_create
        # When True, every Journal Entry create fails — exercises the
        # non-destructive `error` GeneratedJournalEntry path.
        self.fail_je_create = fail_je_create
        # ERPNext Company docnames for list_companies() (v0.4.0 multi-entity).
        self.companies = list(companies) if companies is not None else []
        # Records created by the one-click account import, keyed by doctype.
        self.created = {'Bank': {}, 'Bank Account': {}, 'Custom Field': {},
                        'Bank Account Type': {}, 'Bank Account Subtype': {},
                        'Account': {}, 'Supplier': {}, 'Journal Entry': {},
                        'Supplier Group': {}, 'Customer': {},
                        'Customer Group': {}}

    def get_logged_user(self):
        return 'admin@example.com'

    def _maybe_missing(self, doctype):
        """Raise the 500 ImportError Frappe returns when a doctype's Python
        module isn't installed — for doctypes flagged as missing on this fake."""
        if doctype in self.missing_doctypes:
            from app.erpnext_client import ERPNextAPIError
            module = doctype.lower().replace(' ', '_')
            raise ERPNextAPIError(
                f'GET /api/resource/{doctype} -> 500', status_code=500,
                response_body=('{"exception":"Error: No module named '
                               f"'frappe.core.doctype.{module}'\",\"exc_type\""
                               ':"ImportError"}'))

    def get_doc(self, doctype, name):
        """Return a doc dict, or None on 'not found' (the real client's 404)."""
        self.calls.append(('get_doc', doctype, name))
        self._maybe_missing(doctype)
        if doctype == 'Bank Account Type':
            if name in self.existing_types or name in self.created['Bank Account Type']:
                return {'name': name}
            return None
        if doctype == 'Bank Account Subtype':
            if name in self.existing_subtypes or name in self.created['Bank Account Subtype']:
                return {'name': name}
            return None
        if doctype == 'Supplier Group':
            if (name in self.existing_supplier_groups
                    or name in self.created['Supplier Group']):
                return {'name': name}
            return None
        if doctype == 'Customer Group':
            if (name in self.existing_customer_groups
                    or name in self.created['Customer Group']):
                return {'name': name}
            return None
        if doctype == 'Account':
            return ({**self.chart_accounts, **self.created['Account']}).get(name)
        if doctype == 'Bank Account':
            # v0.4.0 drift check reads a Bank Account's `company`; created
            # accounts live in self.created, keyed by '<account_name> - <bank>'.
            return self.created['Bank Account'].get(name)
        return self.docs.get(name)

    @staticmethod
    def _matches(doc, filters):
        """True when doc satisfies every [field, '=', value] filter."""
        for f in (filters or []):
            field, op, value = f[0], f[1], f[2]
            if op == '=' and str(doc.get(field, '')) != str(value):
                return False
        return True

    @staticmethod
    def _account_matches(doc, filters):
        """Like _matches, but a stored `company` of '' is a wildcard (a preset
        chart account matches any company filter) so tests need not restate it,
        and an unset `disabled` is treated as 0 (enabled) — mirroring ERPNext,
        so the v0.3.1 dropdown's `disabled=0` filter matches created leaves."""
        for f in (filters or []):
            field, op, value = f[0], f[1], f[2]
            if op != '=':
                continue
            if field == 'company' and not doc.get('company'):
                continue
            actual = doc.get(field)
            if field == 'disabled' and actual is None:
                actual = 0
            if str(actual if actual is not None else '') != str(value):
                return False
        return True

    def list_docs(self, doctype, filters=None, fields=None,
                  limit_page_length=0, order_by=None):
        self.calls.append(('list_docs', doctype, filters))
        self._maybe_missing(doctype)
        if doctype == 'Company':
            rows = [{'name': c} for c in self.companies]
            if order_by:
                rows = sorted(rows, key=lambda r: r['name'])
            return rows
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
        if doctype == 'Account':
            pool = {**self.chart_accounts, **self.created['Account']}
            flds = fields or ['name']
            return [{k: d.get(k) for k in flds} for d in pool.values()
                    if self._account_matches(d, filters)]
        if doctype == 'Supplier':
            # supplier_name filter → a dedup lookup against preset + created
            # Suppliers (autonamed on supplier_name).
            names = set(self.existing_suppliers) | set(self.created['Supplier'])
            for f in (filters or []):
                if f[0] == 'supplier_name' and f[1] == '=':
                    return [{'name': f[2]}] if f[2] in names else []
            return [{'name': n} for n in names]
        if doctype == 'Customer':
            # customer_name filter → the AR-side dedup lookup (autonamed on
            # customer_name, exactly like Supplier above).
            names = set(self.existing_customers) | set(self.created['Customer'])
            for f in (filters or []):
                if f[0] == 'customer_name' and f[1] == '=':
                    return [{'name': f[2]}] if f[2] in names else []
            return [{'name': n} for n in names]
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
        if doctype == 'Account':
            from app.erpnext_client import ERPNextAPIError
            if self.fail_account_create:
                raise ERPNextAPIError(
                    'bad', status_code=417,
                    response_body='{"exception": "ValidationError: cannot create Account"}')
            # Frappe autonames an Account '<account_name> - <company_abbr>'.
            name = f"{doc.get('account_name')} - {self.company_abbr}"
            self.created['Account'][name] = {**dict(doc), 'name': name}
            return {'name': name}
        if doctype == 'Bank Account':
            from app.erpnext_client import ERPNextAPIError
            if self.fail_bank_account:
                raise ERPNextAPIError('bad', status_code=417,
                                      response_body='{"exc":"ValidationError"}')
            if self.bank_account_error is not None:
                status, body = self.bank_account_error
                raise ERPNextAPIError('bad', status_code=status,
                                      response_body=body)
            if (self.company_account_mandatory
                    and doc.get('is_company_account') and not doc.get('account')):
                raise ERPNextAPIError(
                    'bad', status_code=417,
                    response_body=('{"exception": "ValidationError: Company '
                                   'Account is mandatory"}'))
            bad = next((f for f in doc if f in self.reject_fields), None)
            if bad is not None:
                raise ERPNextAPIError(
                    'bad', status_code=417,
                    response_body=('{"exception": "ValidationError: ' + bad +
                                   ' is not a valid field of Bank Account"}'))
            bad_link = next((f for f in doc if f in self.link_reject_fields), None)
            if bad_link is not None:
                # Frappe LinkValidationError uses the field's Title Case label,
                # not the snake_case fieldname: "Could not find Account Subtype:".
                label = bad_link.replace('_', ' ').title()
                raise ERPNextAPIError(
                    'bad', status_code=417,
                    response_body=('{"exception": "LinkValidationError: Could '
                                   'not find ' + label + ': ' +
                                   str(doc.get(bad_link)) + '"}'))
            self._counter += 1
            name = f"{doc.get('account_name')} - {doc.get('bank')}"
            self.created['Bank Account'][name] = dict(doc)
            return {'name': name}
        if doctype == 'Bank Account Type':
            name = doc.get('account_type')   # autonames on account_type
            self.created['Bank Account Type'][name] = dict(doc)
            return {'name': name}
        if doctype == 'Bank Account Subtype':
            name = doc.get('account_subtype')  # autonames on account_subtype
            self.created['Bank Account Subtype'][name] = dict(doc)
            return {'name': name}
        if doctype == 'Supplier Group':
            if self.fail_supplier_group_create:
                from app.erpnext_client import ERPNextAPIError
                raise ERPNextAPIError(
                    'bad', status_code=417,
                    response_body='{"exception": "ValidationError: cannot create '
                                  'Supplier Group"}')
            name = doc.get('supplier_group_name')  # autonames on the group name
            self.created['Supplier Group'][name] = dict(doc)
            return {'name': name}
        if doctype == 'Supplier':
            if self.fail_supplier_create:
                from app.erpnext_client import ERPNextAPIError
                raise ERPNextAPIError(
                    'bad', status_code=417,
                    response_body='{"exception": "LinkValidationError: Could not '
                                  'find Supplier Group"}')
            name = doc.get('supplier_name')   # ERPNext autonames on supplier_name
            self.created['Supplier'][name] = dict(doc)
            return {'name': name}
        if doctype == 'Customer Group':
            if self.fail_customer_group_create:
                from app.erpnext_client import ERPNextAPIError
                raise ERPNextAPIError(
                    'bad', status_code=417,
                    response_body='{"exception": "ValidationError: cannot create '
                                  'Customer Group"}')
            name = doc.get('customer_group_name')  # autonames on the group name
            self.created['Customer Group'][name] = dict(doc)
            return {'name': name}
        if doctype == 'Customer':
            if self.fail_customer_create:
                from app.erpnext_client import ERPNextAPIError
                raise ERPNextAPIError(
                    'bad', status_code=417,
                    response_body='{"exception": "LinkValidationError: Could not '
                                  'find Customer Group"}')
            name = doc.get('customer_name')   # ERPNext autonames on customer_name
            self.created['Customer'][name] = dict(doc)
            return {'name': name}
        if doctype == 'Journal Entry':
            if self.fail_je_create:
                from app.erpnext_client import ERPNextAPIError
                raise ERPNextAPIError('bad', status_code=417,
                                      response_body='{"exc":"ValidationError"}')
            self._counter += 1
            name = f'ACC-JV-{self._counter:04d}'
            # ONE dict behind both views, so a later update_doc / submit is
            # visible through get_doc too — there is only one document in a real
            # ERPNext, and the v0.4.0.8 backfill reads back what it just wrote.
            stored = dict(doc)
            self.created['Journal Entry'][name] = stored
            self.docs[name] = stored
            return {'name': name}
        if doctype == 'Custom Field':
            name = f"{doc.get('dt')}-{doc.get('fieldname')}"
            self.created['Custom Field'][name] = dict(doc)
            return {'name': name}
        # Unknown doctype — mimic a generic create.
        self._counter += 1
        name = f'DOC-{self._counter:04d}'
        return {'name': name}

    def update_doc(self, doctype, name, doc):
        """Merge fields into an existing doc (used by the v0.4.0 balance-only
        refresh, which PUTs `plaid_balance` onto a Bank Account)."""
        self.calls.append(('update_doc', doctype, name, doc))
        pool = self.created.get(doctype)
        if pool is not None and name in pool:
            pool[name].update(doc)
        elif name in self.docs:
            self.docs[name].update(doc)
        else:
            # Track the update even if we never saw the create (tests may map an
            # account to a Bank Account name without a prior create).
            self.created.setdefault(doctype, {})[name] = dict(doc)
        return {'name': name, **doc}

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


# ── FakeFrappe: a tiny stand-in for the `frappe` module used by the one-shot
# bench migration scripts (scripts/*.py), so their idempotency can be unit-tested
# without a live ERPNext. Backs Account + Bank Account by dicts and mimics the
# handful of frappe / frappe.db calls the scripts make.

class _FakeDoc:
    """A minimal frappe document: attribute get/set, .flags, .insert(), .save()."""

    def __init__(self, frappe, fields, existing_name=None):
        object.__setattr__(self, '_frappe', frappe)
        object.__setattr__(self, '_fields', dict(fields))

        class _Flags:
            pass
        object.__setattr__(self, 'flags', _Flags())
        object.__setattr__(self, 'name', existing_name)

    def __getattr__(self, k):
        return object.__getattribute__(self, '_fields').get(k)

    def __setattr__(self, k, v):
        if k in ('_frappe', '_fields', 'flags', 'name'):
            object.__setattr__(self, k, v)
        else:
            self._fields[k] = v

    def insert(self, ignore_permissions=False):
        name = f"{self._fields['account_name']} - {self._frappe.abbr}"
        object.__setattr__(self, 'name', name)
        self._fields['name'] = name
        self._frappe.accounts[name] = dict(self._fields)
        return self

    def save(self, ignore_permissions=False):
        self._frappe.accounts[self.name].update(self._fields)
        return self


class _FakeDB:
    def __init__(self, frappe):
        self._f = frappe

    def get_value(self, doctype, name_or_filters, field, as_dict=False):
        pool = self._f.accounts if doctype == 'Account' else {}
        row = None
        if isinstance(name_or_filters, dict):
            for a in pool.values():
                if self._f._match(a, name_or_filters):
                    row = a
                    break
        else:
            row = pool.get(name_or_filters)
        if row is None:
            return None
        if isinstance(field, (list, tuple)):
            if as_dict:
                return {f: row.get(f) for f in field}
            return [row.get(f) for f in field]
        return row.get(field)

    def set_value(self, doctype, name, field, value):
        if doctype == 'Account':
            self._f.accounts[name][field] = value

    def exists(self, doctype, name):
        if doctype == 'Account':
            return name in self._f.accounts
        return name in self._f.other_exists.get(doctype, set())

    def commit(self):
        pass

    def rollback(self):
        pass


class FakeFrappe:
    """Stand-in for the `frappe` module. `accounts` maps docname → Account dict;
    `bank_accounts` is a list of Bank Account dicts. `abbr` is the company suffix
    Frappe appends when autonaming a created Account."""

    def __init__(self, accounts=None, bank_accounts=None, companies=None,
                 abbr='TEST', other_exists=None):
        self.accounts = {n: {**dict(d), 'name': n}
                         for n, d in (accounts or {}).items()}
        self.bank_accounts = [dict(b) for b in (bank_accounts or [])]
        self.abbr = abbr
        self.other_exists = other_exists or {}
        if companies is not None:
            self._companies = list(companies)
        else:
            seen = []
            for a in self.accounts.values():
                c = a.get('company')
                if c and c not in seen:
                    seen.append(c)
            self._companies = seen or ['Testing']
        self.db = _FakeDB(self)

    @staticmethod
    def _match(row, filters):
        for k, v in (filters or {}).items():
            actual = row.get(k)
            if k == 'is_group':
                actual = 1 if actual else 0
                v = 1 if v else 0
            if isinstance(v, (list, tuple)) and v and v[0] == 'in':
                if actual not in v[1]:
                    return False
            elif str(actual if actual is not None else '') != str(v):
                return False
        return True

    def get_all(self, doctype, filters=None, fields=None, order_by=None):
        if doctype == 'Company':
            rows = [{'name': c} for c in self._companies]
            return rows
        if doctype == 'Bank Account':
            rows = [b for b in self.bank_accounts if self._match(b, filters)]
        elif doctype == 'Account':
            rows = [a for a in self.accounts.values() if self._match(a, filters)]
        else:
            rows = []
        if order_by:
            key = order_by.split()[0]
            rows = sorted(rows, key=lambda r: str(r.get(key) or ''))
        flds = fields or ['name']
        return [{f: r.get(f) for f in flds} for r in rows]

    def get_doc(self, arg, name=None):
        if isinstance(arg, dict):
            return _FakeDoc(self, arg)
        return _FakeDoc(self, dict(self.accounts[name]), existing_name=name)

    def destroy(self):
        pass
