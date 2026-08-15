# SPDX-License-Identifier: MIT
"""The migrated tools' handover (v1.0.0).

Fourteen MCP tools whose subject moved to ERPNext in the consolidation. They are
NOT removed — they are wired into a Claude Desktop config and into whatever an
AI has learned to reach for, and deleting them mid-migration turns "here is a
better tool" into "your tool vanished", at which point the model guesses.

So each keeps working, unchanged, and each says where its subject went:

    {"deprecated": true, "use_instead": "erpnext.…", "data": {…}}

This file is the ONE place that envelope is asserted (every other test unwraps
it through tests.fakes.unwrap_tool_payload), so a change to the handover shape
breaks exactly here rather than in forty places that were only ever asserting on
what a tool returned.

    cd app
    python3 -m unittest tests.test_deprecation -v
"""
import json

from app import mcp_settings
from app.blueprints import mcp_server

from tests.test_mcp_server import McpBase

# The plan's §6 migration matrix, restated as a literal. Deliberately hand
# written rather than derived from DEPRECATED_TOOLS — comparing the map to
# itself would assert nothing, and the whole value here is catching a tool that
# was migrated on the ERPNext side and never flagged on this one.
EXPECTED = {
    'get_reconciliation_status': 'erpnext.get_statement_anchor_chain',
    'list_unreconciled_statements': 'erpnext.list_unreconciled_anchors',
    'get_variance_breakdown': 'erpnext.get_anchor_variance_breakdown',
    'list_unmatched_statement_transactions':
        'erpnext.list_unmatched_statement_lines',
    'set_variance_tag': 'erpnext.set_anchor_variance_reason',
    'get_account_topology': 'erpnext.get_account_pairing',
    'pair_accounts': 'erpnext.pair_bank_accounts',
    'list_rules': 'erpnext.list_bank_categorization_rules',
    'create_rule': 'erpnext.create_bank_categorization_rule',
    'create_advisory_agreement': 'erpnext.create_advisory_agreement',
    'get_advisory_agreement_summary': 'erpnext.get_advisory_agreement_summary',
    'update_advisory_agreement': 'erpnext.update_advisory_agreement',
    'get_statement_recon_report': 'erpnext.get_statement_recon_report',
}

# The tools that STAY — the pipe. Plan §6: Plaid connectivity, PDF forensics,
# investment operations, JE generation and the operational gates. If one of
# these ever grows a deprecation flag, something has gone wrong with the split.
PIPE_TOOLS = (
    'sync_now', 'trigger_reparse', 'rebuild_anchors', 'rerun_rules',
    'list_statements', 'get_statement_pdf', 'get_statement_extracted_data',
    'list_plaid_transactions', 'list_investment_transactions', 'list_holdings',
    'list_unpaired_trades', 'get_clearing_status', 'post_clearing_cleanup_je',
    'reset_investment_drafts', 'get_draft_health', 'enable_je_gate',
    'disable_je_gate', 'enable_je_posting', 'disable_je_posting',
    'enable_public_url', 'disable_public_url', 'get_public_url_status',
    'test_public_url', 'set_erpnext_config',
)


class MatrixTest(McpBase):
    def test_every_migrated_tool_carries_a_pointer(self):
        for name, target in EXPECTED.items():
            self.assertEqual(mcp_server.DEPRECATED_TOOLS.get(name), target,
                             f'{name} points somewhere unexpected')

    def test_update_rule_points_at_the_amendment_workflow(self):
        """ERPNext's amendment is a WORKFLOW, not a tool: a Bank Categorization
        Rule is superseded, never patched — the same non-destructive shape
        update_rule implements here."""
        pointer = mcp_server.DEPRECATED_TOOLS['update_rule']
        self.assertIn('amendment workflow', pointer)

    def test_the_count_is_the_plans_fourteen(self):
        self.assertEqual(len(mcp_server.DEPRECATED_TOOLS), 14)

    def test_every_deprecated_name_is_a_real_tool(self):
        """A pointer on a tool that does not exist is a lie an AI cannot act
        on — and a typo here would be invisible without this."""
        for name in mcp_server.DEPRECATED_TOOLS:
            self.assertIn(name, mcp_server.TOOLS)

    def test_the_pipe_tools_are_not_flagged(self):
        for name in PIPE_TOOLS:
            self.assertIn(name, mcp_server.TOOLS, f'{name} disappeared')
            self.assertNotIn(name, mcp_server.DEPRECATED_TOOLS,
                             f'{name} is a pipe tool and must not be deprecated')


