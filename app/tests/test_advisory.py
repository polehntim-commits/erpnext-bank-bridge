# SPDX-License-Identifier: MIT
"""Investment Advisory Agreement automation (v0.5.2, Phase E).

Covered here:

  * daily AUM sampling — deterministic, idempotent, quarter-to-date cumulative
  * base-fee accrual and the bank/manager split
  * quarterly settlement — gated by the fee kill switch, idempotent
  * high-water mark ratchet (only ever moves up)
  * performance fee: hurdle cleared vs not, above-HWM vs underwater
  * the three kill switches all default FALSE and gate their side effects
  * risk-control checks run always; violations detected; alerts gated
  * the dashboard is a render of stored figures

Synthetic names (TEST MANAGER / TEST CLIENT) and round amounts only.

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
from app.models import (AdvisoryAgreement, AdvisoryFeeAccrual, DailyAUM,  # noqa: E402
                        GeneratedJournalEntry, HighWaterMark, HurdleRateSample,
                        PerformanceSnapshot, PlaidAccount, PlaidItem,
                        RiskControlCheck, Security, SecurityHolding)

from tests.fakes import FakeERPClient  # noqa: E402

CLIENT = 'Test Client, LLC'


class AdvisoryBase(unittest.TestCase):
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
        db.session.add(PlaidAccount(
            account_id='brk', item_id='item-om', name='BROKERAGE', mask='9401',
            type='investment', subtype='brokerage', paired_account_id='cash',
            owning_company=CLIENT, balance_current=0.0))
        db.session.add(PlaidAccount(
            account_id='cash', item_id='item-om', name='CASH', mask='3194',
            type='depository', subtype='checking', balance_current=5000.0))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _agreement(self, **kw):
        defaults = dict(
            name='Test Agreement', client_company=CLIENT,
            manager_name='TEST MANAGER', managed_account_ids=['brk'],
            fee_account_id='Fee Account - EC',
            advisory_expense_account='Advisory Fees - EC',
            total_base_fee_rate=0.02, bank_fee_rate=0.0075,
            performance_fee_rate=0.20, effective_date=date(2026, 1, 1))
        defaults.update(kw)
        a = AdvisoryAgreement(**defaults)
        db.session.add(a)
        db.session.commit()
        return a

    def _holding(self, ticker, value, account_id='brk', qty=100):
        sid = f'sec-{ticker}'
        if not Security.query.filter_by(security_id=sid).first():
            db.session.add(Security(security_id=sid, ticker_symbol=ticker,
                                    name=ticker, type='equity'))
        db.session.add(SecurityHolding(
            account_id=account_id, security_id=sid, quantity=qty,
            institution_value=value, institution_price=value / qty))
        db.session.commit()

    def _hurdle_series(self, start, end, rate_pct):
        d = start
        while d <= end:
            advisory.record_hurdle_sample(d, rate_pct, source='test')
            d = date.fromordinal(d.toordinal() + 1)


# ── kill-switch defaults ──────────────────────────────────────────────────────

class KillSwitchDefaultTests(AdvisoryBase):
    def test_all_three_default_false(self):
        a = self._agreement()
        self.assertFalse(a.fee_accrual_enabled)
        self.assertFalse(a.performance_fee_enabled)
        self.assertFalse(a.risk_control_alerts_enabled)


# ── daily AUM + accrual ───────────────────────────────────────────────────────

class DailyAUMTests(AdvisoryBase):
    def test_aum_sums_holdings_and_both_cash_balances(self):
        a = self._agreement()
        self._holding('TESTCO', 100000.0)
        # brk cash 0 + companion cash 5000 + holdings 100000
        self.assertEqual(advisory.agreement_aum(a), 105000.0)

    def test_daily_accrual_is_deterministic(self):
        a = self._agreement()
        self._holding('TESTCO', 100000.0)
        row = advisory.sample_daily_aum(a, on=date(2026, 7, 1))
        # 105000 * 0.02 / 365
        self.assertEqual(row.fee_accrual_daily,
                         round(105000.0 * 0.02 / 365.0, 2))
        self.assertEqual(row.cumulative_fee_accrual_qtd, row.fee_accrual_daily)

    def test_resampling_a_day_does_not_double(self):
        a = self._agreement()
        self._holding('TESTCO', 100000.0)
        advisory.sample_daily_aum(a, on=date(2026, 7, 1))
        advisory.sample_daily_aum(a, on=date(2026, 7, 1))
        self.assertEqual(DailyAUM.query.count(), 1)

    def test_qtd_accumulates(self):
        a = self._agreement()
        self._holding('TESTCO', 100000.0)
        r1 = advisory.sample_daily_aum(a, on=date(2026, 7, 1))
        r2 = advisory.sample_daily_aum(a, on=date(2026, 7, 2))
        self.assertAlmostEqual(r2.cumulative_fee_accrual_qtd,
                               round(r1.fee_accrual_daily
                                     + r2.fee_accrual_daily, 2), places=2)

    def test_bank_manager_split(self):
        a = self._agreement()
        split = advisory.base_fee_split(a, 100.0)
        # bank 0.0075/0.02 = 37.5%, manager 62.5%
        self.assertEqual(split['bank'], 37.5)
        self.assertEqual(split['manager'], 62.5)


# ── quarterly settlement ──────────────────────────────────────────────────────

class SettlementTests(AdvisoryBase):
    def _accrue_quarter(self, a):
        for day in range(1, 8):
            advisory.sample_daily_aum(a, on=date(2026, 7, day))

    def test_settlement_records_but_does_not_post_when_off(self):
        a = self._agreement(fee_accrual_enabled=False)
        self._holding('TESTCO', 100000.0)
        self._accrue_quarter(a)
        client = FakeERPClient()
        accrual = advisory.settle_quarter(client, a, date(2026, 9, 30))
        self.assertIsNotNone(accrual)
        self.assertGreater(accrual.amount, 0)
        self.assertFalse(accrual.posted_to_erpnext)
        self.assertEqual(len(client.created['Journal Entry']), 0)

    def test_settlement_posts_when_on(self):
        a = self._agreement(fee_accrual_enabled=True)
        self._holding('TESTCO', 100000.0)
        self._accrue_quarter(a)
        client = FakeERPClient()
        accrual = advisory.settle_quarter(client, a, date(2026, 9, 30))
        self.assertTrue(accrual.posted_to_erpnext)
        je = client.created['Journal Entry'][accrual.erpnext_je_id]
        self.assertEqual(je['company'], CLIENT)
        # DR advisory expense, CR fee account, balanced.
        debit = sum(l.get('debit_in_account_currency', 0) for l in je['accounts'])
        credit = sum(l.get('credit_in_account_currency', 0) for l in je['accounts'])
        self.assertEqual(debit, credit)
        self.assertEqual(debit, accrual.amount)

    def test_settlement_is_idempotent(self):
        a = self._agreement(fee_accrual_enabled=True)
        self._holding('TESTCO', 100000.0)
        self._accrue_quarter(a)
        client = FakeERPClient()
        advisory.settle_quarter(client, a, date(2026, 9, 30))
        advisory.settle_quarter(client, a, date(2026, 9, 30))
        self.assertEqual(len(client.created['Journal Entry']), 1)
        self.assertEqual(AdvisoryFeeAccrual.query.filter_by(
            fee_type='base').count(), 1)

    def test_only_the_manager_slice_is_settled(self):
        a = self._agreement(fee_accrual_enabled=True)
        self._holding('TESTCO', 100000.0)
        self._accrue_quarter(a)
        client = FakeERPClient()
        accrual = advisory.settle_quarter(client, a, date(2026, 9, 30))
        gross = round(sum(r.fee_accrual_daily for r in DailyAUM.query.all()), 2)
        self.assertLess(accrual.amount, gross)   # bank slice excluded


# ── high-water mark ───────────────────────────────────────────────────────────

class HighWaterMarkTests(AdvisoryBase):
    def test_ratchets_up_only(self):
        a = self._agreement()
        self.assertTrue(advisory.ratchet_high_water_mark(
            a, date(2026, 3, 31), 100000.0, '2026-Q1'))
        self.assertFalse(advisory.ratchet_high_water_mark(
            a, date(2026, 6, 30), 90000.0, '2026-Q2'))   # lower — no new mark
        self.assertTrue(advisory.ratchet_high_water_mark(
            a, date(2026, 9, 30), 110000.0, '2026-Q3'))
        self.assertEqual(advisory.current_high_water_mark(a), 110000.0)
        self.assertEqual(HighWaterMark.query.count(), 2)


# ── performance fee ───────────────────────────────────────────────────────────

class PerformanceTests(AdvisoryBase):
    def test_hurdle_not_cleared_accrues_no_fee(self):
        a = self._agreement()
        # 4% annualized hurdle over the quarter beats a 1% portfolio return.
        self._hurdle_series(date(2026, 7, 1), date(2026, 9, 30), 4.0)
        # 4% annual hurdle over the quarter (~1%) beats a 0.3% return.
        snap = advisory.compute_performance(
            a, date(2026, 9, 30), opening_aum=100000.0, closing_aum=100300.0)
        self.assertFalse(snap.hurdle_cleared)
        self.assertEqual(snap.performance_fee_accrued, 0.0)
        self.assertIn('hurdle not cleared', snap.notes)

    def test_hurdle_cleared_accrues_a_fee_not_yet_posted(self):
        a = self._agreement()
        self._hurdle_series(date(2026, 7, 1), date(2026, 9, 30), 1.0)
        # ~20% quarter return crushes a 1% hurdle.
        snap = advisory.compute_performance(
            a, date(2026, 9, 30), opening_aum=100000.0, closing_aum=120000.0)
        self.assertTrue(snap.hurdle_cleared)
        self.assertTrue(snap.above_high_water_mark)
        self.assertGreater(snap.performance_fee_accrued, 0.0)
        accrual = advisory.accrue_performance_fee(snap)
        self.assertEqual(accrual.fee_type, 'performance')
        self.assertFalse(accrual.posted_to_erpnext)   # pays annually on approval
        self.assertEqual(len(GeneratedJournalEntry.query.all()), 0)

    def test_underwater_accrues_no_fee_even_if_hurdle_cleared(self):
        a = self._agreement()
        # Establish a high mark, then a quarter that beats the hurdle but stays
        # below the prior peak.
        advisory.ratchet_high_water_mark(a, date(2026, 6, 30), 200000.0, '2026-Q2')
        self._hurdle_series(date(2026, 7, 1), date(2026, 9, 30), 1.0)
        snap = advisory.compute_performance(
            a, date(2026, 9, 30), opening_aum=100000.0, closing_aum=150000.0)
        self.assertTrue(snap.hurdle_cleared)
        self.assertFalse(snap.above_high_water_mark)
        self.assertEqual(snap.performance_fee_accrued, 0.0)
        self.assertIn('high-water mark', snap.notes)

    def test_performance_is_idempotent(self):
        a = self._agreement()
        self._hurdle_series(date(2026, 7, 1), date(2026, 9, 30), 1.0)
        advisory.compute_performance(a, date(2026, 9, 30),
                                     opening_aum=100000.0, closing_aum=120000.0)
        advisory.compute_performance(a, date(2026, 9, 30),
                                     opening_aum=100000.0, closing_aum=120000.0)
        self.assertEqual(PerformanceSnapshot.query.count(), 1)


class HurdleFeedTests(AdvisoryBase):
    def test_no_fred_key_degrades_to_none(self):
        self.assertIsNone(advisory.poll_fred_hurdle())

    def test_manual_entry_and_return_math(self):
        advisory.record_hurdle_sample(date(2026, 7, 1), 4.0)
        advisory.record_hurdle_sample(date(2026, 7, 1), 4.5)  # overwrite
        self.assertEqual(HurdleRateSample.query.count(), 1)
        self._hurdle_series(date(2026, 7, 1), date(2026, 9, 30), 4.0)
        r = advisory.hurdle_return(date(2026, 7, 1), date(2026, 9, 30))
        self.assertAlmostEqual(r, 0.04 * (91 / 365.0), places=4)


# ── risk controls ─────────────────────────────────────────────────────────────

class RiskControlTests(AdvisoryBase):
    def test_a_concentrated_position_is_flagged(self):
        a = self._agreement(
            risk_control_config={'single_position_limit_pct': 10.0})
        self._holding('BIGCO', 90000.0)      # 90%
        self._holding('SMALLCO', 10000.0)    # 10%
        check = advisory.run_risk_check(a, on=date(2026, 7, 1))
        rules = {v['rule'] for v in check.violations}
        self.assertIn('single_position_limit', rules)
        big = [v for v in check.violations if v['ticker'] == 'BIGCO'][0]
        self.assertEqual(big['pct'], 90.0)

    def test_a_diversified_portfolio_has_no_violations(self):
        a = self._agreement(
            risk_control_config={'single_position_limit_pct': 10.0})
        for i in range(20):
            self._holding(f'CO{i}', 5000.0, qty=50)  # 5% each
        check = advisory.run_risk_check(a, on=date(2026, 7, 1))
        self.assertEqual(check.violations, [])

    def test_bitcoin_limit(self):
        a = self._agreement(risk_control_config={
            'single_position_limit_pct': 50.0, 'bitcoin_allocation_pct': 5.0})
        self._holding('BTC', 10000.0)        # 10% > 5%
        self._holding('TESTCO', 90000.0)
        check = advisory.run_risk_check(a, on=date(2026, 7, 1))
        self.assertIn('bitcoin_allocation',
                      {v['rule'] for v in check.violations})

    def test_checks_run_but_alerts_are_gated(self):
        a = self._agreement(risk_control_alerts_enabled=False,
                            risk_control_config={'single_position_limit_pct': 10.0})
        self._holding('BIGCO', 100000.0)
        check = advisory.run_risk_check(a, on=date(2026, 7, 1))
        self.assertTrue(check.violations)          # check ran
        from app.models import AuditEvent
        alerts = AuditEvent.query.filter_by(
            event_type='risk_control_violation').count()
        self.assertEqual(alerts, 0)                # but no alert fired

    def test_alerts_fire_when_enabled(self):
        a = self._agreement(risk_control_alerts_enabled=True,
                            risk_control_config={'single_position_limit_pct': 10.0})
        self._holding('BIGCO', 100000.0)
        advisory.run_risk_check(a, on=date(2026, 7, 1))
        from app.models import AuditEvent
        self.assertEqual(AuditEvent.query.filter_by(
            event_type='risk_control_violation').count(), 1)

    def test_risk_check_is_idempotent(self):
        a = self._agreement()
        self._holding('TESTCO', 100000.0)
        advisory.run_risk_check(a, on=date(2026, 7, 1))
        advisory.run_risk_check(a, on=date(2026, 7, 1))
        self.assertEqual(RiskControlCheck.query.count(), 1)


# ── dashboard + UI ────────────────────────────────────────────────────────────

class DashboardTests(AdvisoryBase):
    def test_dashboard_renders_stored_figures(self):
        a = self._agreement()
        self._holding('TESTCO', 100000.0)
        self._hurdle_series(date(2026, 7, 1), date(2026, 9, 30), 1.0)
        snap = advisory.compute_performance(
            a, date(2026, 9, 30), opening_aum=100000.0, closing_aum=120000.0)
        advisory.accrue_performance_fee(snap)
        d = advisory.dashboard(a)
        self.assertEqual(d['aum'], 105000.0)
        self.assertIsNotNone(d['latest_snapshot'])
        self.assertGreater(d['ytd_performance_fee'], 0.0)

    def test_the_advisory_pages_render(self):
        a = self._agreement()
        self._holding('TESTCO', 100000.0)
        client = self.app.test_client()
        self.assertEqual(client.get('/admin/advisory').status_code, 200)
        body = client.get(f'/admin/advisory/{a.id}').data.decode()
        self.assertEqual(client.get(f'/admin/advisory/{a.id}').status_code, 200)
        self.assertIn('TEST MANAGER', body)
        self.assertIn('Fee posting controls', body)

    def test_the_toggle_flips_a_switch(self):
        a = self._agreement()
        client = self.app.test_client()
        resp = client.post(f'/admin/advisory/{a.id}/toggle',
                           data={'switch': 'fee_accrual_enabled', 'enabled': '1'})
        self.assertEqual(resp.status_code, 302)
        db.session.expire_all()
        self.assertTrue(db.session.get(AdvisoryAgreement, a.id).fee_accrual_enabled)


if __name__ == '__main__':
    unittest.main()
