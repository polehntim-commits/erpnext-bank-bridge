# SPDX-License-Identifier: MIT
"""The /admin/intercompany review screen and the Rules editor's
`ignore_for_paired` checkbox (v0.4.1).

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import erpnext_settings, intercompany  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        IntercompanyTransferPair, PlaidAccount, PlaidItem)

from tests.fakes import FakeERPClient  # noqa: E402

FARM = 'Farm LLC'
PERSONAL = 'Personal LLC'
FARM_ACC = 'acct-farm'
PERSONAL_ACC = 'acct-personal'
FARM_GL = 'Farm Checking - FL'
PERSONAL_GL = 'Personal Checking - PL'

CHART = [
    {'account_name': 'Current Assets', 'company': FARM, 'is_group': 1,
     'root_type': 'Asset', 'name': 'Current Assets - FL'},
    {'account_name': 'Current Liabilities', 'company': FARM, 'is_group': 1,
     'root_type': 'Liability', 'name': 'Current Liabilities - FL'},
    {'account_name': 'Current Assets', 'company': PERSONAL, 'is_group': 1,
     'root_type': 'Asset', 'name': 'Current Assets - PL'},
    {'account_name': 'Current Liabilities', 'company': PERSONAL, 'is_group': 1,
     'root_type': 'Liability', 'name': 'Current Liabilities - PL'},
]


class IntercompanyUIBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir, 'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False})
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', FARM)
        self.erp = FakeERPClient(chart_accounts=CHART,
                                 companies=[FARM, PERSONAL])

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _accounts(self):
        for item_id, company in (('item-farm', FARM), ('item-personal', PERSONAL)):
            db.session.add(PlaidItem(
                item_id=item_id, access_token_encrypted=crypto.encrypt('t'),
                institution_id='ins_1', status='active', owning_company=company))
        db.session.add(PlaidAccount(
            account_id=FARM_ACC, item_id='item-farm', name='Farm Checking',
            type='depository', subtype='checking',
            erpnext_bank_account_name='Farm Checking - WF',
            erpnext_gl_account_name=FARM_GL, sync_enabled=True))
        db.session.add(PlaidAccount(
            account_id=PERSONAL_ACC, item_id='item-personal',
            name='Personal Checking', type='depository', subtype='checking',
            erpnext_bank_account_name='Personal Checking - WF',
            erpnext_gl_account_name=PERSONAL_GL, sync_enabled=True))
        db.session.commit()

    def _pair(self, amount=10000.0, booked=True, state='pending'):
        self._accounts()
        db.session.add(BankTransaction(
            plaid_transaction_id='t-out', account_id=FARM_ACC, amount=amount,
            date=date(2026, 7, 10), name='Transfer to Personal',
            erpnext_bank_transaction_id='ACC-BTN-0001'))
        db.session.add(BankTransaction(
            plaid_transaction_id='t-in', account_id=PERSONAL_ACC, amount=-amount,
            date=date(2026, 7, 10), name='Transfer from Farm',
            erpnext_bank_transaction_id='ACC-BTN-0002'))
        db.session.commit()
        pair = intercompany.detect_pairs()[0]
        if booked:
            intercompany.generate_pair_journal_entries(self.erp, pair)
        if state != 'pending':
            pair.state = state
            db.session.commit()
        return pair

    def _patched_erp(self):
        """Route the blueprint's ERPNext lookup at the fake."""
        return mock.patch('app.sync_engine.get_erp_client_or_none',
                          return_value=self.erp)


# ── the page ────────────────────────────────────────────────────────────

