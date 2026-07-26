# SPDX-License-Identifier: MIT
"""v0.7.0 — the public-URL (Tailscale Funnel) setup wizard.

Five surfaces:

  * hostname normalization — every shape an operator can paste, and the junk
    that must be refused rather than turned into a broken redirect URI
  * URL construction — base URL and the exact redirect URI Plaid compares
  * detection — env var wins, persisted value is the fallback, conflict is
    reported rather than hidden
  * the reachability probe — 200 / redirect / 404 / unreachable, with the
    network stubbed (no test ever leaves the box)
  * the admin routes end-to-end — State B render, manual entry → save →
    PLAID_REDIRECT_URI updated → State A render, and idempotency

    cd app
    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, crypto, db  # noqa: E402
from app import funnel, plaid_settings  # noqa: E402
from app.models import AuditEvent  # noqa: E402

HOST = 'umbrel.tail1234.ts.net'
CALLBACK = f'https://{HOST}/bankbridge/plaid/oauth_return'


class FunnelBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self.data_dir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SCHEDULER_ENABLED': False,
            'FERNET_KEY': '',
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self.data_dir,
            # Explicit blank so a real TAILSCALE_FUNNEL_HOSTNAME in the
            # developer's environment can't leak into these assertions.
            'TAILSCALE_FUNNEL_HOSTNAME': '',
        })
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        # funnel.env_hostname() falls back to os.environ when config is blank.
        self._env_patch = mock.patch.dict(
            os.environ, {'TAILSCALE_FUNNEL_HOSTNAME': ''})
        self._env_patch.start()
        plaid_settings._LOGGED_URL_MIGRATIONS.clear()

    def tearDown(self):
        self._env_patch.stop()
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _settings_file(self) -> dict:
        with open(os.path.join(self.data_dir, 'plaid_settings.json')) as fh:
            return json.load(fh)

    def _set_env_hostname(self, value):
        """Point the env var at `value` for the rest of the test."""
        self.app.config['TAILSCALE_FUNNEL_HOSTNAME'] = value


# ── hostname normalization ───────────────────────────────────────────────────
class NormalizeHostnameTest(unittest.TestCase):
    def test_bare_hostname_passes_through(self):
        self.assertEqual(HOST, funnel.normalize_hostname(HOST))

    def test_every_pasteable_shape_normalizes_to_the_same_host(self):
        for raw in (HOST,
                    f'  {HOST}  ',
                    f'https://{HOST}',
                    f'https://{HOST}/',
                    f'http://{HOST}',
                    f'https://{HOST}:443',
                    f'HTTPS://{HOST.upper()}:443/',
                    f'{HOST}.',
                    f'https://{HOST}/bankbridge/plaid/oauth_return',
                    f'{HOST}/bankbridge/plaid/oauth_return',
                    f'https://{HOST}/?oauth_state_id=abc#frag'):
            self.assertEqual(HOST, funnel.normalize_hostname(raw), raw)

    def test_port_443_is_dropped_so_the_string_matches_plaid(self):
        """`host` and `host:443` are the same endpoint but two different
        strings, and Plaid compares the redirect URI exactly."""
        self.assertEqual(CALLBACK, funnel.redirect_uri_for(f'https://{HOST}:443'))

    def test_a_non_default_port_is_kept(self):
        self.assertEqual('h.example:8443',
                         funnel.normalize_hostname('https://h.example:8443/x'))

    def test_userinfo_is_stripped(self):
        self.assertEqual('h.example',
                         funnel.normalize_hostname('https://user:pw@h.example/'))

    def test_junk_is_refused_rather_than_half_accepted(self):
        for raw in (None, '', '   ', 'umbrel', 'localhost', '.', '..',
                    'h..example', 'a b.c', 'h.example:notaport',
                    'h.example:0', 'h.example:99999', 'https://', '-h.example',
                    'h.example-', 'h.exa_mple'):
            self.assertEqual('', funnel.normalize_hostname(raw), repr(raw))

    def test_single_label_is_refused_because_it_cannot_resolve_publicly(self):
        self.assertEqual('', funnel.normalize_hostname('umbrel'))
        self.assertEqual('', funnel.normalize_hostname('http://localhost:5202'))

    def test_an_overlong_hostname_is_refused(self):
        self.assertEqual('', funnel.normalize_hostname(
            '.'.join(['a' * 60] * 5) + '.ts.net'))


# ── URL construction ─────────────────────────────────────────────────────────
class UrlConstructionTest(unittest.TestCase):
    def test_base_url_is_https_with_no_trailing_slash(self):
        self.assertEqual(f'https://{HOST}', funnel.base_url(HOST))

    def test_redirect_uri_appends_the_prefixed_callback_path(self):
        self.assertEqual(CALLBACK, funnel.redirect_uri_for(HOST))

    def test_callback_path_carries_the_bankbridge_prefix(self):
        """The multi-app path prefix convention is load-bearing here — a
        prefix-less callback would collide with the next Umbrel app on the same
        Funnel hostname."""
        self.assertEqual('/bankbridge/plaid/oauth_return',
                         funnel.OAUTH_RETURN_PATH)

    def test_an_invalid_hostname_yields_empty_urls_not_half_urls(self):
        for bad in ('', 'umbrel', 'a b.c', None):
            self.assertEqual('', funnel.base_url(bad), repr(bad))
            self.assertEqual('', funnel.redirect_uri_for(bad), repr(bad))

    def test_the_derived_path_is_a_real_route(self):
        fd, path = tempfile.mkstemp(suffix='.sqlite')
        self.addCleanup(os.remove, path)
        self.addCleanup(os.close, fd)
        self.addCleanup(crypto.reset_cache)
        app = create_app({'TESTING': True, 'SCHEDULER_ENABLED': False,
                          'FERNET_KEY': '',
                          'SQLALCHEMY_DATABASE_URI': f'sqlite:///{path}',
                          'DATA_DIR': tempfile.mkdtemp()})
        self.assertIn(funnel.OAUTH_RETURN_PATH,
                      {str(r) for r in app.url_map.iter_rules()})

    def test_the_documented_port_is_the_app_proxy_port(self):
        self.assertEqual(5202, funnel.APP_PROXY_PORT)


# ── detection ────────────────────────────────────────────────────────────────
class DetectionTest(FunnelBase):
    def test_nothing_configured_is_state_b(self):
        d = funnel.detect()
        self.assertEqual('unconfigured', d['state'])
        self.assertEqual('', d['hostname'])
        self.assertEqual('', d['source'])
        self.assertEqual('', d['redirect_uri'])

    def test_env_var_alone_is_state_a(self):
        self._set_env_hostname(HOST)
        d = funnel.detect()
        self.assertEqual('configured', d['state'])
        self.assertEqual(HOST, d['hostname'])
        self.assertEqual('env', d['source'])
        self.assertEqual(CALLBACK, d['redirect_uri'])

    def test_env_var_is_normalized(self):
        self._set_env_hostname(f'HTTPS://{HOST.upper()}/')
        self.assertEqual(HOST, funnel.detect()['hostname'])

    def test_a_junk_env_var_does_not_fake_state_a(self):
        self._set_env_hostname('not a hostname')
        self.assertEqual('unconfigured', funnel.detect()['state'])

    def test_process_environment_is_read_when_config_is_blank(self):
        with mock.patch.dict(os.environ,
                             {'TAILSCALE_FUNNEL_HOSTNAME': HOST}):
            self.assertEqual(HOST, funnel.detect()['hostname'])

    def test_persisted_hostname_alone_is_state_a(self):
        plaid_settings.save_public_url(funnel_hostname=HOST)
        d = funnel.detect()
        self.assertEqual('configured', d['state'])
        self.assertEqual('saved', d['source'])
        self.assertEqual(HOST, d['hostname'])

    def test_env_wins_over_a_stale_persisted_hostname(self):
        plaid_settings.save_public_url(funnel_hostname='old.tailaaaa.ts.net')
        self._set_env_hostname(HOST)
        d = funnel.detect()
        self.assertEqual(HOST, d['hostname'])
        self.assertEqual('env', d['source'])

    def test_a_disagreement_is_reported_not_hidden(self):
        plaid_settings.save_public_url(funnel_hostname='old.tailaaaa.ts.net')
        self._set_env_hostname(HOST)
        d = funnel.detect()
        self.assertTrue(d['conflict'])
        self.assertEqual(HOST, d['env_hostname'])
        self.assertEqual('old.tailaaaa.ts.net', d['saved_hostname'])

    def test_agreement_is_not_a_conflict(self):
        plaid_settings.save_public_url(funnel_hostname=HOST)
        self._set_env_hostname(HOST)
        self.assertFalse(funnel.detect()['conflict'])

    def test_a_hand_edited_settings_file_cannot_inject_a_hostname(self):
        plaid_settings.save_public_url(funnel_hostname=HOST)
        raw = self._settings_file()
        raw['funnel_hostname'] = 'https://evil host/../x'
        with open(os.path.join(self.data_dir, 'plaid_settings.json'), 'w') as fh:
            json.dump(raw, fh)
        d = funnel.detect()
        self.assertEqual('', d['hostname'])
        self.assertEqual('unconfigured', d['state'])

    def test_redirect_uri_matches_reports_whether_the_setting_is_current(self):
        self._set_env_hostname(HOST)
        self.assertFalse(funnel.detect()['redirect_uri_matches'])
        plaid_settings.save_public_url(redirect_uri=CALLBACK)
        self.assertTrue(funnel.detect()['redirect_uri_matches'])

    def test_detect_does_not_touch_the_network_by_default(self):
        self._set_env_hostname(HOST)
        with mock.patch.object(funnel, 'probe') as probe:
            self.assertIsNone(funnel.detect()['probe'])
            probe.assert_not_called()

    def test_detect_probes_when_asked(self):
        self._set_env_hostname(HOST)
        with mock.patch.object(funnel, 'probe',
                               return_value={'ok': True}) as probe:
            self.assertEqual({'ok': True}, funnel.detect(probe_url=True)['probe'])
            probe.assert_called_once_with(HOST)

    def test_detect_skips_the_probe_with_no_hostname_to_probe(self):
        with mock.patch.object(funnel, 'probe') as probe:
            self.assertIsNone(funnel.detect(probe_url=True)['probe'])
            probe.assert_not_called()


# ── the reachability probe ───────────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ProbeTest(FunnelBase):
    def _with_open(self, side_effect):
        opener = mock.Mock()
        opener.open.side_effect = side_effect
        return mock.patch.object(funnel.urllib.request, 'build_opener',
                                 return_value=opener), opener

    def test_a_200_is_ok(self):
        patch, opener = self._with_open([_FakeResponse(200)])
        with patch:
            r = funnel.probe(HOST)
        self.assertTrue(r['ok'])
        self.assertTrue(r['reachable'])
        self.assertEqual(200, r['status'])
        self.assertEqual(CALLBACK, r['url'])

    def test_it_sends_head_to_the_callback_url(self):
        patch, opener = self._with_open([_FakeResponse(200)])
        with patch:
            funnel.probe(HOST)
        req = opener.open.call_args[0][0]
        self.assertEqual('HEAD', req.get_method())
        self.assertEqual(CALLBACK, req.full_url)

    def test_it_only_ever_requests_the_https_callback_of_a_valid_host(self):
        """The operator supplies a hostname, never a URL — so this probe can
        never be pointed at an arbitrary scheme, host, path or port."""
        patch, opener = self._with_open([_FakeResponse(200)] * 2)
        with patch:
            funnel.probe('h.example/../../etc/passwd')
            funnel.probe('file:///etc/passwd')
        self.assertEqual([f'https://h.example{funnel.OAUTH_RETURN_PATH}'],
                         [c[0][0].full_url for c in opener.open.call_args_list])

    def test_a_redirect_is_surfaced_not_followed(self):
        err = urllib.error.HTTPError(
            CALLBACK, 302, 'Found', {'Location': 'https://elsewhere.test/'}, None)
        patch, _ = self._with_open([err])
        with patch:
            r = funnel.probe(HOST)
        self.assertFalse(r['ok'])
        self.assertTrue(r['reachable'])
        self.assertEqual(302, r['status'])
        self.assertEqual('https://elsewhere.test/', r['location'])
        self.assertIn('redirect', r['detail'])

    def test_a_404_says_check_the_funnel_target(self):
        err = urllib.error.HTTPError(CALLBACK, 404, 'Not Found', {}, None)
        patch, _ = self._with_open([err])
        with patch:
            r = funnel.probe(HOST)
        self.assertFalse(r['ok'])
        self.assertTrue(r['reachable'])
        self.assertEqual(404, r['status'])
        self.assertIn('--set-path', r['detail'])

    def test_a_500_is_reachable_but_not_ok(self):
        err = urllib.error.HTTPError(CALLBACK, 500, 'Boom', {}, None)
        patch, _ = self._with_open([err])
        with patch:
            r = funnel.probe(HOST)
        self.assertFalse(r['ok'])
        self.assertTrue(r['reachable'])
        self.assertEqual(500, r['status'])

    def test_unreachable_explains_that_this_is_not_conclusive(self):
        patch, _ = self._with_open([urllib.error.URLError('nodename nor servname')])
        with patch:
            r = funnel.probe(HOST)
        self.assertFalse(r['ok'])
        self.assertFalse(r['reachable'])
        self.assertIsNone(r['status'])
        self.assertIn('not a failure', r['detail'])

    def test_an_invalid_hostname_never_opens_a_connection(self):
        patch, opener = self._with_open([_FakeResponse(200)])
        with patch:
            r = funnel.probe('umbrel')
        self.assertFalse(r['ok'])
        self.assertEqual('', r['url'])
        opener.open.assert_not_called()

    def test_the_timeout_is_bounded(self):
        patch, opener = self._with_open([_FakeResponse(200)])
        with patch:
            funnel.probe(HOST)
        self.assertEqual(funnel.PROBE_TIMEOUT, opener.open.call_args[1]['timeout'])
        self.assertLessEqual(funnel.PROBE_TIMEOUT, 10)

    def test_redirects_are_not_followed_by_the_opener(self):
        self.assertIsNone(funnel._NoRedirect().redirect_request(
            None, None, 301, 'Moved', {}, 'https://x.test/'))


# ── persistence ──────────────────────────────────────────────────────────────
class SavePublicUrlTest(FunnelBase):
    def test_it_defaults_to_blank_not_to_the_env_var(self):
        """Deliberate inversion of the usual env-seeds-defaults rule: a stale
        persisted hostname must never shadow the machine's live Funnel."""
        self._set_env_hostname(HOST)
        self.assertEqual('', plaid_settings.load()['funnel_hostname'])

    def test_it_persists_the_hostname(self):
        plaid_settings.save_public_url(funnel_hostname=HOST)
        self.assertEqual(HOST, plaid_settings.load()['funnel_hostname'])
        self.assertEqual(HOST, self._settings_file()['funnel_hostname'])

    def test_it_persists_the_redirect_uri(self):
        plaid_settings.save_public_url(redirect_uri=CALLBACK)
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])

    def test_none_leaves_a_field_untouched(self):
        plaid_settings.save_public_url(funnel_hostname=HOST,
                                       redirect_uri=CALLBACK)
        plaid_settings.save_public_url(redirect_uri='https://other.test/x')
        d = plaid_settings.load()
        self.assertEqual(HOST, d['funnel_hostname'])
        self.assertEqual('https://other.test/x', d['redirect_uri'])

    def test_it_does_not_disturb_the_credentials(self):
        plaid_settings.save('client-abc', 'production', 'https://old.test/cb',
                            'https://old.test/hook', sandbox_secret='sbx',
                            production_secret='prd', sync_interval_hours=6)
        plaid_settings.save_public_url(funnel_hostname=HOST,
                                       redirect_uri=CALLBACK)
        d = plaid_settings.load()
        self.assertEqual('client-abc', d['client_id'])
        self.assertEqual('production', d['environment'])
        self.assertEqual('sbx', d['sandbox_secret'])
        self.assertEqual('prd', d['production_secret'])
        self.assertEqual('https://old.test/hook', d['webhook_url'])
        self.assertEqual(6, d['sync_interval_hours'])
        self.assertEqual(CALLBACK, d['redirect_uri'])

    def test_the_credentials_form_does_not_wipe_the_hostname(self):
        plaid_settings.save_public_url(funnel_hostname=HOST)
        plaid_settings.save('cid', 'sandbox', CALLBACK, '')
        self.assertEqual(HOST, plaid_settings.load()['funnel_hostname'])

    def test_a_pre_v048_path_is_migrated_on_the_way_in(self):
        plaid_settings.save_public_url(
            redirect_uri=f'https://{HOST}/plaid/oauth_return')
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])


