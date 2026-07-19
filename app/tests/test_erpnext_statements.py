# SPDX-License-Identifier: MIT
"""Bank statements inside ERPNext (v0.4.10).

The gap this closes: v0.4.9 put the bank's own PDFs in Bank Bridge, which is
where nobody's bookkeeper works. This release provisions a `Bank Statement`
doctype over REST and uploads each statement into it with the PDF attached, so
the answer to "show me March for this account" lives where the accounting does.

Covered here:

  * PROVISIONING, and every way ERPNext can say no — including the one
    Counterparty never had to consider: a `Bank Statement` doctype that already
    exists and is NOT ours, which must be refused rather than written to
  * the wiring that made v0.4.5 ship broken — provisioning is reached from the
    elected scheduler at boot, not only from a path an already-imported install
    never walks again
  * upload: the record's fields, the PDF attachment, and the deliberate choice
    that a FAILED attach still counts as a synced record (the reports run on the
    fields, and the PDF retries on the next tick)
  * idempotency on two independent levels — the local `erpnext_docname` column
    and ERPNext's unique `plaid_statement_id` — including the case the second
    exists for: a restored data volume whose column is blank for records that
    do exist, which must ADOPT rather than duplicate
  * reconciliation flowing one way, Bank Bridge → ERPNext, diffed so a settled
    install writes nothing
  * both reports, and their fallback to local data when ERPNext is unreachable
  * graceful degradation: a 500, an expired key, a disabled switch — each leaves
    local state untouched and retries next tick
  * regressions: v0.4.9 local storage, v0.4.5 Counterparty provisioning and
    v0.4.4 opening-balance anchoring all still behave

    cd app
    python3 -m unittest discover -s tests -v
"""
import unittest
import unittest.mock
from datetime import date, datetime

from app import counterparty, db  # noqa: E402
from app import erpnext_statements as es  # noqa: E402
from app import statements as stmts  # noqa: E402
from app.services import scheduler  # noqa: E402

from tests.fakes import FakeERPClient  # noqa: E402
from tests.test_statements import StatementsBase  # noqa: E402


class ERPStatementBase(StatementsBase):
    """StatementsBase plus a provisioned ERPNext, since almost everything here
    starts from "the doctype exists"."""

    def _client(self, **kwargs):
        kwargs.setdefault('bank_statement_doctype', True)
        return FakeERPClient(**kwargs)

    def _synced(self, client, statement):
        """The ERPNext Bank Statement record for a local statement, or None."""
        return es.find_by_plaid_id(client, statement.statement_id)


# ── provisioning ────────────────────────────────────────────────────────────

