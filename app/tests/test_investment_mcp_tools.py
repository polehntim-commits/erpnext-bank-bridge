# SPDX-License-Identifier: MIT
"""The investment reconciliation MCP toolkit (v0.8.0).

Seven read-only tools that between them let a brokerage period be diagnosed
end-to-end over MCP, without anyone relaying a screenshot of /admin/statements:
what statements exist, what the PDF says, what the parser got out of it, what
Plaid returned on each of its two feeds, what the account holds, and which
trades are missing a leg.

All seven are read-only, so none carries a kill switch — the gate on this
surface is the bearer token, and a tool that cannot write cannot be the thing
that breaks a period. That is asserted, not assumed.

Beyond the happy paths, the cases covered here are the ones where a plausible
implementation would mislead rather than fail:

  * an empty investment feed is AMBIGUOUS — "no trades" and "never pulled" look
    identical — so the answer ships `investments_ever_synced` to settle it;
  * `list_holdings(as_of=…)` cannot return history (holdings are stored as a
    snapshot), and says so instead of passing off today's positions as
    February's;
  * `list_plaid_transactions` on a paired brokerage searches the CASH COMPANION,
    because the brokerage itself holds zero bank transactions by construction
    and an empty array would read as "Plaid sent nothing";
  * `list_unpaired_trades` refuses an unpaired account rather than returning an
    empty list that looks like a clean bill of health.

Synthetic masks, accounts and tickers only — no real account data.

    cd app
    python3 -m unittest discover -s tests -v
"""
import base64
import json
from datetime import date

from app import db
from app import statements as stmts
from app.models import (BankTransaction, PlaidAccount, PlaidStatement,
                        Security, SecurityHolding, SecurityTransaction)

from tests.test_mcp_server import McpBase


class InvestToolBase(McpBase):
    """A paired brokerage with a statement, a PDF, a parse, both feeds and a
    position — everything the seven tools read."""

    def setUp(self):
        super().setUp()
        self.brokerage = PlaidAccount(
            account_id='brk-1', item_id=self.item.item_id,
            name='TEST BROKERAGE', mask='9401', type='investment',
            subtype='brokerage', paired_account_id='cash-1',
            import_status='imported')
        self.cash = PlaidAccount(
            account_id='cash-1', item_id=self.item.item_id,
            name='BROKERAGE CASH SERVICES', mask='9402', type='depository',
            subtype='checking', import_status='imported')
        db.session.add_all([self.brokerage, self.cash])
        db.session.add(Security(security_id='sec-aapl',
                                ticker_symbol='TEST-AAPL', name='Test Inc',
                                type='equity'))
        db.session.commit()

    def _ok(self, name, args=None):
        """Call a tool and return its parsed JSON result, asserting success."""
        _resp, body = self._call_tool(name, args or {})
        result = body['result']
        self.assertFalse(result['isError'],
                         f'{name} failed: {result["content"][0]["text"]}')
        return json.loads(result['content'][0]['text'])

    def _err(self, name, args=None):
        """Call a tool expecting a clean tool error; return the message."""
        _resp, body = self._call_tool(name, args or {})
        result = body['result']
        self.assertTrue(result['isError'], f'{name} unexpectedly succeeded')
        return result['content'][0]['text']

    def _statement_with_pdf(self, *, month=3, year=2026, pdf_body=None,
                            statement_id='st-brk-1'):
        from tests.test_statements import make_pdf
        start, end = stmts.period_bounds(month, year)
        label = stmts.period_label(start)
        path = stmts.pdf_path_for(self.item.item_id, 'brk-1', label,
                                  statement_id)
        data = pdf_body if pdf_body is not None else make_pdf(
            ['WELLS FARGO ADVISORS', 'Opening value of cash and sweep '
             'balances 5,000.00'])
        size = stmts.store_pdf(path, data)
        st = PlaidStatement(
            statement_id=statement_id, plaid_item_id=self.item.item_id,
            plaid_account_id='brk-1', period_start=start, period_end=end,
            opening_balance=5000.0, closing_balance=4200.0,
            portfolio_opening_value=15000.0, portfolio_closing_value=15500.0,
            parse_method='wf_advisors', pdf_path=path, pdf_bytes=size,
            cash_services_account_number='1234567 9402',
            parsed_metadata={'cash_opening': 5000.0, 'cash_closing': 4200.0,
                             'securities_purchased': -1000.0,
                             'securities_sold': 0.0,
                             'dividends_total': 200.0,
                             'parser_version': 'test-1',
                             'layout': 'wf_advisors', 'verified': True,
                             'fields_failed': []})
        db.session.add(st)
        db.session.commit()
        return st, data

    def _trade(self, tid='inv-1', amount=1000.0, when=None, type_='buy'):
        t = SecurityTransaction(
            plaid_investment_transaction_id=tid, account_id='brk-1',
            security_id='sec-aapl', date=when or date(2026, 3, 10),
            name='BUY TEST-AAPL', quantity=10.0, amount=amount, price=100.0,
            fees=0.0, type=type_, subtype=type_)
        db.session.add(t)
        db.session.commit()
        return t

    def _cash_txn(self, tid='bank-1', amount=1000.0, when=None,
                  name='BROKERAGE ACTIVITY'):
        t = BankTransaction(plaid_transaction_id=tid, account_id='cash-1',
                            amount=amount, date=when or date(2026, 3, 11),
                            name=name, merchant_name='WF',
                            category='Transfer > Brokerage')
        db.session.add(t)
        db.session.commit()
        return t

    def _holding(self, quantity=10.0, price=130.0):
        h = SecurityHolding(account_id='brk-1', security_id='sec-aapl',
                            quantity=quantity, institution_price=price,
                            institution_price_as_of=date(2026, 3, 31),
                            institution_value=round(quantity * price, 2),
                            cost_basis=1000.0)
        db.session.add(h)
        db.session.commit()
        return h


