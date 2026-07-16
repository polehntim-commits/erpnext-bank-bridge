# SPDX-License-Identifier: MIT
"""User-configurable sync frequency + cost-aware presets + daily call brake.

  * manual-only mode (interval <= 0) → the scheduler adds no auto-poll job
  * preset cost math: daily = one sync/day, hourly = 24, manual = 0
  * PLAID_MAX_CALLS_PER_DAY brake skips a pull once the Item hits its limit
  * the interval persists via plaid_settings and wins over the env seed

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import sync_config, sync_engine, plaid_settings  # noqa: E402
from app.services import scheduler  # noqa: E402
from app.models import PlaidItem, PlaidSyncLog  # noqa: E402

from tests.fakes import FakePlaidClient, page, txn  # noqa: E402

ACC = 'acct-wf-checking'


class FreqBase(unittest.TestCase):
    EXTRA = {}

    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        cfg = {
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        }
        cfg.update(self.EXTRA)
        self.app = create_app(cfg)
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _item(self, item_id='item-abc'):
        it = PlaidItem(
            item_id=item_id,
            access_token_encrypted=crypto.encrypt('access-sandbox-abc'),
            institution_id='ins_1', institution_name='Wells Fargo',
            status='active')
        db.session.add(it)
        db.session.commit()
        return it


class TestPresetCostMath(unittest.TestCase):
    """Pure preset/cost helpers — no app context needed."""

    def test_daily_interval_generates_one_call_per_day(self):
        # Daily = exactly one automatic sync per day per item.
        self.assertEqual(sync_config.syncs_per_day(24), 1.0)
        self.assertEqual(sync_config.monthly_calls(24, accounts=1), 30.0)
        # Hourly is 24×, 6-hourly is 4×, weekly is a fraction.
        self.assertEqual(sync_config.syncs_per_day(1), 24.0)
        self.assertEqual(sync_config.syncs_per_day(6), 4.0)
        self.assertAlmostEqual(sync_config.syncs_per_day(168), 24.0 / 168)

    def test_manual_is_zero_calls(self):
        for manual in (0, -1, -999):
            self.assertEqual(sync_config.syncs_per_day(manual), 0.0)
            self.assertEqual(sync_config.monthly_calls(manual, 5), 0.0)
            self.assertFalse(sync_config.is_auto_sync_enabled(manual))

    def test_cost_estimate_scales_with_accounts_and_price(self):
        # Daily, 2 accounts, $0.30/call → 30 * 2 * 0.30 = $18.00/month.
        self.assertEqual(
            sync_config.monthly_cost_estimate(24, accounts=2, price_per_call=0.30),
            18.00)
        self.assertEqual(sync_config.monthly_cost_estimate(0, accounts=9), 0.0)

    def test_normalize_and_labels(self):
        self.assertEqual(sync_config.normalize_interval('24'), 24)
        self.assertEqual(sync_config.normalize_interval('garbage'), 24)
        self.assertEqual(sync_config.normalize_interval(-5), 0)
        self.assertIn('Daily', sync_config.preset_label(24))
        self.assertIn('Manual', sync_config.preset_label(0))


class TestManualOnlyDisablesScheduler(FreqBase):
    def test_manual_only_mode_disables_scheduler(self):
        # The scheduler's job-planning helper returns None for manual-only, so
        # no auto-poll job is ever added; a positive interval returns its hours.
        self.assertIsNone(scheduler.poll_interval_or_none(0))
        self.assertIsNone(scheduler.poll_interval_or_none(-1))
        self.assertEqual(scheduler.poll_interval_or_none(24), 24)
        self.assertEqual(scheduler.poll_interval_or_none(6), 6)

        # And the persisted admin setting drives that decision end-to-end.
        plaid_settings.save('CID', 'sandbox', sync_interval_hours=0)
        self.assertEqual(plaid_settings.sync_interval_hours(), 0)
        self.assertIsNone(
            scheduler.poll_interval_or_none(plaid_settings.sync_interval_hours()))

        plaid_settings.save('CID', 'sandbox', sync_interval_hours=24)
        self.assertEqual(plaid_settings.sync_interval_hours(), 24)
        self.assertEqual(
            scheduler.poll_interval_or_none(plaid_settings.sync_interval_hours()),
            24)


class TestIntervalPersistence(FreqBase):
    def test_interval_persists_and_wins_over_env_default(self):
        # Default seeds from config (24 = daily) until overridden.
        self.assertEqual(plaid_settings.sync_interval_hours(), 24)
        # A re-save of unrelated fields must not clobber the stored interval.
        plaid_settings.save('CID', 'sandbox', sync_interval_hours=168)
        self.assertEqual(plaid_settings.sync_interval_hours(), 168)
        plaid_settings.save('CID2', 'production')  # no interval passed
        self.assertEqual(plaid_settings.sync_interval_hours(), 168)


class TestDailyCallBrake(FreqBase):
    EXTRA = {'PLAID_MAX_CALLS_PER_DAY': 2}

    def _pages(self):
        return [page(added=[txn('t1', ACC, 10.0)], has_more=False)]

    def test_max_calls_per_day_brake_engages(self):
        item = self._item()
        # Two pulls already logged today for this item → at the limit.
        for _ in range(2):
            db.session.add(PlaidSyncLog(
                item_id=item.item_id, direction='plaid_pull', count=1,
                status='success'))
        db.session.commit()

        fake = FakePlaidClient(
            accounts=[{'account_id': ACC, 'name': 'Checking', 'mask': '1234',
                       'type': 'depository', 'subtype': 'checking',
                       'balance_available': 1.0, 'balance_current': 2.0,
                       'iso_currency_code': 'USD'}],
            pages=self._pages())
        res = sync_engine.sync_item(item, plaid_client=fake)

        # Pull was skipped: no Plaid call was made and a 'skipped' row was logged.
        self.assertEqual(res.get('skipped'), 'max_calls_per_day')
        self.assertEqual(fake.calls, [])
        skipped = PlaidSyncLog.query.filter_by(
            item_id=item.item_id, status='skipped').count()
        self.assertEqual(skipped, 1)

    def test_brake_lets_pull_through_under_limit(self):
        item = self._item()
        # Only one pull logged today; limit is 2 → the next pull proceeds.
        db.session.add(PlaidSyncLog(
            item_id=item.item_id, direction='plaid_pull', count=1,
            status='success'))
        db.session.commit()

        fake = FakePlaidClient(
            accounts=[{'account_id': ACC, 'name': 'Checking', 'mask': '1234',
                       'type': 'depository', 'subtype': 'checking',
                       'balance_available': 1.0, 'balance_current': 2.0,
                       'iso_currency_code': 'USD'}],
            pages=self._pages())
        res = sync_engine.sync_item(item, plaid_client=fake)

        self.assertNotIn('skipped', res)
        # The real pull happened (Plaid was called) and added the transaction.
        self.assertTrue(any(c[0] == 'transactions_sync' for c in fake.calls))
        self.assertEqual(res['pull']['added'], 1)


if __name__ == '__main__':
    unittest.main()
