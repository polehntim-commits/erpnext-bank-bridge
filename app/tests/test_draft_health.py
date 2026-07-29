# SPDX-License-Identifier: MIT
"""Draft Journal Entry health — the count, the learned threshold, the crossing
(v0.8.5).

WHY THIS EXISTS. The v0.8.4 sync left 112 duplicate settlement-leg drafts
standing for hours. They were found by a human noticing the queue grow. Nothing
in Bank Bridge knew what a normal draft count looked like on this install, so
nothing could tell that this one wasn't.

The two principles under test, not just the arithmetic:

  * DATA DRIVEN, NOT HAND-CODED — the threshold is the P95 of this install's own
    history once there is enough of it, and 50 only until then. Breached samples
    are excluded from the baseline, so an explosion cannot teach the alarm to
    tolerate explosions.
  * KAIROS OVER CHRONOS — the read is a query, answered whenever asked. The
    ACTION fires on the TRANSITION into a breach and on the way back out, and on
    no other reading. Polling a breached queue does not re-alert.

Synthetic amounts only.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, crypto, db, draft_health  # noqa: E402
from app import erpnext_settings  # noqa: E402
from app.erpnext_client import ERPNextAPIError  # noqa: E402
from app.models import AuditEvent, DraftHealthSample  # noqa: E402

from tests.fakes import FakeERPClient  # noqa: E402

COMPANY = 'Orchard Example, LLC'


class DraftHealthBase(unittest.TestCase):
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
        self.client = FakeERPClient()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _draft(self, name, remark, amount=0.0, *, posting_date='2026-07-10',
               creation='2026-07-10 12:00:00', docstatus=0, company=COMPANY):
        self.client.created['Journal Entry'][name] = {
            'company': company, 'user_remark': remark,
            'posting_date': posting_date,
            'accounts': [{'account': 'X - EC',
                          'debit_in_account_currency': amount},
                         {'account': 'Y - EC',
                          'credit_in_account_currency': amount}]}
        self.client.creations[name] = creation
        if docstatus == 1:
            self.client.submitted.add(name)
        elif docstatus == 2:
            self.client.cancelled.add(name)
        return name

    def _history(self, counts, *, breached=False, company=''):
        for n in counts:
            db.session.add(DraftHealthSample(
                company=company, draft_count=n, threshold=50,
                threshold_source='default', breached=breached))
        db.session.commit()


# ── the grouping ────────────────────────────────────────────────────────────

class RemarkPrefixTests(unittest.TestCase):
    """The bucket key is ONE rule, not a per-pipeline table — a table would go
    stale the first time a remark changed and nobody noticed."""

    def test_it_groups_the_remarks_the_pipelines_actually_write(self):
        cases = [
            ('Cash: BANK DEPOSIT SWEEP $500.00 — withdrawal', 'Cash'),
            ('Bought 100 TEST-AAPL at $150.00 = $15,000.00', 'Bought'),
            ('Sold 50 TESTCO at $10.00 = $500.00', 'Sold'),
            ('Cash withdrawal from settlement', 'Cash withdrawal'),
            ('Fee: advisory $200.00', 'Fee'),
            ('TESTCO FUEL — fuel purchase', 'TESTCO FUEL'),
        ]
        for remark, expected in cases:
            with self.subTest(remark=remark):
                self.assertEqual(expected, draft_health.remark_prefix(remark))

    def test_a_missing_remark_gets_its_own_bucket(self):
        """A pile of remarkless drafts is itself a signal — usually something
        posting outside Bank Bridge — so it must not silently join another
        group."""
        self.assertEqual('(no remark)', draft_health.remark_prefix(''))
        self.assertEqual('(no remark)', draft_health.remark_prefix(None))

    def test_a_remark_that_is_all_numbers_still_gets_a_bucket(self):
        self.assertEqual('(other)', draft_health.remark_prefix('12345 678'))


# ── the read ────────────────────────────────────────────────────────────────

class SnapshotTests(DraftHealthBase):
    def test_it_counts_only_drafts(self):
        """A submitted or cancelled entry is not in the queue. Counting them
        would make the alarm fire on a healthy, well-posted ledger."""
        self._draft('ACC-JV-0001', 'Cash: sweep', 100.0)
        self._draft('ACC-JV-0002', 'Bought 1 X at $2.00', 200.0, docstatus=1)
        self._draft('ACC-JV-0003', 'Sold 1 X at $3.00', 300.0, docstatus=2)
        snap = draft_health.snapshot(self.client, COMPANY)
        self.assertEqual(1, snap['draft_count'])
        self.assertEqual(100.0, snap['total_amount'])

    def test_it_sums_the_dollar_volume_sitting_in_drafts(self):
        self._draft('ACC-JV-0001', 'Cash: sweep', 1000.0)
        self._draft('ACC-JV-0002', 'Cash: sweep', 250.50)
        snap = draft_health.snapshot(self.client, COMPANY)
        self.assertEqual(2, snap['draft_count'])
        self.assertEqual(1250.50, snap['total_amount'])

    def test_it_reports_the_oldest_posting_date_and_creation(self):
        """The v0.8.4 duplicates were all pre-2024-12-01 — an oldest posting
        date far behind the rest is exactly the shape of that incident."""
        self._draft('ACC-JV-0001', 'Cash: sweep', 10.0,
                    posting_date='2026-07-10', creation='2026-07-10 09:00:00')
        self._draft('ACC-JV-0002', 'Cash: sweep', 10.0,
                    posting_date='2024-11-30', creation='2026-07-29 22:19:00')
        snap = draft_health.snapshot(self.client, COMPANY)
        self.assertEqual('2024-11-30', snap['oldest_posting_date'])
        self.assertEqual('2026-07-10T09:00:00', snap['oldest_created_at'])

    def test_it_groups_by_remark_prefix_with_counts_and_amounts(self):
        self._draft('ACC-JV-0001', 'Cash: sweep out', 100.0)
        self._draft('ACC-JV-0002', 'Cash: sweep in', 50.0)
        self._draft('ACC-JV-0003', 'Bought 10 TEST-AAPL at $5.00', 50.0)
        snap = draft_health.snapshot(self.client, COMPANY)
        self.assertEqual({'count': 2, 'amount': 150.0},
                         snap['by_prefix']['Cash'])
        self.assertEqual({'count': 1, 'amount': 50.0},
                         snap['by_prefix']['Bought'])

    def test_the_largest_group_sorts_first(self):
        """The operator's first question in a breach is 'which pipeline?'."""
        self._draft('ACC-JV-0001', 'Bought 1 X at $1.00', 1.0)
        for i in range(3):
            self._draft(f'ACC-JV-100{i}', 'Cash: sweep', 1.0)
        snap = draft_health.snapshot(self.client, COMPANY)
        self.assertEqual('Cash', next(iter(snap['by_prefix'])))

    def test_an_empty_queue_is_healthy_not_broken(self):
        snap = draft_health.snapshot(self.client, COMPANY)
        self.assertEqual(0, snap['draft_count'])
        self.assertFalse(snap['breached'])
        self.assertEqual('ok', snap['status'])

    def test_an_unreadable_ledger_raises_rather_than_reporting_zero(self):
        """The one wrong answer this could give. draft_count=0 from an
        unreachable ERPNext reads identically to a perfectly clean queue."""
        with mock.patch.object(self.client, 'list_docs',
                               side_effect=ERPNextAPIError('down',
                                                           status_code=None)):
            with self.assertRaises(ERPNextAPIError):
                draft_health.snapshot(self.client, COMPANY)