# ── registration + posture ──────────────────────────────────────────────────
class ToolkitRegistrationTest(InvestToolBase):

    NEW_TOOLS = ('list_statements', 'get_statement_pdf',
                 'get_statement_extracted_data', 'list_plaid_transactions',
                 'list_investment_transactions', 'list_holdings',
                 'list_unpaired_trades')

    def test_all_seven_are_listed_with_schemas(self):
        tools = {t['name']: t
                 for t in self._rpc('tools/list').get_json()['result']['tools']}
        for name in self.NEW_TOOLS:
            self.assertIn(name, tools)
            self.assertTrue(tools[name]['description'])
            self.assertEqual('object', tools[name]['inputSchema']['type'])
            self.assertIn('account_mask',
                          tools[name]['inputSchema']['properties'])

    def test_none_of_them_mutates(self):
        """Read-only, so no kill switch — and therefore each must work with
        every switch OFF, which is how a fresh install ships."""
        from app.blueprints import mcp_server
        for name in self.NEW_TOOLS:
            self.assertFalse(mcp_server.TOOLS[name]['mutating'], name)

    def test_they_work_with_every_kill_switch_off(self):
        self._statement_with_pdf()
        self._ok('list_statements', {'account_mask': '9401'})


# ── list_statements ─────────────────────────────────────────────────────────
class ListStatementsTest(InvestToolBase):

    def test_it_enumerates_periods_newest_first(self):
        self._statement_with_pdf(month=2, statement_id='st-feb')
        self._statement_with_pdf(month=3, statement_id='st-mar')
        out = self._ok('list_statements', {'account_mask': '9401'})
        self.assertEqual(2, out['count'])
        self.assertEqual(['2026-03', '2026-02'],
                         [r['period'] for r in out['statements']])

    def test_it_reports_cash_and_portfolio_figures_separately(self):
        """On a brokerage these are different numbers, and conflating them is
        how a $278k deposit gets mistaken for a $278k reconciliation error."""
        self._statement_with_pdf()
        stmts.rebuild_statement_anchors('brk-1')
        row = self._ok('list_statements',
                       {'account_mask': '9401'})['statements'][0]
        self.assertEqual(5000.0, row['anchored_opening'])
        self.assertEqual(4200.0, row['anchored_closing'])
        self.assertEqual(15000.0, row['portfolio_opening_value'])
        self.assertEqual(15500.0, row['portfolio_closing_value'])
        self.assertIsNotNone(row['mark_to_market_delta'])

    def test_it_reports_whether_a_pdf_is_really_on_disk(self):
        st, _ = self._statement_with_pdf()
        row = self._ok('list_statements',
                       {'account_mask': '9401'})['statements'][0]
        self.assertTrue(row['has_pdf'])
        self.assertGreater(row['pdf_bytes'], 0)
        st.pdf_path = '/nowhere/gone.pdf'
        db.session.commit()
        row = self._ok('list_statements',
                       {'account_mask': '9401'})['statements'][0]
        self.assertFalse(row['has_pdf'],
                         'a row pointing at a file that no longer exists must '
                         'not claim a PDF is available')

    def test_the_year_filter_narrows(self):
        self._statement_with_pdf(month=3, year=2026, statement_id='st-2026')
        self._statement_with_pdf(month=3, year=2025, statement_id='st-2025')
        out = self._ok('list_statements',
                       {'account_mask': '9401', 'year': 2025})
        self.assertEqual(['2025-03'], [r['period'] for r in out['statements']])

    def test_a_non_integer_year_is_refused(self):
        self.assertIn('year must be an integer',
                      self._err('list_statements',
                                {'account_mask': '9401', 'year': 'last'}))

    def test_an_unknown_mask_is_refused(self):
        self.assertIn('no account with mask',
                      self._err('list_statements', {'account_mask': '0000'}))


