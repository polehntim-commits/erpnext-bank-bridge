# SPDX-License-Identifier: MIT
"""Brokerage statement PDFs, attached to their ERPNext Bank Account (v1.0.3).

What matters here:

  * the file name is the idempotency key — a statement already attached is
    skipped, and a second run uploads nothing
  * nothing is uploaded when the existing-attachment list cannot be read
  * a dry run reports `would_push` and uploads nothing
  * an upload counts as pushed only on a File record ERPNext returned
  * only live, mapped INVESTMENT accounts are in scope
  * the MCP tool narrows by mask and period, and is kill-switch gated
  * the sync's ERPNext leg runs it, fail-soft

Synthetic masks (4242 / 4200) only.

    cd app
    python3 -m unittest tests.test_statement_pdf_push -v
"""
import os
import unittest.mock
from datetime import date

from app import db, erpnext_push, mcp_settings, sync_engine
from app.blueprints import mcp_server
from app.models import PlaidAccount, PlaidStatement

from tests.test_erpnext_push import PushBase

BA = 'WF Brokerage - EC'


class StatementPdfBase(PushBase):
    def _statement(self, acct, period=(date(2026, 6, 1), date(2026, 6, 30)),
                   *, pdf=True):
        st = PlaidStatement(statement_id=f'st-{acct.account_id}-{period[0]}',
                            plaid_item_id=self.item.item_id,
                            plaid_account_id=acct.account_id,
                            period_start=period[0], period_end=period[1])
        if pdf:
            path = os.path.join(self._store, f'{acct.account_id}-{period[0]}.pdf')
            with open(path, 'wb') as fh:
                fh.write(b'%PDF-1.4 synthetic ' + str(period[0]).encode())
            st.pdf_path = path
        db.session.add(st)
        db.session.commit()
        return st


class FilenameTest(StatementPdfBase):
    def test_the_name_is_institution_mask_and_period_end(self):
        acct = self._account('4242', bank_account=BA)
        st = self._statement(acct)
        self.assertEqual(erpnext_push.statement_pdf_filename(acct, st),
                         'WF-Brokerage-4242-2026-06-30-Statement.pdf')


