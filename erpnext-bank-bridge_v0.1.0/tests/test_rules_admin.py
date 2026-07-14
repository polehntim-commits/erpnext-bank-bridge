# SPDX-License-Identifier: MIT
"""Admin UI for v0.3.0: rules CRUD + test endpoint, suppliers list/edit,
generated-entries audit + approve/reject.

    cd erpnext-bank-bridge_v0.1.0
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from datetime import date  # noqa: E402

from app import create_app, db, crypto  # noqa: E402
from app import categorization  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        GeneratedJournalEntry, Supplier)


class AdminBase(unittest.TestCase):
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


class TestNewPagesRender(AdminBase):
    def test_pages_render(self):
        for path in ('/admin/rules', '/admin/suppliers', '/admin/generated_entries'):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, f'{path} → {r.status_code}')

    def test_nav_links_present(self):
        r = self.client.get('/admin')
        self.assertIn(b'/admin/rules', r.data)
        self.assertIn(b'/admin/suppliers', r.data)
        self.assertIn(b'/admin/generated_entries', r.data)


class TestRulesCrud(AdminBase):
    def _add(self, **kw):
        data = dict(name='Fuel', priority='10', active='1',
                    match_type='merchant_contains', match_value='Chevron',
                    debit_account='Fuel - EC', credit_account='Bank - EC',
                    party_type='', party_name='', description_template='')
        data.update(kw)
        return self.client.post('/admin/rules/save', data=data,
                                follow_redirects=True)

    def test_create_rule(self):
        self._add()
        rules = CategorizationRule.query.all()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].name, 'Fuel')
        self.assertEqual(rules[0].priority, 10)
        self.assertTrue(rules[0].active)

    def test_reject_unknown_match_type(self):
        self._add(match_type='bogus')
        self.assertEqual(CategorizationRule.query.count(), 0)

    def test_edit_rule_supersedes_non_destructively(self):
        self._add()
        old = CategorizationRule.query.first()
        rid = old.id
        self.client.post('/admin/rules/save', data=dict(
            id=str(rid), name='Fuel v2', priority='5', active='1',
            match_type='merchant_exact', match_value='Chevron',
            debit_account='Fuel - EC', credit_account='Bank - EC',
            party_type='Supplier', party_name='', description_template=''),
            follow_redirects=True)
        # Old version is archived + points forward; new version is current.
        old = db.session.get(CategorizationRule, rid)
        self.assertEqual(old.name, 'Fuel')          # unchanged (history)
        self.assertTrue(old.archived)
        self.assertFalse(old.active)
        self.assertIsNotNone(old.superseded_by)
        new_rule = db.session.get(CategorizationRule, old.superseded_by)
        self.assertEqual(new_rule.name, 'Fuel v2')
        self.assertEqual(new_rule.priority, 5)
        self.assertEqual(new_rule.match_type, 'merchant_exact')
        self.assertEqual(new_rule.party_type, 'Supplier')
        self.assertFalse(new_rule.archived)
        # Exactly one LIVE rule remains.
        live = CategorizationRule.query.filter_by(archived=False).all()
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].id, new_rule.id)
        # Both rows still exist — nothing hard-deleted.
        self.assertEqual(CategorizationRule.query.count(), 2)

    def test_toggle_rule(self):
        self._add()
        rule = CategorizationRule.query.first()
        self.assertTrue(rule.active)
        self.client.post('/admin/rules/toggle', data={'id': str(rule.id)},
                         follow_redirects=True)
        db.session.refresh(rule)
        self.assertFalse(rule.active)

    def test_delete_rule_archives_not_removes(self):
        self._add()
        rid = CategorizationRule.query.first().id
        self.client.post('/admin/rules/delete', data={'id': str(rid)},
                         follow_redirects=True)
        # Row persists (audit/history) but is archived + inactive.
        self.assertEqual(CategorizationRule.query.count(), 1)
        rule = db.session.get(CategorizationRule, rid)
        self.assertTrue(rule.archived)
        self.assertFalse(rule.active)
        self.assertEqual(CategorizationRule.query.filter_by(archived=False).count(), 0)

    def test_unchecked_active_is_false(self):
        self._add(active='')       # checkbox unchecked → absent
        self.assertFalse(CategorizationRule.query.first().active)


class TestRuleTestEndpoint(AdminBase):
    def test_matching_rule_shows_preview(self):
        db.session.add(CategorizationRule(
            name='Fuel', priority=10, active=True, match_type='merchant_contains',
            match_value='Chevron', debit_account='Fuel - EC',
            credit_account='Bank - EC'))
        db.session.commit()
        r = self.client.post('/admin/rules/test', data={
            'merchant_name': 'Chevron', 'description': 'CHEVRON 01',
            'amount': '42.50', 'category': 'GAS'})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Matched rule', r.data)
        self.assertIn(b'Fuel - EC', r.data)             # JE preview account
        self.assertIn(b'debit_in_account_currency', r.data)

    def test_no_match_message(self):
        db.session.add(CategorizationRule(
            name='Groceries', priority=10, active=True,
            match_type='merchant_contains', match_value='Safeway',
            debit_account='X', credit_account='Y'))
        db.session.commit()
        r = self.client.post('/admin/rules/test', data={
            'merchant_name': 'Chevron', 'amount': '10'})
        self.assertIn(b'No rule matched', r.data)

    def test_offset_rule_preview_shows_bank_placeholder(self):
        db.session.add(CategorizationRule(
            name='Fuel', priority=10, active=True, match_type='merchant_contains',
            match_value='Chevron', offset_account='Fuel Expense - EC',
            offset_direction='auto'))
        db.session.commit()
        r = self.client.post('/admin/rules/test', data={
            'merchant_name': 'Chevron', 'amount': '42.50'})
        self.assertIn(b'Matched rule', r.data)
        self.assertIn(b'Fuel Expense - EC', r.data)
        self.assertIn(b"the transaction&#39;s bank account", r.data)


class TestOffsetRuleForm(AdminBase):
    """v0.3.1 · the rule form is a single offset account + direction, not a
    debit/credit pair."""

    def test_form_renders_offset_not_debit_credit(self):
        r = self.client.get('/admin/rules')
        body = r.get_data(as_text=True)
        self.assertIn('name="offset_account"', body)
        self.assertIn('name="offset_direction"', body)
        self.assertNotIn('name="debit_account"', body)
        self.assertNotIn('name="credit_account"', body)
        # Tooltip wording present.
        self.assertIn('automatically determined from the transaction', body)

    def test_create_offset_rule(self):
        self.client.post('/admin/rules/save', data=dict(
            name='Fuel', priority='10', active='1',
            match_type='merchant_contains', match_value='Chevron',
            offset_account='Fuel Expense - EC', offset_direction='always_debit',
            party_type='', party_name='', description_template=''),
            follow_redirects=True)
        rule = CategorizationRule.query.first()
        self.assertEqual(rule.offset_account, 'Fuel Expense - EC')
        self.assertEqual(rule.offset_direction, 'always_debit')

    def test_bad_direction_defaults_auto(self):
        self.client.post('/admin/rules/save', data=dict(
            name='Fuel', priority='10', active='1',
            match_type='merchant_contains', match_value='Chevron',
            offset_account='Fuel Expense - EC', offset_direction='sideways'),
            follow_redirects=True)
        self.assertEqual(CategorizationRule.query.first().offset_direction, 'auto')

    def test_rules_table_shows_offset_column(self):
        db.session.add(CategorizationRule(
            name='Fuel', priority=10, active=True, match_type='merchant_contains',
            match_value='Chevron', offset_account='Fuel Expense - EC',
            offset_direction='auto'))
        db.session.commit()
        body = self.client.get('/admin/rules').get_data(as_text=True)
        self.assertIn('Offset account', body)
        self.assertIn('Fuel Expense - EC', body)


class TestSuppliersUi(AdminBase):
    def _supplier(self):
        s = Supplier(merchant_name='CHEVRON', normalized_name='Chevron',
                     erpnext_supplier_name='Chevron', transaction_count=3,
                     total_amount=99.0)
        db.session.add(s)
        db.session.commit()
        return s

    def test_list_and_search(self):
        self._supplier()
        r = self.client.get('/admin/suppliers')
        self.assertIn(b'Chevron', r.data)
        r = self.client.get('/admin/suppliers?q=chev')
        self.assertIn(b'Chevron', r.data)
        r = self.client.get('/admin/suppliers?q=nomatch')
        self.assertNotIn(b'>Chevron<', r.data)

    def test_edit_relink(self):
        s = self._supplier()
        self.client.post('/admin/suppliers/edit', data={
            'id': str(s.id), 'normalized_name': 'Chevron',
            'erpnext_supplier_name': 'Chevron Corp'}, follow_redirects=True)
        db.session.refresh(s)
        self.assertEqual(s.erpnext_supplier_name, 'Chevron Corp')

    def test_edit_rejects_duplicate_normalized(self):
        self._supplier()
        other = Supplier(merchant_name='Shell', normalized_name='Shell')
        db.session.add(other)
        db.session.commit()
        self.client.post('/admin/suppliers/edit', data={
            'id': str(other.id), 'normalized_name': 'Chevron',
            'erpnext_supplier_name': ''}, follow_redirects=True)
        db.session.refresh(other)
        self.assertEqual(other.normalized_name, 'Shell')   # unchanged


class TestGeneratedEntriesUi(AdminBase):
    def _entry(self, state='pending_review', je='ACC-JV-0001'):
        g = GeneratedJournalEntry(
            plaid_transaction_id='t1', rule_id=1, rule_name='Fuel',
            erpnext_journal_entry_name=je, state=state, amount=42.5,
            merchant_name='Chevron', description='Fuel purchase')
        db.session.add(g)
        db.session.commit()
        return g

    def test_list_renders(self):
        self._entry()
        r = self.client.get('/admin/generated_entries')
        self.assertIn(b'Chevron', r.data)
        self.assertIn(b'ACC-JV-0001', r.data)

    def test_filter_by_state(self):
        self._entry(state='pending_review')
        r = self.client.get('/admin/generated_entries?state=approved')
        self.assertNotIn(b'ACC-JV-0001', r.data)

    def test_reject_flips_state(self):
        # No ERPNext configured → reject still flips the audit state locally.
        g = self._entry()
        self.client.post('/admin/generated_entries/reject',
                         data={'id': str(g.id)}, follow_redirects=True)
        db.session.refresh(g)
        self.assertEqual(g.state, 'rejected')

    def test_approve_without_erpnext_reports_failure(self):
        g = self._entry()
        r = self.client.post('/admin/generated_entries/approve',
                             data={'id': str(g.id)}, follow_redirects=True)
        db.session.refresh(g)
        # ERPNext not configured → cannot submit → stays pending.
        self.assertEqual(g.state, 'pending_review')
        self.assertIn(b'Could not submit', r.data)


class TestRuleBuilderAutocomplete(AdminBase):
    """v0.3.2 · autocomplete feeds + name suggestion + conflict detection."""

    _txn_seq = 0

    def _txn(self, merchant, amount=10.0, category='', removed=False):
        TestRuleBuilderAutocomplete._txn_seq += 1
        n = TestRuleBuilderAutocomplete._txn_seq
        db.session.add(BankTransaction(
            plaid_transaction_id=f't{n}', account_id='acc1',
            merchant_name=merchant, amount=amount, category=category,
            date=date(2026, 1, 1), removed=removed))

    def test_known_merchants_endpoint_sorted_by_count(self):
        # Chevron seen 3×, Costco 2×, Shell 1×.
        for _ in range(3):
            self._txn('Chevron', amount=40.0, category='Transportation > Gas Stations')
        for _ in range(2):
            self._txn('Costco', amount=120.0, category='Food and Drink > Groceries')
        self._txn('Shell', amount=35.0)
        db.session.commit()
        r = self.client.get('/api/rules/known_merchants')
        self.assertEqual(r.status_code, 200)
        merchants = r.get_json()['merchants']
        names = [m['name'] for m in merchants]
        self.assertEqual(names, ['Chevron', 'Costco', 'Shell'])   # count desc
        chevron = merchants[0]
        self.assertEqual(chevron['count'], 3)
        self.assertEqual(chevron['total_amount'], 120.0)          # 3 × $40
        # Category attached → name suggestion derives "Fuel — Chevron".
        self.assertEqual(chevron['suggested_name'], 'Fuel — Chevron')

    def test_known_merchants_excludes_removed_and_blank(self):
        self._txn('Chevron', category='Transportation > Gas Stations')
        self._txn('', amount=5.0)                 # blank merchant → excluded
        self._txn('Ghost', removed=True)          # removed → excluded
        db.session.commit()
        names = [m['name'] for m in
                 self.client.get('/api/rules/known_merchants').get_json()['merchants']]
        self.assertEqual(names, ['Chevron'])

    def test_known_categories_endpoint_hierarchy_preserved(self):
        deep = 'Food and Drink > Restaurants > Coffee Shop'
        for _ in range(2):
            self._txn('Starbucks', category=deep)
        self._txn('Costco', category='Food and Drink > Groceries')
        db.session.commit()
        r = self.client.get('/api/rules/known_categories')
        self.assertEqual(r.status_code, 200)
        cats = r.get_json()['categories']
        paths = [c['path'] for c in cats]
        self.assertEqual(paths[0], deep)          # count desc → coffee first
        self.assertIn('Food and Drink > Groceries', paths)
        # Full hierarchy preserved verbatim (not truncated to a leaf).
        self.assertEqual(cats[0]['count'], 2)
        self.assertEqual(cats[0]['alias'], 'Coffee')

    def test_name_suggestion_uses_category_alias(self):
        self.assertEqual(
            categorization.suggest_rule_name('Chevron', 'Transportation > Gas Stations'),
            'Fuel — Chevron')
        # Raw PFC leaf label resolves to the same alias.
        self.assertEqual(categorization.category_alias('GAS_STATIONS'), 'Fuel')
        # Unknown category → no alias, falls back to the bare match value.
        self.assertEqual(categorization.suggest_rule_name('Acme', 'Nonsense > Zzz'), 'Acme')

    def test_dropdown_shows_has_rule_badge_for_covered_merchants(self):
        self._txn('Chevron', category='Transportation > Gas Stations')
        self._txn('Costco', category='Food and Drink > Groceries')
        db.session.commit()
        db.session.add(CategorizationRule(
            name='Fuel', priority=100, active=True,
            match_type='merchant_contains', match_value='Chevron',
            offset_account='Fuel - EC', offset_direction='auto'))
        db.session.commit()
        merchants = self.client.get(
            '/api/rules/known_merchants').get_json()['merchants']
        by_name = {m['name']: m for m in merchants}
        self.assertTrue(by_name['Chevron']['has_rule'])
        self.assertFalse(by_name['Costco']['has_rule'])
        # And the badge string renders in the served page's JS payload.
        self.assertIn('already has rule',
                      self.client.get('/admin/rules').get_data(as_text=True))

    def test_conflict_detection_warns_on_lower_priority(self):
        # Existing higher-priority (lower number) rule already matches Chevron.
        db.session.add(CategorizationRule(
            name='Old Fuel Rule', priority=100, active=True,
            match_type='merchant_contains', match_value='Chevron',
            offset_account='Fuel - EC', offset_direction='auto'))
        db.session.commit()
        r = self.client.post('/admin/rules/save', data=dict(
            name='New Fuel', priority='200', active='1',
            match_type='merchant_exact', match_value='Chevron',
            offset_account='Fuel Expense - EC', offset_direction='auto'),
            follow_redirects=True)
        body = r.get_data(as_text=True)
        self.assertIn('already matches', body)
        self.assertIn('Old Fuel Rule', body)
        # Non-blocking: the new rule was still saved.
        self.assertEqual(
            CategorizationRule.query.filter_by(name='New Fuel').count(), 1)

    def test_conflict_detection_silent_when_new_rule_wins(self):
        # New rule at HIGHER priority (lower number) than the existing one →
        # it fires first, so no shadow warning.
        db.session.add(CategorizationRule(
            name='Old Fuel Rule', priority=200, active=True,
            match_type='merchant_contains', match_value='Chevron',
            offset_account='Fuel - EC', offset_direction='auto'))
        db.session.commit()
        r = self.client.post('/admin/rules/save', data=dict(
            name='New Fuel', priority='50', active='1',
            match_type='merchant_exact', match_value='Chevron',
            offset_account='Fuel Expense - EC', offset_direction='auto'),
            follow_redirects=True)
        self.assertNotIn('already matches', r.get_data(as_text=True))

    def test_rule_form_has_autocomplete_widgets(self):
        body = self.client.get('/admin/rules').get_data(as_text=True)
        self.assertIn('id="mv-dd"', body)         # merchant dropdown
        self.assertIn('id="mv-cat"', body)        # category picker
        self.assertIn('id="mv-regex"', body)      # regex tester
        self.assertIn('/api/rules/known_merchants', body)
        self.assertIn('id="name-hint"', body)     # name suggestion slot


if __name__ == '__main__':
    unittest.main()
