# SPDX-License-Identifier: MIT
"""Boot-time self-healing for Postgres app-role password drift (v0.3.5).

The problem this fixes
──────────────────────
Postgres persists the `bankbridge` login role's password from the FIRST time
its data volume was initialized. If a later deploy hands the app a *different*
password than that first-init value — e.g. an earlier init happened while
`APP_SEED` was blank because compose was run by hand over SSH, or a stop/start
cycle propagated env differently — then every connection the app makes fails
with:

    psycopg2.OperationalError: password authentication failed for user "bankbridge"

The historical workaround was to wipe the postgres volume and reinstall, which
throws away Plaid tokens and local history.

The fix
───────
At boot we probe the DB with a `SELECT 1`. If (and only if) that fails with a
*password authentication* error, we open a short-lived **superuser** connection
(using credentials the operator DOES control — `DB_SUPERUSER` /
`DB_SUPERUSER_PASSWORD`, which default to the postgres superuser + `APP_SEED`)
and `ALTER USER` the app role's password to match exactly what the app is
already trying to authenticate with (the password embedded in `DATABASE_URL`).
Then we re-probe. If it now succeeds the drift is healed transparently — no
volume wipe, no manual intervention.

Design guarantees:
  * **Idempotent / transparent** — on a healthy DB the probe succeeds and this
    is a no-op costing one `SELECT 1`. Recovery only ever fires on the specific
    password-auth failure.
  * **Fail-safe** — if the superuser is *also* unreachable we log a loud warning
    and return; we never crash the app or mask a non-auth error.
  * **Secret-safe** — the actual `APP_SEED` / password value is never written to
    the logs; messages only ever say the password was "rotated".

Uses only psycopg2 (already a dependency) for the superuser side channel, kept
deliberately separate from the SQLAlchemy engine/pool the rest of the app uses.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

# psycopg2 is imported lazily inside _rotate_superuser_password so that merely
# importing this module (which happens on every boot) never requires the driver
# — only the actual superuser rotation path does.

log = logging.getLogger('bankbridge.db_recovery')

# Substring that marks the exact failure mode we self-heal. Postgres emits
# `FATAL:  password authentication failed for user "…"` for a wrong password.
_PW_AUTH_MARKER = 'password authentication failed'


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of an ensure_db_auth() pass.

    status is one of:
      'healthy'             — probe succeeded first try; recovery never needed.
      'recovered'           — password drift detected and auto-healed.
      'disabled'            — drift detected but AUTO_RECOVER_DB_AUTH is off.
      'recovery_failed'     — drift detected, self-heal attempted, still broken.
      'probe_failed_other'  — probe failed for a NON-auth reason (not our case).
    """
    status: str
    recovered: bool = False


def _looks_like_password_auth_failure(exc: BaseException) -> bool:
    """True when `exc` (a SQLAlchemy or raw psycopg2 error) is a Postgres
    password-authentication failure — the one drift symptom we self-heal."""
    parts = [str(exc)]
    orig = getattr(exc, 'orig', None)
    if orig is not None:
        parts.append(str(orig))
    return any(_PW_AUTH_MARKER in p.lower() for p in parts)


def _redact(msg: object, secrets) -> str:
    """Stringify `msg`, blanking any occurrence of a known secret so an error
    string can never leak APP_SEED / a password into the logs."""
    out = str(msg)
    for s in secrets:
        if s:
            out = out.replace(s, '***')
    return out


def _probe(engine) -> None:
    """Run `SELECT 1` on a fresh connection. Raises on any failure."""
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))