class TestIntercompanyPage(IntercompanyUIBase):
    def test_page_renders_empty(self):
        r = self.client.get('/admin/intercompany')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'No intercompany transfers detected', r.data)

    def test_nav_links_to_the_page(self):
        r = self.client.get('/admin')
        self.assertIn(b'/admin/intercompany', r.data)

    def test_idle_banner_without_a_second_company(self):
        r = self.client.get('/admin/intercompany')
        self.assertIn(b'Detection is idle', r.data)

    def test_idle_banner_disappears_with_two_companies(self):
        self._accounts()
        r = self.client.get('/admin/intercompany')
        self.assertNotIn(b'Detection is idle', r.data)

    def test_lists_a_detected_pair_with_both_sides(self):
        self._pair()
        r = self.client.get('/admin/intercompany')
        self.assertIn(FARM.encode(), r.data)
        self.assertIn(PERSONAL.encode(), r.data)
        self.assertIn(b'10000.00', r.data)
        self.assertIn(b'Transfer to Personal', r.data)

    def test_shows_both_journal_entry_names(self):
        pair = self._pair()
        r = self.client.get('/admin/intercompany')
        self.assertIn(pair.from_journal_entry.encode(), r.data)
        self.assertIn(pair.to_journal_entry.encode(), r.data)

    def _row_actions(self) -> str:
        """Just the per-row actions cell — the intro paragraph also says the
        word 'Approve', so asserting against the whole page would pass whatever
        buttons were actually rendered."""
        html = self.client.get('/admin/intercompany').data.decode()
        start = html.index('class="ic-actions-cell"')
        return html[start:html.index('</td>', start)]

    def test_offers_approve_and_unpair_on_a_booked_pending_pair(self):
        self._pair()
        actions = self._row_actions()
        self.assertIn('>Approve<', actions)
        self.assertIn('>Unpair<', actions)
        self.assertNotIn('>Retry<', actions)

    def test_offers_retry_not_approve_on_an_unbooked_pair(self):
        # Nothing to submit yet, so offering Approve would only ever error.
        self._pair(booked=False)
        actions = self._row_actions()
        self.assertIn('>Retry<', actions)
        self.assertNotIn('>Approve<', actions)

    def test_offers_only_unpair_on_an_approved_pair(self):
        self._pair(state='approved')
        actions = self._row_actions()
        self.assertIn('Unpair', actions)
        self.assertNotIn('>Approve<', actions)

    def test_state_filter(self):
        self._pair()
        self.assertIn(b'10000.00',
                      self.client.get('/admin/intercompany?state=pending').data)
        self.assertIn(b'No intercompany transfers',
                      self.client.get('/admin/intercompany?state=approved').data)

    def test_company_filter_matches_either_side(self):
        self._pair()
        # The Farm is the SOURCE and Personal the TARGET — filtering on either
        # must find the transfer, since both entities were involved in it.
        for company in (FARM, PERSONAL):
            r = self.client.get(f'/admin/intercompany?company={company}')
            self.assertIn(b'10000.00', r.data, f'{company} filter lost the pair')
        r = self.client.get('/admin/intercompany?company=Nobody+LLC')
        self.assertIn(b'No intercompany transfers', r.data)

    def test_min_confidence_filter(self):
        self._pair()
        self.assertIn(b'10000.00',
                      self.client.get('/admin/intercompany?min_confidence=0.5').data)
        self.assertIn(b'No intercompany transfers',
                      self.client.get('/admin/intercompany?min_confidence=0.99').data)

    def test_a_malformed_confidence_filter_is_ignored_not_fatal(self):
        self._pair()
        r = self.client.get('/admin/intercompany?min_confidence=abc')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'10000.00', r.data)


# ── actions ─────────────────────────────────────────────────────────────

