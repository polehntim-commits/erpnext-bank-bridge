#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Manual DB-auth rescue for EXISTING Bank Bridge installs (v0.3.5, Option C).
#
# Use this ONCE if the app can't reach Postgres with:
#   psycopg2.OperationalError: password authentication failed for user "bankbridge"
# on an install whose volume was created BEFORE the deterministic rescue
# superuser existed (so boot auto-recovery has no superuser to reach).
#
# What it does, all on the Umbrel host via `docker exec` (no volume wipe):
#   1. Reads the current APP_SEED from the server container's DATABASE_URL.
#   2. Temporarily flips the db to `trust` auth (passwordless) and reloads —
#      no restart, no downtime for existing connections.
#   3. Resets the `bankbridge` role password to the current APP_SEED, AND creates
#      the `bridgeadmin` rescue superuser (so future drift self-heals).
#   4. Restores the original pg_hba.conf and reloads.
#   5. Restarts the server container so it reconnects cleanly.
#
# Conservative: backs up pg_hba.conf, runs under `set -e`, and always restores
# auth via an EXIT trap even if a step fails. All db file ops run as the
# `postgres` OS user so file ownership/permissions are preserved. Review before
# running. Requires: bash, docker, and permission to exec into the containers.
#
# Usage:
#   ./rotate_db_password.sh [DB_CONTAINER] [SERVER_CONTAINER]
# If the container names are omitted it auto-detects them by name.
set -euo pipefail

DB_CONTAINER="${1:-}"
SERVER_CONTAINER="${2:-}"
RESCUE_USER="${DB_RESCUE_USER:-bridgeadmin}"
RESCUE_SALT="${DB_RESCUE_SALT:-bankbridge-rescue-v1}"

log() { printf '[rescue] %s\n' "$*" >&2; }
die() { printf '[rescue] ERROR: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker not found on PATH"

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
SELECT format('ALTER ROLE %I WITH PASSWORD %L', :'ruser',
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
