# SPDX-License-Identifier: MIT
"""One-click account import: Plaid account → ERPNext Bank + Bank Account.

  * per-account "Create in ERPNext": Bank + Bank Account created, mapped,
    sync_enabled, import_status stamped
  * Plaid subtype → ERPNext account_type inference (Current / Credit) + override
  * skip filter: loan / mortgage / student / auto / other are unsupported
    (investment accounts are supported balance-only — see test_investments.py)
  * dedup: importing the same account twice makes no duplicate Bank Account,
    and two accounts at one institution share a single Bank
  * bulk import: 5 supported + 3 unsupported → 5 created, 3 skipped
  * custom-field bootstrap (plaid_account_id, last_4) is idempotent
  * the admin endpoints wire it all together

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import erpnext_accounts, erpnext_bank, erpnext_settings  # noqa: E402
from app.erpnext_client import ERPNextError  # noqa: E402
from app.models import PlaidAccount, PlaidItem, PlaidSyncLog  # noqa: E402

from tests.fakes import FakeERPClient, FakeFrappe  # noqa: E402


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
        # Only the catch-all 'other' is unsupported now: by definition Plaid
        # could not classify it, so there is no honest GL home for it.
        #
        # v0.4.14 moved LOANS out of this set — see test_loans.py. Their
        # exclusion was what left a mortgage off the balance sheet entirely.
        # (Investment accounts left in v0.4.0, balance-only.)
        self._item()
        a = self._account('un-other', subtype='', type_='other')
        self.assertFalse(erpnext_accounts.is_supported(a))

    def test_loans_are_supported_as_liabilities(self):
        self._item()
        for i, subtype in enumerate(('mortgage', 'student', 'auto')):
            a = self._account(f'loan-{i}', subtype=subtype, type_='loan')
            self.assertTrue(erpnext_accounts.is_supported(a), subtype)
            self.assertEqual(erpnext_accounts._gl_side(a), 'loan')

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
        # 'other' is the only unsupported type since v0.4.14; loans import as
        # liabilities now.
        self._item()
        self._account('acct-other', subtype='', type_='other')
        erp = FakeERPClient()
        result = erpnext_accounts.import_plaid_account_to_erpnext(
            'acct-other', client=erp)
        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(len(erp.creates_of('Bank Account')), 0)
        a = PlaidAccount.query.filter_by(account_id='acct-other').first()
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
        # Exactly the three fields, created once each despite two calls.
        self.assertEqual(len(created), 3)
        names = {erp.created['Custom Field'][n]['fieldname']
                 for n in erp.created['Custom Field']}
        self.assertEqual(names, {'plaid_account_id', 'last_4', 'plaid_balance'})


class TestBankAccountTypes(ImportBase):
    def _created_types(self, erp):
        return [c for c in erp.calls
                if c[0] == 'create_doc' and c[1] == 'Bank Account Type']

    # Derived from the constant rather than hardcoded: v0.4.12 added
    # 'Investment' (an investment account was typed 'Current' before it), and
    # these three tests are about the provisioning BEHAVIOUR — created when
    # missing, skipped when present, partial when some exist — not about how
    # many types there happen to be this release.
    ALL_TYPES = set(erpnext_accounts.DEFAULT_BANK_ACCOUNT_TYPES)

    def test_bank_account_types_created_when_missing(self):
        erp = FakeERPClient()   # none exist (GET → 404)
        erpnext_accounts.ensure_bank_account_types(erp)
        self.assertEqual(len(self._created_types(erp)), len(self.ALL_TYPES))
        names = {d['account_type'] for d in erp.created['Bank Account Type'].values()}
        self.assertEqual(names, self.ALL_TYPES)

    def test_bank_account_types_skip_when_present(self):
        erp = FakeERPClient(existing_types=set(self.ALL_TYPES))
        erpnext_accounts.ensure_bank_account_types(erp)
        self.assertEqual(len(self._created_types(erp)), 0)

    def test_bank_account_types_partial(self):
        missing = 'Credit'
        erp = FakeERPClient(existing_types=self.ALL_TYPES - {missing})
        erpnext_accounts.ensure_bank_account_types(erp)
        created = self._created_types(erp)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][2]['account_type'], missing)

    def test_single_import_provisions_types(self):
        self._item()
        self._account('acct-1', subtype='checking')
        erp = FakeERPClient()
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertIn('Current', erp.created['Bank Account Type'])
        self.assertIn('Credit', erp.created['Bank Account Type'])


class TestAccountSubtypes(ImportBase):
    """Bug 3 — `Bank Account.account_subtype` is a Link (ERPNext v15 options
    "Bank Account Subtype"), so the subtype values we send must exist as Bank
    Account Subtype records. Bootstrap provisions them idempotently, in Title
    Case."""

    def _created_subtypes(self, erp):
        return [c for c in erp.calls
                if c[0] == 'create_doc' and c[1] == 'Bank Account Subtype']

    def test_account_subtypes_created_when_missing(self):
        erp = FakeERPClient()   # none exist (GET → 404)
        erpnext_accounts.ensure_account_subtypes(erp)
        created = self._created_subtypes(erp)
        self.assertEqual(len(created),
                         len(erpnext_accounts.DEFAULT_ACCOUNT_SUBTYPES))
        names = set(erp.created['Bank Account Subtype'].keys())
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
        self.assertIn('Checking', erp.created['Bank Account Subtype'])
        self.assertIn('Savings', erp.created['Bank Account Subtype'])
        # And the send-side value matches the provisioned docname (Title Case).
        ba_doc = erp.creates_of('Bank Account')[0][2]
        self.assertEqual(ba_doc['account_subtype'], 'Checking')


class TestBulkImport(ImportBase):
    def test_five_supported_three_unsupported(self):
        self._item()
        for i in range(5):
            self._account(f'sup-{i}', subtype='checking', mask=f'{i}{i}{i}{i}')
        # Only 'other' is genuinely unsupported now. Investments went
        # balance-only in v0.4.0 and loans became liabilities in v0.4.14, so
        # neither belongs here any more.
        self._account('un-0', subtype='', type_='other')
        self._account('un-1', subtype='', type_='other')
        self._account('un-2', subtype='', type_='other')
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
        self._account('acct-2', subtype='', type_='other')
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

    def test_retry_drops_is_company_account_on_mandatory_error(self):
        """Fix 5 — a company Bank Account needs a linked GL account we can't
        supply, so ERPNext rejects it with 'Company Account is mandatory'. The
        defensive path retries once as a personal account (is_company_account=0)
        and succeeds."""
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(company_account_mandatory=True)
        result = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)

        self.assertEqual(result['status'], 'imported')
        self.assertTrue(result['created_account'])
        self.assertTrue(result['retried'])
        # Two POSTs: first as a company account (rejected), retry as personal.
        creates = erp.creates_of('Bank Account')
        self.assertEqual(len(creates), 2)
        self.assertEqual(creates[0][2]['is_company_account'], 1)
        self.assertEqual(creates[1][2]['is_company_account'], 0)
        self.assertNotIn('account', creates[1][2])

    def test_retry_logs_promote_to_company_hint(self):
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(company_account_mandatory=True)
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        retry_rows = PlaidSyncLog.query.filter_by(status='retry').all()
        self.assertEqual(len(retry_rows), 1)
        msg = retry_rows[0].error_message.lower()
        # A non-developer must understand what happened and what to do.
        self.assertIn('personal', msg)
        self.assertIn('promote', msg)
        self.assertIn('manually', msg)

    def test_is_company_account_config_false_skips_retry(self):
        """With ERPNEXT_DEFAULT_IS_COMPANY_ACCOUNT=False the first attempt
        already sends is_company_account=0, so the mandatory error never fires
        and there's no retry."""
        self.app.config['ERPNEXT_DEFAULT_IS_COMPANY_ACCOUNT'] = False
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(company_account_mandatory=True)
        result = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)

        self.assertEqual(result['status'], 'imported')
        self.assertFalse(result['retried'])
        creates = erp.creates_of('Bank Account')
        self.assertEqual(len(creates), 1)          # single clean POST, no retry
        self.assertEqual(creates[0][2]['is_company_account'], 0)

    def test_other_mandatory_errors_not_retried_by_this_path(self):
        """A different mandatory-field error ('Bank is mandatory') must NOT be
        caught by the is_company_account fallback — nothing to drop fixes it."""
        self._item()
        self._account('acct-1', subtype='checking')
        erp = FakeERPClient(bank_account_error=(
            417, '{"exception": "MandatoryError: Bank is mandatory"}'))
        stats = erpnext_accounts.import_all_supported_accounts(client=erp)
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['created'], 0)
        self.assertEqual(len(erp.creates_of('Bank Account')), 1)   # no retry

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
    """Fix 4 — some ERPNext instances genuinely don't have (or have a broken)
    linked doctype; the probe then returns HTTP 500 'No module named …'.
    Bootstrap must survive it (log-warn, mark unavailable, don't raise), and the
    send-side must drop the dependent field so the import still runs."""

    def test_bootstrap_survives_missing_account_subtype_doctype(self):
        erp = FakeERPClient(missing_doctypes={'Bank Account Subtype'})
        with self.assertLogs('bankbridge.erpnext.accounts', level='WARNING') as cm:
            status = erpnext_accounts.bootstrap(erp)   # must NOT raise
        self.assertFalse(status[erpnext_accounts.ACCOUNT_SUBTYPE_DT])
        self.assertTrue(status[erpnext_accounts.BANK_ACCOUNT_TYPE_DT])
        self.assertTrue(status['partial'])
        self.assertTrue(erpnext_accounts.is_doctype_unavailable('Bank Account Subtype'))
        # Nothing provisioned for the missing doctype.
        self.assertEqual(len(erp.created['Bank Account Subtype']), 0)
        self.assertTrue(any('Bank Account Subtype doctype unavailable' in m
                            for m in cm.output))

    def test_send_side_drops_account_subtype_when_unavailable(self):
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(missing_doctypes={'Bank Account Subtype'})
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
        erp = FakeERPClient(missing_doctypes={'Bank Account Subtype'})
        # An import triggers the partial bootstrap (marks Bank Account Subtype
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
        self.assertIn('Bank Account Subtype', body)

    def test_test_connection_reports_per_doctype_status(self):
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', 'Example Company LLC')
        erp = FakeERPClient(missing_doctypes={'Bank Account Subtype'})
        client = self.app.test_client()
        with mock.patch('app.erpnext_bank.get_client', return_value=erp):
            r = client.post('/admin/erpnext_settings/test')
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('Bank Account Types: ready', body)
        self.assertIn('Bank Account Subtypes: unavailable', body)
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

    def test_gl_account_column_present(self):
        # v0.2.0 additive column — the create_all-built test DB must have it.
        self.assertIn('erpnext_gl_account_name',
                      {c.name for c in PlaidAccount.__table__.c})

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


