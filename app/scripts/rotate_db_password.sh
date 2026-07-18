#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# DB-auth rescue for Bank Bridge: reset the `bankbridge` role password to the
# current APP_SEED and provision the `bridgeadmin` rescue superuser, so future
# password drift self-heals at boot (app/db_recovery.py).
#
# Use this if the app can't reach Postgres with:
#   psycopg2.OperationalError: password authentication failed for user "bankbridge"
# or PROACTIVELY on an install created before the rescue superuser existed — it
# provisions `bridgeadmin` so the boot self-heal has something to log in with.
#
#
# TWO EXECUTION CONTEXTS (v0.4.7)
# ═══════════════════════════════
# Earlier versions assumed they ran on the Umbrel HOST and shelled out to
# `docker`. Running them the natural way —
#     sudo docker exec fafo-bank-bridge_server_1 bash scripts/rotate_db_password.sh
# — died instantly on `docker not found on PATH`, because the app image has no
# docker client in it. That is the bug this version fixes: the script now
# DETECTS where it is and picks the right mechanism.
#
#   HOST MODE  (docker present and usable)
#     Full-power path, unchanged from v0.3.5. Flips the db to `trust` auth,
#     resets the password, creates the rescue user, restores auth, restarts the
#     server. Works EVEN WHEN NOBODY CAN AUTHENTICATE, because it reaches the
#     db through the container's filesystem rather than the network.
#
#   CONTAINER MODE  (no docker — we are inside the app container)
#     Talks to Postgres over the normal network protocol using psycopg2, which
#     the app already depends on. NOTE: the image is python:3.11-slim with no
#     postgresql-client, so there is no `psql` here — psycopg2 via python3 is
#     the only wire to the database, and it is always present.
#
#     This path needs to authenticate as SOME superuser. That is usually fine:
#     the compose sets POSTGRES_USER=bankbridge, so `bankbridge` IS a superuser
#     and the DATABASE_URL password normally still works — which is exactly the
#     case when you are provisioning `bridgeadmin` ahead of a drift. It tries,
#     in order: bankbridge with the DATABASE_URL password, the derived
#     bridgeadmin, an explicit DB_SUPERUSER, then passwordless (trust) connects.
#
#     If NONE of those authenticate, the password has already drifted AND there
#     is no rescue user yet — no amount of network-side cleverness can log in,
#     and the script says so and points you at HOST MODE, which can.
#
# Force a mode with `--mode host` / `--mode container` (default: auto).
#
# Conservative in both modes: host mode backs up pg_hba.conf and restores it via
# an EXIT trap even if a step fails; container mode only ever issues ALTER/CREATE
# ROLE. Neither touches your data. Secrets are never printed.
#
# Usage:
#   ./rotate_db_password.sh [DB_CONTAINER] [SERVER_CONTAINER]     # host
#   docker exec <server> bash scripts/rotate_db_password.sh       # container
set -euo pipefail

MODE='auto'
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="${2:-auto}"; shift 2 ;;
    --mode=*) MODE="${1#*=}"; shift ;;
    -h|--help) sed -n '2,53p' "$0"; exit 0 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- ${ARGS[@]+"${ARGS[@]}"}

DB_CONTAINER="${1:-}"
SERVER_CONTAINER="${2:-}"
RESCUE_USER="${DB_RESCUE_USER:-bridgeadmin}"
RESCUE_SALT="${DB_RESCUE_SALT:-bankbridge-rescue-v1}"

log() { printf '[rescue] %s\n' "$*" >&2; }
die() { printf '[rescue] ERROR: %s\n' "$*" >&2; exit 1; }

# ── mode detection ───────────────────────────────────────────────────────
# `docker` on PATH is necessary but not sufficient — `docker ps` must actually
# work (the socket could be unmounted, or we could lack permission). Anything
# else means we are inside a container and must go over the wire.
detect_mode() {
  if command -v docker >/dev/null 2>&1 && docker ps >/dev/null 2>&1; then
    echo host
  else
    echo container
  fi
}

if [ "${MODE}" = 'auto' ]; then
  MODE="$(detect_mode)"
  log "auto-detected execution context: ${MODE} mode"
else
  log "execution context forced: ${MODE} mode"
fi