class ProvisioningTests(ERPStatementBase):
    def test_creates_the_doctype_when_absent(self):
        client = FakeERPClient(bank_statement_doctype=False)
        report = es.provision_report(client)
        self.assertTrue(report['ok'])
        self.assertEqual(report['state'], es.PROVISION_CREATED)
        self.assertIn('Bank Statement', client.created['DocType'])

    def test_is_idempotent(self):
        """The second run must not POST a second DocType — this is what makes
        the +20s boot job safe to run on every container start."""
        client = FakeERPClient(bank_statement_doctype=False)
        es.provision_report(client)
        creates = len(client.creates_of('DocType'))
        report = es.provision_report(client)
        self.assertTrue(report['ok'])
        self.assertEqual(report['state'], es.PROVISION_PRESENT)
        self.assertEqual(len(client.creates_of('DocType')), creates)

    def test_spec_declares_the_fields_the_feature_depends_on(self):
        spec = es.bank_statement_doctype_spec()
        by_name = {f['fieldname']: f for f in spec['fields']}
        self.assertEqual(spec['name'], 'Bank Statement')
        self.assertEqual(spec['custom'], 1)
        self.assertEqual(spec['autoname'], 'hash')
        # The unique key IS the idempotency guard; losing it would let a
        # re-run duplicate every statement.
        self.assertEqual(by_name['plaid_statement_id']['unique'], 1)
        self.assertEqual(by_name['plaid_statement_id']['reqd'], 1)
        self.assertEqual(by_name['bank_account']['options'], 'Bank Account')
        self.assertEqual(by_name['bank_account']['reqd'], 1)
        self.assertEqual(by_name['statement_pdf']['fieldtype'], 'Attach')
        self.assertIn('Discrepancy',
                      by_name['reconciliation_status']['options'])
        # Frappe renders a label-less field blank in the desk (the v0.4.5
        # lesson); every field must carry one.
        for field in spec['fields']:
            self.assertTrue(field.get('label'), field['fieldname'])

    def test_permission_denied_is_reported_not_raised(self):
        client = FakeERPClient(doctype_permission_error=True)
        report = es.provision_report(client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['state'], es.PROVISION_PERMISSION)
        self.assertIn('System Manager', es.PROVISION_HELP[report['state']])
        self.assertFalse(es.available(client))

    def test_missing_doctype_api_is_reported(self):
        client = FakeERPClient(missing_doctypes={'DocType'})
        report = es.provision_report(client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['state'], es.PROVISION_NO_DOCTYPE_API)

    def test_probe_failure_is_reported(self):
        client = FakeERPClient()
        with unittest.mock.patch.object(
                client, 'get_doc',
                side_effect=_api_error(503, 'gateway down')):
            report = es.provision_report(client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['state'], es.PROVISION_PROBE_FAILED)

    def test_create_failure_is_reported(self):
        client = FakeERPClient(bank_statement_doctype=False,
                               fail_doctype_create=True)
        report = es.provision_report(client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['state'], es.PROVISION_CREATE_FAILED)
        self.assertIn('cannot', report['reason'])

    def test_no_client_is_reported(self):
        report = es.provision_report(None)
        self.assertFalse(report['ok'])
        self.assertEqual(report['state'], es.PROVISION_NO_CLIENT)

    def test_refuses_a_bank_statement_doctype_it_did_not_create(self):
        """'Bank Statement' is a plausible enough name that another app could
        already own it. Writing our records into a stranger's doctype is the
        one failure here that would damage someone else's data, so an existing
        doctype without `plaid_statement_id` is refused outright."""
        client = FakeERPClient(foreign_bank_statement_doctype=True)
        report = es.provision_report(client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['state'], es.PROVISION_FOREIGN)
        self.assertFalse(es.available(client))
        # And nothing was written to it.
        self.assertEqual(client.created['Bank Statement'], {})

    def test_losing_the_create_race_counts_as_provisioned(self):
        """Two gunicorn workers can reach this at once. The loser must report
        success — the doctype it wanted now exists."""
        client = FakeERPClient(bank_statement_doctype=False)
        real_create = client.create_doc

        def racing_create(doctype, doc):
            if doctype == 'DocType':
                client.bank_statement_doctype = True   # the winner's write
                raise _api_error(409, 'DuplicateEntryError')()
            return real_create(doctype, doc)

        with unittest.mock.patch.object(client, 'create_doc', racing_create):
            report = es.provision_report(client)
        self.assertTrue(report['ok'])
        self.assertEqual(report['state'], es.PROVISION_PRESENT)

    def test_disabled_switch_makes_the_overlay_unavailable(self):
        self.app.config['ERPNEXT_STATEMENTS_ENABLED'] = False
        self.assertFalse(es.is_enabled())
        self.assertFalse(es.available(self._client()))


def _api_error(status, body):
    """A callable raising an ERPNextAPIError, for use as a mock side_effect."""
    from app.erpnext_client import ERPNextAPIError

    def raiser(*args, **kwargs):
        raise ERPNextAPIError(f'-> {status}', status_code=status,
                              response_body=body)
    return raiser


# ── upload ──────────────────────────────────────────────────────────────────

