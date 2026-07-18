# SPDX-License-Identifier: MIT
"""v0.4.8 — the /bankbridge/ path prefix migration.

Covers three surfaces: the new routes answer, the pre-v0.4.8 paths still answer
(permanent redirect, logged), and a plaid_settings.json holding an old URL
auto-migrates on read."""
import importlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import legacy_paths, plaid_settings  # noqa: E402


def _test_config(data_dir, db_path):
    return {'TESTING': True, 'SCHEDULER_ENABLED': False, 'FERNET_KEY': '',
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}',
            'DATA_DIR': data_dir}


class PathPrefixTestCase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self.data_dir = tempfile.mkdtemp()
        self.app = create_app(_test_config(self.data_dir, self._dbpath))
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        plaid_settings._LOGGED_URL_MIGRATIONS.clear()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _write_settings(self, **overrides):
        d = {'client_id': 'cid', 'sandbox_secret': 'sek', 'production_secret': '',
             'environment': 'sandbox', 'redirect_uri': '', 'webhook_url': '',
             'sync_interval_hours': 24}
        d.update(overrides)
        with open(os.path.join(self.data_dir, 'plaid_settings.json'), 'w') as f:
            json.dump(d, f)


# ── New routes are registered and reachable ──────────────────────────────────
class NewRoutesTest(PathPrefixTestCase):
    def _rules(self):
        return {str(r) for r in self.app.url_map.iter_rules()}

    def test_every_plaid_route_is_registered_under_the_prefix(self):
        for path in ('/bankbridge/plaid/oauth_return',
                     '/bankbridge/api/plaid/create_link_token',
                     '/bankbridge/api/plaid/exchange_token',
                     '/bankbridge/api/plaid/set_link_company',
                     '/bankbridge/api/plaid/webhook'):
            self.assertIn(path, self._rules(), path)

    def test_no_unprefixed_plaid_route_remains(self):
        stale = [r for r in self._rules()
                 if r.startswith('/plaid/') or r.startswith('/api/plaid/')]
        self.assertEqual([], stale)

    def test_oauth_return_renders(self):
        r = self.client.get('/bankbridge/plaid/oauth_return')
        self.assertEqual(200, r.status_code)
        self.assertIn(b'Finishing your bank connection', r.data)

    def test_oauth_return_page_posts_to_the_prefixed_exchange(self):
        r = self.client.get('/bankbridge/plaid/oauth_return')
        self.assertIn(b'/bankbridge/api/plaid/exchange_token', r.data)
        self.assertNotIn(b"fetch('/api/plaid/exchange_token'", r.data)

    def test_create_link_token_answers_unconfigured(self):
        r = self.client.post('/bankbridge/api/plaid/create_link_token')
        self.assertEqual(400, r.status_code)
        self.assertIn('not configured', r.get_json()['error'])

    def test_set_link_company_answers(self):
        r = self.client.post('/bankbridge/api/plaid/set_link_company',
                             json={'company': 'Testing'})
        self.assertEqual(200, r.status_code)
        self.assertEqual('Testing', r.get_json()['owning_company'])

    def test_exchange_token_answers(self):
        r = self.client.post('/bankbridge/api/plaid/exchange_token', json={})
        self.assertEqual(400, r.status_code)
        self.assertIn('public_token required', r.get_json()['error'])

    def test_webhook_answers(self):
        r = self.client.post('/bankbridge/api/plaid/webhook',
                             json={'webhook_type': 'ITEM'})
        self.assertEqual(200, r.status_code)
        self.assertTrue(r.get_json()['ok'])

    def test_sync_now_keeps_its_path(self):
        """/api/sync/plaid_now is an admin-UI button, not a Plaid callback."""
        self.assertIn('/api/sync/plaid_now', self._rules())


