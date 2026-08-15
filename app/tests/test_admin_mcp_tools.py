# SPDX-License-Identifier: MIT
"""The admin actions, as MCP tools, and the persistent JE gate (v0.8.4).

Tim ran the OML pipeline end to end on 2026-07-28 and found that rerunning
rules, resetting investment drafts, pausing Journal Entry generation and
re-pointing the ERPNext connection were all things only a human at a browser
could do. That is the relay tax the MCP server exists to remove.

Covered here:

  * each new tool is registered, gated by its own kill switch, and audited
  * `rerun_rules` refuses when the JE gate is off, rather than reporting a
    successful run that posted nothing — the check the admin route never had
  * the JE gate persists, overrides the env var that seeds it, and survives a
    fresh application context (it is a setting, not a process variable)
  * `set_erpnext_config` changes only the fields passed and never echoes the
    secret back
  * `post_clearing_cleanup_je` refuses to write while settlement legs are still
    unposted — the ordering that keeps a cleanup from double-correcting

Synthetic values only.
"""
import json
import os
import tempfile
import unittest

from app import create_app, erpnext_settings, mcp_settings  # noqa: E402
from app.blueprints import mcp_server  # noqa: E402
from app.models import AiActionLog  # noqa: E402

from tests.test_mcp_server import McpBase  # noqa: E402

NEW_TOOLS = ('get_clearing_status', 'rerun_rules', 'reset_investment_drafts',
             'post_clearing_cleanup_je', 'enable_je_gate', 'disable_je_gate',
             'set_erpnext_config')


class RegistrationTests(McpBase):
    def test_every_new_tool_is_listed(self):
        names = {t['name'] for t in
                 self._rpc('tools/list').get_json()['result']['tools']}
        for tool in NEW_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, names)

    def test_every_mutating_tool_has_a_kill_switch(self):
        """A mutating tool with no switch would be ungated — the one failure
        mode this registry cannot be allowed to have."""
        for name, spec in mcp_server.TOOLS.items():
            if spec['mutating']:
                with self.subTest(tool=name):
                    self.assertIn(name, mcp_settings._FIELDS)

    def test_every_kill_switch_defaults_off(self):
        for name in NEW_TOOLS:
            if name in mcp_settings._FIELDS:
                with self.subTest(tool=name):
                    self.assertFalse(mcp_settings._DEFAULTS[name])

    def test_the_read_only_tool_is_not_gated(self):
        self.assertFalse(mcp_server.TOOLS['get_clearing_status']['mutating'])
        self.assertNotIn('get_clearing_status', mcp_settings._FIELDS)

    def test_each_mutating_tool_is_blocked_with_its_switch_off(self):
        for tool in ('rerun_rules', 'reset_investment_drafts',
                     'enable_je_gate', 'disable_je_gate', 'set_erpnext_config'):
            with self.subTest(tool=tool):
                _, body = self._call_tool(tool, {'account_id': 'x'})
                self.assertTrue(body['result']['isError'])
                self.assertIn('kill switch',
                              body['result']['content'][0]['text'])


