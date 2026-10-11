# SPDX-License-Identifier: MIT
"""Rule offset accounts: fully-qualified vs logical (v0.4.0.3, v1.0.9).

v1.0.9 · EVERY rule belongs to exactly one Company (applies_to_company) and
fires only on transactions whose account belongs to that Company. A rule with a
blank scope matches NOTHING — the old "company-agnostic" Mode B rule, where ONE
rule resolved a logical offset under each Company in turn, is gone: each Company
needs its own rule. What survives of the two modes is the offset's SHAPE:

  * a fully-qualified offset ('Meals & Entertainment - BBT') is used verbatim;
  * a bare LOGICAL name ('Meals & Entertainment') is resolved under the rule's
    Company at JE time — now for every rule, not only unscoped ones. A Company
    lacking that account is skipped (no JE, no auto-create) and audited.

The boot migration that converts legacy agnostic offsets to logical names is
still exercised below; the dropdown feed still serves both shapes.

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
from app import categorization, erpnext_bank, erpnext_settings, migrations  # noqa: E402
from app.models import (AuditEvent, BankTransaction, CategorizationRule,  # noqa: E402
                        GeneratedJournalEntry, PlaidAccount, PlaidItem)

from tests.fakes import FakeERPClient  # noqa: E402

ACC_BBT = 'acct-bbt-checking'
ACC_T2 = 'acct-t2-checking'

# A two-Company chart the patched list_accounts serves. Each docname ends in its
# Company suffix, exactly like a real ERPNext Account. Both Companies carry a
# 'Meals & Entertainment' and a 'Fuel Expense'; only BBT has 'Marketing'.
CHART = {
    'Bank Bridge Test': ['Meals & Entertainment - BBT', 'Fuel Expense - BBT',
                         'Marketing - BBT'],
    'Testing II': ['Meals & Entertainment - T2', 'Fuel Expense - T2'],
}


def _fake_list_accounts(client=None, *, company=erpnext_bank._COMPANY_UNSET):
    """Honours the list_accounts company contract: a real name → that Company's
    leaves; None/'' → every Company's leaves; UNSET → the default Company."""
    if company is erpnext_bank._COMPANY_UNSET:
        company = 'Default Co'
    def _rows(co, names):
        return [{'name': n, 'account_name': n.rsplit(' - ', 1)[0],
                 'company': co, 'account_type': 'Expense',
                 'root_type': 'Expense'} for n in names]
    if company:
        return _rows(company, CHART.get(company, []))
    out = []
    for co, names in CHART.items():
        out.extend(_rows(co, names))
    return out


def _chart_erp():
    """A FakeERPClient whose Chart of Accounts mirrors CHART, plus the two bank
    GL leaves, so resolve_logical_account + the push guard have real data."""
    accts = []
    for co, names in CHART.items():
        for n in names:
            accts.append({'account_name': n.rsplit(' - ', 1)[0], 'name': n,
                          'company': co})
    accts += [
        {'account_name': 'WF Checking', 'name': 'WF Checking - BBT',
         'company': 'Bank Bridge Test'},
        {'account_name': 'Checking II', 'name': 'Checking II - T2',
         'company': 'Testing II'},
    ]
    return FakeERPClient(chart_accounts=accts)


class Base(unittest.TestCase):
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
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', 'Default Co')

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    # ── fixtures ────────────────────────────────────────────────────────
    def _item(self):
        db.session.add(PlaidItem(
            item_id='item-abc', access_token_encrypted=crypto.encrypt('x'),
            institution_name='Wells Fargo', status='active'))
        db.session.commit()

    def _account(self, account_id, owning_company, gl):
        db.session.add(PlaidAccount(
            account_id=account_id, item_id='item-abc', name='Checking',
            mask='1234', type='depository', subtype='checking',
            erpnext_bank_account_name=f'{gl} map',
            erpnext_gl_account_name=gl, sync_enabled=True,
            owning_company=owning_company))
        db.session.commit()

    def _both_accounts(self):
        self._item()
        self._account(ACC_BBT, 'Bank Bridge Test', 'WF Checking - BBT')
        self._account(ACC_T2, 'Testing II', 'Checking II - T2')

    def _rule(self, company, offset='Meals & Entertainment'):
        """A rule scoped to `company` (None → the legacy unscoped shape, which
        v1.0.9 never fires)."""
        rule = CategorizationRule(
            name=f'Uber Eats → Meals ({company})', priority=100, active=True,
            match_type='merchant_contains', match_value='Uber Eats',
            offset_account=offset, offset_direction='auto',
            applies_to_company=company)
        db.session.add(rule)
        db.session.commit()
        return rule

    def _row(self, account_id, tid, merchant='Uber Eats'):
        row = BankTransaction(
            plaid_transaction_id=tid, account_id=account_id, amount=32.0,
            merchant_name=merchant, name='UBER EATS', date=date(2026, 7, 10),
            erpnext_bank_transaction_id=f'ACC-BTN-{tid}')
        db.session.add(row)
        db.session.commit()
        return row

    def _offset_line(self, erp):
        je = list(erp.created['Journal Entry'].values())[0]
        return next(l['account'] for l in je['accounts']
                    if 'Meals' in (l['account'] or ''))