# ── get_statement_pdf ───────────────────────────────────────────────────────
class GetStatementPdfTest(InvestToolBase):

    def test_the_pdf_round_trips_through_base64(self):
        _st, data = self._statement_with_pdf()
        out = self._ok('get_statement_pdf',
                       {'account_mask': '9401', 'period': '2026-03'})
        self.assertEqual('base64', out['encoding'])
        self.assertEqual('application/pdf', out['content_type'])
        self.assertEqual(len(data), out['size_bytes'])
        self.assertEqual(data, base64.b64decode(out['content_base64']),
                         'the bytes handed back must be byte-identical to the '
                         'bytes on disk')
        self.assertTrue(out['filename'].endswith('.pdf'))

    def test_an_exact_period_end_also_resolves(self):
        """A caller chaining off get_reconciliation_status already holds the
        anchor's period_end; making it re-derive 'YYYY-MM' would be busywork."""
        self._statement_with_pdf()
        out = self._ok('get_statement_pdf',
                       {'account_mask': '9401', 'period': '2026-03-31'})
        self.assertEqual('2026-03', out['period'])

    def test_an_unknown_period_names_the_ones_that_exist(self):
        """A near miss should cost one round-trip, not a guessing game."""
        self._statement_with_pdf(month=3)
        msg = self._err('get_statement_pdf',
                        {'account_mask': '9401', 'period': '2026-09'})
        self.assertIn('no statement', msg)
        self.assertIn('2026-03', msg)

    def test_a_statement_with_no_stored_pdf_refuses_helpfully(self):
        start, end = stmts.period_bounds(4, 2026)
        db.session.add(PlaidStatement(
            statement_id='st-nopdf', plaid_item_id=self.item.item_id,
            plaid_account_id='brk-1', period_start=start, period_end=end,
            pdf_path='', pdf_bytes=0))
        db.session.commit()
        msg = self._err('get_statement_pdf',
                        {'account_mask': '9401', 'period': '2026-04'})
        self.assertIn('no PDF stored', msg)
        self.assertIn('st-nopdf', msg)

    def test_an_oversized_pdf_hands_back_the_path_instead(self):
        """Base64 in a JSON-RPC result costs ~4/3 its size AND lands in the
        caller's context window; past a point the useful answer is the path."""
        from app.blueprints import mcp_server
        self._statement_with_pdf()
        original = mcp_server._MAX_PDF_BYTES
        mcp_server._MAX_PDF_BYTES = 10
        try:
            msg = self._err('get_statement_pdf',
                            {'account_mask': '9401', 'period': '2026-03'})
        finally:
            mcp_server._MAX_PDF_BYTES = original
        self.assertIn('over the', msg)
        self.assertIn('get_statement_extracted_data', msg)

    def test_a_malformed_period_is_refused(self):
        self.assertIn('period must be YYYY-MM',
                      self._err('get_statement_pdf',
                                {'account_mask': '9401', 'period': 'March'}))


