# SPDX-License-Identifier: MIT
"""v0.7.2 — the "Connect to Claude Desktop" widget on /admin/mcp.

The invariant worth protecting here is that the REAL bearer token never reaches
the page source. v0.6.0 established that /admin/mcp renders only a masked token;
a config preview containing the live one would quietly undo it. So the preview is
masked and the buttons fetch the config from a dedicated endpoint — and there are
tests on both halves of that split.

Also covers URL derivation (from the request, never from the Funnel — see
app/claude_desktop.py for why), OS detection from the User-Agent, and the
download endpoint's headers and 404-when-disabled behaviour.

    cd app
    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import claude_desktop as cd  # noqa: E402
from app import create_app, crypto, db  # noqa: E402

TOKEN = 'super-secret-token-abcd1234'
MAC_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36')
WIN_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/120 Safari/537.36')
LINUX_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120 Safari/537.36')
IPHONE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
             'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile')


# ── OS detection ─────────────────────────────────────────────────────────────
class DetectOsTest(unittest.TestCase):
    def test_desktop_platforms(self):
        self.assertEqual('macos', cd.detect_os(MAC_UA))
        self.assertEqual('windows', cd.detect_os(WIN_UA))
        self.assertEqual('linux', cd.detect_os(LINUX_UA))

    def test_an_iphone_is_not_reported_as_a_mac(self):
        """An iPhone's UA contains "like Mac OS X". Claude Desktop has no mobile
        build, so guessing macOS here would show a path the device can't use."""
        self.assertEqual('', cd.detect_os(IPHONE_UA))

    def test_other_mobiles_are_unknown_too(self):
        for ua in ('Mozilla/5.0 (Linux; Android 14; Pixel 8)',
                   'Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)',
                   'Mozilla/5.0 (iPod touch; CPU iPhone OS 16_0 like Mac OS X)'):
            self.assertEqual('', cd.detect_os(ua), ua)

    def test_missing_or_unrecognised_agents_are_unknown(self):
        for ua in (None, '', '   ', 'curl/8.4.0', 'Wget/1.21'):
            self.assertEqual('', cd.detect_os(ua), repr(ua))

    def test_windows_wins_over_a_stray_linux_token(self):
        self.assertEqual('windows', cd.detect_os('Windows NT 10.0; Linux-ish'))

    def test_every_detected_os_has_a_path_a_label_and_a_quit_hint(self):
        for name in ('macos', 'windows', 'linux'):
            self.assertTrue(cd.config_path_for(name), name)
            self.assertTrue(cd.OS_LABELS[name], name)
            self.assertTrue(cd.QUIT_HINTS[name], name)

    def test_the_documented_paths(self):
        self.assertEqual(
            '~/Library/Application Support/Claude/claude_desktop_config.json',
            cd.config_path_for('macos'))
        self.assertEqual(
            r'%APPDATA%\Claude\claude_desktop_config.json',
            cd.config_path_for('windows'))
        self.assertEqual('~/.config/Claude/claude_desktop_config.json',
                         cd.config_path_for('linux'))

    def test_an_unknown_os_has_no_path(self):
        self.assertEqual('', cd.config_path_for(''))
        self.assertEqual('', cd.config_path_for('plan9'))


# ── URL derivation ───────────────────────────────────────────────────────────
class McpUrlTest(unittest.TestCase):
    def test_it_uses_the_host_and_scheme_it_was_reached_by(self):
        self.assertEqual('http://umbrel.local:5202/mcp',
                         cd.mcp_url('umbrel.local:5202', 'http'))
        self.assertEqual('https://box.example/mcp',
                         cd.mcp_url('box.example', 'https'))

    def test_it_defaults_to_http(self):
        self.assertEqual('http://h.test/mcp', cd.mcp_url('h.test'))

    def test_a_bare_ip_and_port_works(self):
        self.assertEqual('http://192.168.1.20:5202/mcp',
                         cd.mcp_url('192.168.1.20:5202'))

    def test_an_odd_scheme_falls_back_to_http(self):
        for scheme in ('ftp', 'javascript', '', None):
            self.assertTrue(cd.mcp_url('h.test', scheme).startswith('http://'),
                            repr(scheme))

    def test_no_host_yields_no_url(self):
        self.assertEqual('', cd.mcp_url(''))
        self.assertEqual('', cd.mcp_url(None))

    def test_the_path_is_not_under_the_bankbridge_prefix(self):
        """/mcp is LAN-only by design and deliberately outside the Funnel
        prefix — moving it under /bankbridge/ would put it in the published
        path space."""
        self.assertEqual('/mcp', cd.MCP_PATH)
        self.assertNotIn('/bankbridge', cd.mcp_url('h.test'))