# ── a logical offset resolves under the RULE's Company ──────────────────────

class TestLogicalOffsetResolvesPerRuleCompany(Base):
    def test_bbt_rule_resolves_to_bbt_meals(self):
        self._both_accounts()
        self._rule('Bank Bridge Test')
        erp = _chart_erp()
        categorization.generate_journal_entry(erp, self._row(ACC_BBT, 't1'))
        self.assertEqual(len(erp.created['Journal Entry']), 1)
        self.assertEqual(self._offset_line(erp), 'Meals & Entertainment - BBT')
        gje = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='t1').first()
        self.assertIsNotNone(gje.erpnext_journal_entry_name)

    def test_testing_ii_rule_resolves_to_testing_ii_meals(self):
        self._both_accounts()
        self._rule('Testing II')
        erp = _chart_erp()
        categorization.generate_journal_entry(erp, self._row(ACC_T2, 't2'))
        self.assertEqual(self._offset_line(erp), 'Meals & Entertainment - T2')
        je = list(erp.created['Journal Entry'].values())[0]
        self.assertEqual(je['company'], 'Testing II')

    def test_a_rule_does_not_fire_on_another_companys_account(self):
        # v1.0.9 · the BBT rule is out of scope for a Testing II transaction —
        # no JE, rather than borrowing the rule into the other Company.
        self._both_accounts()
        self._rule('Bank Bridge Test')
        erp = _chart_erp()
        categorization.generate_journal_entry(erp, self._row(ACC_T2, 't2'))
        self.assertEqual(len(erp.created['Journal Entry']), 0)

    def test_each_company_needs_its_own_rule(self):
        # Replaces Mode B's "one rule books to both Companies": two rules with
        # the SAME logical offset, one per Company, each landing in that
        # Company's own Meals account.
        self._both_accounts()
        self._rule('Bank Bridge Test')
        self._rule('Testing II')
        erp = _chart_erp()
        categorization.generate_journal_entry(erp, self._row(ACC_BBT, 't1'))
        categorization.generate_journal_entry(erp, self._row(ACC_T2, 't2'))
        offsets = set()
        for je in erp.created['Journal Entry'].values():
            offsets |= {l['account'] for l in je['accounts'] if 'Meals' in l['account']}
        self.assertEqual(
            offsets, {'Meals & Entertainment - BBT', 'Meals & Entertainment - T2'})

    def test_unscoped_rule_never_fires(self):
        # Fail closed: a blank applies_to_company matches nothing, on any
        # Company's account.
        self._both_accounts()
        self._rule(None)
        erp = _chart_erp()
        categorization.generate_journal_entry(erp, self._row(ACC_BBT, 't1'))
        categorization.generate_journal_entry(erp, self._row(ACC_T2, 't2'))
        self.assertEqual(len(erp.created['Journal Entry']), 0)

    def test_resolved_offset_passes_the_push_guard(self):
        # The logical name resolves to the target Company's own account, so the
        # v0.4.0.2 cross-Company guard must NOT block it (no false positive).
        self._both_accounts()
        self._rule('Bank Bridge Test')
        erp = _chart_erp()
        categorization.generate_journal_entry(erp, self._row(ACC_BBT, 't1'))
        gje = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='t1').first()
        self.assertNotEqual(gje.state, 'blocked')
        self.assertEqual(AuditEvent.query.filter_by(
            event_type='journal_entry_blocked_cross_company').count(), 0)


# ── a Company missing the logical account is skipped ─────────────────────────

class TestLogicalOffsetMissingAccount(Base):
    def test_missing_target_account_skips_and_audits(self):
        # 'Marketing' exists only under BBT. A Testing II rule on 'Marketing'
        # has nowhere to book → skipped, not posted, not created.
        self._both_accounts()
        self._rule('Testing II', offset='Marketing')
        erp = _chart_erp()
        categorization.generate_journal_entry(erp, self._row(ACC_T2, 't2'))
        self.assertEqual(len(erp.created['Journal Entry']), 0)
        gje = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='t2').first()
        self.assertEqual(gje.state, 'skipped_missing_account')
        self.assertIn('Marketing', gje.error_message)
        self.assertIn('Testing II', gje.error_message)
        ev = AuditEvent.query.filter_by(
            event_type='journal_entry_skipped_missing_account').first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.subject_type, 'GeneratedJournalEntry')

    def test_each_companys_rule_is_judged_against_its_own_chart(self):
        # Same logical 'Marketing' offset on a rule per Company: BBT books,
        # Testing II (no such account) is skipped.
        self._both_accounts()
        self._rule('Bank Bridge Test', offset='Marketing')
        self._rule('Testing II', offset='Marketing')
        erp = _chart_erp()
        categorization.generate_journal_entry(erp, self._row(ACC_BBT, 't1'))
        categorization.generate_journal_entry(erp, self._row(ACC_T2, 't2'))
        self.assertEqual(len(erp.created['Journal Entry']), 1)
        self.assertEqual(GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='t1').first().state, 'pending_review')
        self.assertEqual(GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='t2').first().state, 'skipped_missing_account')


