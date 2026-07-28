# SPDX-License-Identifier: MIT
"""Investment reconciliation — mark-to-market and the investments pull (v0.8.0).

Two problems, one release.

THE FIRST is that every statement period on both brokerage accounts reported
**Movement: 0.00**, which read as "Plaid returned no activity" when the truth was
that `/investments/transactions/get` was never called. It was never called
because `sync_investments_for_item` returned early whenever
`/investments/holdings/get` answered `{}` — one endpoint silencing an
independent one. Covered here: an Item whose holdings call comes back empty
still gets its transactions pulled, still populates `security_transactions`, and
still produces a non-zero Movement on the anchor.

THE SECOND is that a brokerage's TOTAL ACCOUNT VALUE moves for a reason the
books have no column for — the market. Covered here: the portfolio bridge
(`portfolio_opening`, `portfolio_closing`, `security_flow_sum`,
`mark_to_market_delta`) decomposes it, prefers the bank's own stated securities
figures over our mirror, and — the load-bearing assertion of the whole feature —
**leaves `variance` bit-identical**. A market move does not move cash, so a
repricing must never be able to make a cash period look unreconciled.

Synthetic accounts, masks and tickers only — no real account data.

    cd app
    python3 -m unittest discover -s tests -v
"""
from datetime import date

from app import db
from app import investments, statements as stmts
from app.models import (PlaidAccount, PlaidStatement, Security,
                        SecurityTransaction, StatementAnchor)

from tests.test_statements import StatementsBase


class FakeInvestClient:
    """The two /investments/* methods `sync_investments_for_item` calls.

    Both arguments distinguish UNAVAILABLE from EMPTY, exactly as the real
    wrappers do and as this release depends on:

      * None  → the endpoint answered nothing at all. `plaid_client` logs and
                returns `{}` for an Item without the `investments` product, and
                a `{}` from holdings is the precise condition that used to
                abort the whole pull.
      * []    → the endpoint answered normally with no rows in the window.

    A fake that collapsed the two would make the decoupling untestable, since
    the entire bug is one being mistaken for the other."""
    def __init__(self, holdings=None, transactions=None, securities=None):
        self.holdings_payload = holdings
        self.transactions = transactions
        self.securities = list(securities or [])
        self.transactions_calls = 0
        self.holdings_calls = 0

    def investments_holdings_get(self, access_token):
        self.holdings_calls += 1
        return self.holdings_payload or {}

    def investments_transactions_get(self, access_token, start_date=None,
                                     end_date=None, count=None, offset=0):
        self.transactions_calls += 1
        if self.transactions is None:
            return {}
        if offset:
            return {'investment_transactions': [], 'securities': [],
                    'total_transactions': len(self.transactions)}
        return {'investment_transactions': self.transactions,
                'securities': self.securities,
                'total_transactions': len(self.transactions)}


class InvestReconBase(StatementsBase):
    """A brokerage account with one statement period and the securities feed."""

    def _brokerage(self, account_id='brk-1', mask='9401', paired=None):
        a = PlaidAccount(
            account_id=account_id, item_id='item-abc',
            name='TEST BROKERAGE', mask=mask, type='investment',
            subtype='brokerage', balance_current=4200.0,
            iso_currency_code='USD',
            erpnext_gl_account_name='Marketable Securities - EC',
            paired_account_id=paired, import_status='imported')
        db.session.add(a)
        db.session.commit()
        return a

    def _brokerage_statement(self, account_id='brk-1', *, month=3, year=2026,
                             cash_opening=5000.0, cash_closing=4200.0,
                             portfolio_opening=15000.0,
                             portfolio_closing=15500.0,
                             securities_purchased=None,
                             securities_sold=None,
                             statement_id='st-brk-1'):
        start, end = stmts.period_bounds(month, year)
        meta = {'cash_opening': cash_opening, 'cash_closing': cash_closing,
                'parser_version': 'test-1', 'layout': 'wf_advisors'}
        if securities_purchased is not None:
            meta['securities_purchased'] = securities_purchased
        if securities_sold is not None:
            meta['securities_sold'] = securities_sold
        st = PlaidStatement(
            statement_id=statement_id, plaid_item_id='item-abc',
            plaid_account_id=account_id, period_start=start, period_end=end,
            opening_balance=cash_opening, closing_balance=cash_closing,
            portfolio_opening_value=portfolio_opening,
            portfolio_closing_value=portfolio_closing,
            parse_method='wf_advisors', parsed_metadata=meta)
        db.session.add(st)
        db.session.commit()
        return st

    def _security(self, security_id='sec-aapl', ticker='TEST-AAPL'):
        s = Security(security_id=security_id, ticker_symbol=ticker,
                     name=f'{ticker} Inc', type='equity')
        db.session.add(s)
        db.session.commit()
        return s

    def _sec_txn(self, txn_id, account_id='brk-1', amount=0.0, when=None,
                 type_='buy', subtype='buy', quantity=0.0, price=0.0,
                 security_id='sec-aapl'):
        """One SecurityTransaction. `amount` is PLAID's convention: positive
        means cash LEFT the account (a buy)."""
        t = SecurityTransaction(
            plaid_investment_transaction_id=txn_id, account_id=account_id,
            security_id=security_id, date=when or date(2026, 3, 10),
            name=f'{type_} {quantity}', quantity=quantity, amount=amount,
            price=price, type=type_, subtype=subtype)
        db.session.add(t)
        db.session.commit()
        return t

    def _anchor_for(self, account_id='brk-1'):
        stmts.rebuild_statement_anchors(account_id)
        return (StatementAnchor.query
                .filter_by(account_id=account_id).one())