class TestGLAccountAutoCreate(ImportBase):
    """v0.2.0 — auto-create a matching GL Account in ERPNext's Chart of Accounts
    so an imported company Bank Account links a real `account` and keeps
    is_company_account = 1. Best-effort: it degrades to the v0.1.5 personal
    fallback if the Chart of Accounts can't be walked/created."""

    COMPANY = 'Example Company LLC'

    def _bank_group(self, **kw):
        d = {'account_name': 'Bank Accounts', 'is_group': 1, 'account_type': 'Bank'}
        d.update(kw)
        return d

    def _leaf_creates(self, erp):
        return [c for c in erp.creates_of('Account') if c[2].get('is_group') == 0]

    def test_finds_existing_bank_accounts_group(self):
        erp = FakeERPClient(chart_accounts=[self._bank_group()])
        group = erpnext_accounts.ensure_bank_account_group(erp, self.COMPANY)
        self.assertEqual(group, 'Bank Accounts - EC')
        # Reused the existing group — no Account create.
        self.assertEqual(len(erp.creates_of('Account')), 0)

    def test_creates_bank_accounts_group_when_missing(self):
        # Only the Asset root exists → walk up, creating Current Assets AND the
        # Bank Accounts group under it.
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Application of Funds (Assets)', 'is_group': 1,
             'root_type': 'Asset', 'parent_account': ''}])
        group = erpnext_accounts.ensure_bank_account_group(erp, self.COMPANY)
        self.assertEqual(group, 'Bank Accounts - EC')
        created = [c[2]['account_name'] for c in erp.creates_of('Account')]
        self.assertIn('Current Assets', created)
        self.assertIn('Bank Accounts', created)
        ba = next(c[2] for c in erp.creates_of('Account')
                  if c[2]['account_name'] == 'Bank Accounts')
        # The group is created under the freshly made Current Assets, typed Bank.
        self.assertEqual(ba['parent_account'], 'Current Assets - EC')
        self.assertEqual(ba['account_type'], 'Bank')
        self.assertEqual(ba['is_group'], 1)

    def test_creates_gl_account_for_new_bank_account(self):
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Current Assets', 'is_group': 1, 'root_type': 'Asset'}])
        result = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)

        self.assertEqual(result['status'], 'imported')
        # A Bank-typed leaf GL Account was created, matching the Bank Account name.
        leaves = self._leaf_creates(erp)
        self.assertEqual(len(leaves), 1)
        gl = leaves[0][2]
        self.assertEqual(gl['account_name'], 'Wells Fargo Checking - 0000')
        self.assertEqual(gl['account_type'], 'Bank')
        self.assertEqual(gl['account_currency'], 'USD')
        self.assertEqual(gl['parent_account'], 'Bank Accounts - EC')
        # The Bank Account links it and stays a company account.
        ba_doc = erp.creates_of('Bank Account')[0][2]
        self.assertEqual(ba_doc['account'], 'Wells Fargo Checking - 0000 - EC')
        self.assertEqual(ba_doc['is_company_account'], 1)
        # …and it's persisted back onto the Plaid account.
        a = PlaidAccount.query.filter_by(account_id='acct-1').first()
        self.assertEqual(a.erpnext_gl_account_name, 'Wells Fargo Checking - 0000 - EC')

    def test_reuses_existing_gl_account(self):
        self._item(institution='Wells Fargo')
        a = self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(chart_accounts=[self._bank_group()])
        group = erpnext_accounts.ensure_bank_account_group(erp, self.COMPANY)
        name1 = erpnext_accounts.find_or_create_gl_account_for(
            erp, a, group, self.COMPANY, 'Wells Fargo Checking - 0000')
        # Second call finds the leaf created by the first — no duplicate.
        name2 = erpnext_accounts.find_or_create_gl_account_for(
            erp, a, group, self.COMPANY, 'Wells Fargo Checking - 0000')
        self.assertEqual(name1, name2)
        self.assertEqual(len(self._leaf_creates(erp)), 1)

    def test_falls_back_to_personal_when_gl_creation_fails(self):
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        # The Bank Accounts group exists, but every Account create fails AND the
        # instance enforces a GL link on company accounts → the import must
        # degrade to the v0.1.5 personal-account retry and still succeed.
        erp = FakeERPClient(chart_accounts=[self._bank_group()],
                            fail_account_create=True,
                            company_account_mandatory=True)
        result = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)

        self.assertEqual(result['status'], 'imported')
        self.assertTrue(result['retried'])
        creates = erp.creates_of('Bank Account')
        self.assertEqual(len(creates), 2)                 # company → personal retry
        self.assertEqual(creates[0][2]['is_company_account'], 1)
        self.assertEqual(creates[1][2]['is_company_account'], 0)
        self.assertNotIn('account', creates[1][2])
        # No GL link recorded — the fallback created a personal account.
        a = PlaidAccount.query.filter_by(account_id='acct-1').first()
        self.assertIsNone(a.erpnext_gl_account_name)

    def test_uses_config_bank_group_name_override(self):
        self.app.config['ERPNEXT_BANK_ACCOUNT_GROUP_NAME'] = 'Cash and Bank'
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Cash and Bank', 'is_group': 1, 'account_type': 'Bank'}])
        group = erpnext_accounts.ensure_bank_account_group(erp, self.COMPANY)
        self.assertEqual(group, 'Cash and Bank - EC')
        self.assertEqual(len(erp.creates_of('Account')), 0)

    def test_currency_from_plaid_account(self):
        self._item(institution='RBC')
        a = self._account('acct-cad', subtype='checking', mask='7777')
        a.iso_currency_code = 'CAD'
        db.session.commit()
        erp = FakeERPClient(chart_accounts=[self._bank_group()])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-cad', client=erp)
        gl = self._leaf_creates(erp)[0][2]
        self.assertEqual(gl['account_currency'], 'CAD')

    def test_no_chart_of_accounts_falls_back_gracefully(self):
        """Fresh install: no Bank group, no Current Assets, no Asset root → the
        group resolver returns None and the import lands on the personal path
        (with the instance enforcing a company GL link)."""
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(company_account_mandatory=True)   # empty chart
        result = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertEqual(result['status'], 'imported')
        self.assertTrue(result['retried'])
        self.assertEqual(len(erp.creates_of('Account')), 0)   # nothing created
        creates = erp.creates_of('Bank Account')
        self.assertEqual(creates[-1][2]['is_company_account'], 0)


