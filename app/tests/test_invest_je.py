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
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import erpnext_settings, invest_je  # noqa: E402
from app.erpnext_client import ERPNextAPIError, ERPNextError  # noqa: E402
from app.models import (BankTransaction, GeneratedJournalEntry,  # noqa: E402
                        PlaidAccount, PlaidItem, RetainedLot, Security,
                        SecurityTransaction, TradedCycle)

from tests.fakes import FakeERPClient, FakePlaidClient  # noqa: E402

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
        ms = 'Stocks - EC'
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
        self.assertEqual(lines['Stocks - EC'],
                         (0.0, 8000.0))
        self.assertEqual(lines['Realized Capital Gains - EC'], (0.0, 4000.0))
        # And it balances.
        self.assertEqual(sum(d for d, _ in lines.values()),
                         sum(c for _, c in lines.values()))

    def test_a_sell_at_a_loss_hits_the_loss_account(self):
        self._security()
        self._txn('t-buy', 'buy', 8000.0, qty=100, price=80.0)   # $80/unit cost
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
        self.assertEqual(lines['Stocks - EC'],
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


class MathTests(InvestJEBase):
    """v0.5.12 · the Phase-D JE arithmetic, incl. the two bugs that blew up
    Tim's P&L (cash-sweep rows booked as dividend income; sweep-fund sells
    fabricating basis from price×qty). Synthetic securities only."""

    CLEARING = 'Cash Clearing - Brokerage - EC'
    MS = 'Stocks - EC'
    GAINS = 'Realized Capital Gains - EC'
    LOSSES = 'Realized Capital Losses - EC'
    DIV = 'Dividend Income - EC'
    INT = 'Interest Income - EC'
    ADV = 'Advisory & Management Fees - EC'
    PREM = 'Options Premium Income - EC'

    def _lot(self, per_share, shares=100.0, sid='sec-aapl'):
        db.session.add(RetainedLot(
            security_id=sid, account_id='brk', purchase_date=date(2026, 1, 1),
            cost_basis_per_share=per_share, shares_original=shares,
            shares_remaining=shares))
        db.session.commit()

    def _built(self, txn):
        client = self._client()
        gje = invest_je.generate_investment_je(client, txn)
        return (self._lines(self._je_for(client, gje)) if gje
                and gje.erpnext_journal_entry_name else None), gje

    def test_buy(self):
        self._security()
        lines, _ = self._built(self._txn('t', 'buy', 10000.0, qty=100, price=100.0))
        self.assertEqual(lines[self.MS], (10000.0, 0.0))
        self.assertEqual(lines[self.CLEARING], (0.0, 10000.0))

    def test_sell_at_gain_books_profit_only(self):
        self._security()
        self._lot(80.0)                         # basis $8,000 for 100 shares
        lines, _ = self._built(self._txn('t', 'sell', 12000.0, qty=100, price=120.0))
        self.assertEqual(lines[self.CLEARING], (12000.0, 0.0))
        self.assertEqual(lines[self.MS], (0.0, 8000.0))       # cost, not proceeds
        self.assertEqual(lines[self.GAINS], (0.0, 4000.0))    # gain-over-cost only
        self.assertNotIn(self.LOSSES, lines)

    def test_sell_at_loss(self):
        self._security()
        self._lot(150.0)                        # basis $15,000
        lines, _ = self._built(self._txn('t', 'sell', 12000.0, qty=100, price=120.0))
        self.assertEqual(lines[self.CLEARING], (12000.0, 0.0))
        self.assertEqual(lines[self.MS], (0.0, 15000.0))
        self.assertEqual(lines[self.LOSSES], (3000.0, 0.0))
        self.assertNotIn(self.GAINS, lines)

    def test_dividend(self):
        self._security()
        lines, _ = self._built(self._txn('t', 'cash', -50.0, subtype='dividend'))
        self.assertEqual(lines[self.CLEARING], (50.0, 0.0))
        self.assertEqual(lines[self.DIV], (0.0, 50.0))

    def test_interest(self):
        self._security()
        lines, _ = self._built(self._txn('t', 'cash', -10.0, subtype='interest'))
        self.assertEqual(lines[self.CLEARING], (10.0, 0.0))
        self.assertEqual(lines[self.INT], (0.0, 10.0))

    def test_advisory_fee(self):
        self._security()
        lines, _ = self._built(self._txn('t', 'fee', -3894.0, subtype='fee'))
        self.assertEqual(lines[self.ADV], (3894.0, 0.0))
        self.assertEqual(lines[self.CLEARING], (0.0, 3894.0))

    def test_options_premium_sell_to_open(self):
        self._security(sid='sec-opt', ticker='TEST-OPT', is_option=True)
        lines, _ = self._built(self._txn('t', 'sell', 500.0, qty=1, price=5.0,
                                         sid='sec-opt'))
        self.assertEqual(lines[self.CLEARING], (500.0, 0.0))
        self.assertEqual(lines[self.PREM], (0.0, 500.0))

    # ── BUG 1: cash-sweep movements must NOT post as income ──────────────────
    def test_cash_deposit_is_not_posted(self):
        self._security()
        _, gje = self._built(self._txn('t', 'cash', -100000.0, subtype='deposit'))
        self.assertIsNone(gje)                  # skipped, no JE, no income
        self.assertEqual(GeneratedJournalEntry.query.count(), 0)

    def test_cash_withdrawal_is_not_posted(self):
        self._security()
        _, gje = self._built(self._txn('t', 'cash', 100000.0, subtype='withdrawal'))
        self.assertIsNone(gje)

    # ── BUG 2: no-basis sell uses PROCEEDS, never price×qty ──────────────────
    def test_sell_with_no_lots_books_zero_gain(self):
        self._security()
        # No RetainedLot for this security → no_basis path.
        lines, _ = self._built(self._txn('t', 'sell', 12000.0, qty=100, price=120.0))
        self.assertEqual(lines[self.CLEARING], (12000.0, 0.0))
        self.assertEqual(lines[self.MS], (0.0, 12000.0))      # basis = proceeds
        self.assertNotIn(self.GAINS, lines)                   # zero gain
        self.assertNotIn(self.LOSSES, lines)

    def test_sweep_fund_sell_does_not_fabricate_a_loss(self):
        """The exact live bug: qty 40000 × 'price' 99.60 = $3.98M, but proceeds
        are only $39,840. Basis must track proceeds, so NO phantom loss."""
        self._security(sid='sec-mmf', ticker='TEST-MMF')
        basis, method, _ = invest_je.cost_basis_for_sell(
            self._txn('t', 'sell', 39840.0, qty=40000, price=99.60, sid='sec-mmf'),
            40000.0)
        self.assertEqual(method, 'no_basis')
        self.assertEqual(basis, 39840.0)        # proceeds, NOT 3,984,000


class ForceRegenTests(InvestJEBase):
    """v0.5.13 · a bulk-cancel in ERPNext leaves cancelled GJE rows that block
    their SecurityTransactions from ever regenerating. force=True + the
    reset helper clear the block; pending/approved stay protected."""

    def _cancelled_gje(self, itx):
        g = GeneratedJournalEntry(
            plaid_transaction_id=f'inv:{itx}', plaid_investment_transaction_id=itx,
            erpnext_journal_entry_name='ACC-JV-OLD', state='cancelled')
        db.session.add(g)
        db.session.commit()
        return g

    def test_force_false_skips_a_cancelled_gje(self):
        self._security()
        txn = self._txn('t', 'buy', 10000.0, qty=100, price=100.0)
        self._cancelled_gje('t')
        client = self._client()
        gje = invest_je.generate_investment_je(client, txn)      # force default
        self.assertEqual(gje.erpnext_journal_entry_name, 'ACC-JV-OLD')  # unchanged
        self.assertEqual(len(client.created['Journal Entry']), 0)       # no new JE

    def test_force_true_regenerates_over_a_cancelled_gje(self):
        self._security()
        txn = self._txn('t', 'buy', 10000.0, qty=100, price=100.0)
        self._cancelled_gje('t')
        client = self._client()
        gje = invest_je.generate_investment_je(client, txn, force=True)
        self.assertNotEqual(gje.erpnext_journal_entry_name, 'ACC-JV-OLD')  # new JE
        self.assertEqual(gje.state, 'pending_review')
        self.assertEqual(len(client.created['Journal Entry']), 1)
        # one GJE row for this txn, reused (not duplicated)
        self.assertEqual(GeneratedJournalEntry.query.filter_by(
            plaid_investment_transaction_id='t').count(), 1)

    def test_force_true_never_disturbs_pending_or_approved(self):
        self._security()
        txn = self._txn('t', 'buy', 10000.0, qty=100, price=100.0)
        g = GeneratedJournalEntry(
            plaid_transaction_id='inv:t', plaid_investment_transaction_id='t',
            erpnext_journal_entry_name='ACC-JV-LIVE', state='pending_review')
        db.session.add(g); db.session.commit()
        client = self._client()
        gje = invest_je.generate_investment_je(client, txn, force=True)
        self.assertEqual(gje.erpnext_journal_entry_name, 'ACC-JV-LIVE')  # untouched
        self.assertEqual(len(client.created['Journal Entry']), 0)

    def test_reset_cancelled_gjes_deletes_only_cancelled(self):
        self._security()
        self._cancelled_gje('a')
        db.session.add(GeneratedJournalEntry(
            plaid_transaction_id='inv:b', plaid_investment_transaction_id='b',
            erpnext_journal_entry_name='JE-B', state='pending_review'))
        db.session.commit()
        n = invest_je.reset_cancelled_gjes()
        self.assertEqual(n, 1)
        self.assertEqual(GeneratedJournalEntry.query.count(), 1)   # only the live one
        self.assertEqual(GeneratedJournalEntry.query.first().state, 'pending_review')


class BondBasisTests(InvestJEBase):
    """v0.5.14 · cost basis is amount÷quantity (universal), and a fixed-income
    maturity books accreted discount as Interest Income, not a capital gain.
    Synthetic T-bill only (no real CUSIP / issuer)."""

    INT = 'Interest Income - EC'
    GAINS = 'Realized Capital Gains - EC'
    FI = 'Fixed Income - EC'
    CLEARING = 'Cash Clearing - Brokerage - EC'

    def _bond(self, sid='sec-tbill'):
        s = Security(security_id=sid, ticker_symbol='',
                     name='US Treasury Bill (synthetic)', type='fixed income')
        db.session.add(s); db.session.commit()
        return s

    def _cycle(self, buy_itx, sell_itx, sid='sec-tbill'):
        c = TradedCycle(security_id=sid, buy_transaction_id=buy_itx,
                        sell_transaction_id=sell_itx,
                        buy_date=date(2026, 1, 1), buy_qty=1000000.0,
                        buy_price=99.0, cycle_status='complete')
        db.session.add(c); db.session.commit()
        return c

    def test_bond_basis_uses_amount_over_quantity_not_price(self):
        self._bond()
        # Synthetic bill: buy $1,000,000 face at 99.0 per $100 → paid $990,000.
        self._txn('b', 'buy', 990000.0, qty=1000000.0, price=99.0, sid='sec-tbill')
        # Partial sell of 10,000 face for $9,970.
        sell = self._txn('s', 'sell', -9970.0, qty=-10000.0, price=99.7,
                         sid='sec-tbill')
        self._cycle('b', 's')
        basis, method, _ = invest_je.cost_basis_for_sell(sell, 10000.0)
        self.assertEqual(method, 'specific_id')
        # amount/qty basis = 0.99 × 10,000 = $9,900 — NOT price×qty = $990,000.
        self.assertEqual(basis, 9900.0)
        self.assertLess(basis, 10000.0)         # never more than face

    def test_tbill_maturity_books_interest_income_not_gain(self):
        self._bond()
        self._txn('b', 'buy', 99000.0, qty=100000.0, price=99.0, sid='sec-tbill')
        # Matures at face: sell 100,000 for $100,000.
        sell = self._txn('s', 'sell', -100000.0, qty=-100000.0, price=100.0,
                         sid='sec-tbill')
        self._cycle('b', 's')
        client = self._client()
        gje = invest_je.generate_investment_je(client, sell)
        lines = self._lines(self._je_for(client, gje))
        self.assertEqual(lines[self.CLEARING], (100000.0, 0.0))   # DR Cash face
        self.assertEqual(lines[self.FI], (0.0, 99000.0))          # CR MS at cost
        self.assertEqual(lines[self.INT], (0.0, 1000.0))          # accreted → INTEREST
        self.assertNotIn(self.GAINS, lines)                       # not a capital gain

    def test_equity_gain_still_books_realized_gains(self):
        # Regression: an equity sale at a gain is NOT interest income.
        self._security()                        # type='equity'
        self._txn('b', 'buy', 8000.0, qty=100.0, price=80.0)
        sell = self._txn('s', 'sell', -12000.0, qty=-100.0, price=120.0)
        c = TradedCycle(security_id='sec-aapl', buy_transaction_id='b',
                        sell_transaction_id='s', buy_date=date(2026, 1, 1),
                        buy_qty=100.0, buy_price=80.0, cycle_status='complete')
        db.session.add(c); db.session.commit()
        client = self._client()
        gje = invest_je.generate_investment_je(client, sell)
        lines = self._lines(self._je_for(client, gje))
        self.assertEqual(lines[self.GAINS], (0.0, 4000.0))
        self.assertNotIn(self.INT, lines)


class ContributionRoutingTests(InvestJEBase):
    """v0.5.15 (Option A) · owner-contribution / member-distribution tags route
    the JE cash leg to 1099 Cash Clearing (netting the sec-side), with the
    equity offset (3200/3201) supplied by the operator's rule. Sweep routing
    needs no code — an operator rule with offset_account=1099 already works."""

    def _client_eq(self, **kw):
        chart = [
            {'account_name': 'Assets', 'root_type': 'Asset', 'is_group': 1,
             'parent_account': '', 'name': 'Assets - EC'},
            {'account_name': 'Income', 'root_type': 'Income', 'is_group': 1,
             'parent_account': '', 'name': 'Income - EC'},
            {'account_name': 'Expenses', 'root_type': 'Expense', 'is_group': 1,
             'parent_account': '', 'name': 'Expenses - EC'},
            {'account_name': 'Equity', 'root_type': 'Equity', 'is_group': 1,
             'parent_account': '', 'name': 'Equity - EC'},
        ]
        kw.setdefault('chart_accounts', chart)
        return FakeERPClient(**kw)

    def _row(self, tag, account_id='cash'):
        return type('R', (), {'bb_internal_tag': tag,
                              'account_id': account_id})()

    def test_member_contributions_account_is_created_under_equity(self):
        name = invest_je.member_contributions_account(self._client_eq(), COMPANY)
        self.assertIn('Member Contributions', name)

    def test_owner_contribution_routes_bank_leg_to_cash_clearing(self):
        from app import categorization
        leg = categorization._contribution_bank_leg(
            self._client_eq(), self._row('owner_contribution'), COMPANY)
        self.assertEqual(leg, 'Cash Clearing - Brokerage - EC')

    def test_member_distribution_also_routes(self):
        from app import categorization
        leg = categorization._contribution_bank_leg(
            self._client_eq(), self._row('member_distribution'), COMPANY)
        self.assertEqual(leg, 'Cash Clearing - Brokerage - EC')

    def test_untagged_row_keeps_its_normal_bank_gl(self):
        from app import categorization
        self.assertIsNone(categorization._contribution_bank_leg(
            self._client_eq(), self._row(''), COMPANY))

    def test_contribution_tag_on_an_unpaired_account_is_not_overridden(self):
        from app import categorization
        db.session.add(PlaidAccount(
            account_id='plain', item_id='item-om', name='PLAIN CHECKING',
            mask='7777', type='depository', subtype='checking',
            owning_company=COMPANY))
        db.session.commit()
        self.assertIsNone(categorization._contribution_bank_leg(
            self._client_eq(), self._row('owner_contribution', 'plain'),
            COMPANY))


class SyncFlowWiringTests(InvestJEBase):
    """v0.8.2 · the writer above has been complete since v0.5.1, but until this
    version no production code path ever called it — an Item with the kill
    switch ON mirrored every trade into `security_transactions` and posted
    exactly nothing. These pin the call site in `sync_engine.post_investment_jes`
    and both of its fail-soft guards, because a silent regression here is
    indistinguishable from "the operator forgot to opt in"."""

    def _plaid(self):
        return FakePlaidClient(accounts=[
            {'account_id': 'brk', 'name': 'BUSINESS BROKERAGE',
             'official_name': '', 'mask': '9401', 'type': 'investment',
             'subtype': 'brokerage', 'balance_available': None,
             'balance_current': 1000.0, 'iso_currency_code': 'USD'},
            {'account_id': 'cash', 'name': 'BROKERAGE CASH',
             'official_name': '', 'mask': '3194', 'type': 'depository',
             'subtype': 'checking', 'balance_available': None,
             'balance_current': 100.0, 'iso_currency_code': 'USD'},
        ])

    def _sync(self, poster, erp=NotImplemented):
        """Run a whole `sync_item` with the JE writer stubbed, so what is under
        test is the WIRING, not the writer (covered exhaustively above)."""
        from app import invest_je as ije, sync_engine
        erp = self._client() if erp is NotImplemented else erp
        with mock.patch.object(ije, 'post_investments_for_account', poster):
            return sync_engine.sync_item(self.item, self._plaid(), erp)

    def _recorder(self, seen):
        def post(client, account_id):
            seen.append(account_id)
            return {'posted': 1, 'skipped': 0, 'failed': 0}
        return post

    def test_sync_posts_investment_jes_when_the_flag_is_on(self):
        seen = []
        res = self._sync(self._recorder(seen))
        self.assertEqual(seen, ['brk'])
        self.assertEqual(res['invest_je']['posted'], 1)

    def test_the_depository_sibling_is_not_posted_for(self):
        """The paired cash account carries the BankTransaction half of a trade;
        posting it here would double-book against Cash Clearing."""
        seen = []
        self._sync(self._recorder(seen))
        self.assertNotIn('cash', seen)

    def test_sync_posts_nothing_when_the_flag_is_off(self):
        self.item.invest_je_posting_enabled = False
        db.session.commit()
        seen = []
        res = self._sync(self._recorder(seen))
        self.assertEqual(seen, [])
        for key in ('posted', 'skipped', 'failed'):
            self.assertEqual(res['invest_je'][key], 0, res['invest_je'])
        # v0.8.5 · and it SAYS why, with a code the caller can act on — a pass
        # that posted nothing because the switch is off must not look like a
        # pass that posted nothing because there was nothing to post. A NOTICE,
        # not an error: the switch being off is the designed default.
        self.assertEqual([e['code'] for e in res['invest_je']['notices']],
                         ['invest_je_posting_disabled'])
        self.assertEqual(res['invest_je']['errors'], [])

    def test_sync_posts_nothing_when_erpnext_is_not_configured(self):
        from app import sync_engine
        seen = []
        with mock.patch.object(sync_engine, 'get_erp_client_or_none',
                               lambda: None):
            self._sync(self._recorder(seen), erp=None)
        self.assertEqual(seen, [])

    def test_a_posting_failure_does_not_fail_the_sync(self):
        def boom(client, account_id):
            raise RuntimeError('ERPNext exploded')
        with self.assertLogs('bankbridge.sync', level='WARNING') as logs:
            res = self._sync(boom)
        self.assertTrue(any('invest_je posting failed' in m
                            for m in logs.output), logs.output)
        # The sync itself still completed and left the Item healthy.
        self.assertIn('pull', res)
        self.assertEqual(self.item.status, 'active')
        self.assertIsNone(self.item.last_error)

    def test_one_bad_account_does_not_block_the_others(self):
        for aid, mask in (('brk2', '9402'), ('brk3', '9403')):
            db.session.add(PlaidAccount(
                account_id=aid, item_id='item-om', name=f'BROKERAGE {mask}',
                mask=mask, type='investment', subtype='brokerage',
                owning_company=COMPANY))
        db.session.commit()
        seen = []

        def flaky(client, account_id):
            if account_id == 'brk2':
                raise RuntimeError('this one is broken')
            seen.append(account_id)
            return {'posted': 1, 'skipped': 0, 'failed': 0}

        with self.assertLogs('bankbridge.sync', level='WARNING'):
            res = self._sync(flaky)
        self.assertEqual(sorted(seen), ['brk', 'brk3'])
        self.assertEqual(res['invest_je']['posted'], 2)


CC = '200 - Investment Activities - EC'
MEMBER = 'MEM-0001'


class DimensionTaggingTests(InvestJEBase):
    """v0.8.3 · the per-Item Cost Center and Member every investment JE line
    carries.

    THE BUG. v0.8.2 wired the writer into the sync and 455 JEs landed with
    `cost_center = 'Main - OML'` — the Company default — on every line, and no
    Member. The 44 categorization rules that DO name a cost center only govern
    the bank-transaction path; this module had no dimension wiring at all. The
    account routing was always right (EEIIX → 1323 Mutual Funds, the advisory
    fee → 5700), so the drafts looked correct until you opened one.

    Pinned here: both dimensions land on EVERY line, blank stays blank (so
    ERPNext's own defaults still run), and a dimension ERPNext positively denies
    is dropped with a warning rather than failing a 455-transaction backfill."""

    def _tagged_client(self, cost_center=True, member=True):
        links = []
        if cost_center:
            links.append(('Cost Center', CC))
        if member:
            links.append(('Member', MEMBER))
        return self._client(link_docs=links)

    def _post_one(self, client):
        """Post a single buy and return its JE's account lines."""
        self._security()
        self._txn('t-buy', 'buy', 1000.0, qty=10, price=100.0)
        stats = invest_je.post_investments_for_account(client, 'brk')
        self.assertEqual(stats['posted'], 1, stats)
        gje = GeneratedJournalEntry.query.filter_by(
            plaid_investment_transaction_id='t-buy').first()
        return self._je_for(client, gje)['accounts']

    # ── the fix ──────────────────────────────────────────────────────────────
    def test_the_items_cost_center_lands_on_every_line(self):
        self.item.invest_je_cost_center = CC
        db.session.commit()
        lines = self._post_one(self._tagged_client())
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(line['cost_center'], CC, line)

    def test_the_items_member_lands_on_every_line(self):
        self.item.invest_je_member = MEMBER
        db.session.commit()
        lines = self._post_one(self._tagged_client())
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(line['member'], MEMBER, line)

    def test_both_at_once(self):
        self.item.invest_je_cost_center = CC
        self.item.invest_je_member = MEMBER
        db.session.commit()
        for line in self._post_one(self._tagged_client()):
            self.assertEqual((line['cost_center'], line['member']),
                             (CC, MEMBER), line)

    def test_a_sell_tags_the_gain_line_too(self):
        """Three lines, not two — the realized-gain line is as much investment
        activity as the two it balances."""
        self.item.invest_je_cost_center = CC
        db.session.commit()
        self._security()
        db.session.add(RetainedLot(
            security_id='sec-aapl', account_id='brk',
            purchase_date=date(2026, 1, 5), shares_original=10.0,
            shares_remaining=10.0, cost_basis_per_share=50.0))
        db.session.commit()
        self._txn('t-sell', 'sell', 1000.0, qty=10, price=100.0)
        client = self._tagged_client()
        stats = invest_je.post_investments_for_account(client, 'brk')
        self.assertEqual(stats['posted'], 1, stats)
        gje = GeneratedJournalEntry.query.filter_by(
            plaid_investment_transaction_id='t-sell').first()
        lines = self._je_for(client, gje)['accounts']
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertEqual(line['cost_center'], CC, line)

    # ── unset stays unset ────────────────────────────────────────────────────
    def test_neither_set_writes_no_key_at_all(self):
        """The pre-v0.8.3 shape, preserved exactly. An ABSENT key is what lets
        ERPNext apply the Account's or Company's own default server-side —
        writing a guessed value would override the very defaults it falls back
        to."""
        lines = self._post_one(self._tagged_client())
        for line in lines:
            self.assertNotIn('cost_center', line)
            self.assertNotIn('member', line)

    def test_a_blank_string_is_treated_as_unset(self):
        self.item.invest_je_cost_center = '   '
        self.item.invest_je_member = ''
        db.session.commit()
        for line in self._post_one(self._tagged_client()):
            self.assertNotIn('cost_center', line)
            self.assertNotIn('member', line)

    def test_the_column_defaults_to_null_on_a_fresh_item(self):
        """An upgrade must change nothing until an operator sets one."""
        it = PlaidItem(item_id='item-fresh',
                       access_token_encrypted=crypto.encrypt('x'))
        db.session.add(it)
        db.session.commit()
        self.assertIsNone(it.invest_je_cost_center)
        self.assertIsNone(it.invest_je_member)

    # ── fail-soft ────────────────────────────────────────────────────────────
    def test_a_cost_center_erpnext_denies_is_dropped_not_fatal(self):
        """One stale docname must not cost the operator all 455 JEs."""
        self.item.invest_je_cost_center = 'Typo - EC'
        db.session.commit()
        client = self._tagged_client()
        with self.assertLogs('bankbridge.invest_je', level='WARNING') as logs:
            lines = self._post_one(client)
        self.assertTrue(any('Typo - EC' in m for m in logs.output), logs.output)
        for line in lines:
            self.assertNotIn('cost_center', line)

    def test_a_member_erpnext_denies_is_dropped_not_fatal(self):
        self.item.invest_je_member = 'MEM-GONE'
        db.session.commit()
        client = self._tagged_client()
        with self.assertLogs('bankbridge.invest_je', level='WARNING'):
            lines = self._post_one(client)
        for line in lines:
            self.assertNotIn('member', line)

    def test_a_denied_cost_center_does_not_stop_the_member(self):
        self.item.invest_je_cost_center = 'Typo - EC'
        self.item.invest_je_member = MEMBER
        db.session.commit()
        client = self._tagged_client()
        with self.assertLogs('bankbridge.invest_je', level='WARNING'):
            lines = self._post_one(client)
        for line in lines:
            self.assertNotIn('cost_center', line)
            self.assertEqual(line['member'], MEMBER)

    def test_an_unreachable_erpnext_writes_the_value_anyway(self):
        """NO VERDICT is not a denial. Refusing valid input during a transient
        outage is the worse failure — and ERPNext rejects a genuinely bad link
        at create time regardless."""
        self.item.invest_je_cost_center = CC
        db.session.commit()
        client = self._tagged_client()

        def boom(doctype, name):
            if doctype in ('Cost Center', 'Member'):
                raise ERPNextAPIError('ERPNext is down', status_code=500)
            return None
        with mock.patch.object(client, 'get_doc', boom):
            tags = invest_je.resolve_je_tags(client, self.brk, COMPANY)
        self.assertEqual(tags, {'cost_center': CC})

    # ── the lookup is resolved once per batch, not once per trade ────────────
    def test_a_batch_validates_the_dimensions_once(self):
        self.item.invest_je_cost_center = CC
        db.session.commit()
        self._security()
        for i in range(5):
            self._txn(f't-{i}', 'buy', 100.0 * (i + 1), qty=1, price=100.0)
        client = self._tagged_client()
        stats = invest_je.post_investments_for_account(client, 'brk')
        self.assertEqual(stats['posted'], 5)
        probes = [c for c in client.calls
                  if c[0] == 'get_doc' and c[1] == 'Cost Center']
        self.assertEqual(len(probes), 1, probes)

    # ── end to end through the sync ──────────────────────────────────────────
    def test_a_whole_sync_posts_with_the_dimensions_set(self):
        """posting_enabled + a cost center → sync_item lands tagged JEs. The
        wiring (v0.8.2) and the tagging (v0.8.3) working together is the thing
        an operator actually observes."""
        from app import sync_engine
        self.item.invest_je_cost_center = CC
        self.item.invest_je_member = MEMBER
        db.session.commit()
        self._security()
        self._txn('t-buy', 'buy', 1000.0, qty=10, price=100.0)
        client = self._tagged_client()
        plaid = FakePlaidClient(accounts=[
            {'account_id': 'brk', 'name': 'BUSINESS BROKERAGE',
             'official_name': '', 'mask': '9401', 'type': 'investment',
             'subtype': 'brokerage', 'balance_available': None,
             'balance_current': 1000.0, 'iso_currency_code': 'USD'}])
        res = sync_engine.sync_item(self.item, plaid, client)
        self.assertEqual(res['invest_je']['posted'], 1, res['invest_je'])
        gje = GeneratedJournalEntry.query.filter_by(
            plaid_investment_transaction_id='t-buy').first()
        for line in self._je_for(client, gje)['accounts']:
            self.assertEqual((line['cost_center'], line['member']),
                             (CC, MEMBER), line)


class InvestJEConfigUITests(InvestJEBase):
    """The /admin/accounts form that sets the two dimensions (v0.8.3)."""

    def test_the_endpoint_stores_both(self):
        resp = self.app.test_client().post(
            '/admin/items/item-om/invest_je_config',
            data={'invest_je_cost_center': CC, 'invest_je_member': MEMBER})
        self.assertEqual(resp.status_code, 302)
        db.session.expire_all()
        self.assertEqual(self.item.invest_je_cost_center, CC)
        self.assertEqual(self.item.invest_je_member, MEMBER)

    def test_blank_clears_back_to_null(self):
        """Clearing is a first-class outcome, not an ignored empty form."""
        self.item.invest_je_cost_center = CC
        self.item.invest_je_member = MEMBER
        db.session.commit()
        self.app.test_client().post(
            '/admin/items/item-om/invest_je_config',
            data={'invest_je_cost_center': '', 'invest_je_member': ''})
        db.session.expire_all()
        self.assertIsNone(self.item.invest_je_cost_center)
        self.assertIsNone(self.item.invest_je_member)

    def test_an_unknown_item_does_not_500(self):
        resp = self.app.test_client().post(
            '/admin/items/nope/invest_je_config',
            data={'invest_je_cost_center': CC})
        self.assertEqual(resp.status_code, 302)

    def test_the_accounts_page_renders_the_picker(self):
        """A new context key that collides with a _page() kwarg 500s every
        admin page — so the route is GET-tested, not just the form."""
        self.item.invest_je_cost_center = CC
        db.session.commit()
        resp = self.app.test_client().get('/admin/accounts')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('invest_je_config', body)
        self.assertIn(CC, body)

    def test_the_page_still_renders_when_erpnext_cannot_list_options(self):
        with mock.patch('app.erpnext_bank.list_cost_centers',
                        side_effect=ERPNextError('down')), \
             mock.patch('app.erpnext_bank.list_link_options',
                        side_effect=ERPNextError('down')):
            resp = self.app.test_client().get('/admin/accounts')
        self.assertEqual(resp.status_code, 200)
        # Falls back to a free-text field rather than locking the operator out.
        self.assertIn('name="invest_je_cost_center"',
                      resp.get_data(as_text=True))


class ResetInvestmentDraftsTests(InvestJEBase):
    """v0.8.3 · deleting the drafts so a re-sync rebuilds them with dimensions.

    A Journal Entry's cost center cannot be edited in bulk and the GJE row makes
    the trade idempotently "already handled", so clearing both is the only route
    from 455 wrongly-tagged drafts to 455 right ones."""

    def _post_two(self, client):
        self._security()
        self._txn('t-buy', 'buy', 1000.0, qty=10, price=100.0)
        self._txn('t-fee', 'fee', 25.0)
        invest_je.post_investments_for_account(client, 'brk')
        self.assertEqual(GeneratedJournalEntry.query.count(), 2)

    def test_drafts_are_deleted_here_and_in_erpnext(self):
        client = self._client()
        self._post_two(client)
        names = {g.erpnext_journal_entry_name
                 for g in GeneratedJournalEntry.query.all()}
        stats = invest_je.reset_investment_drafts(client)
        self.assertEqual(stats['drafts_deleted'], 2)
        self.assertFalse(stats['aborted'])
        self.assertEqual(GeneratedJournalEntry.query.count(), 0)
        self.assertTrue(names <= client.deleted, client.deleted)

    def test_a_re_post_rebuilds_them_with_the_dimensions(self):
        """The whole point: reset, set the cost center, re-sync, tagged JEs."""
        client = self._client(link_docs=[('Cost Center', CC)])
        self._post_two(client)
        invest_je.reset_investment_drafts(client)
        self.item.invest_je_cost_center = CC
        db.session.commit()
        stats = invest_je.post_investments_for_account(client, 'brk')
        self.assertEqual(stats['posted'], 2)
        for gje in GeneratedJournalEntry.query.all():
            for line in self._je_for(client, gje)['accounts']:
                self.assertEqual(line['cost_center'], CC, line)

    def test_a_submitted_entry_aborts_the_whole_pass(self):
        """Submitted entries are real ledger history — the first one found
        stops everything, having deleted nothing further."""
        client = self._client()
        self._post_two(client)
        first = GeneratedJournalEntry.query.order_by(
            GeneratedJournalEntry.id.asc()).first()
        client.created['Journal Entry'][
            first.erpnext_journal_entry_name]['docstatus'] = 1
        stats = invest_je.reset_investment_drafts(client)
        self.assertTrue(stats['aborted'])
        self.assertEqual(stats['drafts_deleted'], 0)
        self.assertEqual(GeneratedJournalEntry.query.count(), 2)
        self.assertEqual(client.deleted, set())

    def test_an_approved_row_is_never_touched(self):
        client = self._client()
        self._post_two(client)
        gje = GeneratedJournalEntry.query.first()
        gje.state = 'approved'
        db.session.commit()
        stats = invest_je.reset_investment_drafts(client)
        self.assertEqual(stats['drafts_deleted'], 1)
        self.assertEqual(GeneratedJournalEntry.query.count(), 1)
        self.assertEqual(GeneratedJournalEntry.query.first().state, 'approved')

    def test_it_is_idempotent(self):
        client = self._client()
        self._post_two(client)
        invest_je.reset_investment_drafts(client)
        self.assertEqual(
            invest_je.reset_investment_drafts(client),
            {'drafts_deleted': 0, 'tracker_deleted': 0, 'orphan_deleted': 0,
             'total_deleted': 0, 'aborted': False, 'reason': ''})

    def test_it_can_be_scoped_to_one_account(self):
        db.session.add(PlaidAccount(
            account_id='brk2', item_id='item-om', name='OTHER BROKERAGE',
            mask='9402', type='investment', subtype='brokerage',
            owning_company=COMPANY, erpnext_gl_account_name='Other - EC'))
        db.session.commit()
        client = self._client()
        self._post_two(client)
        self._txn('t-other', 'buy', 500.0, qty=5, price=100.0,
                  account_id='brk2')
        invest_je.post_investments_for_account(client, 'brk2')
        self.assertEqual(GeneratedJournalEntry.query.count(), 3)
        stats = invest_je.reset_investment_drafts(client, 'brk2')
        self.assertEqual(stats['drafts_deleted'], 1)
        self.assertEqual(GeneratedJournalEntry.query.count(), 2)

    def test_the_admin_endpoint_reports_the_count(self):
        client = self._client()
        self._post_two(client)
        with mock.patch('app.erpnext_bank.get_client', lambda: client):
            resp = self.app.test_client().post('/admin/reset_investment_drafts')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('2+draft', resp.headers['Location'])
        self.assertEqual(GeneratedJournalEntry.query.count(), 0)

    def test_the_rebuild_endpoint_still_deletes_drafts(self):
        """rebuild_investment_accounts now delegates its draft pass to
        reset_investment_drafts — it must not have lost the behaviour."""
        client = self._client()
        self._post_two(client)
        stats = invest_je.rebuild_investment_accounts(client, COMPANY)
        self.assertEqual(stats['drafts_deleted'], 2)
        self.assertEqual(GeneratedJournalEntry.query.count(), 0)

    def test_the_rebuild_endpoint_still_aborts_on_a_submitted_entry(self):
        client = self._client()
        self._post_two(client)
        first = GeneratedJournalEntry.query.first()
        client.created['Journal Entry'][
            first.erpnext_journal_entry_name]['docstatus'] = 1
        stats = invest_je.rebuild_investment_accounts(client, COMPANY)
        self.assertTrue(stats['aborted'])
        self.assertEqual(GeneratedJournalEntry.query.count(), 2)


class InvestJEDimensionMigrationTests(unittest.TestCase):
    """The two ADD COLUMNs, on a database that predates them."""

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

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _columns(self):
        from sqlalchemy import inspect as sa_inspect
        return {c['name'] for c in
                sa_inspect(db.engine).get_columns('plaid_items')}

    def test_a_fresh_database_has_both(self):
        self.assertLessEqual({'invest_je_cost_center', 'invest_je_member'},
                             self._columns())

    def test_the_migration_adds_them_to_a_pre_v0_8_3_database(self):
        from app import migrations
        from sqlalchemy import text
        with db.engine.begin() as conn:
            for col in ('invest_je_cost_center', 'invest_je_member'):
                conn.execute(text(
                    f'ALTER TABLE plaid_items DROP COLUMN {col}'))
        self.assertFalse({'invest_je_cost_center', 'invest_je_member'}
                         & self._columns())
        migrations.run_migrations()
        self.assertLessEqual({'invest_je_cost_center', 'invest_je_member'},
                             self._columns())

    def test_re_running_is_a_no_op(self):
        from app import migrations
        migrations.run_migrations()
        migrations.run_migrations()
        self.assertLessEqual({'invest_je_cost_center', 'invest_je_member'},
                             self._columns())