# ── the arithmetic, on its own ──────────────────────────────────────────────
class MarkToMarketArithmeticTest(InvestReconBase):
    """`mark_to_market_delta` solves total-value change for the price term. Each
    case below is a movement whose correct answer is known by construction."""

    def test_a_buy_at_flat_prices_is_not_market_movement(self):
        """$1,000 of cash becomes $1,000 of stock. Total value is unchanged and
        nothing was repriced — the most important case to get right, because
        getting it wrong would report every purchase as a gain."""
        self.assertEqual(0.0, stmts.mark_to_market_delta(
            cash_opening=5000.0, cash_closing=4000.0,
            portfolio_opening=15000.0, portfolio_closing=15000.0,
            security_flow=-1000.0))

    def test_a_price_rise_with_no_trades_is_all_market_movement(self):
        self.assertEqual(200.0, stmts.mark_to_market_delta(
            cash_opening=5000.0, cash_closing=5000.0,
            portfolio_opening=15000.0, portfolio_closing=15200.0,
            security_flow=0.0))

    def test_a_cash_dividend_is_income_not_appreciation(self):
        """Cash and total value both rise $50 and no position was repriced."""
        self.assertEqual(0.0, stmts.mark_to_market_delta(
            cash_opening=5000.0, cash_closing=5050.0,
            portfolio_opening=15000.0, portfolio_closing=15050.0,
            security_flow=0.0))

    def test_a_sale_at_the_carrying_price_is_not_market_movement(self):
        self.assertEqual(0.0, stmts.mark_to_market_delta(
            cash_opening=5000.0, cash_closing=6000.0,
            portfolio_opening=15000.0, portfolio_closing=15000.0,
            security_flow=1000.0))

    def test_a_deposit_of_cash_is_not_market_movement(self):
        """$10,000 arrives and sits in cash — the shape of the land-sale
        proceeds landing on ••9401, which must not be reported as a gain."""
        self.assertEqual(0.0, stmts.mark_to_market_delta(
            cash_opening=5000.0, cash_closing=15000.0,
            portfolio_opening=15000.0, portfolio_closing=25000.0,
            security_flow=0.0))

    def test_it_declines_to_answer_without_portfolio_figures(self):
        """A depository statement states no total account value. None, not
        0.0 — 'no securities to reprice' is not 'prices did not move'."""
        self.assertIsNone(stmts.mark_to_market_delta(
            cash_opening=5000.0, cash_closing=4000.0,
            portfolio_opening=None, portfolio_closing=None,
            security_flow=0.0))

    def test_it_declines_to_answer_without_cash_figures(self):
        self.assertIsNone(stmts.mark_to_market_delta(
            cash_opening=None, cash_closing=None,
            portfolio_opening=15000.0, portfolio_closing=15200.0,
            security_flow=0.0))


