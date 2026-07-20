# SPDX-License-Identifier: MIT
"""Pin the Postgres connection to *our* database container (v0.4.16).

The problem this fixes
──────────────────────
Every Umbrel app shares ONE flat Docker network, and nearly every app names its
Postgres service `db`. Docker's embedded DNS registers a network alias per
service name, so on a box running several such apps the name `db` resolves to
*every* app's database container at once:

    db → ['10.21.0.24', '10.21.0.66', '10.21.0.70', '10.21.0.73']

Only one of those is ours. libpq walks the resolved addresses in order, and the
two failure modes behave very differently:

  * `Connection refused`               → libpq falls through to the next address.
  * `password authentication failed`   → FATAL; libpq stops and the connect dies.

So a connection succeeds or fails depending on where a *foreign* Postgres that
happens to accept TCP lands in a DNS answer order that Docker randomizes per
lookup. That is the whole bug. For five releases it was misread as "APP_SEED
drift" because the symptom is verbatim:

    FATAL:  password authentication failed for user "bankbridge"

— which is what a foreign cluster says when we offer it our credentials. The
seed, the derivation and the stored password were correct the entire time; the
app was simply talking to the wrong server ~3 times out of 4.

The fix
───────
Two layers, because they fail independently:

 1. **A unique network alias** (`bankbridge-db`, set in docker-compose.yml) so
    the name is unambiguous in the first place. This is the real fix, and it is
    the same app-name-prefix convention CLAUDE.md already mandates for Funnel
    paths — applied to the Docker network namespace instead of the URL path.

 2. **This module**, which does not trust the name regardless. It resolves the
    host itself, probes the candidates, and connects to the one that proves it
    is ours. That keeps an install working on the old compose (before the
    operator redeploys) and survives any future alias collision.

A connection is "ours" when it authenticates AND reports the database name we
asked for. A foreign cluster fails the first test; a same-named database on a
foreign cluster would fail the second.

Cheap by design: with an unambiguous host (one address) this adds one
`getaddrinfo` per new pooled connection and nothing else — no probing, no extra
round-trip. Probing only ever happens when the name really is ambiguous.
"""
from __future__ import annotations

import logging
import socket
import threading

from sqlalchemy.engine import make_url

log = logging.getLogger('bankbridge.db_host')

# Marks the "wrong cluster answered" failure. Identical to the drift symptom,
# which is exactly why this bug was misdiagnosed for so long.
_PW_AUTH_MARKER = 'password authentication failed'

# Seconds to wait per candidate when probing an ambiguous host. Kept short: a
# wrong candidate is usually an instant refusal, and boot must not hang.
_PROBE_TIMEOUT = 5