# ── the config document ──────────────────────────────────────────────────────
class ConfigEntryTest(unittest.TestCase):
    def setUp(self):
        self.entry = cd.config_entry('http://h.test:5202/mcp', TOKEN)
        self.server = self.entry['mcpServers']['bankbridge']

    def test_it_is_an_mcpservers_object_keyed_by_the_server_name(self):
        self.assertEqual(['bankbridge'], list(self.entry['mcpServers']))
        self.assertEqual('bankbridge', cd.SERVER_KEY)

    def test_it_runs_mcp_remote_via_npx(self):
        self.assertEqual('npx', self.server['command'])
        self.assertIn('mcp-remote', self.server['args'])
        self.assertIn('-y', self.server['args'])

    def test_it_carries_the_url(self):
        self.assertIn('http://h.test:5202/mcp', self.server['args'])

    def test_it_forces_http_only_transport(self):
        """Bank Bridge implements the Streamable-HTTP surface and no SSE
        endpoint, so an SSE-first probe would fail on every start."""
        args = self.server['args']
        self.assertEqual('http-only', args[args.index('--transport') + 1])

    def test_it_allows_plain_http(self):
        """The URL is a LAN address; mcp-remote refuses http without this."""
        self.assertIn('--allow-http', self.server['args'])

    def test_the_header_arg_contains_no_space(self):
        """Claude Desktop on Windows (and Cursor) fail to escape spaces inside
        `args` when invoking npx, mangling the value. mcp-remote's own README
        prescribes this split, so the space lives in `env` instead."""
        args = self.server['args']
        header = args[args.index('--header') + 1]
        self.assertEqual('Authorization:${AUTH_HEADER}', header)
        self.assertNotIn(' ', header)

    def test_the_token_rides_in_env_with_the_bearer_prefix(self):
        self.assertEqual(f'Bearer {TOKEN}', self.server['env']['AUTH_HEADER'])

    def test_no_arg_contains_the_raw_token(self):
        """It belongs in env only — duplicating it into args would reintroduce
        the space-mangling bug."""
        for arg in self.server['args']:
            self.assertNotIn(TOKEN, arg)

    def test_config_json_is_readable_and_valid(self):
        text = cd.config_json('http://h.test/mcp', TOKEN)
        self.assertEqual(cd.config_entry('http://h.test/mcp', TOKEN),
                         json.loads(text))
        self.assertIn('\n  ', text)  # indented, not one line


# ── masking ──────────────────────────────────────────────────────────────────
class MaskingTest(unittest.TestCase):
    def test_the_preview_token_hides_all_but_the_last_four(self):
        self.assertEqual('••••••••1234', cd.preview_token(TOKEN))

    def test_the_mask_length_does_not_leak_the_token_length(self):
        """The shipped compose sets the token to APP_SEED, so a mask as long as
        the secret would both look broken and disclose its size."""
        short = cd.preview_token('abcdefgh')
        long = cd.preview_token('x' * 400 + 'wxyz')
        self.assertEqual(len(short), len(long))

    def test_a_short_token_is_still_masked(self):
        self.assertEqual('••••••••', cd.preview_token('abcd'))
        self.assertNotIn('abcd', cd.preview_token('abcd'))

    def test_an_empty_token_masks_to_nothing(self):
        self.assertEqual('', cd.preview_token(''))
        self.assertEqual('', cd.preview_token(None))

    def test_the_preview_json_never_contains_the_token(self):
        text = cd.preview_json('http://h.test/mcp', TOKEN)
        self.assertNotIn(TOKEN, text)
        self.assertIn('••••••••1234', text)

    def test_the_preview_json_is_otherwise_identical_to_the_real_thing(self):
        """It has to be a faithful preview, or the operator can't trust it."""
        preview = json.loads(cd.preview_json('http://h.test/mcp', TOKEN))
        real = json.loads(cd.config_json('http://h.test/mcp', TOKEN))
        p = preview['mcpServers']['bankbridge']
        r = real['mcpServers']['bankbridge']
        self.assertEqual(r['command'], p['command'])
        self.assertEqual(r['args'], p['args'])
        self.assertEqual(['AUTH_HEADER'], list(p['env']))

    def test_the_preview_command_never_contains_the_token(self):
        text = cd.preview_claude_code_command('http://h.test/mcp', TOKEN)
        self.assertNotIn(TOKEN, text)
        self.assertIn('••••••••1234', text)


