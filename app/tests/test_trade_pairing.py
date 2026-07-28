# SPDX-License-Identifier: MIT
"""Trade leg pairing — itemising the Cash Clearing imbalance (v0.8.1).

A trade moves money twice, once as securities and once as cash, and WHERE the
cash half lands is a property of the custodian rather than of the trade. Bank
Bridge recognises both shapes:

  * SAME-ACCOUNT ('wf_same_account') — Wells Fargo Advisors, and what OML's live
    data actually shows. Both legs arrive on the brokerage account from
    /investments/transactions/get: a `type=buy` row and a `type=cash` row with
    the same security_id, date and magnitude.
  * CROSS-ACCOUNT ('cross_account') — the security leg on the brokerage, the
    cash leg on the cash-services companion as an 'Increase/Decrease from
    Brokerage activity' BankTransaction, T+1 or T+2 later.

v0.8.0 looked for the second shape only, so against Wells Fargo every trade read
as an orphan — 1,208 of them on ••9401 — and every debit-card purchase on the
companion read as an orphaned cash leg. v0.8.1 tries same-account first and
falls back to cross-account, and ignores companion traffic that is not brokerage
activity.

`invest_je.clearing_imbalance` reports the NET of the unpaired remainder as one
scalar. This module turns the scalar into a worklist, and the property that
makes the worklist trustworthy is the one asserted hardest below:

    Σ delta over an account's rows  ==  clearing_imbalance(account)

A decomposition that did not add back up to the number on the account page would
be a second opinion rather than an explanation, and an operator would have no
way to know which to believe. Since v0.8.1 the identity holds by construction —
`clearing_imbalance` delegates to `trade_pairing.projected_clearing_imbalance`,
so there is only one calculation — and the tests below check that the ONE
calculation is right rather than that two of them agree.

Covered here: both pairing shapes and the order they are tried in; the amount,
currency, security and direction tests that stop a near-miss pairing; the
settlement window; ordinary companion traffic staying out of the trade-leg
worklist entirely; a synthesised ••9401-shaped account converging to a near-zero
imbalance; idempotency; and the Σ-delta identity.

Synthetic accounts and tickers only — no real account data.

    cd app
    python3 -m unittest discover -s tests -v
"""
from datetime import date, timedelta

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
               price=100.0, security_id='sec-aapl', currency='USD'):
        """A security leg. `amount` is PLAID's convention: positive means cash
        LEFT the brokerage (a buy)."""
        t = SecurityTransaction(
            plaid_investment_transaction_id=tid, account_id='brk-1',
            security_id=security_id, date=when, name=f'{type_} TEST-AAPL',
            quantity=quantity, amount=amount, price=price, type=type_,
            subtype=type_, iso_currency_code=currency)
        db.session.add(t)
        db.session.commit()
        return t

    def _wf_cash(self, tid, amount, when, subtype='withdrawal',
                 security_id='sec-aapl', currency='USD'):
        """Wells Fargo's OTHER half: a `type=cash` investment row on the SAME
        brokerage account, carrying the traded security's id.

        Note the convention this exists to exercise — WFA reports BOTH halves of
        a buy as POSITIVE amounts (cash left, said twice), so the sign alone
        cannot say which way a cash row moved money and `subtype` has to."""
        t = SecurityTransaction(
            plaid_investment_transaction_id=tid, account_id='brk-1',
            security_id=security_id, date=when, name=f'cash {subtype}',
            quantity=0.0, amount=amount, price=0.0, type='cash',
            subtype=subtype, iso_currency_code=currency)
        db.session.add(t)
        db.session.commit()
        return t

    def _settlement(self, tid, amount, when, name='BROKERAGE ACTIVITY'):
        """A cash leg on the companion. Same Plaid convention: positive means
        money left the checking account.

        The default name matters: only the companion's BROKERAGE-ACTIVITY lines
        are trade legs at all. See `_ordinary` for the other kind."""
        t = BankTransaction(plaid_transaction_id=tid, account_id='cash-1',
                            amount=amount, date=when, name=name)
        db.session.add(t)
        db.session.commit()
        return t

    def _ordinary(self, tid, amount, when, name='TRADER JOES #402'):
        """Ordinary companion traffic — the cash-services account is a real
        checking account and most of what crosses it was never a trade."""
        return self._settlement(tid, amount, when, name=name)

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
        """The case a trade-keyed table could not hold at all: a brokerage sweep
        on the cash side with no security leg to explain it — what an Item whose
        investments feed was never pulled looks like."""
        self._settlement('bank-1', -278000.0, date(2026, 2, 20),
                         name='Increase from Brokerage activity')
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(1, stats['unpaired_cash'])
        row = self._unpaired()[0]
        self.assertEqual('security', row.missing_leg)
        self.assertIsNone(row.security_txn_id)
        self.assertEqual(278000.0, row.actual_cash_amount)
        self.assertEqual(0.0, row.expected_cash_amount)
        self.assertEqual(-278000.0, row.delta)
        self.assertEqual('Increase from Brokerage activity', row.notes)

    def test_the_two_orphan_kinds_contribute_opposite_signs(self):
        """THE SAME money movement contributes to clearing in OPPOSITE
        directions depending on which leg is the one Bank Bridge can see —
        which is why one live account reads +$297k and the other -$985k off
        the same mechanism.

        Both movements below are $1,000 leaving. Seen only as a trade, it
        leaves -1,000 in clearing; seen only as companion cash, +1,000. The
        two are far apart in date so neither can claim the other."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._settlement('bank-1', 1000.0, date(2026, 6, 1),
                         name='Decrease from Brokerage activity')
        trade_pairing.rebuild('brk-1')
        by_leg = {r.missing_leg: r for r in self._unpaired()}
        self.assertEqual(-1000.0, by_leg['cash'].delta)
        self.assertEqual(1000.0, by_leg['security'].delta)


class SameAccountPairingTest(PairingBase):
    """The Wells Fargo shape — both legs on the brokerage, from the SAME Plaid
    feed. v0.8.0 had no rule for this and left every WFA trade an orphan."""

    def test_a_buy_and_its_same_account_cash_row_pair(self):
        """The live TLT-on-2025-06-05 shape, reduced: a `type=buy` and a
        `type=cash/withdrawal` on ••9401, same security, same date, BOTH
        `amount=+35.47` because cash left the account and the custodian says so
        twice."""
        self._trade('inv-1', 35.47, date(2026, 3, 10))
        self._wf_cash('inv-2', 35.47, date(2026, 3, 10))
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(1, stats['paired'])
        self.assertEqual(1, stats['paired_same_account'])
        self.assertEqual(0, stats['paired_cross_account'])
        self.assertEqual(0, stats['unpaired_security'])
        self.assertEqual(0, stats['unpaired_cash'])
        rows = self._rows()
        self.assertEqual(1, len(rows), 'one trade, one row — the cash half is '
                                       'consumed, not listed a second time')
        row = rows[0]
        self.assertEqual('wf_same_account', row.pairing_scheme())
        self.assertEqual('security', row.cash_source)
        self.assertEqual('inv-1', row.security_txn_id)
        self.assertEqual('inv-2', row.cash_txn_id)
        self.assertEqual(-35.47, row.expected_cash_amount)
        self.assertEqual(-35.47, row.actual_cash_amount)
        self.assertEqual(0.0, row.delta)

    def test_both_halves_positive_still_nets_to_zero(self):
        """The sign trap, stated on its own. Taken at face value two +35.47 rows
        read as $70.94 leaving — one trade counted twice, which is exactly how
        the old code manufactured ••9401's imbalance. `subtype` says
        'withdrawal', so the settlement half is money OUT whatever its sign, and
        the pair nets."""
        self._trade('inv-1', 35.47, date(2026, 3, 10))
        self._wf_cash('inv-2', 35.47, date(2026, 3, 10))
        trade_pairing.rebuild('brk-1')
        self.assertEqual(0.0, trade_pairing.unpaired_total('brk-1'))
        self.assertEqual(0.0, invest_je.clearing_imbalance('brk-1'))

    def test_a_sell_pairs_with_a_cash_deposit(self):
        """The other direction: cash comes IN, and `subtype=deposit` says so."""
        self._trade('inv-1', -2500.0, date(2026, 3, 10), type_='sell')
        self._wf_cash('inv-2', -2500.0, date(2026, 3, 11), subtype='deposit')
        trade_pairing.rebuild('brk-1')
        row = self._rows()[0]
        self.assertEqual('wf_same_account', row.pairing_scheme())
        self.assertEqual('sell', row.buy_or_sell)
        self.assertEqual(2500.0, row.expected_cash_amount)
        self.assertEqual(0.0, row.delta)

    def test_a_sell_pairs_even_when_the_deposit_sign_is_flipped(self):
        """The convention Wells Fargo actually uses is not guaranteed stable
        across subtypes, so direction is read off `subtype` and not off the
        sign. A `deposit` is money in at +2500 or at -2500."""
        self._trade('inv-1', -2500.0, date(2026, 3, 10), type_='sell')
        self._wf_cash('inv-2', 2500.0, date(2026, 3, 11), subtype='deposit')
        trade_pairing.rebuild('brk-1')
        self.assertEqual('wf_same_account', self._rows()[0].pairing_scheme())
        self.assertEqual(0.0, self._rows()[0].delta)

    def test_different_amounts_do_not_pair(self):
        """A near-miss is a finding, not a pair. BOTH halves then surface —
        the buy as a trade missing its settlement, the cash row as a brokerage
        cash movement with nothing to explain it."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._wf_cash('inv-2', 1040.0, date(2026, 3, 10))
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(0, stats['paired'])
        self.assertEqual(2, len(self._unpaired()))
        self.assertEqual({'inv-1', 'inv-2'},
                         {r.security_txn_id for r in self._unpaired()})

    def test_a_rounding_penny_still_pairs(self):
        """Exact first, then one penny of slack — the two halves describe one
        movement and a custodian that rounds them apart has not made them two."""
        self._trade('inv-1', 1000.00, date(2026, 3, 10))
        self._wf_cash('inv-2', 1000.01, date(2026, 3, 10))
        trade_pairing.rebuild('brk-1')
        self.assertEqual('wf_same_account', self._rows()[0].pairing_scheme())
        self.assertEqual(0.01, abs(self._rows()[0].delta))

    def test_two_pennies_do_not_pair(self):
        self._trade('inv-1', 1000.00, date(2026, 3, 10))
        self._wf_cash('inv-2', 1000.02, date(2026, 3, 10))
        trade_pairing.rebuild('brk-1')
        self.assertEqual(2, len(self._unpaired()))

    def test_an_exact_partner_is_not_lost_to_an_approximate_one(self):
        """Why the exact pass runs over EVERY leg before the loose pass runs at
        all. Run as one pass, whichever buy is seen first takes whichever cash
        row is nearest in time, and the penny-apart leftovers strand an orphan
        that is an artifact of iteration order rather than a finding."""
        self._trade('inv-1', 1000.00, date(2026, 3, 10))
        self._trade('inv-2', 1000.01, date(2026, 3, 10))
        self._wf_cash('cash-a', 1000.01, date(2026, 3, 10))
        self._wf_cash('cash-b', 1000.00, date(2026, 3, 10))
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(2, stats['paired'])
        self.assertEqual([], self._unpaired())
        by_sec = {r.security_txn_id: r.cash_txn_id for r in self._rows()}
        self.assertEqual({'inv-1': 'cash-b', 'inv-2': 'cash-a'}, by_sec)

    def test_outside_the_window_does_not_pair(self):
        """Ten days apart is not a settlement. The window is three BUSINESS
        days each way, same as the cross-account rule."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._wf_cash('inv-2', 1000.0, date(2026, 3, 20))
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(0, stats['paired'])
        self.assertEqual(2, len(self._unpaired()))

    def test_a_different_security_does_not_pair(self):
        """security_id is an EXACT match, not a hint. Two same-priced trades on
        one day are ordinary, and letting one claim the other's settlement would
        report a clean account and hide both."""
        db.session.add(Security(security_id='sec-tlt', ticker_symbol='TEST-TLT',
                                name='Test Bond Fund', type='etf'))
        db.session.commit()
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._wf_cash('inv-2', 1000.0, date(2026, 3, 10),
                      security_id='sec-tlt')
        trade_pairing.rebuild('brk-1')
        self.assertEqual(2, len(self._unpaired()))

    def test_a_different_currency_does_not_pair(self):
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._wf_cash('inv-2', 1000.0, date(2026, 3, 10), currency='CAD')
        trade_pairing.rebuild('brk-1')
        self.assertEqual(2, len(self._unpaired()))

    def test_a_cash_row_with_no_security_id_can_never_pair(self):
        """A plain deposit or withdrawal never had a trade behind it, so it is
        nobody's settlement half. It stays unpaired — legitimately, because it
        IS a brokerage cash movement with no counterpart, which is a different
        thing from ordinary companion traffic that was never a trade at all."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._wf_cash('inv-2', 1000.0, date(2026, 3, 10), security_id=None)
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(0, stats['paired'])
        self.assertEqual(2, stats['unpaired_security'])

    def test_a_dividend_moving_the_wrong_way_does_not_pair_with_a_buy(self):
        """`subtype=dividend` states no direction, so Plaid's sign is used — and
        the direction test then insists it AGREE with the security leg. A
        dividend paying cash IN cannot settle a buy that paid cash OUT, however
        neatly the magnitudes line up."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._wf_cash('inv-2', -1000.0, date(2026, 3, 10), subtype='dividend')
        trade_pairing.rebuild('brk-1')
        self.assertEqual(2, len(self._unpaired()))

    def test_one_cash_row_cannot_settle_two_trades(self):
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._trade('inv-2', 1000.0, date(2026, 3, 10))
        self._wf_cash('cash-a', 1000.0, date(2026, 3, 10))
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(1, stats['paired'])
        self.assertEqual(1, stats['unpaired_security'])


class PairingSchemeOrderTest(PairingBase):
    """Same-account first, cross-account as the fallback — the order Wells
    Fargo's being the bank we actually integrate with justifies."""

    def test_the_cross_account_fallback_still_pairs(self):
        """v0.8.0's whole behaviour, preserved: no same-account partner exists,
        so the companion's brokerage-activity line settles the trade."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._settlement('bank-1', 1000.0, date(2026, 3, 11))
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(1, stats['paired'])
        self.assertEqual(0, stats['paired_same_account'])
        self.assertEqual(1, stats['paired_cross_account'])
        row = self._rows()[0]
        self.assertEqual('cross_account', row.pairing_scheme())
        self.assertEqual('bank', row.cash_source)
        self.assertEqual('bank-1', row.cash_txn_id)
        self.assertEqual(0.0, row.delta)

    def test_same_account_wins_when_both_are_available(self):
        """A custodian that reported the settlement twice — once on the
        brokerage, once as a companion sweep — must not have the trade counted
        twice. The same-account half is the settlement; the companion line is
        then a sweep with nothing left to explain it, and says so."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._wf_cash('inv-2', 1000.0, date(2026, 3, 10))
        self._settlement('bank-1', 1000.0, date(2026, 3, 11))
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(1, stats['paired_same_account'])
        self.assertEqual(0, stats['paired_cross_account'])
        self.assertEqual(1, stats['unpaired_cash'])
        self.assertEqual('security',
                         [r for r in self._rows()
                          if not r.is_paired()][0].missing_leg)

    def test_an_unpaired_row_reports_scheme_unpaired(self):
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        trade_pairing.rebuild('brk-1')
        self.assertEqual('unpaired', self._unpaired()[0].pairing_scheme())
        self.assertEqual('unpaired',
                         self._unpaired()[0].to_dict()['pairing_scheme'])


