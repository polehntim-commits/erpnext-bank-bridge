# SPDX-License-Identifier: MIT
"""Categorization rules read from ERPNext, and the matcher's fast path (v1.0.0).

Two things move here and they are easy to conflate: the RULES move to ERPNext,
the ENGINE does not. So what is tested is the seam —

  * the mirror: ERPNext's field spellings folded onto Bank Bridge's columns,
    upserted idempotently on the docname, deactivating (never deleting) a rule
    ERPNext stops returning;
  * the eligible set: with RULES_SOURCE=erpnext AND mirrored rules present, only
    mirrored rules fire; otherwise the local table is the fallback, which is
    also the rollback;
  * the fallbacks, which are the whole reason this is safe to default on: an
    unreachable ERPNext must leave the engine matching, not silently matching
    nothing (a rerun that posted no Journal Entries would look exactly like a
    quiet day);
  * `combined`, the match type the enhanced rule vocabulary adds;
  * and the fast path — one rule fetch per run rather than one per transaction,
    which is what Tim's "maximum efficiency in rule matching" asked for.

    cd app
    python3 -m unittest tests.test_erpnext_rules -v
"""
import json

from app import categorization, db, erpnext_rules, erpnext_settings
from app.models import BankTransaction, CategorizationRule

from tests.fakes import FakeERPClient
from tests.test_categorization import ACC, Base


def erp_rule(name, **kw):
    """One rule as ERPNext's list_bank_categorization_rules returns it —
    deliberately in ERPNext's OWN spellings (`pattern`, `account`, `enabled`,
    `company`), because tolerating them is the thing under test."""
    row = {'name': name, 'rule_name': kw.pop('rule_name', name),
           'match_type': 'merchant_contains', 'pattern': 'chevron',
           'account': '5100 - Farm Fuel - OML', 'enabled': 1, 'priority': 100}
    row.update(kw)
    return row


class RulesBase(Base):
    def setUp(self):
        super().setUp()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', 'Example Co')
        erpnext_rules.reset_cache()
        categorization.invalidate_rule_cache()
        self.addCleanup(erpnext_rules.reset_cache)
        self.addCleanup(categorization.invalidate_rule_cache)
        self.erp = FakeERPClient()

    def _serve(self, rows):
        self.erp.method_returns[erpnext_rules.RULES_METHOD] = {'rules': rows}

    def _local_rule(self, **kw):
        defaults = {'match_type': 'merchant_contains', 'match_value': 'local',
                    'offset_account': '5000 - Local - OML', 'priority': 50,
                    'active': True, 'name': 'a local rule'}
        defaults.update(kw)
        rule = CategorizationRule(**defaults)
        db.session.add(rule)
        db.session.commit()
        return rule

    def _txn(self, merchant='CHEVRON', amount=50.0, name='CHEVRON 1234',
             category=''):
        row = BankTransaction(
            plaid_transaction_id=f'txn-{merchant}-{amount}', account_id=ACC,
            amount=amount, date=None, name=name, merchant_name=merchant,
            category=category, pending=False, removed=False)
        db.session.add(row)
        db.session.commit()
        return row


# ── the field mapping ───────────────────────────────────────────────────────

class NormalizeTest(RulesBase):
    def test_erpnexts_own_spellings_are_accepted(self):
        """`pattern`/`account`/`enabled`/`company` are what ERPNext calls these.
        Discovering after a deploy that 64 rules mapped to an empty match_value
        costs a day of miscategorized transactions; the alias tuple costs
        nothing."""
        out = erpnext_rules.normalize_rule(erp_rule(
            'BCR-0001', company='Orchard Meadow, LLC'))
        self.assertEqual(out['erpnext_rule_name'], 'BCR-0001')
        self.assertEqual(out['match_value'], 'chevron')
        self.assertEqual(out['offset_account'], '5100 - Farm Fuel - OML')
        self.assertEqual(out['applies_to_company'], 'Orchard Meadow, LLC')
        self.assertTrue(out['active'])

    def test_bank_bridges_own_spellings_are_accepted_too(self):
        out = erpnext_rules.normalize_rule({
            'name': 'BCR-0002', 'match_value': 'sorren',
            'offset_account': '310 - G and A - OML', 'active': False,
            'applies_to_company': 'OML'})
        self.assertEqual(out['match_value'], 'sorren')
        self.assertFalse(out['active'])

    def test_an_unknown_match_type_falls_back_to_the_narrowest(self):
        """A rule that matches too little leaves a transaction for a human; one
        that matches too much books it somewhere wrong."""
        out = erpnext_rules.normalize_rule(erp_rule('BCR-3',
                                                    match_type='nonsense'))
        self.assertEqual(out['match_type'], 'merchant_contains')

    def test_every_enhanced_match_type_maps(self):
        for spelling, expected in (
                ('exact', 'merchant_exact'), ('regex', 'description_regex'),
                ('category', 'plaid_category_matches'),
                ('amount', 'amount_range'), ('combined', 'combined'),
                ('MERCHANT_EXACT', 'merchant_exact')):
            out = erpnext_rules.normalize_rule(
                erp_rule('BCR-x', match_type=spelling))
            self.assertEqual(out['match_type'], expected, spelling)

    def test_a_row_without_a_docname_is_refused(self):
        """There would be nothing to upsert ON, and the rule would multiply on
        every fetch."""
        self.assertIsNone(erpnext_rules.normalize_rule({'pattern': 'x'}))

    def test_a_non_numeric_priority_does_not_break_the_fetch(self):
        out = erpnext_rules.normalize_rule(erp_rule('BCR-4', priority='high'))
        self.assertEqual(out['priority'], 100)