# ── the threshold ───────────────────────────────────────────────────────────

class ThresholdTests(DraftHealthBase):
    def test_it_starts_on_the_default_with_no_history(self):
        threshold, source, samples = draft_health.learned_threshold()
        self.assertEqual(draft_health.DEFAULT_THRESHOLD, threshold)
        self.assertEqual('default', source)
        self.assertEqual(0, samples)

    def test_it_stays_on_the_default_below_the_sample_floor(self):
        """A P95 over five readings is an artifact of when they were taken."""
        self._history([5] * (draft_health.MIN_SAMPLES_FOR_BASELINE - 1))
        threshold, source, _ = draft_health.learned_threshold()
        self.assertEqual(draft_health.DEFAULT_THRESHOLD, threshold)
        self.assertEqual('default', source)

    def test_it_learns_from_this_installs_own_history(self):
        """Data driven: an install whose queue habitually runs at 100 should not
        be paged at 51, and one that runs at 4 should not need 50 to notice."""
        self._history([100] * draft_health.MIN_SAMPLES_FOR_BASELINE)
        threshold, source, samples = draft_health.learned_threshold()
        self.assertEqual('baseline_p95', source)
        self.assertEqual(draft_health.MIN_SAMPLES_FOR_BASELINE, samples)
        self.assertEqual(125, threshold)          # 100 P95 × 1.25 headroom

    def test_a_learned_threshold_never_drops_below_the_floor(self):
        """A flat-zero history would compute a P95 of 0 and make the first
        legitimate draft an emergency — which trains the operator to ignore
        the alarm entirely."""
        self._history([0] * draft_health.MIN_SAMPLES_FOR_BASELINE)
        threshold, source, _ = draft_health.learned_threshold()
        self.assertEqual('baseline_p95', source)
        self.assertEqual(draft_health.MIN_LEARNED_THRESHOLD, threshold)

    def test_breached_samples_are_excluded_from_the_baseline(self):
        """The failure mode that kills adaptive alarms: learning from your own
        explosions until nothing is an explosion any more."""
        self._history([4] * draft_health.MIN_SAMPLES_FOR_BASELINE)
        self._history([100000] * 50, breached=True)
        threshold, source, samples = draft_health.learned_threshold()
        self.assertEqual('baseline_p95', source)
        self.assertEqual(draft_health.MIN_SAMPLES_FOR_BASELINE, samples)
        self.assertEqual(draft_health.MIN_LEARNED_THRESHOLD, threshold)

    def test_a_company_scope_learns_from_its_own_samples_only(self):
        """A single-Company total and an all-Companies total are different
        measurements; mixing them would learn a threshold that describes
        neither."""
        self._history([100] * draft_health.MIN_SAMPLES_FOR_BASELINE,
                      company='Other Co')
        threshold, source, _ = draft_health.learned_threshold(COMPANY)
        self.assertEqual('default', source)
        self.assertEqual(draft_health.DEFAULT_THRESHOLD, threshold)

    def test_an_explicit_threshold_overrides_the_learned_one(self):
        """The what-if the MCP tool exposes — it must not pollute the baseline
        with a source of 'baseline_p95' it did not come from."""
        self._draft('ACC-JV-0001', 'Cash: sweep', 1.0)
        snap = draft_health.snapshot(self.client, COMPANY, threshold=0)
        self.assertTrue(snap['breached'])
        self.assertEqual('explicit', snap['threshold_source'])