class TestRuleDropdownAccountList(ImportBase):
    """v0.3.1 · the rule debit/credit datalist (erpnext_bank.list_accounts) must
    return EVERY enabled leaf account — Bank, Cash, Expense, Income — including
    the auto-created Bank Accounts under the '1200' group, scoped to the default
    company. Groups (is_group=1) and disabled accounts are excluded."""

    COMPANY = 'Example Company LLC'

    def _chart(self):
        return [
            {'account_name': 'Bank Accounts', 'is_group': 1, 'account_type': 'Bank',
             'account_number': '1200', 'company': self.COMPANY},
            {'account_name': 'Red Plaidypus Bank Checking', 'is_group': 0,
             'account_type': 'Bank', 'account_number': '1201',
             'parent_account': 'Bank Accounts - EC', 'company': self.COMPANY},
            {'account_name': 'Cash', 'is_group': 0, 'account_type': 'Cash',
             'company': self.COMPANY},
            {'account_name': 'Debtors', 'is_group': 0, 'account_type': 'Receivable',
             'company': self.COMPANY},
            {'account_name': 'Fuel Expense', 'is_group': 0, 'account_type': 'Expense',
             'company': self.COMPANY},
            {'account_name': 'Old Closed Acct', 'is_group': 0, 'account_type': 'Bank',
             'disabled': 1, 'company': self.COMPANY},
        ]

    def test_dropdown_includes_bank_and_all_leaf_types(self):
        erp = FakeERPClient(chart_accounts=self._chart())
        names = {a['name'] for a in erpnext_bank.list_accounts(client=erp)}
        # Auto-created Bank leaf shows up alongside cash/debtors/expense.
        self.assertIn('Red Plaidypus Bank Checking - EC', names)
        self.assertIn('Cash - EC', names)
        self.assertIn('Debtors - EC', names)
        self.assertIn('Fuel Expense - EC', names)

    def test_dropdown_excludes_groups_and_disabled(self):
        erp = FakeERPClient(chart_accounts=self._chart())
        names = {a['name'] for a in erpnext_bank.list_accounts(client=erp)}
        self.assertNotIn('Bank Accounts - EC', names)     # is_group=1
        self.assertNotIn('Old Closed Acct - EC', names)   # disabled=1

    def test_dropdown_filters_are_scoped(self):
        # The GET carries is_group=0, disabled=0, and the default-company scope,
        # but NOT any account_type/root_type restriction.
        erp = FakeERPClient(chart_accounts=self._chart())
        erpnext_bank.list_accounts(client=erp)
        call = next(c for c in erp.calls
                    if c[0] == 'list_docs' and c[1] == 'Account')
        filters = {f[0]: f[2] for f in call[2]}
        self.assertEqual(filters['is_group'], 0)
        self.assertEqual(filters['disabled'], 0)
        self.assertEqual(filters['company'], self.COMPANY)
        self.assertNotIn('account_type', filters)
        self.assertNotIn('root_type', filters)


