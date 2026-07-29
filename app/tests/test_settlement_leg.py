# SPDX-License-Identifier: MIT
"""The same-account settlement leg, and the Cash Clearing repair (v0.8.4).

THE BUG THESE COVER. A paired brokerage's trades settle against Cash Clearing,
which is a bridge and must net to zero. Under the cross-account custodian shape
the other half comes from the companion depository as a BankTransaction the
rules engine posts. Under the Wells Fargo Advisors shape — the one OML actually
has, established in v0.8.1 — it does not: both halves arrive on the brokerage,
a `type=buy` row and a `type=cash` settlement row, and the companion carries
none of it. `build_investment_je` dropped that settlement row as "not income",
so every buy credited clearing and nothing ever debited it. The live ledger
reached -$1,011,119.41 against a brokerage leaf holding zero.

`trade_pairing` could not see it: it PAIRS the two rows and nets their delta to
zero, so `clearing_imbalance` reported ~0 the whole time. The lockstep test that
should have caught the drift compared against a literal copied into the test
file. Both of those are covered here too — the identity is asserted against the
real constants, and the ledger balance is read rather than projected.

Synthetic tickers and round amounts only.

    cd app
    python3 -m unittest discover -s tests -v
"""
import unittest
from unittest import mock

from app import db, invest_je, trade_pairing  # noqa: E402
from app.erpnext_client import ERPNextError  # noqa: E402

from tests.test_invest_je import COMPANY, InvestJEBase  # noqa: E402

BROKERAGE_GL = 'Wells Fargo Brokerage - 9401 - EC'
CLEARING = 'Cash Clearing - Brokerage - EC'


class SettlementLegBase(InvestJEBase):
    """The base fixture's brokerage has no GL leaf of its own — the pre-v0.8.4
    code never needed one, because it never settled against it. Every test here
    does."""
    def setUp(self):
        super().setUp()
        self.brk.erpnext_gl_account_name = BROKERAGE_GL
        db.session.commit()

    def _settlement(self, itx, amount, subtype, **kw):
        return self._txn(itx, 'cash', amount, subtype=subtype, **kw)


class SettlementLegRoutingTests(SettlementLegBase):
    def test_a_withdrawal_debits_clearing_and_credits_the_brokerage(self):
        """The half that funds a buy. Clearing was credited by the buy; this is
        what relieves it."""
        client = self._client()
        self._security()
        txn = self._settlement('t-settle', 9590.00, 'withdrawal')
        gje = invest_je.generate_investment_je(client, txn)
        self.assertIsNotNone(gje)
        lines = self._lines(self._je_for(client, gje))
        self.assertEqual((9590.00, 0.0), lines[CLEARING])
        self.assertEqual((0.0, 9590.00), lines[BROKERAGE_GL])

    def test_a_deposit_debits_the_brokerage_and_credits_clearing(self):
        """The half a sell produces — the mirror image, and the direction that
        would silently double a sell if it were flipped."""
        client = self._client()
        self._security()
        txn = self._settlement('t-settle-in', 18297.64, 'deposit')
        gje = invest_je.generate_investment_je(client, txn)
        lines = self._lines(self._je_for(client, gje))
        self.assertEqual((18297.64, 0.0), lines[BROKERAGE_GL])
        self.assertEqual((0.0, 18297.64), lines[CLEARING])

    def test_a_buy_and_its_settlement_net_clearing_to_zero(self):
        """The whole point. Both halves of one Wells Fargo trade, posted, and
        the bridge nets out — which is what -$1,011,119.41 of unrelieved buys
        was the absence of."""
        client = self._client()
        self._security()
        buy = self._txn('t-buy', 'buy', 9590.00, qty=100, price=95.90)
        settle = self._settlement('t-buy-cash', 9590.00, 'withdrawal')
        for txn in (buy, settle):
            self.assertIsNotNone(invest_je.generate_investment_je(client, txn))
        net = 0.0
        for doc in client.created['Journal Entry'].values():
            for line in doc['accounts']:
                if line['account'] != CLEARING:
                    continue
                net += (line.get('debit_in_account_currency', 0.0)
                        - line.get('credit_in_account_currency', 0.0))
        self.assertEqual(0.0, round(net, 2))

    def test_the_brokerage_leaf_ends_up_carrying_the_cash(self):
        """Buy + settlement collapse to DR Marketable Securities / CR brokerage
        cash — the entry a CPA would have written by hand."""
        client = self._client()
        self._security()
        for txn in (self._txn('t-b', 'buy', 5000.00, qty=50, price=100.0),
                    self._settlement('t-b-cash', 5000.00, 'withdrawal')):
            invest_je.generate_investment_je(client, txn)
        totals = {}
        for doc in client.created['Journal Entry'].values():
            for line in doc['accounts']:
                d, c = (line.get('debit_in_account_currency', 0.0),
                        line.get('credit_in_account_currency', 0.0))
                totals[line['account']] = totals.get(line['account'], 0.0) + d - c
        self.assertEqual(-5000.00, round(totals[BROKERAGE_GL], 2))
        self.assertEqual(5000.00, round(totals['Stocks - EC'], 2))
        self.assertEqual(0.0, round(totals[CLEARING], 2))