# ── where the securities flow comes from ────────────────────────────────────
class SecurityFlowSourceTest(InvestReconBase):

    def test_the_statements_own_figures_win_over_the_mirror(self):
        """The bank asserts 'Securities purchased' and 'Securities sold and
        redeemed' per period. Those beat summing our own feed, because the feed
        is only as complete as the investments pull — which on the Item that
        prompted this release was completely empty."""
        acct = self._brokerage()
        st = self._brokerage_statement(securities_purchased=-4000.0,
                                       securities_sold=1500.0)
        self._security()
        # A mirror that disagrees, to prove which source is consulted.
        self._sec_txn('inv-1', amount=1000.0, quantity=10, price=100.0)
        flow, source = stmts.security_flow_for_period(
            st, acct, st.period_start, st.period_end)
        self.assertEqual('statement', source)
        self.assertEqual(-2500.0, flow)

    def test_it_falls_back_to_the_mirror_when_the_statement_says_nothing(self):
        acct = self._brokerage()
        st = self._brokerage_statement()          # no securities_* keys
        self._security()
        self._sec_txn('inv-1', amount=1000.0, quantity=10, price=100.0)
        flow, source = stmts.security_flow_for_period(
            st, acct, st.period_start, st.period_end)
        self.assertEqual('mirror', source)
        # Plaid's +1000 (cash out) becomes -1000 cash-in-positive.
        self.assertEqual(-1000.0, flow)

    def test_the_mirror_counts_only_buys_and_sells(self):
        """A dividend and a fee move cash without converting it into a
        position. Counting them would charge the difference to the market."""
        acct = self._brokerage()
        st = self._brokerage_statement()
        self._security()
        self._sec_txn('inv-1', amount=1000.0, quantity=10, price=100.0)
        self._sec_txn('inv-2', amount=-200.0, type_='cash', subtype='dividend')
        self._sec_txn('inv-3', amount=35.0, type_='fee', subtype='advisory fee')
        flow, _ = stmts.security_flow_for_period(
            st, acct, st.period_start, st.period_end)
        self.assertEqual(-1000.0, flow)