# ── the mirror ──────────────────────────────────────────────────────────────

class MirrorTest(RulesBase):
    def test_a_fetch_creates_local_rows_keyed_on_the_docname(self):
        self._serve([erp_rule('BCR-0001'), erp_rule('BCR-0002',
                                                    pattern='sorren')])
        stats = erpnext_rules.refresh(self.erp, force=True)
        self.assertTrue(stats['refreshed'])
        self.assertEqual(stats['created'], 2)
        self.assertEqual(CategorizationRule.query.count(), 2)
        self.assertEqual(
            {r.erpnext_rule_name for r in CategorizationRule.query.all()},
            {'BCR-0001', 'BCR-0002'})

    def test_refetching_the_same_rules_is_idempotent(self):
        self._serve([erp_rule('BCR-0001')])
        erpnext_rules.refresh(self.erp, force=True)
        stats = erpnext_rules.refresh(self.erp, force=True)
        self.assertEqual(stats['created'], 0)
        self.assertEqual(stats['updated'], 0)
        self.assertEqual(CategorizationRule.query.count(), 1)

    def test_an_edited_rule_updates_in_place(self):
        self._serve([erp_rule('BCR-0001')])
        erpnext_rules.refresh(self.erp, force=True)
        self._serve([erp_rule('BCR-0001',
                              account='5200 - Repairs - OML')])
        stats = erpnext_rules.refresh(self.erp, force=True)
        self.assertEqual(stats['updated'], 1)
        self.assertEqual(CategorizationRule.query.one().offset_account,
                         '5200 - Repairs - OML')

    def test_a_withdrawn_rule_is_deactivated_not_deleted(self):
        """Deleting would orphan every GeneratedJournalEntry that names it, and
        'which rule booked this?' is a question asked about years-old entries."""
        self._serve([erp_rule('BCR-0001'), erp_rule('BCR-0002')])
        erpnext_rules.refresh(self.erp, force=True)
        self._serve([erp_rule('BCR-0001')])
        stats = erpnext_rules.refresh(self.erp, force=True)
        self.assertEqual(stats['deactivated'], 1)
        self.assertEqual(CategorizationRule.query.count(), 2)
        gone = CategorizationRule.query.filter_by(
            erpnext_rule_name='BCR-0002').one()
        self.assertFalse(gone.active)

    def test_an_empty_rule_set_retires_the_whole_mirror(self):
        """`fetch` returns None (not []) when ERPNext cannot be read, so an
        empty list means ERPNext genuinely holds no rules. Leaving the mirror
        active would keep firing rules ERPNext no longer has — the one thing
        "source of truth" rules out. The engine then falls back to the local
        table, which is the documented behaviour."""
        self._serve([erp_rule('BCR-0001')])
        erpnext_rules.refresh(self.erp, force=True)
        self._serve([])
        stats = erpnext_rules.refresh(self.erp, force=True)
        self.assertEqual(stats['deactivated'], 1)
        self.assertFalse(CategorizationRule.query.one().active)
        self.assertEqual(categorization.rule_source_in_force(), 'local')

    def test_unmappable_rows_do_not_retire_the_mirror(self):
        """ERPNext sent rules and this build could not map them (no docname).
        That is a mapping failure, not a withdrawal — deactivating 64 working
        rules over it would be the worst reading of an ambiguous response."""
        self._serve([erp_rule('BCR-0001')])
        erpnext_rules.refresh(self.erp, force=True)
        self._serve([{'pattern': 'chevron'}])          # no docname
        stats = erpnext_rules.refresh(self.erp, force=True)
        self.assertEqual(stats['skipped'], 1)
        self.assertEqual(stats['deactivated'], 0)
        self.assertTrue(CategorizationRule.query.one().active)

    def test_a_locally_authored_rule_is_never_touched_by_a_fetch(self):
        """It is not a cache of anything, so a fetch has no business editing
        it — and with RULES_SOURCE=local it is the whole rule set."""
        local = self._local_rule()
        self._serve([erp_rule('BCR-0001')])
        erpnext_rules.refresh(self.erp, force=True)
        db.session.refresh(local)
        self.assertTrue(local.active)
        self.assertIsNone(local.erpnext_rule_name)