class SettlementLegBoundaryTests(SettlementLegBase):
    def test_a_dividend_is_still_income_not_a_settlement(self):
        """v0.5.12's guard is narrowed, not removed: a subtype that names an
        income kind still books income."""
        client = self._client()
        self._security()
        txn = self._settlement('t-div', 250.00, 'dividend')
        gje = invest_je.generate_investment_je(client, txn)
        lines = self._lines(self._je_for(client, gje))
        self.assertIn('Dividend Income - EC', lines)
        self.assertNotIn(BROKERAGE_GL, lines)

    def test_an_unknown_subtype_still_posts_nothing(self):
        """The direction cannot be read off Plaid's sign on this custodian, so a
        row that states none is left alone exactly as before."""
        client = self._client()
        self._security()
        txn = self._settlement('t-huh', 100.00, 'something-else')
        self.assertIsNone(invest_je.generate_investment_je(client, txn))
        self.assertEqual({}, client.created['Journal Entry'])

    def test_an_unpaired_brokerage_posts_no_settlement_leg(self):
        """An unpaired account carries both halves in ONE row — Plaid's amount
        IS the cash impact — so a second row would book the movement twice."""
        client = self._client()
        self._security()
        self.brk.paired_account_id = None
        db.session.commit()
        txn = self._settlement('t-unpaired', 400.00, 'withdrawal')
        self.assertIsNone(invest_je.generate_investment_je(client, txn))
        self.assertEqual({}, client.created['Journal Entry'])

    def test_no_gl_leaf_skips_the_leg_without_failing_the_trade(self):
        """An account linked but never mapped. The security leg must still post
        — the shortfall shows up in Cash Clearing, which is what it is for."""
        client = self._client()
        self._security()
        self.brk.erpnext_gl_account_name = None
        db.session.commit()
        settle = self._settlement('t-nogl', 300.00, 'withdrawal')
        self.assertIsNone(invest_je.generate_investment_je(client, settle))
        buy = self._txn('t-nogl-buy', 'buy', 300.00, qty=3, price=100.0)
        self.assertIsNotNone(invest_je.generate_investment_je(client, buy))

    def test_the_settlement_leg_carries_the_accounting_dimensions(self):
        """Every line of every investment JE gets the Item's dimensions
        (v0.8.3). A leg added later must not be the one exception."""
        client = self._client()
        self.item.invest_je_cost_center = 'Harvest - EC'
        db.session.commit()
        self._security()
        txn = self._settlement('t-dim', 750.00, 'withdrawal')
        with mock.patch('app.erpnext_bank.cost_center_exists', return_value=True):
            gje = invest_je.generate_investment_je(client, txn)
        doc = self._je_for(client, gje)
        for line in doc['accounts']:
            self.assertEqual('Harvest - EC', line.get('cost_center'))

    def test_it_is_idempotent_like_every_other_leg(self):
        client = self._client()
        self._security()
        txn = self._settlement('t-once', 120.00, 'withdrawal')
        invest_je.generate_investment_je(client, txn)
        invest_je.generate_investment_je(client, txn)
        self.assertEqual(1, len(client.created['Journal Entry']))