class PushTest(StatementPdfBase):
    def test_every_missing_statement_is_attached_privately(self):
        acct = self._account('4242', bank_account=BA)
        self._statement(acct, (date(2026, 5, 1), date(2026, 5, 31)))
        self._statement(acct, (date(2026, 6, 1), date(2026, 6, 30)))
        out = erpnext_push.push_statement_pdfs(client=self.erp)
        self.assertEqual(len(out['pushed']), 2)
        self.assertEqual(out['failed'], [])
        ups = self.erp.attachments_for(BA)
        self.assertEqual([u['filename'] for u in ups],
                         ['WF-Brokerage-4242-2026-05-31-Statement.pdf',
                          'WF-Brokerage-4242-2026-06-30-Statement.pdf'])
        self.assertTrue(all(u['doctype'] == 'Bank Account' for u in ups))
        self.assertTrue(all(u['is_private'] == 1 for u in ups))

    def test_a_second_run_uploads_nothing(self):
        acct = self._account('4242', bank_account=BA)
        self._statement(acct)
        erpnext_push.push_statement_pdfs(client=self.erp)
        out = erpnext_push.push_statement_pdfs(client=self.erp)
        self.assertEqual(out['pushed'], [])
        self.assertEqual([r['reason'] for r in out['skipped']],
                         ['already_attached'])
        self.assertEqual(len(self.erp.uploads), 1)

    def test_nothing_is_uploaded_when_the_attachment_list_is_unreadable(self):
        from app.erpnext_client import ERPNextAPIError
        acct = self._account('4242', bank_account=BA)
        self._statement(acct)
        with unittest.mock.patch.object(
                self.erp, 'list_docs',
                side_effect=ERPNextAPIError('boom', status_code=500)):
            out = erpnext_push.push_statement_pdfs(client=self.erp)
        self.assertIn('nothing was uploaded', out['error'])
        self.assertEqual(self.erp.uploads, [])

    def test_a_dry_run_reports_and_uploads_nothing(self):
        acct = self._account('4242', bank_account=BA)
        self._statement(acct)
        out = erpnext_push.push_statement_pdfs(client=self.erp, dry_run=True)
        self.assertEqual(out['pushed'], [])
        self.assertEqual([r['file_name'] for r in out['would_push']],
                         ['WF-Brokerage-4242-2026-06-30-Statement.pdf'])
        self.assertEqual(self.erp.uploads, [])

    def test_a_statement_without_a_pdf_is_skipped_with_a_reason(self):
        acct = self._account('4242', bank_account=BA)
        self._statement(acct, pdf=False)
        out = erpnext_push.push_statement_pdfs(client=self.erp)
        self.assertEqual([r['reason'] for r in out['skipped']], ['no_pdf'])

    def test_a_refused_upload_is_failed_not_pushed(self):
        acct = self._account('4242', bank_account=BA)
        self._statement(acct)
        self.erp.fail_upload = True
        out = erpnext_push.push_statement_pdfs(client=self.erp)
        self.assertEqual(out['pushed'], [])
        self.assertEqual(len(out['failed']), 1)

    def test_an_upload_with_no_file_record_is_not_counted(self):
        acct = self._account('4242', bank_account=BA)
        self._statement(acct)
        with unittest.mock.patch.object(self.erp, 'upload_file',
                                        return_value={}):
            out = erpnext_push.push_statement_pdfs(client=self.erp)
        self.assertEqual(out['pushed'], [])
        self.assertIn('not confirmed', out['failed'][0]['error'])

    def test_an_unreachable_erpnext_stops_the_batch(self):
        from app.erpnext_client import ERPNextAPIError
        acct = self._account('4242', bank_account=BA)
        self._statement(acct, (date(2026, 5, 1), date(2026, 5, 31)))
        self._statement(acct, (date(2026, 6, 1), date(2026, 6, 30)))
        calls = []

        def down(*a, **k):
            calls.append(a)
            raise ERPNextAPIError('connection refused', status_code=None)
        with unittest.mock.patch.object(self.erp, 'upload_file',
                                        side_effect=down):
            out = erpnext_push.push_statement_pdfs(client=self.erp)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(out['failed']), 2)
        self.assertIn('not attempted', out['failed'][1]['error'])

    def test_depository_and_retired_accounts_are_out_of_scope(self):
        brk = self._account('4242', bank_account=BA)
        self._statement(brk)
        chk = PlaidAccount(account_id='acc-chk', item_id=self.item.item_id,
                           mask='1111', type='depository', subtype='checking',
                           erpnext_bank_account_name='Checking - EC')
        db.session.add(chk)
        db.session.commit()
        self._statement(chk)
        out = erpnext_push.push_statement_pdfs(client=self.erp)
        self.assertEqual([r['account_mask'] for r in out['pushed']], ['4242'])
        self.assertEqual(self.erp.attachments_for('Checking - EC'), [])

    def test_an_unmapped_brokerage_is_reported(self):
        acct = self._account('4200', bank_account=None)
        self._statement(acct)
        out = erpnext_push.push_statement_pdfs(client=self.erp)
        self.assertEqual([r['reason'] for r in out['skipped']], ['unmapped'])
        self.assertEqual(self.erp.uploads, [])

    def test_pre_relink_statements_reach_the_live_bank_account(self):
        old = self._account('4242', bank_account=None, account_id='acc-old')
        new = self._account('4242', bank_account=BA, account_id='acc-new')
        old.superseded_by_account_id = new.account_id
        db.session.commit()
        self._statement(old, (date(2026, 5, 1), date(2026, 5, 31)))
        self._statement(new, (date(2026, 6, 1), date(2026, 6, 30)))
        out = erpnext_push.push_statement_pdfs(client=self.erp)
        self.assertEqual(len(out['pushed']), 2)
        self.assertEqual(len(self.erp.attachments_for(BA)), 2)

    def test_period_narrows_by_month_or_exact_end(self):
        acct = self._account('4242', bank_account=BA)
        self._statement(acct, (date(2026, 5, 1), date(2026, 5, 31)))
        self._statement(acct, (date(2026, 6, 1), date(2026, 6, 30)))
        out = erpnext_push.push_statement_pdfs(client=self.erp,
                                               period='2026-06', dry_run=True)
        self.assertEqual([r['period_end'] for r in out['would_push']],
                         ['2026-06-30'])
        out = erpnext_push.push_statement_pdfs(client=self.erp,
                                               period='2026-05-31',
                                               dry_run=True)
        self.assertEqual([r['period_end'] for r in out['would_push']],
                         ['2026-05-31'])