class OrdinaryCompanionTrafficTest(PairingBase):
    """The cash-services companion is a real checking account. v0.8.0 read every
    debit-card purchase on it as an orphaned trade leg, which is most of where
    ••9401's -$985,015.56 came from."""

    def test_a_debit_card_purchase_is_not_a_trade_leg(self):
        self._ordinary('bank-1', 84.19, date(2026, 3, 11))
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(0, stats['unpaired_cash'])
        self.assertEqual([], self._rows())
        self.assertEqual(0.0, invest_je.clearing_imbalance('brk-1'))

    def test_ordinary_traffic_cannot_claim_a_trade_either(self):
        """Not a candidate to match, not a finding to report — one rule, applied
        once. A grocery bill that happens to equal a buy must not settle it and
        then have the pair read as clean."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))
        self._ordinary('bank-1', 1000.0, date(2026, 3, 11))
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(0, stats['paired'])
        self.assertEqual(1, stats['unpaired_security'])
        self.assertEqual(0, stats['unpaired_cash'])

    def test_brokerage_activity_is_recognised_in_either_direction(self):
        for name in ('Increase from Brokerage activity',
                     'Decrease from Brokerage activity',
                     'BROKERAGE ACTIVITY'):
            with self.subTest(name=name):
                txn = BankTransaction(plaid_transaction_id=f'x-{name}',
                                      account_id='cash-1', amount=1.0,
                                      date=date(2026, 3, 11), name=name)
                self.assertTrue(trade_pairing._is_brokerage_activity(txn))

    def test_ordinary_names_are_not(self):
        for name in ('TRADER JOES #402', 'ACH OUT', 'WIRE IN', ''):
            with self.subTest(name=name):
                txn = BankTransaction(plaid_transaction_id=f'x-{name}',
                                      account_id='cash-1', amount=1.0,
                                      date=date(2026, 3, 11), name=name)
                self.assertFalse(trade_pairing._is_brokerage_activity(txn))


class LiveShapeRegressionTest(PairingBase):
    """••9401, synthesised. The account that reported 1,208 unpaired security
    legs and -$985,015.56 of clearing under v0.8.0, in the proportions the June
    2025 sample showed: two investment rows per trade on the brokerage, and a
    companion carrying ordinary depository traffic only."""

    TRADES = 300

    def _seed(self):
        start = date(2026, 3, 2)                       # a Monday
        for i in range(self.TRADES):
            when = start + timedelta(days=(i % 5) + 7 * (i // 5))
            amount = round(100.0 + i * 3.17, 2)
            # Wells Fargo's two halves, both POSITIVE, both on the brokerage.
            self._trade(f'inv-{i}-sec', amount, when)
            self._wf_cash(f'inv-{i}-cash', amount, when)
            # The companion, meanwhile, is somebody's checking account.
            self._ordinary(f'bank-{i}', round(20.0 + i, 2), when)

    def test_the_legs_pair_and_the_imbalance_collapses(self):
        self._seed()
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(self.TRADES, stats['paired'])
        self.assertEqual(self.TRADES, stats['paired_same_account'])
        self.assertEqual(0, stats['unpaired_security'],
                         'every WFA trade paired — this is the 1,208 → 0 case')
        self.assertEqual(0, stats['unpaired_cash'],
                         'the companion carried no trade legs to orphan')
        self.assertEqual(0.0, stats['imbalance'])
        self.assertEqual([], self._unpaired())

    def test_the_old_behaviour_is_what_produced_the_headline_number(self):
        """Not a test of current code so much as a record of the arithmetic that
        was wrong, so nobody re-derives it. Counting BOTH halves on the security
        side and subtracting ALL companion traffic is what turned 300 clean
        trades into a six-figure finding."""
        self._seed()
        legs = SecurityTransaction.query.filter_by(account_id='brk-1').all()
        companion = BankTransaction.query.filter_by(account_id='cash-1').all()
        old = round(-sum(t.amount for t in legs)
                    + sum(t.amount for t in companion), 2)
        self.assertLess(old, -100000.0)
        trade_pairing.rebuild('brk-1')
        self.assertEqual(0.0, invest_je.clearing_imbalance('brk-1'))

    def test_a_genuine_gap_still_surfaces_among_the_noise(self):
        """The point of collapsing the false positives: the ONE trade whose
        settlement really is missing is now visible."""
        self._seed()
        self._trade('inv-orphan', 4242.42, date(2026, 4, 6))
        trade_pairing.rebuild('brk-1')
        unpaired = self._unpaired()
        self.assertEqual(1, len(unpaired))
        self.assertEqual('inv-orphan', unpaired[0].security_txn_id)
        self.assertEqual(-4242.42, trade_pairing.unpaired_total('brk-1'))
        self.assertEqual(-4242.42, invest_je.clearing_imbalance('brk-1'))


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
        self._settlement('bank-3', -278000.0, date(2026, 5, 4),
                         name='Increase from Brokerage activity')
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
        self.assertEqual(0, s['paired_same_account'])
        self.assertEqual(2, s['paired_cross_account'])
        self.assertEqual(2, s['unpaired_security'])   # inv-2 and inv-4
        self.assertEqual(1, s['unpaired_cash'])       # the sweep
        self.assertEqual(s['unpaired'],
                         s['unpaired_security'] + s['unpaired_cash'])
        self.assertEqual(s['paired'],
                         s['paired_same_account'] + s['paired_cross_account'])
        self.assertEqual(invest_je.clearing_imbalance('brk-1'),
                         s['unpaired_total'])

    def test_the_identity_holds_with_both_schemes_in_play(self):
        """The case v0.8.1 introduces: one account settling some trades
        same-account and some cross-account, with orphans of both kinds and
        ordinary traffic that counts for nothing. Every one of those paths has
        to contribute the same way to the itemisation and to the scalar, or the
        two stop agreeing exactly where an operator would start looking."""
        self._trade('inv-1', 1000.0, date(2026, 3, 10))          # WFA pair
        self._wf_cash('inv-1c', 1000.0, date(2026, 3, 10))
        self._trade('inv-2', 700.0, date(2026, 3, 17))           # cross pair
        self._settlement('bank-1', 700.0, date(2026, 3, 18))
        self._trade('inv-3', 2500.0, date(2026, 4, 2))           # never settles
        self._settlement('bank-2', -9000.0, date(2026, 4, 20),   # lone sweep
                         name='Increase from Brokerage activity')
        self._ordinary('bank-3', 84.19, date(2026, 4, 21))       # not a trade
        stats = trade_pairing.rebuild('brk-1')
        self.assertEqual(1, stats['paired_same_account'])
        self.assertEqual(1, stats['paired_cross_account'])
        self.assertEqual(1, stats['unpaired_security'])
        self.assertEqual(1, stats['unpaired_cash'])
        total = round(sum(float(r.delta or 0.0) for r in self._rows()), 2)
        # The two paired trades contribute nothing. The orphan buy leaves its
        # own -2,500 in clearing; the lone sweep brought 9,000 IN with no trade
        # to explain it, and an orphan cash leg contributes the NEGATION of what
        # moved — so the two orphans compound rather than cancel.
        self.assertEqual(-2500.0 - 9000.0, total)
        self.assertEqual(invest_je.clearing_imbalance('brk-1'), total)
        self.assertEqual(trade_pairing.unpaired_total('brk-1'), total)

    def test_the_scalar_needs_no_table_to_be_right(self):
        """`clearing_imbalance` recomputes from the source tables rather than
        reading `trade_leg_pairings`, so a never-built or half-built table
        cannot make the headline number wrong — only `totals_agree` false."""
        self._seed_a_messy_account()
        self.assertEqual(0, TradeLegPairing.query.count())
        scalar = invest_je.clearing_imbalance('brk-1')
        trade_pairing.rebuild('brk-1')
        self.assertEqual(scalar, trade_pairing.unpaired_total('brk-1'))


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
