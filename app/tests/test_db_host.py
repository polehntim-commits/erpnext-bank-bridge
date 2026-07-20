# SPDX-License-Identifier: MIT
"""Pinning Postgres connections to our own container (v0.4.16).

The bug: on Umbrel every app shares one Docker network and most name their
database service `db`, so that alias resolves to several apps' containers. libpq
walks the addresses — a refusal falls through, but a password rejection is FATAL
— so a connection lands on a neighbour's Postgres and dies with "password
authentication failed". That is the symptom misread as APP_SEED drift for five
releases.

  * an unambiguous host adds no probing (one address → connect straight through)
  * an ambiguous host is probed; the address that proves ours is used and pinned
  * a foreign cluster rejecting our password never wins, even when listed first
  * a cluster that authenticates but reports another database never wins either
  * the pin is reused (no re-probing) and re-resolves when the container moves
  * recovery refuses to ALTER a role on a cluster that is not ours
  * derivation stays deterministic across processes (it was never the problem)

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import db_host  # noqa: E402
from app import db_recovery  # noqa: E402

URL = 'postgresql://bankbridge:s3cret@db:5432/bankbridge'

# The four addresses `db` really resolved to on Tim's box: our own container
# plus BucketLog's (which rejects us) and two that refuse TCP outright.
OURS = '10.21.0.73'
FOREIGN_AUTH = '10.21.0.66'    # fafo-bucketlog_db_1 — answers, rejects us
REFUSED_A = '10.21.0.70'       # fafo-erpnext_db_1
REFUSED_B = '10.21.0.24'


class _AuthFailed(Exception):
    """Stands in for psycopg2.OperationalError; psycopg2 is imported lazily so
    these tests never need the driver."""


class _Refused(Exception):
    pass


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.statements.append((sql, params))
        if 'current_database' in sql:
            self._rows = [(self._conn.dbname,)]
        elif 'pg_roles' in sql:
            self._rows = [(1,)] if self._conn.has_role else []
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    """A Postgres that reports a database name and may or may not know our role."""

    def __init__(self, dbname, has_role=True):
        self.dbname = dbname
        self.has_role = has_role
        self.statements = []
        self.closed = False
        self.autocommit = False

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


def _resolver(addresses):
    """A getaddrinfo stand-in returning `addresses` in the given order."""
    def resolve(host, port, *a, **kw):
        return [(2, 1, 6, '', (addr, port)) for addr in addresses]
    return resolve


def _connector(behaviour):
    """A psycopg2.connect stand-in dispatching on the pinned address.

    `behaviour` maps address → a _FakeConn factory or an exception to raise.
    Records every address dialled so tests can assert on probe order/count."""
    dialled = []

    def connect(**kwargs):
        addr = kwargs.get('hostaddr') or kwargs.get('host')
        dialled.append(addr)
        outcome = behaviour[addr]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome()

    connect.dialled = dialled
    return connect


class ResolveAddressesTests(unittest.TestCase):
    def test_returns_every_address_deduplicated_in_order(self):
        resolve = _resolver([OURS, FOREIGN_AUTH, OURS, REFUSED_A])
        self.assertEqual(db_host.resolve_addresses('db', 5432, resolve),
                         [OURS, FOREIGN_AUTH, REFUSED_A])

    def test_unresolvable_host_yields_empty_list_not_an_exception(self):
        import socket

        def boom(*a, **kw):
            raise socket.gaierror('Name or service not known')

        self.assertEqual(db_host.resolve_addresses('nope', 5432, boom), [])


class AuthFailureDetectionTests(unittest.TestCase):
    def test_recognises_the_postgres_password_rejection(self):
        exc = _AuthFailed('FATAL:  password authentication failed for user "bankbridge"')
        self.assertTrue(db_host.is_pg_auth_failure(exc))

    def test_connection_refused_is_not_an_auth_failure(self):
        self.assertFalse(db_host.is_pg_auth_failure(_Refused('Connection refused')))


class Psycopg2ParamsTests(unittest.TestCase):
    def test_carries_host_port_dbname_and_credentials(self):
        self.assertEqual(db_host.psycopg2_params(URL), {
            'host': 'db', 'port': 5432, 'dbname': 'bankbridge',
            'user': 'bankbridge', 'password': 's3cret'})

    def test_defaults_apply_when_the_url_omits_port_and_database(self):
        params = db_host.psycopg2_params('postgresql://u:p@somehost/bankbridge')
        self.assertEqual(params['port'], 5432)
        self.assertEqual(params['host'], 'somehost')


class UnambiguousHostTests(unittest.TestCase):
    """A unique alias — the state the compose rename produces — must be cheap."""

    def test_single_address_connects_without_probing(self):
        connect = _connector({OURS: lambda: _FakeConn('bankbridge')})
        pin = db_host._Pin()
        conn = db_host.connect_to_our_db(
            db_host.psycopg2_params(URL), pin,
            connector=connect, resolver=_resolver([OURS]))
        self.assertEqual(conn.dbname, 'bankbridge')
        self.assertEqual(connect.dialled, [OURS])
        # No identity round-trip needed when there is nothing to disambiguate.
        self.assertEqual(conn.statements, [])

    def test_single_address_is_pinned_for_reuse(self):
        connect = _connector({OURS: lambda: _FakeConn('bankbridge')})
        pin = db_host._Pin()
        db_host.connect_to_our_db(db_host.psycopg2_params(URL), pin,
                                  connector=connect, resolver=_resolver([OURS]))
        self.assertEqual(pin.get(), OURS)


class AmbiguousHostTests(unittest.TestCase):
    """The live failure: `db` resolving to four containers, one of them ours."""

    def _behaviour(self):
        return {
            REFUSED_B: _Refused('connection to server at "10.21.0.24" failed: '
                                'Connection refused'),
            FOREIGN_AUTH: _AuthFailed('connection to server at "10.21.0.66" failed: '
                                      'FATAL:  password authentication failed for '
                                      'user "bankbridge"'),
            REFUSED_A: _Refused('connection to server at "10.21.0.70" failed: '
                                'Connection refused'),
            OURS: lambda: _FakeConn('bankbridge'),
        }

    def test_finds_our_database_when_a_foreign_one_is_listed_first(self):
        # This exact ordering is what made the app 500: libpq stops dead on the
        # foreign auth rejection instead of trying the next address.
        connect = _connector(self._behaviour())
        pin = db_host._Pin()
        conn = db_host.connect_to_our_db(
            db_host.psycopg2_params(URL), pin, connector=connect,
            resolver=_resolver([FOREIGN_AUTH, REFUSED_A, OURS]))
        self.assertEqual(conn.dbname, 'bankbridge')
        self.assertEqual(pin.get(), OURS)

    def test_finds_our_database_whatever_the_answer_order(self):
        params = db_host.psycopg2_params(URL)
        orders = [
            [OURS, FOREIGN_AUTH, REFUSED_A, REFUSED_B],
            [FOREIGN_AUTH, OURS, REFUSED_A, REFUSED_B],
            [REFUSED_A, REFUSED_B, FOREIGN_AUTH, OURS],
            [REFUSED_B, FOREIGN_AUTH, OURS, REFUSED_A],
        ]
        for order in orders:
            with self.subTest(order=order):
                pin = db_host._Pin()
                conn = db_host.connect_to_our_db(
                    params, pin, connector=_connector(self._behaviour()),
                    resolver=_resolver(order))
                self.assertEqual(conn.dbname, 'bankbridge')
                self.assertEqual(pin.get(), OURS)

    def test_verifies_identity_before_accepting_a_connection(self):
        connect = _connector(self._behaviour())
        db_host.connect_to_our_db(
            db_host.psycopg2_params(URL), db_host._Pin(), connector=connect,
            resolver=_resolver([FOREIGN_AUTH, OURS]))
        # The winning connection was asked to name its database.
        self.assertEqual(connect.dialled, [FOREIGN_AUTH, OURS])

    def test_cluster_answering_for_another_database_is_rejected(self):
        """A neighbour that shares our role and password but not our database."""
        impostor = _FakeConn('bucketlog')
        behaviour = self._behaviour()
        behaviour[FOREIGN_AUTH] = lambda: impostor
        connect = _connector(behaviour)
        conn = db_host.connect_to_our_db(
            db_host.psycopg2_params(URL), db_host._Pin(), connector=connect,
            resolver=_resolver([FOREIGN_AUTH, OURS]))
        self.assertEqual(conn.dbname, 'bankbridge')
        self.assertTrue(impostor.closed, 'the impostor connection must be closed')

    def test_raises_the_auth_failure_when_no_address_is_ours(self):
        behaviour = self._behaviour()
        del behaviour[OURS]
        with self.assertRaises(_AuthFailed):
            db_host.connect_to_our_db(
                db_host.psycopg2_params(URL), db_host._Pin(),
                connector=_connector(behaviour),
                resolver=_resolver([REFUSED_A, FOREIGN_AUTH]))

    def test_prefers_the_auth_failure_over_a_bare_refusal_when_reporting(self):
        """An auth rejection means a real Postgres answered — the useful error."""
        behaviour = self._behaviour()
        del behaviour[OURS]
        with self.assertRaises(_AuthFailed):
            db_host.connect_to_our_db(
                db_host.psycopg2_params(URL), db_host._Pin(),
                connector=_connector(behaviour),
                resolver=_resolver([REFUSED_B, REFUSED_A, FOREIGN_AUTH]))

    def test_unresolvable_host_falls_back_to_libpq_resolution(self):
        import socket

        def boom(*a, **kw):
            raise socket.gaierror('no such host')

        connect = _connector({'db': lambda: _FakeConn('bankbridge')})
        conn = db_host.connect_to_our_db(
            db_host.psycopg2_params(URL), db_host._Pin(),
            connector=connect, resolver=boom)
        self.assertEqual(conn.dbname, 'bankbridge')
        self.assertEqual(connect.dialled, ['db'])


class PinReuseTests(unittest.TestCase):
    def test_pinned_address_is_reused_without_reprobing(self):
        connect = _connector({
            OURS: lambda: _FakeConn('bankbridge'),
            FOREIGN_AUTH: _AuthFailed('password authentication failed'),
        })
        params = db_host.psycopg2_params(URL)
        pin = db_host._Pin()
        pin.set(OURS)
        db_host.connect_to_our_db(params, pin, connector=connect,
                                  resolver=_resolver([FOREIGN_AUTH, OURS]))
        # Straight to the pin — the foreign address is never dialled again.
        self.assertEqual(connect.dialled, [OURS])

    def test_stale_pin_is_dropped_and_the_host_reprobed(self):
        """The db container was recreated on a new IP — must recover, not wedge."""
        moved = '10.21.0.99'
        connect = _connector({
            OURS: _Refused('Connection refused'),   # old address, container gone
            moved: lambda: _FakeConn('bankbridge'),
        })
        pin = db_host._Pin()
        pin.set(OURS)
        conn = db_host.connect_to_our_db(
            db_host.psycopg2_params(URL), pin, connector=connect,
            resolver=_resolver([moved]))
        self.assertEqual(conn.dbname, 'bankbridge')
        self.assertEqual(pin.get(), moved)

    def test_pin_pointing_at_a_foreign_cluster_is_discarded(self):
        impostor = _FakeConn('bucketlog')
        connect = _connector({
            FOREIGN_AUTH: lambda: impostor,
            OURS: lambda: _FakeConn('bankbridge'),
        })
        pin = db_host._Pin()
        pin.set(FOREIGN_AUTH)
        conn = db_host.connect_to_our_db(
            db_host.psycopg2_params(URL), pin, connector=connect,
            resolver=_resolver([OURS]))
        self.assertEqual(conn.dbname, 'bankbridge')
        self.assertEqual(pin.get(), OURS)
        self.assertTrue(impostor.closed)

    def test_pin_is_threadsafe_across_concurrent_workers(self):
        """Several gunicorn threads opening connections must not corrupt the pin."""
        import threading

        pin = db_host._Pin()
        params = db_host.psycopg2_params(URL)
        errors = []

        def worker():
            try:
                db_host.connect_to_our_db(
                    params, pin,
                    connector=_connector({
                        FOREIGN_AUTH: _AuthFailed('password authentication failed'),
                        OURS: lambda: _FakeConn('bankbridge')}),
                    resolver=_resolver([FOREIGN_AUTH, OURS]))
            except Exception as exc:  # noqa: BLE001 — surfaced by the assert below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(pin.get(), OURS)


class DiagnosticsTests(unittest.TestCase):
    """Observability the four previous attempts lacked — the ambiguity is
    invisible from inside the container unless something prints it."""

    def test_reports_an_ambiguous_host(self):
        info = db_host.log_host_diagnostics(
            URL, resolver=_resolver([OURS, FOREIGN_AUTH, REFUSED_A]))
        self.assertTrue(info['ambiguous'])
        self.assertEqual(info['host'], 'db')
        self.assertEqual(len(info['addresses']), 3)

    def test_reports_a_unique_host_as_unambiguous(self):
        info = db_host.log_host_diagnostics(
            'postgresql://u:p@bankbridge-db:5432/bankbridge',
            resolver=_resolver([OURS]))
        self.assertFalse(info['ambiguous'])
        self.assertEqual(info['host'], 'bankbridge-db')

    def test_warns_loudly_when_the_alias_is_shared(self):
        with self.assertLogs('bankbridge.db_host', level='WARNING') as caught:
            db_host.log_host_diagnostics(URL,
                                         resolver=_resolver([OURS, FOREIGN_AUTH]))
        self.assertIn('AMBIGUOUS', '\n'.join(caught.output))

    def test_diagnostics_never_leak_the_password(self):
        with self.assertLogs('bankbridge.db_host', level='INFO') as caught:
            db_host.log_host_diagnostics(URL,
                                         resolver=_resolver([OURS, FOREIGN_AUTH]))
        self.assertNotIn('s3cret', '\n'.join(caught.output))


class ClusterIdentityGuardTests(unittest.TestCase):
    """Recovery must never ALTER a role on a neighbouring app's Postgres."""

    def test_our_cluster_is_recognised(self):
        conn = _FakeConn('bankbridge', has_role=True)
        self.assertTrue(db_recovery.cluster_is_ours(conn, 'bankbridge',
                                                    'bankbridge'))

    def test_cluster_serving_another_database_is_rejected(self):
        conn = _FakeConn('bucketlog', has_role=True)
        self.assertFalse(db_recovery.cluster_is_ours(conn, 'bankbridge',
                                                     'bankbridge'))

    def test_cluster_without_our_role_is_rejected(self):
        conn = _FakeConn('bankbridge', has_role=False)
        self.assertFalse(db_recovery.cluster_is_ours(conn, 'bankbridge',
                                                     'bankbridge'))

    def test_rotation_refuses_a_foreign_cluster_and_alters_nothing(self):
        foreign = _FakeConn('bucketlog', has_role=False)
        fake_psycopg2 = mock.MagicMock()
        fake_psycopg2.OperationalError = _AuthFailed
        fake_psycopg2.connect.return_value = foreign
        with mock.patch.dict(sys.modules, {'psycopg2': fake_psycopg2,
                                           'psycopg2.sql': mock.MagicMock()}), \
                mock.patch.object(db_host, 'resolve_addresses',
                                  return_value=[FOREIGN_AUTH]):
            with self.assertRaises(db_recovery.ForeignClusterError):
                db_recovery._rotate_with_superuser(
                    URL, 'bridgeadmin', 'pw', 'bankbridge', 'newpw')
        altered = [s for s, _ in foreign.statements if 'ALTER' in str(s).upper()]
        self.assertEqual(altered, [], 'must not ALTER a foreign cluster')
        self.assertTrue(foreign.closed)