# ── the Claude Code one-liner ────────────────────────────────────────────────
class ClaudeCodeCommandTest(unittest.TestCase):
    def test_it_matches_the_documented_cli_syntax(self):
        self.assertEqual(
            'claude mcp add --transport http bankbridge http://h.test/mcp '
            '--header "Authorization: Bearer ' + TOKEN + '"',
            cd.claude_code_command('http://h.test/mcp', TOKEN))

    def test_the_header_keeps_its_space_here(self):
        """Unlike the Desktop JSON: this is a shell command, where the quoted
        value is passed through intact."""
        self.assertIn('"Authorization: Bearer ', cd.claude_code_command('u', 't'))

    def test_it_does_not_use_mcp_remote(self):
        """Claude Code speaks HTTP MCP natively — no proxy needed."""
        self.assertNotIn('mcp-remote', cd.claude_code_command('u', 't'))
        self.assertNotIn('npx', cd.claude_code_command('u', 't'))


# ── the page and the endpoint ────────────────────────────────────────────────
class WidgetBase(unittest.TestCase):
    ENABLED = True

    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self.data_dir = tempfile.mkdtemp()
        env = {'BB_MCP_AUTH_TOKEN': TOKEN} if self.ENABLED else {}
        self._env = mock.patch.dict(os.environ, env, clear=False)
        self._env.start()
        if not self.ENABLED:
            os.environ.pop('BB_MCP_AUTH_TOKEN', None)
        self.app = create_app({
            'TESTING': True, 'SCHEDULER_ENABLED': False, 'FERNET_KEY': '',
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self.data_dir,
            # No sidecar probing: this suite is about the widget, and the
            # Funnel-vs-LAN decision is asserted explicitly where it matters.
            'TAILSCALE_SIDECAR_ENABLED': False,
            'TAILSCALE_FUNNEL_HOSTNAME': '',
        })
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self._env.stop()
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def page(self, ua=MAC_UA, **kw):
        return self.client.get('/admin/mcp', headers={'User-Agent': ua}, **kw)