# ── admin routes, end to end ─────────────────────────────────────────────────
class WizardStateBTest(FunnelBase):
    def test_the_page_renders_state_b(self):
        r = self.client.get('/admin/plaid_settings')
        self.assertEqual(200, r.status_code)
        body = r.data.decode()
        self.assertIn('Plaid Redirect URI — Public URL Setup', body)
        self.assertIn('No public URL yet', body)
        self.assertIn('Manual entry', body)
        self.assertIn('Refresh status', body)

    def test_it_shows_every_setup_step(self):
        body = self.client.get('/admin/plaid_settings').data.decode()
        for snippet in ('tailscale.com/install.sh',
                        'sudo tailscale up',
                        'sudo tailscale funnel',
                        'sudo tailscale funnel status',
                        '--set-path=/bankbridge/plaid/oauth_return',
                        'http://127.0.0.1:5202',
                        'TAILSCALE_FUNNEL_HOSTNAME'):
            self.assertIn(snippet, body, snippet)

    def test_the_recommended_command_is_the_path_restricted_one(self):
        """The broad form publishes /admin and four unauthenticated Plaid write
        endpoints; the README argues this at length. The wizard must not undo
        it."""
        body = self.client.get('/admin/plaid_settings').data.decode()
        recommended = body.index('--set-path=/bankbridge/plaid/oauth_return')
        quickstart = body.index('sudo tailscale funnel --bg 5202')
        self.assertLess(recommended, quickstart)
        self.assertIn('publishes the <i>whole</i> app', body)

    def test_the_manual_entry_form_is_real_markup_not_escaped_text(self):
        """Regression: the form was once pre-rendered into a context variable,
        which Jinja autoescaped — so the operator saw `<form class="card"…` as
        visible text and had nothing to type into."""
        body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn('action="/admin/plaid_settings/funnel/save"', body)
        self.assertIn('<input name="hostname"', body)
        self.assertNotIn('&lt;form', body)
        self.assertNotIn('&lt;input', body)

    def test_both_manual_entry_buttons_are_wired(self):
        body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn('formaction="/admin/plaid_settings/funnel/test"', body)
        self.assertIn('Save as Plaid Redirect URI', body)

    def test_state_b_offers_no_detected_url(self):
        body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertNotIn('Detected public URL', body)
        self.assertNotIn('Use this as Plaid Redirect URI', body)