# ── Mode A: scoped rule uses a specific offset verbatim ──────────────────────

class TestModeAScoped(Base):
    def test_scoped_rule_uses_specific_offset_verbatim(self):
        self._both_accounts()
        rule = CategorizationRule(
            name='Uber Eats (BBT)', priority=100, active=True,
            match_type='merchant_contains', match_value='Uber Eats',
            offset_account='Meals & Entertainment - BBT', offset_direction='auto',
            applies_to_company='Bank Bridge Test')
        db.session.add(rule)
        db.session.commit()
        erp = _chart_erp()
        categorization.generate_journal_entry(erp, self._row(ACC_BBT, 't1'))
        self.assertEqual(self._offset_line(erp), 'Meals & Entertainment - BBT')

    def test_unscoped_rule_with_legacy_qualified_offset_never_fires(self):
        # v1.0.9 · a fully-qualified offset no longer rescues an unscoped rule:
        # blank scope matches nothing, even on the offset's own Company.
        self._both_accounts()
        self._rule(None, offset='Meals & Entertainment - BBT')
        erp = _chart_erp()
        categorization.generate_journal_entry(erp, self._row(ACC_BBT, 't1'))
        self.assertEqual(len(erp.created['Journal Entry']), 0)


# ── the dropdown feed: Mode A fully-qualified vs Mode B logical ──────────────

class TestDropdownFeed(Base):
    def _get(self, company=None):
        url = '/api/rules/known_accounts'
        if company is not None:
            url += '?company=' + company.replace(' ', '+')
        return json.loads(self.client.get(url).get_data(as_text=True))

    def test_scoped_rule_feed_is_fully_qualified(self):
        with mock.patch('app.erpnext_bank.list_accounts',
                        side_effect=_fake_list_accounts):
            data = self._get(company='Bank Bridge Test')
        self.assertEqual(data['mode'], 'specific')
        self.assertIn('Meals & Entertainment - BBT', data['accounts'])
        self.assertNotIn('Meals & Entertainment - T2', data['accounts'])

    def test_agnostic_rule_feed_is_deduped_logical(self):
        with mock.patch('app.erpnext_bank.list_accounts',
                        side_effect=_fake_list_accounts):
            data = self._get(company='')
        self.assertEqual(data['mode'], 'logical')
        # 'Meals & Entertainment' + 'Fuel Expense' appear in both Companies but
        # are offered once, without a suffix; 'Marketing' (BBT-only) too.
        self.assertEqual(sorted(data['accounts']),
                         ['Fuel Expense', 'Marketing', 'Meals & Entertainment'])

    def test_switching_applies_to_company_flips_feed_mode(self):
        # The same endpoint, driven only by the ?company= param, returns the two
        # different shapes — this is what the form's onchange toggles.
        with mock.patch('app.erpnext_bank.list_accounts',
                        side_effect=_fake_list_accounts):
            scoped = self._get(company='Testing II')
            agnostic = self._get(company='')
        self.assertEqual(scoped['mode'], 'specific')
        self.assertTrue(all(' - ' in a for a in scoped['accounts']))
        self.assertEqual(agnostic['mode'], 'logical')
        self.assertTrue(all(' - ' not in a for a in agnostic['accounts']))


# ── save path: mode is inferred from applies_to_company ──────────────────────