class UploadTests(ERPStatementBase):
    def test_creates_the_record_and_attaches_the_pdf(self):
        self._account()
        st = self._statement()
        client = self._client()
        result = es.sync_statement(client, st)

        self.assertEqual(result['action'], 'created')
        record = client.created['Bank Statement'][result['docname']]
        self.assertEqual(record['bank_account'], 'BA-1')
        self.assertEqual(record['plaid_statement_id'], 'st-1')
        self.assertEqual(record['period_start'], '2026-07-01')
        self.assertEqual(record['period_end'], '2026-07-31')
        self.assertEqual(record['opening_balance'], 17600.0)
        self.assertEqual(record['closing_balance'], 17650.0)

        attachments = client.attachments_for(result['docname'])
        self.assertEqual(len(attachments), 1)
        self.assertTrue(attachments[0]['content'].startswith(b'%PDF'))
        self.assertEqual(attachments[0]['fieldname'], 'statement_pdf')
        # Private: these are the operator's own bank records.
        self.assertEqual(attachments[0]['is_private'], 1)
        # And the Attach field points at the uploaded file.
        self.assertTrue(record.get('statement_pdf'))

    def test_records_the_docname_locally(self):
        self._account()
        st = self._statement()
        client = self._client()
        result = es.sync_statement(client, st)
        self.assertEqual(st.erpnext_docname, result['docname'])
        self.assertIsInstance(st.erpnext_synced_at, datetime)

    def test_a_second_sync_uploads_nothing(self):
        self._account()
        st = self._statement()
        client = self._client()
        es.sync_statement(client, st)
        uploads = len(client.uploads)

        result = es.sync_statement(client, st)
        self.assertEqual(result['action'], 'skipped')
        self.assertEqual(len(client.created['Bank Statement']), 1)
        self.assertEqual(len(client.uploads), uploads)

    def test_adopts_an_existing_erpnext_record(self):
        """The restored-backup case: ERPNext holds the record but the local
        column is blank. Creating a second one would break the unique index and
        put two statements for one month in front of the CPA."""
        self._account()
        st = self._statement()
        client = self._client()
        es.sync_statement(client, st)
        docname = st.erpnext_docname

        # Simulate the restore: the local row forgets, ERPNext remembers.
        st.erpnext_docname = None
        st.erpnext_synced_at = None
        db.session.commit()

        result = es.sync_statement(client, st)
        self.assertEqual(result['action'], 'adopted')
        self.assertEqual(result['docname'], docname)
        self.assertEqual(st.erpnext_docname, docname)
        self.assertEqual(len(client.created['Bank Statement']), 1)

    def test_skips_a_statement_whose_account_is_not_in_erpnext(self):
        """`bank_account` is a required Link, so there is nothing valid to
        create. It is a SKIP with a reason, not a failure — importing the
        account fixes it with no action here."""
        account = self._account()
        st = self._statement()
        # Clear the ERPNext Bank Account link the fixture sets.
        account.erpnext_bank_account_name = None
        db.session.commit()

        client = self._client()
        result = es.sync_statement(client, st)
        self.assertEqual(result['action'], 'no_account')
        self.assertIn('Bank Account', result['reason'])
        self.assertEqual(client.created['Bank Statement'], {})
        self.assertIsNone(st.erpnext_docname)

    def test_skips_a_statement_with_no_period(self):
        self._account()
        st = self._statement()
        st.period_start = None
        st.period_end = None
        db.session.commit()
        client = self._client()
        result = es.sync_statement(client, st)
        self.assertEqual(result['action'], 'no_period')
        self.assertEqual(client.created['Bank Statement'], {})

    def test_a_failed_attachment_still_leaves_a_usable_record(self):
        """The fields are what the reports run on. Losing the PDF must not lose
        the record — and `statement_pdf` stays blank, so the next tick retries."""
        self._account()
        st = self._statement()
        client = self._client(fail_upload=True)
        result = es.sync_statement(client, st)
        self.assertEqual(result['action'], 'created')
        record = client.created['Bank Statement'][result['docname']]
        self.assertFalse(record.get('statement_pdf'))
        self.assertEqual(st.erpnext_docname, result['docname'])

    def test_a_statement_with_no_pdf_on_disk_still_syncs(self):
        self._account()
        st = self._statement(pdf=False)
        client = self._client()
        result = es.sync_statement(client, st)
        self.assertEqual(result['action'], 'created')
        self.assertEqual(client.uploads, [])

    def test_losing_the_create_race_adopts_the_winners_record(self):
        self._account()
        st = self._statement()
        client = self._client(bank_statement_create_race=True)
        result = es.sync_statement(client, st)
        self.assertEqual(result['action'], 'adopted')
        self.assertEqual(len(client.created['Bank Statement']), 1)
        self.assertEqual(st.erpnext_docname, result['docname'])

    def test_a_refused_create_is_reported_not_raised(self):
        self._account()
        st = self._statement()
        client = self._client(fail_bank_statement_create=True)
        result = es.sync_statement(client, st)
        self.assertEqual(result['action'], 'failed')
        self.assertTrue(result['reason'])
        self.assertIsNone(st.erpnext_docname)

    def test_dry_run_writes_nothing(self):
        self._account()
        st = self._statement()
        client = self._client()
        result = es.sync_statement(client, st, dry_run=True)
        self.assertEqual(result['action'], 'created')
        self.assertEqual(client.created['Bank Statement'], {})
        self.assertIsNone(st.erpnext_docname)

    def test_balances_are_omitted_rather_than_zeroed_when_unparseable(self):
        """0.00 is a real balance. Asserting it for a statement we could not
        read would be a lie with no tell."""
        self._account()
        st = self._statement(opening=None, closing=None)
        client = self._client()
        result = es.sync_statement(client, st)
        record = client.created['Bank Statement'][result['docname']]
        self.assertNotIn('opening_balance', record)
        self.assertNotIn('closing_balance', record)


# ── sync_all ────────────────────────────────────────────────────────────────