# ══════════════════════════════════════════════════════════════════════════
# CONTAINER MODE — network-protocol repair via psycopg2
# ══════════════════════════════════════════════════════════════════════════
if [ "${MODE}" = 'container' ]; then
  command -v python3 >/dev/null 2>&1 || die \
    "container mode needs python3 (it is present in the app image); if you are on the Umbrel host, install/enable docker or pass --mode host"

  log "no usable docker client — repairing over the postgres protocol"
  # All logic in python: psycopg2 is a hard dependency of the app, unlike psql
  # which the slim image does not ship. Secrets stay inside the process (read
  # from the environment already present), never on a command line.
  DB_RESCUE_USER="${RESCUE_USER}" DB_RESCUE_SALT="${RESCUE_SALT}" \
  python3 - <<'PYEOF'
import hashlib
import hmac
import os
import sys

try:
    import psycopg2
    from psycopg2 import sql
except ImportError:
    sys.exit('[rescue] ERROR: psycopg2 is not importable — are you inside the '
             'Bank Bridge app container?')


def log(msg):
    print('[rescue] %s' % msg, file=sys.stderr)


def parse_db_url(url):
    """postgresql://user:pw@host:port/db → dict. urllib does the percent-
    decoding, which matters because an APP_SEED can contain reserved chars."""
    from urllib.parse import urlparse, unquote
    u = urlparse(url)
    if not u.scheme.startswith('postgres'):
        sys.exit('[rescue] ERROR: DATABASE_URL is not a postgres URL')
    return {
        'user': unquote(u.username or 'bankbridge'),
        'password': unquote(u.password or ''),
        'host': u.hostname or 'db',
        'port': u.port or 5432,
        'dbname': (u.path or '/bankbridge').lstrip('/') or 'bankbridge',
    }


db_url = os.environ.get('DATABASE_URL', '').strip()
if not db_url:
    sys.exit('[rescue] ERROR: DATABASE_URL is not set in this container')
cfg = parse_db_url(db_url)

APP_USER = cfg['user']
# The password the app authenticates with IS the current APP_SEED, so it is by
# definition the value the role must be reset to. Mirrors
# app/db_recovery.ensure_db_auth(), which rotates to url.password for the same
# reason. An explicit APP_SEED overrides, for the odd manual run.
target_pw = os.environ.get('APP_SEED', '').strip() or cfg['password']
if not target_pw:
    sys.exit('[rescue] ERROR: could not determine the target password — set '
             'APP_SEED=... and re-run')

rescue_user = (os.environ.get('DB_RESCUE_USER', '').strip() or 'bridgeadmin')
rescue_salt = (os.environ.get('DB_RESCUE_SALT', '').strip()
               or 'bankbridge-rescue-v1')
# Seed resolution mirrors config.py's DB_RESCUE_SEED exactly, so this script and
# the app derive the SAME password: DB_RESCUE_SEED → POSTGRES_PASSWORD →
# SECRET_KEY, falling back to the target password.
rescue_seed = (os.environ.get('DB_RESCUE_SEED', '').strip()
               or os.environ.get('POSTGRES_PASSWORD', '').strip()
               or os.environ.get('SECRET_KEY', '').strip()
               or target_pw)
# Must match app/db_recovery.rescue_password() and the pgcrypto derivation in
# scripts/initdb.d/10-create-rescue-superuser.sh byte for byte:
#   hex(HMAC-SHA256(key=APP_SEED, msg=salt))
rescue_pw = hmac.new(rescue_seed.encode(), rescue_salt.encode(),
                     hashlib.sha256).hexdigest()


def connect(user, password):
    return psycopg2.connect(host=cfg['host'], port=cfg['port'],
                            dbname=cfg['dbname'], user=user,
                            password=password, connect_timeout=5)


# Ordered superuser candidates. `bankbridge` first: POSTGRES_USER=bankbridge
# makes it the sole superuser on a stock install, and on the common
# "provision bridgeadmin before anything broke" run its password still works.
candidates = [(APP_USER, target_pw, 'app role with the current APP_SEED'),
              (rescue_user, rescue_pw, 'existing rescue superuser')]
explicit_su = os.environ.get('DB_SUPERUSER', '').strip()
if explicit_su:
    candidates.append((explicit_su,
                       os.environ.get('DB_SUPERUSER_PASSWORD', '').strip()
                       or target_pw, 'explicitly configured DB_SUPERUSER'))
