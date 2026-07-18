# SPDX-License-Identifier: MIT
"""Multi-entity Level 1 (v0.4.0): the owning ERPNext Company chosen at Plaid
Link time and inherited (overridable) per account.

  * an Item's owning_company propagates to its child accounts at link time
  * a per-account override wins over the Item's Company (correction path)
  * imported ERPNext Bank Accounts carry the resolved Company field
  * generated Journal Entries book to the account's owning Company
  * drift: an ERPNext-side Company change is flagged as an AuditEvent and the
    transaction is refused (not posted into the wrong entity's books)
  * backward compat: owning_company=None resolves to the ERPNext default Company
  * the list_companies() helper + the Link/override endpoints

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import erpnext_accounts, erpnext_bank, erpnext_settings, sync_engine  # noqa: E402
from app import categorization  # noqa: E402
from app.models import (AuditEvent, BankTransaction, CategorizationRule,  # noqa: E402
                        GeneratedJournalEntry, PlaidAccount, PlaidItem)

from tests.fakes import FakeERPClient, FakePlaidClient, page, txn  # noqa: E402

ACC = 'acct-wf-checking'


class MultiEntityBase(unittest.TestCase):
    EXTRA_CONFIG: dict = {}

    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        cfg = {
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        }
        cfg.update(self.EXTRA_CONFIG)
        self.app = create_app(cfg)
        self.ctx = self.app.app_context()
        self.ctx.push()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET',
                              'Example Company LLC')

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _item(self, item_id='item-abc', owning_company=None):
        it = PlaidItem(item_id=item_id,
                       access_token_encrypted=crypto.encrypt('access-x'),
                       institution_id='ins_1', institution_name='Wells Fargo',
                       status='active', owning_company=owning_company)
        db.session.add(it)
        db.session.commit()
        return it

    def _account(self, account_id=ACC, item_id='item-abc', mapped=True,
                 owning_company=None, subtype='checking', type_='depository',
                 gl=None):
        a = PlaidAccount(
            account_id=account_id, item_id=item_id, name='WF Checking',
            mask='1234', type=type_, subtype=subtype,
            erpnext_bank_account_name='WF Checking - Ops' if mapped else None,
            erpnext_gl_account_name=gl, sync_enabled=True,
            owning_company=owning_company)
        db.session.add(a)
        db.session.commit()
        return a

    def _plaid_accounts(self):
        return [{'account_id': ACC, 'name': 'WF Checking', 'official_name': '',
                 'mask': '1234', 'type': 'depository', 'subtype': 'checking',
                 'balance_available': 900.0, 'balance_current': 1000.0,
                 'iso_currency_code': 'USD'}]


# ── resolution + inheritance ────────────────────────────────────────────────

class TestResolution(MultiEntityBase):
    def test_resolution_order_account_then_item_then_default(self):
        self._item(owning_company='Farm LLC')
        a = self._account(owning_company='Orchard LLC')
        # account override wins
        self.assertEqual(erpnext_accounts.owning_company_for(a), 'Orchard LLC')
        # clear the account override → falls to the Item's Company
        a.owning_company = None
        db.session.commit()
        self.assertEqual(erpnext_accounts.owning_company_for(a), 'Farm LLC')
        # clear the Item's too → ERPNext default Company
        it = PlaidItem.query.filter_by(item_id='item-abc').first()
        it.owning_company = None
        db.session.commit()
        self.assertEqual(erpnext_accounts.owning_company_for(a),
                         'Example Company LLC')

    def test_explicit_is_empty_without_a_choice(self):
        self._item()                       # no owning_company
        a = self._account()                # no owning_company
        self.assertEqual(erpnext_accounts.explicit_owning_company(a), '')
        # …but owning_company_for still resolves to the default.
        self.assertEqual(erpnext_accounts.owning_company_for(a),
                         'Example Company LLC')

    def test_item_company_propagates_to_new_accounts_at_link(self):
        item = self._item(owning_company='Farm LLC')
        plaid = FakePlaidClient(accounts=self._plaid_accounts())
        sync_engine.refresh_accounts(item, plaid, 'access-x')
        a = PlaidAccount.query.filter_by(account_id=ACC).first()
        self.assertEqual(a.owning_company, 'Farm LLC')

    def test_refresh_preserves_per_account_override(self):
        item = self._item(owning_company='Farm LLC')
        self._account(owning_company='Orchard LLC')   # an existing override
        plaid = FakePlaidClient(accounts=self._plaid_accounts())
        sync_engine.refresh_accounts(item, plaid, 'access-x')   # a later sync
        a = PlaidAccount.query.filter_by(account_id=ACC).first()
        self.assertEqual(a.owning_company, 'Orchard LLC')  # not clobbered


# ── ERPNext push: Bank Account + JE Company ─────────────────────────────────

class TestErpnextCompanyField(MultiEntityBase):
    def test_bank_account_gets_item_owning_company(self):
        self._item(owning_company='Farm LLC')
        self._account(mapped=False)
        erp = FakeERPClient()
        erpnext_accounts.import_plaid_account_to_erpnext(ACC, client=erp)
        ba_doc = erp.creates_of('Bank Account')[0][2]
        self.assertEqual(ba_doc['company'], 'Farm LLC')

    def test_bank_account_account_override_wins_over_item(self):
        self._item(owning_company='Farm LLC')
        self._account(mapped=False, owning_company='Orchard LLC')
        erp = FakeERPClient()
        erpnext_accounts.import_plaid_account_to_erpnext(ACC, client=erp)
        ba_doc = erp.creates_of('Bank Account')[0][2]
        self.assertEqual(ba_doc['company'], 'Orchard LLC')

    def test_bank_account_falls_back_to_default_company(self):
        self._item()                    # no owning_company anywhere
        self._account(mapped=False)
        erp = FakeERPClient()
        erpnext_accounts.import_plaid_account_to_erpnext(ACC, client=erp)
        ba_doc = erp.creates_of('Bank Account')[0][2]
        self.assertEqual(ba_doc['company'], 'Example Company LLC')

    def test_journal_entry_books_to_owning_company(self):
        self._item(owning_company='Farm LLC')
        self._account(owning_company='Orchard LLC',
                      gl='WF Checking - 1201 - EC')
        rule = CategorizationRule(name='fuel', priority=100, active=True,
                                  match_type='merchant_contains',
                                  match_value='Chevron',
                                  offset_account='Fuel Expense - EC',
                                  offset_direction='auto')
        db.session.add(rule)
        row = BankTransaction(plaid_transaction_id='t1', account_id=ACC,
                              amount=50.0, merchant_name='Chevron',
                              name='CHEVRON 01', date=date(2026, 7, 10),
                              erpnext_bank_transaction_id='ACC-BTN-0001')
        db.session.add(row)
        db.session.commit()
        erp = FakeERPClient()
        categorization.generate_journal_entry(erp, row)
        je = list(erp.created['Journal Entry'].values())[0]
        self.assertEqual(je['company'], 'Orchard LLC')


# ── drift detection ─────────────────────────────────────────────────────────

class TestCompanyDrift(MultiEntityBase):
    def _pending_row(self, tid='t1'):
        row = BankTransaction(plaid_transaction_id=tid, account_id=ACC,
                              amount=25.0, name='Coffee', date=date(2026, 7, 10))
        db.session.add(row)
        db.session.commit()
        return row

    def test_drift_refused_and_audited(self):
        self._item(owning_company='Farm LLC')
        self._account(owning_company='Farm LLC')
        self._pending_row()
        erp = FakeERPClient()
        # ERPNext believes the Bank Account belongs to a DIFFERENT Company.
        erp.created['Bank Account']['WF Checking - Ops'] = {'company': 'Orchard LLC'}
        stats = sync_engine.push_pending(erp, 'item-abc')
        self.assertEqual(stats['drift'], 1)
        self.assertEqual(stats['posted'], 0)
        # No Bank Transaction was created in ERPNext.
        self.assertEqual(len(erp.creates_of('Bank Transaction')), 0)
        # The row stays pending (posted_at NULL) with a drift sync_error.
        row = BankTransaction.query.filter_by(plaid_transaction_id='t1').first()
        self.assertIsNone(row.posted_at)
        self.assertIn('drift', (row.sync_error or '').lower())
        # An AuditEvent records the drift.
        ev = AuditEvent.query.filter_by(event_type='company_drift_detected').first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.subject_id, ACC)

    def test_no_drift_when_company_aligned(self):
        self._item(owning_company='Farm LLC')
        self._account(owning_company='Farm LLC')
        self._pending_row()
        erp = FakeERPClient()
        erp.created['Bank Account']['WF Checking - Ops'] = {'company': 'Farm LLC'}
        stats = sync_engine.push_pending(erp, 'item-abc')
        self.assertEqual(stats['drift'], 0)
        self.assertEqual(stats['posted'], 1)
        row = BankTransaction.query.filter_by(plaid_transaction_id='t1').first()
        self.assertIsNotNone(row.posted_at)

    def test_no_drift_check_without_explicit_company(self):
        # Backward compat: no owning_company anywhere → the drift probe is
        # skipped entirely and the row posts as it did pre-v0.4.0, even though
        # ERPNext reports some Company.
        self._item()
        self._account()
        self._pending_row()
        erp = FakeERPClient()
        erp.created['Bank Account']['WF Checking - Ops'] = {'company': 'Whatever Co'}
        stats = sync_engine.push_pending(erp, 'item-abc')
        self.assertEqual(stats['drift'], 0)
        self.assertEqual(stats['posted'], 1)


# ── helpers + endpoints ─────────────────────────────────────────────────────

class TestListCompanies(MultiEntityBase):
    def test_list_companies_returns_names(self):
        erp = FakeERPClient(companies=['Farm LLC', 'Orchard LLC'])
        self.assertEqual(erpnext_bank.list_companies(erp),
                         ['Farm LLC', 'Orchard LLC'])


class TestEndpoints(MultiEntityBase):
    def test_set_link_company_then_exchange_stamps_item(self):
        client = self.app.test_client()
        fake_plaid = FakePlaidClient(accounts=[])
        with mock.patch.object(sync_engine, 'get_plaid_client',
                               return_value=fake_plaid):
            r1 = client.post('/bankbridge/api/plaid/set_link_company',
                             json={'company': 'Farm LLC'})
            self.assertEqual(r1.status_code, 200)
            r2 = client.post('/bankbridge/api/plaid/exchange_token',
                             json={'public_token': 'public-x'})
            self.assertEqual(r2.status_code, 200)
        item = PlaidItem.query.filter_by(item_id='item-abc').first()
        self.assertIsNotNone(item)
        self.assertEqual(item.owning_company, 'Farm LLC')

    def test_set_account_company_endpoint_overrides_and_audits(self):
        self._item(owning_company='Farm LLC')
        self._account(owning_company=None)
        client = self.app.test_client()
        r = client.post(f'/api/accounts/{ACC}/set_company',
                        data={'owning_company': 'Orchard LLC'})
        self.assertIn(r.status_code, (301, 302))
        a = PlaidAccount.query.filter_by(account_id=ACC).first()
        self.assertEqual(a.owning_company, 'Orchard LLC')
        ev = AuditEvent.query.filter_by(event_type='owning_company_override').first()
        self.assertIsNotNone(ev)

    def test_set_account_company_blank_clears_override(self):
        self._item(owning_company='Farm LLC')
        self._account(owning_company='Orchard LLC')
        client = self.app.test_client()
        client.post(f'/api/accounts/{ACC}/set_company',
                    data={'owning_company': ''})
        a = PlaidAccount.query.filter_by(account_id=ACC).first()
        self.assertIsNone(a.owning_company)
        # …now inherits the Item's Company again.
        self.assertEqual(erpnext_accounts.owning_company_for(a), 'Farm LLC')


if __name__ == '__main__':
    unittest.main()
