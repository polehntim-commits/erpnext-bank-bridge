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


if __name__ == '__main__':
    unittest.main()