class LockstepTests(SettlementLegBase):
    def test_the_posted_types_are_the_clearing_types(self):
        """The drift this release paid for. Asserted against both real
        constants — the pre-v0.8.4 version of this compared trade_pairing's
        constant to a literal copied into the test file, which agreed for three
        releases while invest_je posted only three of the four."""
        self.assertEqual(invest_je.CLEARING_POSTED_TYPES,
                         trade_pairing.CLEARING_TYPES)

    def test_every_clearing_type_can_actually_produce_a_document(self):
        """The claim the constant makes, exercised rather than restated: each
        of the four types builds a JE that touches Cash Clearing."""
        client = self._client()
        self._security()
        rows = {
            'buy': self._txn('l-buy', 'buy', 100.0, qty=1, price=100.0),
            'sell': self._txn('l-sell', 'sell', 100.0, qty=1, price=100.0),
            'fee': self._txn('l-fee', 'fee', 10.0),
            'cash': self._settlement('l-cash', 25.0, 'withdrawal'),
        }
        for kind, txn in rows.items():
            with self.subTest(kind=kind):
                self.assertIn(kind, invest_je.CLEARING_POSTED_TYPES)
                doc, _plan = invest_je.build_investment_je(
                    client, txn, self.brk, COMPANY,
                    invest_je.Security.query.filter_by(
                        security_id=txn.security_id).first())
                self.assertIsNotNone(doc, f'{kind} produced no document')
                self.assertIn(CLEARING,
                              {a['account'] for a in doc['accounts']})


class ClearingStatusTests(SettlementLegBase):
    def _with_gl(self, balance):
        """A fake whose GL Entry table puts `balance` (debit-positive) on the
        clearing account."""
        client = self._client()
        client.gl_entries = [{
            'account': CLEARING, 'company': COMPANY,
            'debit': balance if balance > 0 else 0.0,
            'credit': -balance if balance < 0 else 0.0}]
        return client

    def test_it_reads_the_ledger_not_the_projection(self):
        """The two disagreeing IS the bug — a status that only projected would
        have reported healthy throughout."""
        client = self._with_gl(-1011119.41)
        status = invest_je.clearing_status(client, 'brk')
        self.assertEqual(-1011119.41, status['ledger_balance'])
        self.assertEqual(0.0, status['projected_imbalance'])

    def test_it_counts_the_settlement_legs_still_unposted(self):
        client = self._with_gl(-500.0)
        self._security()
        self._settlement('s-1', 300.0, 'withdrawal')
        self._settlement('s-2', 200.0, 'deposit')
        self._settlement('s-3', 50.0, 'dividend')      # income, not a settlement
        status = invest_je.clearing_status(client, 'brk')
        self.assertEqual(2, status['unposted_settlements'])
        self.assertFalse(status['ready_for_cleanup'])

    def test_posting_them_clears_the_backlog(self):
        client = self._with_gl(-300.0)
        self._security()
        txn = self._settlement('s-done', 300.0, 'withdrawal')
        invest_je.generate_investment_je(client, txn)
        status = invest_je.clearing_status(client, 'brk')
        self.assertEqual(0, status['unposted_settlements'])
        self.assertTrue(status['ready_for_cleanup'])

    def test_an_unknown_account_reports_an_error_rather_than_raising(self):
        self.assertIn('error', invest_je.clearing_status(self._client(), 'nope'))


