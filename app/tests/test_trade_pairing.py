# SPDX-License-Identifier: MIT
"""Trade leg pairing — itemising the Cash Clearing imbalance (v0.8.0).

A trade at a paired Wells-Fargo-style brokerage moves money twice: a SECURITY
leg on the brokerage (from /investments/transactions/get) and a CASH leg on its
cash-services companion (from /transactions/sync). `invest_je` books each
against `Cash Clearing - Brokerage` from opposite sides, so a complete pair nets
to zero and a lone leg sits there forever.

`invest_je.clearing_imbalance` already reports the NET of that — one scalar over
all history, +$297,358.90 on one live account and -$985,015.56 on the other.
This module turns the scalar into a worklist, and the property that makes the
worklist trustworthy is the one asserted hardest below:

    Σ delta over an account's rows  ==  clearing_imbalance(account)

A decomposition that did not add back up to the number on the account page would
be a second opinion rather than an explanation, and an operator would have no
way to know which to believe.

Covered here: two matching legs pair and contribute nothing; a trade whose
settlement never arrived surfaces with missing_leg='cash'; companion cash with
no trade behind it surfaces with missing_leg='security' (the shape of the larger
live imbalance, and the case a trade-keyed table could not represent at all);
the settlement window; idempotency; and the Σ-delta identity.

Synthetic accounts and tickers only — no real account data.

    cd app
    python3 -m unittest discover -s tests -v
"""
from datetime import date

from app import db
from app import invest_je, trade_pairing
from app.models import (BankTransaction, PlaidAccount, Security,
                        SecurityTransaction, TradeLegPairing)

from tests.test_statements import StatementsBase


class PairingBase(StatementsBase):

    def setUp(self):
        super().setUp()
        self.brokerage = PlaidAccount(
            account_id='brk-1', item_id='item-abc', name='TEST BROKERAGE',
            mask='9401', type='investment', subtype='brokerage',
            paired_account_id='cash-1', import_status='imported')
        self.cash = PlaidAccount(
            account_id='cash-1', item_id='item-abc',
            name='BROKERAGE CASH SERVICES', mask='9402', type='depository',
            subtype='checking', import_status='imported')
        db.session.add_all([self.brokerage, self.cash])
        db.session.add(Security(security_id='sec-aapl',
                                ticker_symbol='TEST-AAPL', name='Test Inc',
                                type='equity'))
        db.session.commit()

    def _trade(self, tid, amount, when, type_='buy', quantity=10.0,
               price=100.0):
        """A security leg. `amount` is PLAID's convention: positive means cash
        LEFT the brokerage (a buy)."""
        t = SecurityTransaction(
            plaid_investment_transaction_id=tid, account_id='brk-1',
            security_id='sec-aapl', date=when, name=f'{type_} TEST-AAPL',
            quantity=quantity, amount=amount, price=price, type=type_,
            subtype=type_)
        db.session.add(t)
        db.session.commit()
        return t

    def _settlement(self, tid, amount, when, name='BROKERAGE ACTIVITY'):
        """A cash leg on the companion. Same Plaid convention: positive means
        money left the checking account."""
        t = BankTransaction(plaid_transaction_id=tid, account_id='cash-1',
                            amount=amount, date=when, name=name)
        db.session.add(t)
        db.session.commit()
        return t

    def _rows(self):
        return trade_pairing.pairings_for_account('brk-1')

    def _unpaired(self):
        return trade_pairing.pairings_for_account('brk-1', unpaired_only=True)


