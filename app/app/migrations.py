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


# Every additive column an upgrade has introduced, oldest first, as
# (table, column, ddl). `ddl` is the full definition that follows the column
# name — a type plus any DEFAULT, e.g. 'VARCHAR(140)' or 'BOOLEAN DEFAULT
# false'. Each is applied by _add_column_if_missing (an inspected, idempotent
# ADD COLUMN) in run_migrations, BEFORE any data backfill runs.
#
# To ship a new additive column: add the db.Column to the model AND append one
# line here. That's the whole migration — no Alembic, no revision files.
SCHEMA_MIGRATIONS: list[tuple[str, str, str]] = [
    # v0.1.1 — one-click account import: track which Plaid accounts have been
    # auto-provisioned into ERPNext.
    ('plaid_accounts', 'import_status', "VARCHAR(20) DEFAULT 'pending'"),
    # v0.2.0 — the created/linked GL Account docname for an imported company
    # Bank Account.
    ('plaid_accounts', 'erpnext_gl_account_name', 'TEXT'),
    # v0.3.0 — non-destructive rule history (supersede/archive).
    ('categorization_rules', 'superseded_by', 'INTEGER'),
    ('categorization_rules', 'archived', 'BOOLEAN DEFAULT false'),
    # v0.3.0 — audit cross-link from the HTTP-level sync log to an AuditEvent.
    ('plaid_sync_log', 'subject_id', 'VARCHAR(120)'),
    # v0.3.1 — bank-account-agnostic rules: a rule names only the offset
    # (categorized) side; the bank side comes from the transaction's account.
    ('categorization_rules', 'offset_account', "VARCHAR(255) DEFAULT ''"),
    ('categorization_rules', 'offset_direction', "VARCHAR(20) DEFAULT 'auto'"),
    # v0.4.0 — multi-entity L1: the owning ERPNext Company, chosen at Plaid Link
    # time on the Item and inherited (overridable) per account. Both backfill to
    # NULL on an existing install, which the push path resolves to the ERPNext
    # default Company — so v0.3.9 installs keep working with no manual step.
    ('plaid_items', 'owning_company', 'VARCHAR(140)'),
    ('plaid_accounts', 'owning_company', 'VARCHAR(140)'),
    # v0.4.0 — balance-only investment support: flag Plaid investment accounts
    # so the sync loop skips /transactions/sync for them. Backfills to false;
    # the flag is re-derived from type/subtype on the next account refresh.
    ('plaid_accounts', 'balance_only', 'BOOLEAN DEFAULT false'),
]


