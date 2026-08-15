# SPDX-License-Identifier: MIT
"""MCP server (v0.6.0).

The AI-operable surface: a JSON-RPC 2.0 endpoint at /mcp gated by a bearer token
(absent → 404) with per-mutating-tool kill switches (all default OFF) and an
AiActionLog row per call.

Covered here:
  * token missing → 404 (feature disabled)
  * valid token required → 401 without a good bearer
  * tools/list returns schemas with descriptions
  * a read tool returns correct JSON
  * a mutating tool is blocked when its kill switch is OFF, allowed when ON
  * every call (allowed or blocked) writes an AiActionLog row

Synthetic masks (4242 / 4200) and names only — no real account data.
"""
import json
import os
from datetime import date

from app import db, mcp_settings
from app.models import (AiActionLog, CategorizationRule, PlaidAccount,
                        PlaidItem, PlaidStatement, StatementAnchor)

from tests.fakes import unwrap_tool_payload
from tests.test_statements import StatementsBase

TOKEN = 'test-mcp-token-abcd'


class McpBase(StatementsBase):
    def setUp(self):
        super().setUp()
        os.environ['BB_MCP_AUTH_TOKEN'] = TOKEN
        self.client_ = self.app.test_client()

    def tearDown(self):
        os.environ.pop('BB_MCP_AUTH_TOKEN', None)
        super().tearDown()

    def _rpc(self, method, params=None, *, token=TOKEN, id=1):
        headers = {}
        if token is not None:
            headers['Authorization'] = f'Bearer {token}'
        return self.client_.post('/mcp', json={
            'jsonrpc': '2.0', 'id': id, 'method': method,
            'params': params or {}}, headers=headers)

    def _call_tool(self, name, arguments=None, **kw):
        resp = self._rpc('tools/call',
                         {'name': name, 'arguments': arguments or {}}, **kw)
        return resp, resp.get_json()

    def _seed_account(self, mask='4242'):
        a = PlaidAccount(account_id='acc-' + mask, item_id=self.item.item_id,
                         name='TEST BROKERAGE', mask=mask, type='investment',
                         subtype='brokerage')
        db.session.add(a)
        db.session.commit()
        return a


class TokenGateTest(McpBase):
    def test_missing_token_returns_404(self):
        os.environ.pop('BB_MCP_AUTH_TOKEN', None)   # feature disabled
        resp = self._rpc('initialize', token=None)
        self.assertEqual(resp.status_code, 404)

    def test_unauthorized_without_a_valid_bearer(self):
        self.assertEqual(self._rpc('tools/list', token=None).status_code, 401)
        self.assertEqual(self._rpc('tools/list', token='wrong').status_code, 401)

    def test_valid_token_authorizes(self):
        self.assertEqual(self._rpc('tools/list').status_code, 200)


class ToolsListTest(McpBase):
    def test_tools_list_returns_schemas_with_descriptions(self):
        body = self._rpc('tools/list').get_json()
        tools = {t['name']: t for t in body['result']['tools']}
        self.assertIn('get_reconciliation_status', tools)
        self.assertIn('create_rule', tools)
        for t in tools.values():
            self.assertTrue(t['description'])
            self.assertEqual(t['inputSchema']['type'], 'object')

    def test_initialize_reports_server_info(self):
        body = self._rpc('initialize').get_json()
        self.assertEqual(body['result']['serverInfo']['name'], 'bank-bridge')
        self.assertIn('protocolVersion', body['result'])