class PairingTest(PairingBase):

    def test_two_matching_legs_pair_and_net_to_nothing(self):
        """A $1,000 buy on Tuesday settling on Wednesday. Both legs present, so
        clearing is clean and there is nothing for an operator to chase."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._settlement('bank-1', 1000.0, date(2026, 3, 11))
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(1, stats['paired'])
        self.assertEqual(0, stats['unpaired_security'])
        self.assertEqual(0, stats['unpaired_cash'])
        row = self._rows()[0]
        self.assertTrue(row.is_paired())
        self.assertEqual('paired', row.status)
        self.assertEqual('', row.missing_leg)
        self.assertEqual('bank-1', row.cash_txn_id)
        self.assertEqual(date(2026, 3, 11), row.cash_date)
        self.assertEqual(-1000.0, row.expected_cash_amount)
        self.assertEqual(-1000.0, row.actual_cash_amount)
        self.assertEqual(0.0, row.delta)
        self.assertEqual([], self._unpaired())

    def test_a_sell_pairs_too(self):
        """Opposite direction, same mechanism: cash comes IN on both sides."""
        self._trade('inv-1', -2500.0, date(2026, 3, 10), type_='sell')
        self._settlement('bank-1', -2500.0, date(2026, 3, 12))
        trade_pairing.rebuild('brk-1')
        row = self._rows()[0]
        self.assertEqual('sell', row.buy_or_sell)
        self.assertTrue(row.is_paired())
        self.assertEqual(2500.0, row.expected_cash_amount)
        self.assertEqual(0.0, row.delta)

    def test_a_trade_whose_settlement_never_arrived_surfaces(self):
        """The ••6030 shape: security legs Bank Bridge holds whose cash never
        appeared on the companion. Positive delta, positive imbalance."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(0, stats['paired'])
        self.assertEqual(1, stats['unpaired_security'])
        row = self._unpaired()[0]
        self.assertEqual('cash', row.missing_leg)
        self.assertIsNone(row.cash_txn_id)
        self.assertIsNone(row.actual_cash_amount)
        self.assertEqual(-1000.0, row.delta)
        self.assertIn('no settlement', row.notes)

    def test_companion_cash_with_no_trade_behind_it_surfaces(self):
        """The ••9401 shape, and the case a trade-keyed table could not hold at
        all: money moved on the cash side with no security leg to explain it —
        what an Item whose investments feed was never pulled looks like."""
        self._settlement('bank-1', -278000.0, date(2026, 2, 20),
                         name='WIRE IN')
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(1, stats['unpaired_cash'])
        row = self._unpaired()[0]
        self.assertEqual('security', row.missing_leg)
        self.assertIsNone(row.security_txn_id)
        self.assertEqual(278000.0, row.actual_cash_amount)
        self.assertEqual(0.0, row.expected_cash_amount)
        self.assertEqual(-278000.0, row.delta)
        self.assertEqual('WIRE IN', row.notes)

    def test_the_two_orphan_kinds_contribute_opposite_signs(self):
        """THE SAME money movement contributes to clearing in OPPOSITE
        directions depending on which leg is the one Bank Bridge can see —
        which is why one live account reads +$297k and the other -$985k off
        the same mechanism.

        Both movements below are $1,000 leaving. Seen only as a trade, it
        leaves -1,000 in clearing; seen only as companion cash, +1,000. The
        two are far apart in date so neither can claim the other."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._settlement('bank-1', 1000.0, date(2026, 6, 1), name='ACH OUT')
        trade_pairing.rebuild('brk-1')
        by_leg = {r.missing_leg: r for r in self._unpaired()}
        self.assertEqual(-1000.0, by_leg['cash'].delta)
        self.assertEqual(1000.0, by_leg['security'].delta)


class SettlementWindowTest(PairingBase):

    def test_settlement_three_business_days_out_still_pairs(self):
        """Thursday trade, following Tuesday settlement — three business days,
        which spans a weekend and five calendar days."""
        self._trade('inv-1', 1000.0, date(2026, 3, 12))     # Thursday
        self._settlement('bank-1', 1000.0, date(2026, 3, 17))  # Tuesday
        trade_pairing.rebuild('brk-1')
        self.assertTrue(self._rows()[0].is_paired())

    def test_settlement_far_outside_the_window_does_not_pair(self):
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._settlement('bank-1', 1000.0, date(2026, 4, 20))
        trade_pairing.rebuild('brk-1')
        self.assertEqual(2, len(self._unpaired()),
                         'both legs should surface as orphans, not pair '
                         'across six weeks')

    def test_a_different_amount_in_the_window_does_not_pair(self):
        """Exact on amount, or not at all — the legs describe one movement and
        the custodian reports both to the cent."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._settlement('bank-1', 1040.0, date(2026, 3, 11))
        trade_pairing.rebuild('brk-1')
        self.assertEqual(2, len(self._unpaired()))

    def test_the_later_of_two_equal_candidates_wins(self):
        """Settlement FOLLOWS a trade. With an identical debit one day either
        side, the one after is the settlement and the one before belongs to
        some earlier trade."""
        self._trade('inv-1', 1000.0, date(2026, 3, 11))
        self._settlement('bank-early', 1000.0, date(2026, 3, 10))
        self._settlement('bank-late', 1000.0, date(2026, 3, 12))
        trade_pairing.rebuild('brk-1')
        paired = [r for r in self._rows() if r.is_paired()]
        self.assertEqual(1, len(paired))
        self.assertEqual('bank-late', paired[0].cash_txn_id)

    def test_one_cash_leg_cannot_settle_two_trades(self):
        """Greedy, but never double-claiming — a single $1,000 debit explains
        one of two identical buys, and the other stays a finding."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._trade('inv-2', 1000.0, date(2026, 3, 10))
        self._settlement('bank-1', 1000.0, date(2026, 3, 11))
        trade_pairing.rebuild('brk-1')
        self.assertEqual(1, sum(1 for r in self._rows() if r.is_paired()))
        self.assertEqual(1, len(self._unpaired()))


class ClearingIdentityTest(PairingBase):
    """Σ delta == clearing_imbalance. The property that makes the itemisation
    an explanation of the headline number rather than a rival to it."""

    def _seed_a_messy_account(self):
        self._trade('inv-1', 1000.0, date(2026, 3, 10))       # settles
        self._settlement('bank-1', 1000.0, date(2026, 3, 11))
        self._trade('inv-2', 2500.0, date(2026, 3, 17))       # never settles
        self._trade('inv-3', -800.0, date(2026, 4, 2), type_='sell')
        self._settlement('bank-2', -800.0, date(2026, 4, 3))  # settles
        self._settlement('bank-3', -278000.0, date(2026, 5, 4), name='WIRE IN')
        self._trade('inv-4', 35.0, date(2026, 5, 20), type_='fee',
                    quantity=0.0, price=0.0)                  # never settles

    def test_the_deltas_sum_to_the_reported_imbalance(self):
        self._seed_a_messy_account()
        trade_pairing.rebuild('brk-1')
        total = round(sum(float(r.delta or 0.0) for r in self._rows()), 2)
        self.assertEqual(invest_je.clearing_imbalance('brk-1'), total)

    def test_unpaired_total_reproduces_it_too(self):
        """Because a paired row contributes exactly zero — which is what lets
        the Cash Clearing balance be recomputed from the unpaired rows ALONE,
        i.e. from movements someone can actually go and look up."""
        self._seed_a_messy_account()
        trade_pairing.rebuild('brk-1')
        self.assertEqual(invest_je.clearing_imbalance('brk-1'),
                         trade_pairing.unpaired_total('brk-1'))

    def test_a_fully_settled_account_has_a_zero_imbalance(self):
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._settlement('bank-1', 1000.0, date(2026, 3, 11))
        trade_pairing.rebuild('brk-1')
        self.assertEqual(0.0, trade_pairing.unpaired_total('brk-1'))
        self.assertEqual(0.0, invest_je.clearing_imbalance('brk-1'))

    def test_the_matched_types_match_what_invest_je_posts_to_clearing(self):
        """The identity above holds only while the two type lists agree, so
        the agreement is asserted rather than left to drift."""
        posted = ('buy', 'sell', 'fee', 'cash')
        self.assertEqual(posted, trade_pairing.CLEARING_TYPES)

    def test_the_summary_partitions_the_total(self):
        self._seed_a_messy_account()
        trade_pairing.rebuild('brk-1')
        s = trade_pairing.summary('brk-1')
        self.assertEqual(2, s['paired'])
        self.assertEqual(2, s['unpaired_security'])   # inv-2 and inv-4
        self.assertEqual(1, s['unpaired_cash'])       # the wire
        self.assertEqual(s['unpaired'],
                         s['unpaired_security'] + s['unpaired_cash'])
        self.assertEqual(invest_je.clearing_imbalance('brk-1'),
                         s['unpaired_total'])


class RebuildBehaviourTest(PairingBase):

    def test_rebuild_is_idempotent(self):
        """It runs on every investments sync; a second pass must converge, not
        accumulate a second opinion about the same trade."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._settlement('bank-1', 1000.0, date(2026, 3, 11))
        trade_pairing.rebuild('brk-1')
        first = [r.to_dict() for r in self._rows()]
        trade_pairing.rebuild('brk-1')
        second = [r.to_dict() for r in self._rows()]
        self.assertEqual(1, len(second))
        for a, b in zip(first, second):
            self.assertEqual(
                {k: v for k, v in a.items() if k not in ('id', 'paired_at')},
                {k: v for k, v in b.items() if k not in ('id', 'paired_at')})

    def test_a_late_settlement_pairs_on_the_next_rebuild(self):
        """T+2 across a sync boundary: the trade lands unpaired, the settlement
        arrives, and the next rebuild resolves it with no manual step."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        trade_pairing.rebuild('brk-1')
        self.assertEqual(1, len(self._unpaired()))
        self._settlement('bank-1', 1000.0, date(2026, 3, 11))
        trade_pairing.rebuild('brk-1')
        self.assertEqual([], self._unpaired())

    def test_an_unpaired_account_gets_no_rows_at_all(self):
        """An unpaired investment account has both legs in one Plaid row, so
        it has no clearing account and cannot have this problem. Rows would
        assert a failure mode it cannot have."""
        solo = PlaidAccount(account_id='solo-1', item_id='item-abc',
                            name='SOLO IRA', mask='7777', type='investment',
                            subtype='ira')
        db.session.add(solo)
        db.session.commit()
        db.session.add(SecurityTransaction(
            plaid_investment_transaction_id='inv-solo', account_id='solo-1',
            security_id='sec-aapl', date=date(2026, 3, 10), quantity=1.0,
            amount=100.0, price=100.0, type='buy', subtype='buy'))
        db.session.commit()
        stats = trade_pairing.rebuild('solo-1')
        self.assertEqual(0, stats['accounts'])
        self.assertEqual(0, TradeLegPairing.query.filter_by(
            account_id='solo-1').count())
        self.assertEqual(0.0, invest_je.clearing_imbalance('solo-1'))

    def test_rebuild_across_all_accounts_touches_only_paired_ones(self):
        self._account()                      # a plain depository account
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        stats = trade_pairing.rebuild()
        self.assertEqual(1, stats['accounts'])

    def test_pending_and_removed_cash_rows_are_ignored(self):
        """Same exclusion, for the same reason, as every other reconciliation
        sum: a pending row is provisional and a removed one Plaid took back."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        t = self._settlement('bank-1', 1000.0, date(2026, 3, 11))
        t.pending = True
        db.session.commit()
        trade_pairing.rebuild('brk-1')
        self.assertEqual(1, len(self._unpaired()))
        self.assertEqual('cash', self._unpaired()[0].missing_leg)

    def test_days_since_ages_an_orphan(self):
        """Age separates 'settlement has not landed yet' from 'this is never
        going to settle'."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        trade_pairing.rebuild('brk-1')
        row = self._unpaired()[0]
        self.assertEqual(31, trade_pairing.days_since(
            row, as_of=date(2026, 4, 10)))

    def test_date_bounds_filter_the_read(self):
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._trade('inv-2', 2000.0, date(2026, 6, 10))
        trade_pairing.rebuild('brk-1')
        rows = trade_pairing.pairings_for_account(
            'brk-1', unpaired_only=True, from_date=date(2026, 6, 1),
            to_date=date(2026, 6, 30))
        self.assertEqual(['inv-2'], [r.security_txn_id for r in rows])