# Trust-auth fallbacks (POSTGRES_HOST_AUTH_METHOD=trust): password ignored.
candidates.append((APP_USER, None, 'app role via trust auth'))
candidates.append((rescue_user, None, 'rescue user via trust auth'))

conn = None
used = ''
for user, pw, label in candidates:
    try:
        conn = connect(user, pw)
        used = '%s (%s)' % (user, label)
        break
    except psycopg2.OperationalError:
        continue

if conn is None:
    sys.exit(
        '[rescue] ERROR: could not authenticate to Postgres as any superuser '
        'candidate.\n'
        '[rescue] This means the "%s" password has ALREADY drifted and no '
        'rescue superuser exists yet.\n'
        '[rescue] Container mode cannot fix that — resetting a password '
        'requires logging in first.\n'
        '[rescue] Run it from the Umbrel HOST instead, where it can flip the '
        'db to trust auth:\n'
        '[rescue]     cd /path/to/erpnext-bank-bridge/app && sudo bash '
        'scripts/rotate_db_password.sh' % APP_USER)

log('connected as %s' % used)
conn.autocommit = True
try:
    with conn.cursor() as cur:
        cur.execute('SELECT rolsuper FROM pg_roles WHERE rolname = current_user')
        row = cur.fetchone()
        if not row or not row[0]:
            sys.exit('[rescue] ERROR: connected, but that role is not a '
                     'superuser — it cannot reset passwords or create roles. '
                     'Run from the host with --mode host.')

        # 1 · app role password → current APP_SEED. Identifier quoted via
        # sql.Identifier; the password rides as a bound parameter.
        cur.execute(sql.SQL('ALTER ROLE {} WITH PASSWORD %s').format(
            sql.Identifier(APP_USER)), (target_pw,))
        log("reset '%s' password to the current APP_SEED" % APP_USER)

        # 2 · rescue superuser — create if absent, then (re)set its password so
        # a pre-existing one carrying a stale password is corrected too.
        cur.execute('SELECT 1 FROM pg_roles WHERE rolname = %s', (rescue_user,))
        existed = cur.fetchone() is not None
        if not existed:
            cur.execute(sql.SQL('CREATE ROLE {} LOGIN SUPERUSER').format(
                sql.Identifier(rescue_user)))
        cur.execute(sql.SQL('ALTER ROLE {} WITH LOGIN SUPERUSER PASSWORD %s')
                    .format(sql.Identifier(rescue_user)), (rescue_pw,))
        log("%s rescue superuser '%s'"
            % ('updated existing' if existed else 'CREATED', rescue_user))
finally:
    conn.close()

# 3 · verify both credentials independently, on fresh connections. Without this
# the script could report success while the app still cannot log in.
failures = []
for user, pw, what in ((APP_USER, target_pw, 'app role'),
                       (rescue_user, rescue_pw, 'rescue superuser')):
    try:
        c = connect(user, pw)
        c.close()
        log('verified: %s "%s" can log in' % (what, user))
    except psycopg2.OperationalError as e:
        failures.append('%s "%s": %s' % (what, user, str(e).strip()))

if failures:
    sys.exit('[rescue] ERROR: post-change verification FAILED:\n[rescue]   '
             + '\n[rescue]   '.join(failures))

log('done — app role password matches APP_SEED and the rescue superuser is in '
    'place.')
log('future drift will now self-heal at boot (AUTO_RECOVER_DB_AUTH).')
log('NOTE: restart the Bank Bridge container so it reconnects with the reset '
    'password.')
PYEOF
  exit $?
fi

# ══════════════════════════════════════════════════════════════════════════
# HOST MODE — filesystem-level repair via docker (works even when auth is dead)
# ══════════════════════════════════════════════════════════════════════════
command -v docker >/dev/null 2>&1 || die \
  "host mode requires docker on PATH (re-run without --mode host to use container mode)"

# ── locate containers ────────────────────────────────────────────────────
if [ -z "${DB_CONTAINER}" ]; then
  DB_CONTAINER="$(docker ps --format '{{.Names}}' \
    | grep -Ei 'bank[-_]?bridge.*db|db.*bank[-_]?bridge|bankbridge_db' \
    | head -n1 || true)"
fi
[ -n "${DB_CONTAINER}" ] || die "could not auto-detect the postgres container; pass it as arg 1"