class FetchFailureTest(RulesBase):
    def test_an_unreachable_erpnext_reads_as_none_not_as_no_rules(self):
        """None and [] are different answers: [] means 'ERPNext has no rules',
        None means 'ERPNext did not answer'. Conflating them would silently
        deactivate every mirrored rule on an outage."""
        self.erp.method_failures[erpnext_rules.RULES_METHOD] = (404, 'nope')
        self.assertIsNone(erpnext_rules.fetch(self.erp))
        stats = erpnext_rules.refresh(self.erp, force=True)
        self.assertFalse(stats['refreshed'])
        self.assertEqual(stats['reason'], 'erpnext unreachable')

    def test_an_outage_does_not_deactivate_the_mirror(self):
        self._serve([erp_rule('BCR-0001')])
        erpnext_rules.refresh(self.erp, force=True)
        self.erp.method_returns.clear()
        self.erp.method_failures[erpnext_rules.RULES_METHOD] = (None, 'refused')
        erpnext_rules.refresh(self.erp, force=True)
        self.assertTrue(CategorizationRule.query.one().active)

    def test_three_envelope_shapes_are_tolerated(self):
        rows = [erp_rule('BCR-0001')]
        for envelope in (rows, {'rules': rows}, {'data': rows},
                         {'message': rows}):
            self.assertEqual(erpnext_rules._rule_rows(envelope), rows)
        self.assertIsNone(erpnext_rules._rule_rows({'error': 'x'}))

    def test_the_ttl_suppresses_a_second_fetch_but_force_does_not(self):
        self._serve([erp_rule('BCR-0001')])
        erpnext_rules.refresh(self.erp, force=True)
        calls = len([m for m, _ in self.erp.method_calls
                     if m == erpnext_rules.RULES_METHOD])
        self.assertEqual(erpnext_rules.refresh(self.erp)['reason'], 'cached')
        self.assertEqual(
            len([m for m, _ in self.erp.method_calls
                 if m == erpnext_rules.RULES_METHOD]), calls)
        erpnext_rules.refresh(self.erp, force=True)
        self.assertEqual(
            len([m for m, _ in self.erp.method_calls
                 if m == erpnext_rules.RULES_METHOD]), calls + 1)

    def test_rules_source_local_skips_the_fetch_entirely(self):
        erpnext_settings.set_source('rules_source', 'local')
        self._serve([erp_rule('BCR-0001')])
        stats = erpnext_rules.refresh(self.erp, force=True)
        self.assertEqual(stats['reason'], 'RULES_SOURCE=local')
        self.assertEqual(CategorizationRule.query.count(), 0)


# ── which set actually fires ────────────────────────────────────────────────

