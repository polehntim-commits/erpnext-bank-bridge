# SPDX-License-Identifier: MIT
"""Guided rule-authoring workflow (v0.4.6).

The first-sync experience: filter the Transactions tab down to what no rule
caught, group it by merchant, and open the Rules editor pre-filled from a group
or a single row. Plus the two things that make the Rules list legible — a cached
per-rule match count, and a scope-mismatch warning raised at authoring time
instead of at push time.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import categorization, erpnext_settings, rule_stats  # noqa: E402
from app.models import (AuditEvent, BankTransaction,  # noqa: E402
                        CategorizationRule, GeneratedJournalEntry,
                        PlaidAccount, PlaidItem)

from tests.fakes import FakeERPClient  # noqa: E402

ACC = 'acct-checking'


class AuthoringBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    # ── fixtures ────────────────────────────────────────────────────────────

    def _account(self, company='Alpha LLC', account_id=ACC):
        db.session.add(PlaidItem(item_id='item-1', access_token_encrypted='x',
                                 owning_company=company))
        db.session.add(PlaidAccount(account_id=account_id, item_id='item-1',
                                    name='Checking', mask='1234',
                                    owning_company=company))
        db.session.commit()

    def _txn(self, tid, *, merchant='', name='TXN', amount=10.0, posted=True,
             removed=False, account_id=ACC):
        row = BankTransaction(
            plaid_transaction_id=tid, account_id=account_id, amount=amount,
            name=name, merchant_name=merchant, date=date(2026, 3, 1),
            removed=removed,
            posted_at=datetime(2026, 3, 2) if posted else None)
        db.session.add(row)
        db.session.commit()
        return row

    def _je(self, tid, state='pending_review', rule_id=None, je_name='JE-1'):
        row = GeneratedJournalEntry(
            plaid_transaction_id=tid, state=state, rule_id=rule_id,
            erpnext_journal_entry_name=(
                je_name if state in ('pending_review', 'approved') else None))
        db.session.add(row)
        db.session.commit()
        return row

    def _rule(self, **kw):
        data = dict(name='Fuel', priority=100, active=True,
                    match_type='merchant_contains', match_value='Chevron',
                    offset_account='Fuel Expense - AL')
        data.update(kw)
        rule = CategorizationRule(**data)
        db.session.add(rule)
        db.session.commit()
        return rule

    def _ids(self, state):
        """The plaid_transaction_ids the given rule-state filter returns."""
        q = rule_stats.apply_state_filter(BankTransaction.query, state)
        return {r.plaid_transaction_id for r in q.all()}


# ── 1. the rule-state filter ────────────────────────────────────────────────

class TestStateFilter(AuthoringBase):
    def setUp(self):
        super().setUp()
        self._account()
        # One transaction per outcome the engine can produce.
        self._txn('t-unmatched', merchant='Nobody')
        self._txn('t-live', merchant='Chevron')
        self._je('t-live', state='pending_review')
        self._txn('t-approved', merchant='Chevron')
        self._je('t-approved', state='approved')
        self._txn('t-error', merchant='Chevron')
        self._je('t-error', state='error')
        self._txn('t-blocked', merchant='Chevron')
        self._je('t-blocked', state='blocked')
        self._txn('t-skipped', merchant='Chevron')
        self._je('t-skipped', state='skipped_missing_account')
        self._txn('t-rejected', merchant='Chevron')
        self._je('t-rejected', state='rejected')
        self._txn('t-reversed', merchant='Chevron')
        self._je('t-reversed', state='reversed')

    def test_unmatched_returns_only_transactions_without_a_je(self):
        self.assertEqual(self._ids('unmatched'), {'t-unmatched'})

    def test_matched_excludes_unmatched(self):
        matched = self._ids('matched')
        self.assertEqual(matched, {'t-live', 't-approved'})
        self.assertNotIn('t-unmatched', matched)

    def test_je_error_shows_only_failed_states(self):
        # blocked (cross-Company) and skipped (missing account) are JE-creation
        # failures too — they belong here, not in "matched".
        self.assertEqual(self._ids('je_error'),
                         {'t-error', 't-blocked', 't-skipped'})

    def test_je_cancelled_shows_rejected_and_reversed(self):
        self.assertEqual(self._ids('je_cancelled'), {'t-rejected', 't-reversed'})

    def test_filters_partition_the_eligible_rows(self):
        # No transaction appears under two filters, and together they account
        # for every eligible row — which is what makes the dropdown trustworthy.
        buckets = [self._ids(s) for s in rule_stats.STATE_FILTER_VALUES]
        union = set()
        for b in buckets:
            self.assertFalse(union & b, f'overlapping filter buckets: {union & b}')
            union |= b
        eligible = {r.plaid_transaction_id for r in
                    BankTransaction.query.filter(*rule_stats.eligible_filter())}
        self.assertEqual(union, eligible)

    def test_unmatched_ignores_pending_and_removed_rows(self):
        # A row the engine has never been offered is not "unmatched" — listing it
        # would send the operator off writing rules that cannot fire yet.
        self._txn('t-pending', merchant='Nobody', posted=False)
        self._txn('t-removed', merchant='Nobody', removed=True)
        self.assertEqual(self._ids('unmatched'), {'t-unmatched'})

    def test_filters_use_subqueries_not_materialized_id_lists(self):
        # An install with tens of thousands of generated entries would otherwise
        # build an SQL IN(...) list past Postgres' bind-parameter cap. Pin the
        # shape: the compiled SQL must contain a nested SELECT, not N params.
        sql = str(rule_stats.apply_state_filter(
            BankTransaction.query, 'unmatched'))
        self.assertIn('SELECT', sql.split('WHERE', 1)[1])
        self.assertIn('generated_journal_entries', sql)

    def test_per_row_lookup_is_bounded_by_the_page(self):
        self._txn('t-a', merchant='A')
        self._txn('t-b', merchant='B')
        self._je('t-b', state='approved')
        self.assertEqual(rule_stats.tx_ids_with_je_among(['t-a', 't-b']),
                         {'t-b'})
        self.assertEqual(rule_stats.tx_ids_with_je_among([]), set())

    def test_unknown_state_degrades_to_no_filter(self):
        q = rule_stats.apply_state_filter(BankTransaction.query, 'nonsense')
        self.assertEqual(q.count(), BankTransaction.query.count())

    def test_page_renders_each_filter(self):
        for value in ('',) + rule_stats.STATE_FILTER_VALUES:
            r = self.client.get('/admin/transactions?state=' + value)
            self.assertEqual(r.status_code, 200, f'state={value!r}')

    def test_page_filter_narrows_the_listing(self):
        body = self.client.get(
            '/admin/transactions?state=unmatched').get_data(as_text=True)
        self.assertIn('t-unmatched'[:0] + 'Nobody', body)
        self.assertNotIn('JE-1', body)


# ── 2 & 3. grouping + prefill ───────────────────────────────────────────────

class TestGrouping(AuthoringBase):
    def test_groups_count_and_total_correctly(self):
        rows = [self._txn(f't-u{i}', merchant='Uber Eats', amount=10.0)
                for i in range(3)]
        rows.append(self._txn('t-c1', merchant='Chevron', amount=50.0))
        rows.append(self._txn('t-c2', merchant='Chevron', amount=25.0))
        groups, ungrouped = rule_stats.group_unmatched(rows)
        by_label = {g['label']: g for g in groups}
        self.assertEqual(by_label['Uber Eats']['count'], 3)
        self.assertEqual(by_label['Uber Eats']['total'], 30.0)
        self.assertEqual(by_label['Chevron']['count'], 2)
        self.assertEqual(by_label['Chevron']['total'], 75.0)
        self.assertEqual(ungrouped, [])
        # Biggest group first — that's the rule worth writing first.
        self.assertEqual(groups[0]['label'], 'Uber Eats')

    def test_singletons_fall_through_to_ungrouped(self):
        rows = [self._txn('t-a', merchant='Uber Eats'),
                self._txn('t-b', merchant='Uber Eats'),
                self._txn('t-c', merchant='One Off Store')]
        groups, ungrouped = rule_stats.group_unmatched(rows)
        self.assertEqual([g['label'] for g in groups], ['Uber Eats'])
        self.assertEqual([r.plaid_transaction_id for r in ungrouped], ['t-c'])

    def test_merchantless_rows_group_by_description_signature(self):
        rows = [self._txn('t-a', name='SQ *BLUE BOTTLE 4471 SEATTLE WA'),
                self._txn('t-b', name='SQ *BLUE BOTTLE 8890 PORTLAND OR')]
        groups, ungrouped = rule_stats.group_unmatched(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['kind'], 'description')
        self.assertEqual(groups[0]['label'], 'SQ BLUE BOTTLE')
        self.assertEqual(ungrouped, [])

    def test_description_signature_strips_reference_noise(self):
        self.assertEqual(
            rule_stats.description_signature('CHEVRON 0123456 #77 CA'),
            'CHEVRON CA')
        self.assertEqual(rule_stats.description_signature(''), '')

    def test_prefill_for_transaction_uses_merchant_exact(self):
        row = self._txn('t-a', merchant='Uber Eats')
        self.assertEqual(rule_stats.prefill_for(row),
                         {'match_type': 'merchant_exact',
                          'match_value': 'Uber Eats', 'name': 'Uber Eats'})

    def test_prefill_for_transaction_without_merchant_uses_regex(self):
        row = self._txn('t-a', name='ACH TRANSFER 998877')
        pre = rule_stats.prefill_for(row)
        self.assertEqual(pre['match_type'], 'description_regex')
        # The regex must actually match the description it was derived from.
        probe = CategorizationRule(match_type='description_regex',
                                   match_value=pre['match_value'])
        self.assertTrue(categorization.rule_matches(
            probe, description='ACH TRANSFER 998877'))

    def test_prefill_for_group_uses_merchant_contains(self):
        rows = [self._txn('t-a', merchant='Uber Eats'),
                self._txn('t-b', merchant='Uber Eats')]
        groups, _ = rule_stats.group_unmatched(rows)
        self.assertEqual(rule_stats.prefill_for_group(groups[0]),
                         {'match_type': 'merchant_contains',
                          'match_value': 'Uber Eats', 'name': 'Uber Eats'})

    def test_unmatched_page_renders_group_with_create_link(self):
        self._account(company='Alpha LLC')
        self._txn('t-a', merchant='Uber Eats')
        self._txn('t-b', merchant='Uber Eats')
        body = self.client.get(
            '/admin/transactions?state=unmatched').get_data(as_text=True)
        self.assertIn('2</b> unmatched from', body)
        self.assertIn('Uber Eats', body)
        self.assertIn('Create rule from this group', body)
        self.assertIn('match_type=merchant_contains', body)
        self.assertIn('applies_to_company=Alpha+LLC', body)

    def test_per_row_create_button_only_on_unmatched_rows(self):
        self._account()
        self._txn('t-open', merchant='Nobody')
        self._txn('t-done', merchant='Chevron')
        self._je('t-done', state='approved')
        body = self.client.get('/admin/transactions').get_data(as_text=True)
        self.assertIn('+ Rule', body)
        # One button — for the unmatched row only.
        self.assertEqual(body.count('+ Rule'), 1)
        self.assertIn('match_type=merchant_exact', body)


class TestPrefillEditor(AuthoringBase):
    def test_prefill_populates_match_fields_and_company(self):
        body = self.client.get(
            '/admin/rules?prefill=1&match_type=merchant_contains'
            '&match_value=Uber+Eats&name=Uber+Eats'
            '&applies_to_company=Alpha+LLC').get_data(as_text=True)
        self.assertIn('value="Uber Eats"', body)
        self.assertIn('<option value="merchant_contains" selected>', body)
        self.assertIn('Alpha LLC', body)

    def test_prefill_leaves_party_type_on_auto_and_template_empty(self):
        # v0.4.0.9's Auto default and v0.4.0.4's template auto-fill both depend
        # on the prefill NOT supplying those two fields.
        body = self.client.get(
            '/admin/rules?prefill=1&match_type=merchant_exact'
            '&match_value=Chevron').get_data(as_text=True)
        self.assertIn('value="Auto" selected', body)
        self.assertIn('<textarea name="description_template" '
                      'id="description-template" rows="2"', body)
        # The textarea must come back EMPTY, so the editor's auto-fill owns it.
        self.assertRegex(body, r'id="description-template"[^>]*></textarea>')

    def test_prefill_rejects_an_unknown_match_type(self):
        body = self.client.get(
            '/admin/rules?prefill=1&match_type=drop_table'
            '&match_value=x').get_data(as_text=True)
        self.assertNotIn('drop_table', body)
        self.assertIn('<option value="merchant_exact" selected>', body)

    def test_prefill_does_not_create_a_rule(self):
        self.client.get('/admin/rules?prefill=1&match_type=merchant_exact'
                        '&match_value=Chevron&name=Fuel')
        self.assertEqual(CategorizationRule.query.count(), 0)


# ── 4. match counts ─────────────────────────────────────────────────────────

class TestMatchCounts(AuthoringBase):
    def test_rollup_counts_generated_entries_per_rule(self):
        a = self._rule(name='Fuel')
        b = self._rule(name='Meals', match_value='Chipotle')
        for i in range(3):
            self._txn(f't-a{i}', merchant='Chevron')
            self._je(f't-a{i}', rule_id=a.id, state='approved')
        self._txn('t-b0', merchant='Chipotle')
        self._je('t-b0', rule_id=b.id, state='approved')
        result = rule_stats.rollup_match_counts()
        self.assertEqual(result['scanned'], 2)
        self.assertEqual(result['updated'], 2)
        self.assertEqual(db.session.get(CategorizationRule, a.id).match_count, 3)
        self.assertEqual(db.session.get(CategorizationRule, b.id).match_count, 1)

    def test_rollup_is_a_no_op_when_nothing_moved(self):
        a = self._rule()
        self._txn('t-a', merchant='Chevron')
        self._je('t-a', rule_id=a.id)
        rule_stats.rollup_match_counts()
        again = rule_stats.rollup_match_counts()
        self.assertEqual(again['updated'], 0)
        self.assertEqual(again['skipped'], 1)

    def test_archived_versions_credit_their_live_successor(self):
        # An edit clones the rule by design (v0.3.0 non-destructive history). If
        # the count didn't follow the supersede chain, every edit would reset a
        # working rule to 0 and make it look dead.
        old = self._rule(name='Fuel v1')
        self._txn('t-a', merchant='Chevron')
        self._je('t-a', rule_id=old.id)
        new = self._rule(name='Fuel v2')
        old.archived = True
        old.active = False
        old.superseded_by = new.id
        db.session.commit()
        rule_stats.rollup_match_counts()
        self.assertEqual(db.session.get(CategorizationRule, new.id).match_count, 1)
        self.assertEqual(db.session.get(CategorizationRule, old.id).match_count, 0)

    def test_rollup_survives_a_supersede_cycle(self):
        # superseded_by should always point forward; a loop must not hang the
        # scheduler thread that runs this.
        a = self._rule(name='A')
        b = self._rule(name='B')
        a.superseded_by = b.id
        b.superseded_by = a.id
        db.session.commit()
        self._txn('t-a', merchant='Chevron')
        self._je('t-a', rule_id=a.id)
        rule_stats.rollup_match_counts()   # must terminate
        counts = {r.name: r.match_count for r in CategorizationRule.query.all()}
        self.assertEqual(sum(counts.values()), 1)

    def test_rollup_preserves_updated_at(self):
        # updated_at means "when the operator last changed this rule" — a
        # background counter refresh is not an edit.
        rule = self._rule()
        stamp = datetime(2020, 1, 1, 12, 0, 0)
        rule.updated_at = stamp
        db.session.commit()
        self._txn('t-a', merchant='Chevron')
        self._je('t-a', rule_id=rule.id)
        rule_stats.rollup_match_counts()
        self.assertEqual(db.session.get(CategorizationRule, rule.id).updated_at,
                         stamp)

    def test_rules_page_shows_the_match_count_column(self):
        rule = self._rule()
        self._txn('t-a', merchant='Chevron')
        self._je('t-a', rule_id=rule.id)
        rule_stats.rollup_match_counts()
        body = self.client.get('/admin/rules').get_data(as_text=True)
        self.assertIn('Matches', body)
        # v0.5.9 · the column now shows Historical | Active. Historical (the
        # audit lifetime count) renders bold; the split header is present.
        self.assertIn('hist | active', body)
        self.assertIn('<b>1</b>', body)
        self.assertIn('active', body)

    def test_zero_match_rule_is_flagged(self):
        self._rule(name='Dead rule')
        body = self.client.get('/admin/rules').get_data(as_text=True)
        self.assertIn('has never matched a transaction', body)

    def test_matches_column_sorts_both_ways(self):
        low = self._rule(name='Low', priority=1)
        high = self._rule(name='High', priority=200)
        low.match_count, high.match_count = 1, 9
        db.session.commit()
        desc = self.client.get('/admin/rules?sort=matches').get_data(as_text=True)
        self.assertLess(desc.index('>High<'), desc.index('>Low<'))
        asc = self.client.get(
            '/admin/rules?sort=matches_asc').get_data(as_text=True)
        self.assertLess(asc.index('>Low<'), asc.index('>High<'))
        # Default ordering stays priority-first.
        default = self.client.get('/admin/rules').get_data(as_text=True)
        self.assertLess(default.index('>Low<'), default.index('>High<'))

    def test_manual_rollup_endpoint_updates_and_audits(self):
        rule = self._rule()
        self._txn('t-a', merchant='Chevron')
        self._je('t-a', rule_id=rule.id)
        r = self.client.post('/admin/rules/rollup_match_counts',
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.session.get(CategorizationRule, rule.id).match_count, 1)
        self.assertEqual(AuditEvent.query.filter_by(
            event_type='rule_match_counts_rolled_up').count(), 1)

    def test_rerun_rules_refreshes_the_count(self):
        self._account()
        rule = self._rule(match_value='Chevron')
        row = self._txn('t-a', merchant='Chevron')
        erp = FakeERPClient()
        with mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=erp), \
             mock.patch('app.categorization.generate_journal_entry') as gen:
            def _fake(client, r, **kw):
                return self._je(r.plaid_transaction_id, rule_id=rule.id)
            gen.side_effect = _fake
            self.client.post('/admin/transactions/rerun_rules',
                             follow_redirects=True)
        self.assertEqual(db.session.get(CategorizationRule, rule.id).match_count, 1)
        self.assertIsNotNone(row)

    def test_scheduler_interval_helper(self):
        from app.services import scheduler
        self.assertEqual(
            scheduler.match_count_rollup_interval_or_none(self.app), 24)
        self.app.config['RULE_MATCH_COUNT_ROLLUP_INTERVAL_HOURS'] = 0
        self.assertIsNone(
            scheduler.match_count_rollup_interval_or_none(self.app))
        self.app.config['RULE_MATCH_COUNT_ROLLUP_INTERVAL_HOURS'] = 'nonsense'
        self.assertEqual(
            scheduler.match_count_rollup_interval_or_none(self.app), 24)


# ── 5. scope-mismatch warning at save ───────────────────────────────────────

class ScopeWarningBase(AuthoringBase):
    """A configured ERPNext whose 'Fuel Expense - BL' account lives in Beta LLC,
    so a rule scoped to Alpha LLC pointing at it is a genuine mismatch."""
    def setUp(self):
        super().setUp()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', 'Alpha LLC')
        self.erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Fuel Expense', 'name': 'Fuel Expense - AL',
             'company': 'Alpha LLC', 'root_type': 'Expense',
             'account_type': 'Expense Account'},
            {'account_name': 'Fuel Expense', 'name': 'Fuel Expense - BL',
             'company': 'Beta LLC', 'root_type': 'Expense',
             'account_type': 'Expense Account'},
        ])

    def _save(self, **kw):
        data = dict(name='Fuel', priority='10', active='1',
                    match_type='merchant_contains', match_value='Chevron',
                    offset_account='Fuel Expense - BL',
                    applies_to_company='Alpha LLC',
                    party_type='', party_name='', description_template='')
        data.update(kw)
        with mock.patch('app.erpnext_bank.get_client', return_value=self.erp), \
             mock.patch('app.erpnext_accounts.get_client', return_value=self.erp):
            return self.client.post('/admin/rules/save', data=data,
                                    follow_redirects=True)


class TestScopeWarning(ScopeWarningBase):
    def test_warning_fires_when_offset_company_differs(self):
        body = self._save().get_data(as_text=True)
        self.assertIn('belongs to another Company', body)
        self.assertIn('Beta LLC', body)
        self.assertIn('Save anyway', body)
        # Nothing persisted — the warning is raised BEFORE the save.
        self.assertEqual(CategorizationRule.query.count(), 0)

    def test_second_submit_with_confirm_token_persists(self):
        self._save()
        self._save(confirm_scope_mismatch='1')
        rules = CategorizationRule.query.all()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].offset_account, 'Fuel Expense - BL')

    def test_no_warning_when_companies_agree(self):
        self._save(offset_account='Fuel Expense - AL')
        self.assertEqual(CategorizationRule.query.count(), 1)

    def test_no_warning_for_a_company_agnostic_rule(self):
        # Mode B (v0.4.0.3): a logical name resolves per-Company at JE time, so
        # there is no single Company for it to disagree with.
        self._save(applies_to_company='', offset_account='Fuel Expense')
        self.assertEqual(CategorizationRule.query.count(), 1)

    def test_no_warning_when_erpnext_is_unconfigured(self):
        erpnext_settings.save('', '', '', '')
        self._save()
        self.assertEqual(CategorizationRule.query.count(), 1)

    def test_warning_preserves_the_edited_rule_id(self):
        # The re-rendered form must keep the id, or confirming the warning would
        # create a SECOND rule instead of superseding the one being edited.
        self._save(offset_account='Fuel Expense - AL')
        original = CategorizationRule.query.one()
        body = self._save(id=str(original.id)).get_data(as_text=True)
        self.assertIn(f'name="id" value="{original.id}"', body)
        self._save(id=str(original.id), confirm_scope_mismatch='1')
        live = CategorizationRule.query.filter_by(archived=False).all()
        self.assertEqual(len(live), 1)
        self.assertEqual(
            db.session.get(CategorizationRule, original.id).superseded_by,
            live[0].id)


# ── 6. resolved Company label on the Offset Account field ───────────────────

class TestOffsetCompanyLabel(AuthoringBase):
    def test_scoped_rule_names_its_company_in_the_label(self):
        rule = self._rule(applies_to_company='Alpha LLC')
        body = self.client.get(
            f'/admin/rules?edit={rule.id}').get_data(as_text=True)
        self.assertIn('id="oa-company-label"', body)
        self.assertIn('(in <b style="color:#2e9e5b">Alpha LLC</b>)', body)

    def test_agnostic_rule_says_the_name_is_logical(self):
        rule = self._rule(applies_to_company=None)
        body = self.client.get(
            f'/admin/rules?edit={rule.id}').get_data(as_text=True)
        self.assertIn('resolves per-Company at JE time', body)

    def test_label_is_kept_in_step_by_the_scope_select(self):
        body = self.client.get('/admin/rules').get_data(as_text=True)
        self.assertIn('updateOffsetCompanyLabel', body)
        # It must run from updateOffsetModeHint, which is what the
        # applies_to_company change handler calls (v0.4.0.3).
        self.assertIn('function updateOffsetModeHint() {\n    '
                      'updateOffsetCompanyLabel();', body)


# ── regressions the bundle must not disturb ─────────────────────────────────

class TestRegressions(ScopeWarningBase):
    def test_v4009_party_type_validation_still_blocks(self):
        # An Expense offset account cannot carry a Supplier party — ERPNext
        # refuses it at submit, so the save is refused here.
        body = self._save(offset_account='Fuel Expense - AL',
                          applies_to_company='Alpha LLC',
                          party_type='Supplier').get_data(as_text=True)
        self.assertEqual(CategorizationRule.query.count(), 0)
        self.assertIn('⚠', body)

    def test_v4003_mode_a_and_mode_b_offsets_are_preserved(self):
        # Mode A: a scoped rule keeps its fully-qualified offset verbatim.
        self._save(offset_account='Fuel Expense - AL')
        scoped = CategorizationRule.query.one()
        self.assertEqual(scoped.offset_account, 'Fuel Expense - AL')
        self.assertEqual(scoped.applies_to_company, 'Alpha LLC')
        # Mode B: an agnostic rule keeps its LOGICAL name and no Company.
        self._save(name='Meals', match_value='Chipotle',
                   applies_to_company='', offset_account='Meals')
        agnostic = CategorizationRule.query.filter_by(name='Meals').one()
        self.assertEqual(agnostic.offset_account, 'Meals')
        self.assertIsNone(agnostic.applies_to_company)

    def test_v4004_description_template_autofill_still_present(self):
        body = self.client.get('/admin/rules').get_data(as_text=True)
        self.assertIn('DEFAULT_TEMPLATES', body)
        self.assertIn('maybeAutofill', body)
        r = self.client.get('/api/rules/preview_description'
                            '?match_type=merchant_exact&match_value=Chevron'
                            '&offset_account=Fuel+Expense+-+AL')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Fuel Expense', r.get_json()['template'])

    def test_v4002_cross_company_push_guard_still_active(self):
        from app import erpnext_accounts
        doc = {'company': 'Alpha LLC',
               'accounts': [{'account': 'Fuel Expense - BL'},
                            {'account': 'Fuel Expense - AL'}]}
        mismatches = erpnext_accounts.je_company_mismatches(self.erp, doc)
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]['account'], 'Fuel Expense - BL')
        self.assertEqual(mismatches[0]['account_company'], 'Beta LLC')


# ── the additive migration ──────────────────────────────────────────────────

class TestMatchCountMigration(AuthoringBase):
    """`match_count` reaches an EXISTING database only through SCHEMA_MIGRATIONS
    — create_all() never adds a column to a table it didn't just build."""

    def _columns(self):
        from sqlalchemy import inspect
        return {c['name'] for c in
                inspect(db.engine).get_columns('categorization_rules')}

    def _downgrade(self):
        from sqlalchemy import text
        with db.engine.begin() as conn:
            # Indexed, so the index goes first — SQLite refuses to drop an
            # indexed column (same dance as balance_only in test_migrations).
            conn.execute(text('DROP INDEX IF EXISTS '
                              'ix_categorization_rules_match_count'))
            conn.execute(text('ALTER TABLE categorization_rules '
                              'DROP COLUMN match_count'))

    def test_listed_in_schema_migrations(self):
        from app import migrations
        self.assertIn(('categorization_rules', 'match_count',
                       'INTEGER DEFAULT 0'), migrations.SCHEMA_MIGRATIONS)

    def test_upgrade_adds_the_column_and_backfills_to_zero(self):
        from app import migrations
        rule = self._rule()
        rule_id = rule.id
        db.session.remove()
        self._downgrade()
        self.assertNotIn('match_count', self._columns())
        migrations.run_migrations()
        self.assertIn('match_count', self._columns())
        # The pre-existing rule survives and reads as "not rolled up yet".
        self.assertEqual(
            db.session.get(CategorizationRule, rule_id).match_count, 0)

    def test_upgrade_is_idempotent(self):
        from app import migrations
        db.session.remove()
        self._downgrade()
        migrations.run_migrations()
        migrations.run_migrations()
        self.assertIn('match_count', self._columns())

    def test_rollup_fills_in_history_after_the_upgrade(self):
        # The real upgrade path: an install with existing generated entries gets
        # its true counts on the first rollup, not just zeroes.
        from app import migrations
        rule = self._rule()
        rule_id = rule.id
        self._txn('t-a', merchant='Chevron')
        self._je('t-a', rule_id=rule_id)
        db.session.remove()
        self._downgrade()
        migrations.run_migrations()
        rule_stats.rollup_match_counts()
        self.assertEqual(
            db.session.get(CategorizationRule, rule_id).match_count, 1)


if __name__ == '__main__':
    unittest.main()