class TestChildAccountNumbering(ImportBase):
    """v0.3.9 · leaves under a numbered parent group get an account_number
    slotted into their liquidity band (most-liquid lowest): under group 1200,
    Cash Management → 1201, Checking → 1211, Savings → 1221, CD → 1231. A
    number-less parent skips numbering entirely (no worse than before)."""

    COMPANY = 'Example Company LLC'

    def _numbered_group(self, number='1200', **kw):
        d = {'account_name': 'Bank Accounts', 'is_group': 1,
             'account_type': 'Bank', 'account_number': number}
        d.update(kw)
        return d

    def _leaf(self, erp):
        return next(c[2] for c in erp.creates_of('Account')
                    if c[2].get('is_group') == 0)

    def _leaf_numbers(self, erp):
        return {c[2]['account_name']: c[2].get('account_number')
                for c in erp.creates_of('Account') if c[2].get('is_group') == 0}

    def test_checking_takes_its_liquidity_band(self):
        # Checking is rank 2 → band 1211-1220; a lone checking → 1211 (1201-1210
        # is reserved for cash-equivalents).
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(chart_accounts=[self._numbered_group('1200')])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertEqual(self._leaf(erp)['account_number'], '1211')

    def test_cash_management_takes_lowest_band(self):
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='cash management', mask='0000')
        erp = FakeERPClient(chart_accounts=[self._numbered_group('1200')])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertEqual(self._leaf(erp)['account_number'], '1201')

    def test_second_account_in_band_increments_within_band(self):
        # An existing checking at 1211 → a new checking takes the next slot in the
        # checking band, 1212 (not appended at the bottom of the group).
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='9999')
        erp = FakeERPClient(chart_accounts=[
            self._numbered_group('1200'),
            {'account_name': 'Existing Checking', 'is_group': 0,
             'account_type': 'Bank', 'account_number': '1211',
             'parent_account': 'Bank Accounts - EC'}])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertEqual(self._leaf(erp)['account_number'], '1212')

    def test_liquidity_ordering_cash_checking_savings_cd(self):
        # The headline invariant: Cash Management < Checking < Savings < CD.
        self._item(institution='Wells Fargo')
        self._account('a-cd', subtype='cd', mask='4000')
        self._account('a-sav', subtype='savings', mask='3000')
        self._account('a-cm', subtype='cash management', mask='1000')
        self._account('a-chk', subtype='checking', mask='2000')
        erp = FakeERPClient(chart_accounts=[self._numbered_group('1200')])
        # Import in a deliberately non-liquidity order — placement is by rank.
        for aid in ('a-cd', 'a-sav', 'a-cm', 'a-chk'):
            erpnext_accounts.import_plaid_account_to_erpnext(aid, client=erp)
        nums = self._leaf_numbers(erp)
        cm = int(nums['Wells Fargo Cash Management - 1000'])
        chk = int(nums['Wells Fargo Checking - 2000'])
        sav = int(nums['Wells Fargo Savings - 3000'])
        cd = int(nums['Wells Fargo Cd - 4000'])
        self.assertLess(cm, chk)
        self.assertLess(chk, sav)
        self.assertLess(sav, cd)
        self.assertEqual((cm, chk, sav, cd), (1201, 1211, 1221, 1231))

    def test_money_market_sorts_after_savings_same_band(self):
        # Savings and Money Market share rank 3; Savings takes 1221, Money Market
        # the next band slot 1222 (after Savings, though it sorts first by name).
        self._item(institution='Wells Fargo')
        self._account('a-sav', subtype='savings', mask='3000')
        self._account('a-mm', subtype='money market', mask='4000')
        erp = FakeERPClient(chart_accounts=[self._numbered_group('1200')])
        erpnext_accounts.import_plaid_account_to_erpnext('a-sav', client=erp)
        erpnext_accounts.import_plaid_account_to_erpnext('a-mm', client=erp)
        nums = self._leaf_numbers(erp)
        self.assertEqual(nums['Wells Fargo Savings - 3000'], '1221')
        self.assertEqual(nums['Wells Fargo Money Market - 4000'], '1222')

    def test_unnumbered_parent_skips_numbering(self):
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Bank Accounts', 'is_group': 1,
             'account_type': 'Bank'}])   # no account_number
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertNotIn('account_number', self._leaf(erp))

    def test_numbering_writes_audit_event(self):
        from app.models import AuditEvent
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(chart_accounts=[self._numbered_group('1200')])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        ev = AuditEvent.query.filter_by(
            event_type='gl_account_number_assigned').first()
        self.assertIsNotNone(ev)
        self.assertIn('1211', ev.notes)

    def test_liquidity_rank_map(self):
        self._item()
        ranks = {
            'cash management': 1, 'paypal': 1, 'checking': 2, 'savings': 3,
            'money market': 3, 'cd': 4,
        }
        for st, r in ranks.items():
            a = self._account(f'r-{st}', subtype=st)
            self.assertEqual(erpnext_accounts.liquidity_rank(a), r, st)
        # Credit side shares the rank map (disjoint keys, never same group).
        cc = self._account('r-cc', subtype='credit card', type_='credit')
        loc = self._account('r-loc', subtype='line of credit', type_='credit')
        self.assertEqual(erpnext_accounts.liquidity_rank(cc), 1)
        self.assertEqual(erpnext_accounts.liquidity_rank(loc), 2)
        # Unmapped → last.
        u = self._account('r-x', subtype='prepaid')
        self.assertEqual(erpnext_accounts.liquidity_rank(u), 99)


