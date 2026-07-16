#!/bin/sh
# SPDX-License-Identifier: MIT
#
# v0.3.5 — create a deterministic RESCUE superuser at first volume init.
#
# Mounted into the postgres container's /docker-entrypoint-initdb.d/, so it runs
# EXACTLY ONCE, only on a fresh data volume (that is the entrypoint's contract).
# Existing installs never run this — they repair via scripts/rotate_db_password.sh.
#
# Why: the compose sets POSTGRES_USER=bankbridge, making `bankbridge` the SOLE
# superuser. If its password ever drifts from APP_SEED, nothing can log in to
# reset it. This creates a SECOND superuser, `bridgeadmin`, whose password is
# derived deterministically as HMAC-SHA256(key=APP_SEED, msg=salt). The app
# re-derives the same value at boot (app/db_recovery.rescue_password) to log in
# and repair a drifted `bankbridge` password — no volume wipe.
#
# The derivation MUST match app/db_recovery.rescue_password():
#   password = hex( HMAC-SHA256(key = APP_SEED, msg = salt) )
# Computed here server-side via pgcrypto (bundled with the postgres image) so we
# don't depend on openssl being present. pgcrypto's hmac(data, key, type) takes
# the MESSAGE first and the KEY second, hence hmac(:salt, :seed, 'sha256').
set -eu

RESCUE_USER="${DB_RESCUE_USER:-bridgeadmin}"
RESCUE_SALT="${DB_RESCUE_SALT:-bankbridge-rescue-v1}"
# Seed = APP_SEED. The compose sets POSTGRES_PASSWORD to APP_SEED on the db
# container; allow an explicit override via DB_RESCUE_SEED.
RESCUE_SEED="${DB_RESCUE_SEED:-${POSTGRES_PASSWORD:-}}"

if [ -z "${RESCUE_SEED}" ]; then
  echo "[rescue-user] DB_RESCUE_SEED / POSTGRES_PASSWORD is empty — skipping" >&2
  exit 0
fi

# Derive the password server-side. -tA => tuples-only, unaligned (bare value).
RESCUE_PW="$(psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" -tAc \
  "CREATE EXTENSION IF NOT EXISTS pgcrypto;
   SELECT encode(hmac('${RESCUE_SALT}', '${RESCUE_SEED}', 'sha256'), 'hex');")"

if [ -z "${RESCUE_PW}" ]; then
  echo "[rescue-user] failed to derive rescue password — skipping" >&2
  exit 1
fi

# Create the role if missing, then (re)set its password. Identifiers are quoted
# with %I / :"var"; the password is passed as a psql literal via :'var'.
psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" \
  -v user="${RESCUE_USER}" -v pw="${RESCUE_PW}" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN SUPERUSER', :'user')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'user')
\gexec
ALTER ROLE :"user" WITH PASSWORD :'pw';
SQL

echo "[rescue-user] ensured rescue superuser '${RESCUE_USER}'"
