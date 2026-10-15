# SPDX-License-Identifier: MIT
"""Plaid Statements, end to end (v1.0.10).

Covered:
  * the switch: OFF on a new install, migrated ON (once, persisted) for an
    install that already holds Plaid statements, an explicit choice sticks
  * Link: "Enable statements for this connection" = update mode with
    products=['statements']; refused without an item or while switched off;
    the completion route records consent and pulls
  * the admin button and the on/off control render, and gate on the switch
  * each statement PDF lands on its ERPNext Statement Anchor and fills
    `statement_pdf` — depository accounts too; idempotent; link-only when the
    file is already attached; `no_anchor` when ERPNext has none for the period
  * get_statement_coverage: supported / manual_upload_required /
    not_consented / off, plus missing months
  * pull_statements is kill-switch gated; get_erpnext_push_status reports PDFs
  * no access token and no PDF bytes ever reach the log

Synthetic masks and ids only.

    cd app
    python3 -m unittest tests.test_plaid_statements -v
"""
import json
import logging
import os
import unittest
import unittest.mock
from datetime import date, datetime, timezone

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import (db, erpnext_push, mcp_settings, plaid_settings,  # noqa: E402
                 statements as stmts)
from app.blueprints import mcp_server  # noqa: E402
from app.models import (PlaidAccount, PlaidItem, PlaidStatement,  # noqa: E402
                        PlaidSyncLog)

from tests.fakes import FakeERPClient, FakePlaidClient  # noqa: E402
from tests.test_statements import StatementsBase, checking_pdf  # noqa: E402

CHK = 'Checking 3158 - EC'
BRK = 'Brokerage 6030 - EC'