class _Pin:
    """Remembers the address that last proved to be our database.

    Shared across pooled connections so the probe cost is paid once, not per
    connect. Cleared whenever the pinned address stops working, so a db
    container recreated on a new IP re-probes instead of failing forever."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._addr: str | None = None

    def get(self) -> str | None:
        with self._lock:
            return self._addr

    def set(self, addr: str | None) -> None:
        with self._lock:
            self._addr = addr


def resolve_addresses(host: str, port: int = 5432, resolver=None) -> list[str]:
    """Every IP `host` currently resolves to, de-duplicated and ordered.

    `resolver` is injectable so tests can model an ambiguous alias without
    needing a Docker network."""
    resolve = resolver or socket.getaddrinfo
    try:
        infos = resolve(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        log.warning('could not resolve DB host %r: %s', host, exc)
        return []
    seen: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return seen


def is_pg_auth_failure(exc: BaseException) -> bool:
    """True when `exc` is Postgres refusing our password — i.e. either genuine
    drift or, far more often, a foreign cluster that has never heard of us."""
    return _PW_AUTH_MARKER in str(exc).lower()


def _connect_to(addr: str | None, params: dict, connector=None):
    """psycopg2-connect to `addr`, presenting the original hostname to libpq.

    `hostaddr` fixes the address while `host` stays the name, so authentication
    and any TLS handshake behave exactly as they would without pinning — we are
    choosing *which* server answers, not changing how we talk to it."""
    import psycopg2

    connect = connector or psycopg2.connect
    kwargs = dict(params)
    if addr is not None:
        kwargs['hostaddr'] = addr
    return connect(**kwargs)


def _proves_ours(conn, expected_db: str) -> bool:
    """True when the far end is the database we meant to reach.

    Authenticating already rules out every foreign cluster (they reject our
    credentials). The `current_database()` check additionally rules out a
    cluster that shares our role and password but not our database — belt and
    braces, and it costs one round-trip on a connection we are keeping."""
    with conn.cursor() as cur:
        cur.execute('SELECT current_database()')
        row = cur.fetchone()
    return bool(row) and row[0] == expected_db


def connect_to_our_db(params: dict, pin: _Pin, connector=None, resolver=None):
    """Open a psycopg2 connection to our database, whatever else shares the name.

    Order of attempts:
      1. the pinned address, if we have proved one before (the steady state);
      2. otherwise resolve the host — a single address means the name is
         unambiguous, so connect straight through with no probing;
      3. with several addresses, probe each and keep the one that proves ours.

    Raises the most informative error if nothing qualifies: a password failure
    from a real candidate beats a connection refusal from a dead one."""
    host = params.get('host') or 'db'
    port = int(params.get('port') or 5432)
    expected_db = params.get('dbname') or ''

    pinned = pin.get()
    if pinned:
        try:
            conn = _connect_to(pinned, params, connector)
            if _proves_ours(conn, expected_db):
                return conn
            conn.close()
        except Exception as exc:  # noqa: BLE001 — pin went stale; re-probe below
            log.info('pinned DB address %s no longer usable (%s); re-resolving',
                     pinned, type(exc).__name__)
        pin.set(None)

    addresses = resolve_addresses(host, port, resolver)
    if not addresses:
        # Resolution failed outright — let libpq produce the canonical error.
        return _connect_to(None, params, connector)

    if len(addresses) == 1:
        conn = _connect_to(addresses[0], params, connector)
        pin.set(addresses[0])
        return conn

    log.warning(
        'DB host %r resolves to %d addresses (%s) — another Umbrel app is '
        'sharing this network alias. Probing for our own database; set a '
        'unique alias (see docker-compose.yml) to remove the ambiguity.',
        host, len(addresses), ', '.join(addresses))

    auth_failures: list[BaseException] = []
    other_failures: list[BaseException] = []
    for addr in addresses:
        try:
            conn = _connect_to(addr, params, connector)
        except Exception as exc:  # noqa: BLE001 — try the next candidate
            (auth_failures if is_pg_auth_failure(exc) else other_failures).append(exc)
            continue
        try:
            if _proves_ours(conn, expected_db):
                log.warning('pinned DB connections to %s (the only address that '
                            'is our own database)', addr)
                pin.set(addr)
                return conn
            conn.close()
        except Exception as exc:  # noqa: BLE001 — unusable candidate
            other_failures.append(exc)
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — already broken; nothing to save
                pass

    # An auth failure means a real Postgres answered and rejected us, which is
    # the actionable error; a refusal just means nothing was listening.
    if auth_failures:
        raise auth_failures[0]
    if other_failures:
        raise other_failures[0]
    raise RuntimeError(
        f'no address for {host!r} hosts database {expected_db!r}')


def psycopg2_params(db_url: str) -> dict:
    """psycopg2 connect kwargs for `db_url`, minus the host address itself."""
    url = make_url(db_url)
    params: dict = {
        'host': url.host or 'db',
        'port': url.port or 5432,
        'dbname': url.database or 'bankbridge',
    }
    if url.username:
        params['user'] = url.username
    if url.password:
        params['password'] = url.password
    return params


def make_creator(db_url: str):
    """A SQLAlchemy `creator` that always lands on our own database.

    Returns (creator, pin) — the pin is handed back so callers (and tests) can
    inspect or reset which address is in use."""
    params = psycopg2_params(db_url)
    pin = _Pin()

    def creator():
        return connect_to_our_db(params, pin)

    return creator, pin


def log_host_diagnostics(db_url: str, resolver=None) -> dict:
    """Log what the DB hostname resolves to, and return it for the tests.

    This is the observability the four previous attempts at this bug lacked:
    the ambiguity is invisible from inside the app unless something prints it.
    Never raises and never logs a credential."""
    url = make_url(db_url)
    host = url.host or 'db'
    port = url.port or 5432
    addresses = resolve_addresses(host, port, resolver)
    info = {'host': host, 'port': port, 'addresses': addresses,
            'ambiguous': len(addresses) > 1}
    if info['ambiguous']:
        log.warning(
            'DB host %r is AMBIGUOUS: %s. On Umbrel every app shares one '
            'network and most name their database service "db", so this alias '
            'points at other apps too. Connections are pinned to our own '
            'database, but the alias should be made unique.',
            host, ', '.join(addresses))
    else:
        log.info('DB host %r resolves to %s', host,
                 ', '.join(addresses) or '(unresolved)')
    return info