class JeGateTests(McpBase):
    def test_it_defaults_to_the_env_var(self):
        """An install that never touches the toggle behaves exactly as it did
        before v0.8.4 — the whole compatibility claim."""
        self.assertFalse(erpnext_settings.je_generation_enabled())

    def test_a_hand_edited_false_string_reads_as_off(self):
        """This file is operator-editable and `bool("false")` is True — the
        coercion has to go through `_as_bool`, not the builtin."""
        erpnext_settings.set_je_generation(True)
        path = os.path.join(self.app.config['DATA_DIR'],
                            'erpnext_settings.json')
        with open(path) as fh:
            data = json.load(fh)
        for written, expected in (('false', False), ('off', False),
                                  ('0', False), ('true', True), ('yes', True)):
            with self.subTest(written=written):
                data['auto_generate_journal_entries'] = written
                with open(path, 'w') as fh:
                    json.dump(data, fh)
                self.assertEqual(expected,
                                 erpnext_settings.je_generation_enabled())

    def test_the_toggle_overrides_the_env_default(self):
        self.app.config['ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES'] = True
        erpnext_settings.set_je_generation(False)
        self.assertFalse(erpnext_settings.je_generation_enabled())

    def test_enabling_through_mcp_persists(self):
        mcp_settings.save({'enable_je_gate': True})
        _, body = self._call_tool('enable_je_gate')
        self.assertFalse(body['result']['isError'])
        self.assertTrue(erpnext_settings.je_generation_enabled())

    def test_disabling_through_mcp_persists(self):
        erpnext_settings.set_je_generation(True)
        mcp_settings.save({'disable_je_gate': True})
        _, body = self._call_tool('disable_je_gate')
        payload = json.loads(body['result']['content'][0]['text'])
        self.assertFalse(payload['auto_generate_journal_entries'])
        self.assertTrue(payload['was'])
        self.assertFalse(erpnext_settings.je_generation_enabled())

    def test_it_survives_a_fresh_application_context(self):
        """It is a setting on disk, not a process variable — the point of the
        change. A compose edit and an app recreate used to be the only way."""
        erpnext_settings.set_je_generation(True)
        datadir = self.app.config['DATA_DIR']
        fd, path = tempfile.mkstemp(suffix='.sqlite')
        self.addCleanup(os.remove, path)
        self.addCleanup(os.close, fd)
        other = create_app({
            'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{path}',
            'DATA_DIR': datadir, 'FERNET_KEY': '', 'SCHEDULER_ENABLED': False,
            'ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES': False,
        })
        with other.app_context():
            self.assertTrue(erpnext_settings.je_generation_enabled())

    def test_the_admin_toggle_flips_it(self):
        client = self.app.test_client()
        resp = client.post('/admin/erpnext_settings/je_gate',
                           data={'enabled': '1'})
        self.assertEqual(302, resp.status_code)
        self.assertTrue(erpnext_settings.je_generation_enabled())
        client.post('/admin/erpnext_settings/je_gate', data={'enabled': '0'})
        self.assertFalse(erpnext_settings.je_generation_enabled())

    def test_the_settings_page_renders_the_toggle(self):
        """_page() has bitten this project before — a new context key can 500
        every admin page. Test-GET the route."""
        resp = self.app.test_client().get('/admin/erpnext_settings')
        self.assertEqual(200, resp.status_code)
        self.assertIn(b'Journal Entry generation', resp.data)


class RerunRulesTests(McpBase):
    def test_it_refuses_when_the_gate_is_off(self):
        """A rerun that generated nothing because posting was switched off used
        to look exactly like a rerun that found nothing to do."""
        mcp_settings.save({'rerun_rules': True})
        erpnext_settings.set_je_generation(False)
        _, body = self._call_tool('rerun_rules')
        self.assertTrue(body['result']['isError'])
        self.assertIn('OFF', body['result']['content'][0]['text'])

    def test_it_runs_when_the_gate_is_on(self):
        mcp_settings.save({'rerun_rules': True})
        erpnext_settings.set_je_generation(True)
        _, body = self._call_tool('rerun_rules')
        self.assertFalse(body['result']['isError'])
        payload = json.loads(body['result']['content'][0]['text'])
        # v0.8.5 added `dedup_skipped` — how many transactions the rerun
        # declined to write a JE for because ERPNext already held one.
        # v1.0.0 added `rule_source` and `rules_refreshed`: since the rules
        # moved to ERPNext, a rerun that generated nothing has two very
        # different causes, and these two keys are what tell them apart.
        self.assertEqual({'considered', 'matched', 'generated',
                          'dedup_skipped', 'rule_source', 'rules_refreshed'},
                         set(payload))
        self.assertIn(payload['rule_source'], ('erpnext', 'local'))

    def test_the_admin_route_and_the_tool_share_one_implementation(self):
        """Two copies of this drifted once already — the route had no gate
        check at all while the settings page claimed posting was off."""
        from app import categorization
        self.assertTrue(callable(categorization.rerun_rules))
        erpnext_settings.set_je_generation(False)
        resp = self.app.test_client().post('/admin/transactions/rerun_rules')
        self.assertEqual(302, resp.status_code)
        self.assertIn('OFF', resp.headers['Location'])