class WidgetEnabledTest(WidgetBase):
    def test_the_page_renders_the_widget(self):
        r = self.page()
        self.assertEqual(200, r.status_code)
        body = r.data.decode()
        self.assertIn('Connect to Claude Desktop', body)
        self.assertIn('Connect from Claude Code', body)
        self.assertIn('id="cdCopyBtn"', body)
        self.assertIn('Download config file', body)

    def test_THE_TOKEN_IS_NEVER_IN_THE_PAGE_SOURCE(self):
        """The headline invariant of this release."""
        self.assertNotIn(TOKEN, self.page().data.decode())

    def test_the_preview_shows_a_masked_token(self):
        body = self.page().data.decode()
        self.assertIn('••••••••1234', body)
        self.assertIn('token masked', body)

    def test_it_says_the_buttons_carry_the_real_token(self):
        self.assertIn('include the real token', self.page().data.decode())

    def test_the_preview_carries_the_working_config_shape(self):
        body = self.page().data.decode()
        self.assertIn('mcpServers', body)
        self.assertIn('mcp-remote', body)
        self.assertIn('http-only', body)
        self.assertIn('--allow-http', body)
        self.assertIn('Authorization:${AUTH_HEADER}', body)

    def test_it_shows_the_os_specific_path(self):
        self.assertIn('Library/Application Support/Claude', self.page().data.decode())
        self.assertIn(r'%APPDATA%\Claude',
                      self.page(ua=WIN_UA).data.decode())
        self.assertIn('~/.config/Claude', self.page(ua=LINUX_UA).data.decode())

    def test_it_labels_the_detected_os(self):
        self.assertIn('<b>macOS</b>', self.page().data.decode())
        self.assertIn('<b>Windows</b>', self.page(ua=WIN_UA).data.decode())

    def test_it_always_calls_the_path_a_default_that_may_differ(self):
        self.assertIn('may\n    differ on your install',
                      self.page().data.decode().replace('may differ',
                                                        'may\n    differ'))

    def test_an_unknown_os_lists_every_path_instead_of_guessing(self):
        body = self.page(ua='curl/8.4.0').data.decode()
        self.assertIn('unknown OS', body)
        self.assertIn('Library/Application Support/Claude', body)
        self.assertIn(r'%APPDATA%\Claude', body)
        self.assertIn('~/.config/Claude', body)

    def test_it_gives_the_os_specific_quit_instruction(self):
        self.assertIn('Cmd-Q', self.page().data.decode())
        self.assertIn('Alt+F4', self.page(ua=WIN_UA).data.decode())

    def test_it_warns_against_replacing_an_existing_config(self):
        body = self.page().data.decode()
        self.assertIn("don't replace the whole file", body)
        self.assertIn('merge', body)

    def test_it_shows_the_claude_code_one_liner_masked(self):
        body = self.page().data.decode()
        self.assertIn('claude mcp add --transport http bankbridge', body)
        self.assertIn('id="cdCmdCopyBtn"', body)
        self.assertNotIn(TOKEN, body)

    def test_the_url_comes_from_the_request_host(self):
        body = self.page(base_url='http://umbrel.local:5202').data.decode()
        self.assertIn('http://umbrel.local:5202/mcp', body)

    def test_the_download_control_is_a_styled_download_anchor(self):
        """It must be an <a download> so the browser's own save machinery runs —
        and it must carry its own styling, because BASE_CSS scopes `.secondary`
        to button elements, so a bare class= would render a plain blue link."""
        body = self.page().data.decode()
        anchor = body[body.index('<a href="/admin/mcp/claude_desktop_config.json"'):]
        anchor = anchor[:anchor.index('</a>')]
        self.assertIn('download', anchor)
        self.assertIn('border:1px solid', anchor)
        self.assertIn('padding:', anchor)
        self.assertNotIn('class="secondary"', anchor)

    def test_the_buttons_fetch_rather_than_embed(self):
        body = self.page().data.decode()
        self.assertIn('data-src="/admin/mcp/claude_desktop_config.json"', body)
        self.assertIn('fetch(', body)

    def test_the_copy_button_has_an_execcommand_fallback(self):
        """The admin UI is plain http on the LAN, where navigator.clipboard
        does not exist."""
        body = self.page().data.decode()
        self.assertIn('execCommand', body)
        self.assertIn('isSecureContext', body)

    def test_it_mentions_the_kill_switches_when_all_are_off(self):
        self.assertIn('mutating tool stays blocked', self.page().data.decode())


class FunnelIsNotUsedTest(WidgetBase):
    """The URL must stay the LAN address even when a Funnel exists: the Funnel
    publishes only the OAuth callback path, so https://<host>/mcp 404s — and
    publishing /mcp would put the AI surface on the public Internet."""

    def test_a_funnel_hostname_does_not_become_the_mcp_url(self):
        self.app.config['TAILSCALE_SIDECAR_ENABLED'] = False
        self.app.config['TAILSCALE_FUNNEL_HOSTNAME'] = 'umbrel.tail1234.ts.net'
        body = self.page(base_url='http://umbrel.local:5202').data.decode()
        self.assertIn('http://umbrel.local:5202/mcp', body)
        self.assertNotIn('https://umbrel.tail1234.ts.net/mcp', body)

    def test_it_explains_why_the_funnel_is_not_used(self):
        self.app.config['TAILSCALE_FUNNEL_HOSTNAME'] = 'umbrel.tail1234.ts.net'
        body = self.page().data.decode()
        self.assertIn('deliberately', body)
        self.assertIn('only the Plaid OAuth callback path', body)

    def test_the_served_config_also_uses_the_lan_url(self):
        self.app.config['TAILSCALE_FUNNEL_HOSTNAME'] = 'umbrel.tail1234.ts.net'
        r = self.client.get('/admin/mcp/claude_desktop_config.json',
                            base_url='http://umbrel.local:5202')
        args = r.get_json()['mcpServers']['bankbridge']['args']
        self.assertIn('http://umbrel.local:5202/mcp', args)
        self.assertNotIn('https://umbrel.tail1234.ts.net/mcp', args)