if [ -z "${SERVER_CONTAINER}" ]; then
  SERVER_CONTAINER="$(docker ps --format '{{.Names}}' \
    | grep -Ei 'bank[-_]?bridge.*server|bankbridge$|bank[-_]?bridge$' \
    | grep -viE 'db' | head -n1 || true)"
fi
log "db container:     ${DB_CONTAINER}"
log "server container: ${SERVER_CONTAINER:-<none detected>}"

# ── discover the target password (current APP_SEED) ──────────────────────
TARGET_PW="${APP_SEED:-}"
if [ -z "${TARGET_PW}" ] && [ -n "${SERVER_CONTAINER}" ]; then
  DB_URL="$(docker exec "${SERVER_CONTAINER}" printenv DATABASE_URL 2>/dev/null || true)"
  # postgresql://bankbridge:<PW>@db:5432/bankbridge  → extract <PW>
  TARGET_PW="$(printf '%s' "${DB_URL}" | sed -n 's#^[^:]*://[^:]*:\(.*\)@.*#\1#p')"
  [ -n "${TARGET_PW}" ] || TARGET_PW="$(docker exec "${SERVER_CONTAINER}" printenv SECRET_KEY 2>/dev/null || true)"
fi
[ -n "${TARGET_PW}" ] || die "could not determine APP_SEED; set APP_SEED=... and re-run"
log "resolved target password from the running config (value hidden)"

PGDATA="$(docker exec "${DB_CONTAINER}" sh -c 'echo "${PGDATA:-/var/lib/postgresql/data}"')"
HBA="${PGDATA}/pg_hba.conf"

reload_db() { docker exec -u postgres "${DB_CONTAINER}" pg_ctl reload -D "${PGDATA}" >/dev/null; }

restore_hba() {
  log "restoring original pg_hba.conf"
  docker exec -u postgres "${DB_CONTAINER}" sh -c \
    "[ -f '${HBA}.rescue-bak' ] && mv -f '${HBA}.rescue-bak' '${HBA}'" || \
      log "WARNING: pg_hba restore may have failed — inspect ${HBA}"
  reload_db || log "WARNING: reload after restore failed — inspect the db container"
}
trap restore_hba EXIT

# ── 1 · enable trust auth on the container's connections ─────────────────
log "enabling temporary trust auth"
docker exec -u postgres "${DB_CONTAINER}" sh -c "
  set -e
  cp -p '${HBA}' '${HBA}.rescue-bak'
  { echo 'local all all trust'
    echo 'host  all all 127.0.0.1/32 trust'
    echo 'host  all all ::1/128      trust'
    echo 'host  all all all          trust'
    cat '${HBA}.rescue-bak'; } > '${HBA}'
"
reload_db

# ── 2 · reset bankbridge + create the rescue superuser ───────────────────
# SQL body via stdin (docker exec -i); psql vars via -v on argv so no in-
# container shell quoting is needed. Rescue password is derived server-side with
# pgcrypto to match app/db_recovery.rescue_password(): hmac(msg=salt, key=seed).
log "resetting 'bankbridge' password and ensuring rescue superuser '${RESCUE_USER}'"
docker exec -i -u postgres "${DB_CONTAINER}" \
  psql -v ON_ERROR_STOP=1 -U bankbridge -d bankbridge \
  -v tpw="${TARGET_PW}" -v ruser="${RESCUE_USER}" -v rsalt="${RESCUE_SALT}" <<'SQL'
CREATE EXTENSION IF NOT EXISTS pgcrypto;
ALTER ROLE bankbridge WITH PASSWORD :'tpw';
SELECT format('CREATE ROLE %I LOGIN SUPERUSER', :'ruser')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ruser')
\gexec
SELECT format('ALTER ROLE %I WITH LOGIN SUPERUSER PASSWORD %L', :'ruser',
              encode(hmac(:'rsalt', :'tpw', 'sha256'), 'hex'))
\gexec
SQL

# ── 3 · restore auth (also via trap) and restart the server ──────────────
trap - EXIT
restore_hba

if [ -n "${SERVER_CONTAINER}" ]; then
  log "restarting server container to force a clean reconnect"
  docker restart "${SERVER_CONTAINER}" >/dev/null || log "WARNING: could not restart ${SERVER_CONTAINER}; restart it manually"
fi

log "done — 'bankbridge' password reset and rescue superuser '${RESCUE_USER}' in place."
log "future password drift will now self-heal at boot (AUTO_RECOVER_DB_AUTH)."