class ReadToolTest(McpBase):
    def test_account_topology_returns_json(self):
        self._seed_account('4242')
        resp, body = self._call_tool('get_account_topology')
        self.assertEqual(resp.status_code, 200)
        result = body['result']
        self.assertFalse(result['isError'])
        payload = unwrap_tool_payload(json.loads(result['content'][0]['text']))
        self.assertEqual(payload['count'], 1)
        self.assertEqual(payload['accounts'][0]['mask'], '4242')

    def test_reconciliation_status_for_a_mask(self):
        acct = self._seed_account('4242')
        st = PlaidStatement(statement_id='s1', plaid_item_id=self.item.item_id,
                            plaid_account_id=acct.account_id,
                            period_start=date(2026, 6, 1),
                            period_end=date(2026, 6, 30))
        db.session.add(st); db.session.commit()
        db.session.add(StatementAnchor(
            account_id=acct.account_id, statement_id=st.id,
            period_start=date(2026, 6, 1), period_end=date(2026, 6, 30),
            anchored_opening=100.0, anchored_closing=100.0,
            transaction_sum=0.0, computed_closing=100.0, variance=0.0))
        db.session.commit()
        _, body = self._call_tool('get_reconciliation_status',
                                  {'account_mask': '4242'})
        payload = unwrap_tool_payload(
            json.loads(body['result']['content'][0]['text']))
        self.assertEqual(payload['account_mask'], '4242')
        self.assertEqual(len(payload['anchors']), 1)
        self.assertEqual(payload['anchors'][0]['variance'], 0.0)

    def test_unknown_mask_is_a_tool_error_not_a_500(self):
        resp, body = self._call_tool('get_reconciliation_status',
                                     {'account_mask': '9999'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body['result']['isError'])


class KillSwitchTest(McpBase):
    def test_mutating_tool_blocked_when_kill_switch_off(self):
        resp, body = self._call_tool('create_rule', {
            'match_type': 'merchant_contains', 'match_value': 'TESTVENDOR',
            'offset_account': '5000 - Test', 'tag': 'test'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body['result']['isError'])
        self.assertIn('kill switch', body['result']['content'][0]['text'])
        self.assertEqual(CategorizationRule.query.count(), 0)

    def test_mutating_tool_allowed_when_kill_switch_on(self):
        mcp_settings.save({'create_rule': True})
        resp, body = self._call_tool('create_rule', {
            'match_type': 'merchant_contains', 'match_value': 'TESTVENDOR',
            'offset_account': '5000 - Test', 'tag': 'test'})
        self.assertFalse(body['result']['isError'])
        self.assertEqual(CategorizationRule.query.count(), 1)
        self.assertEqual(CategorizationRule.query.first().match_value,
                         'TESTVENDOR')

    def test_read_tools_are_never_gated(self):
        # No kill switch touched — a read tool still works.
        self._seed_account('4200')
        _, body = self._call_tool('get_account_topology')
        self.assertFalse(body['result']['isError'])


class AuditLogTest(McpBase):
    def test_every_call_writes_an_audit_row(self):
        self._seed_account('4242')
        self._call_tool('get_account_topology')
        rows = AiActionLog.query.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].tool_name, 'get_account_topology')
        self.assertTrue(rows[0].ok)

    def test_blocked_mutation_is_logged_with_ok_false(self):
        self._call_tool('create_rule', {'match_type': 'merchant_contains',
                                        'match_value': 'X'})
        row = AiActionLog.query.filter_by(tool_name='create_rule').first()
        self.assertIsNotNone(row)
        self.assertFalse(row.ok)
        self.assertIn('kill switch', row.result_summary)


class ModelDeclarationTest(McpBase):
    def test_ai_action_log_table_exists(self):
        # Built by create_all on a fresh DB — no migration DDL needed.
        self.assertEqual(AiActionLog.query.count(), 0)


class AdminPageTest(McpBase):
    def test_admin_mcp_page_renders(self):
        for url in ('/admin/mcp',):
            self.assertEqual(self.client_.get(url).status_code, 200, url)
        body = self.client_.get('/admin/mcp').data.decode()
        self.assertIn('MCP server', body)
        self.assertIn('kill switch', body.lower())
        self.assertIn('create_rule', body)

    def test_toggle_saves_switches(self):
        self.client_.post('/admin/mcp/toggle',
                          data={'create_rule': 'on'})
        self.assertTrue(mcp_settings.load()['create_rule'])
        self.assertFalse(mcp_settings.load()['trigger_reparse'])

    def test_test_connection_lists_tools(self):
        resp = self.client_.post('/admin/mcp/test')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('tools advertised', resp.data.decode())