class EligibleSetTest(RulesBase):
    def test_mirrored_rules_displace_local_ones_when_present(self):
        """'Single source of truth' means exactly this. A mixed set where a
        stale local rule could outrank an ERPNext one at the same priority
        would be the worst of both."""
        self._local_rule(match_value='chevron', priority=1)
        self._serve([erp_rule('BCR-0001')])
        erpnext_rules.refresh(self.erp, force=True)
        winner, _ = categorization.evaluate_rules(self._txn())
        self.assertEqual(winner.erpnext_rule_name, 'BCR-0001')
        self.assertEqual(categorization.rule_source_in_force(), 'erpnext')

    def test_the_local_table_is_the_fallback_when_erpnext_holds_no_rules(self):
        """A rerun that matched nothing because a method 404'd would post no
        Journal Entries and look exactly like a quiet day."""
        self._local_rule(match_value='chevron')
        self.assertEqual(categorization.rule_source_in_force(), 'local')
        winner, _ = categorization.evaluate_rules(self._txn())
        self.assertIsNotNone(winner)

    def test_rules_source_local_uses_the_local_table_even_with_a_mirror(self):
        """The rollback: a settings flip, not a restore."""
        self._local_rule(match_value='chevron', priority=1)
        self._serve([erp_rule('BCR-0001')])
        erpnext_rules.refresh(self.erp, force=True)
        erpnext_settings.set_source('rules_source', 'local')
        categorization.invalidate_rule_cache()
        winner, _ = categorization.evaluate_rules(self._txn())
        self.assertIsNone(winner.erpnext_rule_name)
        self.assertEqual(categorization.rule_source_in_force(), 'local')

    def test_a_deactivated_mirrored_rule_stops_firing(self):
        self._serve([erp_rule('BCR-0001')])
        erpnext_rules.refresh(self.erp, force=True)
        self._serve([erp_rule('BCR-0001', enabled=0)])
        erpnext_rules.refresh(self.erp, force=True)
        winner, _ = categorization.evaluate_rules(self._txn())
        self.assertIsNone(winner)


# ── the matcher ─────────────────────────────────────────────────────────────

class CombinedMatchTest(RulesBase):
    def _combined(self, spec, **kw):
        return self._local_rule(match_type='combined',
                                match_value=json.dumps(spec), **kw)

    def test_all_requires_every_clause(self):
        """'Wells Fargo, but only the wires over $10,000' — the shape the
        workaround used to smuggle into a regex."""
        rule = self._combined({'all': [
            {'match_type': 'merchant_contains', 'match_value': 'wells'},
            {'match_type': 'amount_range', 'match_value': '[10000, 1000000]'}]})
        self.assertTrue(categorization.rule_matches(
            rule, merchant_name='WELLS FARGO', amount=25000.0))
        self.assertFalse(categorization.rule_matches(
            rule, merchant_name='WELLS FARGO', amount=250.0))
        self.assertFalse(categorization.rule_matches(
            rule, merchant_name='CHEVRON', amount=25000.0))

    def test_any_requires_one_clause(self):
        rule = self._combined({'any': [
            {'match_type': 'merchant_exact', 'match_value': 'CHEVRON'},
            {'match_type': 'merchant_exact', 'match_value': 'SHELL'}]})
        self.assertTrue(categorization.rule_matches(rule,
                                                    merchant_name='SHELL'))
        self.assertFalse(categorization.rule_matches(rule,
                                                     merchant_name='TEXACO'))

    def test_all_and_any_together_must_both_hold(self):
        rule = self._combined({
            'any': [{'match_type': 'merchant_exact', 'match_value': 'CHEVRON'},
                    {'match_type': 'merchant_exact', 'match_value': 'SHELL'}],
            'all': [{'match_type': 'amount_range',
                     'match_value': '[100, 500]'}]})
        self.assertTrue(categorization.rule_matches(
            rule, merchant_name='SHELL', amount=250.0))
        self.assertFalse(categorization.rule_matches(
            rule, merchant_name='SHELL', amount=25.0))

    def test_a_malformed_combined_rule_matches_nothing(self):
        """Total, like the other five: a predicate that cannot be read should
        leave the transaction for a human, not claim everything."""
        for bad in ('', 'not json', '[]', '{}', '{"all": []}',
                    '{"all": "chevron"}'):
            rule = self._local_rule(match_type='combined', match_value=bad)
            self.assertFalse(categorization.rule_matches(
                rule, merchant_name='CHEVRON', amount=1.0), bad)

    def test_nesting_is_bounded(self):
        """A rule that accidentally nests itself must not recurse forever."""
        inner = {'all': [{'match_type': 'merchant_contains',
                          'match_value': 'chev'}]}
        depth = inner
        for _ in range(6):
            depth = {'all': [{'match_type': 'combined',
                              'match_value': json.dumps(depth)}]}
        rule = self._combined(depth)
        self.assertFalse(categorization.rule_matches(rule,
                                                     merchant_name='CHEVRON'))

    def test_combined_is_in_the_vocabulary(self):
        self.assertIn('combined', categorization.MATCH_TYPES)