class ClearingCleanupTests(SettlementLegBase):
    def _with_gl(self, balance):
        client = self._client()
        client.gl_entries = [{
            'account': CLEARING, 'company': COMPANY,
            'debit': balance if balance > 0 else 0.0,
            'credit': -balance if balance < 0 else 0.0}]
        return client

    def test_a_dry_run_writes_nothing(self):
        client = self._with_gl(-1011119.41)
        result = invest_je.clearing_cleanup_je(client, 'brk')
        self.assertTrue(result['dry_run'])
        self.assertIsNone(result['journal_entry'])
        self.assertEqual({}, client.created['Journal Entry'])
        self.assertEqual(1011119.41, result['amount'])

    def test_a_credit_balance_is_debited_off_clearing(self):
        client = self._with_gl(-1011119.41)
        result = invest_je.clearing_cleanup_je(client, 'brk', dry_run=False)
        lines = self._lines(client.created['Journal Entry'][result['journal_entry']])
        self.assertEqual((1011119.41, 0.0), lines[CLEARING])
        self.assertEqual((0.0, 1011119.41), lines[BROKERAGE_GL])

    def test_a_debit_balance_goes_the_other_way(self):
        client = self._with_gl(2500.0)
        result = invest_je.clearing_cleanup_je(client, 'brk', dry_run=False)
        lines = self._lines(client.created['Journal Entry'][result['journal_entry']])
        self.assertEqual((0.0, 2500.0), lines[CLEARING])
        self.assertEqual((2500.0, 0.0), lines[BROKERAGE_GL])

    def test_a_flat_clearing_account_is_left_alone(self):
        client = self._with_gl(0.0)
        result = invest_je.clearing_cleanup_je(client, 'brk', dry_run=False)
        self.assertIn('already flat', result['skipped'])
        self.assertEqual({}, client.created['Journal Entry'])

    def test_what_it_writes_is_a_draft(self):
        """Never submitted. A six-figure correction is the operator's to
        approve, and this only ever proposes it."""
        client = self._with_gl(-400.0)
        result = invest_je.clearing_cleanup_je(client, 'brk', dry_run=False)
        self.assertNotIn(result['journal_entry'], client.submitted)

    def test_an_unknown_account_raises(self):
        with self.assertRaises(ERPNextError):
            invest_je.clearing_cleanup_je(self._client(), 'nope')


class OrphanDraftSweepTests(SettlementLegBase):
    """A draft whose GeneratedJournalEntry row is gone. Tim's 2026-07-28 reset
    deleted 642 and left 189 of these standing, because the pass iterates the
    tracker and the tracker no longer named them."""

    def _orphan(self, name, remark, submitted=False):
        client_docs = self.client.created['Journal Entry']
        client_docs[name] = {'user_remark': remark, 'company': COMPANY,
                             'accounts': []}
        if submitted:
            self.client.submitted.add(name)
        return name

    def setUp(self):
        super().setUp()
        self.client = self._client()

    def test_it_deletes_a_draft_no_tracker_row_claims(self):
        self._orphan('ACC-JV-9001', 'Bought 100 TEST-AAPL at $10.00 = $1,000.00')
        stats = invest_je.reset_investment_drafts(self.client)
        self.assertEqual(1, stats['orphan_deleted'])
        self.assertEqual(1, stats['total_deleted'])
        self.assertIn('ACC-JV-9001', self.client.deleted)

    def test_it_reports_the_two_counts_separately(self):
        """'642 deleted' while 189 survived is what sent Tim looking for a bug
        in the delete. '642 tracked + 189 orphaned' explains itself."""
        self._security()
        txn = self._txn('t-tracked', 'buy', 100.0, qty=1, price=100.0)
        invest_je.generate_investment_je(self.client, txn)
        self._orphan('ACC-JV-9002', 'Sold 5 TEST-AAPL at $20.00 = $100.00')
        stats = invest_je.reset_investment_drafts(self.client)
        self.assertEqual(1, stats['tracker_deleted'])
        self.assertEqual(1, stats['orphan_deleted'])
        self.assertEqual(2, stats['total_deleted'])
        self.assertEqual(2, stats['drafts_deleted'])   # retained alias

    def test_it_leaves_a_je_this_module_did_not_write(self):
        """Matched on the remark, not the owner: on a self-hosted install the
        API user IS the operator, so an owner filter would delete their own
        hand-written drafts."""
        self._orphan('ACC-JV-9003', 'Quarterly depreciation adjustment')
        stats = invest_je.reset_investment_drafts(self.client)
        self.assertEqual(0, stats['orphan_deleted'])
        self.assertNotIn('ACC-JV-9003', self.client.deleted)

    def test_it_never_touches_a_submitted_entry(self):
        self._orphan('ACC-JV-9004', 'Bought 1 TEST-AAPL at $1.00 = $1.00',
                     submitted=True)
        stats = invest_je.reset_investment_drafts(self.client)
        self.assertEqual(0, stats['orphan_deleted'])
        self.assertNotIn('ACC-JV-9004', self.client.deleted)

    def test_a_tracked_draft_is_the_tracker_passs_business_not_the_sweeps(self):
        """Deleted once, not twice — and counted as tracked."""
        self._security()
        txn = self._txn('t-both', 'buy', 100.0, qty=1, price=100.0)
        invest_je.generate_investment_je(self.client, txn)
        stats = invest_je.reset_investment_drafts(self.client)
        self.assertEqual(1, stats['tracker_deleted'])
        self.assertEqual(0, stats['orphan_deleted'])

    def test_an_erpnext_that_cannot_list_fails_soft(self):
        """The tracker pass already committed its deletes; a listing failure
        must not undo or mask that."""
        with mock.patch.object(self.client, 'list_docs',
                               side_effect=ERPNextError('down')):
            stats = invest_je.reset_investment_drafts(self.client)
        self.assertEqual(0, stats['orphan_deleted'])
        self.assertFalse(stats['aborted'])