# ── the action, which is kairotic ───────────────────────────────────────────

class ObservationTests(DraftHealthBase):
    def _flood(self, n, prefix='Cash: sweep'):
        for i in range(n):
            self._draft(f'ACC-JV-9{i:03d}', prefix, 1.0)

    def test_an_observation_is_persisted_as_history(self):
        self._draft('ACC-JV-0001', 'Cash: sweep', 42.0)
        draft_health.observe(self.client, COMPANY)
        rows = DraftHealthSample.query.all()
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0].draft_count)
        self.assertEqual(42.0, rows[0].total_amount)
        self.assertEqual(COMPANY, rows[0].company)

    def test_crossing_the_threshold_fires_exactly_once(self):
        """KAIROS. The state changing is the event. The second identical alert
        is the one that teaches an operator to stop reading them."""
        self._flood(draft_health.DEFAULT_THRESHOLD + 1)
        first = draft_health.observe(self.client, COMPANY)
        second = draft_health.observe(self.client, COMPANY)
        self.assertTrue(first['crossed'])
        self.assertFalse(second['crossed'])
        self.assertTrue(second['breached'])
        events = AuditEvent.query.filter_by(
            event_type='draft_health_threshold_crossed').all()
        self.assertEqual(1, len(events))

    def test_coming_back_under_fires_a_recovery(self):
        self._flood(draft_health.DEFAULT_THRESHOLD + 1)
        draft_health.observe(self.client, COMPANY)
        self.client.created['Journal Entry'].clear()
        back = draft_health.observe(self.client, COMPANY)
        self.assertTrue(back['recovered'])
        self.assertFalse(back['breached'])
        self.assertEqual(1, AuditEvent.query.filter_by(
            event_type='draft_health_recovered').count())

    def test_a_healthy_reading_fires_nothing(self):
        """Chronos gathers; state decides. A poll that finds nothing new must
        cost nothing but a history row."""
        self._draft('ACC-JV-0001', 'Cash: sweep', 1.0)
        snap = draft_health.observe(self.client, COMPANY)
        self.assertFalse(snap['crossed'])
        self.assertFalse(snap['recovered'])
        self.assertEqual(0, AuditEvent.query.filter(
            AuditEvent.event_type.like('draft_health%')).count())

    def test_the_alert_payload_names_the_group_that_ran_away(self):
        """FAIL FORWARD: the drafts get deleted, so the audit row is the only
        thing left that says which pipeline produced them."""
        self._flood(draft_health.DEFAULT_THRESHOLD + 1)
        draft_health.observe(self.client, COMPANY)
        ev = AuditEvent.query.filter_by(
            event_type='draft_health_threshold_crossed').first()
        self.assertIsNotNone(ev)
        self.assertIn('Cash', ev.payload_after)

    def test_refreshing_the_page_cannot_manufacture_a_baseline(self):
        """`observe` runs on every page load and every MCP call. Without the
        debounce, twenty refreshes in a minute would satisfy the sample floor
        and 'learn' a threshold from one moment's reading — which is a
        hand-coded number wearing a data-driven costume."""
        self._draft('ACC-JV-0001', 'Cash: sweep', 1.0)
        for _ in range(10):
            draft_health.observe(self.client, COMPANY)
        self.assertEqual(1, DraftHealthSample.query.count())

    def test_a_state_change_is_always_recorded_however_recent(self):
        """The debounce must never suppress a crossing: that row is what stops
        the same alert firing twice."""
        self._draft('ACC-JV-0001', 'Cash: sweep', 1.0)
        draft_health.observe(self.client, COMPANY)
        self._flood(draft_health.DEFAULT_THRESHOLD + 1)
        crossing = draft_health.observe(self.client, COMPANY)
        self.assertTrue(crossing['crossed'])
        self.assertTrue(crossing['recorded'])
        self.assertEqual(2, DraftHealthSample.query.count())
        # …and the alert still fires exactly once afterwards.
        self.assertFalse(draft_health.observe(self.client, COMPANY)['crossed'])
        self.assertEqual(1, AuditEvent.query.filter_by(
            event_type='draft_health_threshold_crossed').count())

    def test_observe_quietly_swallows_an_unreadable_ledger(self):
        """The sync calls this AFTER it has already posted everything. A health
        read that failed must not turn a successful sync into a failed one."""
        with mock.patch.object(self.client, 'list_docs',
                               side_effect=ERPNextAPIError('down',
                                                           status_code=None)):
            self.assertIsNone(
                draft_health.observe_quietly(self.client, COMPANY))

    def test_observe_quietly_is_a_no_op_without_a_client(self):
        self.assertIsNone(draft_health.observe_quietly(None))