# ── the anchor, end to end ──────────────────────────────────────────────────
class AnchorPortfolioBridgeTest(InvestReconBase):

    def _seed_a_full_period(self):
        """Cash 5,000 → 4,200 via a $1,000 buy and a $200 dividend; securities
        10,000 → 11,300 (the $1,000 bought, plus $300 the market added); total
        account value 15,000 → 15,500. Every figure is forced, so the only
        unknown is the one under test."""
        acct = self._brokerage()
        st = self._brokerage_statement()
        self._security()
        self._sec_txn('inv-buy', amount=1000.0, quantity=10, price=100.0,
                      when=date(2026, 3, 10))
        self._sec_txn('inv-div', amount=-200.0, type_='cash',
                      subtype='dividend', when=date(2026, 3, 20))
        return acct, st

    def test_it_decomposes_a_period_with_both_a_trade_and_a_price_change(self):
        self._seed_a_full_period()
        anchor = self._anchor_for()
        self.assertEqual(15000.0, anchor.portfolio_opening)
        self.assertEqual(15500.0, anchor.portfolio_closing)
        self.assertEqual(500.0, anchor.portfolio_delta())
        self.assertEqual(-1000.0, anchor.security_flow_sum)
        self.assertEqual(300.0, anchor.mark_to_market_delta)

    def test_the_cash_identity_is_untouched_by_the_bridge(self):
        """THE ASSERTION THIS WHOLE FEATURE TURNS ON. Cash reconciles exactly:
        5,000 - 1,000 + 200 = 4,200. The market added $300 in the same period,
        and `variance` must still be 0.00 — folding the repricing into
        computed_closing (as the sprint brief originally proposed) would report
        a period that balances perfectly as $300 out."""
        self._seed_a_full_period()
        anchor = self._anchor_for()
        self.assertEqual(5000.0, anchor.anchored_opening)
        self.assertEqual(-800.0, anchor.transaction_sum)
        self.assertEqual(4200.0, anchor.computed_closing)
        self.assertEqual(0.0, anchor.variance)
        self.assertTrue(anchor.reconciles())
        # …and the explanation sits beside it, not inside it.
        self.assertEqual(300.0, anchor.mark_to_market_delta)

    def test_a_period_that_does_not_reconcile_still_measures_the_market(self):
        """••9401's February: the bank's cash closing jumped by a deposit Plaid
        never returned. The cash variance is the finding; the market movement
        must be measured correctly ANYWAY, from the statement's own figures,
        so the two findings stay separable."""
        acct = self._brokerage()
        self._brokerage_statement(cash_opening=5000.0, cash_closing=15000.0,
                                  portfolio_opening=15000.0,
                                  portfolio_closing=25200.0)
        anchor = self._anchor_for()
        self.assertEqual(0.0, anchor.transaction_sum)     # Plaid sent nothing
        self.assertEqual(10000.0, anchor.variance)        # the missing deposit
        self.assertFalse(anchor.reconciles())
        # (25200-15000) - (15000-5000) + 0 = 200 of actual appreciation.
        self.assertEqual(200.0, anchor.mark_to_market_delta)

    def test_a_depository_account_gets_no_market_movement_at_all(self):
        """No securities to reprice. NULL, and `reconciles()` unaffected."""
        self._account()                        # depository/checking
        self._statement(opening=17600.0, closing=17650.0)
        self._txn('t-1', amount=-50.0, when=date(2026, 7, 5))
        anchor = self._anchor_for('acct-1')
        self.assertIsNone(anchor.mark_to_market_delta)
        self.assertIsNone(anchor.portfolio_opening)
        self.assertEqual(0.0, anchor.security_flow_sum)
        self.assertEqual(0.0, anchor.variance)
        self.assertTrue(anchor.reconciles())

    def test_reconciles_never_consults_the_market_column(self):
        """Belt and braces on the separation: a period whose cash is off stays
        unreconciled no matter how large the market move beside it."""
        anchor = StatementAnchor(account_id='x', statement_id=1,
                                 anchored_opening=1.0, anchored_closing=2.0,
                                 transaction_sum=0.0, computed_closing=1.0,
                                 variance=1.0, mark_to_market_delta=-1.0)
        self.assertFalse(anchor.reconciles())

    def test_the_bridge_survives_a_rebuild(self):
        """`rebuild_statement_anchors` is re-run after every parser upgrade;
        the new columns have to be written on the update path too, not only on
        the insert."""
        self._seed_a_full_period()
        self._anchor_for()
        stmts.rebuild_statement_anchors('brk-1')
        anchor = StatementAnchor.query.filter_by(account_id='brk-1').one()
        self.assertEqual(300.0, anchor.mark_to_market_delta)

    def test_it_is_exposed_over_mcp_and_in_to_dict(self):
        self._seed_a_full_period()
        d = self._anchor_for().to_dict()
        self.assertEqual(300.0, d['mark_to_market_delta'])
        self.assertEqual(-1000.0, d['security_flow_sum'])
        self.assertEqual(500.0, d['portfolio_delta'])
        self.assertEqual(0.0, d['variance'])