# ── get_statement_extracted_data ────────────────────────────────────────────
class GetStatementExtractedDataTest(InvestToolBase):

    def _with_activity(self):
        from app.models import StatementTransaction
        st, _ = self._statement_with_pdf()
        db.session.add(StatementTransaction(
            statement_id=st.id, sequence=1, posted_date=date(2026, 3, 10),
            amount=-1000.0, description='PURCHASE TEST-AAPL',
            section='activity detail', match_status='no_match'))
        db.session.add(StatementTransaction(
            statement_id=st.id, sequence=2, posted_date=date(2026, 3, 20),
            amount=200.0, description='DIVIDEND TEST-AAPL',
            section='activity detail', match_status='matched'))
        db.session.commit()
        return st

    def test_it_round_trips_every_parsed_figure(self):
        """VERBATIM, not filtered to a known list — an unrecognised key is
        exactly what someone diagnosing a parser regression needs to see."""
        self._with_activity()
        out = self._ok('get_statement_extracted_data',
                       {'account_mask': '9401', 'period': '2026-03'})
        meta = out['parsed_metadata']
        self.assertEqual(5000.0, meta['cash_opening'])
        self.assertEqual(4200.0, meta['cash_closing'])
        self.assertEqual(-1000.0, meta['securities_purchased'])
        self.assertEqual(200.0, meta['dividends_total'])
        self.assertEqual('test-1', out['parser_version'])
        self.assertEqual('wf_advisors', out['parse_layout'])
        self.assertTrue(out['parse_verified'])
        self.assertEqual([], out['fields_failed'])
        self.assertEqual('1234567 9402', out['cash_services_account_number'])

    def test_it_returns_every_extracted_activity_row(self):
        self._with_activity()
        out = self._ok('get_statement_extracted_data',
                       {'account_mask': '9401', 'period': '2026-03'})
        self.assertEqual(2, out['extracted_transaction_count'])
        first = out['extracted_transactions'][0]
        self.assertEqual('PURCHASE TEST-AAPL', first['description'])
        self.assertEqual('no_match', first['match_status'])
        # Both sign conventions, spelled out, so a caller comparing this
        # against the transaction feeds never has to guess which way round it
        # is.
        self.assertEqual(-1000.0, first['amount'])
        self.assertEqual(1000.0, first['plaid_convention_amount'])

    def test_it_says_holdings_are_not_extracted_from_pdfs(self):
        """Empty and explained, rather than absent. A caller must be able to
        tell 'none extracted' from 'this build does not report them'."""
        self._with_activity()
        out = self._ok('get_statement_extracted_data',
                       {'account_mask': '9401', 'period': '2026-03'})
        self.assertEqual([], out['extracted_holdings'])
        self.assertIn('list_holdings', out['extracted_holdings_note'])

    def test_a_statement_with_no_metadata_still_answers(self):
        """A row parsed before v0.4.41 recorded nothing about how. It must
        degrade to empty, not 500."""
        start, end = stmts.period_bounds(4, 2026)
        db.session.add(PlaidStatement(
            statement_id='st-bare', plaid_item_id=self.item.item_id,
            plaid_account_id='brk-1', period_start=start, period_end=end))
        db.session.commit()
        out = self._ok('get_statement_extracted_data',
                       {'account_mask': '9401', 'period': '2026-04'})
        self.assertEqual({}, out['parsed_metadata'])
        self.assertEqual(0, out['extracted_transaction_count'])


