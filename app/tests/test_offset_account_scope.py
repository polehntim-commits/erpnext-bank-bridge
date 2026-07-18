# SPDX-License-Identifier: MIT
"""Cross-Company isolation of the Offset Account dropdown + push-time guard
(v0.4.0.2).

The Rules-editor Offset Account picker must only offer GL Accounts from the
Company that owns the rule, resolved in priority order:

  1. the rule's own `applies_to_company` (sent as ?company=…);
  2. else the active session scope (the navbar switcher);
  3. else — no scope anywhere — every Company's accounts (each carries its
     `- <abbr>` Company suffix), so the operator picks deliberately.

The feed is cached per Company for the session and invalidated when the scope
changes. As a backstop, the JE push refuses to post any Journal Entry whose GL
account belongs to a different Company than the target Bank Account.

    cd app
    python3 -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import categorization, erpnext_bank, erpnext_settings  # noqa: E402
from app.blueprints import admin_ui  # noqa: E402
from app.models import (AuditEvent, BankTransaction, CategorizationRule,  # noqa: E402
                        GeneratedJournalEntry, PlaidAccount, PlaidItem)

from tests.fakes import FakeERPClient  # noqa: E402

ACC = 'acct-wf-checking'

# The per-Company chart the patched list_accounts serves. Each name already ends
# in its Company suffix, exactly like a real ERPNext Account docname.
CHART = {
    'Alpha LLC': ['Fuel Expense - AL', 'Meals & Entertainment - AL'],
    'Beta LLC': ['Fuel Expense - BL', 'Meals & Entertainment - BL'],
}


def _fake_list_accounts(client=None, *, company=erpnext_bank._COMPANY_UNSET):
    """Stand-in for erpnext_bank.list_accounts that honours the v0.4.0.2 company
    contract: a real name → that Company's leaves; None/'' → every Company's
    leaves; the UNSET sentinel → the default Company (back-compat)."""
    if company is erpnext_bank._COMPANY_UNSET:
        company = 'Default Co'
    if company:
        names = CHART.get(company, [])
        return [{'name': n, 'account_name': n.rsplit(' - ', 1)[0],
                 'company': company, 'account_type': 'Expense',
                 'root_type': 'Expense'} for n in names]
    # all Companies
    out = []
    for co, names in CHART.items():
        for n in names:
            out.append({'name': n, 'account_name': n.rsplit(' - ', 1)[0],
                        'company': co, 'account_type': 'Expense',
                        'root_type': 'Expense'})
    return out


class ScopeBase(unittest.TestCase):
    EXTRA_CONFIG: dict = {}

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
        cfg.update(self.EXTRA_CONFIG)
        self.app = create_app(cfg)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        # A configured ERPNext connection whose *default* Company is neither
        # Alpha nor Beta — the pre-fix bug served this Company regardless of scope.
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', 'Default Co')

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _get_accounts(self, rule_company=None):
        url = '/api/rules/known_accounts'
        if rule_company is not None:
            url += '?company=' + rule_company.replace(' ', '+')
        resp = self.client.get(url)
        return json.loads(resp.get_data(as_text=True))


# ── the dropdown feed: P1 / P2 / P3 resolution ──────────────────────────────

class TestOffsetAccountFeed(ScopeBase):
    def test_p1_filters_by_rule_applies_to_company(self):
        # Rule scoped to Alpha → only Alpha's chart, never the ERPNext default.
        with mock.patch('app.erpnext_bank.list_accounts',
                        side_effect=_fake_list_accounts):
            data = self._get_accounts(rule_company='Alpha LLC')
        self.assertEqual(data['company'], 'Alpha LLC')
        self.assertIn('Fuel Expense - AL', data['accounts'])
        self.assertNotIn('Fuel Expense - BL', data['accounts'])
        # And crucially NOT the default Company's accounts.
        self.assertNotIn('Default Co', json.dumps(data['accounts']))

    def test_agnostic_feed_is_logical_even_under_session_scope(self):
        # v0.4.0.3 · an AGNOSTIC rule (empty ?company=) is Mode B regardless of the
        # navbar session scope: the feed offers deduplicated LOGICAL account names,
        # NOT any one Company's fully-qualified accounts. (Pre-.3 this fell back to
        # the session Company — which would have wrongly pinned the offset.)
        self.client.get('/admin/set_company?company=Beta+LLC&next=/admin/rules')
        with mock.patch('app.erpnext_bank.list_accounts',
                        side_effect=_fake_list_accounts):
            data = self._get_accounts(rule_company='')
        self.assertEqual(data['company'], '')
        self.assertEqual(data.get('mode'), 'logical')
        self.assertIn('Fuel Expense', data['accounts'])          # logical, no suffix
        self.assertNotIn('Fuel Expense - BL', data['accounts'])  # not fully-qualified

    def test_p1_rule_scope_overrides_session_scope(self):
        # Session scoped to Beta, but the rule is explicitly Alpha → Alpha wins.
        self.client.get('/admin/set_company?company=Beta+LLC&next=/admin/rules')
        with mock.patch('app.erpnext_bank.list_accounts',
                        side_effect=_fake_list_accounts):
            data = self._get_accounts(rule_company='Alpha LLC')
        self.assertEqual(data['company'], 'Alpha LLC')
        self.assertIn('Fuel Expense - AL', data['accounts'])
        self.assertNotIn('Fuel Expense - BL', data['accounts'])

    def test_agnostic_feed_dedupes_logical_names_across_companies(self):
        # v0.4.0.3 · no rule scope → Mode B: the same logical account present in
        # both Companies' charts ('Fuel Expense', 'Meals & Entertainment') is
        # offered ONCE, without any `- <abbr>` suffix, sorted.
        with mock.patch('app.erpnext_bank.list_accounts',
                        side_effect=_fake_list_accounts):
            data = self._get_accounts(rule_company='')
        self.assertEqual(data['company'], '')
        self.assertEqual(data.get('mode'), 'logical')
        self.assertEqual(data['accounts'],
                         ['Fuel Expense', 'Meals & Entertainment'])

    def test_feed_cached_per_company_and_invalidated_on_scope_change(self):
        # First agnostic (Mode B) fetch hits ERPNext once and caches under the
        # logical-names key; a second identical fetch is served from cache (no
        # extra call). A scope change clears the cache, so the next fetch re-hits.
        spy = mock.Mock(side_effect=_fake_list_accounts)
        with mock.patch('app.erpnext_bank.list_accounts', spy):
            self._get_accounts(rule_company='')
            self._get_accounts(rule_company='')
            self.assertEqual(spy.call_count, 1)     # second served from cache
            # Changing the session scope invalidates the per-Company cache…
            self.client.get('/admin/set_company?company=&next=/admin/rules')
            self._get_accounts(rule_company='')
            self.assertEqual(spy.call_count, 2)     # …so this re-fetches


# ── rules-editor pre-fill (v0.4.2 precursor) ────────────────────────────────

class TestRuleFormScopeInheritance(ScopeBase):
    def test_new_rule_form_prefills_applies_to_company_from_session(self):
        # Building a rule while scoped to a Company defaults the new rule's
        # Applies-to-Company to that Company, so its offset draws from that chart.
        with mock.patch('app.erpnext_bank.list_companies',
                        return_value=['Alpha LLC', 'Beta LLC']):
            self.client.get('/admin/set_company?company=Alpha+LLC&next=/admin/rules')
            body = self.client.get('/admin/rules').get_data(as_text=True)
        self.assertIn('<option value="Alpha LLC" selected>Alpha LLC</option>', body)


# ── push-time cross-Company guard ────────────────────────────────────────────

class PushGuardBase(ScopeBase):
    def _item(self, owning_company=None):
        db.session.add(PlaidItem(
            item_id='item-abc', access_token_encrypted=crypto.encrypt('x'),
            institution_name='Wells Fargo', status='active',
            owning_company=owning_company))
        db.session.commit()

    def _account(self, owning_company='Orchard LLC', gl='WF Checking - OL'):
        db.session.add(PlaidAccount(
            account_id=ACC, item_id='item-abc', name='WF Checking', mask='1234',
            type='depository', subtype='checking',
            erpnext_bank_account_name='WF Checking - Ops',
            erpnext_gl_account_name=gl, sync_enabled=True,
            owning_company=owning_company))
        db.session.commit()

    def _rule(self, offset_account):
        rule = CategorizationRule(
            name='fuel', priority=100, active=True,
            match_type='merchant_contains', match_value='Chevron',
            offset_account=offset_account, offset_direction='auto')
        db.session.add(rule)
        db.session.commit()
        return rule

    def _row(self, tid='t1'):
        row = BankTransaction(
            plaid_transaction_id=tid, account_id=ACC, amount=50.0,
            merchant_name='Chevron', name='CHEVRON 01', date=date(2026, 7, 10),
            erpnext_bank_transaction_id='ACC-BTN-0001')
        db.session.add(row)
        db.session.commit()
        return row

    def _erp_with_chart(self):
        # A chart where the offset lives under Alpha and the bank leaf under
        # Orchard — a genuine cross-Company reference to catch.
        return FakeERPClient(chart_accounts=[
            {'account_name': 'Fuel Expense', 'name': 'Fuel Expense - AL',
             'company': 'Alpha LLC'},
            {'account_name': 'Fuel Expense', 'name': 'Fuel Expense - OL',
             'company': 'Orchard LLC'},
            {'account_name': 'WF Checking', 'name': 'WF Checking - OL',
             'company': 'Orchard LLC'},
        ])


class TestPushTimeGuard(PushGuardBase):
    def test_cross_company_je_is_blocked_not_posted(self):
        self._item(owning_company='Orchard LLC')
        self._account(owning_company='Orchard LLC')
        # Offset belongs to Alpha, but the transaction's Company is Orchard.
        self._rule(offset_account='Fuel Expense - AL')
        row = self._row()
        erp = self._erp_with_chart()
        categorization.generate_journal_entry(erp, row)
        # No Journal Entry reached ERPNext.
        self.assertEqual(len(erp.created['Journal Entry']), 0)
        gje = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='t1').first()
        self.assertEqual(gje.state, 'blocked')
        self.assertIn('cross-Company', gje.error_message)
        self.assertIn('Alpha LLC', gje.error_message)

    def test_block_records_audit_event(self):
        self._item(owning_company='Orchard LLC')
        self._account(owning_company='Orchard LLC')
        self._rule(offset_account='Fuel Expense - AL')
        row = self._row()
        erp = self._erp_with_chart()
        categorization.generate_journal_entry(erp, row)
        ev = AuditEvent.query.filter_by(
            event_type='journal_entry_blocked_cross_company').first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.subject_type, 'GeneratedJournalEntry')

    def test_matching_company_je_posts_normally(self):
        # No false positives: an in-Company offset posts as usual.
        self._item(owning_company='Orchard LLC')
        self._account(owning_company='Orchard LLC')
        self._rule(offset_account='Fuel Expense - OL')   # Orchard, matches
        row = self._row()
        erp = self._erp_with_chart()
        categorization.generate_journal_entry(erp, row)
        self.assertEqual(len(erp.created['Journal Entry']), 1)
        gje = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='t1').first()
        self.assertIsNotNone(gje.erpnext_journal_entry_name)
        self.assertNotEqual(gje.state, 'blocked')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
