# SPDX-License-Identifier: MIT
"""AUM double-count regression (v0.7.5).

Plaid defines `balances.current` on an INVESTMENT account as the account's
total market value — the priced holdings plus settled cash. Summing
`SecurityHolding.institution_value` AND `PlaidAccount.balance_current` for the
same account therefore counts every position twice, and an agreement's AUM
(and the base fee accrued off it) came out roughly double.

The bug hid from the v0.5.2 suite because that suite's brokerage fixture is
seeded `balance_current=0.0` — the one value for which the double-count is
invisible. Every test here seeds a NON-ZERO brokerage balance, which is what
production actually holds.

The invariant these tests lock in:

    investment account  → holdings sum, or balance_current when no holdings
    everything else     → balance_current
    paired companion    → always added (separate account, its own cash)

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import advisory  # noqa: E402
from app.models import (AdvisoryAgreement, DailyAUM, PlaidAccount,  # noqa: E402
                        PlaidItem, Security, SecurityHolding)

CLIENT = 'Test Client, LLC'


class AUMBase(unittest.TestCase):
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
        db.session.add(PlaidItem(
            item_id='item-om', access_token_encrypted=crypto.encrypt('x'),
            institution_name='Wells Fargo', status='active',
            owning_company=CLIENT))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _account(self, account_id, **kw):
        defaults = dict(account_id=account_id, item_id='item-om',
                        name=account_id.upper(), type='investment',
                        subtype='brokerage', owning_company=CLIENT,
                        balance_current=0.0)
        defaults.update(kw)
        a = PlaidAccount(**defaults)
        db.session.add(a)
        db.session.commit()
        return a

    def _holding(self, account_id, ticker, value, qty=100):
        sid = f'sec-{ticker}-{account_id}'
        if not Security.query.filter_by(security_id=sid).first():
            db.session.add(Security(security_id=sid, ticker_symbol=ticker,
                                    name=ticker, type='equity'))
        db.session.add(SecurityHolding(
            account_id=account_id, security_id=sid, quantity=qty,
            institution_value=value, institution_price=value / qty))
        db.session.commit()

    def _agreement(self, account_ids, **kw):
        defaults = dict(
            name='Test Agreement', client_company=CLIENT,
            manager_name='TEST MANAGER', managed_account_ids=list(account_ids),
            fee_account_id='Fee Account - EC',
            advisory_expense_account='Advisory Fees - EC',
            total_base_fee_rate=0.02, bank_fee_rate=0.0075,
            performance_fee_rate=0.20, effective_date=date(2026, 1, 1))
        defaults.update(kw)
        a = AdvisoryAgreement(**defaults)
        db.session.add(a)
        db.session.commit()
        return a


class InvestmentDoubleCountTests(AUMBase):
    """THE bug: holdings and balance_current summed for one investment account."""

    def test_holdings_and_balance_current_are_not_summed(self):
        # A brokerage whose Plaid balance_current EQUALS its holdings total,
        # which is exactly what Plaid reports for an investment account.
        self._account('brk', balance_current=1_000_000.0)
        self._holding('brk', 'TESTCO', 1_000_000.0)
        # Pre-fix this returned 2_000_000.0 — the position counted twice.
        self.assertEqual(advisory.account_market_value('brk'), 1_000_000.0)

    def test_holdings_win_when_balance_current_disagrees(self):
        # Holdings are the granular, per-position, dated truth; balance_current
        # is a single cached scalar. When they disagree, holdings decide.
        self._account('brk', balance_current=999_999.0)
        self._holding('brk', 'AAA', 600_000.0)
        self._holding('brk', 'BBB', 400_000.0)
        self.assertEqual(advisory.account_market_value('brk'), 1_000_000.0)

    def test_empty_holdings_falls_back_to_balance_current(self):
        # An investment account Plaid returns no holdings for (unsupported
        # custodian, or the `investments` product isn't enabled) must still
        # report its value rather than collapsing to zero.
        self._account('brk', balance_current=500_000.0)
        self.assertEqual(advisory.account_market_value('brk'), 500_000.0)

    def test_quantity_times_price_when_institution_value_missing(self):
        # The derived-value branch must be inside the same either/or.
        acct = self._account('brk', balance_current=250_000.0)
        db.session.add(Security(security_id='sec-x', ticker_symbol='X',
                                name='X', type='equity'))
        db.session.add(SecurityHolding(
            account_id='brk', security_id='sec-x', quantity=1000,
            institution_value=None, institution_price=250.0))
        db.session.commit()
        self.assertEqual(advisory.account_market_value('brk'), 250_000.0)
        self.assertIsNotNone(acct)

    def test_brokerage_type_also_deduplicates(self):
        # Plaid surfaces some custodians as type='brokerage' rather than
        # 'investment'; the same double-count applies, so the same rule must.
        self._account('brk2', type='brokerage', balance_current=800_000.0)
        self._holding('brk2', 'CCC', 800_000.0)
        self.assertEqual(advisory.account_market_value('brk2'), 800_000.0)


class NonInvestmentTests(AUMBase):
    """Depository/credit accounts keep using balance_current, unchanged."""

    def test_depository_uses_balance_current(self):
        self._account('chk', type='depository', subtype='checking',
                      balance_current=50_000.0)
        self.assertEqual(advisory.account_market_value('chk'), 50_000.0)

    def test_depository_with_anomalous_holding_does_not_double_count(self):
        # A stray holding row against a checking account must not inflate it.
        self._account('chk', type='depository', subtype='checking',
                      balance_current=50_000.0)
        self._holding('chk', 'JUNK', 7_500.0)
        self.assertEqual(advisory.account_market_value('chk'), 50_000.0)

    def test_credit_card_uses_balance_current(self):
        self._account('cc', type='credit', subtype='credit card',
                      balance_current=1_200.0)
        self.assertEqual(advisory.account_market_value('cc'), 1_200.0)

    def test_unknown_account_is_zero(self):
        self.assertEqual(advisory.account_market_value('nope'), 0.0)


class PairedCompanionTests(AUMBase):
    """The cash companion is a SEPARATE account — always added."""

    def test_holdings_plus_companion_cash(self):
        self._account('brk', balance_current=1_000_000.0,
                      paired_account_id='cash')
        self._account('cash', type='depository', subtype='checking',
                      balance_current=50_000.0)
        self._holding('brk', 'TESTCO', 1_000_000.0)
        # holdings 1_000_000 + companion 50_000 — NOT + brk.balance_current.
        self.assertEqual(advisory.account_market_value('brk'), 1_050_000.0)

    def test_companion_added_on_the_empty_holdings_fallback(self):
        self._account('brk', balance_current=500_000.0,
                      paired_account_id='cash')
        self._account('cash', type='depository', subtype='checking',
                      balance_current=25_000.0)
        self.assertEqual(advisory.account_market_value('brk'), 525_000.0)

    def test_missing_companion_is_ignored(self):
        self._account('brk', balance_current=100_000.0,
                      paired_account_id='ghost')
        self._holding('brk', 'TESTCO', 100_000.0)
        self.assertEqual(advisory.account_market_value('brk'), 100_000.0)


class AgreementAUMTests(AUMBase):
    """Agreement-level AUM is the sum of the corrected per-account values."""

    def test_multi_account_agreement_sums_corrected_values(self):
        self._account('brk', balance_current=1_000_000.0,
                      paired_account_id='cash')
        self._account('cash', type='depository', subtype='checking',
                      balance_current=50_000.0)
        self._holding('brk', 'TESTCO', 1_000_000.0)
        self._account('ira', balance_current=300_000.0, subtype='ira')
        self._holding('ira', 'DDD', 300_000.0)

        a = self._agreement(['brk', 'ira'])
        # brk 1_000_000 + companion 50_000 + ira 300_000
        self.assertEqual(advisory.agreement_aum(a), 1_350_000.0)

    def test_daily_sample_accrues_off_the_corrected_aum(self):
        self._account('brk', balance_current=1_000_000.0)
        self._holding('brk', 'TESTCO', 1_000_000.0)
        a = self._agreement(['brk'])

        row = advisory.sample_daily_aum(a, on=date(2026, 3, 31))
        self.assertEqual(row.total_market_value, 1_000_000.0)
        # 1_000_000 × 0.02 / 365 — half of what the doubled AUM would accrue.
        self.assertEqual(row.fee_accrual_daily, round(1_000_000 * 0.02 / 365, 2))

    def test_resample_overwrites_a_doubled_historical_row(self):
        # An operator recomputing a day recorded before the fix must land on
        # the corrected figure, not add to it.
        self._account('brk', balance_current=1_000_000.0)
        self._holding('brk', 'TESTCO', 1_000_000.0)
        a = self._agreement(['brk'])
        db.session.add(DailyAUM(agreement_id=a.id, date=date(2026, 3, 31),
                                total_market_value=2_000_000.0,
                                fee_accrual_daily=109.59))
        db.session.commit()

        row = advisory.sample_daily_aum(a, on=date(2026, 3, 31))
        self.assertEqual(row.total_market_value, 1_000_000.0)
        self.assertEqual(
            DailyAUM.query.filter_by(agreement_id=a.id,
                                     date=date(2026, 3, 31)).count(), 1)


if __name__ == '__main__':
    unittest.main()
