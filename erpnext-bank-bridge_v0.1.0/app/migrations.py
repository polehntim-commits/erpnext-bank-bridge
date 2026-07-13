# SPDX-License-Identifier: MIT
"""Framework-free, idempotent schema migrations, run once at boot.

`db.create_all()` creates any MISSING TABLE but never adds a new COLUMN to a
table that already exists — so every additive column an upgrade introduces
ships here as an inspected `ALTER TABLE … ADD COLUMN`. Each step first checks
the live schema (via SQLAlchemy's inspector) and only acts when the column is
absent, so re-running on an already-migrated database is a no-op and a fresh
database (where create_all already built the full model) skips every step.

Kept deliberately tiny and dependency-light (no Alembic) to match the app's
single-container, Postgres-only deployment. Failures are logged but never block
boot: a genuinely broken migration surfaces later as an obvious import error
rather than a silent half-upgrade."""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from . import db

log = logging.getLogger('bankbridge.migrations')


def _add_column_if_missing(table: str, column: str, ddl_type: str,
                           default_sql: str | None = None) -> None:
    """ADD COLUMN <column> <ddl_type> [DEFAULT <default_sql>] on <table> when it
    isn't already there. Portable across Postgres (production) and SQLite
    (tests): both support the plain `ALTER TABLE … ADD COLUMN` form used here."""
    insp = inspect(db.engine)
    if table not in insp.get_table_names():
        return  # fresh DB — create_all already built the full schema
    cols = {c['name'] for c in insp.get_columns(table)}
    if column in cols:
        return
    ddl = f'ALTER TABLE {table} ADD COLUMN {column} {ddl_type}'
    if default_sql is not None:
        ddl += f' DEFAULT {default_sql}'
    with db.engine.begin() as conn:
        conn.execute(text(ddl))
    log.info('migration: added %s.%s', table, column)


def run_migrations() -> None:
    """Apply all pending additive migrations. Call inside an app context, right
    after db.create_all(). Safe to call on every boot."""
    try:
        # v0.1.1 — one-click account import: track which Plaid accounts have
        # been auto-provisioned into ERPNext.
        _add_column_if_missing('plaid_accounts', 'import_status',
                               'VARCHAR(20)', default_sql="'pending'")
    except Exception:  # pragma: no cover - never block boot on a migration
        log.warning('schema migration failed; continuing', exc_info=True)
