# SPDX-License-Identifier: MIT
"""Sync on demand — `sync_engine.run_sync` and the `sync_now` MCP tool (v0.8.5).

WHY THIS EXISTS. On 2026-07-29 the Cash Clearing cleanup stalled with 77
settlement legs still unposted for the ••9401 brokerage. `trigger_reparse` read
the statement PDFs and drafted nothing (wrong pipeline); `reset_investment_drafts`
refused on the first submitted entry (correctly). The only thing that would post
them was the Sync Now button in a browser, and a $1M ledger correction waited on
a click. The button's logic now lives in `run_sync` and both surfaces call it.

Covered here:

  * the happy path — transactions fetched, posted, and reported PER ACCOUNT
  * dry_run reports what would post and contacts nothing, writing nothing
  * account_id scopes the run: the other account's backlog is left alone
  * one bank failing does not stop the others — status 'partial', a structured
    error naming the Item, and the healthy bank's work still landed
  * every error carries a CODE and a REMEDY, not just a message
  * include_investments=false leaves the investment JE pass unrun
  * an already-posted trade counts as skipped_duplicate, not as a fresh post
  * the tool refuses while its kill switch is OFF, and is audited either way
  * the button and the tool call the same function

Synthetic ids, masks and amounts only.
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, crypto, db, mcp_settings, sync_engine  # noqa: E402
from app.blueprints import mcp_server  # noqa: E402
from app.models import (AiActionLog, BankTransaction, GeneratedJournalEntry,
                        PlaidAccount, PlaidItem, SecurityTransaction)  # noqa: E402
from app.plaid_client import PlaidError  # noqa: E402

from tests.fakes import FakeERPClient, FakePlaidClient, page, txn  # noqa: E402

TOKEN = 'test-mcp-token-sync-now'
CHK = 'acct-chk'
BRK = 'acct-brk'


class SyncNowBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
            'TAILSCALE_SIDECAR_ENABLED': False,
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

    # ── fixtures ────────────────────────────────────────────────────────────
    def _item(self, item_id='item-a', **kw):
        it = PlaidItem(item_id=item_id,
                       access_token_encrypted=crypto.encrypt('access-' + item_id),
                       institution_id='ins_1', institution_name='Test Bank',
                       status='active', **kw)
        db.session.add(it)
        db.session.commit()
        return it

    def _account(self, account_id=CHK, item_id='item-a', mask='1234',
                 type_='depository', subtype='checking', mapped=True):
        a = PlaidAccount(account_id=account_id, item_id=item_id,
                         name='Test ' + mask, mask=mask, type=type_,
                         subtype=subtype, sync_enabled=True,
                         erpnext_bank_account_name=(f'Bank {mask} - T'
                                                    if mapped else None))
        db.session.add(a)
        db.session.commit()
        return a

    def _plaid_account_payload(self, account_id, mask, type_='depository',
                               subtype='checking'):
        return {'account_id': account_id, 'name': 'Test ' + mask,
                'official_name': '', 'mask': mask, 'type': type_,
                'subtype': subtype, 'balance_available': None,
                'balance_current': 100.0, 'iso_currency_code': 'USD'}

    def _row(self, result, account_id):
        for row in result['accounts']:
            if row['account_id'] == account_id:
                return row
        self.fail(f'no summary row for {account_id}: '
                  f"{[r['account_id'] for r in result['accounts']]}")


# ── the happy path ──────────────────────────────────────────────────────────
class HappyPathTests(SyncNowBase):
    def test_it_fetches_posts_and_reports_per_account(self):
        self._item()
        self._account()
        plaid = FakePlaidClient(
            accounts=[self._plaid_account_payload(CHK, '1234')],
            pages=[page(added=[txn('t1', CHK, 25.5, name='Coffee'),
                               txn('t2', CHK, -100.0, name='Deposit')])])
        res = sync_engine.run_sync(plaid_client=plaid, erp_client=FakeERPClient())

        self.assertEqual(res['status'], 'ok', res['errors'])
        self.assertFalse(res['dry_run'])
        self.assertEqual(res['scope'], 'all')
        self.assertEqual(res['items'], 1)
        row = self._row(res, CHK)
        self.assertEqual(row['transactions_fetched']['added'], 2)
        self.assertEqual(row['transactions_fetched']['total'], 2)
        self.assertEqual(row['bank_transactions_posted'], 2)
        self.assertEqual(row['errors'], [])
        self.assertEqual(res['totals']['transactions_fetched'], 2)
        self.assertEqual(res['totals']['bank_transactions_posted'], 2)

    def test_a_first_ever_sync_reports_the_account_it_just_discovered(self):
        """The accounts are read BEFORE the pull, and a first sync creates them
        DURING it. Without a late lookup a brand-new bank's opening fifty
        transactions report as zero — the sync worked and the summary said
        nothing happened."""
        self._item()                      # no PlaidAccount row yet
        plaid = FakePlaidClient(
            accounts=[self._plaid_account_payload(CHK, '1234')],
            pages=[page(added=[txn('t1', CHK, 10.0), txn('t2', CHK, 20.0)])])
        res = sync_engine.run_sync(plaid_client=plaid, erp_client=FakeERPClient())
        self.assertEqual(res['totals']['transactions_fetched'], 2, res)
        self.assertEqual(self._row(res, CHK)['transactions_fetched']['added'], 2)

    def test_it_reports_elapsed_time_overall_and_per_account(self):
        """An operator deciding whether to retry needs to know what the last
        attempt cost them — and which bank ate the time."""
        self._item()
        self._account()
        res = sync_engine.run_sync(
            plaid_client=FakePlaidClient(accounts=[], pages=[page()]),
            erp_client=FakeERPClient())
        self.assertIsInstance(res['elapsed_seconds'], float)
        self.assertGreaterEqual(res['elapsed_seconds'], 0.0)
        self.assertIsInstance(self._row(res, CHK)['item_elapsed_seconds'], float)

    def test_a_removal_is_attributed_to_the_account_it_came_from(self):
        """Plaid's `removed` list carries no account_id — the attribution has to
        come from the mirrored row, or the account's column reads zero for work
        that plainly happened."""
        self._item()
        self._account()
        erp = FakeERPClient()
        first = FakePlaidClient(
            accounts=[self._plaid_account_payload(CHK, '1234')],
            pages=[page(added=[txn('t1', CHK, 25.5)])])
        sync_engine.run_sync(plaid_client=first, erp_client=erp)
        second = FakePlaidClient(accounts=[], pages=[page(removed=['t1'])])
        res = sync_engine.run_sync(plaid_client=second, erp_client=erp)
        self.assertEqual(self._row(res, CHK)['transactions_fetched']['removed'], 1)
        self.assertEqual(self._row(res, CHK)['bank_transactions_cancelled'], 1)


# ── dry run ─────────────────────────────────────────────────────────────────
class ExplodingPlaidClient(FakePlaidClient):
    """Any Plaid call at all is a test failure — a preview that bills the
    operator is not a preview."""
    def transactions_sync(self, *a, **kw):  # pragma: no cover - must not run
        raise AssertionError('dry_run contacted Plaid')

    def get_accounts(self, *a, **kw):  # pragma: no cover - must not run
        raise AssertionError('dry_run contacted Plaid')


class DryRunTests(SyncNowBase):
    def _pending(self, n=3):
        self._item()
        self._account()
        for i in range(n):
            db.session.add(BankTransaction(
                plaid_transaction_id=f'p{i}', account_id=CHK, amount=10.0 + i,
                name='Pending'))
        db.session.commit()

    def test_it_contacts_nothing_and_says_so(self):
        self._pending()
        res = sync_engine.run_sync(dry_run=True,
                                   plaid_client=ExplodingPlaidClient(),
                                   erp_client=FakeERPClient())
        self.assertTrue(res['dry_run'])
        self.assertFalse(res['plaid_contacted'])
        self.assertIn('cannot be known', res['note'])

    def test_it_counts_what_would_post(self):
        self._pending(3)
        res = sync_engine.run_sync(dry_run=True, erp_client=FakeERPClient())
        self.assertEqual(self._row(res, CHK)['would_post'], 3)

    def test_it_writes_nothing(self):
        self._pending(2)
        erp = FakeERPClient()
        sync_engine.run_sync(dry_run=True, erp_client=erp)
        self.assertEqual(erp.creates_of(), [])
        self.assertEqual(
            BankTransaction.query.filter(
                BankTransaction.posted_at.isnot(None)).count(), 0)

    def test_an_unmapped_account_is_named_with_a_remedy(self):
        """The 'where did my transactions go' answer, before the operator has
        to go looking for it."""
        self._item()
        self._account(mapped=False)
        db.session.add(BankTransaction(plaid_transaction_id='p0',
                                       account_id=CHK, amount=5.0, name='X'))
        db.session.commit()
        res = sync_engine.run_sync(dry_run=True, erp_client=FakeERPClient())
        row = self._row(res, CHK)
        self.assertEqual(row['unpostable_pending'], 1)
        self.assertEqual(row['errors'][0]['code'], 'account_unmapped')
        self.assertIn('/admin/accounts', row['errors'][0]['remedy'])


# ── scoping ─────────────────────────────────────────────────────────────────
class ScopeTests(SyncNowBase):
    def _two_banks(self):
        self._item('item-a')
        self._item('item-b')
        self._account(CHK, 'item-a', '1234')
        self._account('acct-other', 'item-b', '5678')
        for aid in (CHK, 'acct-other'):
            db.session.add(BankTransaction(
                plaid_transaction_id='pending-' + aid, account_id=aid,
                amount=42.0, name='Pending'))
        db.session.commit()

    def test_an_unknown_account_is_refused_before_anything_is_spent(self):
        with self.assertRaises(sync_engine.SyncScopeError) as caught:
            sync_engine.run_sync(account_id='acct-nope',
                                 plaid_client=ExplodingPlaidClient())
        self.assertIn('acct-nope', str(caught.exception))

    def test_only_the_scoped_account_posts(self):
        self._two_banks()
        plaid = FakePlaidClient(accounts=[], pages=[page(), page()])
        res = sync_engine.run_sync(account_id=CHK, plaid_client=plaid,
                                   erp_client=FakeERPClient())
        self.assertEqual(res['scope'], CHK)
        self.assertEqual(res['items'], 1)
        self.assertEqual([r['account_id'] for r in res['accounts']], [CHK])
        posted = BankTransaction.query.filter(
            BankTransaction.posted_at.isnot(None)).all()
        self.assertEqual([r.account_id for r in posted], [CHK])

    def test_a_scoped_run_polls_only_its_own_bank(self):
        self._two_banks()
        plaid = FakePlaidClient(accounts=[], pages=[page(), page()])
        sync_engine.run_sync(account_id=CHK, plaid_client=plaid,
                             erp_client=FakeERPClient())
        pulls = [c for c in plaid.calls if c[0] == 'transactions_sync']
        self.assertEqual(len(pulls), 1, plaid.calls)

    def test_an_account_on_a_parked_item_says_why_nothing_happened(self):
        self._item('item-parked', needs_reauth=True)
        self._account('acct-parked', 'item-parked', '9999')
        res = sync_engine.run_sync(account_id='acct-parked',
                                   plaid_client=FakePlaidClient(),
                                   erp_client=FakeERPClient())
        codes = [e['code'] for e in res['errors']]
        self.assertIn('item_needs_reauth', codes)
        self.assertEqual(res['status'], 'failed')


# ── partial failure ─────────────────────────────────────────────────────────
class PartialFailureTests(SyncNowBase):
    def test_one_bank_failing_does_not_stop_the_others(self):
        self._item('item-a')
        self._item('item-b')
        self._account(CHK, 'item-a', '1234')
        self._account('acct-other', 'item-b', '5678')

        class HalfBroken(FakePlaidClient):
            def transactions_sync(self, access_token, cursor=None, count=500):
                if access_token.endswith('item-b'):
                    raise PlaidError('INSTITUTION_DOWN')
                return super().transactions_sync(access_token, cursor, count)

        plaid = HalfBroken(accounts=[], pages=[page(added=[txn('t1', CHK, 9.0)])])
        res = sync_engine.run_sync(plaid_client=plaid, erp_client=FakeERPClient())

        self.assertEqual(res['status'], 'partial')
        self.assertEqual(res['totals']['bank_transactions_posted'], 1)
        broken = [e for e in res['errors'] if e['code'] == 'plaid_error']
        self.assertEqual(len(broken), 1, res['errors'])
        self.assertEqual(broken[0]['item_id'], 'item-b')
        self.assertTrue(broken[0]['remedy'])

    def test_the_failure_lands_on_the_account_the_operator_would_retry(self):
        self._item('item-b')
        self._account('acct-other', 'item-b', '5678')

        class Broken(FakePlaidClient):
            def transactions_sync(self, *a, **kw):
                raise PlaidError('ITEM_LOGIN_REQUIRED')

        res = sync_engine.run_sync(plaid_client=Broken(accounts=[]),
                                   erp_client=FakeERPClient())
        row = self._row(res, 'acct-other')
        self.assertEqual([e['code'] for e in row['errors']], ['plaid_error'])
        self.assertEqual(res['status'], 'failed')

    def test_every_error_carries_a_code_and_a_remedy(self):
        """Fail Forward: a bare string teaches nobody anything. Every code this
        engine can emit has a remedy written for it."""
        for code, remedy in sync_engine.SYNC_ERROR_REMEDIES.items():
            with self.subTest(code=code):
                self.assertTrue(remedy.strip(), code)
        entry = sync_engine._err('plaid_error', 'boom', item_id='item-a')
        self.assertEqual(entry['code'], 'plaid_error')
        self.assertEqual(entry['item_id'], 'item-a')
        self.assertTrue(entry['remedy'])

    def test_the_error_list_is_capped_and_says_that_it_is(self):
        errors = []
        for i in range(sync_engine.MAX_ERRORS_REPORTED + 5):
            sync_engine._collect(errors, sync_engine._err('plaid_error', i))
        self.assertEqual(len(errors), sync_engine.MAX_ERRORS_REPORTED + 1)
        self.assertEqual(errors[-1]['code'], 'errors_truncated')


# ── the investment half ─────────────────────────────────────────────────────
class InvestmentTests(SyncNowBase):
    def _brokerage(self):
        item = self._item('item-a', invest_je_posting_enabled=True)
        self._account(BRK, 'item-a', '9401', type_='investment',
                      subtype='brokerage')
        return item

    def test_include_investments_false_leaves_the_je_pass_unrun(self):
        self._brokerage()
        calls = []
        with mock.patch.object(sync_engine, 'post_investment_jes',
                               lambda *a, **kw: calls.append(a) or {}):
            sync_engine.run_sync(include_investments=False,
                                 plaid_client=FakePlaidClient(accounts=[],
                                                              pages=[page()]),
                                 erp_client=FakeERPClient())
        self.assertEqual(calls, [])

    def test_include_investments_defaults_to_running_it(self):
        self._brokerage()
        calls = []

        def spy(item, erp_client, only_account_id=''):
            calls.append(only_account_id)
            return {'posted': 0, 'skipped': 0, 'skipped_duplicate': 0,
                    'failed': 0, 'by_account': {}, 'errors': []}

        with mock.patch.object(sync_engine, 'post_investment_jes', spy):
            sync_engine.run_sync(plaid_client=FakePlaidClient(accounts=[],
                                                              pages=[page()]),
                                 erp_client=FakeERPClient())
        self.assertEqual(calls, [''])

    def test_a_scoped_run_scopes_the_je_pass_too(self):
        self._brokerage()
        calls = []

        def spy(item, erp_client, only_account_id=''):
            calls.append(only_account_id)
            return {'posted': 0, 'skipped': 0, 'skipped_duplicate': 0,
                    'failed': 0, 'by_account': {}, 'errors': []}

        with mock.patch.object(sync_engine, 'post_investment_jes', spy):
            sync_engine.run_sync(account_id=BRK,
                                 plaid_client=FakePlaidClient(accounts=[],
                                                              pages=[page()]),
                                 erp_client=FakeERPClient())
        self.assertEqual(calls, [BRK])

    def test_an_already_posted_trade_counts_as_a_duplicate_not_a_post(self):
        """The count that made a re-run over 455 settled trades report 'posted
        455' having written nothing. An operator cannot act on that number."""
        from app import invest_je
        self._brokerage()
        db.session.add(SecurityTransaction(
            plaid_investment_transaction_id='itx-1', account_id=BRK,
            type='buy', subtype='buy', quantity=1.0, price=10.0, amount=10.0))
        db.session.add(GeneratedJournalEntry(
            plaid_transaction_id='inv:itx-1',
            plaid_investment_transaction_id='itx-1',
            erpnext_journal_entry_name='JE-EXISTING', state='posted'))
        db.session.commit()
        stats = invest_je.post_investments_for_account(FakeERPClient(), BRK)
        self.assertEqual(stats['skipped_duplicate'], 1, stats)
        self.assertEqual(stats['posted'], 0, stats)

    def test_the_disabled_switch_is_a_notice_not_an_error(self):
        """It is the designed default on nearly every Item. Reporting it as a
        failure would mark every healthy sweep 'partial', and a status that is
        always 'partial' is not read on the day it matters."""
        self._item('item-a')                       # posting NOT enabled
        self._account(BRK, 'item-a', '9401', type_='investment',
                      subtype='brokerage')
        stats = sync_engine.post_investment_jes(
            PlaidItem.query.filter_by(item_id='item-a').first(), FakeERPClient())
        self.assertEqual([e['code'] for e in stats['notices']],
                         ['invest_je_posting_disabled'])
        self.assertEqual(stats['errors'], [])
        self.assertFalse(stats['posting_enabled'])

    def test_a_healthy_sweep_over_a_disabled_item_still_reads_ok(self):
        self._item('item-a')                       # posting NOT enabled
        self._account(BRK, 'item-a', '9401', type_='investment',
                      subtype='brokerage')
        res = sync_engine.run_sync(
            plaid_client=FakePlaidClient(accounts=[], pages=[page()]),
            erp_client=FakeERPClient())
        self.assertEqual(res['status'], 'ok', res['errors'])
        self.assertEqual([n['code'] for n in res['notices']],
                         ['invest_je_posting_disabled'])