class TestFuzzyMatchReuse(ImportBase):
    """v0.3.1 · before creating a GL Account, reuse a close-enough existing leaf
    (stdlib difflib similarity + last-4 mask signal) instead of a near-dup."""

    COMPANY = 'Example Company LLC'

    def _leaf_creates(self, erp):
        return [c for c in erp.creates_of('Account') if c[2].get('is_group') == 0]

    def test_high_similarity_reuses_existing(self):
        # Existing 'Wells Fargo Checking' (no mask) ~ intended 'Wells Fargo
        # Checking - 0000' → reuse, no new leaf created.
        self._item(institution='Wells Fargo')
        a = self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Bank Accounts', 'is_group': 1, 'account_type': 'Bank'},
            {'account_name': 'Wells Fargo Checking', 'is_group': 0,
             'account_type': 'Bank', 'parent_account': 'Bank Accounts - EC'}])
        result = erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(len(self._leaf_creates(erp)), 0)   # reused, not created
        a = PlaidAccount.query.filter_by(account_id='acct-1').first()
        self.assertEqual(a.erpnext_gl_account_name, 'Wells Fargo Checking - EC')

    def test_low_similarity_creates_new(self):
        # Existing 'Red Plaidypus Bank Checking' is NOT similar enough to the
        # intended 'Wells Fargo Checking - 0000' → a fresh leaf is created.
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Bank Accounts', 'is_group': 1, 'account_type': 'Bank'},
            {'account_name': 'Red Plaidypus Bank Checking', 'is_group': 0,
             'account_type': 'Bank', 'parent_account': 'Bank Accounts - EC'}])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        leaves = self._leaf_creates(erp)
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0][2]['account_name'], 'Wells Fargo Checking - 0000')

    def test_threshold_override_forces_create(self):
        # Same close pair, but a threshold of 99 makes it fall below the bar →
        # create new instead of reuse.
        self.app.config['ERPNEXT_FUZZY_MATCH_THRESHOLD'] = 99
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Bank Accounts', 'is_group': 1, 'account_type': 'Bank'},
            {'account_name': 'Wells Fargo Checkings', 'is_group': 0,
             'account_type': 'Bank', 'parent_account': 'Bank Accounts - EC'}])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertEqual(len(self._leaf_creates(erp)), 1)

    def test_reuse_writes_audit_event(self):
        from app.models import AuditEvent
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Bank Accounts', 'is_group': 1, 'account_type': 'Bank'},
            {'account_name': 'Wells Fargo Checking', 'is_group': 0,
             'account_type': 'Bank', 'parent_account': 'Bank Accounts - EC'}])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        ev = AuditEvent.query.filter_by(event_type='fuzzy_match_found').first()
        self.assertIsNotNone(ev)

    def test_mask_signal_matches_when_last4_shared(self):
        # Base names only ~partially similar, but the shared last-4 '4242' in the
        # candidate name tips it into a reuse.
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='4242')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Bank Accounts', 'is_group': 1, 'account_type': 'Bank'},
            {'account_name': 'Wells Fargo Checking Acct 4242', 'is_group': 0,
             'account_type': 'Bank', 'parent_account': 'Bank Accounts - EC'}])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-1', client=erp)
        self.assertEqual(len(self._leaf_creates(erp)), 0)

    def test_skip_fuzzy_creates_new_despite_match(self):
        self._item(institution='Wells Fargo')
        a = self._account('acct-1', subtype='checking', mask='0000')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Bank Accounts', 'is_group': 1, 'account_type': 'Bank'},
            {'account_name': 'Wells Fargo Checking', 'is_group': 0,
             'account_type': 'Bank', 'parent_account': 'Bank Accounts - EC'}])
        erpnext_accounts.import_plaid_account_to_erpnext(
            'acct-1', client=erp, fuzzy_decision='create_new')
        self.assertEqual(len(self._leaf_creates(erp)), 1)


