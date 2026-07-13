# SPDX-License-Identifier: MIT
"""One-click account import: Plaid account → ERPNext Bank + Bank Account.

  * per-account "Create in ERPNext": Bank + Bank Account created, mapped,
    sync_enabled, import_status stamped
  * Plaid subtype → ERPNext account_type inference (Current / Credit) + override
  * skip filter: loan / investment / 401k / mortgage etc. are unsupported
  * dedup: importing the same account twice makes no duplicate Bank Account,
    and two accounts at one institution share a single Bank
  * bulk import: 5 supported + 3 unsupported → 5 created, 3 skipped
  * custom-field bootstrap (plaid_account_id, last_4) is idempotent
  * the admin endpoints wire it all together

    cd erpnext-bank-bridge_v0.1.0
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import erpnext_accounts, erpnext_settings  # noqa: E402
from app.erpnext_client import ERPNextError  # noqa: E402
from app.models import PlaidAccount, PlaidItem, PlaidSyncLog  # noqa: E402

from tests.fakes import FakeERPClient  # noqa: E402


class ImportBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        # A configured ERPNext connection (so the endpoints act, not bail).
        erpnext_settings.save('http://erp.test', 'K', 'SECRET',
                              'Example Company LLC')

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _item(self, item_id='item-abc', institution='Wells Fargo',
              institution_id='ins_1'):
        it = PlaidItem(item_id=item_id,
                       access_token_encrypted=crypto.encrypt('access-x'),
                       institution_id=institution_id,
                       institution_name=institution, status='active')
        db.session.add(it)
        db.session.commit()
        return it

    def _account(self, account_id, subtype='checking', type_='depository',
                 mask='0000', name=None, item_id='item-abc'):
        a = PlaidAccount(account_id=account_id, item_id=item_id,
                         name=name or f'{subtype} {mask}', mask=mask,
                         type=type_, subtype=subtype, import_status='pending')
        db.session.add(a)
        db.session.commit()
        return a


class TestSupportFilter(ImportBase):
    def test_supported_subtypes(self):
        self._item()
        for st in ('checking', 'savings', 'cd', 'money market',
                   'cash management', 'paypal'):
            a = self._account(f'dep-{st}', subtype=st)
            self.assertTrue(erpnext_accounts.is_supported(a), st)
        for st in ('credit card', 'line of credit'):
            a = self._account(f'cred-{st}', subtype=st, type_='credit')
            self.assertTrue(erpnext_accounts.is_supported(a), st)

    def test_unsupported_types_and_subtypes(self):
        self._item()
        cases = [
            ('mortgage', 'loan'),
            ('student', 'loan'),
            ('401k', 'investment'),
            ('ira', 'investment'),
            ('roth', 'investment'),
            ('hsa', 'depository'),
            ('brokerage', 'brokerage'),
            ('', 'other'),
        ]
        for i, (st, ty) in enumerate(cases):
            a = self._account(f'un-{i}', subtype=st, type_=ty)
            self.assertFalse(erpnext_accounts.is_supported(a), f'{ty}/{st}')

    def test_account_type_inference(self):
        self._item()
        chk = self._account('a-chk', subtype='checking')
        cc = self._account('a-cc', subtype='credit card', type_='credit')
        self.assertEqual(erpnext_accounts.erpnext_account_type(chk), 'Current')
        self.assertEqual(erpnext_accounts.erpnext_account_type(cc), 'Credit')

    def test_account_type_override_wins(self):
        self._item()
        chk = self._account('a-chk', subtype='checking')
        self.app.config['ERPNEXT_DEFAULT_BANK_ACCOUNT_TYPE'] = 'Bank'
        self.assertEqual(erpnext_accounts.erpnext_account_type(chk), 'Bank')


class TestSingleImport(ImportBase):
    def test_create_flow_maps_and_enables(self):
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient()
        result = erpnext_accounts.import_plaid_account_to_erpnext(
            'acct-1', client=erp)

        self.assertEqual(result['status'], 'imported')
        self.assertTrue(result['created_account'])
        # Bank + Bank Account each created once.
        self.assertEqual(len(erp.creates_of('Bank')), 1)
        self.assertEqual(len(erp.creates_of('Bank Account')), 1)
        ba_doc = erp.creates_of('Bank Account')[0][2]
        self.assertEqual(ba_doc['account_name'], 'Wells Fargo Checking - 0000')
        self.assertEqual(ba_doc['bank'], 'Wells Fargo')
        self.assertEqual(ba_doc['account_type'], 'Current')
        self.assertEqual(ba_doc['is_company_account'], 1)
        self.assertEqual(ba_doc['company'], 'Example Company LLC')
        self.assertEqual(ba_doc['plaid_account_id'], 'acct-1')
        self.assertEqual(ba_doc['last_4'], '0000')

        a = PlaidAccount.query.filter_by(account_id='acct-1').first()
        self.assertEqual(a.erpnext_bank_account_name, result['bank_account'])
        self.assertTrue(a.sync_enabled)
        self.assertEqual(a.import_status, 'imported')

    def test_credit_card_maps_to_credit(self):
        self._item()
        self._account('acct-cc', subtype='credit card', type_='credit', mask='9999')
        erp = FakeERPClient()
        erpnext_accounts.import_plaid_account_to_erpnext('acct-cc', client=erp)
        ba_doc = erp.creates_of('Bank Account')[0][2]
        self.assertEqual(ba_doc['account_type'], 'Credit')

    def test_unsupported_marks_status_no_erpnext_writes(self):
        self._item()
        self._account('acct-mort', subtype='mortgage', type_='loan')
        erp = FakeERPClient()
        result = erpnext_accounts.import_plaid_account_to_erpnext(
            'acct-mort', client=erp)
        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(len(erp.creates_of('Bank Account')), 0)
        a = PlaidAccount.query.filter_by(account_id='acct-mort').first()
        self.assertEqual(a.import_status, 'unsupported')
        self.assertIsNone(a.erpnext_bank_account_name)

    def test_import_logs_synclog_row(self):
        self._item()
        self._account('acct-1')
        erpnext_accounts.import_plaid_account_to_erpnext(
            'acct-1', client=FakeERPClient())
        self.assertEqual(
            PlaidSyncLog.query.filter_by(direction='erpnext_account_import').count(), 1)


class TestDedup(ImportBase):
    def test_same_account_twice_no_duplicate(self):
        self._item()
        self._account('acct-1', subtype='checking')
        erp = FakeERPClient()
        first = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        # Second call: account already mapped → skipped, no new writes.
        second = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertEqual(second['status'], 'skipped')
        self.assertEqual(len(erp.creates_of('Bank Account')), 1)

    def test_dedup_when_mapping_lost(self):
        """Even if the local mapping pointer is cleared, the plaid_account_id
        custom-field dedup prevents a duplicate Bank Account."""
        self._item()
        self._account('acct-1', subtype='checking')
        erp = FakeERPClient()
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        a = PlaidAccount.query.filter_by(account_id='acct-1').first()
        a.erpnext_bank_account_name = None      # simulate lost pointer
        a.import_status = 'pending'
        db.session.commit()
        result = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertEqual(result['status'], 'imported')
        self.assertFalse(result['created_account'])   # found the existing one
        self.assertEqual(len(erp.creates_of('Bank Account')), 1)

    def test_two_accounts_share_one_bank(self):
        self._item(institution='Wells Fargo')
        self._account('acct-chk', subtype='checking', mask='1111')
        self._account('acct-sav', subtype='savings', mask='2222')
        erp = FakeERPClient()
        erpnext_accounts.import_plaid_account_to_erpnext('acct-chk', client=erp)
        erpnext_accounts.import_plaid_account_to_erpnext('acct-sav', client=erp)
        self.assertEqual(len(erp.creates_of('Bank')), 1)       # one Bank
        self.assertEqual(len(erp.creates_of('Bank Account')), 2)  # two accounts


class TestCustomFieldBootstrap(ImportBase):
    def test_bootstrap_idempotent(self):
        erp = FakeERPClient()
        erpnext_accounts.ensure_custom_fields(erp)
        erpnext_accounts.ensure_custom_fields(erp)   # second call: no-op
        created = [c for c in erp.calls
                   if c[0] == 'create_doc' and c[1] == 'Custom Field']
        # Exactly the two fields, created once each despite two calls.
        self.assertEqual(len(created), 2)
        names = {erp.created['Custom Field'][n]['fieldname']
                 for n in erp.created['Custom Field']}
        self.assertEqual(names, {'plaid_account_id', 'last_4'})


class TestBankAccountTypes(ImportBase):
    def _created_types(self, erp):
        return [c for c in erp.calls
                if c[0] == 'create_doc' and c[1] == 'Bank Account Type']

    def test_bank_account_types_created_when_missing(self):
        erp = FakeERPClient()   # neither type exists (GET → 404)
        erpnext_accounts.ensure_bank_account_types(erp)
        self.assertEqual(len(self._created_types(erp)), 2)
        names = {d['account_type'] for d in erp.created['Bank Account Type'].values()}
        self.assertEqual(names, {'Current', 'Credit'})

    def test_bank_account_types_skip_when_present(self):
        erp = FakeERPClient(existing_types={'Current', 'Credit'})
        erpnext_accounts.ensure_bank_account_types(erp)
        self.assertEqual(len(self._created_types(erp)), 0)

    def test_bank_account_types_partial(self):
        erp = FakeERPClient(existing_types={'Current'})   # only Credit missing
        erpnext_accounts.ensure_bank_account_types(erp)
        created = self._created_types(erp)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][2]['account_type'], 'Credit')

    def test_single_import_provisions_types(self):
        self._item()
        self._account('acct-1', subtype='checking')
        erp = FakeERPClient()
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertIn('Current', erp.created['Bank Account Type'])
        self.assertIn('Credit', erp.created['Bank Account Type'])


class TestAccountSubtypes(ImportBase):
    """Bug 3 — `Bank Account.account_subtype` is a Link on Tim's instance, so the
    subtype values we send must exist as Account Subtype records. Bootstrap
    provisions them idempotently, in Title Case."""

    def _created_subtypes(self, erp):
        return [c for c in erp.calls
                if c[0] == 'create_doc' and c[1] == 'Account Subtype']

    def test_account_subtypes_created_when_missing(self):
        erp = FakeERPClient()   # none exist (GET → 404)
        erpnext_accounts.ensure_account_subtypes(erp)
        created = self._created_subtypes(erp)
        self.assertEqual(len(created),
                         len(erpnext_accounts.DEFAULT_ACCOUNT_SUBTYPES))
        names = set(erp.created['Account Subtype'].keys())
        self.assertEqual(names, set(erpnext_accounts.DEFAULT_ACCOUNT_SUBTYPES))
        # Title Case, per Frappe docname convention.
        self.assertIn('Checking', names)
        self.assertIn('Credit Card', names)
        self.assertNotIn('checking', names)

    def test_account_subtypes_skip_when_present(self):
        erp = FakeERPClient(
            existing_subtypes=set(erpnext_accounts.DEFAULT_ACCOUNT_SUBTYPES))
        erpnext_accounts.ensure_account_subtypes(erp)
        self.assertEqual(len(self._created_subtypes(erp)), 0)

    def test_bootstrap_provisions_subtypes(self):
        self._item()
        self._account('acct-1', subtype='checking')
        erp = FakeERPClient()
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertIn('Checking', erp.created['Account Subtype'])
        self.assertIn('Savings', erp.created['Account Subtype'])
        # And the send-side value matches the provisioned docname (Title Case).
        ba_doc = erp.creates_of('Bank Account')[0][2]
        self.assertEqual(ba_doc['account_subtype'], 'Checking')


class TestBulkImport(ImportBase):
    def test_five_supported_three_unsupported(self):
        self._item()
        for i in range(5):
            self._account(f'sup-{i}', subtype='checking', mask=f'{i}{i}{i}{i}')
        self._account('un-0', subtype='mortgage', type_='loan')
        self._account('un-1', subtype='401k', type_='investment')
        self._account('un-2', subtype='brokerage', type_='brokerage')
        erp = FakeERPClient()
        stats = erpnext_accounts.import_all_supported_accounts(client=erp)
        self.assertEqual(stats['created'], 5)
        self.assertEqual(stats['unsupported'], 3)
        self.assertEqual(len(erp.creates_of('Bank Account')), 5)
        self.assertIn('Created 5/8', stats['summary'])
        self.assertIn('skipped 3 unsupported', stats['summary'])
        # All five supported now mapped + enabled.
        mapped = PlaidAccount.query.filter(
            PlaidAccount.erpnext_bank_account_name.isnot(None)).count()
        self.assertEqual(mapped, 5)

    def test_bulk_summary_includes_retried_count(self):
        self._item()
        self._account('acct-1', subtype='checking')
        self._account('acct-2', subtype='savings')
        # Both accounts hit the LinkValidationError, drop account_subtype, retry
        # and succeed → 2 created, 2 retried.
        erp = FakeERPClient(link_reject_fields={'account_subtype'})
        stats = erpnext_accounts.import_all_supported_accounts(client=erp)
        self.assertEqual(stats['created'], 2)
        self.assertEqual(stats['retried'], 2)
        self.assertEqual(stats['failed'], 0)
        self.assertIn('2 retried successfully', stats['summary'])

    def test_already_mapped_silently_skipped(self):
        self._item()
        a = self._account('acct-1', subtype='checking')
        a.erpnext_bank_account_name = 'Pre-existing - Bank'
        db.session.commit()
        self._account('acct-2', subtype='savings')
        erp = FakeERPClient()
        stats = erpnext_accounts.import_all_supported_accounts(client=erp)
        self.assertEqual(stats['created'], 1)          # only acct-2
        self.assertEqual(stats['skipped_mapped'], 1)   # acct-1 untouched
        self.assertEqual(len(erp.creates_of('Bank Account')), 1)


class TestEndpoints(ImportBase):
    def setUp(self):
        super().setUp()
        self.client = self.app.test_client()

    def test_create_endpoint(self):
        self._item()
        self._account('acct-1', subtype='checking')
        erp = FakeERPClient()
        with mock.patch('app.erpnext_accounts.get_client', return_value=erp):
            r = self.client.post('/admin/accounts/create',
                                 data={'account_id': 'acct-1'})
        self.assertEqual(r.status_code, 302)
        a = PlaidAccount.query.filter_by(account_id='acct-1').first()
        self.assertIsNotNone(a.erpnext_bank_account_name)
        self.assertTrue(a.sync_enabled)

    def test_import_all_endpoint_redirects_to_dashboard(self):
        self._item()
        self._account('acct-1', subtype='checking')
        self._account('acct-2', subtype='savings')
        erp = FakeERPClient()
        with mock.patch('app.erpnext_accounts.get_client', return_value=erp):
            r = self.client.post('/admin/accounts/import_all')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/admin?flash=', r.headers['Location'])
        self.assertIn('Imported+2+accounts', r.headers['Location'])

    def test_create_endpoint_requires_erpnext(self):
        # Wipe the ERPNext connection → endpoint should bail with a flash.
        erpnext_settings.save('', '', '', '')
        self._item()
        self._account('acct-1', subtype='checking')
        r = self.client.post('/admin/accounts/create',
                             data={'account_id': 'acct-1'})
        self.assertEqual(r.status_code, 302)
        a = PlaidAccount.query.filter_by(account_id='acct-1').first()
        self.assertIsNone(a.erpnext_bank_account_name)

    def test_accounts_page_shows_create_and_unsupported(self):
        self._item()
        self._account('acct-1', subtype='checking')
        self._account('acct-2', subtype='mortgage', type_='loan')
        # Don't hit the (unreachable) ERPNext for the dropdown's Bank Account
        # list — that's the transaction bridge's concern, not this page's logic.
        with mock.patch('app.erpnext_bank.list_bank_accounts', return_value=[]):
            r = self.client.get('/admin/accounts')
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('Create in ERPNext', body)
        self.assertIn('not supported', body)
        self.assertIn('Import all supported accounts', body)


class TestDefensiveFieldDrop(ImportBase):
    """Bug 1b — ERPNext rejects a preferred Bank Account field as unknown; the
    import retries once with it stripped instead of failing the whole batch."""

    def test_retry_drops_unknown_field_and_succeeds(self):
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        # ERPNext on this instance has no account_subtype field on Bank Account.
        erp = FakeERPClient(reject_fields={'account_subtype'})
        result = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)

        self.assertEqual(result['status'], 'imported')
        self.assertTrue(result['created_account'])
        # Two POSTs: the first (rejected) with the field, the retry without it.
        creates = erp.creates_of('Bank Account')
        self.assertEqual(len(creates), 2)
        self.assertIn('account_subtype', creates[0][2])
        self.assertNotIn('account_subtype', creates[1][2])
        # The retry decision was recorded to the sync log for traceability.
        retry_rows = PlaidSyncLog.query.filter_by(status='retry').all()
        self.assertEqual(len(retry_rows), 1)
        self.assertIn('account_subtype', retry_rows[0].error_message)

    def test_retry_drops_account_subtype_on_link_validation_error(self):
        """Bug 3 — ERPNext rejects account_subtype with a LinkValidationError
        ('Could not find Account Subtype: savings') because the link target is
        missing. The defensive path drops the field and retries once."""
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='savings', mask='0000')
        erp = FakeERPClient(link_reject_fields={'account_subtype'})
        result = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)

        self.assertEqual(result['status'], 'imported')
        self.assertTrue(result['created_account'])
        self.assertTrue(result['retried'])
        # Two POSTs: the first (rejected) with the field, the retry without it.
        creates = erp.creates_of('Bank Account')
        self.assertEqual(len(creates), 2)
        self.assertIn('account_subtype', creates[0][2])
        self.assertNotIn('account_subtype', creates[1][2])
        # The retry decision names the field and the LinkValidationError cause.
        retry_rows = PlaidSyncLog.query.filter_by(status='retry').all()
        self.assertEqual(len(retry_rows), 1)
        self.assertIn('account_subtype', retry_rows[0].error_message)
        self.assertIn('LinkValidationError', retry_rows[0].error_message)

    def test_could_not_find_matches_spaced_label(self):
        # The Link error names the Title Case label ('Account Subtype'), not the
        # snake_case fieldname — _unknown_fields_in must match the spaced form.
        doc = {'account_name': 'X', 'bank': 'B', 'account_type': 'Current',
               'account_subtype': 'Savings'}
        body = 'LinkValidationError: Could not find Account Subtype: Savings'
        self.assertEqual(
            erpnext_accounts._unknown_fields_in(body, doc), ['account_subtype'])

    def test_mandatory_error_is_not_retried(self):
        self._item()
        self._account('acct-1', subtype='checking')
        # A 417 mandatory-field error → surfaced, never retried.
        erp = FakeERPClient(bank_account_error=(
            417, '{"exception": "MandatoryError: company is mandatory"}'))
        stats = erpnext_accounts.import_all_supported_accounts(client=erp)
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['created'], 0)
        self.assertEqual(len(erp.creates_of('Bank Account')), 1)   # no retry

    def test_unknown_fields_never_drops_essential(self):
        doc = {'account_name': 'X', 'bank': 'B', 'account_type': 'Current',
               'account_subtype': 'checking'}
        body = 'ValidationError: account_name is not a valid field, account_subtype too'
        # account_name is essential → protected even though it's named.
        self.assertEqual(
            erpnext_accounts._unknown_fields_in(body, doc), ['account_subtype'])

    def test_unrelated_error_is_not_retried_and_logs_body(self):
        self._item()
        self._account('acct-1', subtype='checking')
        # A 417 whose body is NOT a field error → must surface, not retry.
        erp = FakeERPClient(bank_account_error=(
            417, '{"exception": "LinkValidationError: company is mandatory"}'))
        stats = erpnext_accounts.import_all_supported_accounts(client=erp)

        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['created'], 0)
        # Exactly one Bank Account POST — no retry.
        self.assertEqual(len(erp.creates_of('Bank Account')), 1)
        # The actual Frappe body reached the sync log (per-failure row).
        rows = PlaidSyncLog.query.filter_by(status='failed').all()
        self.assertTrue(rows)
        self.assertTrue(any('LinkValidationError' in (r.error_message or '')
                            for r in rows))


class TestMissingLinkedDoctypes(ImportBase):
    """Fix 4 — some ERPNext instances don't have (or have a broken) linked
    doctype. Tim's returns HTTP 500 'No module named …' for the Account Subtype
    probe. Bootstrap must survive it (log-warn, mark unavailable, don't raise),
    and the send-side must drop the dependent field so the import still runs."""

    def test_bootstrap_survives_missing_account_subtype_doctype(self):
        erp = FakeERPClient(missing_doctypes={'Account Subtype'})
        with self.assertLogs('bankbridge.erpnext.accounts', level='WARNING') as cm:
            status = erpnext_accounts.bootstrap(erp)   # must NOT raise
        self.assertFalse(status[erpnext_accounts.ACCOUNT_SUBTYPE_DT])
        self.assertTrue(status[erpnext_accounts.BANK_ACCOUNT_TYPE_DT])
        self.assertTrue(status['partial'])
        self.assertTrue(erpnext_accounts.is_doctype_unavailable('Account Subtype'))
        # Nothing provisioned for the missing doctype.
        self.assertEqual(len(erp.created['Account Subtype']), 0)
        self.assertTrue(any('Account Subtype doctype unavailable' in m
                            for m in cm.output))

    def test_send_side_drops_account_subtype_when_unavailable(self):
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(missing_doctypes={'Account Subtype'})
        result = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)

        self.assertEqual(result['status'], 'imported')
        self.assertTrue(result['created_account'])
        # A single, clean POST — the field was dropped up-front, not retried.
        creates = erp.creates_of('Bank Account')
        self.assertEqual(len(creates), 1)
        self.assertNotIn('account_subtype', creates[0][2])
        # The other fields still ride along.
        self.assertEqual(creates[0][2]['account_type'], 'Current')
        self.assertEqual(creates[0][2]['plaid_account_id'], 'acct-1')

    def test_bootstrap_survives_missing_bank_account_type_doctype(self):
        erp = FakeERPClient(missing_doctypes={'Bank Account Type'})
        status = erpnext_accounts.bootstrap(erp)   # must NOT raise
        self.assertFalse(status[erpnext_accounts.BANK_ACCOUNT_TYPE_DT])
        self.assertTrue(status[erpnext_accounts.ACCOUNT_SUBTYPE_DT])
        self.assertTrue(erpnext_accounts.is_doctype_unavailable('Bank Account Type'))
        self.assertEqual(len(erp.created['Bank Account Type']), 0)

    def test_send_side_drops_account_type_when_unavailable(self):
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(missing_doctypes={'Bank Account Type'})
        result = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)

        self.assertEqual(result['status'], 'imported')
        creates = erp.creates_of('Bank Account')
        self.assertEqual(len(creates), 1)
        # account_type dropped (its Bank Account Type doctype is unavailable) …
        self.assertNotIn('account_type', creates[0][2])
        # … and it's no longer treated as an essential (never-dropped) field.
        self.assertNotIn('account_type',
                         erpnext_accounts._essential_bank_account_fields())

    def test_admin_page_survives_bootstrap_failure(self):
        """The Accounts page must render even when ERPNext is broken and a
        bootstrap step failed — with the partial-bootstrap banner."""
        self._item()
        self._account('acct-1', subtype='checking')
        erp = FakeERPClient(missing_doctypes={'Account Subtype'})
        # An import triggers the partial bootstrap (marks Account Subtype
        # unavailable) but must not poison the session or raise.
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        client = self.app.test_client()
        # ERPNext is unreachable for the dropdown too — the page still loads.
        with mock.patch('app.erpnext_bank.list_bank_accounts',
                        side_effect=ERPNextError('ERPNext down')):
            r = client.get('/admin/accounts')
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('ERPNext bootstrap partially failed', body)
        self.assertIn('Account Subtype', body)

    def test_test_connection_reports_per_doctype_status(self):
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', 'Example Company LLC')
        erp = FakeERPClient(missing_doctypes={'Account Subtype'})
        client = self.app.test_client()
        with mock.patch('app.erpnext_bank.get_client', return_value=erp):
            r = client.post('/admin/erpnext_settings/test')
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('Bank Account Types: ready', body)
        self.assertIn('Account Subtypes: unavailable', body)
        self.assertIn('Custom fields: ready', body)


class TestSyncLogWrites(ImportBase):
    """Bug 2 — the account-import audit line must actually persist. The
    direction label 'erpnext_account_import' (22 chars) overflowed VARCHAR(20)
    on Postgres and silently killed the commit."""

    def test_direction_column_wide_enough(self):
        # ORM-level regression guard, engine-independent: the declared length
        # must fit the longest direction label we log.
        length = PlaidSyncLog.__table__.c.direction.type.length
        self.assertGreaterEqual(length, len('erpnext_account_import'))

    def test_item_id_nullable(self):
        self.assertTrue(PlaidSyncLog.__table__.c.item_id.nullable)

    def test_log_persists_long_error_message(self):
        big = 'E' * 600
        erpnext_accounts._log('item-abc', 0, 'failed', big)
        row = PlaidSyncLog.query.filter_by(direction='erpnext_account_import').first()
        self.assertIsNotNone(row)
        self.assertEqual(row.error_message, big)
        self.assertEqual(row.status, 'failed')

    def test_log_persists_empty_item_id(self):
        erpnext_accounts._log('', 3, 'success', 'batch done')
        row = PlaidSyncLog.query.filter_by(direction='erpnext_account_import').first()
        self.assertIsNotNone(row)
        self.assertEqual(row.item_id, '')
        self.assertEqual(row.count, 3)


class TestErrorBodyCapture(unittest.TestCase):
    """Bug 1a — a 4xx exception's str() carries the truncated response body so
    every place that logs/flashes it shows the real Frappe error."""

    def test_str_includes_truncated_body(self):
        from app.erpnext_client import ERPNextAPIError
        e = ERPNextAPIError('POST /api/resource/Bank Account -> 417',
                            status_code=417,
                            response_body='Not a valid field: account_subtype')
        s = str(e)
        self.assertIn('-> 417', s)
        self.assertIn('Not a valid field: account_subtype', s)

    def test_str_truncates_long_body(self):
        from app.erpnext_client import ERPNextAPIError
        e = ERPNextAPIError('boom', status_code=500, response_body='x' * 5000)
        # base + ': ' + first 500 chars of body.
        self.assertEqual(len(str(e)), len('boom') + 2 + ERPNextAPIError.BODY_SNIPPET)


if __name__ == '__main__':
    unittest.main()