class SyncAllTests(ERPStatementBase):
    def test_syncs_every_pending_statement(self):
        self._account()
        self._statement('st-1', month=5)
        self._statement('st-2', month=6)
        self._statement('st-3', month=7)
        client = self._client()
        stats = es.sync_all(client)
        self.assertEqual(stats['created'], 3)
        self.assertEqual(stats['failed'], 0)
        self.assertEqual(len(client.created['Bank Statement']), 3)

    def test_is_idempotent(self):
        self._account()
        self._statement('st-1', month=6)
        self._statement('st-2', month=7)
        client = self._client()
        es.sync_all(client)

        stats = es.sync_all(client)
        self.assertEqual(stats['created'], 0)
        self.assertEqual(stats['scanned'], 0)   # nothing is pending any more
        self.assertEqual(len(client.created['Bank Statement']), 2)

    def test_one_bad_statement_does_not_stop_the_others(self):
        self._account()
        self._statement('st-1', month=6)
        bad = self._statement('st-2', month=7)
        bad.period_start = None
        bad.period_end = None
        db.session.commit()
        self._statement('st-3', month=8)

        client = self._client()
        stats = es.sync_all(client)
        self.assertEqual(stats['created'], 2)
        self.assertEqual(stats['no_period'], 1)

    def test_reports_unavailability_without_touching_local_state(self):
        self._account()
        st = self._statement()
        client = FakeERPClient(foreign_bank_statement_doctype=True)
        es.provision_report(client)      # marks it unavailable

        stats = es.sync_all(client)
        self.assertTrue(stats['errors'])
        self.assertEqual(stats['created'], 0)
        self.assertIsNone(st.erpnext_docname)
        # v0.4.9's own state is untouched.
        self.assertTrue(stmts.pdf_exists(st))

    def test_disabled_switch_is_a_no_op(self):
        self.app.config['ERPNEXT_STATEMENTS_ENABLED'] = False
        self._account()
        st = self._statement()
        client = self._client()
        stats = es.sync_all(client)
        self.assertEqual(stats['created'], 0)
        self.assertIn('ERPNEXT_STATEMENTS_ENABLED', stats['errors'][0])
        self.assertEqual(client.created['Bank Statement'], {})
        self.assertIsNone(st.erpnext_docname)


# ── reconciliation ──────────────────────────────────────────────────────────

class ReconciliationSyncTests(ERPStatementBase):
    def test_maps_a_reconciling_period_to_reconciled(self):
        self._account()
        st = self._statement()
        # opening 17,600 + 50 in = closing 17,650 (Plaid signs inflow negative).
        self._txn('t-1', amount=-50.0, when=date(2026, 7, 10))
        fields = es.verdict_fields(st)
        self.assertEqual(fields['reconciliation_status'], es.STATUS_RECONCILED)
        self.assertEqual(fields['variance_amount'], 0.0)

    def test_maps_a_gap_to_discrepancy_and_keeps_the_sign(self):
        """The sign says whether the mirror is over or short. Storing |delta|
        would throw that away for one report's convenience."""
        self._account()
        st = self._statement()          # no transactions → 50 short
        fields = es.verdict_fields(st)
        self.assertEqual(fields['reconciliation_status'], es.STATUS_DISCREPANCY)
        self.assertEqual(fields['variance_amount'], -50.0)

    def test_an_unparseable_statement_is_not_checked_not_a_discrepancy(self):
        self._account()
        st = self._statement(opening=None, closing=None)
        fields = es.verdict_fields(st)
        self.assertEqual(fields['reconciliation_status'], es.STATUS_NOT_CHECKED)

    def test_the_verdict_reaches_erpnext_on_create(self):
        self._account()
        st = self._statement()
        client = self._client()
        result = es.sync_statement(client, st)
        record = client.created['Bank Statement'][result['docname']]
        self.assertEqual(record['reconciliation_status'],
                         es.STATUS_DISCREPANCY)
        self.assertEqual(record['variance_amount'], -50.0)

    def test_a_later_backfill_updates_the_verdict_in_erpnext(self):
        """A statement synced in March reconciles against the mirror as it was
        THEN. Closing the gap later has to move ERPNext's status, or the CPA is
        reading a stale verdict."""
        self._account()
        st = self._statement()
        client = self._client()
        es.sync_statement(client, st)
        docname = st.erpnext_docname
        self.assertEqual(client.created['Bank Statement'][docname]
                         ['reconciliation_status'], es.STATUS_DISCREPANCY)

        self._txn('t-late', amount=-50.0, when=date(2026, 7, 20))
        self.assertEqual(es.push_reconciliation(client, st), 'updated')
        record = client.created['Bank Statement'][docname]
        self.assertEqual(record['reconciliation_status'], es.STATUS_RECONCILED)
        self.assertEqual(record['variance_amount'], 0.0)

    def test_an_unchanged_verdict_costs_no_write(self):
        self._account()
        st = self._statement()
        client = self._client()
        es.sync_statement(client, st)
        writes = len([c for c in client.calls if c[0] == 'update_doc'])

        self.assertEqual(es.push_reconciliation(client, st), 'unchanged')
        self.assertEqual(
            len([c for c in client.calls if c[0] == 'update_doc']), writes)

    def test_sync_all_refreshes_verdicts_on_existing_records(self):
        self._account()
        st = self._statement()
        client = self._client()
        es.sync_all(client)
        self._txn('t-late', amount=-50.0, when=date(2026, 7, 20))

        stats = es.sync_all(client)
        self.assertEqual(stats['reconciled'], 1)
        self.assertEqual(client.created['Bank Statement'][st.erpnext_docname]
                         ['reconciliation_status'], es.STATUS_RECONCILED)

    def test_a_failed_update_is_reported_not_raised(self):
        self._account()
        st = self._statement()
        client = self._client()
        es.sync_statement(client, st)
        self._txn('t-late', amount=-50.0, when=date(2026, 7, 20))
        with unittest.mock.patch.object(
                client, 'update_doc', side_effect=_api_error(500, 'boom')):
            self.assertEqual(es.push_reconciliation(client, st), 'failed')


