# SPDX-License-Identifier: MIT
"""Balance-only investment support (v0.4.0).

  * investment accounts are now supported (balance-only) and get a Bank Account
    + GL leaf under Non-current Assets → Investments, subdivided by subtype
  * 401k/IRA/HSA → Retirement; brokerage/stock/bond → Marketable Securities;
    crypto exchange → Digital Assets (even from a non-investment institution)
  * the balance_only flag is set on investment accounts, cleared on depository
  * the sync loop skips /transactions/sync for a balance-only-only Item
  * each refresh mirrors the current balance onto the ERPNext Bank Account
  * reserved account numbers (Investments 1300, Retirement 1310, …) on a
    numbered chart

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import erpnext_accounts, erpnext_settings, sync_engine  # noqa: E402
from app.models import PlaidAccount, PlaidItem  # noqa: E402

from tests.fakes import FakeERPClient, FakePlaidClient  # noqa: E402

COMPANY = 'Example Company LLC'


class InvestmentBase(unittest.TestCase):
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
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', COMPANY)

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
                       institution_id='ins_1', institution_name='Vanguard',
                       status='active', owning_company=owning_company)
        db.session.add(it)
        db.session.commit()
        return it

    def _account(self, account_id, subtype, type_='investment', mask='0000',
                 item_id='item-abc', mapped=False, balance_current=None):
        a = PlaidAccount(
            account_id=account_id, item_id=item_id,
            name=f'{subtype} {mask}', mask=mask, type=type_, subtype=subtype,
            balance_current=balance_current, import_status='pending',
            balance_only=erpnext_accounts.is_investment_type(type_, subtype),
            erpnext_bank_account_name='INV Acct' if mapped else None)
        db.session.add(a)
        db.session.commit()
        return a

    def _noncurrent_chart(self, number=None):
        d = {'account_name': 'Non-current Assets', 'is_group': 1,
             'root_type': 'Asset', 'parent_account': ''}
        if number:
            d['account_number'] = number
        return [d]

    def _leaf(self, erp):
        return next(c[2] for c in erp.creates_of('Account')
                    if c[2].get('is_group') == 0)

    def _group_created(self, erp, account_name):
        return next((c[2] for c in erp.creates_of('Account')
                     if c[2].get('account_name') == account_name), None)


# ── classification ──────────────────────────────────────────────────────────

class TestClassification(InvestmentBase):
    def test_investment_is_supported(self):
        self._item()
        for st, ty in (('401k', 'investment'), ('brokerage', 'brokerage'),
                       ('ira', 'investment'), ('crypto exchange', 'investment')):
            a = self._account(f'x-{st}', subtype=st, type_=ty)
            self.assertTrue(erpnext_accounts.is_supported(a), f'{ty}/{st}')

    def test_subgroup_mapping(self):
        self._item()
        cases = {
            '401k': 'Retirement', 'ira': 'Retirement', 'hsa': 'Retirement',
            'brokerage': 'Marketable Securities', 'stock': 'Marketable Securities',
            'bond': 'Marketable Securities', 'crypto exchange': 'Digital Assets',
            'annuity': 'Other',
        }
        for st, expect in cases.items():
            a = self._account(f'x-{st}', subtype=st)
            self.assertEqual(erpnext_accounts.investment_subgroup(a), expect, st)

    def test_liquidity_ranks_extended(self):
        self._item()
        self.assertEqual(
            erpnext_accounts.liquidity_rank(self._account('b', 'brokerage')), 5)
        self.assertEqual(
            erpnext_accounts.liquidity_rank(self._account('r', '401k')), 6)
        self.assertEqual(
            erpnext_accounts.liquidity_rank(
                self._account('c', 'crypto exchange')), 7)


# ── chart placement ─────────────────────────────────────────────────────────

class TestPlacement(InvestmentBase):
    def test_401k_under_retirement(self):
        self._item()
        self._account('a-401k', subtype='401k', mask='1111')
        erp = FakeERPClient(chart_accounts=self._noncurrent_chart())
        result = erpnext_accounts.import_plaid_account_to_erpnext(
            'a-401k', client=erp)
        self.assertEqual(result['status'], 'imported')
        # Investments group under Non-current Assets, Retirement under it.
        inv = self._group_created(erp, 'Investments')
        self.assertEqual(inv['parent_account'], 'Non-current Assets - EC')
        ret = self._group_created(erp, 'Retirement')
        self.assertEqual(ret['parent_account'], 'Investments - EC')
        # Leaf under Retirement.
        self.assertEqual(self._leaf(erp)['parent_account'], 'Retirement - EC')

    def test_brokerage_under_marketable(self):
        self._item()
        self._account('a-brk', subtype='brokerage', type_='brokerage')
        erp = FakeERPClient(chart_accounts=self._noncurrent_chart())
        erpnext_accounts.import_plaid_account_to_erpnext('a-brk', client=erp)
        self.assertIsNotNone(self._group_created(erp, 'Marketable Securities'))
        self.assertEqual(self._leaf(erp)['parent_account'],
                         'Marketable Securities - EC')

    def test_crypto_under_digital_assets(self):
        self._item()
        self._account('a-cx', subtype='crypto exchange')
        erp = FakeERPClient(chart_accounts=self._noncurrent_chart())
        erpnext_accounts.import_plaid_account_to_erpnext('a-cx', client=erp)
        self.assertEqual(self._leaf(erp)['parent_account'], 'Digital Assets - EC')

    def test_crypto_from_bank_institution_still_digital(self):
        # subtype crypto_exchange on a *depository* account (a bank, not an
        # investment institution) still lands under Digital Assets.
        self._item()
        self._account('a-cx', subtype='crypto_exchange', type_='depository')
        erp = FakeERPClient(chart_accounts=self._noncurrent_chart())
        erpnext_accounts.import_plaid_account_to_erpnext('a-cx', client=erp)
        self.assertEqual(self._leaf(erp)['parent_account'], 'Digital Assets - EC')

    def test_reserved_group_numbers_on_numbered_chart(self):
        self._item()
        self._account('a-401k', subtype='401k')
        erp = FakeERPClient(chart_accounts=self._noncurrent_chart(number='1500'))
        erpnext_accounts.import_plaid_account_to_erpnext('a-401k', client=erp)
        self.assertEqual(self._group_created(erp, 'Investments')['account_number'],
                         '1300')
        self.assertEqual(self._group_created(erp, 'Retirement')['account_number'],
                         '1310')


# ── balance_only flag + sync behaviour ──────────────────────────────────────

class TestBalanceOnlyFlag(InvestmentBase):
    def _plaid_accounts(self, *specs):
        out = []
        for aid, ty, st, bal in specs:
            out.append({'account_id': aid, 'name': st, 'official_name': '',
                        'mask': '0000', 'type': ty, 'subtype': st,
                        'balance_available': None, 'balance_current': bal,
                        'iso_currency_code': 'USD'})
        return out

    def test_flag_set_on_investment_cleared_on_depository(self):
        item = self._item()
        plaid = FakePlaidClient(accounts=self._plaid_accounts(
            ('a-401k', 'investment', '401k', 23631.98),
            ('a-chk', 'depository', 'checking', 500.0)))
        sync_engine.refresh_accounts(item, plaid, 'access-x')
        inv = PlaidAccount.query.filter_by(account_id='a-401k').first()
        chk = PlaidAccount.query.filter_by(account_id='a-chk').first()
        self.assertTrue(inv.balance_only)
        self.assertFalse(chk.balance_only)

    def test_sync_skips_transactions_sync_for_balance_only_item(self):
        item = self._item()
        plaid = FakePlaidClient(accounts=self._plaid_accounts(
            ('a-401k', 'investment', '401k', 23631.98)))
        erp = FakeERPClient()
        sync_engine.sync_item(item, plaid, erp)
        # Accounts were refreshed (balance), but transactions_sync was NOT called.
        kinds = [c[0] for c in plaid.calls]
        self.assertIn('get_accounts', kinds)
        self.assertNotIn('transactions_sync', kinds)


class TestBalanceRefresh(InvestmentBase):
    def test_refresh_writes_balance_to_bank_account(self):
        self._item()
        self._account('a-401k', subtype='401k', mapped=True,
                      balance_current=23631.98)
        erp = FakeERPClient()
        count = erpnext_accounts.refresh_investment_balances(erp)
        self.assertEqual(count, 1)
        updates = [c for c in erp.calls
                   if c[0] == 'update_doc' and c[1] == 'Bank Account']
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][3]['plaid_balance'], 23631.98)

    def test_unmapped_balance_only_account_is_not_pushed(self):
        self._item()
        self._account('a-401k', subtype='401k', mapped=False,
                      balance_current=100.0)
        erp = FakeERPClient()
        self.assertEqual(erpnext_accounts.refresh_investment_balances(erp), 0)


if __name__ == '__main__':
    unittest.main()
