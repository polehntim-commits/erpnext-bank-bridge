# SPDX-License-Identifier: MIT
"""Boot-time self-heal of postgres app-role password drift (v0.3.5).

  * a healthy DB probes clean → recovery never fires (transparent no-op)
  * a "password authentication failed" probe → rotate the app-role password via
    the first reachable superuser candidate → re-probe succeeds → boot continues
  * candidates are tried in order: optional DB_SUPERUSER, then the deterministic
    `bridgeadmin` rescue user (HMAC(APP_SEED, salt))
  * every candidate failing → loud warning pointing at the manual rescue script,
    NO crash
  * a successful recovery writes a `db_auth_recovered` AuditEvent
  * AUTO_RECOVER_DB_AUTH=False → self-heal is skipped entirely
  * a non-auth probe error is left to the normal boot retry (not "recovered")
  * the rescue password derivation is deterministic + secrets never leak

    cd erpnext-bank-bridge_v0.1.0
    python3 -m unittest discover -s tests -v
"""
import hashlib
import hmac
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from sqlalchemy.exc import OperationalError  # noqa: E402

from app import create_app, db, crypto  # noqa: E402
from app import db_recovery  # noqa: E402
from app.models import AuditEvent  # noqa: E402


class _FakePgError(Exception):
    """Stands in for psycopg2.OperationalError so these tests don't require the
    driver to be installed (the app imports psycopg2 lazily, and the local test
    env is sqlite-only)."""


def _auth_error(user='bankbridge'):
    """A SQLAlchemy OperationalError wrapping the driver's password-auth error,
    exactly as raised when the app role's password has drifted."""
    orig = _FakePgError(
        f'FATAL:  password authentication failed for user "{user}"\n')
    return OperationalError('SELECT 1', {}, orig)


class Base(unittest.TestCase):
    EXTRA_CONFIG = {}

    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        cfg = {'TESTING': True,
               'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
               'DATA_DIR': self._datadir, 'FERNET_KEY': '',
               'SCHEDULER_ENABLED': False}
        cfg.update(self.EXTRA_CONFIG)
        self.app = create_app(cfg)
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)


class TestEnsureDbAuth(Base):
    def test_auto_recovery_does_not_fire_on_healthy_db(self):
        # The real sqlite probe succeeds → no rotation attempt at all.
        with mock.patch('app.db_recovery._rotate_with_superuser') as rot:
            res = db_recovery.ensure_db_auth(self.app, db.engine)
        self.assertEqual(res.status, 'healthy')
        self.assertFalse(res.recovered)
        rot.assert_not_called()

    def test_auto_recovery_fires_on_password_mismatch(self):
        calls = {'n': 0}

        def fake_probe(engine):
            calls['n'] += 1
            if calls['n'] == 1:
                raise _auth_error()          # first probe: drifted password
            return None                      # re-probe after rotation: healthy

        with mock.patch('app.db_recovery._probe', side_effect=fake_probe), \
             mock.patch('app.db_recovery._rotate_with_superuser') as rot:
            res = db_recovery.ensure_db_auth(self.app, db.engine)

        self.assertEqual(res.status, 'recovered')
        self.assertTrue(res.recovered)
        # Default DB_SUPERUSER is blank, so the only candidate is the rescue user.
        self.assertEqual(res.via, 'bridgeadmin')
        rot.assert_called_once()
        # The role password is set to the value the app authenticates with (the
        # DATABASE_URL password); the sqlite test URI has no user so target_user
        # falls back to the default role name.
        _url, su_user, _su_pw, target_user, _target_pw = rot.call_args.args
        self.assertEqual(su_user, 'bridgeadmin')
        self.assertEqual(target_user, 'bankbridge')

    def test_candidates_tried_in_order_superuser_then_rescue(self):
        # With an explicit DB_SUPERUSER configured, it is tried first; when it
        # fails the rescue user is tried next and succeeds.
        self.app.config['DB_SUPERUSER'] = 'pgadmin'
        self.app.config['DB_SUPERUSER_PASSWORD'] = 'sekret'
        seen = []

        def rot(db_url, su_user, su_pw, target_user, target_pw):
            seen.append(su_user)
            if su_user == 'pgadmin':
                raise _FakePgError('password authentication failed for user "pgadmin"')
            return None

        calls = {'n': 0}

        def fake_probe(engine):
            calls['n'] += 1
            if calls['n'] == 1:
                raise _auth_error()
            return None

        with mock.patch('app.db_recovery._probe', side_effect=fake_probe), \
             mock.patch('app.db_recovery._rotate_with_superuser', side_effect=rot):
            res = db_recovery.ensure_db_auth(self.app, db.engine)

        self.assertEqual(seen, ['pgadmin', 'bridgeadmin'])   # order preserved
        self.assertEqual(res.status, 'recovered')
        self.assertEqual(res.via, 'bridgeadmin')

    def test_auto_recovery_fails_gracefully_when_all_superusers_unauth(self):
        def always_auth_fail(engine):
            raise _auth_error()

        def rot_fail(*a, **k):
            raise _FakePgError(
                'FATAL:  password authentication failed for user "bridgeadmin"\n')

        with mock.patch('app.db_recovery._probe', side_effect=always_auth_fail), \
             mock.patch('app.db_recovery._rotate_with_superuser',
                        side_effect=rot_fail):
            res = db_recovery.ensure_db_auth(self.app, db.engine)  # must not raise

        self.assertEqual(res.status, 'recovery_failed')
        self.assertFalse(res.recovered)

    def test_recovery_failed_when_reprobe_still_broken(self):
        # Rotation "succeeds" but the app role is still unreachable afterwards.
        def always_auth_fail(engine):
            raise _auth_error()

        with mock.patch('app.db_recovery._probe', side_effect=always_auth_fail), \
             mock.patch('app.db_recovery._rotate_with_superuser'):
            res = db_recovery.ensure_db_auth(self.app, db.engine)
        self.assertEqual(res.status, 'recovery_failed')

    def test_auto_recovery_audit_event(self):
        calls = {'n': 0}

        def fake_probe(engine):
            calls['n'] += 1
            if calls['n'] == 1:
                raise _auth_error()
            return None

        with mock.patch('app.db_recovery._probe', side_effect=fake_probe), \
             mock.patch('app.db_recovery._rotate_with_superuser'):
            db_recovery.ensure_db_auth(self.app, db.engine)

        ev = AuditEvent.query.filter_by(event_type='db_auth_recovered').one()
        self.assertEqual(ev.actor, 'system')
        self.assertIn('APP_SEED', ev.notes)
        self.assertIn('bridgeadmin', ev.notes)     # records which superuser
        self.assertIn('db_auth_recovered', db_recovery_event_types())

    def test_non_auth_error_does_not_recover(self):
        def conn_refused(engine):
            raise OperationalError(
                'SELECT 1', {},
                _FakePgError('could not connect to server'))

        with mock.patch('app.db_recovery._probe', side_effect=conn_refused), \
             mock.patch('app.db_recovery._rotate_with_superuser') as rot:
            res = db_recovery.ensure_db_auth(self.app, db.engine)
        self.assertEqual(res.status, 'probe_failed_other')
        rot.assert_not_called()