class TestSaveInfersMode(Base):
    def test_blank_company_is_refused(self):
        # v1.0.9 · an unscoped rule would never fire, so the form won't save one.
        r = self.client.post('/admin/rules/save', data={
            'name': 'Uber Eats → Meals', 'priority': '100', 'active': '1',
            'match_type': 'merchant_contains', 'match_value': 'Uber Eats',
            'offset_account': 'Meals & Entertainment', 'offset_direction': 'auto',
            'applies_to_company': ''})
        self.assertIn('Choose the Company this rule belongs to',
                      r.get_data(as_text=True))
        self.assertEqual(CategorizationRule.query.count(), 0)

    def test_scoped_rule_saves_logical_name_untouched(self):
        # A logical offset on a scoped rule is stored as typed; it resolves
        # under the rule's Company at JE time.
        self.client.post('/admin/rules/save', data={
            'name': 'Uber Eats → Meals', 'priority': '100', 'active': '1',
            'match_type': 'merchant_contains', 'match_value': 'Uber Eats',
            'offset_account': 'Meals & Entertainment', 'offset_direction': 'auto',
            'applies_to_company': 'Bank Bridge Test',
            'confirm_scope_mismatch': '1', 'confirm_party_mismatch': '1'})
        rule = CategorizationRule.query.filter_by(archived=False).first()
        self.assertEqual(rule.applies_to_company, 'Bank Bridge Test')
        self.assertEqual(rule.offset_account, 'Meals & Entertainment')

    def test_scoped_rule_saves_specific_offset(self):
        self.client.post('/admin/rules/save', data={
            'name': 'Uber Eats (BBT)', 'priority': '100', 'active': '1',
            'match_type': 'merchant_contains', 'match_value': 'Uber Eats',
            'offset_account': 'Meals & Entertainment - BBT',
            'offset_direction': 'auto',
            'applies_to_company': 'Bank Bridge Test'})
        rule = CategorizationRule.query.filter_by(archived=False).first()
        self.assertEqual(rule.applies_to_company, 'Bank Bridge Test')
        self.assertEqual(rule.offset_account, 'Meals & Entertainment - BBT')


# ── logical_account_name: pure reduction + idempotency ───────────────────────

class TestLogicalAccountName(unittest.TestCase):
    def test_strips_company_abbr_suffix(self):
        self.assertEqual(
            categorization.logical_account_name('Meals & Entertainment - BBT'),
            'Meals & Entertainment')

    def test_strips_leading_number_and_suffix(self):
        self.assertEqual(
            categorization.logical_account_name('5100 - Fuel Expense - EC'),
            'Fuel Expense')

    def test_leaves_bare_logical_name_untouched(self):
        self.assertEqual(
            categorization.logical_account_name('Meals & Entertainment'),
            'Meals & Entertainment')

    def test_preserves_name_that_legitimately_contains_dash(self):
        # 'Draws' is not an all-caps abbr, so ' - Draws' is NOT mistaken for a
        # Company suffix — only the trailing ' - EC' is stripped.
        self.assertEqual(
            categorization.logical_account_name('Owner - Draws - EC'),
            'Owner - Draws')

    def test_is_idempotent(self):
        for raw in ('Meals & Entertainment - BBT', '5100 - Fuel Expense - EC',
                    'Owner - Draws - EC', 'Meals & Entertainment', ''):
            once = categorization.logical_account_name(raw)
            self.assertEqual(categorization.logical_account_name(once), once)


# ── boot migration: legacy agnostic offset → logical ─────────────────────────

class TestAgnosticOffsetMigration(Base):
    def _rule(self, offset, company=None, active=True, archived=False):
        r = CategorizationRule(
            name='r', priority=100, active=active, archived=archived,
            match_type='merchant_contains', match_value='X',
            offset_account=offset, applies_to_company=company)
        db.session.add(r)
        db.session.commit()
        return r

    def test_converts_agnostic_fully_qualified_offset(self):
        r = self._rule('Meals & Entertainment - BBT')
        migrations._migrate_agnostic_offset_to_logical()
        db.session.refresh(r)
        self.assertEqual(r.offset_account, 'Meals & Entertainment')

    def test_scoped_rule_offset_is_left_alone(self):
        r = self._rule('Meals & Entertainment - BBT', company='Bank Bridge Test')
        migrations._migrate_agnostic_offset_to_logical()
        db.session.refresh(r)
        self.assertEqual(r.offset_account, 'Meals & Entertainment - BBT')

    def test_migration_is_idempotent(self):
        r = self._rule('Meals & Entertainment - BBT')
        migrations._migrate_agnostic_offset_to_logical()
        migrations._migrate_agnostic_offset_to_logical()
        db.session.refresh(r)
        self.assertEqual(r.offset_account, 'Meals & Entertainment')

    def test_name_with_legitimate_dash_not_over_stripped(self):
        # 'Owner - Draws - EC' → 'Owner - Draws' (once), and re-running does NOT
        # strip further to 'Owner' — the edge case Option 2 must handle safely.
        r = self._rule('Owner - Draws - EC')
        migrations._migrate_agnostic_offset_to_logical()
        migrations._migrate_agnostic_offset_to_logical()
        db.session.refresh(r)
        self.assertEqual(r.offset_account, 'Owner - Draws')

    def test_archived_rule_is_not_migrated(self):
        r = self._rule('Meals & Entertainment - BBT', active=False, archived=True)
        migrations._migrate_agnostic_offset_to_logical()
        db.session.refresh(r)
        self.assertEqual(r.offset_account, 'Meals & Entertainment - BBT')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