# ── reports ─────────────────────────────────────────────────────────────────

class DiscrepancyReportTests(ERPStatementBase):
    def test_lists_statements_over_the_threshold(self):
        self._account()
        self._statement('st-1', month=7)             # 50 short
        client = self._client()
        es.sync_all(client)

        report = es.discrepancy_report(client, threshold=10.0)
        self.assertEqual(report['source'], 'erpnext')
        self.assertEqual(len(report['rows']), 1)
        self.assertEqual(report['rows'][0]['variance'], -50.0)
        self.assertEqual(report['rows'][0]['bank_account'], 'BA-1')

    def test_compares_the_absolute_variance(self):
        """A mirror that is $50 OVER is exactly as wrong as one $50 short. A
        signed filter would show only half the problems."""
        self._account()
        st = self._statement('st-1', month=7)
        self._txn('t-1', amount=-150.0, when=date(2026, 7, 10))  # 100 over
        client = self._client()
        es.sync_all(client)
        self.assertEqual(es.verdict_fields(st)['variance_amount'], 100.0)

        report = es.discrepancy_report(client, threshold=10.0)
        self.assertEqual(len(report['rows']), 1)
        self.assertEqual(report['rows'][0]['variance'], 100.0)

    def test_respects_the_threshold(self):
        self._account()
        self._statement('st-1', month=7)             # 50 short
        client = self._client()
        es.sync_all(client)
        self.assertEqual(es.discrepancy_report(client, threshold=100.0)['rows'],
                         [])

    def test_uses_the_configured_threshold_by_default(self):
        self.app.config['ERPNEXT_STATEMENT_VARIANCE_THRESHOLD'] = 200.0
        self._account()
        self._statement('st-1', month=7)
        client = self._client()
        es.sync_all(client)
        report = es.discrepancy_report(client)
        self.assertEqual(report['threshold'], 200.0)
        self.assertEqual(report['rows'], [])

    def test_ignores_statements_that_could_not_be_reconciled(self):
        self._account()
        self._statement('st-1', month=7, opening=None, closing=None)
        client = self._client()
        es.sync_all(client)
        self.assertEqual(es.discrepancy_report(client, threshold=10.0)['rows'],
                         [])

    def test_worst_first(self):
        self._account('acct-1')
        self._statement('st-1', month=6)
        self._statement('st-2', month=7, closing=18600.0)   # 1,000 short
        client = self._client()
        es.sync_all(client)
        rows = es.discrepancy_report(client, threshold=10.0)['rows']
        self.assertEqual([abs(r['variance']) for r in rows], [1000.0, 50.0])

    def test_falls_back_to_local_data_when_erpnext_is_down(self):
        self._account()
        self._statement('st-1', month=7)
        client = self._client(fail_list={'Bank Statement': (500, 'boom')})
        report = es.discrepancy_report(client, threshold=10.0)
        self.assertEqual(report['source'], 'local')
        self.assertEqual(len(report['rows']), 1)
        self.assertEqual(report['rows'][0]['variance'], -50.0)

    def test_falls_back_when_erpnext_auth_expired(self):
        self._account()
        self._statement('st-1', month=7)
        client = self._client(
            fail_list={'Bank Statement': (401, 'InvalidAuthorizationToken')})
        report = es.discrepancy_report(client, threshold=10.0)
        self.assertEqual(report['source'], 'local')
        self.assertEqual(len(report['rows']), 1)