class McpToolTest(StatementPdfBase):
    def setUp(self):
        super().setUp()
        p = unittest.mock.patch.object(mcp_server, '_erp_client_or_error',
                                       return_value=self.erp)
        p.start()
        self.addCleanup(p.stop)

    def test_it_is_registered_mutating_and_off_by_default(self):
        self.assertTrue(mcp_server.TOOLS['push_statement_pdfs']['mutating'])
        self.assertFalse(mcp_settings._DEFAULTS['push_statement_pdfs'])

    def test_one_account_one_period(self):
        a = self._account('4242', bank_account=BA)
        b = self._account('4200', bank_account='WF Brokerage 2 - EC')
        self._statement(a, (date(2026, 5, 1), date(2026, 5, 31)))
        self._statement(a, (date(2026, 6, 1), date(2026, 6, 30)))
        self._statement(b)
        result, summary = mcp_server._push_statement_pdfs(
            {'account_mask': '4242', 'period': '2026-06'})
        self.assertEqual([r['file_name'] for r in result['pushed']],
                         ['WF-Brokerage-4242-2026-06-30-Statement.pdf'])
        self.assertEqual(len(self.erp.uploads), 1)
        self.assertIn('pushed 1', summary)

    def test_dry_run_string_is_honoured(self):
        acct = self._account('4242', bank_account=BA)
        self._statement(acct)
        result, summary = mcp_server._push_statement_pdfs({'dry_run': 'true'})
        self.assertEqual(self.erp.uploads, [])
        self.assertIn('DRY RUN', summary)

    def test_a_malformed_period_is_a_tool_error(self):
        self._account('4242', bank_account=BA)
        with self.assertRaises(mcp_server.ToolError):
            mcp_server._push_statement_pdfs({'period': 'June'})

    def test_a_depository_mask_is_refused(self):
        db.session.add(PlaidAccount(account_id='acc-chk',
                                    item_id=self.item.item_id, mask='1111',
                                    type='depository',
                                    erpnext_bank_account_name='Checking - EC'))
        db.session.commit()
        with self.assertRaises(mcp_server.ToolError):
            mcp_server._push_statement_pdfs({'account_mask': '1111'})


class SyncLegTest(StatementPdfBase):
    def test_the_sync_push_leg_attaches_new_statements(self):
        acct = self._account('4242', bank_account=BA)
        self._statement(acct)
        out = sync_engine._push_to_erpnext(self.erp, [acct])
        self.assertEqual(out['statement_pdfs']['pushed'], 1)
        self.assertEqual(len(self.erp.attachments_for(BA)), 1)
        self.assertIn('queue', out)

    def test_a_failing_pdf_leg_does_not_stop_the_queue_drain(self):
        acct = self._account('4242', bank_account=BA)
        with unittest.mock.patch.object(erpnext_push, 'push_statement_pdfs',
                                        side_effect=RuntimeError('x')):
            out = sync_engine._push_to_erpnext(self.erp, [acct])
        self.assertIn('error', out['statement_pdfs'])
        self.assertIn('queue', out)
