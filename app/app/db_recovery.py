# SPDX-License-Identifier: MIT
"""Boot-time self-healing for Postgres app-role password drift (v0.3.5).

The problem this fixes
──────────────────────
Postgres persists the `bankbridge` login role's password from the FIRST time
its data volume was initialized. If a later deploy hands the app a *different*
password than that first-init value — e.g. an earlier init happened while
`APP_SEED` was blank because compose was run by hand over SSH — then every
connection the app makes fails with:

    psycopg2.OperationalError: password authentication failed for user "bankbridge"

The historical workaround was to wipe the postgres volume and reinstall, which
throws away Plaid tokens and local history.

Why we can't just use the `postgres` superuser
───────────────────────────────────────────────
The compose runs the db with `POSTGRES_USER=bankbridge`, so postgres init makes
`bankbridge` the SOLE superuser — there is no separate `postgres` role to fall
back to (`psql -U postgres` → `role "postgres" does not exist`). And when
`bankbridge`'s own password has drifted, we can't authenticate as it either.

The fix — a deterministic rescue superuser
───────────────────────────────────────────
Fresh installs create a SECOND superuser, `bridgeadmin` (see
scripts/initdb.d/10-create-rescue-superuser.sh), whose password is derived
deterministically as HMAC-SHA256(key=APP_SEED, msg=salt). Because the same
APP_SEED + salt reproduce the same password on every boot, the app can always
re-derive it (rescue_password below) and log in as `bridgeadmin` to reset the
drifted `bankbridge` password — no volume wipe, no manual step.

At boot we probe with `SELECT 1`. Only on a *password authentication* failure we
try, in order, each configured superuser candidate — the optional `DB_SUPERUSER`
then the `bridgeadmin` rescue user — opening a short-lived connection and
`ALTER USER`-ing the app role's password to match what the app is already
authenticating with (the password embedded in `DATABASE_URL`). Then we re-probe.

Existing installs predate the rescue user (their volume only has `bankbridge`),
so auto-recovery has no superuser to reach and fails safe with a clear pointer
to the one-time manual repair: scripts/rotate_db_password.sh (which ALSO creates
`bridgeadmin`, so subsequent drifts self-heal).

Design guarantees:
  * **Idempotent / transparent** — on a healthy DB the probe succeeds and this
    is a no-op costing one `SELECT 1`. Recovery only fires on the password-auth
    failure.
  * **Fail-safe** — if no superuser candidate is reachable we log a loud warning
    (pointing at the manual script) and return; we never crash the app or mask a
    non-auth error.
  * **Secret-safe** — the actual APP_SEED / password values are never written to
    the logs; messages only ever say the password was "rotated".

Uses only psycopg2 (already a dependency) for the superuser side channel, kept
deliberately separate from the SQLAlchemy engine/pool the rest of the app uses.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

# psycopg2 is imported lazily inside _rotate_with_superuser so that merely
# importing this module (which happens on every boot) never requires the driver
# — only the actual superuser rotation path does.

log = logging.getLogger('bankbridge.db_recovery')

# Substring that marks the exact failure mode we self-heal. Postgres emits
# `FATAL:  password authentication failed for user "…"` for a wrong password.
_PW_AUTH_MARKER = 'password authentication failed'

# Path shown to the operator when auto-recovery can't reach any superuser.
_MANUAL_SCRIPT = 'scripts/rotate_db_password.sh'


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
    via: str | None = None  # which superuser role performed a successful rotation


def rescue_password(seed: str, salt: str) -> str:
    """Deterministically derive the rescue superuser's password from APP_SEED.

    HMAC-SHA256(key=seed, msg=salt) as lowercase hex. MUST stay byte-for-byte
    identical to the server-side derivation in
    scripts/initdb.d/10-create-rescue-superuser.sh, which computes it via
    pgcrypto as `encode(hmac(salt, seed, 'sha256'), 'hex')` — note pgcrypto's
    hmac(data, key, …) takes the message first and the key second, so there
    data=salt and key=seed, matching (key=seed, msg=salt) here."""
    return hmac.new(seed.encode(), salt.encode(), hashlib.sha256).hexdigest()


def _looks_like_password_auth_failure(exc: BaseException) -> bool:
    """True when `exc` (a SQLAlchemy or raw driver error) is a Postgres
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


