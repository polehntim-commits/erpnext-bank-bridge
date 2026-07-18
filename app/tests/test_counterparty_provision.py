# SPDX-License-Identifier: MIT
"""Counterparty doctype provisioning reaches an upgrading install (v0.4.6).

THE BUG. v0.4.5 shipped the overlay but only ever reached its provisioning code
through `erpnext_accounts.bootstrap`, which runs on ERPNext account IMPORT and
from the settings page. An install that had already imported its accounts under
an earlier version never ran it: the doctype was never created, no CREATE was
ever attempted, and the only symptom was 404s from the read paths.
`ensure_counterparty_doctype` was written to be called at startup — nothing
called it.

These tests pin the fix: a startup job that provisions once per container, a
report that says WHICH outcome occurred and why, and a management CLI to force
it without a restart.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import counterparty, erpnext_accounts  # noqa: E402
from app.models import AuditEvent  # noqa: E402
from app.services import scheduler  # noqa: E402

from scripts import provision_counterparty_doctype as cli  # noqa: E402
from tests.fakes import FakeERPClient  # noqa: E402


class ProvisionBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)


# ── the report: which outcome, and why ──────────────────────────────────────

class TestProvisionReport(ProvisionBase):
    def test_creates_and_says_so(self):
        client = FakeERPClient(counterparty_doctype=False)
        report = counterparty.provision_report(client)
        self.assertTrue(report['ok'])
        self.assertEqual(report['state'], counterparty.PROVISION_CREATED)
        self.assertIn('Counterparty', client.created['DocType'])

    def test_already_present_is_distinguishable_from_created(self):
        # v0.4.5 returned a bare True for both, which is why a never-provisioned
        # install looked exactly like a healthy one in the log.
        client = FakeERPClient(counterparty_doctype=True)
        report = counterparty.provision_report(client)
        self.assertTrue(report['ok'])
        self.assertEqual(report['state'], counterparty.PROVISION_PRESENT)
        self.assertEqual(client.created['DocType'], {})

    def test_permission_denial_reports_the_reason(self):
        client = FakeERPClient(doctype_permission_error=True)
        report = counterparty.provision_report(client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['state'], counterparty.PROVISION_PERMISSION)
        self.assertIn('Not permitted', report['reason'])
        self.assertIn('System Manager',
                      counterparty.PROVISION_HELP[report['state']])

    def test_failed_create_reports_the_reason(self):
        client = FakeERPClient(counterparty_doctype=False,
                               fail_doctype_create=True)
        report = counterparty.provision_report(client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['state'], counterparty.PROVISION_CREATE_FAILED)
        self.assertTrue(report['reason'])

    def test_unconfigured_erpnext_reports_not_configured(self):
        report = counterparty.provision_report(None)
        self.assertFalse(report['ok'])
        self.assertEqual(report['state'], counterparty.PROVISION_NO_CLIENT)

    def test_every_failure_state_carries_operator_help(self):
        for state in (counterparty.PROVISION_PERMISSION,
                      counterparty.PROVISION_NO_DOCTYPE_API,
                      counterparty.PROVISION_PROBE_FAILED,
                      counterparty.PROVISION_CREATE_FAILED,
                      counterparty.PROVISION_NO_CLIENT):
            self.assertTrue(counterparty.PROVISION_HELP.get(state),
                            f'{state} has no operator-facing explanation')

    def test_logs_name_the_outcome(self):
        client = FakeERPClient(counterparty_doctype=False)
        with self.assertLogs('bankbridge.counterparty', level='INFO') as cm:
            counterparty.provision_report(client)
        self.assertTrue(any('creating it' in m for m in cm.output))
        self.assertTrue(any('provisioned the Counterparty' in m
                            for m in cm.output))

    def test_already_present_is_logged_not_silent(self):
        # The v0.4.5 happy path returned True without a word, so a healthy
        # install and a broken one produced identical boot logs.
        client = FakeERPClient(counterparty_doctype=True)
        with self.assertLogs('bankbridge.counterparty', level='INFO') as cm:
            counterparty.provision_report(client)
        self.assertTrue(any('already present' in m for m in cm.output))

    def test_failure_is_logged_with_its_reason(self):
        client = FakeERPClient(doctype_permission_error=True)
        with self.assertLogs('bankbridge.counterparty', level='WARNING') as cm:
            counterparty.provision_report(client)
        blob = ' '.join(cm.output)
        self.assertIn('NOT provisioned', blob)
        self.assertIn(counterparty.PROVISION_PERMISSION, blob)

    def test_ensure_counterparty_doctype_keeps_its_boolean_contract(self):
        # 15 existing call sites depend on the bool. provision_report is the
        # structured form of the SAME work, not a replacement.
        self.assertTrue(counterparty.ensure_counterparty_doctype(
            FakeERPClient(counterparty_doctype=True)))
        self.assertFalse(counterparty.ensure_counterparty_doctype(
            FakeERPClient(doctype_permission_error=True)))

    def test_provision_is_idempotent(self):
        client = FakeERPClient(counterparty_doctype=False)
        states = [counterparty.provision_report(client)['state']
                  for _ in range(4)]
        self.assertEqual(states[0], counterparty.PROVISION_CREATED)
        self.assertEqual(set(states[1:]), {counterparty.PROVISION_PRESENT})
        creates = [c for c in client.calls
                   if c[0] == 'create_doc' and c[1] == 'DocType']
        self.assertEqual(len(creates), 1)


# ── the startup job: the wiring that was missing ────────────────────────────

class TestStartupProvision(ProvisionBase):
    def test_startup_job_provisions_the_doctype(self):
        client = FakeERPClient(counterparty_doctype=False)
        with mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=client):
            scheduler._run_counterparty_provision(self.app)
        self.assertIn('Counterparty', client.created['DocType'])
        self.assertTrue(counterparty.available(client))

    def test_startup_job_records_an_audit_event(self):
        client = FakeERPClient(counterparty_doctype=False)
        with mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=client):
            scheduler._run_counterparty_provision(self.app)
        ev = AuditEvent.query.filter_by(
            event_type='counterparty_doctype_provision').one()
        self.assertIn(counterparty.PROVISION_CREATED, (ev.notes or ''))

    def test_startup_job_logs_the_reason_when_it_cannot_provision(self):
        client = FakeERPClient(doctype_permission_error=True)
        with mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=client), \
             self.assertLogs('bankbridge.scheduler', level='WARNING') as cm:
            scheduler._run_counterparty_provision(self.app)
        blob = ' '.join(cm.output)
        self.assertIn('UNAVAILABLE', blob)
        self.assertIn('System Manager', blob)

    def test_startup_job_respects_the_master_switch(self):
        self.app.config['COUNTERPARTY_OVERLAY_ENABLED'] = False
        client = FakeERPClient(counterparty_doctype=False)
        with mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=client):
            scheduler._run_counterparty_provision(self.app)
        self.assertEqual(client.created['DocType'], {})

    def test_startup_job_survives_an_unconfigured_erpnext(self):
        with mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=None):
            scheduler._run_counterparty_provision(self.app)   # must not raise
        ev = AuditEvent.query.filter_by(
            event_type='counterparty_doctype_provision').one()
        self.assertIn(counterparty.PROVISION_NO_CLIENT, (ev.notes or ''))

    def test_startup_job_never_raises_on_an_erpnext_blowup(self):
        boom = mock.Mock(side_effect=RuntimeError('ERPNext exploded'))
        with mock.patch('app.sync_engine.get_erp_client_or_none', boom):
            scheduler._run_counterparty_provision(self.app)   # swallowed

    def test_startup_job_pairs_existing_parties(self):
        client = FakeERPClient(counterparty_doctype=False,
                               existing_suppliers=('Wells Fargo',),
                               existing_customers=('Wells Fargo',))
        with mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=client):
            scheduler._run_counterparty_provision(self.app)
        self.assertIn('Wells Fargo', client.created['Counterparty'])

    def test_startup_job_skips_pairing_when_auto_pair_is_off(self):
        self.app.config['COUNTERPARTY_AUTO_PAIR'] = False
        client = FakeERPClient(counterparty_doctype=False,
                               existing_suppliers=('Wells Fargo',))
        with mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=client):
            scheduler._run_counterparty_provision(self.app)
        self.assertIn('Counterparty', client.created['DocType'])
        self.assertEqual(client.created['Counterparty'], {})

    def test_scheduler_registers_the_provision_job(self):
        # The regression guard for the actual v0.4.5 bug: the job must be
        # ATTACHED to the elected scheduler, not merely defined.
        added = []

        class _FakeSched:
            def __init__(self, *a, **kw): pass
            def start(self): pass
            def add_job(self, fn, trigger, **kw):
                added.append((kw.get('id'), trigger))

        with mock.patch('apscheduler.schedulers.background.BackgroundScheduler',
                        _FakeSched), \
             mock.patch('app.plaid_settings.sync_interval_hours',
                        return_value=6), \
             mock.patch('fcntl.flock'):
            self.app.config['TESTING'] = False
            try:
                scheduler._schedulers.clear()
                scheduler.ensure_scheduler_started(self.app)
            finally:
                self.app.config['TESTING'] = True
                scheduler._schedulers.clear()
        ids = [i for i, _ in added]
        self.assertIn('counterparty_provision', ids)
        # A one-shot: there is nothing to re-check on an interval.
        self.assertEqual(
            [t for i, t in added if i == 'counterparty_provision'], ['date'])


# ── the management CLI ──────────────────────────────────────────────────────

class TestProvisionCli(ProvisionBase):
    def test_cli_creates_the_doctype(self):
        client = FakeERPClient(counterparty_doctype=False)
        report = cli.run(client, pair=False)
        self.assertTrue(report['ok'])
        self.assertEqual(report['state'], counterparty.PROVISION_CREATED)
        self.assertIn('Counterparty', client.created['DocType'])

    def test_cli_is_idempotent(self):
        client = FakeERPClient(counterparty_doctype=False)
        cli.run(client, pair=False)
        second = cli.run(client, pair=False)
        self.assertEqual(second['state'], counterparty.PROVISION_PRESENT)
        creates = [c for c in client.calls
                   if c[0] == 'create_doc' and c[1] == 'DocType']
        self.assertEqual(len(creates), 1)

    def test_cli_pairs_by_default(self):
        client = FakeERPClient(counterparty_doctype=False,
                               existing_suppliers=('Wells Fargo',),
                               existing_customers=('Wells Fargo',))
        report = cli.run(client)
        self.assertIsNotNone(report['paired'])
        self.assertIn('Wells Fargo', client.created['Counterparty'])

    def test_cli_no_pair_skips_pairing(self):
        client = FakeERPClient(counterparty_doctype=False,
                               existing_suppliers=('Wells Fargo',))
        report = cli.run(client, pair=False)
        self.assertIsNone(report['paired'])
        self.assertEqual(client.created['Counterparty'], {})

    def test_cli_reports_a_refusal_without_raising(self):
        client = FakeERPClient(doctype_permission_error=True)
        report = cli.run(client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['state'], counterparty.PROVISION_PERMISSION)
        self.assertIsNone(report['paired'])

    def test_cli_respects_the_master_switch(self):
        self.app.config['COUNTERPARTY_OVERLAY_ENABLED'] = False
        client = FakeERPClient(counterparty_doctype=False)
        report = cli.run(client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['state'], 'disabled')
        self.assertEqual(client.created['DocType'], {})

    def test_cli_exit_code_gates_a_deploy_step(self):
        ok = FakeERPClient(counterparty_doctype=False)
        with mock.patch('app.create_app', return_value=self.app), \
             mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=ok):
            self.assertEqual(cli.main(['--no-pair']), 0)
        bad = FakeERPClient(doctype_permission_error=True)
        with mock.patch('app.create_app', return_value=self.app), \
             mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=bad):
            self.assertEqual(cli.main(['--no-pair']), 1)

    def test_cli_reports_an_unconfigured_erpnext(self):
        with mock.patch('app.create_app', return_value=self.app), \
             mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=None):
            self.assertEqual(cli.main([]), 1)


# ── regression: the v0.4.5 path still works ─────────────────────────────────

class TestExistingBootstrapPathUnchanged(ProvisionBase):
    def test_account_import_bootstrap_still_provisions(self):
        client = FakeERPClient(counterparty_doctype=False)
        status = erpnext_accounts.bootstrap(client)
        self.assertTrue(status[counterparty.COUNTERPARTY_DT])
        self.assertIn('Counterparty', client.created['DocType'])

    def test_a_refused_overlay_is_still_not_a_partial_import_bootstrap(self):
        client = FakeERPClient(doctype_permission_error=True)
        status = erpnext_accounts.bootstrap(client)
        self.assertFalse(status[counterparty.COUNTERPARTY_DT])
        self.assertFalse(status['partial'])


if __name__ == '__main__':
    unittest.main()