class BootWiringTests(unittest.TestCase):
    """The creator has to actually reach SQLAlchemy, or none of the above runs."""

    def _app(self, **config):
        from flask import Flask
        app = Flask(__name__)
        app.config.update(config)
        return app

    def test_creator_is_installed_for_a_postgres_uri(self):
        from app import _install_db_host_pinning
        app = self._app(SQLALCHEMY_DATABASE_URI=URL,
                        SQLALCHEMY_ENGINE_OPTIONS={'pool_size': 5})
        with mock.patch.object(db_host, 'resolve_addresses', return_value=[OURS]):
            _install_db_host_pinning(app)
        options = app.config['SQLALCHEMY_ENGINE_OPTIONS']
        self.assertTrue(callable(options.get('creator')))
        # Existing engine options must survive, not be replaced wholesale.
        self.assertEqual(options['pool_size'], 5)

    def test_pin_is_exposed_for_inspection(self):
        from app import _install_db_host_pinning
        app = self._app(SQLALCHEMY_DATABASE_URI=URL, SQLALCHEMY_ENGINE_OPTIONS={})
        with mock.patch.object(db_host, 'resolve_addresses', return_value=[OURS]):
            _install_db_host_pinning(app)
        self.assertIn('db_host_pin', app.extensions['bankbridge'])

    def test_testing_apps_keep_their_plain_engine(self):
        from app import _install_db_host_pinning
        app = self._app(TESTING=True, SQLALCHEMY_DATABASE_URI=URL,
                        SQLALCHEMY_ENGINE_OPTIONS={})
        _install_db_host_pinning(app)
        self.assertNotIn('creator', app.config['SQLALCHEMY_ENGINE_OPTIONS'])

    def test_non_postgres_uri_is_left_alone(self):
        from app import _install_db_host_pinning
        app = self._app(SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                        SQLALCHEMY_ENGINE_OPTIONS={})
        _install_db_host_pinning(app)
        self.assertNotIn('creator', app.config['SQLALCHEMY_ENGINE_OPTIONS'])

    def test_boot_survives_a_failure_to_install_pinning(self):
        """Diagnostics must never be the thing that stops the app starting."""
        from app import _install_db_host_pinning
        app = self._app(SQLALCHEMY_DATABASE_URI=URL, SQLALCHEMY_ENGINE_OPTIONS={})
        with mock.patch.object(db_host, 'log_host_diagnostics',
                               side_effect=RuntimeError('boom')):
            _install_db_host_pinning(app)  # must not raise
        self.assertNotIn('creator', app.config['SQLALCHEMY_ENGINE_OPTIONS'])


class DerivationDeterminismTests(unittest.TestCase):
    """The seed derivation was never the bug — lock that in so a future
    regression here is distinguishable from the alias collision."""

    def test_rescue_password_is_stable_within_a_process(self):
        a = db_recovery.rescue_password('seed-value', 'bankbridge-rescue-v1')
        b = db_recovery.rescue_password('seed-value', 'bankbridge-rescue-v1')
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_rescue_password_is_stable_across_processes(self):
        """A fresh interpreter must derive the same value — proves there is no
        per-process salt, counter or clock in the derivation."""
        code = ('import os;os.environ.setdefault("DATABASE_URL","postgresql://x:x@l/x");'
                'from app.db_recovery import rescue_password;'
                'print(rescue_password("seed-value","bankbridge-rescue-v1"))')
        out = subprocess.run(
            [sys.executable, '-c', code],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(
            out.stdout.strip(),
            db_recovery.rescue_password('seed-value', 'bankbridge-rescue-v1'))

    def test_different_seeds_give_different_passwords(self):
        self.assertNotEqual(
            db_recovery.rescue_password('seed-a', 'salt'),
            db_recovery.rescue_password('seed-b', 'salt'))


if __name__ == '__main__':
    unittest.main()