# ── the operator-facing surfaces ────────────────────────────────────────────

class RoutesTests(DraftHealthBase):
    def setUp(self):
        super().setUp()
        self.http = self.app.test_client()

    def _patch_client(self):
        from app import sync_engine
        return mock.patch.object(sync_engine, 'get_erp_client_or_none',
                                 return_value=self.client)

    def test_the_json_endpoint_returns_the_reading(self):
        self._draft('ACC-JV-0001', 'Cash: sweep', 75.0)
        with self._patch_client():
            body = self.http.get('/admin/draft_health').get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(1, body['draft_count'])
        self.assertEqual(75.0, body['total_amount'])
        self.assertIn('Cash', body['by_prefix'])

    def test_the_json_endpoint_says_so_when_erpnext_is_unreachable(self):
        """503 with a reason, not a 200 with a zero — see snapshot()."""
        with mock.patch.object(self.client, 'list_docs',
                               side_effect=ERPNextAPIError('down',
                                                           status_code=None)):
            with self._patch_client():
                resp = self.http.get('/admin/draft_health')
        self.assertEqual(503, resp.status_code)
        self.assertFalse(resp.get_json()['ok'])

    def test_the_dashboard_renders(self):
        """Every new admin route gets a GET in its own test — a reserved
        context key 500s the page and nothing else catches it."""
        self._draft('ACC-JV-0001', 'Cash: sweep', 75.0)
        with self._patch_client():
            resp = self.http.get('/admin/draft_health.html')
        self.assertEqual(200, resp.status_code)
        body = resp.get_data(as_text=True)
        self.assertIn('Draft Journal Entry health', body)
        self.assertIn('Cash', body)

    def test_the_dashboard_renders_without_erpnext(self):
        from app import sync_engine
        with mock.patch.object(sync_engine, 'get_erp_client_or_none',
                               return_value=None):
            resp = self.http.get('/admin/draft_health.html')
        self.assertEqual(200, resp.status_code)
        self.assertIn('Could not read ERPNext', resp.get_data(as_text=True))

    def test_the_mcp_tool_is_read_only_and_always_available(self):
        """A mutating tool is gated behind a kill switch that defaults OFF. This
        one cannot change the books, and an AI operator that cannot see the
        queue is exactly the operator the v0.8.4 incident had."""
        from app.blueprints.mcp_server import TOOLS
        self.assertIn('get_draft_health', TOOLS)
        self.assertFalse(TOOLS['get_draft_health']['mutating'])

    def test_the_mcp_tool_returns_the_reading(self):
        from app.blueprints import mcp_server
        self._draft('ACC-JV-0001', 'Cash: sweep', 12.0)
        with mock.patch.object(mcp_server, '_erp_client_or_error',
                               return_value=self.client):
            result, summary = mcp_server.TOOLS['get_draft_health']['handler'](
                {'company': COMPANY})
        self.assertEqual(1, result['draft_count'])
        self.assertIn('1 draft Journal Entries', summary)

    def test_the_mcp_tool_errors_rather_than_reporting_an_empty_queue(self):
        from app.blueprints import mcp_server
        with mock.patch.object(self.client, 'list_docs',
                               side_effect=ERPNextAPIError('down',
                                                           status_code=None)):
            with mock.patch.object(mcp_server, '_erp_client_or_error',
                                   return_value=self.client):
                with self.assertRaises(mcp_server.ToolError):
                    mcp_server.TOOLS['get_draft_health']['handler']({})


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