class EnvelopeTest(McpBase):
    def _raw(self, name, arguments=None):
        _, body = self._call_tool(name, arguments or {})
        result = body['result']
        self.assertFalse(result['isError'], result['content'][0]['text'])
        return json.loads(result['content'][0]['text'])

    def test_a_deprecated_read_tool_wraps_its_answer(self):
        self._seed_account('4242')
        payload = self._raw('get_account_topology')
        self.assertIs(payload['deprecated'], True)
        self.assertEqual(payload['use_instead'], 'erpnext.get_account_pairing')
        self.assertIn('data', payload)

    def test_the_wrapped_data_is_exactly_what_the_tool_always_returned(self):
        """Deprecation is a label, not a behaviour change. These tools have to
        keep answering correctly through the whole migration window."""
        self._seed_account('4242')
        payload = self._raw('get_account_topology')
        self.assertEqual(payload['data']['count'], 1)
        self.assertEqual(payload['data']['accounts'][0]['mask'], '4242')

    def test_a_current_tool_is_not_wrapped(self):
        """A caller should not have to strip an envelope that isn't there."""
        payload = self._raw('get_public_url_status')
        self.assertNotIn('deprecated', payload)
        self.assertIn('mode', payload)

    def test_a_deprecated_mutating_tool_wraps_too_when_permitted(self):
        mcp_settings.save({'pair_accounts': True})
        brk = self._seed_account('4242')
        self._seed_account('4200')
        payload = self._raw('pair_accounts',
                            {'brokerage_mask': '4242',
                             'cash_services_mask': '4200'})
        self.assertIs(payload['deprecated'], True)
        self.assertEqual(payload['use_instead'], 'erpnext.pair_bank_accounts')
        self.assertEqual(payload['data']['cash_services_mask'], '4200')
        self.assertEqual(brk.paired_account_id, 'acc-4200')

    def test_a_kill_switch_refusal_is_still_a_plain_error_not_an_envelope(self):
        """An error result's content is a human message, not JSON. Wrapping it
        would make the refusal unreadable to the client that must show it."""
        _, body = self._call_tool('pair_accounts', {'brokerage_mask': '4242'})
        result = body['result']
        self.assertTrue(result['isError'])
        self.assertIn('kill switch', result['content'][0]['text'])

    def test_a_tool_error_is_not_wrapped_either(self):
        mcp_settings.save({'set_variance_tag': True})
        _, body = self._call_tool('set_variance_tag',
                                  {'anchor_id': 999, 'reason': 'x'})
        result = body['result']
        self.assertTrue(result['isError'])
        self.assertIn('no anchor', result['content'][0]['text'])


class ListingTest(McpBase):
    def _tools(self):
        body = self._rpc('tools/list').get_json()
        return {t['name']: t for t in body['result']['tools']}

    def test_a_deprecated_tool_announces_itself_before_it_is_called(self):
        """The response flag only reaches a caller that already committed. The
        description is what changes the CHOICE."""
        spec = self._tools()['get_reconciliation_status']
        self.assertTrue(spec['description'].startswith('DEPRECATED (v1.0.0)'))
        self.assertIn('erpnext.get_statement_anchor_chain',
                      spec['description'])

    def test_the_notice_is_a_prefix_so_a_truncating_client_keeps_it(self):
        for name in mcp_server.DEPRECATED_TOOLS:
            head = self._tools()[name]['description'][:200]
            self.assertIn('DEPRECATED', head, name)

    def test_the_original_description_survives_the_prefix(self):
        """The tool still works; its own documentation still has to be there."""
        spec = self._tools()['list_rules']
        self.assertIn('categorization rules', spec['description'])

    def test_a_current_tool_keeps_a_clean_description(self):
        spec = self._tools()['sync_now']
        self.assertNotIn('DEPRECATED', spec['description'])

    def test_the_new_consolidation_tools_are_listed(self):
        tools = self._tools()
        self.assertIn('get_erpnext_push_status', tools)
        self.assertIn('flush_erpnext_push_queue', tools)


class PushStatusToolTest(McpBase):
    def test_it_reports_the_sources_and_an_empty_queue(self):
        payload = json.loads(
            self._call_tool('get_erpnext_push_status')[1]
            ['result']['content'][0]['text'])
        self.assertEqual(payload['queue_depth'], 0)
        self.assertEqual(payload['sources']['anchor_source'], 'erpnext')
        self.assertIn(payload['sources']['rule_source_in_force'],
                      ('erpnext', 'local'))

    def test_it_counts_the_accounts_an_anchor_push_would_skip(self):
        """An unmapped account is the usual reason a reconciliation never
        reaches ERPNext, and it is a mapping decision only an operator can
        make — so it is reported as a count, not queued as a failure."""
        self._seed_account('4242')
        payload = json.loads(
            self._call_tool('get_erpnext_push_status')[1]
            ['result']['content'][0]['text'])
        self.assertEqual(payload['unmapped_accounts'], 1)

    def test_the_flush_tool_is_gated(self):
        _, body = self._call_tool('flush_erpnext_push_queue')
        self.assertTrue(body['result']['isError'])
        self.assertIn('kill switch', body['result']['content'][0]['text'])

    def test_the_status_tool_is_not_gated(self):
        """Reading the state of a queue cannot change the books — the same
        reasoning get_draft_health carries."""
        self.assertNotIn('get_erpnext_push_status', mcp_settings._FIELDS)
        _, body = self._call_tool('get_erpnext_push_status')
        self.assertFalse(body['result']['isError'])


if __name__ == '__main__':  # pragma: no cover
    import unittest
    unittest.main()
