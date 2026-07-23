# SPDX-License-Identifier: MIT
"""Investment transactions posted as Journal Entries (v0.5.1, Phase D).

Covered here:

  * the kill switch — nothing posts until an Item is explicitly opted in, and
    that default is FALSE on a fresh row so an upgrade auto-posts nothing
  * the Cash Clearing bridge that keeps a paired brokerage's trades from being
    double-booked (the SecurityTransaction JE and its companion BankTransaction
    JE settle against clearing, which nets to zero)
  * buys, sells (with realized gain AND loss), advisory fees, dividends and
    written options landing on the right accounts
  * cost basis: Specific Identification via TradedCycle, FIFO via RetainedLot
  * idempotency — a re-sync of the same trade generates no second JE
  * company scoping on every line
  * the clearing-imbalance check that surfaces a mismatched pair

Synthetic tickers (TEST-AAPL / TESTCO) and round amounts only — no real
securities, no real trade sizes.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import erpnext_settings, invest_je  # noqa: E402
from app.models import (BankTransaction, GeneratedJournalEntry,  # noqa: E402
                        PlaidAccount, PlaidItem, RetainedLot, Security,
                        SecurityTransaction, TradedCycle)

from tests.fakes import FakeERPClient  # noqa: E402

COMPANY = 'Orchard Example, LLC'


class InvestJEBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir, 'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', COMPANY)
        self.item = PlaidItem(
            item_id='item-om', access_token_encrypted=crypto.encrypt('x'),
            institution_name='Wells Fargo', status='active',
            owning_company=COMPANY, invest_je_posting_enabled=True)
        db.session.add(self.item)
        self.brk = PlaidAccount(
            account_id='brk', item_id='item-om', name='BUSINESS BROKERAGE',
            mask='9401', type='investment', subtype='brokerage',
            paired_account_id='cash', owning_company=COMPANY,
            erpnext_bank_account_name='BA-9401')
        self.cash = PlaidAccount(
            account_id='cash', item_id='item-om', name='BROKERAGE CASH',
            mask='3194', type='depository', subtype='checking',
            owning_company=COMPANY,
            erpnext_gl_account_name='Cash Sweep - EC')
        db.session.add_all([self.brk, self.cash])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    # A Chart of Accounts with the three roots the leaves hang under.
    def _client(self, **kw):
        chart = [
            {'account_name': 'Assets', 'root_type': 'Asset', 'is_group': 1,
             'parent_account': '', 'name': 'Assets - EC'},
            {'account_name': 'Income', 'root_type': 'Income', 'is_group': 1,
             'parent_account': '', 'name': 'Income - EC'},
            {'account_name': 'Expenses', 'root_type': 'Expense', 'is_group': 1,
             'parent_account': '', 'name': 'Expenses - EC'},
        ]
        kw.setdefault('chart_accounts', chart)
        return FakeERPClient(**kw)

    def _security(self, sid='sec-aapl', ticker='TEST-AAPL', **kw):
        s = Security(security_id=sid, ticker_symbol=ticker,
                     name=f'{ticker} Inc', type='equity', **kw)
        db.session.add(s)
        db.session.commit()
        return s

    def _txn(self, itx, type_, amount, qty=0.0, price=0.0, sid='sec-aapl',
             subtype='', account_id='brk', when=date(2026, 7, 10)):
        t = SecurityTransaction(
            plaid_investment_transaction_id=itx, account_id=account_id,
            security_id=sid, date=when, quantity=qty, amount=amount,
            price=price, type=type_, subtype=subtype,
            name=f'{type_} {itx}')
        db.session.add(t)
        db.session.commit()
        return t

    def _lines(self, je):
        """{account_label: (debit, credit)} for a created JE doc."""
        return {a['account']: (a.get('debit_in_account_currency', 0.0),
                               a.get('credit_in_account_currency', 0.0))
                for a in je['accounts']}

    def _je_for(self, client, gje):
        return client.created['Journal Entry'][gje.erpnext_journal_entry_name]


class KillSwitchTests(InvestJEBase):
    def test_defaults_false_on_a_fresh_item(self):
        fresh = PlaidItem(item_id='item-new',
                          access_token_encrypted=crypto.encrypt('y'),
                          institution_name='WF', status='active')
        db.session.add(fresh)
        db.session.commit()
        self.assertFalse(fresh.invest_je_posting_enabled)
        self.assertFalse(invest_je.posting_enabled(fresh))

    def test_disabled_item_posts_nothing(self):
        self.item.invest_je_posting_enabled = False
        db.session.commit()
        self._security()
        txn = self._txn('t-buy', 'buy', 1000.0, qty=10, price=100.0)
        client = self._client()
        self.assertIsNone(invest_je.generate_investment_je(client, txn))
        self.assertEqual(len(client.created['Journal Entry']), 0)
        self.assertEqual(GeneratedJournalEntry.query.count(), 0)

    def test_enabled_item_posts(self):
        self._security()
        txn = self._txn('t-buy', 'buy', 1000.0, qty=10, price=100.0)
        client = self._client()
        gje = invest_je.generate_investment_je(client, txn)
        self.assertIsNotNone(gje.erpnext_journal_entry_name)


class ClearingTests(InvestJEBase):
    """The bridge account that nets to zero across a trade and its companion."""

    def test_a_buy_debits_securities_and_credits_clearing(self):
        self._security()
        txn = self._txn('t-buy', 'buy', 10000.0, qty=100, price=100.0)
        client = self._client()
        gje = invest_je.generate_investment_je(client, txn)
        lines = self._lines(self._je_for(client, gje))
        ms = 'Marketable Securities - TEST-AAPL - EC'
        clearing = 'Cash Clearing - Brokerage - EC'
        self.assertEqual(lines[ms], (10000.0, 0.0))
        self.assertEqual(lines[clearing], (0.0, 10000.0))

    def test_clearing_nets_to_zero_across_the_companion_post(self):
        """The SecurityTransaction JE credits clearing; the companion sweep
        BankTransaction (posted by the rules engine) debits it. Net zero."""
        self._security()
        # Buy $10k on the brokerage → clearing credited 10k.
        self._txn('t-buy', 'buy', 10000.0, qty=100, price=100.0)
        # The companion recorded the same $10k leaving the bank.
        db.session.add(BankTransaction(
            plaid_transaction_id='sweep', account_id='cash', amount=10000.0,
            date=date(2026, 7, 10), name='Decrease from Brokerage activity'))
        db.session.commit()
        # Projected clearing balance: security cash-in (-10k) vs companion
        # cash-in (-10k) → zero.
        self.assertEqual(invest_je.clearing_imbalance('brk'), 0.0)

    def test_a_mismatched_pair_shows_a_nonzero_imbalance(self):
        self._security()
        self._txn('t-buy', 'buy', 10000.0, qty=100, price=100.0)
        # Companion only recorded $9k — a missing $1k of movement.
        db.session.add(BankTransaction(
            plaid_transaction_id='sweep', account_id='cash', amount=9000.0,
            date=date(2026, 7, 10), name='Decrease from Brokerage activity'))
        db.session.commit()
        self.assertEqual(invest_je.clearing_imbalance('brk'), -1000.0)

    def test_unpaired_account_settles_against_its_own_bank_leaf(self):
        self.brk.paired_account_id = None
        self.brk.erpnext_gl_account_name = 'Brokerage Sweep - EC'
        db.session.commit()
        self._security()
        txn = self._txn('t-buy', 'buy', 10000.0, qty=100, price=100.0)
        client = self._client()
        gje = invest_je.generate_investment_je(client, txn)
        lines = self._lines(self._je_for(client, gje))
        self.assertIn('Brokerage Sweep - EC', lines)
        self.assertNotIn('Cash Clearing - Brokerage - EC', lines)
        self.assertEqual(invest_je.clearing_imbalance('brk'), 0.0)


class SellTests(InvestJEBase):
    def test_a_sell_with_a_gain_via_specific_id(self):
        """Sell 100 at $12k, cost basis via TradedCycle buy price $80 → basis
        $8k, gain $4k. DR clearing 12k, CR MS 8k, CR gains 4k."""
        self._security()
        self._txn('t-buy', 'buy', 8000.0, qty=100, price=80.0)
        sell = self._txn('t-sell', 'sell', 12000.0, qty=100, price=120.0)
        db.session.add(TradedCycle(
            security_id='sec-aapl', buy_transaction_id='t-buy',
            sell_transaction_id='t-sell', buy_date=date(2026, 6, 1),
            buy_qty=100, buy_price=80.0, sell_qty=100, sell_price=120.0))
        db.session.commit()
        client = self._client()
        gje = invest_je.generate_investment_je(client, sell)
        lines = self._lines(self._je_for(client, gje))
        self.assertEqual(lines['Cash Clearing - Brokerage - EC'], (12000.0, 0.0))
        self.assertEqual(lines['Marketable Securities - TEST-AAPL - EC'],
                         (0.0, 8000.0))
        self.assertEqual(lines['Realized Capital Gains - EC'], (0.0, 4000.0))
        # And it balances.
        self.assertEqual(sum(d for d, _ in lines.values()),
                         sum(c for _, c in lines.values()))

    def test_a_sell_at_a_loss_hits_the_loss_account(self):
        self._security()
        sell = self._txn('t-sell', 'sell', 6000.0, qty=100, price=60.0)
        db.session.add(TradedCycle(
            security_id='sec-aapl', buy_transaction_id='t-buy',
            sell_transaction_id='t-sell', buy_date=date(2026, 6, 1),
            buy_qty=100, buy_price=80.0, sell_qty=100, sell_price=60.0))
        db.session.commit()
        client = self._client()
        gje = invest_je.generate_investment_je(client, sell)
        lines = self._lines(self._je_for(client, gje))
        self.assertEqual(lines['Realized Capital Losses - EC'], (2000.0, 0.0))
        self.assertEqual(lines['Marketable Securities - TEST-AAPL - EC'],
                         (0.0, 8000.0))
        self.assertEqual(sum(d for d, _ in lines.values()),
                         sum(c for _, c in lines.values()))

    def test_fifo_fallback_consumes_retained_lots(self):
        """No TradedCycle → FIFO over RetainedLot, oldest first."""
        self._security()
        db.session.add_all([
            RetainedLot(security_id='sec-aapl', account_id='brk',
                        purchase_date=date(2026, 1, 1), cost_basis_per_share=50.0,
                        shares_original=60, shares_remaining=60),
            RetainedLot(security_id='sec-aapl', account_id='brk',
                        purchase_date=date(2026, 3, 1), cost_basis_per_share=70.0,
                        shares_original=60, shares_remaining=60)])
        db.session.commit()
        sell = self._txn('t-sell', 'sell', 12000.0, qty=100, price=120.0)
        # cost_basis_for_sell is PURE: it computes basis + a plan, mutating
        # nothing until the JE actually posts.
        basis, method, plan = invest_je.cost_basis_for_sell(sell, 100)
        # 60@50 + 40@70 = 3000 + 2800 = 5800
        self.assertEqual(basis, 5800.0)
        self.assertEqual(method, 'fifo')
        lots = RetainedLot.query.order_by(RetainedLot.purchase_date).all()
        self.assertEqual([l.shares_remaining for l in lots], [60, 60])  # untouched
        # Posting the JE consumes them: oldest fully, newer partially.
        invest_je.generate_investment_je(self._client(), sell)
        db.session.expire_all()
        lots = RetainedLot.query.order_by(RetainedLot.purchase_date).all()
        self.assertEqual(lots[0].shares_remaining, 0)
        self.assertEqual(lots[1].shares_remaining, 20)

    def test_a_lot_decrement_rolls_back_when_erpnext_fails(self):
        self._security()
        db.session.add(RetainedLot(
            security_id='sec-aapl', account_id='brk',
            purchase_date=date(2026, 1, 1), cost_basis_per_share=50.0,
            shares_original=100, shares_remaining=100))
        db.session.commit()
        sell = self._txn('t-sell', 'sell', 12000.0, qty=100, price=120.0)
        client = self._client(fail_je_create=True)
        gje = invest_je.generate_investment_je(client, sell)
        self.assertEqual(gje.state, 'error')
        db.session.expire_all()
        self.assertEqual(RetainedLot.query.first().shares_remaining, 100)


class FeeAndIncomeTests(InvestJEBase):
    def test_an_advisory_fee_debits_the_expense(self):
        self._security(sid='sweep', ticker='')
        # Plaid: a fee is positive amount (cash out).
        fee = self._txn('t-fee', 'fee', 3894.71, sid='sweep',
                        subtype='fee/interest')
        client = self._client()
        gje = invest_je.generate_investment_je(client, fee)
        lines = self._lines(self._je_for(client, gje))
        self.assertEqual(lines['Advisory & Management Fees - EC'],
                         (3894.71, 0.0))
        self.assertEqual(lines['Cash Clearing - Brokerage - EC'],
                         (0.0, 3894.71))

    def test_a_dividend_credits_income(self):
        self._security()
        div = self._txn('t-div', 'cash', -35.58, subtype='cash/dividend')
        client = self._client()
        gje = invest_je.generate_investment_je(client, div)
        lines = self._lines(self._je_for(client, gje))
        self.assertEqual(lines['Dividend Income - EC'], (0.0, 35.58))
        self.assertEqual(lines['Cash Clearing - Brokerage - EC'], (35.58, 0.0))

    def test_interest_routes_to_interest_income(self):
        self._security()
        it = self._txn('t-int', 'cash', -10.00, subtype='cash/interest')
        client = self._client()
        gje = invest_je.generate_investment_je(client, it)
        self.assertIn('Interest Income - EC',
                      self._lines(self._je_for(client, gje)))


class OptionsTests(InvestJEBase):
    def test_sell_to_open_credits_premium_income(self):
        self._security(sid='opt', ticker='TESTCO-CALL', is_option=True,
                       option_contract_type='call')
        sto = self._txn('t-sto', 'sell', -250.0, qty=-1, sid='opt')
        client = self._client()
        gje = invest_je.generate_investment_je(client, sto)
        lines = self._lines(self._je_for(client, gje))
        self.assertEqual(lines['Options Premium Income - EC'], (0.0, 250.0))
        self.assertEqual(lines['Cash Clearing - Brokerage - EC'], (250.0, 0.0))

    def test_buy_to_close_debits_premium_losses(self):
        self._security(sid='opt', ticker='TESTCO-CALL', is_option=True,
                       option_contract_type='call')
        btc = self._txn('t-btc', 'buy', 80.0, qty=1, sid='opt')
        client = self._client()
        gje = invest_je.generate_investment_je(client, btc)
        lines = self._lines(self._je_for(client, gje))
        self.assertEqual(lines['Options Premium Losses - EC'], (80.0, 0.0))


class IdempotencyAndScopingTests(InvestJEBase):
    def test_resync_generates_no_second_je(self):
        self._security()
        txn = self._txn('t-buy', 'buy', 1000.0, qty=10, price=100.0)
        client = self._client()
        first = invest_je.generate_investment_je(client, txn)
        again = invest_je.generate_investment_je(client, txn)
        self.assertEqual(first.id, again.id)
        self.assertEqual(len(client.created['Journal Entry']), 1)
        self.assertEqual(GeneratedJournalEntry.query.count(), 1)

    def test_the_key_is_the_investment_transaction_id(self):
        self._security()
        txn = self._txn('t-buy', 'buy', 1000.0, qty=10, price=100.0)
        gje = invest_je.generate_investment_je(self._client(), txn)
        self.assertEqual(gje.plaid_investment_transaction_id, 't-buy')
        self.assertEqual(gje.plaid_transaction_id, 'inv:t-buy')

    def test_every_line_carries_the_company(self):
        self._security()
        txn = self._txn('t-buy', 'buy', 1000.0, qty=10, price=100.0)
        client = self._client()
        gje = invest_je.generate_investment_je(client, txn)
        self.assertEqual(self._je_for(client, gje)['company'], COMPANY)

    def test_transfers_and_cancels_are_not_posted(self):
        self._security()
        for i, kind in enumerate(('transfer', 'cancel')):
            t = self._txn(f't-{i}', kind, 500.0, qty=5)
            self.assertIsNone(
                invest_je.generate_investment_je(self._client(), t))
        self.assertEqual(GeneratedJournalEntry.query.count(), 0)

    def test_batch_posts_and_reports(self):
        self._security()
        self._txn('t-buy', 'buy', 1000.0, qty=10, price=100.0)
        self._txn('t-xfer', 'transfer', 500.0, qty=5)
        client = self._client()
        stats = invest_je.post_investments_for_account(client, 'brk')
        self.assertEqual(stats['posted'], 1)
        self.assertEqual(stats['skipped'], 1)

    def test_the_remark_carries_security_detail(self):
        self._security()
        txn = self._txn('t-buy', 'buy', 15000.0, qty=100, price=150.0)
        client = self._client()
        gje = invest_je.generate_investment_je(client, txn)
        remark = self._je_for(client, gje)['user_remark']
        self.assertIn('TEST-AAPL', remark)
        self.assertIn('100', remark)


class KillSwitchUITests(InvestJEBase):
    def test_the_toggle_endpoint_flips_the_switch(self):
        self.item.invest_je_posting_enabled = False
        db.session.commit()
        client = self.app.test_client()
        resp = client.post('/admin/items/item-om/invest_je_posting',
                           data={'enabled': '1'})
        self.assertEqual(resp.status_code, 302)
        db.session.expire_all()
        self.assertTrue(PlaidItem.query.first().invest_je_posting_enabled)
        client.post('/admin/items/item-om/invest_je_posting',
                    data={'enabled': '0'})
        db.session.expire_all()
        self.assertFalse(PlaidItem.query.first().invest_je_posting_enabled)

    def test_the_accounts_page_shows_the_switch_on_an_investment_item(self):
        body = self.app.test_client().get('/admin/accounts').data.decode()
        self.assertIn('Investment JE posting', body)
        self.assertIn('/admin/items/item-om/invest_je_posting', body)


class MigrationTests(InvestJEBase):
    def test_columns_declared(self):
        from app.migrations import SCHEMA_MIGRATIONS, SCHEMA_INDEXES
        cols = {(t, c) for t, c, _ in SCHEMA_MIGRATIONS}
        self.assertIn(('plaid_items', 'invest_je_posting_enabled'), cols)
        self.assertIn(('generated_journal_entries',
                       'plaid_investment_transaction_id'), cols)
        idx = {(t, c) for _, t, c in SCHEMA_INDEXES}
        self.assertIn(('generated_journal_entries',
                       'plaid_investment_transaction_id'), idx)


if __name__ == '__main__':
    unittest.main()