# ── the two transaction feeds ───────────────────────────────────────────────
class ListPlaidTransactionsTest(InvestToolBase):

    def test_it_searches_the_cash_companion_for_a_paired_brokerage(self):
        """The brokerage holds zero bank transactions by construction. A
        listing that honoured only its own account_id would return an empty
        array and read as 'Plaid sent nothing' — the exact misreading this
        sprint exists to end."""
        self._cash_txn()
        out = self._ok('list_plaid_transactions',
                       {'account_mask': '9401', 'from_date': '2026-03-01',
                        'to_date': '2026-03-31'})
        self.assertEqual(1, out['count'])
        self.assertEqual('9402', out['transactions'][0]['account_mask'],
                         'each row must say which account it came from')
        self.assertEqual('9402', out['paired_cash_mask'])

    def test_it_reports_both_sign_conventions(self):
        self._cash_txn(amount=1000.0)
        row = self._ok('list_plaid_transactions',
                       {'account_mask': '9401', 'from_date': '2026-03-01',
                        'to_date': '2026-03-31'})['transactions'][0]
        self.assertEqual(1000.0, row['amount'])          # Plaid: money OUT
        self.assertEqual(-1000.0, row['cash_in_amount'])

    def test_the_type_filter_narrows(self):
        self._cash_txn('bank-1', name='BROKERAGE ACTIVITY')
        t = self._cash_txn('bank-2', name='COFFEE SHOP')
        t.category = 'Food and Drink'
        db.session.commit()
        out = self._ok('list_plaid_transactions',
                       {'account_mask': '9401', 'from_date': '2026-03-01',
                        'to_date': '2026-03-31',
                        'transaction_type': 'brokerage'})
        self.assertEqual(['bank-1'],
                         [r['transaction_id'] for r in out['transactions']])

    def test_reversed_dates_are_refused(self):
        self.assertIn('is after',
                      self._err('list_plaid_transactions',
                                {'account_mask': '9401',
                                 'from_date': '2026-03-31',
                                 'to_date': '2026-03-01'}))

    def test_a_malformed_date_is_refused_by_name(self):
        msg = self._err('list_plaid_transactions',
                        {'account_mask': '9401', 'from_date': 'March',
                         'to_date': '2026-03-31'})
        self.assertIn('from_date must be YYYY-MM-DD', msg)