class WizardStateATest(FunnelBase):
    def setUp(self):
        super().setUp()
        self._set_env_hostname(HOST)

    def test_the_page_renders_state_a(self):
        r = self.client.get('/admin/plaid_settings')
        self.assertEqual(200, r.status_code)
        body = r.data.decode()
        self.assertIn('Detected public URL', body)
        self.assertIn(f'https://{HOST}', body)
        self.assertIn(CALLBACK, body)
        self.assertIn('Use this as Plaid Redirect URI', body)
        self.assertIn('Copy Plaid dashboard URL', body)
        self.assertIn('Refresh status', body)
        self.assertNotIn('No public URL yet', body)

    def test_it_names_the_detection_source(self):
        self.assertIn('from TAILSCALE_FUNNEL_HOSTNAME',
                      self.client.get('/admin/plaid_settings').data.decode())
        self.app.config['TAILSCALE_FUNNEL_HOSTNAME'] = ''
        plaid_settings.save_public_url(funnel_hostname=HOST)
        self.assertIn('saved here',
                      self.client.get('/admin/plaid_settings').data.decode())

    def test_the_copy_button_carries_the_full_redirect_uri(self):
        body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn(f'data-copy="{CALLBACK}"', body)

    def test_it_says_whether_the_setting_is_already_current(self):
        body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn('not yet', body)
        plaid_settings.save_public_url(redirect_uri=CALLBACK)
        body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn('already saved', body)

    def test_it_explains_where_the_url_comes_from(self):
        body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn('terminates HTTPS at Tailscale', body)
        self.assertIn('port 5202', body)

    def test_the_manual_entry_form_is_real_markup_here_too(self):
        body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn('Change the public hostname', body)
        self.assertIn('<input name="hostname"', body)
        self.assertNotIn('&lt;form', body)

    def test_a_saved_hostname_prefills_the_manual_entry_field(self):
        self.app.config['TAILSCALE_FUNNEL_HOSTNAME'] = ''
        plaid_settings.save_public_url(funnel_hostname=HOST)
        self.assertIn(f'name="hostname" value="{HOST}"',
                      self.client.get('/admin/plaid_settings').data.decode())

    def test_a_conflict_is_shown(self):
        plaid_settings.save_public_url(funnel_hostname='old.tailaaaa.ts.net')
        body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn('Two different hostnames are configured', body)
        self.assertIn('old.tailaaaa.ts.net', body)


