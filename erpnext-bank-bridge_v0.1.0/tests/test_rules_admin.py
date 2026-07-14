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

from app import create_app, db, crypto  # noqa: E402
from app.models import (CategorizationRule, GeneratedJournalEntry,  # noqa: E402
                        Supplier)


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

    def test_edit_rule(self):
        self._add()
        rid = CategorizationRule.query.first().id
        self.client.post('/admin/rules/save', data=dict(
            id=str(rid), name='Fuel v2', priority='5', active='1',
            match_type='merchant_exact', match_value='Chevron',
            debit_account='Fuel - EC', credit_account='Bank - EC',
            party_type='Supplier', party_name='', description_template=''),
            follow_redirects=True)
        rule = db.session.get(CategorizationRule, rid)
        self.assertEqual(rule.name, 'Fuel v2')
        self.assertEqual(rule.priority, 5)
        self.assertEqual(rule.match_type, 'merchant_exact')
        self.assertEqual(rule.party_type, 'Supplier')

    def test_toggle_rule(self):
        self._add()
        rule = CategorizationRule.query.first()
        self.assertTrue(rule.active)
        self.client.post('/admin/rules/toggle', data={'id': str(rule.id)},
                         follow_redirects=True)
        db.session.refresh(rule)
        self.assertFalse(rule.active)

    def test_delete_rule(self):
        self._add()
        rid = CategorizationRule.query.first().id
        self.client.post('/admin/rules/delete', data={'id': str(rid)},
                         follow_redirects=True)
        self.assertEqual(CategorizationRule.query.count(), 0)

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


if __name__ == '__main__':
    unittest.main()