# ── the two surfaces ────────────────────────────────────────────────────────
class SharedCoreTests(SyncNowBase):
    def test_the_button_calls_run_sync(self):
        """The button IS the tool from the other side. If they ever stop sharing
        a function, 'I clicked it and it worked' stops meaning anything about
        what the AI can do."""
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return {'items': 1, 'totals': {'transactions_fetched': 0,
                                           'bank_transactions_posted': 0},
                    'errors': [], 'accounts': [], 'status': 'ok'}

        with mock.patch.object(sync_engine, 'run_sync', fake):
            resp = self.app.test_client().post('/admin/sync_now')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(seen.get('actor'), 'admin')

    def test_the_scheduler_still_gets_the_shape_it_reads(self):
        self._item()
        self._account()
        res = sync_engine.sync_all(FakePlaidClient(accounts=[], pages=[page()]),
                                   FakeERPClient())
        self.assertEqual(res['items'], 1)
        self.assertIn('results', res)


class McpToolTests(SyncNowBase):
    def setUp(self):
        super().setUp()
        os.environ['BB_MCP_AUTH_TOKEN'] = TOKEN
        self.http = self.app.test_client()

    def tearDown(self):
        os.environ.pop('BB_MCP_AUTH_TOKEN', None)
        super().tearDown()

    def _call(self, arguments=None):
        resp = self.http.post('/mcp', json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'sync_now', 'arguments': arguments or {}}},
            headers={'Authorization': f'Bearer {TOKEN}'})
        return resp.get_json()['result']

    def test_it_is_registered_as_a_mutating_tool_with_a_kill_switch(self):
        names = {t['name'] for t in self.http.post('/mcp', json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
            headers={'Authorization': f'Bearer {TOKEN}'}
        ).get_json()['result']['tools']}
        self.assertIn('sync_now', names)
        self.assertTrue(mcp_server.TOOLS['sync_now']['mutating'])
        self.assertIn('sync_now', mcp_settings._FIELDS)

    def test_the_kill_switch_defaults_off(self):
        self.assertFalse(mcp_settings._DEFAULTS['sync_now'])
        self.assertFalse(mcp_settings.is_tool_enabled('sync_now'))

    def test_it_refuses_while_the_switch_is_off(self):
        result = self._call({'dry_run': True})
        self.assertTrue(result['isError'])
        self.assertIn('kill switch', result['content'][0]['text'])

    def test_a_refusal_is_audited(self):
        self._call({'dry_run': True})
        row = AiActionLog.query.filter_by(tool_name='sync_now').first()
        self.assertIsNotNone(row)
        self.assertFalse(row.ok)

    def test_it_runs_a_dry_run_once_the_switch_is_on(self):
        mcp_settings.save({'sync_now': True})
        self._item()
        self._account()
        db.session.add(BankTransaction(plaid_transaction_id='p0',
                                       account_id=CHK, amount=7.0, name='X'))
        db.session.commit()
        import json
        result = self._call({'dry_run': True})
        self.assertFalse(result['isError'], result['content'][0]['text'])
        payload = json.loads(result['content'][0]['text'])
        self.assertTrue(payload['dry_run'])
        self.assertFalse(payload['plaid_contacted'])
        row = AiActionLog.query.filter_by(tool_name='sync_now', ok=True).first()
        self.assertIn('DRY RUN', row.result_summary)

    def test_an_unknown_account_is_a_clean_tool_error(self):
        mcp_settings.save({'sync_now': True})
        result = self._call({'account_id': 'acct-nope', 'dry_run': True})
        self.assertTrue(result['isError'])
        self.assertIn('acct-nope', result['content'][0]['text'])

    def test_a_real_run_reports_per_account_and_is_audited(self):
        mcp_settings.save({'sync_now': True})
        self._item()
        self._account()
        plaid = FakePlaidClient(
            accounts=[self._plaid_account_payload(CHK, '1234')],
            pages=[page(added=[txn('t1', CHK, 12.0)])])
        import json
        with mock.patch.object(sync_engine, 'get_plaid_client', lambda: plaid), \
             mock.patch.object(sync_engine, 'get_erp_client_or_none',
                               FakeERPClient), \
             mock.patch('app.plaid_settings.is_configured', lambda: True):
            result = self._call({})
        self.assertFalse(result['isError'], result['content'][0]['text'])
        payload = json.loads(result['content'][0]['text'])
        self.assertEqual(payload['status'], 'ok', payload['errors'])
        self.assertEqual(payload['accounts'][0]['transactions_fetched']['added'],
                         1)
        self.assertEqual(payload['accounts'][0]['bank_transactions_posted'], 1)
        row = AiActionLog.query.filter_by(tool_name='sync_now', ok=True).first()
        self.assertIn('1 posted', row.result_summary)

    def test_a_quoted_false_is_not_read_as_true(self):
        """`bool("false")` is True. A dry_run that inverted on a quoted argument
        would spend real Plaid calls and write real documents on a preview."""
        mcp_settings.save({'sync_now': True})
        self._item()
        self._account()
        import json
        result = self._call({'dry_run': 'true'})
        self.assertTrue(json.loads(result['content'][0]['text'])['dry_run'])
        with mock.patch.object(sync_engine, 'run_sync') as run:
            run.return_value = {'scope': 'all', 'dry_run': False,
                                'status': 'ok', 'errors': [],
                                'elapsed_seconds': 0.0, 'accounts': [],
                                'totals': {'transactions_fetched': 0,
                                           'bank_transactions_posted': 0,
                                           'investment_jes_drafted': 0,
                                           'investment_jes_skipped_duplicate': 0}}
            self._call({'dry_run': 'false', 'include_investments': 'false'})
        self.assertEqual(run.call_args.kwargs['dry_run'], False)
        self.assertEqual(run.call_args.kwargs['include_investments'], False)

    def test_a_nonsense_flag_is_refused_rather_than_guessed(self):
        mcp_settings.save({'sync_now': True})
        result = self._call({'dry_run': 'maybe'})
        self.assertTrue(result['isError'])
        self.assertIn('dry_run', result['content'][0]['text'])

    def test_it_does_not_schedule_anything(self):
        """Kairos, not chronos: a manual trigger that quietly registered a job
        would be the tool deciding when to work, which is not its call."""
        mcp_settings.save({'sync_now': True})
        from app.services import scheduler
        with mock.patch.object(scheduler, 'ensure_scheduler_started') as sched:
            self._call({'dry_run': True})
        sched.assert_not_called()


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