class CoverageReportTests(ERPStatementBase):
    AS_OF = date(2026, 7, 15)

    def test_month_keys_end_with_the_last_closed_month(self):
        """The month in progress has no statement yet; counting it as a gap
        would put a permanent false positive at the top of the report."""
        keys = es.month_keys(3, as_of=self.AS_OF)
        self.assertEqual(keys, ['2026-04', '2026-05', '2026-06'])

    def test_identifies_a_missing_month(self):
        self._account()
        self._statement('st-4', month=4)
        self._statement('st-5', month=5)
        # June missing.
        client = self._client()
        es.sync_all(client)

        report = es.coverage_report(client, months=6, as_of=self.AS_OF)
        self.assertEqual(report['source'], 'erpnext')
        row = report['rows'][0]
        self.assertEqual(row['missing'], ['2026-06'])
        self.assertEqual(row['gap_count'], 1)

    def test_reports_complete_coverage_as_no_gaps(self):
        self._account()
        for month in (4, 5, 6):
            self._statement(f'st-{month}', month=month)
        client = self._client()
        es.sync_all(client)
        row = es.coverage_report(client, months=3, as_of=self.AS_OF)['rows'][0]
        self.assertEqual(row['missing'], [])
        self.assertEqual(row['months_held'], 3)

    def test_does_not_blame_an_account_for_months_before_it_existed(self):
        """An account linked in May cannot be missing January."""
        self._account()
        self._statement('st-5', month=5)
        self._statement('st-6', month=6)
        client = self._client()
        es.sync_all(client)
        row = es.coverage_report(client, months=12, as_of=self.AS_OF)['rows'][0]
        self.assertEqual(row['missing'], [])

    def test_flags_an_account_that_stopped_reporting(self):
        """The trailing gap — the one that actually happens when a bank drops a
        product or an Item quietly de-authorizes."""
        self._account()
        self._statement('st-1', month=1)
        self._statement('st-2', month=2)
        client = self._client()
        es.sync_all(client)
        row = es.coverage_report(client, months=6, as_of=self.AS_OF)['rows'][0]
        self.assertEqual(row['missing'], ['2026-03', '2026-04', '2026-05',
                                          '2026-06'])

    def test_falls_back_to_local_data_when_erpnext_is_down(self):
        self._account()
        self._statement('st-4', month=4)
        self._statement('st-6', month=6)
        client = self._client(fail_list={'Bank Statement': (500, 'boom')})
        report = es.coverage_report(client, months=6, as_of=self.AS_OF)
        self.assertEqual(report['source'], 'local')
        self.assertEqual(report['rows'][0]['missing'], ['2026-05'])

    def test_separates_accounts(self):
        self._account('acct-1')
        self._account('acct-2')
        self._statement('st-1a', account_id='acct-1', month=5)
        self._statement('st-1b', account_id='acct-1', month=6)
        self._statement('st-2a', account_id='acct-2', month=5)
        client = self._client(fail_list={'Bank Statement': (500, 'down')})
        report = es.coverage_report(client, months=6, as_of=self.AS_OF)
        by_account = {r['account']: r for r in report['rows']}
        self.assertEqual(by_account['acct-1']['missing'], [])
        self.assertEqual(by_account['acct-2']['missing'], ['2026-06'])


# ── scheduler wiring — the v0.4.6 lesson ────────────────────────────────────

class SchedulerWiringTests(ERPStatementBase):
    def test_the_boot_job_provisions_and_syncs(self):
        """This is the test v0.4.5 did not have. The doctype spec was correct
        then too; what was missing was anything CALLING it at startup."""
        self._account()
        st = self._statement()
        client = self._client(bank_statement_doctype=False)
        with unittest.mock.patch('app.sync_engine.get_erp_client_or_none',
                                 return_value=client):
            scheduler._run_bank_statement_provision(self.app)
        self.assertIn('Bank Statement', client.created['DocType'])
        self.assertEqual(len(client.created['Bank Statement']), 1)
        db.session.refresh(st)
        self.assertTrue(st.erpnext_docname)

    def test_the_boot_job_survives_an_unreachable_erpnext(self):
        """A container must boot when ERPNext is down."""
        with unittest.mock.patch('app.sync_engine.get_erp_client_or_none',
                                 return_value=None):
            scheduler._run_bank_statement_provision(self.app)   # must not raise

    def test_the_boot_job_survives_a_refusing_erpnext(self):
        self._account()
        st = self._statement()
        client = FakeERPClient(doctype_permission_error=True)
        with unittest.mock.patch('app.sync_engine.get_erp_client_or_none',
                                 return_value=client):
            scheduler._run_bank_statement_provision(self.app)
        db.session.refresh(st)
        self.assertIsNone(st.erpnext_docname)
        self.assertTrue(stmts.pdf_exists(st))   # local state intact

    def test_the_boot_job_honours_the_auto_sync_switch(self):
        self.app.config['ERPNEXT_STATEMENTS_AUTO_SYNC'] = False
        self._account()
        st = self._statement()
        client = self._client(bank_statement_doctype=False)
        with unittest.mock.patch('app.sync_engine.get_erp_client_or_none',
                                 return_value=client):
            scheduler._run_bank_statement_provision(self.app)
        self.assertIn('Bank Statement', client.created['DocType'])
        self.assertEqual(client.created['Bank Statement'], {})
        db.session.refresh(st)
        self.assertIsNone(st.erpnext_docname)

    def test_the_statement_pull_pushes_to_erpnext_afterwards(self):
        self._account()
        st = self._statement()
        client = self._client()
        with unittest.mock.patch.object(stmts, 'fetch_all',
                                        return_value=stmts._blank_stats()), \
             unittest.mock.patch('app.sync_engine.get_erp_client_or_none',
                                 return_value=client), \
             unittest.mock.patch('app.plaid_settings.is_configured',
                                 return_value=True):
            scheduler._run_statements_pull(self.app)
        db.session.refresh(st)
        self.assertTrue(st.erpnext_docname)


