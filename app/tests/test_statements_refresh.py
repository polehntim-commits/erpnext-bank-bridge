# SPDX-License-Identifier: MIT
"""/statements/refresh (v1.0.6).

THE GAP. Plaid extracts an Item's statements once, at Link. Bank Bridge never
called /statements/refresh, so every monthly pull re-listed the Link-time set,
stored nothing and reported success; OML's ••6030 brokerage sat at June 2026
for three months. And statements_list swallowed every error into [], so a
failing listing read exactly like "nothing new".

Covered:
  * the scheduled path asks for a refresh, waits for the webhook, then lists
  * no webhook in time → it lists anyway (poll fallback) and leaves 'timeout'
  * a late webhook with nobody waiting fetches the Item's statements itself
  * a listing error lands on the pull record (PlaidSyncLog), classified
  * a refused refresh is recorded and the pull still lists
  * the refresh window starts a month before the newest held statement

Synthetic ids only.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import time
import unittest
import unittest.mock
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import db, statements as stmts  # noqa: E402
from app.models import PlaidItem, PlaidStatement, PlaidSyncLog  # noqa: E402
from app.plaid_client import PlaidClient, PlaidError  # noqa: E402

from tests.fakes import FakePlaidClient  # noqa: E402
from tests.test_statements import StatementsBase, checking_pdf  # noqa: E402


class RefreshBase(StatementsBase):
    def setUp(self):
        super().setUp()
        self._account()
        self._statement('st-jun', month=6, year=2026)
        self.plaid = FakePlaidClient(
            statements=[self._listed('st-jun', month=6)],
            statement_pdfs={'st-jun': checking_pdf(),
                            'st-jul': checking_pdf()})
        p = unittest.mock.patch.object(stmts, '_plaid_client_or_none',
                                       lambda c=None: self.plaid)
        p.start()
        self.addCleanup(p.stop)

    def make_july_listable(self):
        self.plaid.statements.append(self._listed('st-jul', month=7))

    def records(self, direction):
        return PlaidSyncLog.query.filter_by(direction=direction) \
            .order_by(PlaidSyncLog.id).all()

    def fresh_item(self):
        db.session.expire_all()
        return PlaidItem.query.filter_by(item_id='item-abc').one()


class ScheduledRefreshTests(RefreshBase):
    def test_refresh_then_webhook_then_list_stores_the_new_month(self):
        def webhook_arrives():
            self.make_july_listable()
            stmts.mark_refresh_complete('item-abc', 'SUCCESS')
        self.plaid.on_refresh = webhook_arrives
        result = stmts.fetch_all(refresh=True, wait_seconds=30,
                                 poll_seconds=0.01)
        self.assertEqual(1, result['refresh_requested'])
        self.assertEqual(1, result['refresh_completed'])
        self.assertEqual(1, result['stored'])
        self.assertIsNotNone(PlaidStatement.query.filter_by(
            statement_id='st-jul').first())
        names = [c[0] for c in self.plaid.calls]
        self.assertLess(names.index('statements_refresh'),
                        names.index('statements_list'))
        self.assertEqual('complete', self.fresh_item().statements_refresh_status)

    def test_no_webhook_lists_anyway_and_leaves_timeout(self):
        self.plaid.on_refresh = self.make_july_listable   # ready, but silent
        t0 = time.monotonic()
        result = stmts.fetch_all(refresh=True, wait_seconds=0.05,
                                 poll_seconds=0.01)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(1, result['refresh_timed_out'])
        self.assertEqual(1, result['stored'])
        self.assertEqual('timeout', self.fresh_item().statements_refresh_status)

    def test_a_late_webhook_with_nobody_waiting_fetches_itself(self):
        stmts.fetch_all(refresh=True, wait_seconds=0, poll_seconds=0.01)
        self.assertEqual('timeout', self.fresh_item().statements_refresh_status)
        self.make_july_listable()
        resp = self.app.test_client().post(
            '/bankbridge/api/plaid/webhook',
            json={'webhook_type': 'STATEMENTS',
                  'webhook_code': 'STATEMENTS_REFRESH_COMPLETE',
                  'item_id': 'item-abc', 'result': 'SUCCESS'})
        self.assertEqual(200, resp.status_code)
        self.assertIsNotNone(PlaidStatement.query.filter_by(
            statement_id='st-jul').first())
        self.assertEqual('complete', self.fresh_item().statements_refresh_status)

    def test_a_failure_webhook_is_recorded_and_fetches_nothing(self):
        self.fresh_item().statements_refresh_status = 'timeout'
        db.session.commit()
        self.assertFalse(stmts.mark_refresh_complete('item-abc', 'FAILURE'))
        self.assertEqual('failed', self.fresh_item().statements_refresh_status)
        self.assertEqual('failed', self.records('statements_refresh')[-1].status)

    def test_the_scheduler_job_refreshes(self):
        from app.services import scheduler
        self.app.config['STATEMENTS_REFRESH_WAIT_SECONDS'] = 0
        self.app.config['ERPNEXT_STATEMENTS_AUTO_SYNC'] = False
        with unittest.mock.patch('app.plaid_settings.is_configured',
                                 return_value=True):
            scheduler._run_statements_pull(self.app)
        self.assertIn('statements_refresh', [c[0] for c in self.plaid.calls])


class ErrorRecordTests(RefreshBase):
    def test_a_listing_error_lands_on_the_pull_record(self):
        self.plaid.statements_error = PlaidError(
            'statements_list failed: ITEM_LOGIN_REQUIRED')
        result = stmts.fetch_all()
        self.assertEqual(1, len(result['errors']))
        rec = self.records('statements_pull')[-1]
        self.assertEqual('failed', rec.status)
        self.assertIn('ITEM_LOGIN_REQUIRED', rec.error_message)
        self.assertIn('1 error(s)', stmts.pull_summary(result))

    def test_an_unsupported_item_is_classified_not_failed(self):
        self.plaid.statements_error = PlaidError(
            'statements_list failed: PRODUCTS_NOT_SUPPORTED')
        result = stmts.fetch_all()
        self.assertEqual(1, result['unsupported'])
        self.assertEqual('unsupported',
                         self.records('statements_pull')[-1].status)

    def test_a_refused_refresh_is_recorded_and_the_pull_still_lists(self):
        self.plaid.refresh_error = PlaidError('statements_refresh failed: '
                                              'RATE_LIMIT_EXCEEDED')
        result = stmts.fetch_all(refresh=True, wait_seconds=1,
                                 poll_seconds=0.01)
        self.assertEqual(0, result['refresh_requested'])
        self.assertEqual(1, result['listed'])
        rec = self.records('statements_refresh')[-1]
        self.assertEqual('failed', rec.status)
        self.assertIn('RATE_LIMIT_EXCEEDED', rec.error_message)
        self.assertEqual('failed', self.fresh_item().statements_refresh_status)

    def test_a_clean_pull_records_the_newest_period(self):
        stmts.fetch_all()
        rec = self.records('statements_pull')[-1]
        self.assertEqual('success', rec.status)
        self.assertIn('2026-06-30', rec.error_message)


class WindowAndClientTests(RefreshBase):
    def test_window_starts_a_month_before_the_newest_statement(self):
        start, end = stmts.refresh_window(self.fresh_item(), today=date(2026, 10, 3))
        self.assertEqual(date(2026, 10, 3), end)
        self.assertEqual(date(2026, 5, 31), start)   # still covers June

    def test_window_with_nothing_held_reaches_back_24_months(self):
        PlaidStatement.query.delete()
        db.session.commit()
        start, _ = stmts.refresh_window(self.fresh_item(), today=date(2026, 10, 3))
        self.assertEqual(date(2024, 10, 13), start)

    def test_the_client_sends_the_window_and_raises_on_failure(self):
        api = unittest.mock.Mock()
        api.statements_refresh.return_value = {'request_id': 'r-1'}
        rid = PlaidClient(api=api).statements_refresh(
            'access-x', date(2026, 6, 1), date(2026, 10, 3))
        self.assertEqual('r-1', rid)
        req = api.statements_refresh.call_args[0][0]
        self.assertEqual(date(2026, 6, 1), req.start_date)
        self.assertEqual(date(2026, 10, 3), req.end_date)
        api.statements_refresh.side_effect = Exception('INVALID_INPUT')
        with self.assertRaises(PlaidError):
            PlaidClient(api=api).statements_refresh(
                'a', date(2026, 6, 1), date(2026, 10, 3))

    def test_admin_pull_button_refreshes(self):
        resp = self.app.test_client().post('/admin/statements/pull')
        self.assertEqual(302, resp.status_code)
        self.assertIn('statements_refresh', [c[0] for c in self.plaid.calls])


if __name__ == '__main__':
    unittest.main()
