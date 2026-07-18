# SPDX-License-Identifier: MIT
"""Optional HTTP Basic Auth gate for the admin UI.

  * both ADMIN_BASIC_AUTH_* set → /admin requires credentials (401 without)
  * correct user+password → 200; wrong password or wrong user → 401
  * a werkzeug password *hash* is accepted as the configured password
  * either env var blank → no auth (backward-compatible LAN mode)
  * the Plaid callback / JSON API (separate blueprint) is NEVER gated

    cd app
    python3 -m unittest discover -s tests -v
"""
import base64
import os
import tempfile
import unittest

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app, db, crypto  # noqa: E402


def _basic(user, pw):
    raw = f'{user}:{pw}'.encode()
    return {'Authorization': 'Basic ' + base64.b64encode(raw).decode()}


class AuthBase(unittest.TestCase):
    # Subclasses set AUTH to extra test_config keys.
    AUTH = {}

    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        cfg = {
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        }
        cfg.update(self.AUTH)
        self.app = create_app(cfg)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)


class TestAuthEnforced(AuthBase):
    AUTH = {'ADMIN_BASIC_AUTH_USER': 'boss', 'ADMIN_BASIC_AUTH_PASS': 's3cret'}

    def test_no_credentials_401(self):
        r = self.client.get('/admin')
        self.assertEqual(r.status_code, 401)
        # Browser is told to prompt for Basic credentials.
        self.assertIn('Basic', r.headers.get('WWW-Authenticate', ''))

    def test_correct_credentials_200(self):
        r = self.client.get('/admin', headers=_basic('boss', 's3cret'))
        self.assertEqual(r.status_code, 200)

    def test_root_dashboard_also_gated(self):
        self.assertEqual(self.client.get('/').status_code, 401)
        self.assertEqual(
            self.client.get('/', headers=_basic('boss', 's3cret')).status_code, 200)

    def test_wrong_password_401(self):
        r = self.client.get('/admin', headers=_basic('boss', 'nope'))
        self.assertEqual(r.status_code, 401)

    def test_wrong_user_401(self):
        r = self.client.get('/admin', headers=_basic('intruder', 's3cret'))
        self.assertEqual(r.status_code, 401)

    def test_plaid_api_never_gated(self):
        # The JSON API lives on a separate blueprint — Plaid's OAuth callback
        # must stay reachable without admin credentials. Unconfigured Plaid
        # yields a 400 (not a 401), proving no auth challenge was raised.
        r = self.client.get('/bankbridge/api/plaid/create_link_token')
        self.assertEqual(r.status_code, 400)
        self.assertNotIn('WWW-Authenticate', r.headers)

    def test_health_never_gated(self):
        r = self.client.get('/api/health')
        self.assertEqual(r.status_code, 200)


class TestAuthHashedPassword(AuthBase):
    AUTH = {
        'ADMIN_BASIC_AUTH_USER': 'boss',
        'ADMIN_BASIC_AUTH_PASS': generate_password_hash('h4shed-pw'),
    }

    def test_hashed_password_correct_200(self):
        r = self.client.get('/admin', headers=_basic('boss', 'h4shed-pw'))
        self.assertEqual(r.status_code, 200)

    def test_hashed_password_wrong_401(self):
        r = self.client.get('/admin', headers=_basic('boss', 'wrong'))
        self.assertEqual(r.status_code, 401)


class TestAuthDisabledWhenPassBlank(AuthBase):
    AUTH = {'ADMIN_BASIC_AUTH_USER': 'boss', 'ADMIN_BASIC_AUTH_PASS': ''}

    def test_no_auth_required(self):
        self.assertEqual(self.client.get('/admin').status_code, 200)


class TestAuthDisabledWhenUserBlank(AuthBase):
    AUTH = {'ADMIN_BASIC_AUTH_USER': '', 'ADMIN_BASIC_AUTH_PASS': 's3cret'}

    def test_no_auth_required(self):
        self.assertEqual(self.client.get('/admin').status_code, 200)


class TestAuthDefaultOff(AuthBase):
    # No ADMIN_BASIC_AUTH_* in config at all — the stock deployment.
    def test_no_auth_required(self):
        self.assertEqual(self.client.get('/admin').status_code, 200)
        self.assertEqual(self.client.get('/').status_code, 200)


if __name__ == '__main__':
    unittest.main()
