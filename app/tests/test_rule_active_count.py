# SPDX-License-Identifier: MIT
"""Two-count display on CategorizationRule (v0.5.9).

`match_counts()` is the HISTORICAL lifetime count (every JE a rule ever
generated); `active_match_counts()` is the CURRENTLY-ACTIVE count (JEs since the
rule was last switched ON, 0 while OFF). Toggling OFF freezes the historical
number and zeroes the active one; toggling back ON restarts the active count
from that instant.

Also asserts the display-vs-engine coupling the feature is really about: an OFF
rule genuinely does not fire (evaluate_rules filters active=True), so its active
count of 0 is the truth, not just a label.

Synthetic rule/merchant names only.
"""
from datetime import datetime

from app import db, rule_stats
from app import categorization
from app.models import CategorizationRule, GeneratedJournalEntry

from tests.test_statements import StatementsBase


class RuleActiveCountTest(StatementsBase):
    def _rule(self, activated_at, active=True):
        r = CategorizationRule(name='TEST RULE', match_type='merchant_contains',
                               match_value='TESTVENDOR', active=active,
                               activated_at=activated_at,
                               created_at=activated_at)
        db.session.add(r)
        db.session.commit()
        return r

    def _je(self, tid, rule_id, when):
        db.session.add(GeneratedJournalEntry(
            plaid_transaction_id=tid, rule_id=rule_id, state='approved',
            created_at=when))
        db.session.commit()

    def _counts(self, rule):
        return (rule_stats.match_counts().get(rule.id, 0),
                rule_stats.active_match_counts().get(rule.id, 0))

    def test_new_rule_has_zero_and_zero(self):
        rule = self._rule(datetime(2026, 6, 1))
        self.assertEqual(self._counts(rule), (0, 0))

    def test_three_matches_read_three_and_three(self):
        rule = self._rule(datetime(2026, 6, 1))
        for i, day in enumerate((2, 3, 4)):
            self._je(f't{i}', rule.id, datetime(2026, 6, day))
        self.assertEqual(self._counts(rule), (3, 3))   # historical, active

    def test_flipping_off_freezes_historical_and_zeroes_active(self):
        rule = self._rule(datetime(2026, 6, 1))
        for i, day in enumerate((2, 3, 4)):
            self._je(f't{i}', rule.id, datetime(2026, 6, day))
        rule.active = False
        db.session.commit()
        self.assertEqual(self._counts(rule), (3, 0))   # historical stays 3

    def test_reactivating_restarts_active_from_the_new_instant(self):
        rule = self._rule(datetime(2026, 6, 1))
        for i, day in enumerate((2, 3, 4)):
            self._je(f't{i}', rule.id, datetime(2026, 6, day))
        # OFF, then ON again LATER than every existing JE.
        rule.active = False
        db.session.commit()
        rule.active = True
        rule.activated_at = datetime(2026, 6, 10)
        db.session.commit()
        # historical unchanged; active resets to 0 (the 3 JEs predate reactivation)
        self.assertEqual(self._counts(rule), (3, 0))
        # a NEW match after reactivation counts as active.
        self._je('t-new', rule.id, datetime(2026, 6, 11))
        self.assertEqual(self._counts(rule), (4, 1))

    def test_off_on_off_cycle_accumulates_active_only_while_on(self):
        rule = self._rule(datetime(2026, 6, 1))
        self._je('a', rule.id, datetime(2026, 6, 2))          # ON stretch 1
        rule.active = False; db.session.commit()              # OFF
        self.assertEqual(self._counts(rule), (1, 0))
        rule.active = True; rule.activated_at = datetime(2026, 6, 5)
        db.session.commit()                                   # ON stretch 2
        self._je('b', rule.id, datetime(2026, 6, 6))
        self.assertEqual(self._counts(rule), (2, 1))          # only stretch-2 JE
        rule.active = False; db.session.commit()              # OFF again
        self.assertEqual(self._counts(rule), (2, 0))

    def test_inactive_rule_does_not_fire(self):
        """The display's premise: an OFF rule is filtered out of evaluate_rules,
        so it truly matches nothing — the active count of 0 is real."""
        rule = self._rule(datetime(2026, 6, 1), active=False)
        row = type('Row', (), {'merchant_name': 'TESTVENDOR PURCHASE',
                               'name': 'TESTVENDOR PURCHASE', 'category': '',
                               'amount': -10.0, 'account_id': None,
                               'intercompany_pair_id': None})()
        self.assertIsNone(categorization.evaluate_rules(row)[0])
        rule.active = True
        db.session.commit()
        self.assertIsNotNone(categorization.evaluate_rules(row)[0])


class MigrationDeclarationTest(StatementsBase):
    def test_activated_at_column_declared(self):
        from app.migrations import SCHEMA_MIGRATIONS
        cols = {(t, c) for t, c, _ in SCHEMA_MIGRATIONS}
        self.assertIn(('categorization_rules', 'activated_at'), cols)


class RulesPageRenderTest(StatementsBase):
    def setUp(self):
        super().setUp()
        self.client_ = self.app.test_client()

    def test_rules_page_shows_both_counts(self):
        on = CategorizationRule(name='ON RULE', match_type='merchant_contains',
                                match_value='TESTVENDOR', active=True,
                                activated_at=datetime(2026, 6, 1),
                                created_at=datetime(2026, 6, 1), match_count=3)
        off = CategorizationRule(name='OFF RULE', match_type='merchant_contains',
                                 match_value='TESTOTHER', active=False,
                                 activated_at=datetime(2026, 6, 1),
                                 created_at=datetime(2026, 6, 1), match_count=6)
        db.session.add_all([on, off]); db.session.commit()
        db.session.add(GeneratedJournalEntry(
            plaid_transaction_id='x', rule_id=on.id, state='approved',
            created_at=datetime(2026, 6, 2)))
        db.session.commit()
        resp = self.client_.get('/admin/rules')
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertIn('hist | active', body)     # the split header
        self.assertIn('active', body)
        self.assertIn('— (off)', body)           # the OFF rule's active cell


class BackfillTest(StatementsBase):
    def test_backfill_sets_activated_at_to_created_at(self):
        from sqlalchemy import text
        from app.migrations import _backfill_rule_activated_at
        r = CategorizationRule(name='LEGACY', match_type='merchant_contains',
                               match_value='X', created_at=datetime(2025, 1, 2))
        db.session.add(r); db.session.commit()
        # Simulate a pre-v0.5.9 row: NULL out activated_at.
        db.session.execute(text('UPDATE categorization_rules '
                                'SET activated_at = NULL WHERE id = :i'),
                           {'i': r.id})
        db.session.commit()
        _backfill_rule_activated_at()
        db.session.refresh(r)
        self.assertEqual(r.activated_at, r.created_at)