# ── Backward-compat redirects ────────────────────────────────────────────────
class LegacyRedirectTest(PathPrefixTestCase):
    def test_old_oauth_return_redirects_permanently(self):
        r = self.client.get('/plaid/oauth_return')
        self.assertEqual(301, r.status_code)
        self.assertTrue(
            r.headers['Location'].endswith('/bankbridge/plaid/oauth_return'))

    def test_old_oauth_return_redirect_preserves_query_string(self):
        r = self.client.get('/plaid/oauth_return?oauth_state_id=abc123&x=1')
        self.assertEqual(301, r.status_code)
        self.assertTrue(r.headers['Location'].endswith(
            '/bankbridge/plaid/oauth_return?oauth_state_id=abc123&x=1'))

    def test_old_oauth_return_redirect_is_followable_end_to_end(self):
        r = self.client.get('/plaid/oauth_return?oauth_state_id=abc123',
                            follow_redirects=True)
        self.assertEqual(200, r.status_code)
        self.assertIn(b'Finishing your bank connection', r.data)

    def test_old_webhook_path_redirects(self):
        r = self.client.post('/plaid/webhook', json={'webhook_type': 'ITEM'})
        self.assertEqual(308, r.status_code)
        self.assertTrue(
            r.headers['Location'].endswith('/bankbridge/api/plaid/webhook'))

    def test_old_api_webhook_path_redirects(self):
        r = self.client.post('/api/plaid/webhook', json={'webhook_type': 'ITEM'})
        self.assertEqual(308, r.status_code)
        self.assertTrue(
            r.headers['Location'].endswith('/bankbridge/api/plaid/webhook'))

    def test_old_api_write_endpoints_redirect(self):
        for old, new in (
                ('/api/plaid/create_link_token',
                 '/bankbridge/api/plaid/create_link_token'),
                ('/api/plaid/exchange_token',
                 '/bankbridge/api/plaid/exchange_token'),
                ('/api/plaid/set_link_company',
                 '/bankbridge/api/plaid/set_link_company')):
            r = self.client.post(old, json={})
            self.assertEqual(308, r.status_code, old)
            self.assertTrue(r.headers['Location'].endswith(new), old)

    def test_post_redirects_use_308_so_the_body_survives(self):
        """A 301 lets a client downgrade POST→GET and drop the body; 308 does
        not. Following the redirect must reach the real handler with its JSON
        payload intact."""
        r = self.client.post('/api/plaid/set_link_company',
                             json={'company': 'Testing'},
                             follow_redirects=True)
        self.assertEqual(200, r.status_code)
        self.assertEqual('Testing', r.get_json()['owning_company'])

    def test_redirect_logs_at_info_with_both_paths(self):
        with self.assertLogs('bankbridge.legacy_paths', level='INFO') as cm:
            self.client.get('/plaid/oauth_return')
        joined = '\n'.join(cm.output)
        self.assertIn('/plaid/oauth_return', joined)
        self.assertIn('/bankbridge/plaid/oauth_return', joined)
        self.assertIn('INFO', joined)

    def test_non_plaid_paths_are_untouched(self):
        self.assertEqual(200, self.client.get('/api/health').status_code)
        self.assertNotEqual(301, self.client.get('/admin/').status_code)

    def test_unknown_plaid_lookalike_still_404s(self):
        self.assertEqual(404, self.client.get('/plaid/nope').status_code)