class WidgetDisabledTest(WidgetBase):
    ENABLED = False

    def test_the_page_still_renders(self):
        self.assertEqual(200, self.page().status_code)

    def test_it_shows_the_grey_not_enabled_card(self):
        body = self.page().data.decode()
        self.assertIn('Connect to Claude Desktop', body)
        self.assertIn('MCP is not enabled', body)
        self.assertIn('BB_MCP_AUTH_TOKEN', body)

    def test_no_config_is_rendered_at_all(self):
        body = self.page().data.decode()
        self.assertNotIn('mcpServers', body)
        self.assertNotIn('mcp-remote', body)
        self.assertNotIn('claude mcp add', body)
        self.assertNotIn('••••', body)

    def test_no_buttons_are_offered(self):
        body = self.page().data.decode()
        self.assertNotIn('id="cdCopyBtn"', body)
        self.assertNotIn('Download config file', body)


class ConfigEndpointTest(WidgetBase):
    URL = '/admin/mcp/claude_desktop_config.json'

    def test_it_serves_json_with_the_real_token(self):
        r = self.client.get(self.URL)
        self.assertEqual(200, r.status_code)
        self.assertEqual('application/json', r.headers['Content-Type'])
        entry = r.get_json()['mcpServers']['bankbridge']
        self.assertEqual(f'Bearer {TOKEN}', entry['env']['AUTH_HEADER'])

    def test_it_downloads_under_the_expected_filename(self):
        r = self.client.get(self.URL)
        self.assertEqual('attachment; filename=claude_desktop_config.json',
                         r.headers['Content-Disposition'])

    def test_it_is_never_cached(self):
        """It carries a bearer token, and it must not be re-served after
        BB_MCP_AUTH_TOKEN is rotated."""
        self.assertEqual('no-store',
                         self.client.get(self.URL).headers['Cache-Control'])

    def test_the_payload_parses_as_json(self):
        json.loads(self.client.get(self.URL).data.decode())

    def test_the_claude_code_format_returns_the_one_liner_as_text(self):
        r = self.client.get(self.URL + '?format=claude_code')
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.headers['Content-Type'].startswith('text/plain'))
        # Exactly one charset parameter — Flask appends its own.
        self.assertEqual(1, r.headers['Content-Type'].count('charset'))
        text = r.data.decode()
        self.assertIn('claude mcp add --transport http bankbridge', text)
        self.assertIn(f'Bearer {TOKEN}', text)

    def test_the_claude_code_format_is_not_an_attachment(self):
        r = self.client.get(self.URL + '?format=claude_code')
        self.assertNotIn('Content-Disposition', r.headers)

    def test_an_unknown_format_falls_back_to_json(self):
        r = self.client.get(self.URL + '?format=nonsense')
        self.assertEqual('application/json', r.headers['Content-Type'])


class ConfigEndpointDisabledTest(WidgetBase):
    ENABLED = False
    URL = '/admin/mcp/claude_desktop_config.json'

    def test_it_404s_like_the_mcp_endpoint_itself(self):
        """With no token the feature does not exist, so there is no config."""
        self.assertEqual(404, self.client.get(self.URL).status_code)

    def test_the_claude_code_format_404s_too(self):
        self.assertEqual(
            404, self.client.get(self.URL + '?format=claude_code').status_code)

    def test_it_leaks_nothing(self):
        body = self.client.get(self.URL).data.decode()
        self.assertNotIn('mcpServers', body)
        self.assertNotIn('Bearer', body)


# The current-version pin lives in tests/test_version.py, not here.


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