class TestFuzzyProbeAndModal(ImportBase):
    """v0.3.1 · the /admin/accounts/create modal flow: probe surfaces a
    candidate; Reuse takes it, Create-new-anyway skips dedup."""

    def setUp(self):
        super().setUp()
        self.client = self.app.test_client()
        self._item(institution='Wells Fargo')
        self._account('acct-1', subtype='checking', mask='0000')

    def _erp_with_candidate(self):
        return FakeERPClient(chart_accounts=[
            {'account_name': 'Bank Accounts', 'is_group': 1, 'account_type': 'Bank'},
            {'account_name': 'Wells Fargo Checking', 'is_group': 0,
             'account_type': 'Bank', 'parent_account': 'Bank Accounts - EC'}])

    def test_probe_returns_candidate(self):
        erp = self._erp_with_candidate()
        cand = erpnext_accounts.probe_fuzzy_gl_match('acct-1', client=erp)
        self.assertIsNotNone(cand)
        self.assertEqual(cand['account_name'], 'Wells Fargo Checking')
        self.assertGreaterEqual(cand['score'], 85)

    def test_probe_none_when_no_similar_account(self):
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Bank Accounts', 'is_group': 1, 'account_type': 'Bank'}])
        self.assertIsNone(erpnext_accounts.probe_fuzzy_gl_match('acct-1', client=erp))

    def test_create_endpoint_shows_modal_on_match(self):
        erp = self._erp_with_candidate()
        # Patching get_client covers both the probe and (would-be) import.
        with mock.patch('app.erpnext_accounts.get_client', return_value=erp):
            resp = self.client.post('/admin/accounts/create',
                                    data={'account_id': 'acct-1'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Possible duplicate account', resp.data)
        self.assertIn(b'Wells Fargo Checking', resp.data)
        # No import happened yet — still unmapped.
        self.assertIsNone(
            PlaidAccount.query.filter_by(account_id='acct-1').first()
            .erpnext_bank_account_name)

    def test_create_new_anyway_skips_dedup_and_logs_rejection(self):
        from app.models import AuditEvent
        erp = self._erp_with_candidate()
        with mock.patch.object(erpnext_accounts, 'get_client', return_value=erp):
            resp = self.client.post('/admin/accounts/create',
                                    data={'account_id': 'acct-1',
                                          'fuzzy_decision': 'create_new',
                                          'fuzzy_candidate': 'Wells Fargo Checking - EC'})
        self.assertEqual(resp.status_code, 302)
        leaves = [c for c in erp.creates_of('Account') if c[2].get('is_group') == 0]
        self.assertEqual(len(leaves), 1)   # a brand-new leaf, dedup skipped
        self.assertIsNotNone(AuditEvent.query.filter_by(
            event_type='fuzzy_match_rejected_by_user').first())

    def test_reuse_maps_to_existing(self):
        erp = self._erp_with_candidate()
        with mock.patch.object(erpnext_accounts, 'get_client', return_value=erp):
            resp = self.client.post('/admin/accounts/create',
                                    data={'account_id': 'acct-1',
                                          'fuzzy_decision': 'reuse',
                                          'fuzzy_candidate': 'Wells Fargo Checking - EC'})
        self.assertEqual(resp.status_code, 302)
        leaves = [c for c in erp.creates_of('Account') if c[2].get('is_group') == 0]
        self.assertEqual(len(leaves), 0)   # reused, nothing created
        a = PlaidAccount.query.filter_by(account_id='acct-1').first()
        self.assertEqual(a.erpnext_gl_account_name, 'Wells Fargo Checking - EC')


class TestGLParentByType(ImportBase):
    """v0.3.9 · the GL leaf's parent group is chosen by Plaid type — depository
    stays on the Assets side (Bank Accounts), credit lands under Current
    Liabilities → Credit Cards, loan under Loans. The leaf's account_type is
    always 'Bank' so Bank Reconciliation still works on it."""

    COMPANY = 'Example Company LLC'

    def _leaf_creates(self, erp):
        return [c for c in erp.creates_of('Account') if c[2].get('is_group') == 0]

    def _group_create(self, erp, account_name):
        return next((c[2] for c in erp.creates_of('Account')
                     if c[2].get('is_group') == 1
                     and c[2]['account_name'] == account_name), None)

    def test_depository_lands_under_bank_accounts(self):
        self._item(institution='Wells Fargo')
        self._account('acct-chk', subtype='checking', mask='0000')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Bank Accounts', 'is_group': 1,
             'account_type': 'Bank', 'root_type': 'Asset'}])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-chk', client=erp)
        leaf = self._leaf_creates(erp)[0][2]
        self.assertEqual(leaf['parent_account'], 'Bank Accounts - EC')
        self.assertEqual(leaf['account_type'], 'Bank')

    def test_credit_card_lands_under_credit_cards_liability(self):
        self._item(institution='Red Plaidypus Bank')
        self._account('acct-cc', subtype='credit card', type_='credit', mask='9999')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Bank Accounts', 'is_group': 1,
             'account_type': 'Bank', 'root_type': 'Asset'},
            {'account_name': 'Current Liabilities', 'is_group': 1,
             'root_type': 'Liability'}])
        result = erpnext_accounts.import_plaid_account_to_erpnext('acct-cc', client=erp)
        self.assertEqual(result['status'], 'imported')
        # A Credit Cards group was auto-created under Current Liabilities …
        grp = self._group_create(erp, 'Credit Cards')
        self.assertIsNotNone(grp)
        self.assertEqual(grp['parent_account'], 'Current Liabilities - EC')
        # … and the card leaf lives under it, still account_type Bank.
        leaf = self._leaf_creates(erp)[0][2]
        self.assertEqual(leaf['parent_account'], 'Credit Cards - EC')
        self.assertEqual(leaf['account_type'], 'Bank')
        # The Bank Account links the liability-side leaf and stays a company acct.
        ba_doc = erp.creates_of('Bank Account')[0][2]
        self.assertEqual(ba_doc['account'], leaf['account_name'] + ' - EC')
        self.assertEqual(ba_doc['is_company_account'], 1)

    def test_credit_card_reuses_existing_credit_cards_group(self):
        self._item(institution='Red Plaidypus Bank')
        self._account('acct-cc', subtype='credit card', type_='credit', mask='9999')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Current Liabilities', 'is_group': 1,
             'root_type': 'Liability'},
            {'account_name': 'Credit Cards', 'is_group': 1, 'root_type': 'Liability',
             'parent_account': 'Current Liabilities - EC'}])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-cc', client=erp)
        # No new group created — the existing Credit Cards group is reused.
        self.assertIsNone(self._group_create(erp, 'Credit Cards'))
        leaf = self._leaf_creates(erp)[0][2]
        self.assertEqual(leaf['parent_account'], 'Credit Cards - EC')

    def test_resolve_gl_parent_loan_lands_under_longterm_liabilities(self):
        # Loans are gated out by is_supported in the real flow, but the parent
        # resolver still maps them correctly: loan → Loans under Long-term
        # Liabilities when that branch exists.
        self._item()
        a = self._account('acct-loan', subtype='', type_='loan')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Current Liabilities', 'is_group': 1,
             'root_type': 'Liability'},
            {'account_name': 'Long-term Liabilities', 'is_group': 1,
             'root_type': 'Liability'}])
        parent = erpnext_accounts.resolve_gl_parent(erp, a, self.COMPANY)
        self.assertEqual(parent, 'Loans - EC')
        grp = self._group_create(erp, 'Loans')
        self.assertEqual(grp['parent_account'], 'Long-term Liabilities - EC')

    def test_resolve_gl_parent_loan_falls_back_to_current_liabilities(self):
        self._item()
        a = self._account('acct-loan', subtype='', type_='loan')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Current Liabilities', 'is_group': 1,
             'root_type': 'Liability'}])   # no Long-term branch
        parent = erpnext_accounts.resolve_gl_parent(erp, a, self.COMPANY)
        self.assertEqual(parent, 'Loans - EC')
        grp = self._group_create(erp, 'Loans')
        self.assertEqual(grp['parent_account'], 'Current Liabilities - EC')

    def test_resolve_gl_parent_reuses_stock_loans_liabilities_group(self):
        self._item()
        a = self._account('acct-loan', subtype='', type_='loan')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Loans (Liabilities)', 'is_group': 1,
             'root_type': 'Liability'}])
        parent = erpnext_accounts.resolve_gl_parent(erp, a, self.COMPANY)
        self.assertEqual(parent, 'Loans (Liabilities) - EC')
        self.assertIsNone(self._group_create(erp, 'Loans'))   # reused, not created

    def test_gl_side_classification(self):
        self._item()
        dep = self._account('d', subtype='savings')
        cc = self._account('c', subtype='credit card', type_='credit')
        loc = self._account('l', subtype='line of credit', type_='credit')
        ln = self._account('n', subtype='', type_='loan')
        self.assertEqual(erpnext_accounts._gl_side(dep), 'depository')
        self.assertEqual(erpnext_accounts._gl_side(cc), 'credit')
        self.assertEqual(erpnext_accounts._gl_side(loc), 'credit')
        self.assertEqual(erpnext_accounts._gl_side(ln), 'loan')