class TestConfigDisable(Base):
    EXTRA_CONFIG = {'AUTO_RECOVER_DB_AUTH': False}

    def test_config_disable_skips_recovery(self):
        def always_auth_fail(engine):
            raise _auth_error()

        with mock.patch('app.db_recovery._probe', side_effect=always_auth_fail), \
             mock.patch('app.db_recovery._rotate_with_superuser') as rot:
            res = db_recovery.ensure_db_auth(self.app, db.engine)
        self.assertEqual(res.status, 'disabled')
        self.assertFalse(res.recovered)
        rot.assert_not_called()


class TestRescueCredentials(unittest.TestCase):
    def test_rescue_password_is_deterministic_hmac(self):
        seed, salt = 'app-seed-value', 'bankbridge-rescue-v1'
        got = db_recovery.rescue_password(seed, salt)
        # Deterministic: same inputs → same output on every call.
        self.assertEqual(got, db_recovery.rescue_password(seed, salt))
        # And it IS HMAC-SHA256(key=seed, msg=salt) hex — the exact contract the
        # pgcrypto side script must reproduce (hmac(msg=salt, key=seed)).
        self.assertEqual(
            got,
            hmac.new(seed.encode(), salt.encode(), hashlib.sha256).hexdigest())
        self.assertEqual(len(got), 64)         # sha256 hex


class TestHelpers(unittest.TestCase):
    def test_password_auth_failure_detection(self):
        self.assertTrue(
            db_recovery._looks_like_password_auth_failure(_auth_error()))
        self.assertTrue(db_recovery._looks_like_password_auth_failure(
            _FakePgError('password authentication failed')))
        self.assertFalse(db_recovery._looks_like_password_auth_failure(
            _FakePgError('could not connect to server')))

    def test_redact_blanks_secrets(self):
        msg = 'connecting with password super-seed-value failed'
        red = db_recovery._redact(msg, ('super-seed-value', ''))
        self.assertNotIn('super-seed-value', red)
        self.assertIn('***', red)


def db_recovery_event_types():
    from app import audit
    return audit.EVENT_TYPES


if __name__ == '__main__':
    unittest.main()