# ── admin pages ─────────────────────────────────────────────────────────────
#
# These render the report templates for real. Worth their own class: the first
# version of this release shipped a discrepancies page that 500'd on every
# request — `render_template_string`'s own first parameter is named `source`,
# so passing a context key of that name raised TypeError — and every one of the
# module-level tests above still passed, because none of them rendered a page.

class AdminPageTests(ERPStatementBase):
    def setUp(self):
        super().setUp()
        self.client_ = self.app.test_client()

    def _erp(self, **kwargs):
        client = self._client(**kwargs)
        patcher = unittest.mock.patch('app.sync_engine.get_erp_client_or_none',
                                      return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def test_the_discrepancies_page_renders(self):
        self._account()
        self._statement('st-1', month=7)
        erp = self._erp()
        es.sync_all(erp)
        resp = self.client_.get('/admin/statements/reports/discrepancies')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertIn('-50.00', body)
        self.assertIn('BA-1', body)

    def test_the_discrepancies_page_renders_when_empty(self):
        self._erp()
        resp = self.client_.get('/admin/statements/reports/discrepancies')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('No discrepancies', resp.data.decode())

    def test_the_coverage_page_renders_and_names_the_gap(self):
        self._account()
        self._statement('st-4', month=4)
        self._statement('st-5', month=5)
        self._statement('st-7', month=7)     # June missing
        erp = self._erp()
        es.sync_all(erp)
        resp = self.client_.get('/admin/statements/reports/coverage')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('2026-06', resp.data.decode())

    def test_the_coverage_page_renders_when_empty(self):
        self._erp()
        resp = self.client_.get('/admin/statements/reports/coverage')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('No statements yet', resp.data.decode())

    def test_the_pages_say_so_when_falling_back_to_local_data(self):
        self._account()
        self._statement('st-1', month=7)
        self._erp(fail_list={'Bank Statement': (500, 'boom')})
        for url in ('/admin/statements/reports/discrepancies',
                    '/admin/statements/reports/coverage'):
            resp = self.client_.get(url)
            self.assertEqual(resp.status_code, 200, url)
            self.assertIn('could not be reached', resp.data.decode(), url)

    def test_the_statements_page_offers_the_reports(self):
        self._account()
        self._statement('st-1', month=7)
        self._erp()
        body = self.client_.get('/admin/statements').data.decode()
        self.assertIn('/admin/statements/reports/discrepancies', body)
        self.assertIn('/admin/statements/reports/coverage', body)
        self.assertIn('/admin/statements/sync_erpnext', body)

    def test_the_sync_button_uploads_and_reports(self):
        self._account()
        st = self._statement('st-1', month=7)
        erp = self._erp()
        resp = self.client_.post('/admin/statements/sync_erpnext')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('Created+1', resp.headers['Location'])
        db.session.refresh(st)
        self.assertTrue(st.erpnext_docname)
        self.assertEqual(len(erp.created['Bank Statement']), 1)

    def test_the_sync_button_counts_already_synced_statements(self):
        """`skipped` is structurally 0 in a bulk run (pending_statements
        excludes synced rows), so the flash reports `already_synced` — the
        number an operator pressing the button twice expects to see."""
        self._account()
        self._statement('st-1', month=7)
        self._erp()
        self.client_.post('/admin/statements/sync_erpnext')
        resp = self.client_.post('/admin/statements/sync_erpnext')
        self.assertIn('already+synced+1', resp.headers['Location'])

    def test_the_sync_button_reports_a_disabled_overlay(self):
        self.app.config['ERPNEXT_STATEMENTS_ENABLED'] = False
        self._erp()
        resp = self.client_.post('/admin/statements/sync_erpnext')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('ERPNEXT_STATEMENTS_ENABLED', resp.headers['Location'])


# ── the backfill script ─────────────────────────────────────────────────────

class BackfillScriptTests(ERPStatementBase):
    def _run(self, client, **kwargs):
        from scripts import backfill_erpnext_statements as script
        return script.run(client, verbose=False, **kwargs)

    def test_creates_records_for_every_local_statement(self):
        self._account()
        self._statement('st-1', month=6)
        self._statement('st-2', month=7)
        client = self._client(bank_statement_doctype=False)
        stats = self._run(client)
        self.assertTrue(stats['provision']['ok'])
        self.assertEqual(stats['created'], 2)
        self.assertEqual(len(client.created['Bank Statement']), 2)
        self.assertEqual(len(client.uploads), 2)

    def test_is_idempotent(self):
        self._account()
        self._statement('st-1', month=6)
        self._statement('st-2', month=7)
        client = self._client(bank_statement_doctype=False)
        self._run(client)

        stats = self._run(client)
        self.assertEqual(stats['created'], 0)
        self.assertEqual(len(client.created['Bank Statement']), 2)
        self.assertEqual(len(client.uploads), 2)

    def test_dry_run_writes_nothing(self):
        self._account()
        self._statement('st-1', month=7)
        client = self._client()
        stats = self._run(client, dry_run=True)
        self.assertEqual(stats['created'], 1)
        self.assertEqual(client.created['Bank Statement'], {})
        self.assertEqual(client.uploads, [])

    def test_reports_an_unavailable_doctype(self):
        self._account()
        self._statement('st-1', month=7)
        client = FakeERPClient(doctype_permission_error=True)
        stats = self._run(client)
        self.assertFalse(stats['provision']['ok'])
        self.assertEqual(stats['created'], 0)

    def test_adopts_records_a_previous_run_created(self):
        self._account()
        st = self._statement('st-1', month=7)
        client = self._client()
        es.sync_statement(client, st)
        st.erpnext_docname = None       # the restored-backup case
        db.session.commit()

        stats = self._run(client)
        self.assertEqual(stats['adopted'], 1)
        self.assertEqual(stats['created'], 0)
        self.assertEqual(len(client.created['Bank Statement']), 1)


class ProvisionScriptTests(ERPStatementBase):
    def test_creates_the_doctype(self):
        from scripts import provision_bank_statement_doctype as script
        client = FakeERPClient(bank_statement_doctype=False)
        report = script.run(client)
        self.assertTrue(report['ok'])
        self.assertEqual(report['state'], es.PROVISION_CREATED)

    def test_reports_a_refusal(self):
        from scripts import provision_bank_statement_doctype as script
        client = FakeERPClient(doctype_permission_error=True)
        report = script.run(client)
        self.assertFalse(report['ok'])
        self.assertEqual(report['state'], es.PROVISION_PERMISSION)


# ── regressions ─────────────────────────────────────────────────────────────

class RegressionTests(ERPStatementBase):
    def test_v049_local_storage_is_unaffected(self):
        """The ERPNext overlay must not change where or whether the PDF lands
        on disk — every v0.4.9 consumer reads that path."""
        self._account()
        st = self._statement()
        client = self._client()
        path_before = st.pdf_path
        es.sync_statement(client, st)
        db.session.refresh(st)
        self.assertEqual(st.pdf_path, path_before)
        self.assertTrue(stmts.pdf_exists(st))
        self.assertEqual(st.opening_balance, 17600.0)

    def test_v049_reconciliation_is_unchanged(self):
        self._account()
        st = self._statement()
        self._txn('t-1', amount=-50.0, when=date(2026, 7, 10))
        verdict = stmts.reconcile_statement(st)
        self.assertEqual(verdict['status'], 'ok')
        self.assertEqual(verdict['delta'], 0.0)

    def test_v045_counterparty_provisioning_still_works(self):
        """Two doctypes now provision at boot. Neither may break the other —
        they share the unavailable-doctype registry."""
        client = FakeERPClient(counterparty_doctype=False,
                               bank_statement_doctype=False)
        self.assertTrue(counterparty.provision_report(client)['ok'])
        self.assertTrue(es.provision_report(client)['ok'])
        self.assertIn('Counterparty', client.created['DocType'])
        self.assertIn('Bank Statement', client.created['DocType'])
        self.assertTrue(counterparty.available(client))
        self.assertTrue(es.available(client))

    def test_an_unavailable_statement_doctype_does_not_disable_counterparty(self):
        client = FakeERPClient(counterparty_doctype=True,
                               foreign_bank_statement_doctype=True)
        es.provision_report(client)
        self.assertFalse(es.available(client))
        self.assertTrue(counterparty.provision_report(client)['ok'])
        self.assertTrue(counterparty.available(client))

    def test_v044_statement_anchored_opening_balance_still_works(self):
        """choose_anchor_statement is load-bearing for opening balances. The
        ERPNext columns must not disturb it."""
        account = self._account()
        st = self._statement()
        self._txn('t-1', amount=-50.0, when=date(2026, 7, 10))
        client = self._client()
        es.sync_statement(client, st)
        db.session.refresh(st)

        anchor = stmts.anchor_for(account)
        self.assertIsNotNone(anchor)
        amount, when, chosen = anchor
        self.assertEqual(amount, 17600.0)
        self.assertEqual(when, date(2026, 7, 1))
        self.assertEqual(chosen.statement_id, 'st-1')

    def test_the_new_columns_default_to_unsynced(self):
        """An upgrading install's existing rows must read as 'not in ERPNext
        yet' so the boot job picks all of them up."""
        self._account()
        st = self._statement()
        self.assertIsNone(st.erpnext_docname)
        self.assertIsNone(st.erpnext_synced_at)
        self.assertIn(st, es.pending_statements())
        self.assertEqual(st.to_dict()['erpnext_docname'], '')

    def test_the_migration_declares_both_columns(self):
        from app.migrations import SCHEMA_MIGRATIONS
        added = {(t, c) for t, c, _ in SCHEMA_MIGRATIONS}
        self.assertIn(('plaid_statements', 'erpnext_docname'), added)
        self.assertIn(('plaid_statements', 'erpnext_synced_at'), added)


if __name__ == '__main__':
    unittest.main()