class TestIntercompanyActions(IntercompanyUIBase):
    def test_approve_submits_both_and_redirects(self):
        pair = self._pair()
        with self._patched_erp():
            r = self.client.post('/admin/intercompany/approve',
                                 data={'id': pair.id})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            db.session.get(IntercompanyTransferPair, pair.id).state, 'approved')
        self.assertEqual(self.erp.submitted,
                         {pair.from_journal_entry, pair.to_journal_entry})

    def test_approve_returns_json_to_a_fetch_caller(self):
        pair = self._pair()
        with self._patched_erp():
            r = self.client.post('/admin/intercompany/approve',
                                 data={'id': pair.id},
                                 headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual(r.get_json()['state'], 'approved')

    def test_unpair_clears_both_transactions(self):
        pair = self._pair()
        with self._patched_erp():
            self.client.post('/admin/intercompany/reject', data={'id': pair.id})
        self.assertEqual(
            db.session.get(IntercompanyTransferPair, pair.id).state, 'rejected')
        for tid in ('t-out', 't-in'):
            row = BankTransaction.query.filter_by(
                plaid_transaction_id=tid).first()
            self.assertIsNone(row.intercompany_pair_id)

    def test_retry_books_a_previously_unbooked_pair(self):
        pair = self._pair(booked=False)
        with self._patched_erp():
            self.client.post('/admin/intercompany/retry', data={'id': pair.id})
        self.assertIsNotNone(
            db.session.get(IntercompanyTransferPair, pair.id).from_journal_entry)

    def test_a_bad_id_is_rejected(self):
        r = self.client.post('/admin/intercompany/approve',
                             data={'id': 'nope'},
                             headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.status_code, 400)

    def test_an_unknown_id_is_a_404(self):
        r = self.client.post('/admin/intercompany/approve',
                             data={'id': '9999'},
                             headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.status_code, 404)

    def test_approving_an_unbooked_pair_reports_the_reason(self):
        pair = self._pair(booked=False)
        with self._patched_erp():
            r = self.client.post('/admin/intercompany/approve',
                                 data={'id': pair.id},
                                 headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.status_code, 409)
        self.assertFalse(r.get_json()['ok'])


class TestIntercompanyBulk(IntercompanyUIBase):
    def test_bulk_approve_selected(self):
        pair = self._pair()
        with self._patched_erp():
            r = self.client.post('/admin/intercompany/bulk',
                                 data={'action': 'approve', 'ids': [pair.id]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            db.session.get(IntercompanyTransferPair, pair.id).state, 'approved')

    def test_bulk_with_nothing_selected_acts_on_all_pending(self):
        pair = self._pair()
        with self._patched_erp():
            r = self.client.post('/admin/intercompany/bulk',
                                 data={'action': 'approve'},
                                 headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.get_json()['done'], 1)
        self.assertEqual(
            db.session.get(IntercompanyTransferPair, pair.id).state, 'approved')

    def test_bulk_unpair(self):
        pair = self._pair()
        with self._patched_erp():
            self.client.post('/admin/intercompany/bulk',
                             data={'action': 'reject', 'ids': [pair.id]})
        self.assertEqual(
            db.session.get(IntercompanyTransferPair, pair.id).state, 'rejected')

    def test_an_unknown_bulk_action_is_rejected(self):
        r = self.client.post('/admin/intercompany/bulk',
                             data={'action': 'explode'},
                             headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.status_code, 400)

    def test_partial_failure_is_reported_not_rolled_back(self):
        good = self._pair()
        # A second pair that was never booked, so Approve on it must fail while
        # the booked one still succeeds.
        db.session.add_all([
            BankTransaction(plaid_transaction_id='t-out2', account_id=FARM_ACC,
                            amount=42.0, date=date(2026, 7, 12), name='Wire'),
            BankTransaction(plaid_transaction_id='t-in2',
                            account_id=PERSONAL_ACC, amount=-42.0,
                            date=date(2026, 7, 12), name='Wire')])
        db.session.commit()
        bad = intercompany.detect_pairs()[0]
        with self._patched_erp():
            r = self.client.post('/admin/intercompany/bulk',
                                 data={'action': 'approve',
                                       'ids': [good.id, bad.id]},
                                 headers={'X-Requested-With': 'fetch'})
        body = r.get_json()
        self.assertEqual(body['done'], 1)
        self.assertEqual(body['failed'], 1)
        self.assertEqual(
            db.session.get(IntercompanyTransferPair, good.id).state, 'approved')


# ── the Rules editor checkbox ───────────────────────────────────────────

class TestRuleIgnoreForPaired(IntercompanyUIBase):
    def setUp(self):
        super().setUp()
        # The Rules page asks ERPNext for its Company list; without this the
        # test client spends ~13s per request in connection retries against the
        # non-existent erp.test.
        self._companies = mock.patch('app.erpnext_bank.list_companies',
                                     return_value=[FARM, PERSONAL])
        self._companies.start()
        self.addCleanup(self._companies.stop)

    def test_the_checkbox_is_offered_and_pre_checked_for_a_new_rule(self):
        r = self.client.get('/admin/rules')
        self.assertIn(b'name="ignore_for_paired"', r.data)
        # Pre-checked: a rule saved without thinking about it stays out of the
        # way of intercompany transfers.
        html = r.data.decode()
        idx = html.index('name="ignore_for_paired"')
        self.assertIn('checked', html[idx:idx + 200])

    def test_saving_with_the_box_checked_stores_true(self):
        self.client.post('/admin/rules/save', data={
            'name': 'Transfers', 'priority': '10', 'active': '1',
            'match_type': 'description_regex', 'match_value': 'Transfer',
            'offset_account': 'Owner Draws', 'ignore_for_paired': '1'})
        rule = CategorizationRule.query.filter_by(name='Transfers').first()
        self.assertTrue(rule.ignore_for_paired)

    def test_clearing_the_box_stores_false(self):
        self.client.post('/admin/rules/save', data={
            'name': 'Transfers', 'priority': '10', 'active': '1',
            'match_type': 'description_regex', 'match_value': 'Transfer',
            'offset_account': 'Owner Draws'})
        rule = CategorizationRule.query.filter_by(name='Transfers').first()
        self.assertFalse(rule.ignore_for_paired)

    def test_the_stored_value_round_trips_into_the_edit_form(self):
        self.client.post('/admin/rules/save', data={
            'name': 'Fuel', 'priority': '10', 'active': '1',
            'match_type': 'merchant_contains', 'match_value': 'Chevron',
            'offset_account': 'Fuel'})
        rule = CategorizationRule.query.filter_by(name='Fuel').first()
        self.assertFalse(rule.ignore_for_paired)
        html = self.client.get(f'/admin/rules?edit={rule.id}').data.decode()
        idx = html.index('name="ignore_for_paired"')
        self.assertNotIn('checked', html[idx:idx + 200])


if __name__ == '__main__':
    unittest.main()