class ListInvestmentTransactionsTest(InvestToolBase):

    def test_it_returns_the_investment_feed_with_resolved_tickers(self):
        self._trade()
        out = self._ok('list_investment_transactions',
                       {'account_mask': '9401', 'from_date': '2026-03-01',
                        'to_date': '2026-03-31'})
        self.assertEqual(1, out['count'])
        row = out['investment_transactions'][0]
        self.assertEqual('inv-1', row['investment_transaction_id'])
        self.assertEqual('buy', row['type'])
        self.assertEqual('TEST-AAPL', row['ticker'])
        self.assertEqual(10.0, row['quantity'])
        self.assertEqual(100.0, row['price'])
        self.assertEqual(1000.0, row['amount'])
        self.assertEqual(-1000.0, row['cash_in_amount'])

    def test_an_empty_result_says_whether_the_feed_was_ever_pulled(self):
        """Zero rows means either 'no trades in this window' or 'this Item's
        investments feed was never pulled' — opposite findings with the same
        shape. The second is what produced Movement 0.00 on thirteen periods,
        so the answer has to distinguish them without a second call."""
        out = self._ok('list_investment_transactions',
                       {'account_mask': '9401', 'from_date': '2026-03-01',
                        'to_date': '2026-03-31'})
        self.assertEqual(0, out['count'])
        self.assertFalse(out['investments_ever_synced'])
        self.assertIsNone(out['investments_synced_at'])

    def test_a_synced_item_says_so(self):
        from datetime import datetime, timezone
        self.item.investments_synced_at = datetime.now(timezone.utc)
        db.session.commit()
        out = self._ok('list_investment_transactions',
                       {'account_mask': '9401', 'from_date': '2026-03-01',
                        'to_date': '2026-03-31'})
        self.assertTrue(out['investments_ever_synced'])
        self.assertIsNotNone(out['investments_synced_at'])

    def test_the_window_bounds_are_honoured(self):
        self._trade('inv-in', when=date(2026, 3, 10))
        self._trade('inv-out', when=date(2026, 6, 10))
        out = self._ok('list_investment_transactions',
                       {'account_mask': '9401', 'from_date': '2026-03-01',
                        'to_date': '2026-03-31'})
        self.assertEqual(['inv-in'],
                         [r['investment_transaction_id']
                          for r in out['investment_transactions']])


# ── list_holdings ───────────────────────────────────────────────────────────
class ListHoldingsTest(InvestToolBase):

    def test_it_returns_positions_with_a_total(self):
        self._holding(quantity=10.0, price=130.0)
        out = self._ok('list_holdings', {'account_mask': '9401'})
        self.assertEqual(1, out['count'])
        h = out['holdings'][0]
        self.assertEqual('TEST-AAPL', h['ticker'])
        self.assertEqual(10.0, h['quantity'])
        self.assertEqual(1300.0, h['institution_value'])
        self.assertEqual(1000.0, h['cost_basis'])
        self.assertEqual(1300.0, out['total_institution_value'])
        self.assertEqual('2026-03-31', out['snapshot_priced_as_of'])

    def test_as_of_is_answered_honestly_rather_than_ignored(self):
        """`security_holdings` is a snapshot table — each pull REPLACES the
        rows — so no position history exists. Returning the current snapshot
        and CALLING it historical would be the worst option available."""
        self._holding()
        out = self._ok('list_holdings',
                       {'account_mask': '9401', 'as_of': '2026-02-28'})
        self.assertFalse(out['is_historical'])
        self.assertEqual('2026-02-28', out['requested_as_of'])
        self.assertIn('snapshot', out['as_of_note'].lower())
        self.assertIn('list_statements', out['as_of_note'])
        self.assertEqual(1, out['count'])

    def test_without_as_of_there_is_no_note_to_read(self):
        self._holding()
        out = self._ok('list_holdings', {'account_mask': '9401'})
        self.assertNotIn('as_of_note', out)

    def test_an_account_with_no_positions_answers_empty(self):
        out = self._ok('list_holdings', {'account_mask': '9401'})
        self.assertEqual(0, out['count'])
        self.assertEqual(0.0, out['total_institution_value'])


