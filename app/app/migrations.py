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
    # v0.4.0.1 — multi-entity rule scoping: a rule may be restricted to one
    # owning Company. NULL = company-agnostic (applies everywhere), so existing
    # rules are unaffected.
    ('categorization_rules', 'applies_to_company', 'VARCHAR(140)'),
    # v0.4.0.7 — omit the Party from a rule's generated JE (transfers between
    # accounts you own). Backfills to false, so every existing rule keeps
    # naming its party exactly as before.
    ('categorization_rules', 'skip_party', 'BOOLEAN DEFAULT false'),
    # v0.4.0.8 — sell-side support. `party_type` has been on the model since
    # v0.3.0, so on any install it already exists and this line is a no-op; it
    # is listed for completeness now that v0.4.0.8 gives the column a fourth
    # value ('Auto') and makes it load-bearing. Backfills to NULL = no Party,
    # which is exactly what a pre-v0.4.0.8 rule without one already meant.
    ('categorization_rules', 'party_type', 'VARCHAR(20)'),
    # v0.4.1 — intercompany transfer detection. The `intercompany_transfer_pairs`
    # TABLE itself needs no line here (create_all builds a missing table); these
    # three are the additive columns on tables that already exist.
    #
    # Both pair links backfill to NULL = "not part of an intercompany transfer",
    # which is true of every transaction and every JE on an existing install, so
    # nothing about a v0.4.0.9 database changes behaviour on upgrade.
    ('bank_transactions', 'intercompany_pair_id', 'INTEGER'),
    ('generated_journal_entries', 'intercompany_pair_id', 'INTEGER'),
    # NOTE the DEFAULT true, which is the opposite of every other boolean here:
    # an existing rule must NOT start firing on paired transactions the moment
    # this feature ships. Backfilling to false would leave a generic "Transfer"
    # rule booking one leg of every intercompany move to P&L — exactly the bug
    # v0.4.1 exists to fix — so the safe backfill is the protective value.
    ('categorization_rules', 'ignore_for_paired', 'BOOLEAN DEFAULT true'),
    # v0.4.4 — opening balances. Points at the GeneratedJournalEntry booking what
    # the account already held when it was linked (see app/opening_balance.py).
    # Backfills to NULL = "no opening balance booked", which is true of every
    # account on a pre-v0.4.4 install; scripts/backfill_opening_balances.py is
    # the one-shot that estimates and books them.
    ('plaid_accounts', 'opening_balance_je_id', 'INTEGER'),
    # v0.4.6 — cached per-rule match count for the Rules list. Backfills to 0,
    # which reads as "not rolled up yet" rather than "never matched"; the first
    # run of the daily rollup (or the manual trigger on the Rules page) fills in
    # the real numbers from the existing generated_journal_entries rows, so no
    # history is lost on upgrade.
    ('categorization_rules', 'match_count', 'INTEGER DEFAULT 0'),
    # v0.4.7 — operator-initiated disconnect (Plaid /item/remove). Backfills to
    # false + NULL, i.e. "still linked", which is true of every Item on an
    # existing install, so an upgrade changes nothing about what syncs. NOT NULL
    # is safe on the ADD because the DEFAULT fills every existing row in the same
    # statement (and it keeps a migrated schema identical to the one create_all
    # builds on a fresh database).
    ('plaid_items', 'disconnected', 'BOOLEAN DEFAULT false NOT NULL'),
    ('plaid_items', 'disconnected_at', 'TIMESTAMP'),
    # v0.4.9 — bank statements. The `plaid_statements` TABLE needs no line here:
    # create_all builds a missing table, and this release adds no column to any
    # table that already exists (the same situation as v0.4.1's
    # intercompany_transfer_pairs above). An upgrading install therefore gains
    # one empty table and changes no existing row — statements only ever ADD
    # information, so there is nothing to backfill and nothing to undo.
    #
    # v0.4.10 — the ERPNext Bank Statement upload. UNLIKE v0.4.9 these DO need a
    # line each: an install that ran v0.4.9 already has `plaid_statements`, so
    # create_all() will not touch it and the two new columns would be missing.
    # Both backfill to NULL, which the sync pass reads as "not uploaded yet" —
    # so every statement an install already holds is picked up on the next
    # scheduler tick with no manual step.
    ('plaid_statements', 'erpnext_docname', 'VARCHAR(255)'),
    ('plaid_statements', 'erpnext_synced_at', 'TIMESTAMP'),
    # v0.4.11 — reconnect safety. `needs_reauth` gets a NOT NULL default so the
    # sync loop's filter can treat it as a plain boolean on an upgrading
    # database rather than having to spell out `IS NOT TRUE` for legacy NULLs
    # (the lesson `disconnected` learned in v0.4.7 — see sync_all's comment).
    ('plaid_items', 'needs_reauth', 'BOOLEAN DEFAULT false NOT NULL'),
    ('plaid_items', 'reauth_reason', 'VARCHAR(255)'),
    ('plaid_items', 'reauth_detected_at', 'TIMESTAMP'),
    # Backfills to NULL = "this account was never superseded", which is true of
    # every account on an existing install.
    ('plaid_accounts', 'superseded_by_account_id', 'VARCHAR(120)'),
    # v0.4.12 — investment mark-to-market. Both backfill to NULL, which reads as
    # "never revalued": the first pass then SEEDS the baseline from the booked
    # opening balance and posts nothing, so an upgrading install cannot have the
    # whole account value booked as a fictional one-off gain.
    ('plaid_accounts', 'last_revalued_balance', 'DOUBLE PRECISION'),
    ('plaid_accounts', 'last_revalued_at', 'TIMESTAMP'),
    # v0.4.14 — loans as liabilities. All backfill to NULL: an existing install
    # has no loan accounts imported at all (they were refused until now), and a
    # NULL ytd baseline means "seed, don't post" — so the first sync after the
    # upgrade cannot book a year of interest as one entry.
    ('plaid_accounts', 'liability_detail', 'TEXT'),
    ('plaid_accounts', 'liability_refreshed_at', 'TIMESTAMP'),
    ('plaid_accounts', 'loan_ytd_interest_booked', 'DOUBLE PRECISION'),
    ('plaid_accounts', 'loan_ytd_principal_seen', 'DOUBLE PRECISION'),
    # v0.4.28 — investments sync timestamp. Backfills to NULL = "never
    # pulled investments for this Item", which lets sync_investments_for_item
    # take the initial-backfill branch (730-day window) rather than a delta
    # window on an upgrading install where the column didn't exist yet.
    ('plaid_items', 'investments_synced_at', 'TIMESTAMP'),
    # v0.4.41 — richer statement parsing. All five backfill to NULL/''/false,
    # i.e. "this row was parsed by an older recognizer and says nothing about
    # portfolio value or its own trustworthiness" — which is exactly true. The
    # v0.4.40 balances stay put until an operator re-parses from
    # /admin/statements, so an upgrade cannot silently move a number that a
    # posted opening balance was anchored on.
    ('plaid_statements', 'portfolio_opening_value', 'DOUBLE PRECISION'),
    ('plaid_statements', 'portfolio_closing_value', 'DOUBLE PRECISION'),
    ('plaid_statements', 'parse_method', "VARCHAR(40) DEFAULT ''"),
    ('plaid_statements', 'parse_suspect', 'BOOLEAN DEFAULT false'),
    # JSONB, not TEXT: the blob is queryable (`parsed_metadata->>'layout'`,
    # `parsed_metadata->>'parser_version'`) without unpacking it in Python,
    # which is the whole reason a re-parse can target only the rows a newer
    # recognizer hasn't reached. Backfills to NULL = "parsed before v0.4.41
    # recorded anything about how", which is exactly true.
    ('plaid_statements', 'parsed_metadata', 'JSONB'),
    # v0.4.43 — statement-anchored reconciliation. The `statement_anchors`
    # TABLE needs no line here: create_all() builds a missing table, and this
    # release adds no column to a table that already exists (the same situation
    # as v0.4.1's intercompany_transfer_pairs and v0.4.9's plaid_statements).
    # An upgrading install gains one empty table and changes no existing row —
    # anchors are DERIVED from statements that are already held, so the first
    # run of rebuild_statement_anchors fills the whole history with nothing to
    # backfill and nothing to undo.
    #
    # v0.4.44 — account pairing. Both backfill to NULL, which reads as "not
    # paired" and "this statement printed no cash-services number" — true of
    # every row on an existing install, so an upgrade changes no reconciliation
    # until the next re-parse detects a pair and rebuilds the anchors.
    ('plaid_accounts', 'paired_account_id', 'VARCHAR(120)'),
    ('plaid_statements', 'cash_services_account_number', 'VARCHAR(40)'),
    # v0.4.49 — Bank-Bridge-internal attribution tags. Both backfill to '',
    # i.e. "no tag", which is true of every rule and every transaction on an
    # existing install. The tag is never sent to ERPNext (see the columns'
    # docstrings); an upgrade adds two empty columns and changes no behaviour
    # until an operator sets a tag on a rule and backfills.
    ('categorization_rules', 'bb_internal_tag', "TEXT DEFAULT ''"),
    ('bank_transactions', 'bb_internal_tag', "TEXT DEFAULT ''"),
    # v0.5.1 — Phase D: investment transactions posted as Journal Entries.
    # The kill switch defaults FALSE so an upgrade posts NOTHING until the
    # operator opts an Item in — these are real P&L entries. The investment-id
    # column is the idempotency key for an investment JE; NULL on every
    # existing (bank-side) row, so an upgrade changes no behaviour.
    ('plaid_items', 'invest_je_posting_enabled', 'BOOLEAN DEFAULT false NOT NULL'),
    ('generated_journal_entries', 'plaid_investment_transaction_id',
     'VARCHAR(120)'),
]

