# SPDX-License-Identifier: MIT
"""Admin UI + settings + Plaid response normalization.

  * every admin page renders 200
  * Plaid settings persist + mask secrets; blank secret keeps the old one
  * ERPNext settings persist + mask
  * plaid_client response-normalization helpers shape added/removed/category
  * exchange_token stores the access_token ENCRYPTED (never plaintext)

    cd erpnext-bank-bridge_v0.1.0
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import plaid_settings, erpnext_settings  # noqa: E402
from app.models import PlaidItem  # noqa: E402
from app import plaid_client as pc  # noqa: E402

from tests.fakes import FakePlaidClient  # noqa: E402


class AdminBase(unittest.TestCase):
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

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)


class TestPages(AdminBase):
    def test_all_pages_render(self):
        for path in ('/', '/admin', '/admin/link_bank', '/admin/accounts',
                     '/admin/transactions', '/admin/sync_log',
                     '/admin/plaid_settings', '/admin/erpnext_settings'):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, f'{path} → {r.status_code}')

    def test_health(self):
        r = self.client.get('/api/health')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['status'], 'ok')


class TestPlaidSettings(AdminBase):
    def test_persist_and_mask(self):
        self.client.post('/admin/plaid_settings', data={
            'client_id': 'CID1', 'environment': 'sandbox',
            'sandbox_secret': 'SANDSECRET', 'production_secret': '',
            'redirect_uri': 'http://umbrel.local:5202/plaid/oauth_return'})
        s = plaid_settings.load()
        self.assertEqual(s['client_id'], 'CID1')
        self.assertEqual(s['sandbox_secret'], 'SANDSECRET')
        self.assertTrue(plaid_settings.is_configured())
        # Masked preview never shows the full secret.
        self.assertNotIn('SANDSECRET', plaid_settings.masked()['sandbox_secret'])
        # Re-save with blank secret keeps the old one.
        self.client.post('/admin/plaid_settings', data={
            'client_id': 'CID2', 'environment': 'sandbox',
            'sandbox_secret': '', 'production_secret': ''})
        self.assertEqual(plaid_settings.load()['sandbox_secret'], 'SANDSECRET')
        self.assertEqual(plaid_settings.load()['client_id'], 'CID2')

    def test_active_secret_follows_environment(self):
        plaid_settings.save('CID', 'production', sandbox_secret='SAND',
                            production_secret='PROD')
        self.assertEqual(plaid_settings.active_secret(), 'PROD')
        plaid_settings.save('CID', 'sandbox')
        self.assertEqual(plaid_settings.active_secret(), 'SAND')


class TestERPNextSettings(AdminBase):
    def test_persist_and_mask(self):
        self.client.post('/admin/erpnext_settings', data={
            'url': 'http://erp.test', 'api_key': 'K', 'api_secret': 'SECRETVAL',
            'default_company': 'Example Company LLC'})
        s = erpnext_settings.load()
        self.assertEqual(s['url'], 'http://erp.test')
        self.assertEqual(s['api_secret'], 'SECRETVAL')
        self.assertNotIn('SECRETVAL', erpnext_settings.masked_secret())


class TestExchangeTokenEncryption(AdminBase):
    def test_exchange_stores_encrypted_access_token(self):
        plaid_settings.save('CID', 'sandbox', sandbox_secret='SAND')
        fake = FakePlaidClient(accounts=[{
            'account_id': 'a1', 'name': 'Checking', 'official_name': '',
            'mask': '1234', 'type': 'depository', 'subtype': 'checking',
            'balance_available': 1.0, 'balance_current': 2.0,
            'iso_currency_code': 'USD'}])
        with mock.patch('app.sync_engine.get_plaid_client', return_value=fake):
            r = self.client.post('/api/plaid/exchange_token',
                                 json={'public_token': 'public-sandbox-xyz'})
        self.assertEqual(r.status_code, 200)
        item = PlaidItem.query.filter_by(item_id='item-abc').first()
        self.assertIsNotNone(item)
        # Stored value is ciphertext, not the plaintext access token.
        self.assertNotEqual(item.access_token_encrypted, 'access-sandbox-abc')
        self.assertNotIn('access-sandbox-abc', item.access_token_encrypted)
        self.assertEqual(crypto.decrypt(item.access_token_encrypted),
                         'access-sandbox-abc')


class TestPlaidNormalization(unittest.TestCase):
    """Pure-function normalization — no app context / SDK needed."""
    def test_normalize_txn_shapes_fields(self):
        out = pc._normalize_txn({
            'transaction_id': 't1', 'account_id': 'a1', 'amount': 12.34,
            'iso_currency_code': 'USD', 'date': '2026-07-10', 'name': 'Store',
            'merchant_name': 'Merch', 'pending': True,
            'personal_finance_category': {'detailed': 'GENERAL_MERCHANDISE'}})
        self.assertEqual(out['transaction_id'], 't1')
        self.assertEqual(out['amount'], 12.34)
        self.assertTrue(out['pending'])
        self.assertEqual(out['category'], 'GENERAL_MERCHANDISE')

    def test_category_falls_back_to_legacy_list(self):
        out = pc._normalize_txn({'transaction_id': 't', 'account_id': 'a',
                                 'amount': 1, 'category': ['Food', 'Coffee']})
        self.assertEqual(out['category'], 'Food > Coffee')

    def test_normalize_account_pulls_balances(self):
        out = pc._normalize_account({
            'account_id': 'a1', 'name': 'Chk', 'mask': '9999',
            'type': 'depository', 'subtype': 'checking',
            'balances': {'available': 100.0, 'current': 150.0,
                         'iso_currency_code': 'USD'}})
        self.assertEqual(out['balance_current'], 150.0)
        self.assertEqual(out['iso_currency_code'], 'USD')

    def test_removed_normalization(self):
        self.assertEqual(pc._normalize_removed({'transaction_id': 'x'}),
                         {'transaction_id': 'x'})


if __name__ == '__main__':
    unittest.main()
