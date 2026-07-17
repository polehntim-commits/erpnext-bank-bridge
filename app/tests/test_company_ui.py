# SPDX-License-Identifier: MIT
"""Multi-entity Company indicators across the admin UI (v0.4.0.1).

  * navbar Current-Company switcher: dropdown when >1 Company, name when ==1,
    nothing on a pre-multi-entity install
  * /admin/transactions Company scope: filter correctness + persistent header
  * /admin/rules Company scope: scoped + company-agnostic rules surface; header
  * rule engine honours applies_to_company (a scoped rule only fires in-scope)
  * the applies_to_company migration adds the column on an upgrading DB
  * retroactive Item Company reassignment endpoint

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from sqlalchemy import inspect, text  # noqa: E402

from app import create_app, db, crypto, migrations  # noqa: E402
from app import categorization, erpnext_settings  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        PlaidAccount, PlaidItem)


class CompanyUIBase(unittest.TestCase):
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
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        erpnext_settings.save('http://erp.test', 'K', 'S', 'Default Co')

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _item(self, item_id, company=None, name='Bank'):
        db.session.add(PlaidItem(
            item_id=item_id, access_token_encrypted=crypto.encrypt('x'),
            institution_name=name, status='active', owning_company=company))
        db.session.commit()

    def _account(self, account_id, item_id, company=None):
        db.session.add(PlaidAccount(
            account_id=account_id, item_id=item_id, name=account_id,
            mask=account_id[-4:], owning_company=company))
        db.session.commit()

    def _txn(self, tid, account_id, name, when):
        db.session.add(BankTransaction(
            plaid_transaction_id=tid, account_id=account_id, name=name,
            amount=10.0, date=when))
        db.session.commit()

    def _two_companies(self):
        self._item('it-A', company='Alpha LLC', name='Alpha Bank')
        self._item('it-B', company='Beta LLC', name='Beta Bank')
        self._account('ac-A', 'it-A')
        self._account('ac-B', 'it-B')
        self._txn('t-A', 'ac-A', 'Alpha purchase', date(2026, 1, 1))
        self._txn('t-B', 'ac-B', 'Beta purchase', date(2026, 1, 2))


class TestNavbarSwitcher(CompanyUIBase):
    def test_dropdown_when_multiple_companies(self):
        self._two_companies()
        body = self.client.get('/admin/transactions').get_data(as_text=True)
        self.assertIn('Current Company:', body)
        self.assertIn('All Companies', body)      # the dropdown's (all) option
        self.assertIn('Alpha LLC', body)
        self.assertIn('Beta LLC', body)

    def test_name_only_when_single_company(self):
        self._item('it-A', company='Alpha LLC')
        self._account('ac-A', 'it-A')
        body = self.client.get('/admin/transactions').get_data(as_text=True)
        self.assertIn('Current Company:', body)
        self.assertIn('Alpha LLC', body)
        # A lone Company is shown as text, not a switchable dropdown.
        self.assertNotIn('All Companies', body)

    def test_absent_on_pre_multi_entity_install(self):
        # No owning_company anywhere → the navbar looks exactly as it did pre-0.4.
        self._item('it-A', company=None)
        self._account('ac-A', 'it-A')
        body = self.client.get('/admin/transactions').get_data(as_text=True)
        self.assertNotIn('Current Company:', body)


class TestTransactionsScope(CompanyUIBase):
    def test_filter_and_header_follow_scope(self):
        self._two_companies()
        # Unscoped: both transactions + the "all Companies" header.
        body = self.client.get('/admin/transactions').get_data(as_text=True)
        self.assertIn('across all Companies', body)
        self.assertIn('Alpha purchase', body)
        self.assertIn('Beta purchase', body)
        # Scope to Alpha → only Alpha's transaction + the scoped header.
        self.client.get('/admin/set_company?company=Alpha+LLC&next=/admin/transactions')
        body = self.client.get('/admin/transactions').get_data(as_text=True)
        self.assertIn('Viewing transactions for:', body)
        self.assertIn('Alpha purchase', body)
        self.assertNotIn('Beta purchase', body)
        # Clearing the scope brings everything back.
        self.client.get('/admin/set_company?company=&next=/admin/transactions')
        body = self.client.get('/admin/transactions').get_data(as_text=True)
        self.assertIn('across all Companies', body)
        self.assertIn('Beta purchase', body)


class TestRulesScope(CompanyUIBase):
    def test_scoped_and_agnostic_rules_surface(self):
        self._two_companies()
        db.session.add(CategorizationRule(name='Alpha rule',
                       match_type='merchant_contains', match_value='a',
                       applies_to_company='Alpha LLC'))
        db.session.add(CategorizationRule(name='Beta rule',
                       match_type='merchant_contains', match_value='b',
                       applies_to_company='Beta LLC'))
        db.session.add(CategorizationRule(name='Global rule',
                       match_type='merchant_contains', match_value='g'))
        db.session.commit()
        self.client.get('/admin/set_company?company=Alpha+LLC&next=/admin/rules')
        body = self.client.get('/admin/rules').get_data(as_text=True)
        self.assertIn('Viewing rules for:', body)
        self.assertIn('Alpha rule', body)     # scoped to Alpha
        self.assertIn('Global rule', body)    # company-agnostic → always shown
        self.assertNotIn('Beta rule', body)   # scoped to a different Company

    def test_engine_honours_company_scope(self):
        # A rule scoped to Alpha must fire for an Alpha transaction and NOT for a
        # Beta one, even though both match the predicate.
        self._two_companies()
        db.session.add(CategorizationRule(
            name='Alpha only', priority=10, match_type='merchant_contains',
            match_value='purchase', offset_account='Some Expense',
            applies_to_company='Alpha LLC'))
        db.session.commit()
        alpha_txn = BankTransaction.query.filter_by(account_id='ac-A').first()
        beta_txn = BankTransaction.query.filter_by(account_id='ac-B').first()
        alpha_txn.merchant_name = 'purchase'
        beta_txn.merchant_name = 'purchase'
        db.session.commit()
        self.assertIsNotNone(categorization.find_matching_rule(alpha_txn))
        self.assertIsNone(categorization.find_matching_rule(beta_txn))


class TestItemCompanyReassign(CompanyUIBase):
    def test_set_item_company_endpoint(self):
        self._item('it-A', company='Alpha LLC')
        self._account('ac-A', 'it-A')
        self.client.post('/admin/items/it-A/set_company',
                         data={'owning_company': 'Beta LLC'})
        it = PlaidItem.query.filter_by(item_id='it-A').first()
        self.assertEqual(it.owning_company, 'Beta LLC')
        # Blank clears back to the ERPNext default.
        self.client.post('/admin/items/it-A/set_company',
                         data={'owning_company': ''})
        it = PlaidItem.query.filter_by(item_id='it-A').first()
        self.assertIsNone(it.owning_company)


class TestAppliesToCompanyMigration(CompanyUIBase):
    def test_migration_adds_column_on_upgrade(self):
        # Simulate a pre-0.4.0.1 DB: drop the new column (and its index first —
        # SQLite refuses to drop an indexed column).
        with db.engine.begin() as conn:
            conn.execute(text(
                'DROP INDEX IF EXISTS ix_categorization_rules_applies_to_company'))
            conn.execute(text(
                'ALTER TABLE categorization_rules DROP COLUMN applies_to_company'))
        cols = {c['name'] for c in inspect(db.engine).get_columns(
            'categorization_rules')}
        self.assertNotIn('applies_to_company', cols)
        migrations.run_migrations()
        cols = {c['name'] for c in inspect(db.engine).get_columns(
            'categorization_rules')}
        self.assertIn('applies_to_company', cols)
        # And the ORM can query the table again.
        self.assertEqual(CategorizationRule.query.all(), [])


if __name__ == '__main__':
    unittest.main()