def _candidate_superusers(app, url):
    """Ordered list of (role, password) pairs to try for the rescue rotation.

    Preference order: an explicitly-configured DB_SUPERUSER first (in case an
    operator provisioned a real one), then the deterministic `bridgeadmin`
    rescue user derived from APP_SEED."""
    candidates = []
    su = (app.config.get('DB_SUPERUSER') or '').strip()
    if su:
        candidates.append((su, app.config.get('DB_SUPERUSER_PASSWORD') or ''))
    rescue_user = (app.config.get('DB_RESCUE_USER') or '').strip()
    seed = app.config.get('DB_RESCUE_SEED') or ''
    salt = app.config.get('DB_RESCUE_SALT') or ''
    if rescue_user and seed and salt:
        candidates.append((rescue_user, rescue_password(seed, salt)))
    return candidates


class ForeignClusterError(RuntimeError):
    """Raised when a superuser connection landed on a Postgres that is not ours.

    v0.4.16 — on Umbrel the `db` hostname resolves to every app's database
    container (see app/db_host.py), so a recovery connection can genuinely open
    against a *neighbouring app's* cluster. Rotating a role password there would
    corrupt that app's credentials, so we refuse and move on."""


def cluster_is_ours(conn, dbname: str, target_user: str) -> bool:
    """True when `conn` is open against our own cluster.

    Two independent markers, both cheap and both requiring no state we have to
    migrate in:

      * the connection reports the database name we asked for, and
      * our app role exists in this cluster's `pg_authid`.

    A neighbouring app's Postgres has neither a `bankbridge` database nor a
    `bankbridge` role, so it fails both. A fresh install passes both, because
    postgres init creates the database and role before the app ever boots."""
    with conn.cursor() as cur:
        cur.execute('SELECT current_database()')
        row = cur.fetchone()
        if not row or row[0] != dbname:
            return False
        cur.execute('SELECT 1 FROM pg_roles WHERE rolname = %s', (target_user,))
        return cur.fetchone() is not None


def _rotate_with_superuser(db_url: str, superuser: str, superuser_password: str,
                           target_user: str, target_password: str) -> None:
    """Open a superuser psycopg2 connection and `ALTER USER <target_user>` to
    `target_password`. Raises the last connection error if the superuser can't
    connect.

    Tries the given superuser password first, then a passwordless connect
    (covers a db configured with POSTGRES_HOST_AUTH_METHOD=trust). The role name
    is quoted via psycopg2.sql.Identifier and the password is bound as a
    parameter (psycopg2 does client-side literal substitution, so this is safe
    for the ALTER utility statement).

    v0.4.16 — the hostname may resolve to several containers, so we try each
    resolved address and, critically, verify the cluster is OURS before issuing
    any ALTER. Writing to a neighbour's Postgres is far worse than failing to
    recover, so an unverified cluster is skipped, never repaired."""
    import psycopg2
    from psycopg2 import sql

    from .db_host import resolve_addresses

    url = make_url(db_url)
    host = url.host or 'db'
    port = url.port or 5432
    dbname = url.database or 'postgres'

    pw_candidates: list[str | None] = []
    if superuser_password:
        pw_candidates.append(superuser_password)
    pw_candidates.append(None)  # trust-auth fallback

    # `None` keeps libpq's own resolution as a last resort when the name does
    # not resolve here (e.g. a socket path or an already-literal address).
    addresses: list[str | None] = list(resolve_addresses(host, port)) or [None]

    last_exc: BaseException | None = None
    for addr in addresses:
        for pw in pw_candidates:
            kwargs = {'host': host, 'port': port, 'dbname': dbname,
                      'user': superuser, 'password': pw, 'connect_timeout': 5}
            if addr is not None:
                kwargs['hostaddr'] = addr
            try:
                conn = psycopg2.connect(**kwargs)
            except psycopg2.OperationalError as exc:
                last_exc = exc
                continue
            try:
                if not cluster_is_ours(conn, dbname, target_user):
                    last_exc = ForeignClusterError(
                        f'{addr or host} answered but is not our cluster — '
                        'refusing to rotate a password there')
                    log.warning(
                        'DB auth recovery: %s hosts a different Postgres (no '
                        '"%s" database + role); skipping it rather than '
                        'altering another app\'s credentials.',
                        addr or host, dbname)
                    break  # a foreign cluster stays foreign whatever password
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