class UseDetectedTest(FunnelBase):
    def setUp(self):
        super().setUp()
        self._set_env_hostname(HOST)

    def test_it_saves_the_derived_redirect_uri(self):
        r = self.client.post('/admin/plaid_settings/funnel/use',
                             data={'hostname': HOST})
        self.assertEqual(302, r.status_code)
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])

    def test_it_does_not_copy_an_env_hostname_into_the_settings_file(self):
        """Otherwise clearing the env var later would leave a phantom behind
        that looks locally configured."""
        self.client.post('/admin/plaid_settings/funnel/use',
                         data={'hostname': HOST})
        self.assertEqual('', plaid_settings.load()['funnel_hostname'])

    def test_it_is_idempotent(self):
        for _ in range(3):
            r = self.client.post('/admin/plaid_settings/funnel/use',
                                 data={'hostname': HOST})
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])
        self.assertIn('already', r.headers['Location'])

    def test_the_second_save_writes_no_second_audit_event(self):
        self.client.post('/admin/plaid_settings/funnel/use',
                         data={'hostname': HOST})
        self.client.post('/admin/plaid_settings/funnel/use',
                         data={'hostname': HOST})
        self.assertEqual(1, AuditEvent.query.filter_by(
            event_type='plaid_public_url_saved').count())

    def test_it_records_an_audit_event_with_the_previous_value(self):
        plaid_settings.save_public_url(redirect_uri='http://old.test/cb')
        self.client.post('/admin/plaid_settings/funnel/use',
                         data={'hostname': HOST})
        ev = AuditEvent.query.filter_by(
            event_type='plaid_public_url_saved').one()
        self.assertIn('http://old.test/cb', ev.payload_before)
        self.assertIn(CALLBACK, ev.payload_after)
        self.assertEqual('admin_ui', ev.actor)

    def test_junk_is_refused_with_a_message_and_changes_nothing(self):
        before = plaid_settings.load()['redirect_uri']
        r = self.client.post('/admin/plaid_settings/funnel/use',
                             data={'hostname': 'not a host'})
        self.assertIn('public+hostname', r.headers['Location'])
        self.assertEqual(before, plaid_settings.load()['redirect_uri'])

    def test_it_lands_back_on_the_wizard_section(self):
        r = self.client.post('/admin/plaid_settings/funnel/use',
                             data={'hostname': HOST})
        self.assertTrue(r.headers['Location'].endswith('#public-url'))