def _add_column_if_missing(table: str, column: str, ddl: str) -> None:
    """ADD COLUMN <column> <ddl> on <table> when it isn't already there. <ddl>
    is the full definition after the column name (type plus any DEFAULT).

    Portable across Postgres (production) and SQLite (tests): both support the
    plain `ALTER TABLE … ADD COLUMN` form used here. The "already there?" guard
    is a SQLAlchemy-inspector check rather than a Postgres-only `ADD COLUMN IF
    NOT EXISTS`, precisely so the same code path is a no-op on SQLite (which has
    no IF NOT EXISTS on ADD COLUMN)."""
    insp = inspect(db.engine)
    if table not in insp.get_table_names():
        return  # fresh DB — create_all already built the full schema
    cols = {c['name'] for c in insp.get_columns(table)}
    if column in cols:
        return
    with db.engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
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
    after db.create_all(). Safe to call on every boot.

    Ordering is load-bearing and must not be shuffled:

      1. ADD every column in SCHEMA_MIGRATIONS.
      2. Widen/relax types on existing columns (v0.1.2).
      3. Run ORM-based data backfills LAST.

    Step 3 is last because a backfill issues ORM queries (e.g.
    ``PlaidAccount.query``) whose generated SELECT lists *every* column the
    model declares — including columns added in step 1. Running a backfill
    before its columns exist raises UndefinedColumn on an upgrading database,
    and because the whole body is wrapped in a fail-open ``except`` that error
    would abort the remaining ADDs, leaving the schema half-migrated. (That was
    the v0.4.0 → v0.4.0.1 regression: the offset backfill ran before the
    owning_company/balance_only ADDs and swallowed them, so every later
    ``PlaidItem.query`` 500'd with ``column plaid_items.owning_company does not
    exist``.) Keep all ADDs ahead of all backfills."""
    try:
        # 1. Additive columns — idempotent, no-op once present or on a fresh DB.
        for table, column, ddl in SCHEMA_MIGRATIONS:
            _add_column_if_missing(table, column, ddl)
        # 2. In-place type/constraint fixes (v0.1.2): the account-import audit
        # line logs direction 'erpnext_account_import' (22 chars), which
        # overflows the original VARCHAR(20) and made the commit fail on
        # Postgres (the log row silently never persisted). Widen direction,
        # guarantee error_message is TEXT so a captured Frappe traceback/body
        # fits, and drop item_id's NOT NULL so a batch action can log without a
        # single owning item.
        _widen_column('plaid_sync_log', 'direction', 'VARCHAR(64)', 64)
        _widen_column('plaid_sync_log', 'error_message', 'TEXT', 10 ** 9)
        _drop_not_null('plaid_sync_log', 'item_id')
        # 3. ORM-based data backfills — MUST run after every ADD above, since
        # they SELECT models whose columns those ADDs create. v0.3.1: give every
        # pre-v0.3.1 rule a single offset_account from its debit/credit pair.
        _migrate_rule_offset_accounts()
    except Exception:  # pragma: no cover - never block boot on a migration
        log.warning('schema migration failed; continuing', exc_info=True)


def _migrate_rule_offset_accounts() -> None:
    """One-time backfill (v0.3.1): give every pre-v0.3.1 rule (which named a
    debit/credit pair) a single `offset_account`. Prefer whichever side is NOT a
    known bank GL account (the bank side now comes from the transaction); if
    neither/both look like a bank account, fall back to `debit_account`. Sets
    `offset_direction='auto'` when unset. Idempotent: only touches rules whose
    `offset_account` is still blank, so re-running (and a fresh DB, where there
    are no legacy rules) is a no-op."""
    insp = inspect(db.engine)
    if 'categorization_rules' not in insp.get_table_names():
        return
    cols = {c['name'] for c in insp.get_columns('categorization_rules')}
    if not {'offset_account', 'debit_account', 'credit_account'} <= cols:
        return  # columns not all present yet (shouldn't happen post-ADD)
    from .models import CategorizationRule, PlaidAccount
    # The set of GL accounts that ARE bank accounts (so we can pick the other
    # side as the offset). Empty on a fresh install → we just fall back to debit.
    bank_gls = {(a.erpnext_gl_account_name or '').strip()
                for a in PlaidAccount.query.filter(
                    PlaidAccount.erpnext_gl_account_name.isnot(None)).all()}
    bank_gls.discard('')
    rules = CategorizationRule.query.filter(
        (CategorizationRule.offset_account.is_(None))
        | (CategorizationRule.offset_account == '')).all()
    changed = 0
    for r in rules:
        debit = (r.debit_account or '').strip()
        credit = (r.credit_account or '').strip()
        if not debit and not credit:
            continue  # nothing to migrate (a v0.3.1-native rule with no offset)
        if credit in bank_gls and debit and debit not in bank_gls:
            offset = debit
        elif debit in bank_gls and credit and credit not in bank_gls:
            offset = credit
        else:
            offset = debit or credit
        r.offset_account = offset
        if not (r.offset_direction or '').strip():
            r.offset_direction = 'auto'
        changed += 1
    if changed:
        db.session.commit()
        log.info('migration: backfilled offset_account on %d rule(s)', changed)