class FastPathTest(RulesBase):
    def test_the_rule_set_is_fetched_once_not_once_per_transaction(self):
        """The point of the snapshot. Before v1.0.0 this issued a full
        CategorizationRule.query per transaction — a few thousand table scans
        for a rule set that changes a few times a month."""
        for i in range(5):
            self._local_rule(match_value=f'nomatch{i}', priority=i)
        rows = [self._txn(merchant=f'VENDOR{i}', amount=float(i))
                for i in range(20)]
        seen = []
        original = categorization.RuleSnapshot

        class Counting(original):
            def __init__(self, rule):
                seen.append(rule.id)
                super().__init__(rule)

        categorization.RuleSnapshot = Counting
        self.addCleanup(setattr, categorization, 'RuleSnapshot', original)
        categorization.invalidate_rule_cache()
        for row in rows:
            categorization.evaluate_rules(row)
        # Five rules hydrated ONCE, not five per transaction.
        self.assertEqual(len(seen), 5)

    def test_a_new_rule_is_seen_immediately_without_an_explicit_invalidation(
            self):
        """The cache is keyed on a table fingerprint, so correctness does not
        depend on every authoring surface remembering to call an invalidator."""
        txn = self._txn()
        self.assertIsNone(categorization.evaluate_rules(txn)[0])
        self._local_rule(match_value='chevron')
        winner, _ = categorization.evaluate_rules(txn)
        self.assertIsNotNone(winner)

    def test_an_edit_is_seen_immediately_too(self):
        rule = self._local_rule(match_value='chevron')
        txn = self._txn()
        self.assertIsNotNone(categorization.evaluate_rules(txn)[0])
        rule.active = False
        db.session.commit()
        self.assertIsNone(categorization.evaluate_rules(txn)[0])

    def test_the_winner_is_a_live_row_not_a_snapshot(self):
        """Generating the Journal Entry needs the offset account, the cost
        centers, the party fields and the template — the whole row."""
        self._local_rule(match_value='chevron',
                         offset_account='5100 - Fuel - OML')
        winner, _ = categorization.evaluate_rules(self._txn())
        self.assertIsInstance(winner, CategorizationRule)
        self.assertEqual(winner.offset_account, '5100 - Fuel - OML')

    def test_first_match_wins_by_priority_then_id(self):
        low = self._local_rule(match_value='chevron', priority=10,
                               name='wins')
        self._local_rule(match_value='chevron', priority=20, name='loses')
        winner, trace = categorization.evaluate_rules(self._txn())
        self.assertEqual(winner.id, low.id)
        # The trace stops at the winner — it is the audit record of what was
        # evaluated, not of the whole table.
        self.assertEqual(len(trace), 1)


class CacheScopeTest(RulesBase):
    """The consolidation's three read caches are keyed by things derived from
    the database — a rule-table fingerprint, an agreement id — and those keys
    are only unique WITHIN one database. Held process-globally they would leak
    across a second app, which is exactly what the suite builds a hundred times
    over. See app/appcache.py."""

    def test_two_apps_do_not_share_a_rule_snapshot(self):
        import os
        import tempfile
        from app import appcache, create_app, crypto
        self._local_rule(match_value='chevron', name='app-one rule')
        self.assertEqual([s.name for s in categorization.rule_snapshots()],
                         ['app-one rule'])

        fd, path = tempfile.mkstemp(suffix='.sqlite')
        datadir = tempfile.mkdtemp()
        self.addCleanup(os.close, fd)
        self.addCleanup(os.remove, path)
        self.addCleanup(crypto.reset_cache)
        other = create_app({
            'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{path}',
            'DATA_DIR': datadir, 'FERNET_KEY': '', 'SCHEDULER_ENABLED': False})
        with other.app_context():
            # A DIFFERENT, empty database. A shared cache would hand back the
            # first app's rule — one row, id 1, same fingerprint shape.
            self.assertEqual(categorization.rule_snapshots(), ())
            self.assertIsNot(appcache.bucket('rule_snapshots'), None)
        # And the first app's snapshot survives the excursion intact.
        self.assertEqual([s.name for s in categorization.rule_snapshots()],
                         ['app-one rule'])

    def test_the_bucket_is_scoped_to_the_app_object(self):
        from app import appcache
        appcache.bucket('probe')['x'] = 1
        self.assertEqual(appcache.bucket('probe'), {'x': 1})
        appcache.clear('probe')
        self.assertEqual(appcache.bucket('probe'), {})


if __name__ == '__main__':  # pragma: no cover
    import unittest
    unittest.main()