def _rotate_superuser_password(db_url: str, superuser: str,
                               superuser_password: str, target_user: str,
                               target_password: str) -> None:
    """Open a superuser psycopg2 connection and `ALTER USER <target_user>` to
    `target_password`. Raises the last connection error if no superuser
    connection candidate succeeds.

    Tries the configured superuser password first, then a passwordless connect
    (covers a db configured with POSTGRES_HOST_AUTH_METHOD=trust). The role name
    is quoted via psycopg2.sql.Identifier and the password is bound as a
    parameter (psycopg2 does client-side literal substitution, so this is safe
    for the ALTER utility statement)."""
    import psycopg2
    from psycopg2 import sql

    url = make_url(db_url)
    host = url.host or 'db'
    port = url.port or 5432
    dbname = url.database or 'postgres'

    candidates = []
    if superuser_password:
        candidates.append(superuser_password)
    candidates.append(None)  # trust-auth fallback

    last_exc: BaseException | None = None
    for pw in candidates:
        try:
            conn = psycopg2.connect(host=host, port=port, dbname=dbname,
                                    user=superuser, password=pw,
                                    connect_timeout=5)
        except psycopg2.OperationalError as exc:
            last_exc = exc
            continue
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL('ALTER USER {} WITH PASSWORD %s').format(
                        sql.Identifier(target_user)),
                    (target_password,))
            return
        finally:
            conn.close()
    raise last_exc or RuntimeError('no superuser connection candidate available')


def _record_recovery_event(target_user: str) -> None:
    """Write the post-recovery AuditEvent. Best-effort (audit.record never
    raises), and only reached after a successful re-probe, by which point the
    audit_events table is guaranteed present (drift only happens on an already-
    initialized volume)."""
    from . import audit
    audit.record(
        'db_auth_recovered',
        actor='system',
        notes=(f'Rotated {target_user} password to match current APP_SEED. '
               'Cause: postgres init drift from prior deploy.'))


def ensure_db_auth(app, engine) -> RecoveryResult:
    """Boot-time DB auth self-heal. Call inside an app context, right before
    db.create_all(). Returns a RecoveryResult; never raises for the auth-drift
    case (fail-safe). Non-auth probe errors are swallowed here too so the normal
    boot path / pool_pre_ping retry handles them.

    Transparent by design: a healthy DB just runs one `SELECT 1`."""
    # 1 · probe
    try:
        _probe(engine)
        return RecoveryResult('healthy')
    except OperationalError as exc:
        if not _looks_like_password_auth_failure(exc):
            # Connection refused, DB starting up, etc. — not ours to fix here.
            log.warning('DB probe failed (non-auth); leaving it to normal boot '
                        'retry: %s', _redact(exc, ()))
            return RecoveryResult('probe_failed_other')
        log.warning('DB probe failed: password authentication failed for the '
                    'app role. Likely APP_SEED drift from a prior postgres '
                    'volume init — attempting self-heal.')

    # 2 · gate
    if not app.config.get('AUTO_RECOVER_DB_AUTH', True):
        log.warning('AUTO_RECOVER_DB_AUTH is disabled; skipping self-heal. '
                    'Manual intervention required to reset the app role '
                    'password.')
        return RecoveryResult('disabled')

    # 3 · rotate via superuser
    db_url = app.config['SQLALCHEMY_DATABASE_URI']
    superuser = app.config.get('DB_SUPERUSER', 'postgres') or 'postgres'
    superuser_password = app.config.get('DB_SUPERUSER_PASSWORD') or ''
    url = make_url(db_url)
    target_user = url.username or 'bankbridge'
    # Set the role password to exactly what the app authenticates with — the
    # password already embedded in DATABASE_URL (which IS the current APP_SEED).
    target_password = url.password or ''
    secrets = (superuser_password, target_password)

    try:
        _rotate_superuser_password(db_url, superuser, superuser_password,
                                   target_user, target_password)
    except Exception as exc:  # noqa: BLE001 - fail-safe, never crash boot
        log.error('DB auth recovery failed — could not rotate the app role '
                  'password via the "%s" superuser: %s. Manual intervention '
                  'required.', superuser, _redact(exc, secrets))
        return RecoveryResult('recovery_failed')

    # 4 · re-probe with the app credentials (dispose first to drop any
    # connections the pool cached before the rotation).
    try:
        engine.dispose()
        _probe(engine)
    except OperationalError as exc:
        log.error('DB auth recovery rotated the password but the app role is '
                  'still unreachable: %s. Manual intervention required.',
                  _redact(exc, secrets))
        return RecoveryResult('recovery_failed')

    log.warning('auto-recovered DB auth drift: rotated the app role password to '
                'match the current APP_SEED. Continuing boot.')
    _record_recovery_event(target_user)
    return RecoveryResult('recovered', recovered=True)