class TestPreciseSubtype(ImportBase):
    """v0.3.9 · account_subtype maps 1:1 onto the 10 provisioned Bank Account
    Subtype masters, replacing the coarse Current/Other buckets."""

    def test_precise_subtype_mapping(self):
        self._item()
        cases = {
            ('checking', 'depository'): 'Checking',
            ('savings', 'depository'): 'Savings',
            ('cd', 'depository'): 'Cd',
            ('money market', 'depository'): 'Money Market',
            ('cash management', 'depository'): 'Cash Management',
            ('paypal', 'depository'): 'Paypal',
            ('credit card', 'credit'): 'Credit Card',
            ('line of credit', 'credit'): 'Line Of Credit',
        }
        for i, ((st, ty), expected) in enumerate(cases.items()):
            a = self._account(f'ps-{i}', subtype=st, type_=ty)
            self.assertEqual(erpnext_accounts.erpnext_account_subtype(a), expected,
                             f'{ty}/{st}')

    def test_credit_type_without_subtype_reads_credit_card(self):
        self._item()
        a = self._account('cc-x', subtype='', type_='credit')
        self.assertEqual(erpnext_accounts.erpnext_account_subtype(a), 'Credit Card')

    def test_unmapped_subtype_falls_back_to_other(self):
        self._item()
        a = self._account('u-x', subtype='prepaid', type_='depository')
        self.assertEqual(erpnext_accounts.erpnext_account_subtype(a), 'Other')

    def test_import_sends_precise_subtype(self):
        self._item(institution='Red Plaidypus Bank')
        self._account('acct-cc', subtype='credit card', type_='credit', mask='9999')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Current Liabilities', 'is_group': 1,
             'root_type': 'Liability'}])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-cc', client=erp)
        ba_doc = erp.creates_of('Bank Account')[0][2]
        self.assertEqual(ba_doc['account_subtype'], 'Credit Card')


class TestGroupAccountNumbering(ImportBase):
    """v0.3.9 · auto-created GROUP accounts get an account_number in the parent's
    range, and the leaf numbering handles range-numbered parents (e.g. Current
    Liabilities '2100-2400')."""

    COMPANY = 'Example Company LLC'

    def _group_create(self, erp, account_name):
        return next((c[2] for c in erp.creates_of('Account')
                     if c[2].get('is_group') == 1
                     and c[2]['account_name'] == account_name), None)

    def test_group_number_from_hundred_spaced_siblings(self):
        # Current Liabilities children 2100/2200/2300/2400 → new group 2500.
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Current Liabilities', 'is_group': 1,
             'root_type': 'Liability', 'account_number': '2100-2400'},
            {'account_name': 'Accounts Payable', 'is_group': 1,
             'account_number': '2100', 'parent_account': 'Current Liabilities - EC'},
            {'account_name': 'Duties and Taxes', 'is_group': 1,
             'account_number': '2300', 'parent_account': 'Current Liabilities - EC'},
            {'account_name': 'Loans (Liabilities)', 'is_group': 1,
             'account_number': '2400', 'parent_account': 'Current Liabilities - EC'}])
        erpnext_accounts.ensure_credit_card_group(erp, self.COMPANY)
        grp = self._group_create(erp, 'Credit Cards')
        self.assertEqual(grp['account_number'], '2500')

    def test_group_number_skipped_when_chart_unnumbered(self):
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Current Liabilities', 'is_group': 1,
             'root_type': 'Liability'}])   # no numbers anywhere
        erpnext_accounts.ensure_credit_card_group(erp, self.COMPANY)
        grp = self._group_create(erp, 'Credit Cards')
        self.assertNotIn('account_number', grp)

    def test_credit_card_leaf_numbered_from_range_parent(self):
        # Card leaf under the newly-numbered Credit Cards group (2500) → 2501.
        self._item(institution='Red Plaidypus Bank')
        self._account('acct-cc', subtype='credit card', type_='credit', mask='9999')
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Current Liabilities', 'is_group': 1,
             'root_type': 'Liability', 'account_number': '2100-2400'},
            {'account_name': 'Loans (Liabilities)', 'is_group': 1,
             'account_number': '2400', 'parent_account': 'Current Liabilities - EC'}])
        erpnext_accounts.import_plaid_account_to_erpnext('acct-cc', client=erp)
        leaf = next(c[2] for c in erp.creates_of('Account')
                    if c[2].get('is_group') == 0)
        self.assertEqual(leaf['account_number'], '2501')