# ── list_unpaired_trades ────────────────────────────────────────────────────
class ListUnpairedTradesTest(InvestToolBase):

    def test_a_trade_missing_its_settlement_surfaces(self):
        from app import trade_pairing
        self._trade(amount=1000.0, when=date(2026, 3, 10))
        trade_pairing.rebuild('brk-1')
        out = self._ok('list_unpaired_trades', {'account_mask': '9401'})
        self.assertEqual(1, out['count'])
        row = out['unpaired'][0]
        self.assertEqual('2026-03-10', row['date'])
        self.assertEqual('TEST-AAPL', row['security'])
        self.assertEqual('buy', row['action'])
        self.assertEqual('cash', row['missing_leg'])
        self.assertIsNone(row['actual_cash_amount'])
        self.assertIsNotNone(row['days_since_transaction'])

    def test_companion_cash_with_no_trade_surfaces_too(self):
        """A brokerage sweep on the companion with no security leg behind it —
        what an Item that never pulled its investments feed looks like."""
        from app import trade_pairing
        self._cash_txn('bank-wire', amount=-278000.0, when=date(2026, 2, 20),
                       name='Increase from Brokerage activity')
        trade_pairing.rebuild('brk-1')
        row = self._ok('list_unpaired_trades',
                       {'account_mask': '9401'})['unpaired'][0]
        self.assertEqual('security', row['missing_leg'])
        self.assertEqual('unpaired', row['pairing_scheme'])
        self.assertEqual(278000.0, row['actual_cash_amount'])
        self.assertEqual(-278000.0, row['delta'])

    def test_ordinary_companion_traffic_is_not_listed_as_a_trade_leg(self):
        """v0.8.1 · the cash-services companion is a real checking account, and
        v0.8.0 read every debit-card purchase on it as an orphaned trade leg —
        most of where ••9401's -$985,015.56 came from. A grocery bill never had
        a security leg, so it is not a finding."""
        from app import trade_pairing
        t = self._cash_txn('bank-groceries', amount=84.19,
                           when=date(2026, 2, 20), name='TRADER JOES #402')
        t.merchant_name = "Trader Joe's"
        t.category = 'Food and Drink > Groceries'
        db.session.commit()
        trade_pairing.rebuild('brk-1')
        out = self._ok('list_unpaired_trades', {'account_mask': '9401'})
        self.assertEqual(0, out['count'])
        self.assertEqual(0.0, out['unpaired_total'])
        self.assertTrue(out['totals_agree'])

    def test_a_wells_fargo_same_account_pair_does_not_surface(self):
        """v0.8.1 · both legs on the brokerage — a `type=buy` row and a
        `type=cash` row with the same security_id, date and magnitude, both
        POSITIVE because cash left and the custodian says so twice."""
        from app import trade_pairing
        self._trade(amount=1000.0, when=date(2026, 3, 10))
        db.session.add(SecurityTransaction(
            plaid_investment_transaction_id='inv-cash', account_id='brk-1',
            security_id='sec-aapl', date=date(2026, 3, 10), name='cash',
            quantity=0.0, amount=1000.0, price=0.0, type='cash',
            subtype='withdrawal'))
        db.session.commit()
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(1, stats['paired_same_account'])
        out = self._ok('list_unpaired_trades', {'account_mask': '9401'})
        self.assertEqual(0, out['count'])
        self.assertEqual(0.0, out['unpaired_total'])
        self.assertEqual(1, out['summary']['paired_same_account'])
        self.assertEqual(0, out['summary']['paired_cross_account'])

    def test_a_paired_trade_does_not_surface(self):
        from app import trade_pairing
        self._trade(amount=1000.0, when=date(2026, 3, 10))
        self._cash_txn('bank-1', amount=1000.0, when=date(2026, 3, 11))
        trade_pairing.rebuild('brk-1')
        out = self._ok('list_unpaired_trades', {'account_mask': '9401'})
        self.assertEqual(0, out['count'])
        self.assertEqual(0.0, out['unpaired_total'])

    def test_the_total_is_cross_checked_against_the_reported_imbalance(self):
        """Both numbers are returned so a caller can SEE they agree; if they
        ever don't, the pairing table is stale and hiding that behind a single
        figure would be the wrong kind of tidy."""
        from app import trade_pairing
        self._trade(amount=1000.0, when=date(2026, 3, 10))
        self._cash_txn('bank-wire', amount=-2000.0, when=date(2026, 6, 1))
        trade_pairing.rebuild('brk-1')
        out = self._ok('list_unpaired_trades', {'account_mask': '9401'})
        self.assertTrue(out['totals_agree'])
        self.assertEqual(out['reported_clearing_imbalance'],
                         out['unpaired_total'])

    def test_an_unpaired_account_is_refused_rather_than_answered_empty(self):
        """An empty list would read as a clean bill of health for a question
        that does not apply to this account at all."""
        db.session.add(PlaidAccount(
            account_id='solo-1', item_id=self.item.item_id, name='SOLO IRA',
            mask='7777', type='investment', subtype='ira'))
        db.session.commit()
        msg = self._err('list_unpaired_trades', {'account_mask': '7777'})
        self.assertIn('no cash-services companion', msg)
        self.assertIn('list_investment_transactions', msg)

    def test_date_bounds_narrow_the_worklist(self):
        from app import trade_pairing
        self._trade('inv-mar', amount=1000.0, when=date(2026, 3, 10))
        self._trade('inv-jun', amount=2000.0, when=date(2026, 6, 10))
        trade_pairing.rebuild('brk-1')
        out = self._ok('list_unpaired_trades',
                       {'account_mask': '9401', 'from_date': '2026-06-01',
                        'to_date': '2026-06-30'})
        self.assertEqual(['inv-jun'],
                         [r['security_txn_id'] for r in out['unpaired']])


