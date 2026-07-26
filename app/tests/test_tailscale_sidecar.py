# SPDX-License-Identifier: MIT
"""v0.7.1 — the Tailscale sidecar that makes the public URL one click.

Nothing here touches a real network or a real socket: `_health` and
`_localapi_get` are the two seams where the sidecar is reached, and every test
stubs one or both. That keeps the suite hermetic on a developer box that may or
may not be running Tailscale itself.

Covers the four field states the release exists to handle —

    sidecar absent | present-but-unauthenticated | authenticated-no-funnel |
    funnel-active

— plus the serve config writer (the actual mechanism that turns Funnel on), the
FQDN reader, the status cache, the admin routes, and the four MCP tools with
their kill switches.

    cd app
    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import socket
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, crypto, db  # noqa: E402
from app import funnel, mcp_settings, plaid_settings  # noqa: E402
from app import tailscale_sidecar as ts  # noqa: E402
from app.models import AuditEvent  # noqa: E402

HOST = 'fafo-bank-bridge.tail1234.ts.net'
CALLBACK = f'https://{HOST}/bankbridge/plaid/oauth_return'
OAUTH_PATH = '/bankbridge/plaid/oauth_return'

#: A minimal `tailscale status --json` payload.
LOCALAPI_STATUS = {'CertDomains': [HOST], 'BackendState': 'Running',
                   'Self': {'DNSName': f'{HOST}.'}}


class SidecarBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self.data_dir = tempfile.mkdtemp()
        self.config_dir = tempfile.mkdtemp()
        self.serve_config = os.path.join(self.config_dir, 'serve.json')
        self.app = create_app({
            'TESTING': True,
            'SCHEDULER_ENABLED': False,
            'FERNET_KEY': '',
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self.data_dir,
            'TAILSCALE_FUNNEL_HOSTNAME': '',
            'TAILSCALE_SIDECAR_ENABLED': True,
            'TAILSCALE_SERVE_CONFIG': self.serve_config,
            'TAILSCALE_SOCKET': os.path.join(self.config_dir, 'ts.sock'),
            'TAILSCALE_LOCAL_ADDR_PORT': '127.0.0.1:41414',
        })
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._env_patch = mock.patch.dict(
            os.environ, {'TAILSCALE_FUNNEL_HOSTNAME': ''})
        self._env_patch.start()
        plaid_settings._LOGGED_URL_MIGRATIONS.clear()
        ts.reset_cache()

    def tearDown(self):
        self._env_patch.stop()
        ts.reset_cache()
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)
        shutil.rmtree(self.data_dir, ignore_errors=True)
        shutil.rmtree(self.config_dir, ignore_errors=True)

    # ── the four field states, as context managers ───────────────────────────
    def absent(self):
        return self._stub((False, False), None)

    def unauthenticated(self):
        return self._stub((True, False), None)

    def authenticated(self, localapi=LOCALAPI_STATUS):
        return self._stub((True, True), localapi)

    def _stub(self, health, localapi):
        ts.reset_cache()
        return _Stubs(
            mock.patch.object(ts, '_health', return_value=health),
            mock.patch.object(ts, '_localapi_get', return_value=localapi))

    def write_funnel_on(self):
        """Put the sidecar into the funnel-active state on disk."""
        ts.write_serve_config(5202, OAUTH_PATH, funnel=True)


class _Stubs:
    """Apply several patches as one context manager."""

    def __init__(self, *patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        ts.reset_cache()
        return False


# ── the serve config: the mechanism that turns Funnel on ─────────────────────
class ServeConfigTest(SidecarBase):
    def test_it_has_the_ipn_serveconfig_shape(self):
        cfg = ts.build_serve_config(5202, OAUTH_PATH, funnel=True)
        self.assertEqual({'443': {'HTTPS': True}}, cfg['TCP'])
        self.assertIn('${TS_CERT_DOMAIN}:443', cfg['Web'])
        self.assertIn('${TS_CERT_DOMAIN}:443', cfg['AllowFunnel'])

    def test_it_uses_the_cert_domain_placeholder_not_a_baked_hostname(self):
        """containerboot substitutes ${TS_CERT_DOMAIN}, which keeps the file
        correct on any tailnet — an operator can move the data volume."""
        raw = json.dumps(ts.build_serve_config(5202, OAUTH_PATH, funnel=True))
        self.assertIn('${TS_CERT_DOMAIN}', raw)
        self.assertNotIn('tail1234', raw)

    def test_it_proxies_to_our_own_localhost(self):
        """The whole point of the sidecar: Funnel only proxies to its own
        localhost, and sharing the namespace makes that our gunicorn."""
        cfg = ts.build_serve_config(5202, OAUTH_PATH, funnel=True)
        handler = cfg['Web']['${TS_CERT_DOMAIN}:443']['Handlers'][OAUTH_PATH]
        self.assertEqual('http://127.0.0.1:5202', handler['Proxy'])

    def test_it_publishes_only_the_callback_path(self):
        """A '/' handler would publish /admin and the four unauthenticated Plaid
        write endpoints to the Internet."""
        cfg = ts.build_serve_config(5202, OAUTH_PATH, funnel=True)
        handlers = cfg['Web']['${TS_CERT_DOMAIN}:443']['Handlers']
        self.assertEqual([OAUTH_PATH], list(handlers))

    def test_allow_funnel_false_is_a_tailnet_only_serve(self):
        cfg = ts.build_serve_config(5202, OAUTH_PATH, funnel=False)
        self.assertFalse(cfg['AllowFunnel']['${TS_CERT_DOMAIN}:443'])
        # The handler survives, so the app is still reachable over the tailnet.
        self.assertIn(OAUTH_PATH, cfg['Web']['${TS_CERT_DOMAIN}:443']['Handlers'])

    def test_write_then_read_round_trips(self):
        written = ts.write_serve_config(5202, OAUTH_PATH, funnel=True)
        self.assertEqual(written, ts.read_serve_config())

    def test_write_creates_the_parent_directory(self):
        nested = os.path.join(self.config_dir, 'deep', 'nested', 'serve.json')
        self.app.config['TAILSCALE_SERVE_CONFIG'] = nested
        ts.write_serve_config(5202, OAUTH_PATH, funnel=True)
        self.assertTrue(os.path.exists(nested))

    def test_write_leaves_no_temp_file_behind(self):
        """The file is WATCHED by containerboot, so the write is atomic via
        os.replace — a half-written document would be applied or rejected."""
        ts.write_serve_config(5202, OAUTH_PATH, funnel=True)
        self.assertEqual(['serve.json'], os.listdir(self.config_dir))

    def test_write_is_idempotent_byte_for_byte(self):
        ts.write_serve_config(5202, OAUTH_PATH, funnel=True)
        with open(self.serve_config) as fh:
            first = fh.read()
        ts.write_serve_config(5202, OAUTH_PATH, funnel=True)
        with open(self.serve_config) as fh:
            self.assertEqual(first, fh.read())

    def test_read_of_a_missing_file_is_none(self):
        self.assertIsNone(ts.read_serve_config())

    def test_read_of_a_corrupt_file_is_none_not_a_crash(self):
        with open(self.serve_config, 'w') as fh:
            fh.write('{not json')
        self.assertIsNone(ts.read_serve_config())

    def test_funnel_requested_reads_allow_funnel(self):
        self.assertFalse(ts._funnel_requested(None))
        self.assertFalse(ts._funnel_requested({}))
        self.assertFalse(ts._funnel_requested(
            ts.build_serve_config(5202, OAUTH_PATH, funnel=False)))
        self.assertTrue(ts._funnel_requested(
            ts.build_serve_config(5202, OAUTH_PATH, funnel=True)))

    def test_writing_invalidates_the_status_cache(self):
        with self.authenticated():
            self.assertFalse(ts.status()['funnel_active'])
            ts.write_serve_config(5202, OAUTH_PATH, funnel=True)
            self.assertTrue(ts.status()['funnel_active'])


# ── /healthz: absent vs unauthenticated vs up ────────────────────────────────
class HealthTest(SidecarBase):
    def _urlopen(self, side_effect):
        return mock.patch.object(ts.urllib.request, 'urlopen',
                                 side_effect=side_effect)

    def test_200_is_present_and_authenticated(self):
        with self._urlopen([_FakeResponse(200)]):
            self.assertEqual((True, True), ts._health())

    def test_503_is_present_but_not_authenticated(self):
        """containerboot up, no tailnet IP — the missing-TS_AUTHKEY state."""
        err = ts.urllib.error.HTTPError(
            'http://x/healthz', 503, 'Service Unavailable', {}, None)
        with self._urlopen([err]):
            self.assertEqual((True, False), ts._health())

    def test_connection_refused_is_absent(self):
        with self._urlopen([ts.urllib.error.URLError('refused')]):
            self.assertEqual((False, False), ts._health())

    def test_os_error_is_absent(self):
        with self._urlopen([OSError('boom')]):
            self.assertEqual((False, False), ts._health())

    def test_it_requests_the_configured_health_address(self):
        with self._urlopen([_FakeResponse(200)]) as urlopen:
            ts._health()
        self.assertEqual('http://127.0.0.1:41414/healthz',
                         urlopen.call_args[0][0])

    def test_the_health_probe_is_bounded(self):
        with self._urlopen([_FakeResponse(200)]) as urlopen:
            ts._health()
        self.assertEqual(ts.TIMEOUT_SECONDS, urlopen.call_args[1]['timeout'])
        self.assertLessEqual(ts.TIMEOUT_SECONDS, 5)


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── the FQDN read (LocalAPI) ─────────────────────────────────────────────────
class FqdnTest(SidecarBase):
    def test_cert_domains_is_preferred(self):
        self.assertEqual(HOST, ts._fqdn_from_status(
            {'CertDomains': [HOST], 'Self': {'DNSName': 'other.example.'}}))

    def test_self_dnsname_is_the_fallback_with_its_dot_stripped(self):
        self.assertEqual(HOST, ts._fqdn_from_status(
            {'Self': {'DNSName': f'{HOST}.'}}))

    def test_it_lowercases(self):
        self.assertEqual(HOST, ts._fqdn_from_status(
            {'CertDomains': [HOST.upper()]}))

    def test_empty_and_malformed_payloads_yield_nothing(self):
        for payload in ({}, {'CertDomains': []}, {'CertDomains': None},
                        {'CertDomains': ['']}, {'Self': None},
                        {'Self': {}}, {'Self': {'DNSName': ''}},
                        {'CertDomains': 'notalist', 'Self': 'notadict'}):
            self.assertEqual('', ts._fqdn_from_status(payload), repr(payload))

    def test_localapi_failure_is_swallowed(self):
        """The LocalAPI is undocumented upstream, so a read is enrichment only —
        it must never raise into a page render."""
        with mock.patch.object(ts, '_UnixHTTPConnection',
                               side_effect=OSError('no socket')):
            self.assertIsNone(ts._localapi_get('/localapi/v0/status'))

    def test_localapi_non_200_is_none(self):
        conn = mock.Mock()
        conn.getresponse.return_value = mock.Mock(status=403)
        with mock.patch.object(ts, '_UnixHTTPConnection', return_value=conn):
            self.assertIsNone(ts._localapi_get('/localapi/v0/status'))

    def test_localapi_sends_the_canonical_host_and_csrf_headers(self):
        resp = mock.Mock(status=200)
        resp.read.return_value = json.dumps(LOCALAPI_STATUS).encode()
        conn = mock.Mock()
        conn.getresponse.return_value = resp
        with mock.patch.object(ts, '_UnixHTTPConnection', return_value=conn):
            self.assertEqual(LOCALAPI_STATUS,
                             ts._localapi_get('/localapi/v0/status'))
        headers = conn.request.call_args[1]['headers']
        self.assertEqual('local-tailscaled.sock', headers['Host'])
        self.assertEqual('localapi', headers['Sec-Tailscale'])

    def test_the_unix_connection_targets_the_configured_socket_path(self):
        conn = ts._UnixHTTPConnection('/tmp/nope.sock', 1.0)
        self.assertEqual('/tmp/nope.sock', conn._path)
        self.assertEqual('local-tailscaled.sock', conn.host)
        with self.assertRaises(OSError):
            conn.connect()  # nothing is listening — must raise, not hang

    def test_the_unix_connection_really_speaks_to_a_unix_socket(self):
        """One end-to-end pass over a real AF_UNIX socket, so the transport is
        proven rather than assumed."""
        path = os.path.join(self.config_dir, 'live.sock')
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        self.addCleanup(server.close)
        body = json.dumps(LOCALAPI_STATUS).encode()

        import threading

        def serve():
            conn, _ = server.accept()
            conn.recv(4096)
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: '
                         + str(len(body)).encode() + b'\r\n\r\n' + body)
            conn.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        self.app.config['TAILSCALE_SOCKET'] = path
        self.assertEqual(LOCALAPI_STATUS, ts._localapi_get('/localapi/v0/status'))
        t.join(timeout=5)


# ── composed status + caching ────────────────────────────────────────────────
class StatusTest(SidecarBase):
    def test_absent(self):
        with self.absent():
            s = ts.status()
        self.assertFalse(s['present'])
        self.assertFalse(s['authenticated'])
        self.assertEqual('', s['hostname'])

    def test_unauthenticated_reports_present(self):
        with self.unauthenticated():
            s = ts.status()
        self.assertTrue(s['present'])
        self.assertFalse(s['authenticated'])
        self.assertEqual('', s['hostname'])

    def test_authenticated_reports_the_hostname_and_backend_state(self):
        with self.authenticated():
            s = ts.status()
        self.assertTrue(s['present'])
        self.assertTrue(s['authenticated'])
        self.assertEqual(HOST, s['hostname'])
        self.assertEqual('Running', s['backend_state'])
        self.assertTrue(s['localapi_ok'])

    def test_an_unauthenticated_node_is_still_queried_for_its_auth_url(self):
        """Verified against tailscale/tailscale:v1.90.8 with an empty
        TS_AUTHKEY: an unauthenticated daemon reports BackendState=NeedsLogin
        plus an AuthURL, which is a one-click login the operator can use instead
        of minting a key. Skipping the read would throw that away."""
        with mock.patch.object(ts, '_health', return_value=(True, False)), \
                mock.patch.object(ts, '_localapi_get', return_value={
                    'BackendState': 'NeedsLogin',
                    'AuthURL': 'https://login.tailscale.com/a/abc123',
                    'CertDomains': None,
                    'Self': {'DNSName': ''}}) as localapi:
            s = ts.status()
        localapi.assert_called_once_with('/localapi/v0/status')
        self.assertEqual('https://login.tailscale.com/a/abc123', s['auth_url'])
        self.assertEqual('NeedsLogin', s['backend_state'])
        # …and it still has no usable hostname, exactly as the real daemon says.
        self.assertEqual('', s['hostname'])
        self.assertFalse(s['authenticated'])

    def test_an_authenticated_node_reports_no_auth_url(self):
        with self.authenticated():
            self.assertEqual('', ts.status()['auth_url'])

    def test_an_absent_sidecar_is_not_asked_for_its_fqdn(self):
        with mock.patch.object(ts, '_health', return_value=(False, False)), \
                mock.patch.object(ts, '_localapi_get') as localapi:
            ts.status()
        localapi.assert_not_called()

    def test_authenticated_without_localapi_still_reports_present(self):
        with self.authenticated(localapi=None):
            s = ts.status()
        self.assertTrue(s['authenticated'])
        self.assertFalse(s['localapi_ok'])
        self.assertEqual('', s['hostname'])

    def test_the_status_is_cached(self):
        with mock.patch.object(ts, '_health',
                               return_value=(True, True)) as health, \
                mock.patch.object(ts, '_localapi_get',
                                  return_value=LOCALAPI_STATUS):
            ts.status()
            ts.status()
            ts.status()
        self.assertEqual(1, health.call_count)

    def test_force_bypasses_the_cache(self):
        with mock.patch.object(ts, '_health',
                               return_value=(True, True)) as health, \
                mock.patch.object(ts, '_localapi_get',
                                  return_value=LOCALAPI_STATUS):
            ts.status()
            ts.status(force=True)
        self.assertEqual(2, health.call_count)

    def test_reset_cache_forces_a_re_read(self):
        with mock.patch.object(ts, '_health',
                               return_value=(True, True)) as health, \
                mock.patch.object(ts, '_localapi_get',
                                  return_value=LOCALAPI_STATUS):
            ts.status()
            ts.reset_cache()
            ts.status()
        self.assertEqual(2, health.call_count)

    def test_an_expired_cache_is_re_read(self):
        with mock.patch.object(ts, '_health',
                               return_value=(True, True)) as health, \
                mock.patch.object(ts, '_localapi_get',
                                  return_value=LOCALAPI_STATUS):
            ts.status()
            ts._cache['at'] -= (ts.CACHE_TTL_SECONDS + 1)
            ts.status()
        self.assertEqual(2, health.call_count)

    def test_a_caller_cannot_mutate_the_cached_dict(self):
        with self.authenticated():
            first = ts.status()
            first['hostname'] = 'tampered'
            self.assertEqual(HOST, ts.status()['hostname'])

    def test_the_master_switch_short_circuits_everything(self):
        self.app.config['TAILSCALE_SIDECAR_ENABLED'] = False
        with mock.patch.object(ts, '_health') as health:
            s = ts.status()
        health.assert_not_called()
        self.assertFalse(s['enabled'])
        self.assertFalse(s['present'])


# ── the five wizard modes ────────────────────────────────────────────────────
class ModeTest(SidecarBase):
    def test_absent_is_none(self):
        with self.absent():
            self.assertEqual('none', funnel.detect()['mode'])

    def test_absent_with_an_env_hostname_is_manual(self):
        self.app.config['TAILSCALE_FUNNEL_HOSTNAME'] = HOST
        with self.absent():
            d = funnel.detect()
        self.assertEqual('manual', d['mode'])
        self.assertEqual('env', d['source'])

    def test_unauthenticated_is_sidecar_unauth(self):
        with self.unauthenticated():
            d = funnel.detect()
        self.assertEqual('sidecar_unauth', d['mode'])
        self.assertEqual('unconfigured', d['state'])

    def test_authenticated_without_funnel_is_sidecar_ready(self):
        with self.authenticated():
            d = funnel.detect()
        self.assertEqual('sidecar_ready', d['mode'])
        # The hostname is already known, so `state` is configured — the URL just
        # isn't public yet.
        self.assertEqual('configured', d['state'])
        self.assertEqual(HOST, d['hostname'])
        self.assertEqual('sidecar', d['source'])

    def test_authenticated_with_funnel_is_sidecar_funnel(self):
        self.write_funnel_on()
        with self.authenticated():
            d = funnel.detect()
        self.assertEqual('sidecar_funnel', d['mode'])
        self.assertEqual(CALLBACK, d['redirect_uri'])

    def test_funnel_on_but_hostname_unknown_degrades_to_ready(self):
        """Rather than offering a URL we would have to leave blank."""
        self.write_funnel_on()
        with self.authenticated(localapi=None):
            d = funnel.detect()
        self.assertEqual('sidecar_ready', d['mode'])
        self.assertTrue(d['sidecar']['funnel_active'])

    def test_the_sidecar_outranks_env_and_saved(self):
        self.app.config['TAILSCALE_FUNNEL_HOSTNAME'] = 'env.tailaaaa.ts.net'
        plaid_settings.save_public_url(funnel_hostname='saved.tailbbbb.ts.net')
        with self.authenticated():
            d = funnel.detect()
        self.assertEqual(HOST, d['hostname'])
        self.assertEqual('sidecar', d['source'])

    def test_env_still_wins_when_the_sidecar_cannot_name_itself(self):
        self.app.config['TAILSCALE_FUNNEL_HOSTNAME'] = 'env.tailaaaa.ts.net'
        with self.authenticated(localapi=None):
            d = funnel.detect()
        self.assertEqual('env.tailaaaa.ts.net', d['hostname'])
        self.assertEqual('env', d['source'])

    def test_a_sidecar_hostname_is_normalized(self):
        with self.authenticated(localapi={'CertDomains': [f'{HOST.upper()}.']}):
            self.assertEqual(HOST, funnel.detect()['hostname'])

    def test_a_junk_sidecar_hostname_does_not_become_a_url(self):
        with self.authenticated(localapi={'CertDomains': ['not a host']}):
            d = funnel.detect()
        self.assertEqual('', d['sidecar_hostname'])
        self.assertEqual('sidecar_ready', d['mode'])

    def test_detect_reuses_a_handed_in_status_without_re_reading(self):
        handed = {'present': True, 'authenticated': True, 'hostname': HOST,
                  'funnel_active': True, 'localapi_ok': True,
                  'backend_state': 'Running', 'serve_config_present': True,
                  'enabled': True}
        with mock.patch.object(ts, '_health') as health:
            d = funnel.detect(sidecar_status=handed)
        health.assert_not_called()
        self.assertEqual('sidecar_funnel', d['mode'])


# ── enable / disable ─────────────────────────────────────────────────────────
class EnableDisableTest(SidecarBase):
    def test_enable_writes_the_config_and_saves_the_uri(self):
        with self.authenticated():
            r = funnel.enable_public_url()
        self.assertTrue(r['ok'])
        self.assertTrue(r['saved'])
        self.assertEqual(CALLBACK, r['url'])
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])
        self.assertTrue(ts._funnel_requested(ts.read_serve_config()))

    def test_enable_is_idempotent(self):
        with self.authenticated():
            funnel.enable_public_url()
            r = funnel.enable_public_url()
        self.assertTrue(r['ok'])
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])

    def test_enable_without_a_sidecar_fails_cleanly(self):
        with self.absent():
            r = funnel.enable_public_url()
        self.assertFalse(r['ok'])
        self.assertIn('No Tailscale sidecar', r['detail'])
        self.assertIsNone(ts.read_serve_config())

    def test_enable_unauthenticated_says_to_set_the_authkey(self):
        with self.unauthenticated():
            r = funnel.enable_public_url()
        self.assertFalse(r['ok'])
        self.assertIn('TS_AUTHKEY', r['detail'])
        self.assertIsNone(ts.read_serve_config())

    def test_enable_with_no_known_hostname_is_a_partial_success(self):
        """The Funnel really is on, so reporting failure would be wrong and
        would tempt the operator into enabling it twice."""
        with self.authenticated(localapi=None):
            r = funnel.enable_public_url()
        self.assertTrue(r['ok'])
        self.assertIsNone(r['url'])
        self.assertFalse(r['saved'])
        self.assertIn('Refresh', r['detail'])
        self.assertTrue(ts._funnel_requested(ts.read_serve_config()))

    def test_disable_turns_funnel_off_but_keeps_the_handler(self):
        with self.authenticated():
            funnel.enable_public_url()
            r = funnel.disable_public_url()
        self.assertTrue(r['ok'])
        cfg = ts.read_serve_config()
        self.assertFalse(ts._funnel_requested(cfg))
        self.assertIn(OAUTH_PATH, cfg['Web']['${TS_CERT_DOMAIN}:443']['Handlers'])

    def test_disable_leaves_the_saved_redirect_uri_alone(self):
        """It is still what the Plaid dashboard has registered — clearing it
        would turn one reversible click into a two-place re-registration."""
        with self.authenticated():
            funnel.enable_public_url()
            self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])
            funnel.disable_public_url()
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])

    def test_disable_without_a_sidecar_fails_cleanly(self):
        with self.absent():
            r = funnel.disable_public_url()
        self.assertFalse(r['ok'])

    def test_enable_then_disable_then_enable_round_trips(self):
        with self.authenticated():
            funnel.enable_public_url()
            funnel.disable_public_url()
            self.assertEqual('sidecar_ready', funnel.detect()['mode'])
            funnel.enable_public_url()
            self.assertEqual('sidecar_funnel', funnel.detect()['mode'])


# ── admin UI ─────────────────────────────────────────────────────────────────
class AdminUiTest(SidecarBase):
    def test_unauth_state_asks_for_an_authkey(self):
        with self.unauthenticated():
            r = self.client.get('/admin/plaid_settings')
        self.assertEqual(200, r.status_code)
        body = r.data.decode()
        self.assertIn('needs authentication', body)
        self.assertIn('TS_AUTHKEY', body)
        self.assertIn('login.tailscale.com/admin/settings/keys', body)
        self.assertNotIn('Enable Public URL', body)

    def test_unauth_state_offers_the_one_click_login_link_when_known(self):
        auth_url = 'https://login.tailscale.com/a/1e37f6a201b1ab'
        with self._stub((True, False), {'BackendState': 'NeedsLogin',
                                        'AuthURL': auth_url}):
            body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn(f'href="{auth_url}"', body)
        self.assertIn('Log in from your browser'.lower(),
                      body.lower().replace('log in from your\n    browser',
                                           'log in from your browser'))
        self.assertIn('BackendState: NeedsLogin', body)
        # The authkey route is still offered — the link is single-use.
        self.assertIn('TS_AUTHKEY', body)

    def test_unauth_state_without_a_login_link_still_renders(self):
        with self.unauthenticated():
            body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn('Set an auth key', body)
        self.assertNotIn('login.tailscale.com/a/', body)

    def test_the_login_link_opens_safely(self):
        """It is an external link on an admin page; noopener is the minimum."""
        with self._stub((True, False),
                        {'AuthURL': 'https://login.tailscale.com/a/x'}):
            body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn('rel="noopener noreferrer"', body)

    def test_ready_state_offers_one_button(self):
        with self.authenticated():
            r = self.client.get('/admin/plaid_settings')
        body = r.data.decode()
        self.assertIn('Enable Public URL', body)
        self.assertIn('action="/admin/plaid_settings/funnel/enable"', body)
        self.assertNotIn('needs authentication', body)

    def test_funnel_state_shows_the_url_and_a_disable_button(self):
        self.write_funnel_on()
        with self.authenticated():
            r = self.client.get('/admin/plaid_settings')
        body = r.data.decode()
        self.assertIn(CALLBACK, body)
        self.assertIn('served by the Tailscale sidecar', body)
        self.assertIn('Disable Funnel', body)
        self.assertIn('action="/admin/plaid_settings/funnel/disable"', body)

    def test_the_manual_path_has_no_disable_button(self):
        """There is no sidecar to disable — the button would 404 the operator's
        expectations."""
        self.app.config['TAILSCALE_FUNNEL_HOSTNAME'] = HOST
        with self.absent():
            body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn(CALLBACK, body)
        self.assertNotIn('Disable Funnel', body)

    def test_enable_via_the_button_saves_and_flashes(self):
        with self.authenticated():
            r = self.client.post('/admin/plaid_settings/funnel/enable')
        self.assertEqual(200, r.status_code)
        self.assertIn('Public URL enabled', r.data.decode())
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])

    def test_enable_via_the_button_renders_the_post_write_state(self):
        """Not the 30s-stale cached one — the operator must see it took."""
        with self.authenticated():
            r = self.client.post('/admin/plaid_settings/funnel/enable')
        self.assertIn('Disable Funnel', r.data.decode())

    def test_enable_records_an_audit_event(self):
        with self.authenticated():
            self.client.post('/admin/plaid_settings/funnel/enable')
        ev = AuditEvent.query.filter_by(
            event_type='plaid_public_url_saved').one()
        self.assertIn(CALLBACK, ev.payload_after)
        self.assertIn('tailscale_sidecar', ev.payload_after)

    def test_disable_via_the_button_flashes_and_audits(self):
        self.write_funnel_on()
        with self.authenticated():
            r = self.client.post('/admin/plaid_settings/funnel/disable')
        self.assertEqual(200, r.status_code)
        self.assertIn('no longer served', r.data.decode())
        self.assertEqual(1, AuditEvent.query.filter_by(
            event_type='plaid_public_url_disabled').count())

    def test_enable_without_a_sidecar_reports_it_on_the_page(self):
        with self.absent():
            r = self.client.post('/admin/plaid_settings/funnel/enable')
        self.assertEqual(200, r.status_code)
        self.assertIn('No Tailscale sidecar', r.data.decode())
        self.assertEqual(0, AuditEvent.query.filter_by(
            event_type='plaid_public_url_saved').count())

    def test_every_mode_renders_200(self):
        for label, stub in (('absent', self.absent),
                            ('unauth', self.unauthenticated),
                            ('ready', self.authenticated)):
            with stub():
                r = self.client.get('/admin/plaid_settings')
            self.assertEqual(200, r.status_code, label)

    def test_the_manual_entry_form_is_reachable_in_every_sidecar_mode(self):
        for stub in (self.unauthenticated, self.authenticated):
            with stub():
                body = self.client.get('/admin/plaid_settings').data.decode()
            self.assertIn('<input name="hostname"', body)
            self.assertNotIn('&lt;form', body)


# ── MCP tools ────────────────────────────────────────────────────────────────
class McpToolTest(SidecarBase):
    """Driven over the real /mcp endpoint, like test_mcp_server.py — so these
    exercise the token gate, the JSON-RPC envelope and the request context the
    AiActionLog write needs, not just the handler functions."""

    TOKEN = 'test-mcp-token'

    def setUp(self):
        super().setUp()
        self._token_patch = mock.patch.dict(
            os.environ, {'BB_MCP_AUTH_TOKEN': self.TOKEN})
        self._token_patch.start()

    def tearDown(self):
        self._token_patch.stop()
        super().tearDown()

    def _call(self, name, args=None):
        resp = self.client.post(
            '/mcp',
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                  'params': {'name': name, 'arguments': args or {}}},
            headers={'Authorization': f'Bearer {self.TOKEN}'})
        self.assertEqual(200, resp.status_code)
        return resp.get_json()['result']

    def _payload(self, result):
        return json.loads(result['content'][0]['text'])

    def test_the_four_tools_are_advertised(self):
        resp = self.client.post(
            '/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
            headers={'Authorization': f'Bearer {self.TOKEN}'})
        names = {t['name'] for t in resp.get_json()['result']['tools']}
        for n in ('get_public_url_status', 'test_public_url',
                  'enable_public_url', 'disable_public_url'):
            self.assertIn(n, names)

    def test_the_mutating_pair_is_declared_mutating(self):
        from app.blueprints.mcp_server import TOOLS
        self.assertTrue(TOOLS['enable_public_url']['mutating'])
        self.assertTrue(TOOLS['disable_public_url']['mutating'])
        self.assertFalse(TOOLS['get_public_url_status']['mutating'])
        self.assertFalse(TOOLS['test_public_url']['mutating'])

    def test_status_tool_is_read_only_and_needs_no_switch(self):
        with self.authenticated():
            r = self._call('get_public_url_status')
        self.assertFalse(r['isError'])
        d = self._payload(r)
        self.assertEqual('sidecar_ready', d['mode'])
        self.assertTrue(d['sidecar_authenticated'])
        self.assertEqual(HOST, d['hostname'])
        self.assertFalse(d['funnel_active'])

    def test_status_tool_relays_the_login_link(self):
        auth_url = 'https://login.tailscale.com/a/abc'
        with self._stub((True, False), {'BackendState': 'NeedsLogin',
                                        'AuthURL': auth_url}):
            d = self._payload(self._call('get_public_url_status'))
        self.assertEqual('sidecar_unauth', d['mode'])
        self.assertEqual(auth_url, d['auth_url'])
        self.assertEqual('NeedsLogin', d['backend_state'])

    def test_status_tool_reports_no_login_link_when_authenticated(self):
        with self.authenticated():
            self.assertIsNone(
                self._payload(self._call('get_public_url_status'))['auth_url'])

    def test_status_tool_reports_every_mode(self):
        with self.absent():
            self.assertEqual('none',
                             self._payload(self._call('get_public_url_status'))['mode'])
        with self.unauthenticated():
            self.assertEqual('sidecar_unauth',
                             self._payload(self._call('get_public_url_status'))['mode'])
        self.write_funnel_on()
        with self.authenticated():
            self.assertEqual('sidecar_funnel',
                             self._payload(self._call('get_public_url_status'))['mode'])

    def test_enable_is_blocked_by_default(self):
        with self.authenticated():
            r = self._call('enable_public_url')
        self.assertTrue(r['isError'])
        self.assertIn('kill switch', r['content'][0]['text'])
        self.assertIsNone(ts.read_serve_config())

    def test_disable_is_blocked_by_default(self):
        with self.authenticated():
            r = self._call('disable_public_url')
        self.assertTrue(r['isError'])
        self.assertIn('kill switch', r['content'][0]['text'])

    def test_enable_works_once_its_switch_is_on(self):
        mcp_settings.save({'enable_public_url': True})
        with self.authenticated():
            r = self._call('enable_public_url')
        self.assertFalse(r['isError'])
        d = self._payload(r)
        self.assertEqual(CALLBACK, d['url'])
        self.assertEqual(HOST, d['hostname'])
        self.assertTrue(d['saved_as_redirect_uri'])
        self.assertEqual(CALLBACK, d['register_in_plaid_dashboard'])
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])

    def test_enable_audits_with_the_mcp_actor(self):
        mcp_settings.save({'enable_public_url': True})
        with self.authenticated():
            self._call('enable_public_url')
        ev = AuditEvent.query.filter_by(
            event_type='plaid_public_url_saved').one()
        self.assertEqual('mcp', ev.actor)

    def test_enable_without_a_sidecar_is_a_tool_error(self):
        mcp_settings.save({'enable_public_url': True})
        with self.absent():
            r = self._call('enable_public_url')
        self.assertTrue(r['isError'])
        self.assertIn('No Tailscale sidecar', r['content'][0]['text'])

    def test_disable_works_once_its_switch_is_on(self):
        mcp_settings.save({'disable_public_url': True})
        self.write_funnel_on()
        with self.authenticated():
            r = self._call('disable_public_url')
        self.assertFalse(r['isError'])
        self.assertFalse(self._payload(r)['funnel_active'])
        self.assertTrue(self._payload(r)['saved_redirect_uri_unchanged'])
        self.assertFalse(ts._funnel_requested(ts.read_serve_config()))

    def test_test_public_url_probes_and_is_read_only(self):
        self.write_funnel_on()
        with self.authenticated(), mock.patch.object(
                funnel, 'probe', return_value={
                    'ok': True, 'reachable': True, 'status': 200,
                    'url': CALLBACK, 'location': '',
                    'detail': 'HTTP 200 — reachable.'}) as probe:
            r = self._call('test_public_url')
        self.assertFalse(r['isError'])
        probe.assert_called_once_with(HOST)
        d = self._payload(r)
        self.assertTrue(d['ok'])
        self.assertEqual(CALLBACK, d['url'])

    def test_test_public_url_errors_when_nothing_is_configured(self):
        with self.absent():
            r = self._call('test_public_url')
        self.assertTrue(r['isError'])
        self.assertIn('no public hostname', r['content'][0]['text'])

    def test_the_new_switches_default_off_and_are_listed(self):
        state = mcp_settings.load()
        self.assertFalse(state['enable_public_url'])
        self.assertFalse(state['disable_public_url'])

    def test_the_admin_mcp_page_lists_the_new_switches(self):
        r = self.client.get('/admin/mcp')
        self.assertEqual(200, r.status_code)
        body = r.data.decode()
        self.assertIn('enable_public_url', body)
        self.assertIn('disable_public_url', body)

    def test_every_call_is_written_to_the_ai_action_log(self):
        from app.models import AiActionLog
        with self.authenticated():
            self._call('get_public_url_status')
        self.assertEqual(1, AiActionLog.query.filter_by(
            tool_name='get_public_url_status', ok=True).count())


class VersionTest(unittest.TestCase):
    def test_version_is_bumped(self):
        import app as app_pkg
        self.assertEqual('0.7.1', app_pkg.__version__)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