class LedgerBalanceTests(SettlementLegBase):
    def test_cancelled_entries_are_excluded(self):
        client = self._client()
        client.gl_entries = [
            {'account': CLEARING, 'company': COMPANY, 'debit': 100.0,
             'credit': 0.0},
            {'account': CLEARING, 'company': COMPANY, 'debit': 0.0,
             'credit': 40.0},
            {'account': CLEARING, 'company': COMPANY, 'debit': 999.0,
             'credit': 0.0, 'is_cancelled': 1},
        ]
        self.assertEqual(60.0,
                         invest_je.ledger_clearing_balance(client, COMPANY))

    def test_another_accounts_entries_do_not_count(self):
        client = self._client()
        client.gl_entries = [
            {'account': CLEARING, 'company': COMPANY, 'debit': 10.0,
             'credit': 0.0},
            {'account': BROKERAGE_GL, 'company': COMPANY, 'debit': 5000.0,
             'credit': 0.0},
        ]
        self.assertEqual(10.0,
                         invest_je.ledger_clearing_balance(client, COMPANY))


class AdminClearingRouteTests(SettlementLegBase):
    """The buttons on /admin/accounts. `_page()` has 500'd this project over a
    reserved context key before, so a new route gets followed through to the
    page it redirects to rather than checked for a 302 and left there."""

    def setUp(self):
        super().setUp()
        self.http = self.app.test_client()

    def _patched(self, status, cleanup=None):
        client = self._client()
        patches = [
            mock.patch('app.erpnext_bank.get_client', return_value=client),
            mock.patch('app.invest_je.clearing_status', return_value=status),
        ]
        if cleanup is not None:
            patches.append(mock.patch('app.invest_je.clearing_cleanup_je',
                                      return_value=cleanup))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return client

    def test_the_status_button_renders_its_flash(self):
        self._patched({'clearing_account': CLEARING, 'ledger_balance': -1011119.41,
                       'projected_imbalance': 0.0, 'unposted_settlements': 189,
                       'ready_for_cleanup': False})
        resp = self.http.post('/admin/clearing_status', follow_redirects=True)
        self.assertEqual(200, resp.status_code)
        self.assertIn(b'1,011,119.41', resp.data)
        self.assertIn(b'unposted', resp.data)

    def test_the_cleanup_button_refuses_while_the_backfill_is_pending(self):
        client = self._patched({'clearing_account': CLEARING,
                                'ledger_balance': -1000.0,
                                'projected_imbalance': 0.0,
                                'unposted_settlements': 5,
                                'ready_for_cleanup': False})
        resp = self.http.post('/admin/clearing_cleanup', follow_redirects=True)
        self.assertEqual(200, resp.status_code)
        self.assertIn(b'refused', resp.data)
        self.assertEqual({}, client.created['Journal Entry'])

    def test_the_cleanup_button_writes_a_draft_once_the_backfill_is_done(self):
        self._patched({'clearing_account': CLEARING, 'ledger_balance': -400.0,
                       'projected_imbalance': 0.0, 'unposted_settlements': 0,
                       'ready_for_cleanup': True},
                      cleanup={'skipped': '', 'journal_entry': 'ACC-JV-7777',
                               'amount': 400.0, 'counter_account': BROKERAGE_GL})
        resp = self.http.post('/admin/clearing_cleanup', follow_redirects=True)
        self.assertEqual(200, resp.status_code)
        self.assertIn(b'ACC-JV-7777', resp.data)

    def test_the_accounts_page_still_renders_with_the_new_card(self):
        resp = self.http.get('/admin/accounts')
        self.assertEqual(200, resp.status_code)
        self.assertIn(b'Cash Clearing', resp.data)


if __name__ == '__main__':
    unittest.main()