# ── audit ───────────────────────────────────────────────────────────────────
class ToolkitAuditTest(InvestToolBase):

    def test_every_call_is_logged(self):
        """Read tools are un-gated, which makes the audit trail the only record
        that they ran."""
        from app.models import AiActionLog
        self._statement_with_pdf()
        before = AiActionLog.query.count()
        self._ok('list_statements', {'account_mask': '9401'})
        self._err('list_statements', {'account_mask': '0000'})
        rows = AiActionLog.query.order_by(AiActionLog.id.desc()).limit(2).all()
        self.assertEqual(before + 2, AiActionLog.query.count())
        self.assertEqual({True, False}, {bool(r.ok) for r in rows})


# ── the derived table stays fresh ───────────────────────────────────────────
class RebuildRefreshesPairingsTest(InvestToolBase):
    """`trade_leg_pairings` is derived, so it goes stale between syncs. The
    tool an operator (or an AI) reaches for when a number looks wrong has to
    refresh it — otherwise `totals_agree: false` has no remedy short of waiting
    for the next Plaid pull."""

    def test_rebuild_anchors_re_derives_the_pairings(self):
        from app import mcp_settings
        from app.models import TradeLegPairing
        self._trade(amount=1000.0, when=date(2026, 3, 10))
        self.assertEqual(0, TradeLegPairing.query.count())
        mcp_settings.save({'rebuild_anchors': True})
        out = self._ok('rebuild_anchors', {'account_mask': '9401'})
        self.assertIsNotNone(out['trade_leg_pairing'])
        self.assertEqual(1, out['trade_leg_pairing']['unpaired_security'])
        self.assertEqual(1, TradeLegPairing.query.count())

    def test_a_late_settlement_is_resolved_by_a_rebuild(self):
        """T+2 across a sync boundary: the trade lands unpaired, the settlement
        arrives, and a rebuild clears it with no manual step."""
        from app import mcp_settings, trade_pairing
        self._trade(amount=1000.0, when=date(2026, 3, 10))
        trade_pairing.rebuild('brk-1')
        self.assertEqual(1, self._ok('list_unpaired_trades',
                                     {'account_mask': '9401'})['count'])
        self._cash_txn('bank-1', amount=1000.0, when=date(2026, 3, 11))
        mcp_settings.save({'rebuild_anchors': True})
        self._ok('rebuild_anchors', {'account_mask': '9401'})
        out = self._ok('list_unpaired_trades', {'account_mask': '9401'})
        self.assertEqual(0, out['count'])
        self.assertTrue(out['totals_agree'])
