# SPDX-License-Identifier: MIT
"""Idempotent startup schema migrations (app/migrations.py).

Guards the v0.4.0 → v0.4.0.1 regression: on an existing (pre-v0.4.0) database
the additive ADD COLUMN steps must run to completion, and — critically — must
run BEFORE the ORM-based data backfill, whose SELECT lists every model column.
Previously the backfill ran first, hit UndefinedColumn on the not-yet-added
owning_company/balance_only columns, and the fail-open handler swallowed the
remaining ADDs — so every later PlaidItem.query 500'd.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from sqlalchemy import inspect, text  # noqa: E402

from app import create_app, db, crypto  # noqa: E402
from app import migrations  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        PlaidItem)

# The columns introduced after v0.3.x that an upgrading install must gain. The
# regression was specifically that these never got added.
V040_COLUMNS = [
    ('plaid_items', 'owning_company'),
    ('plaid_accounts', 'owning_company'),
    ('plaid_accounts', 'balance_only'),
]


class MigrationBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        cfg = {
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        }
        self.app = create_app(cfg)
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _columns(self, table):
        return {c['name'] for c in inspect(db.engine).get_columns(table)}

    def _downgrade_to_pre_v040(self):
        """Rewind the freshly-built schema to look like a v0.3.x install by
        dropping the v0.4.0 columns create_all() just added. balance_only is
        indexed, so its index goes first (SQLite refuses to drop an indexed
        column)."""
        with db.engine.begin() as conn:
            conn.execute(text(
                'DROP INDEX IF EXISTS ix_plaid_accounts_balance_only'))
            for table, column in V040_COLUMNS:
                conn.execute(
                    text(f'ALTER TABLE {table} DROP COLUMN {column}'))


class MigrationTests(MigrationBase):
    def test_fresh_install_has_columns_and_is_noop(self):
        # create_all() already built the full model, so every column exists and
        # run_migrations (called during create_app) added nothing.
        for table, column in V040_COLUMNS:
            self.assertIn(column, self._columns(table))
        # Re-running is a clean no-op — no error, columns unchanged.
        migrations.run_migrations()
        for table, column in V040_COLUMNS:
            self.assertIn(column, self._columns(table))

    def test_upgrade_adds_missing_columns(self):
        self._downgrade_to_pre_v040()
        for table, column in V040_COLUMNS:
            self.assertNotIn(column, self._columns(table))
        migrations.run_migrations()
        for table, column in V040_COLUMNS:
            self.assertIn(column, self._columns(table),
                          f'{table}.{column} should be added on upgrade')

    def test_upgrade_lets_plaiditem_query_succeed(self):
        # The exact production symptom: PlaidItem.query fails with
        # "column plaid_items.owning_company does not exist" until the migration
        # adds the column. This is the regression guard.
        self._downgrade_to_pre_v040()
        migrations.run_migrations()
        # Would raise OperationalError/ProgrammingError before the fix.
        self.assertEqual(PlaidItem.query.all(), [])

    def test_migration_is_idempotent(self):
        self._downgrade_to_pre_v040()
        migrations.run_migrations()
        first = {(t, c): self._columns(t) for t, c in V040_COLUMNS}
        # Second and third runs must not raise and must not change the schema.
        migrations.run_migrations()
        migrations.run_migrations()
        for (t, c), cols in first.items():
            self.assertEqual(cols, self._columns(t))
            self.assertIn(c, self._columns(t))

    def test_backfill_runs_after_column_adds(self):
        # Ordering guarantee: SCHEMA_MIGRATIONS ADDs must all precede the
        # ORM-based backfill. Assert the offset columns (queried by the backfill
        # indirectly via the model) and the v0.4.0 columns coexist after one
        # run on a downgraded DB — i.e. no ADD was aborted mid-way.
        self._downgrade_to_pre_v040()
        migrations.run_migrations()
        acct_cols = self._columns('plaid_accounts')
        self.assertTrue({'owning_company', 'balance_only'} <= acct_cols)
        rule_cols = self._columns('categorization_rules')
        self.assertIn('offset_account', rule_cols)

    def test_runs_on_sqlite_dialect(self):
        # The test suite exercises the SQLite path (production is Postgres); the
        # inspector-based existence check is what keeps ADD COLUMN portable
        # across both without a Postgres-only IF NOT EXISTS.
        self.assertEqual(db.engine.dialect.name, 'sqlite')
        self._downgrade_to_pre_v040()
        migrations.run_migrations()
        self.assertIn('owning_company', self._columns('plaid_items'))


# ── v0.4.1 · intercompany transfer detection ────────────────────────────────

# The columns v0.4.1 adds to tables that already exist. The pair TABLE itself is
# built by create_all() and needs no migration line.
V041_COLUMNS = [
    ('bank_transactions', 'intercompany_pair_id'),
    ('generated_journal_entries', 'intercompany_pair_id'),
    ('categorization_rules', 'ignore_for_paired'),
]


class IntercompanyMigrationTests(MigrationBase):
    def _downgrade_to_pre_v041(self):
        """Rewind to a v0.4.0.9 install: drop the three v0.4.1 columns (indexed
        ones lose their index first — SQLite refuses to drop an indexed column)
        and the pair table."""
        with db.engine.begin() as conn:
            for index in ('ix_bank_transactions_intercompany_pair_id',
                          'ix_generated_journal_entries_intercompany_pair_id'):
                conn.execute(text(f'DROP INDEX IF EXISTS {index}'))
            for table, column in V041_COLUMNS:
                conn.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))
            conn.execute(text('DROP TABLE IF EXISTS intercompany_transfer_pairs'))

    def test_fresh_install_has_the_columns_and_the_table(self):
        for table, column in V041_COLUMNS:
            self.assertIn(column, self._columns(table))
        self.assertIn('intercompany_transfer_pairs',
                      inspect(db.engine).get_table_names())

    def test_upgrade_adds_the_missing_columns(self):
        self._downgrade_to_pre_v041()
        for table, column in V041_COLUMNS:
            self.assertNotIn(column, self._columns(table))
        migrations.run_migrations()
        for table, column in V041_COLUMNS:
            self.assertIn(column, self._columns(table),
                          f'{table}.{column} should be added on upgrade')

    def test_upgrade_is_idempotent(self):
        self._downgrade_to_pre_v041()
        migrations.run_migrations()
        migrations.run_migrations()
        for table, column in V041_COLUMNS:
            self.assertIn(column, self._columns(table))

    def test_existing_rules_backfill_to_ignoring_paired_transactions(self):
        """The backfill direction is the whole safety argument: an existing
        rule must NOT start firing on paired transactions the moment v0.4.1
        ships, or a generic Transfer rule would keep booking one leg of every
        intercompany move to profit & loss."""
        rule = CategorizationRule(name='Transfers', priority=10, active=True,
                                  archived=False, match_type='description_regex',
                                  match_value='Transfer',
                                  offset_account='Owner Draws')
        db.session.add(rule)
        db.session.commit()
        rule_id = rule.id
        db.session.remove()
        self._downgrade_to_pre_v041()
        migrations.run_migrations()
        migrated = db.session.get(CategorizationRule, rule_id)
        self.assertTrue(migrated.ignore_for_paired,
                        'a pre-v0.4.1 rule must default to ignoring paired '
                        'transactions, not to firing on them')

    def test_existing_transactions_backfill_to_unpaired(self):
        self._downgrade_to_pre_v041()
        migrations.run_migrations()
        # Would raise before the column is added — the same shape as the
        # v0.4.0 → v0.4.0.1 regression this file exists to guard.
        self.assertEqual(BankTransaction.query.all(), [])


class InternalTagMigrationTests(MigrationBase):
    """v0.4.49 · bb_internal_tag on both categorization_rules and
    bank_transactions. Additive, backfilling to '' — an upgrade adds two empty
    columns and changes no behaviour until an operator sets a tag."""

    V049_COLUMNS = (('categorization_rules', 'bb_internal_tag'),
                    ('bank_transactions', 'bb_internal_tag'))

    def _downgrade(self):
        with db.engine.begin() as conn:
            for table, column in self.V049_COLUMNS:
                conn.execute(
                    text(f'ALTER TABLE {table} DROP COLUMN {column}'))

    def test_fresh_install_has_the_columns(self):
        for table, column in self.V049_COLUMNS:
            self.assertIn(column, self._columns(table))

    def test_upgrade_adds_them_and_query_succeeds(self):
        self._downgrade()
        for table, column in self.V049_COLUMNS:
            self.assertNotIn(column, self._columns(table))
        migrations.run_migrations()
        for table, column in self.V049_COLUMNS:
            self.assertIn(column, self._columns(table),
                          f'{table}.{column} should be added on upgrade')
        # The production symptom this guards: a query 500s until the column
        # exists.
        from app.models import CategorizationRule
        self.assertEqual(CategorizationRule.query.all(), [])
        self.assertEqual(BankTransaction.query.all(), [])

    def test_idempotent(self):
        self._downgrade()
        migrations.run_migrations()
        migrations.run_migrations()
        for table, column in self.V049_COLUMNS:
            self.assertIn(column, self._columns(table))


if __name__ == '__main__':
    unittest.main()