class AnchorERP(FakeERPClient):
    """FakeERPClient plus ERPNext's Statement Anchor doctype and the Files
    attached to its records."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.anchors = {}            # name -> dict
        self.files = []              # {name, file_name, file_url, doctype, docname}

    def add_anchor(self, bank_account, start, end, pdf=None):
        name = f'SA-{len(self.anchors) + 1:05d}'
        self.anchors[name] = {'name': name, 'bank_account': bank_account,
                              'period_start': start.isoformat(),
                              'period_end': end.isoformat(),
                              'statement_pdf': pdf}
        return name

    def list_docs(self, doctype, filters=None, fields=None,
                  limit_page_length=20, **kw):
        if doctype == 'Statement Anchor':
            want = next(f[2] for f in filters if f[0] == 'bank_account')
            return [dict(a) for a in self.anchors.values()
                    if a['bank_account'] in want]
        if doctype == 'File' and any(f[2] == 'Statement Anchor'
                                     for f in filters or []):
            names = next(f[2] for f in filters if f[0] == 'attached_to_name')
            return [{'name': f['name'], 'file_name': f['file_name'],
                     'file_url': f['file_url'],
                     'attached_to_name': f['docname']}
                    for f in self.files if f['doctype'] == 'Statement Anchor'
                    and f['docname'] in names]
        return super().list_docs(doctype, filters=filters, fields=fields,
                                 limit_page_length=limit_page_length, **kw)

    def upload_file(self, filename, content_bytes, doctype=None, docname=None,
                    is_private=1, fieldname=None):
        reply = super().upload_file(filename, content_bytes, doctype=doctype,
                                    docname=docname, is_private=is_private,
                                    fieldname=fieldname)
        self.files.append({'name': reply['name'], 'file_name': filename,
                           'file_url': reply['file_url'], 'doctype': doctype,
                           'docname': docname})
        return reply

    def update_doc(self, doctype, name, doc):
        if doctype == 'Statement Anchor':
            self.calls.append(('update_doc', doctype, name, doc))
            self.anchors[name].update(doc)
            return {'name': name, **self.anchors[name]}
        return super().update_doc(doctype, name, doc)

    def get_doc(self, doctype, name):
        if doctype == 'Statement Anchor':
            return dict(self.anchors.get(name) or {}) or None
        return super().get_doc(doctype, name)


class Base(StatementsBase):
    def acct(self, account_id, mask, type_, subtype, bank_account):
        a = PlaidAccount(account_id=account_id, item_id='item-abc', mask=mask,
                         name=f'{subtype} {mask}', type=type_, subtype=subtype,
                         erpnext_bank_account_name=bank_account)
        db.session.add(a)
        db.session.commit()
        return a

    def stmt(self, acct, month, *, sid=None, pdf=True, year=2026):
        start, end = stmts.period_bounds(month, year)
        st = PlaidStatement(statement_id=sid or f'{acct.mask}-{month}',
                            plaid_item_id='item-abc',
                            plaid_account_id=acct.account_id,
                            period_start=start, period_end=end)
        if pdf:
            st.pdf_path = stmts.pdf_path_for('item-abc', acct.account_id,
                                             stmts.period_label(start),
                                             st.statement_id)
            st.pdf_bytes = stmts.store_pdf(st.pdf_path, checking_pdf())
        db.session.add(st)
        db.session.commit()
        return st


class SwitchTests(Base):
    def test_a_new_install_is_off(self):
        self.app.config['STATEMENTS_ENABLED'] = False
        self.assertFalse(plaid_settings.statements_enabled())

    def test_an_install_already_holding_plaid_statements_stays_on(self):
        self.app.config['STATEMENTS_ENABLED'] = False
        a = self.acct('a1', '3158', 'depository', 'checking', CHK)
        self.stmt(a, 6)
        self.assertTrue(plaid_settings.statements_enabled())
        self.assertTrue(plaid_settings.load()['statements_enabled'])

    def test_an_explicit_choice_sticks(self):
        a = self.acct('a1', '3158', 'depository', 'checking', CHK)
        self.stmt(a, 6)
        plaid_settings.set_statements_enabled(False)
        self.assertFalse(plaid_settings.statements_enabled())
        self.assertFalse(stmts.is_enabled())


class LinkTests(Base):
    def setUp(self):
        super().setUp()
        plaid_settings.save('cid', 'sandbox', sandbox_secret='s')
        self.client = self.app.test_client()

    def test_enable_statements_mints_an_update_mode_token_with_the_product(self):
        fake = unittest.mock.Mock()
        fake.create_link_token.return_value = 'link-up'
        with unittest.mock.patch('app.sync_engine.get_plaid_client',
                                 return_value=fake):
            r = self.client.post('/bankbridge/api/plaid/create_link_token',
                                 json={'item_id': 'item-abc',
                                       'enable_statements': 1})
        self.assertEqual(200, r.status_code, r.get_json())
        kw = fake.create_link_token.call_args.kwargs
        self.assertEqual('access-x', kw['access_token'])
        self.assertTrue(kw['add_statements'])
        self.assertTrue(r.get_json()['enable_statements'])

    def test_it_needs_an_item_and_the_switch_on(self):
        r = self.client.post('/bankbridge/api/plaid/create_link_token',
                             json={'enable_statements': 1})
        self.assertEqual(400, r.status_code)
        plaid_settings.set_statements_enabled(False)
        r = self.client.post('/bankbridge/api/plaid/create_link_token',
                             json={'item_id': 'item-abc',
                                   'enable_statements': 1})
        self.assertEqual(409, r.status_code)

    def test_consent_completion_records_it_and_pulls(self):
        with unittest.mock.patch.object(stmts, 'run_refresh_pull',
                                        return_value={'stored': 0}) as pull:
            r = self.client.post('/bankbridge/api/plaid/statements_consented',
                                 json={'item_id': 'item-abc'})
        self.assertEqual(200, r.status_code)
        pull.assert_called_once()
        item = PlaidItem.query.filter_by(item_id='item-abc').one()
        self.assertIsNotNone(item.statements_consented_at)

    def test_the_accounts_page_offers_the_button_only_when_on(self):
        self.acct('a1', '3158', 'depository', 'checking', CHK)
        html = self.client.get('/admin/accounts').get_data(as_text=True)
        self.assertIn('Enable statements for this connection', html)
        self.assertIn('data-bb-statements="1"', html)
        plaid_settings.set_statements_enabled(False)
        html = self.client.get('/admin/accounts').get_data(as_text=True)
        self.assertNotIn('Enable statements for this connection', html)

    def test_the_statements_page_toggles_the_switch(self):
        r = self.client.post('/admin/statements/toggle', data={'enabled': '0'})
        self.assertEqual(302, r.status_code)
        self.assertFalse(plaid_settings.statements_enabled())
        html = self.client.get('/admin/statements').get_data(as_text=True)
        self.assertIn('Turn Plaid Statements on', html)


class AnchorPdfTests(Base):
    def setUp(self):
        super().setUp()
        self.erp = AnchorERP()
        self.chk = self.acct('a1', '3158', 'depository', 'checking', CHK)
        self.brk = self.acct('a2', '6030', 'investment', 'brokerage', BRK)
        self.jun_chk = self.stmt(self.chk, 6)
        self.jun_brk = self.stmt(self.brk, 6)
        self.sa_chk = self.erp.add_anchor(CHK, date(2026, 6, 1),
                                          date(2026, 6, 30))
        self.sa_brk = self.erp.add_anchor(BRK, date(2026, 6, 1),
                                          date(2026, 6, 30))

    def test_each_pdf_lands_on_its_anchor_and_fills_the_field(self):
        out = erpnext_push.push_statement_pdfs_to_anchors(client=self.erp)
        self.assertEqual([], out['failed'], out)
        self.assertEqual({self.sa_chk, self.sa_brk},
                         {r['anchor'] for r in out['pushed']})
        self.assertEqual('WF-Checking-3158-2026-06-30-Statement.pdf',
                         next(r['file_name'] for r in out['pushed']
                              if r['anchor'] == self.sa_chk))
        for sa in (self.sa_chk, self.sa_brk):
            self.assertTrue(self.erp.anchors[sa]['statement_pdf'])
        self.assertTrue(all(u['doctype'] == 'Statement Anchor'
                            and u['is_private'] == 1
                            for u in self.erp.uploads))

    def test_a_second_run_uploads_nothing(self):
        erpnext_push.push_statement_pdfs_to_anchors(client=self.erp)
        out = erpnext_push.push_statement_pdfs_to_anchors(client=self.erp)
        self.assertEqual([], out['pushed'])
        self.assertEqual(2, len(self.erp.uploads))
        self.assertEqual({'already_attached'},
                         {r['reason'] for r in out['skipped']})

    def test_an_attached_file_with_an_empty_field_is_only_linked(self):
        erpnext_push.push_statement_pdfs_to_anchors(client=self.erp)
        self.erp.anchors[self.sa_chk]['statement_pdf'] = None
        out = erpnext_push.push_statement_pdfs_to_anchors(client=self.erp)
        self.assertEqual([self.sa_chk], [r['anchor'] for r in out['linked']])
        self.assertEqual(2, len(self.erp.uploads))

    def test_a_period_erpnext_has_no_anchor_for_is_skipped(self):
        self.stmt(self.chk, 7)
        out = erpnext_push.push_statement_pdfs_to_anchors(client=self.erp)
        self.assertIn('no_anchor', [r['reason'] for r in out['skipped']])

    def test_a_dry_run_uploads_nothing(self):
        out = erpnext_push.push_statement_pdfs_to_anchors(client=self.erp,
                                                          dry_run=True)
        self.assertEqual(2, len(out['would_push']))
        self.assertEqual([], self.erp.uploads)

    def test_push_status_reports_anchors_missing_their_pdf(self):
        before = erpnext_push.statement_pdf_status(self.erp)
        self.assertEqual((2, 0), (before['anchors'], before['with_pdf']))
        erpnext_push.push_statement_pdfs_to_anchors(client=self.erp)
        after = erpnext_push.statement_pdf_status(self.erp)
        self.assertEqual((2, 2, []), (after['anchors'], after['with_pdf'],
                                      after['missing_pdf']))


class CoverageAndToolTests(Base):
    def tool(self, name, args=None):
        with self.app.test_request_context():
            res = mcp_server.dispatch_tool(name, args or {})
        text = res['content'][0]['text']
        return res, (text if res.get('isError') else json.loads(text))

    def log_pull(self, status, message=''):
        db.session.add(PlaidSyncLog(item_id='item-abc',
                                    direction='statements_pull', count=0,
                                    status=status, error_message=message))
        db.session.commit()

    def test_coverage_reports_status_and_missing_months(self):
        chk = self.acct('a1', '3158', 'depository', 'checking', CHK)
        self.acct('a2', '6030', 'investment', 'brokerage', BRK)
        self.stmt(chk, 6)
        self.stmt(chk, 8)
        self.log_pull('success', 'listed 2, stored 2')
        _, out = self.tool('get_statement_coverage')
        rows = {r['account_mask']: r for r in out['accounts']}
        self.assertEqual('supported', rows['3158']['status'])
        self.assertIn('2026-07', rows['3158']['missing_months'])
        self.assertEqual('2026-08-31', rows['3158']['last_statement'])
        self.assertEqual('manual_upload_required', rows['6030']['status'])
        self.assertTrue(rows['6030']['manual_upload_required'])

    def test_coverage_says_not_consented_and_off(self):
        self.acct('a1', '3158', 'depository', 'checking', CHK)
        self.log_pull('unsupported', 'statements_list failed: '
                                     'ADDITIONAL_CONSENT_REQUIRED')
        _, out = self.tool('get_statement_coverage')
        self.assertEqual('not_consented', out['accounts'][0]['status'])
        plaid_settings.set_statements_enabled(False)
        _, out = self.tool('get_statement_coverage')
        self.assertEqual('off', out['accounts'][0]['status'])

    def test_pull_statements_is_gated_and_runs(self):
        self.assertFalse(mcp_settings._DEFAULTS['pull_statements'])
        res, text = self.tool('pull_statements')
        self.assertTrue(res['isError'])
        self.assertIn('kill switch', text)
        mcp_settings.save({'pull_statements': True})
        with unittest.mock.patch.object(stmts, 'run_refresh_pull',
                                        return_value={'stored': 0}) as pull:
            res, _ = self.tool('pull_statements')
        self.assertFalse(res.get('isError'))
        pull.assert_called_once()


class NoSecretsInLogsTests(Base):
    def test_a_pull_logs_neither_the_access_token_nor_pdf_bytes(self):
        self.acct('a1', '3158', 'depository', 'checking', CHK)
        secret_pdf = checking_pdf() + b'SECRET-PDF-MARKER'
        plaid = FakePlaidClient(
            statements=[self._listed('st-9', month=9)],
            statement_pdfs={'st-9': secret_pdf})
        plaid.statements[0]['account_id'] = 'a1'
        with self.assertLogs('bankbridge', level='DEBUG') as cm:
            logging.getLogger('bankbridge.test').debug('start')
            stmts.fetch_all(plaid_client=plaid, refresh=True, wait_seconds=0)
        text = '\n'.join(cm.output)
        self.assertNotIn('access-x', text)
        self.assertNotIn('SECRET-PDF-MARKER', text)
        self.assertIsNotNone(PlaidStatement.query.filter_by(
            statement_id='st-9').first())


if __name__ == '__main__':
    unittest.main()