# ── the pull that never happened ────────────────────────────────────────────
class InvestmentsPullDecouplingTest(InvestReconBase):
    """The regression that produced Movement 0.00 on thirteen periods across
    two accounts."""

    def _txn_payload(self, tid='inv-1', amount=1000.0, when='2026-03-10',
                     type_='buy'):
        return {'investment_transaction_id': tid, 'account_id': 'brk-1',
                'security_id': 'sec-aapl', 'date': when, 'name': 'BUY TEST',
                'quantity': 10.0, 'amount': amount, 'price': 100.0,
                'fees': 0.0, 'type': type_, 'subtype': type_,
                'iso_currency_code': 'USD'}

    def test_an_empty_holdings_response_no_longer_aborts_the_pull(self):
        self._brokerage()
        client = FakeInvestClient(holdings=None,
                                  transactions=[self._txn_payload()],
                                  securities=[{'security_id': 'sec-aapl',
                                               'ticker_symbol': 'TEST-AAPL',
                                               'name': 'Test Inc',
                                               'type': 'equity'}])
        stats = investments.sync_investments_for_item(self.item, client,
                                                      'access-x')
        self.assertEqual(1, client.transactions_calls,
                         'the transactions endpoint must be called even when '
                         'holdings answers nothing')
        self.assertEqual(1, stats['txns_added'])
        self.assertIsNone(stats['skipped'])
        self.assertTrue(stats['holdings_skipped'])
        self.assertIsNone(stats['txns_skipped'])
        self.assertEqual(1, SecurityTransaction.query.count())

    def test_the_movement_column_stops_reading_zero(self):
        """The whole point, measured where the operator sees it: with the
        trade ingested, the anchor's transaction_sum is no longer 0.00."""
        self._brokerage()
        self._brokerage_statement()
        client = FakeInvestClient(holdings=None,
                                  transactions=[self._txn_payload()],
                                  securities=[{'security_id': 'sec-aapl',
                                               'ticker_symbol': 'TEST-AAPL'}])
        investments.sync_investments_for_item(self.item, client, 'access-x')
        anchor = self._anchor_for()
        self.assertEqual(-1000.0, anchor.transaction_sum)

    def test_both_endpoints_declining_is_still_reported_as_unavailable(self):
        """The pre-v0.8.0 message survives for the case it actually described:
        an Item that was never granted the `investments` product."""
        self._brokerage()
        client = FakeInvestClient(holdings=None, transactions=None)
        stats = investments.sync_investments_for_item(self.item, client,
                                                      'access-x')
        self.assertIn('investments product unavailable', stats['skipped'])
        self.assertTrue(stats['holdings_skipped'])
        self.assertTrue(stats['txns_skipped'])
        self.assertIsNone(self.item.investments_synced_at,
                          'a pull that reached neither endpoint must not stamp '
                          'a successful sync time, or the next pull narrows '
                          'its window on the strength of a call that failed')

    def test_an_available_but_empty_window_is_not_reported_as_unavailable(self):
        """The distinction the old code could not draw. An account that simply
        did not trade this month is a successful sync — it stamps
        investments_synced_at so the next pull can narrow its window — and it
        must not be labelled with the product-missing message, which would send
        an operator to the Plaid dashboard to fix nothing."""
        self._brokerage()
        client = FakeInvestClient(holdings=None, transactions=[])
        stats = investments.sync_investments_for_item(self.item, client,
                                                      'access-x')
        self.assertIsNone(stats['skipped'])
        self.assertIsNone(stats['txns_skipped'])
        self.assertEqual(0, stats['txns_added'])
        self.assertIsNotNone(self.item.investments_synced_at)

    def test_an_account_with_holdings_and_transactions_still_gets_both(self):
        """The unchanged happy path — the decoupling must not cost the case
        that already worked."""
        self._brokerage()
        client = FakeInvestClient(
            holdings={'holdings': [{'account_id': 'brk-1',
                                    'security_id': 'sec-aapl',
                                    'quantity': 10.0,
                                    'institution_price': 130.0,
                                    'institution_value': 1300.0}],
                      'securities': [{'security_id': 'sec-aapl',
                                      'ticker_symbol': 'TEST-AAPL'}]},
            transactions=[self._txn_payload()])
        stats = investments.sync_investments_for_item(self.item, client,
                                                      'access-x')
        self.assertEqual(1, stats['holdings'])
        self.assertEqual(1, stats['txns_added'])
        self.assertIsNone(stats['skipped'])
        self.assertIsNone(stats['holdings_skipped'])
        self.assertIsNotNone(self.item.investments_synced_at)

    def test_an_item_with_no_investment_accounts_is_still_skipped_cheaply(self):
        self._account()                        # depository only
        client = FakeInvestClient(transactions=[self._txn_payload()])
        stats = investments.sync_investments_for_item(self.item, client,
                                                      'access-x')
        self.assertEqual('no investment accounts on this Item',
                         stats['skipped'])
        self.assertEqual(0, client.holdings_calls)
        self.assertEqual(0, client.transactions_calls)

    def test_one_stated_figure_is_enough_to_prefer_the_statement(self):
        """A month with buying and no selling prints one line and omits the
        other. Requiring both would discard a good bank figure whenever the
        account traded one way — which on a Buy-5-Sell-4 book is most months."""
        acct = self._brokerage()
        st = self._brokerage_statement(securities_purchased=-4000.0)
        self._security()
        self._sec_txn('inv-1', amount=1000.0, quantity=10, price=100.0)
        flow, source = stmts.security_flow_for_period(
            st, acct, st.period_start, st.period_end)
        self.assertEqual('statement', source)
        self.assertEqual(-4000.0, flow)