class SetErpnextConfigTests(McpBase):
    def setUp(self):
        super().setUp()
        mcp_settings.save({'set_erpnext_config': True})

    def test_it_changes_only_the_fields_passed(self):
        before = erpnext_settings.load()
        _, body = self._call_tool('set_erpnext_config',
                                  {'default_company': 'Other Co'})
        payload = json.loads(body['result']['content'][0]['text'])
        self.assertEqual(['default_company'], payload['changed_fields'])
        after = erpnext_settings.load()
        self.assertEqual('Other Co', after['default_company'])
        self.assertEqual(before['url'], after['url'])
        self.assertEqual(before['api_secret'], after['api_secret'])

    def test_it_never_echoes_the_secret(self):
        _, body = self._call_tool('set_erpnext_config',
                                  {'api_secret': 'super-secret-value'})
        text = body['result']['content'][0]['text']
        self.assertNotIn('super-secret-value', text)
        # The payload is JSON, so the mask arrives \u-escaped — decode before
        # asserting on it rather than matching the escape sequence.
        self.assertTrue(json.loads(text)['api_secret'].startswith('••••'))

    def test_an_omitted_secret_keeps_the_stored_one(self):
        _, _ = self._call_tool('set_erpnext_config',
                               {'url': 'http://new.erp.test'})
        self.assertEqual('SECRET', erpnext_settings.load()['api_secret'])

    def test_it_reports_whether_the_connection_is_now_complete(self):
        _, body = self._call_tool('set_erpnext_config', {'url': 'http://e.test'})
        payload = json.loads(body['result']['content'][0]['text'])
        self.assertTrue(payload['configured'])


class CleanupOrderingTests(McpBase):
    def test_it_refuses_to_post_while_settlements_are_unposted(self):
        """A cleanup written mid-backfill corrects an imbalance the backfill is
        about to correct again, and the ledger ends up wrong by the same six
        figures in the other direction."""
        mcp_settings.save({'post_clearing_cleanup_je': True})
        with unittest.mock.patch.object(
                mcp_server, '_erp_client_or_error', return_value=object()), \
             unittest.mock.patch(
                 'app.invest_je.clearing_status',
                 return_value={'unposted_settlements': 3,
                               'ledger_balance': -1000.0,
                               'projected_imbalance': 0.0}):
            _, body = self._call_tool('post_clearing_cleanup_je',
                                      {'account_id': 'brk', 'dry_run': False})
        self.assertTrue(body['result']['isError'])
        self.assertIn('still', body['result']['content'][0]['text'])

    def test_a_dry_run_is_allowed_regardless(self):
        """Previewing is how an operator finds out the backfill is pending."""
        mcp_settings.save({'post_clearing_cleanup_je': True})
        with unittest.mock.patch.object(
                mcp_server, '_erp_client_or_error', return_value=object()), \
             unittest.mock.patch(
                 'app.invest_je.clearing_status',
                 return_value={'unposted_settlements': 3}), \
             unittest.mock.patch(
                 'app.invest_je.clearing_cleanup_je',
                 return_value={'skipped': 'dry run', 'journal_entry': None,
                               'amount': 1000.0, 'counter_account': 'X'}):
            _, body = self._call_tool('post_clearing_cleanup_je',
                                      {'account_id': 'brk'})
        self.assertFalse(body['result']['isError'])


class AuditTests(McpBase):
    def test_a_blocked_call_is_still_logged(self):
        before = AiActionLog.query.count()
        self._call_tool('enable_je_gate')
        self.assertEqual(before + 1, AiActionLog.query.count())
        self.assertFalse(AiActionLog.query.order_by(
            AiActionLog.id.desc()).first().ok)

    def test_an_allowed_call_is_logged_ok(self):
        mcp_settings.save({'enable_je_gate': True})
        self._call_tool('enable_je_gate')
        row = AiActionLog.query.order_by(AiActionLog.id.desc()).first()
        self.assertEqual('enable_je_gate', row.tool_name)
        self.assertTrue(row.ok)


class SwitchDescriptionTests(McpBase):
    def test_every_switch_has_a_description_on_the_admin_page(self):
        """A switch with no label renders as a bare checkbox an operator cannot
        make a trust decision about."""
        from app.blueprints import admin_ui
        for name in mcp_settings._FIELDS:
            with self.subTest(switch=name):
                self.assertIn(name, admin_ui._MCP_SWITCH_DESC)

    def test_the_mcp_page_renders(self):
        resp = self.app.test_client().get('/admin/mcp')
        self.assertEqual(200, resp.status_code)


if __name__ == '__main__':
    unittest.main()