def _record_recovery_event(target_user: str, via: str) -> None:
    """Write the post-recovery AuditEvent. Best-effort (audit.record never
    raises), and only reached after a successful re-probe, by which point the
    audit_events table is guaranteed present (drift only happens on an already-
    initialized volume)."""
    from . import audit
    audit.record(
        'db_auth_recovered',
        actor='system',
        notes=(f'Rotated {target_user} password to match current APP_SEED via '
               f'the "{via}" superuser. Cause: postgres init drift from prior '
               'deploy.'))


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
        # v0.4.16 — this message used to assert APP_SEED drift, and that
        # confident misattribution cost four releases of fixes to a password
        # that was never wrong. The far more common cause is a shared `db`
        # network alias resolving to a neighbouring app's Postgres, which
        # rejects our credentials with this identical error. Name both, and
        # point at the diagnostic that tells them apart.
        log.warning(
            'DB probe failed: password authentication failed for the app role. '
            'Two causes look identical here — (a) the connection reached '
            'ANOTHER app\'s postgres via a shared network alias (check the '
            '"DB host ... is AMBIGUOUS" line logged at boot), or (b) genuine '
            'APP_SEED drift from a prior volume init. Attempting self-heal; it '
            'will refuse to touch a cluster that is not ours.')

    # 2 · gate
    if not app.config.get('AUTO_RECOVER_DB_AUTH', True):
        log.warning('AUTO_RECOVER_DB_AUTH is disabled; skipping self-heal. '
                    'Manual intervention required (%s).', _MANUAL_SCRIPT)
        return RecoveryResult('disabled')

    # 3 · rotate via the first reachable superuser candidate
    db_url = app.config['SQLALCHEMY_DATABASE_URI']
    url = make_url(db_url)
    target_user = url.username or 'bankbridge'
    # Set the role password to exactly what the app authenticates with — the
    # password already embedded in DATABASE_URL (which IS the current APP_SEED).
    target_password = url.password or ''

    candidates = _candidate_superusers(app, url)
    secrets = tuple({p for _, p in candidates} | {target_password,
                                                  app.config.get('DB_RESCUE_SEED') or ''})
    used = None
    last_exc: BaseException | None = None
    for su_user, su_pw in candidates:
        try:
            _rotate_with_superuser(db_url, su_user, su_pw, target_user,
                                   target_password)
            used = su_user
            break
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            last_exc = exc
            log.warning('DB auth recovery: superuser "%s" could not reset the '
                        'app role: %s', su_user, _redact(exc, secrets))

    if used is None:
        tried = ', '.join(u for u, _ in candidates) or '(none configured)'
        log.error('DB auth recovery failed — no superuser candidate (%s) could '
                  'reset the "%s" role. This install likely predates the rescue '
                  'superuser; run %s once to repair it (that also creates the '
                  'rescue user so future drift self-heals). Last error: %s',
                  tried, target_user, _MANUAL_SCRIPT, _redact(last_exc, secrets))
        return RecoveryResult('recovery_failed')

    # 4 · re-probe with the app credentials (dispose first to drop any
    # connections the pool cached before the rotation).
    try:
        engine.dispose()
        _probe(engine)
    except OperationalError as exc:
        log.error('DB auth recovery rotated the password via "%s" but the app '
                  'role is still unreachable: %s. Manual intervention required '
                  '(%s).', used, _redact(exc, secrets), _MANUAL_SCRIPT)
        return RecoveryResult('recovery_failed', via=used)

    log.warning('auto-recovered DB auth drift: rotated the app role password to '
                'match the current APP_SEED via the "%s" superuser. Continuing '
                'boot.', used)
    _record_recovery_event(target_user, used)
    return RecoveryResult('recovered', recovered=True, via=used)
