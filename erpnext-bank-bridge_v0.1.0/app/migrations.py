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


def _column(table: str, column: str) -> dict | None:
    """The live column info dict for <table>.<column>, or None if either the
    table or the column is absent."""
    insp = inspect(db.engine)
    if table not in insp.get_table_names():
        return None
    for c in insp.get_columns(table):
        if c['name'] == column:
            return c
    return None


def _widen_column(table: str, column: str, target_ddl: str,
                  min_length: int) -> None:
    """Widen <table>.<column> to <target_ddl> when its current declared length
    is smaller than <min_length>. No-op on SQLite (which doesn't enforce VARCHAR
    length, so a fresh test DB is already correct via the model) and when the
    column is already unbounded (TEXT → declared length is None) or wide enough.
    Idempotent: once widened, the length check short-circuits on the next boot."""
    if db.engine.dialect.name == 'sqlite':
        return
    col = _column(table, column)
    if col is None:
        return
    length = getattr(col['type'], 'length', None)
    if length is not None and length < min_length:
        with db.engine.begin() as conn:
            conn.execute(text(
                f'ALTER TABLE {table} ALTER COLUMN {column} TYPE {target_ddl}'))
        log.info('migration: widened %s.%s to %s', table, column, target_ddl)


def _drop_not_null(table: str, column: str) -> None:
    """Drop a NOT NULL constraint from <table>.<column> so a batch action can
    log with a NULL/'' item_id. No-op on SQLite and when the column is already
    nullable. Idempotent."""
    if db.engine.dialect.name == 'sqlite':
        return
    col = _column(table, column)
    if col is None or col.get('nullable', True):
        return
    with db.engine.begin() as conn:
        conn.execute(text(
            f'ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL'))
    log.info('migration: made %s.%s nullable', table, column)


def run_migrations() -> None:
    """Apply all pending additive migrations. Call inside an app context, right
    after db.create_all(). Safe to call on every boot."""
    try:
        # v0.1.1 — one-click account import: track which Plaid accounts have
        # been auto-provisioned into ERPNext.
        _add_column_if_missing('plaid_accounts', 'import_status',
                               'VARCHAR(20)', default_sql="'pending'")
        # v0.1.2 — the account-import audit line logs direction
        # 'erpnext_account_import' (22 chars), which overflows the original
        # VARCHAR(20) and made the commit fail on Postgres (the log row silently
        # never persisted). Widen direction, guarantee error_message is TEXT so
        # a captured Frappe traceback/body fits, and drop item_id's NOT NULL so
        # a batch action can log without a single owning item.
        _widen_column('plaid_sync_log', 'direction', 'VARCHAR(64)', 64)
        _widen_column('plaid_sync_log', 'error_message', 'TEXT', 10 ** 9)
        _drop_not_null('plaid_sync_log', 'item_id')
        # v0.2.0 — auto-create the matching GL Account in ERPNext's Chart of
        # Accounts so an imported company Bank Account can link a real `account`.
        # Record the created/linked GL Account docname on the Plaid account.
        _add_column_if_missing('plaid_accounts', 'erpnext_gl_account_name', 'TEXT')
        # v0.3.0 — auto-Supplier creation + rules-based Journal Entry generation
        # add three NEW tables (suppliers, categorization_rules,
        # generated_journal_entries) + one more (audit_events). New tables are
        # created by db.create_all() (which runs just before this), so they need
        # no step here. The additive COLUMNS below are only for a database that
        # already built categorization_rules / plaid_sync_log under an earlier
        # v0.3.0 build — on a fresh DB create_all already has them, so each check
        # short-circuits.
        #   * non-destructive rule history (supersede/archive)
        _add_column_if_missing('categorization_rules', 'superseded_by', 'INTEGER')
        _add_column_if_missing('categorization_rules', 'archived', 'BOOLEAN',
                               default_sql='false')
        #   * audit cross-link from the HTTP-level sync log to an AuditEvent subject
        _add_column_if_missing('plaid_sync_log', 'subject_id', 'VARCHAR(120)')
    except Exception:  # pragma: no cover - never block boot on a migration
        log.warning('schema migration failed; continuing', exc_info=True)