class ManualEntryEndToEndTest(FunnelBase):
    """The headline flow: nothing configured → paste a URL → save →
    PLAID_REDIRECT_URI is updated and the page flips to State A."""

    def test_manual_entry_to_saved_redirect_uri(self):
        self.assertEqual('unconfigured', funnel.detect()['state'])
        body = self.client.get('/admin/plaid_settings').data.decode()
        self.assertIn('No public URL yet', body)

        r = self.client.post('/admin/plaid_settings/funnel/save',
                             data={'hostname': f'https://{HOST}/'},
                             follow_redirects=True)
        self.assertEqual(200, r.status_code)

        # The setting an OAuth link actually reads…
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])
        # …persisted to disk, not just in memory…
        self.assertEqual(CALLBACK, self._settings_file()['redirect_uri'])
        self.assertEqual(HOST, self._settings_file()['funnel_hostname'])
        # …detection now reports State A from the saved value…
        d = funnel.detect()
        self.assertEqual('configured', d['state'])
        self.assertEqual('saved', d['source'])
        self.assertTrue(d['redirect_uri_matches'])
        # …the page reflects it…
        body = r.data.decode()
        self.assertIn('Detected public URL', body)
        self.assertIn(CALLBACK, body)
        self.assertNotIn('No public URL yet', body)
        # …the credentials form shows the same value…
        self.assertIn(f'name="redirect_uri" value="{CALLBACK}"', body)
        # …and it is on the audit trail.
        self.assertEqual(1, AuditEvent.query.filter_by(
            event_type='plaid_public_url_saved').count())

    def test_pasting_the_whole_redirect_uri_does_not_double_the_path(self):
        self.client.post('/admin/plaid_settings/funnel/save',
                         data={'hostname': CALLBACK})
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])

    def test_pasting_a_pre_v048_callback_lands_on_the_prefixed_path(self):
        self.client.post('/admin/plaid_settings/funnel/save',
                         data={'hostname': f'https://{HOST}/plaid/oauth_return'})
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])

    def test_an_http_url_is_saved_as_https(self):
        """Plaid requires https in production; silently keeping http would
        produce a redirect URI the bank refuses."""
        self.client.post('/admin/plaid_settings/funnel/save',
                         data={'hostname': f'http://{HOST}'})
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])

    def test_saving_is_idempotent(self):
        for _ in range(3):
            self.client.post('/admin/plaid_settings/funnel/save',
                             data={'hostname': HOST})
        self.assertEqual(CALLBACK, plaid_settings.load()['redirect_uri'])
        self.assertEqual(HOST, plaid_settings.load()['funnel_hostname'])

    def test_junk_changes_nothing(self):
        r = self.client.post('/admin/plaid_settings/funnel/save',
                             data={'hostname': 'localhost:5202'})
        self.assertIn('public+hostname', r.headers['Location'])
        self.assertEqual('', plaid_settings.load()['funnel_hostname'])
        self.assertEqual('unconfigured', funnel.detect()['state'])

    def test_a_missing_field_is_handled(self):
        r = self.client.post('/admin/plaid_settings/funnel/save', data={})
        self.assertEqual(302, r.status_code)
        self.assertEqual('unconfigured', funnel.detect()['state'])