# Additive UNIQUE indexes an upgrade introduces, as (index_name, table,
# column). Kept separate from SCHEMA_MIGRATIONS because a column ADD and its
# index are two statements; create_all() builds them together on a fresh DB, so
# these only matter when the column was just added to an existing table.
SCHEMA_INDEXES: list[tuple[str, str, str]] = [
    # v0.5.1 · the investment-JE idempotency key. UNIQUE so a re-sync that
    # tried to insert a second JE for the same trade fails loudly rather than
    # double-posting.
    ('ux_gje_plaid_investment_txn', 'generated_journal_entries',
     'plaid_investment_transaction_id'),
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


def _add_unique_index_if_missing(index_name: str, table: str,
                                 column: str) -> None:
    """CREATE UNIQUE INDEX <index_name> ON <table>(<column>) when it isn't
    already present and the column exists.

    Portable across Postgres and SQLite (both support `CREATE UNIQUE INDEX IF
    NOT EXISTS`, and the inspector guard keeps it a clean no-op either way).
    Runs AFTER the column ADDs, so the column it indexes is guaranteed present
    on both a fresh DB (create_all) and an upgrading one (the ADD above)."""
    insp = inspect(db.engine)
    if table not in insp.get_table_names():
        return
    cols = {c['name'] for c in insp.get_columns(table)}
    if column not in cols:
        return  # its column ADD did not happen (shouldn't occur post-ADD)
    existing = {ix['name'] for ix in insp.get_indexes(table)}
    if index_name in existing:
        return
    with db.engine.begin() as conn:
        conn.execute(text(
            f'CREATE UNIQUE INDEX IF NOT EXISTS {index_name} '
            f'ON {table} ({column})'))
    log.info('migration: added unique index %s on %s.%s', index_name, table,
             column)


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
        # Additive unique indexes — after every column ADD, since each indexes
        # a column the loop above guarantees exists (v0.5.1).
        for index_name, table, column in SCHEMA_INDEXES:
            _add_unique_index_if_missing(index_name, table, column)
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
        # v0.4.0.3: the new 'skipped_missing_account' JE state (23 chars)
        # overflows the original generated_journal_entries.state VARCHAR(20) and
        # would make the INSERT fail on Postgres — widen it to 40.
        _widen_column('generated_journal_entries', 'state', 'VARCHAR(40)', 40)
        # 3. ORM-based data backfills — MUST run after every ADD above, since
        # they SELECT models whose columns those ADDs create. v0.3.1: give every
        # pre-v0.3.1 rule a single offset_account from its debit/credit pair.
        _migrate_rule_offset_accounts()
        # v0.4.0.3: convert legacy Company-agnostic rules' pinned, fully-qualified
        # offset into a logical account name (Mode B). Runs after the v0.3.1
        # backfill so a rule that just gained its offset_account is considered too.
        _migrate_agnostic_offset_to_logical()
        # v0.4.0.9: clear party_type off rules whose offset account ERPNext will
        # not accept a party on. Runs last — it reads each rule's FINAL
        # offset_account, so it must see whatever the two backfills above wrote.
        _migrate_incompatible_party_types()
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


def _migrate_agnostic_offset_to_logical() -> None:
    """One-time backfill (v0.4.0.3): convert a Company-agnostic rule's
    fully-qualified `offset_account` (a legacy value pinned to one Company's chart
    by the v0.4.0.2 dropdown) into a LOGICAL account name. Agnostic rules are now
    Mode B — their offset resolves to each transaction's own Company at JE time —
    so a pinned 'Meals & Entertainment - BBT' would fail to resolve for every
    other Company. Only ACTIVE, non-archived, agnostic (applies_to_company
    NULL/'') rules are touched; SCOPED rules keep their specific offset.

    Idempotent by construction: categorization.logical_account_name is a fixed
    point, so a rule whose offset is already logical (logical(offset) == offset)
    is left untouched — including a name that legitimately contains ' - ' (e.g.
    'Owner - Draws'), which the uppercase-abbr heuristic won't over-strip. Safe on
    a fresh DB (no legacy rules) and when the column is absent."""
    insp = inspect(db.engine)
    if 'categorization_rules' not in insp.get_table_names():
        return
    cols = {c['name'] for c in insp.get_columns('categorization_rules')}
    if not {'offset_account', 'applies_to_company', 'active', 'archived'} <= cols:
        return  # columns not all present yet (shouldn't happen post-ADD)
    from .categorization import logical_account_name
    from .models import CategorizationRule
    rules = CategorizationRule.query.filter(
        CategorizationRule.active.is_(True),
        CategorizationRule.archived.is_(False)).all()
    changed = 0
    for r in rules:
        if (r.applies_to_company or '').strip():
            continue  # scoped (Mode A) — offset stays fully-qualified
        offset = (r.offset_account or '').strip()
        if not offset:
            continue
        logical = logical_account_name(offset)
        if logical and logical != offset:
            r.offset_account = logical
            changed += 1
    if changed:
        db.session.commit()
        log.info('migration: converted %d agnostic rule offset(s) to logical '
                 'account name(s)', changed)


def _migrate_incompatible_party_types() -> None:
    """One-shot backfill (v0.4.0.9): clear `party_type` off any rule whose OFFSET
    ACCOUNT is one ERPNext will not accept a Party on.

    THE BUG THIS REPAIRS: v0.4.0.8's party_type='Auto' derivation keyed on the
    offset account's root_type alone — Income → Customer, Expense → Supplier —
    and the Rules editor happily stored a literal Supplier/Customer against any
    account at all. But ERPNext enforces the FINER `account_type`:

        ValidationError: Party Type and Party can only be set for Receivable /
        Payable account Interest Income - BBT

    An ordinary Income account has root_type='Income' and account_type='Income
    Account', so a Customer hung off it produced a Journal Entry that CREATED
    fine and then failed at SUBMIT — leaving the operator with stuck drafts and
    no way to approve them. Rules saved before v0.4.0.9's save-time validation
    are still carrying those party_types, so they get repaired here.

    A rule is flipped to no-party only on a POSITIVE mismatch — Supplier on a
    non-Payable account, or Customer on a non-Receivable one. Anything we cannot
    read is LEFT ALONE: an unconfigured/unreachable ERPNext, an offset that no
    longer resolves, or a blank account_type all yield no verdict, and silently
    stripping the operator's party choice on a transient outage would be a worse
    bug than the one being fixed.

    Idempotent by construction: a rule is only touched when its stored
    party_type contradicts its offset's account_type, so the second run — and
    every run after — flips nothing. `party_type='Auto'` and NULL are never
    touched; Auto re-derives correctly at JE time under the new matrix.

    ARCHIVED rules are deliberately left alone. A rule version is archived
    rather than deleted precisely so a past auto-JE decision stays
    reconstructible (see admin_ui.save_rule), and rewriting one would falsify
    that history for no gain — an archived rule never fires again.

    Best-effort throughout: any ERPNext trouble logs and returns, because a
    migration must never block boot."""
    insp = inspect(db.engine)
    if 'categorization_rules' not in insp.get_table_names():
        return
    cols = {c['name'] for c in insp.get_columns('categorization_rules')}
    if not {'party_type', 'offset_account', 'applies_to_company',
            'archived'} <= cols:
        return  # columns not all present yet (shouldn't happen post-ADD)
    from . import erpnext_settings
    if not erpnext_settings.is_configured():
        return  # nothing to resolve an account_type against
    from .categorization import PARTY_ACCOUNT_TYPES
    from .models import CategorizationRule
    rules = CategorizationRule.query.filter(
        CategorizationRule.archived.is_(False),
        CategorizationRule.party_type.in_(tuple(PARTY_ACCOUNT_TYPES.values()))
    ).all()
    if not rules:
        return  # fast path: nothing declares a literal side
    from . import erpnext_bank
    try:
        client = erpnext_bank.get_client()
    except Exception:
        log.info('migration: ERPNext unavailable, leaving party_types as-is')
        return
    # (offset, company) → account_type, so N rules over a handful of distinct
    # offsets cost a handful of lookups rather than one apiece.
    seen: dict[tuple[str, str], str] = {}
    flipped = 0
    for r in rules:
        offset = (r.offset_account or '').strip()
        if not offset:
            continue
        key = (offset, (r.applies_to_company or '').strip())
        if key not in seen:
            try:
                seen[key] = erpnext_bank.account_types_for_account(
                    client, key[0], key[1])[1]
            except Exception:
                seen[key] = ''
        acct_type = seen[key]
        if not acct_type:
            continue                    # undeterminable → no verdict, leave it
        declared = (r.party_type or '').strip()
        if PARTY_ACCOUNT_TYPES.get(acct_type) == declared:
            continue                    # already compatible
        want = 'Payable' if declared == 'Supplier' else 'Receivable'
        log.warning('migration: rule %s "%s" party_type %s → none '
                    '(offset %s is %s not %s)',
                    r.id, r.name or '', declared, offset, acct_type, want)
        r.party_type = None
        flipped += 1
    if flipped:
        db.session.commit()
        log.warning('migration: cleared party_type on %d rule(s) whose offset '
                    'account ERPNext will not accept a party on', flipped)