class TestCreditCardMigrationScript(unittest.TestCase):
    """v0.3.9 · the retroactive migration (scripts/migrate_credit_cards_to_
    liabilities.py) moves credit-card GL accounts to Current Liabilities → Credit
    Cards and is idempotent (a second run changes nothing)."""

    def setUp(self):
        import importlib.util
        here = os.path.dirname(__file__)
        path = os.path.join(here, '..', 'scripts',
                            'migrate_credit_cards_to_liabilities.py')
        spec = importlib.util.spec_from_file_location('_mig_cc', path)
        self.mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mig)

    def _fake(self):
        return FakeFrappe(
            accounts={
                'Source of Funds (Liabilities) - TEST': dict(
                    account_name='Source of Funds (Liabilities)', parent_account='',
                    company='Testing', is_group=1, root_type='Liability',
                    account_number='2000'),
                '2100-2400 - Current Liabilities - TEST': dict(
                    account_name='Current Liabilities',
                    parent_account='Source of Funds (Liabilities) - TEST',
                    company='Testing', is_group=1, root_type='Liability',
                    account_number='2100-2400'),
                '1200 - Bank Accounts - TEST': dict(
                    account_name='Bank Accounts',
                    parent_account='Current Assets - TEST', company='Testing',
                    is_group=1, root_type='Asset', account_type='Bank',
                    account_number='1200'),
                'RP Credit Card - 3333 - TEST': dict(
                    account_name='RP Credit Card - 3333',
                    parent_account='1200 - Bank Accounts - TEST', company='Testing',
                    is_group=0, root_type='Asset', account_type='Bank'),
                'RP Credit Card - 9999 - TEST': dict(
                    account_name='RP Credit Card - 9999',
                    parent_account='2100-2400 - Current Liabilities - TEST',
                    company='Testing', is_group=0, root_type='Liability',
                    account_type='Current Liability'),
            },
            bank_accounts=[
                {'name': 'RP Credit Card - 3333 - RP',
                 'account': 'RP Credit Card - 3333 - TEST', 'account_type': 'Credit'},
                {'name': 'RP Credit Card - 9999 - RP',
                 'account': 'RP Credit Card - 9999 - TEST', 'account_type': 'Credit'},
                {'name': 'RP Checking - 0000 - RP',
                 'account': 'RP Checking - 0000 - TEST', 'account_type': 'Current'},
            ])

    def test_moves_both_cards_and_fixes_type(self):
        fake = self._fake()
        moved = self.mig.run(fake)
        self.assertEqual(set(moved),
                         {'RP Credit Card - 3333 - TEST',
                          'RP Credit Card - 9999 - TEST'})
        # The Credit Cards group was created under Current Liabilities.
        grp = fake.accounts.get('Credit Cards - TEST')
        self.assertIsNotNone(grp)
        self.assertEqual(grp['parent_account'],
                         '2100-2400 - Current Liabilities - TEST')
        # Both cards now sit under it, account_type forced back to Bank.
        for gl in ('RP Credit Card - 3333 - TEST', 'RP Credit Card - 9999 - TEST'):
            self.assertEqual(fake.accounts[gl]['parent_account'], 'Credit Cards - TEST')
            self.assertEqual(fake.accounts[gl]['account_type'], 'Bank')

    def test_second_run_is_idempotent(self):
        fake = self._fake()
        self.mig.run(fake)
        moved2 = self.mig.run(fake)
        self.assertEqual(moved2, [])
        # And no duplicate Credit Cards group was created.
        groups = [n for n, a in fake.accounts.items()
                  if a.get('account_name') == 'Credit Cards']
        self.assertEqual(len(groups), 1)


class TestNumberBackfillScript(unittest.TestCase):
    """v0.3.9 · backfill_account_numbers.py re-orders existing managed leaves by
    liquidity (Cash Management < Checking < Savings < CD), leaves unrecognized
    accounts alone, and is idempotent."""

    def setUp(self):
        import importlib.util
        here = os.path.dirname(__file__)
        path = os.path.join(here, '..', 'scripts', 'backfill_account_numbers.py')
        spec = importlib.util.spec_from_file_location('_bf_num', path)
        self.bf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bf)

    def _fake(self):
        def leaf(subtype_name, cur=None):
            an = f'RP {subtype_name}'
            return dict(account_name=an, parent_account='1200 - Bank Accounts - TEST',
                        company='Testing', is_group=0, root_type='Asset',
                        account_type='Bank', account_number=cur)
        return FakeFrappe(accounts={
            '1100-1600 - Current Assets - TEST': dict(
                account_name='Current Assets', parent_account='', company='Testing',
                is_group=1, root_type='Asset', account_number='1100-1600'),
            '1200 - Bank Accounts - TEST': dict(
                account_name='Bank Accounts',
                parent_account='1100-1600 - Current Assets - TEST', company='Testing',
                is_group=1, root_type='Asset', account_type='Bank',
                account_number='1200'),
            # Deliberately mis-ordered / unnumbered leaves.
            'RP Cd - 2222 - TEST': leaf('Cd - 2222', cur='1202'),
            'RP Cash Management - 9002 - TEST': leaf('Cash Management - 9002', cur='1201'),
            'RP Checking - 0000 - TEST': leaf('Checking - 0000', cur='1203'),
            'RP Savings - 1111 - TEST': leaf('Savings - 1111'),
            'RP Money Market - 4444 - TEST': leaf('Money Market - 4444'),
            # Not a recognized subtype → must be left untouched.
            'Plaid Test - TEST': dict(
                account_name='Plaid Test', parent_account='1200 - Bank Accounts - TEST',
                company='Testing', is_group=0, root_type='Asset', account_type='Bank',
                account_number='Test'),
        })

    def _num(self, fake, name):
        return fake.accounts[name]['account_number']

    def test_liquidity_ordering(self):
        fake = self._fake()
        self.bf.run(fake)
        cm = int(self._num(fake, 'RP Cash Management - 9002 - TEST'))
        chk = int(self._num(fake, 'RP Checking - 0000 - TEST'))
        sav = int(self._num(fake, 'RP Savings - 1111 - TEST'))
        cd = int(self._num(fake, 'RP Cd - 2222 - TEST'))
        # Cash Management < Checking < Savings < CD — the required invariant.
        self.assertLess(cm, chk)
        self.assertLess(chk, sav)
        self.assertLess(sav, cd)
        # And the concrete banded numbers.
        self.assertEqual((cm, chk, sav, cd), (1201, 1211, 1221, 1231))
        # Money Market shares Savings' band, slotted just after it.
        self.assertEqual(self._num(fake, 'RP Money Market - 4444 - TEST'), '1222')

    def test_unrecognized_leaf_untouched(self):
        fake = self._fake()
        self.bf.run(fake)
        self.assertEqual(self._num(fake, 'Plaid Test - TEST'), 'Test')

    def test_idempotent(self):
        fake = self._fake()
        self.bf.run(fake)
        assigned2 = self.bf.run(fake)
        self.assertEqual(assigned2, [])


if __name__ == '__main__':
    unittest.main()