class TestUrlButtonTest(FunnelBase):
    def test_it_reports_a_reachable_callback(self):
        with mock.patch.object(funnel, 'probe', return_value={
                'ok': True, 'reachable': True, 'status': 200, 'url': CALLBACK,
                'location': '', 'detail': 'HTTP 200 — all good.'}) as probe:
            r = self.client.post('/admin/plaid_settings/funnel/test',
                                 data={'hostname': f'https://{HOST}/'})
        self.assertEqual(200, r.status_code)
        probe.assert_called_once_with(HOST)
        body = r.data.decode()
        self.assertIn('URL test:', body)
        self.assertIn('HTTP 200', body)
        self.assertIn('banner-ok', body)

    def test_it_reports_an_unreachable_callback_without_saving(self):
        # Note the baseline is the PLAID_REDIRECT_URI env seed, not '' — Test URL
        # must leave whatever was there completely alone.
        before = plaid_settings.load()['redirect_uri']
        with mock.patch.object(funnel, 'probe', return_value={
                'ok': False, 'reachable': False, 'status': None,
                'url': CALLBACK, 'location': '',
                'detail': 'Not reachable from this container.'}):
            r = self.client.post('/admin/plaid_settings/funnel/test',
                                 data={'hostname': HOST})
        self.assertIn('Not reachable', r.data.decode())
        self.assertIn('banner-warn', r.data.decode())
        self.assertEqual('', plaid_settings.load()['funnel_hostname'])
        self.assertEqual(before, plaid_settings.load()['redirect_uri'])

    def test_a_missing_hostname_asks_for_one_without_probing(self):
        with mock.patch.object(funnel, 'probe') as probe:
            r = self.client.post('/admin/plaid_settings/funnel/test',
                                 data={'hostname': ''})
        probe.assert_not_called()
        self.assertEqual(200, r.status_code)
        self.assertIn('Enter a hostname', r.data.decode())