# ── plaid_settings.json auto-migration ───────────────────────────────────────
class SettingsMigrationTest(PathPrefixTestCase):
    def test_stored_tailnet_redirect_uri_auto_migrates(self):
        """Tim's live value — must flip without manual intervention."""
        self._write_settings(
            redirect_uri='https://umbrel.tail2b0bb0.ts.net/plaid/oauth_return')
        self.assertEqual(
            'https://umbrel.tail2b0bb0.ts.net/bankbridge/plaid/oauth_return',
            plaid_settings.load()['redirect_uri'])

    def test_stored_webhook_url_auto_migrates(self):
        self._write_settings(
            webhook_url='http://umbrel.local:5202/api/plaid/webhook')
        self.assertEqual('http://umbrel.local:5202/bankbridge/api/plaid/webhook',
                         plaid_settings.load()['webhook_url'])

    def test_migration_is_idempotent(self):
        new = 'https://umbrel.tail2b0bb0.ts.net/bankbridge/plaid/oauth_return'
        self._write_settings(redirect_uri=new)
        self.assertEqual(new, plaid_settings.load()['redirect_uri'])
        self.assertEqual(new, plaid_settings.load()['redirect_uri'])

    def test_migration_leaves_blank_and_foreign_urls_alone(self):
        self._write_settings(redirect_uri='', webhook_url='https://x.test/hook')
        d = plaid_settings.load()
        self.assertEqual('', d['redirect_uri'])
        self.assertEqual('https://x.test/hook', d['webhook_url'])

    def test_migration_logs_once_at_info(self):
        self._write_settings(
            redirect_uri='https://umbrel.tail2b0bb0.ts.net/plaid/oauth_return')
        with self.assertLogs('bankbridge.plaid_settings', level='INFO') as cm:
            plaid_settings.load()
        self.assertIn('/bankbridge/plaid/oauth_return', '\n'.join(cm.output))

    def test_saving_an_old_url_normalizes_it(self):
        plaid_settings.save(
            'cid', 'sandbox',
            redirect_uri='http://umbrel.local:5202/plaid/oauth_return',
            webhook_url='http://umbrel.local:5202/api/plaid/webhook')
        d = plaid_settings.load()
        self.assertEqual('http://umbrel.local:5202/bankbridge/plaid/oauth_return',
                         d['redirect_uri'])
        self.assertEqual('http://umbrel.local:5202/bankbridge/api/plaid/webhook',
                         d['webhook_url'])
        on_disk = json.load(
            open(os.path.join(self.data_dir, 'plaid_settings.json')))
        self.assertEqual(d['redirect_uri'], on_disk['redirect_uri'])

    def test_admin_form_shows_the_migrated_url(self):
        self._write_settings(
            redirect_uri='https://umbrel.tail2b0bb0.ts.net/plaid/oauth_return')
        r = self.client.get('/admin/plaid_settings')
        self.assertEqual(200, r.status_code)
        self.assertIn(b'/bankbridge/plaid/oauth_return', r.data)
        self.assertNotIn(
            b'value="https://umbrel.tail2b0bb0.ts.net/plaid/oauth_return"', r.data)

    def test_admin_form_placeholders_use_the_new_paths(self):
        r = self.client.get('/admin/plaid_settings')
        self.assertIn(b'placeholder="http://umbrel.local:5202'
                      b'/bankbridge/plaid/oauth_return"', r.data)
        self.assertIn(b'placeholder="http://umbrel.local:5202'
                      b'/bankbridge/api/plaid/webhook"', r.data)


# ── Config default + helper unit tests ───────────────────────────────────────
class RedirectUriDefaultTest(unittest.TestCase):
    """`config` is reloaded to re-evaluate the env-seeded class attributes, then
    reloaded once more on the way out so a stray value can't leak sideways."""

    def _reload_config(self, redirect_uri):
        """Reload `config` with PLAID_REDIRECT_URI set to `redirect_uri`, or
        unset when it is None."""
        import config as config_module
        with mock.patch.dict(os.environ, {}, clear=False):
            if redirect_uri is None:
                os.environ.pop('PLAID_REDIRECT_URI', None)
            else:
                os.environ['PLAID_REDIRECT_URI'] = redirect_uri
            importlib.reload(config_module)
            return config_module.Config.PLAID_REDIRECT_URI

    def tearDown(self):
        import config as config_module
        importlib.reload(config_module)

    def test_env_default_uses_the_new_path(self):
        self.assertEqual(
            'http://umbrel.local:5202/bankbridge/plaid/oauth_return',
            self._reload_config(None))

    def test_env_override_still_wins(self):
        self.assertEqual('https://x.test/custom', self._reload_config('https://x.test/custom'))


class MigrateUrlUnitTest(unittest.TestCase):
    def test_path_only_rewrite_preserves_scheme_host_and_query(self):
        self.assertEqual(
            'https://h.example:8443/bankbridge/plaid/oauth_return?a=1',
            legacy_paths.migrate_url(
                'https://h.example:8443/plaid/oauth_return?a=1'))

    def test_every_mapped_path_round_trips_to_a_real_route(self):
        fd, path = tempfile.mkstemp(suffix='.sqlite')
        self.addCleanup(os.remove, path)
        self.addCleanup(os.close, fd)
        self.addCleanup(crypto.reset_cache)
        app = create_app(_test_config(tempfile.mkdtemp(), path))
        rules = {str(r) for r in app.url_map.iter_rules()}
        for old, new in legacy_paths.LEGACY_PATH_MAP.items():
            self.assertIn(new, rules, f'{old} → {new}')

    def test_unmapped_paths_pass_through(self):
        for url in ('', 'https://x.test/admin/plaid_settings',
                    'https://x.test/api/sync/plaid_now'):
            self.assertEqual(url, legacy_paths.migrate_url(url))


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