class RefreshStatusTest(FunnelBase):
    def test_refresh_re_runs_detection_and_probes(self):
        self._set_env_hostname(HOST)
        with mock.patch.object(funnel, 'probe', return_value={
                'ok': True, 'reachable': True, 'status': 200, 'url': CALLBACK,
                'location': '', 'detail': 'HTTP 200 — reachable.'}) as probe:
            r = self.client.get('/admin/plaid_settings?refresh=1')
        self.assertEqual(200, r.status_code)
        probe.assert_called_once_with(HOST)
        self.assertIn('HTTP 200', r.data.decode())

    def test_a_plain_load_never_waits_on_the_network(self):
        self._set_env_hostname(HOST)
        with mock.patch.object(funnel, 'probe') as probe:
            r = self.client.get('/admin/plaid_settings')
        probe.assert_not_called()
        self.assertEqual(200, r.status_code)
        self.assertNotIn('URL test:', r.data.decode())

    def test_refresh_with_nothing_configured_still_renders(self):
        r = self.client.get('/admin/plaid_settings?refresh=1')
        self.assertEqual(200, r.status_code)
        self.assertIn('No public URL yet', r.data.decode())


class VersionTest(unittest.TestCase):
    def test_version_is_bumped(self):
        import app as app_pkg
        self.assertEqual('0.7.0', app_pkg.__version__)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
