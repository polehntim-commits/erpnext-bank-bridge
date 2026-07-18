# SPDX-License-Identifier: MIT
"""v0.4.0.9 · party_type respects ERPNext's account_type, not just root_type.

THE BUG: v0.4.0.8 derived the party side from the offset account's ROOT type —
Income → Customer, Expense → Supplier. But ERPNext validates a Journal Entry
line's Party against the finer `account_type`, and it does so at SUBMIT:

    ValidationError: Party Type and Party can only be set for Receivable /
    Payable account Interest Income - BBT

An ordinary Income account is an 'Income Account', not a 'Receivable', so an
interest-income rule generated Journal Entries that CREATED fine and then could
not be approved — leaving the operator with stuck drafts and no way forward.

Three fixes are covered here:
  A. the Auto derivation consults account_type (PARTY_TYPE_MATRIX);
  B. the Rules editor refuses to SAVE an incompatible literal party_type;
  C. a boot migration repairs the incompatible rules already in the database.

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
from app import categorization, erpnext_bank, migrations  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        PlaidAccount, PlaidItem)
from tests.fakes import FakeERPClient  # noqa: E402

# One account per (root_type, account_type) shape the matrix cares about. The
# two Income rows are the crux: same root_type, opposite verdicts, because only
# one of them is a Receivable ledger.
CHART = [
    {'name': 'Interest Income - T', 'account_name': 'Interest Income',
     'company': 'Testing', 'root_type': 'Income',
     'account_type': 'Income Account'},
    {'name': 'Grower Receivable - T', 'account_name': 'Grower Receivable',
     'company': 'Testing', 'root_type': 'Income',
     'account_type': 'Receivable'},
    {'name': 'Fuel Expense - T', 'account_name': 'Fuel Expense',
     'company': 'Testing', 'root_type': 'Expense',
     'account_type': 'Expense Account'},
    {'name': 'Grower Payable - T', 'account_name': 'Grower Payable',
     'company': 'Testing', 'root_type': 'Expense', 'account_type': 'Payable'},
    {'name': 'Checking 1111 - T', 'account_name': 'Checking 1111',
     'company': 'Testing', 'root_type': 'Asset', 'account_type': 'Bank'},
    # A logical name ('Shared Ledger') that resolves to DIFFERENT account_types
    # under different Companies — the Mode B warn-but-allow case.
    {'name': 'Shared Ledger - T', 'account_name': 'Shared Ledger',
     'company': 'Testing', 'root_type': 'Expense', 'account_type': 'Payable'},
    {'name': 'Shared Ledger - O', 'account_name': 'Shared Ledger',
     'company': 'Other Co', 'root_type': 'Expense',
     'account_type': 'Expense Account'},
]


def _erp(**kw):
    kw.setdefault('companies', ['Testing', 'Other Co'])
    kw.setdefault('chart_accounts', CHART)
    kw.setdefault('company_abbr', 'T')
    return FakeERPClient(**kw)


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
            'ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES': True,
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

    def _rule(self, **kw):
        vals = {'name': 'R', 'match_type': 'description_regex',
                'match_value': '.*', 'offset_account': 'Interest Income - T',
                'offset_direction': 'auto', 'party_type': 'Auto',
                'applies_to_company': 'Testing', 'active': True,
                'archived': False, 'priority': 10}
        vals.update(kw)
        rule = CategorizationRule(**vals)
        db.session.add(rule)
        db.session.commit()
        return rule


class AutoModeConsultsAccountType(Base):
    """Fix A — the matrix keys on (root_type, account_type)."""

    def test_income_account_books_no_party(self):
        """TIM'S BUG. Interest Income is root_type=Income but account_type=
        'Income Account', so v0.4.0.8 hung a Customer off it and the JE failed
        at submit. It must now derive NO party."""
        self.assertEqual(
            categorization.party_type_for_offset(
                _erp(), 'Interest Income - T', 'Testing'), '')

    def test_income_plus_receivable_books_a_customer(self):
        self.assertEqual(
            categorization.party_type_for_offset(
                _erp(), 'Grower Receivable - T', 'Testing'), 'Customer')

    def test_expense_account_books_no_party(self):
        self.assertEqual(
            categorization.party_type_for_offset(
                _erp(), 'Fuel Expense - T', 'Testing'), '')

    def test_expense_plus_payable_books_a_supplier(self):
        self.assertEqual(
            categorization.party_type_for_offset(
                _erp(), 'Grower Payable - T', 'Testing'), 'Supplier')

    def test_matrix_is_exhaustive_about_what_it_refuses(self):
        """Every pair outside the two party-legal ones books no party — an
        undeterminable type included, since guessing is what caused the bug."""
        f = categorization.party_type_for_account_types
        self.assertEqual(f('Income', 'Receivable'), 'Customer')
        self.assertEqual(f('Expense', 'Payable'), 'Supplier')
        for root, acct in (('Income', 'Income Account'),
                           ('Income', 'Payable'),
                           ('Expense', 'Expense Account'),
                           ('Expense', 'Receivable'),
                           ('Asset', 'Bank'), ('Asset', 'Receivable'),
                           ('Liability', 'Payable'), ('Equity', ''),
                           ('', ''), ('Income', '')):
            self.assertEqual(f(root, acct), '',
                             f'({root!r}, {acct!r}) must book no party')

    def test_account_type_comes_from_the_same_fetch_as_root_type(self):
        """The refinement must not cost an extra ERPNext round-trip — both
        fields come off one Account doc."""
        erp = _erp()
        root, acct = erpnext_bank.account_types_for_account(
            erp, 'Grower Receivable - T', 'Testing')
        self.assertEqual((root, acct), ('Income', 'Receivable'))
        self.assertEqual(
            [c for c in erp.calls if c[0] in ('get_doc', 'list_docs')
             and c[1] == 'Account'],
            [('get_doc', 'Account', 'Grower Receivable - T')])

    def test_je_for_an_income_account_rule_carries_no_party(self):
        """End to end: the generated Journal Entry has no party line at all, so
        ERPNext's submit-time validation has nothing to object to."""
        db.session.add(PlaidItem(item_id='item-1', access_token_encrypted='x',
                                 institution_id='ins_1',
                                 institution_name='Wells Fargo',
                                 owning_company='Testing'))
        db.session.add(PlaidAccount(
            account_id='acct-1', item_id='item-1', name='Checking',
            mask='1111', type='depository', subtype='checking',
            erpnext_bank_account_name='Checking - WF',
            erpnext_gl_account_name='Checking 1111 - T',
            owning_company='Testing', import_status='imported'))
        db.session.commit()
        erp = _erp()
        rule = self._rule(name='Interest', party_name='Wells Fargo')
        row = BankTransaction(
            plaid_transaction_id='t-int', account_id='acct-1', amount=-12.5,
            name='INTRST PYMNT', date=date(2026, 7, 10),
            erpnext_bank_transaction_id='ACC-BTN-0001',
            posted_at=categorization._now())
        db.session.add(row)
        db.session.commit()

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        doc = erp.created['Journal Entry'][gje.erpnext_journal_entry_name]
        self.assertEqual(gje.state, 'pending_review')
        self.assertEqual([ln for ln in doc['accounts'] if ln.get('party')], [])


class SaveTimeValidation(Base):
    """Fix B — the Rules editor refuses an incompatible literal party_type."""

    def _save(self, **kw):
        data = dict(name='Interest', priority='10', active='1',
                    match_type='description_regex', match_value='INTRST',
                    offset_account='Interest Income - T',
                    offset_direction='auto', party_type='Customer',
                    party_name='Wells Fargo', description_template='',
                    applies_to_company='Testing')
        data.update(kw)
        with mock.patch('app.erpnext_settings.is_configured', return_value=True), \
                mock.patch('app.erpnext_bank.get_client', return_value=_erp()):
            return self.client.post('/admin/rules/save', data=data,
                                    follow_redirects=True)

    def test_customer_on_an_income_account_is_blocked(self):
        r = self._save(party_type='Customer')
        self.assertEqual(CategorizationRule.query.count(), 0)
        body = r.data.decode()
        self.assertIn('Interest Income - T', body)
        self.assertIn('Income Account', body)
        self.assertIn('Receivable', body)

    def test_supplier_on_an_expense_account_is_blocked(self):
        r = self._save(party_type='Supplier',
                       offset_account='Fuel Expense - T')
        self.assertEqual(CategorizationRule.query.count(), 0)
        self.assertIn('Expense Account', r.data.decode())

    def test_the_block_message_names_both_ways_out(self):
        """The message is the only thing the operator sees when a save is
        refused, so it has to carry the fix."""
        body = self._save(party_type='Customer').data.decode()
        self.assertIn('none', body)
        self.assertIn('Auto', body)

    def test_auto_and_none_are_allowed_on_any_offset(self):
        for pt in ('Auto', ''):
            CategorizationRule.query.delete()
            db.session.commit()
            self._save(party_type=pt)
            self.assertEqual(CategorizationRule.query.count(), 1,
                             f'party_type={pt!r} must save on any offset')

    def test_a_compatible_literal_party_type_saves(self):
        self._save(party_type='Customer',
                   offset_account='Grower Receivable - T')
        self.assertEqual(CategorizationRule.query.count(), 1)

    def test_agnostic_rule_warns_but_saves_on_confirmation(self):
        """Mode B · 'Shared Ledger' is Payable under Testing and an Expense
        Account under Other Co. The operator may know the rule never fires
        there, so the first save warns and the confirmed resubmit goes through."""
        r = self._save(party_type='Supplier', offset_account='Shared Ledger',
                       applies_to_company='')
        self.assertEqual(CategorizationRule.query.count(), 0)
        self.assertIn('Expense Account', r.data.decode())
        self.assertIn('confirm_party_mismatch', r.data.decode())

        self._save(party_type='Supplier', offset_account='Shared Ledger',
                   applies_to_company='', confirm_party_mismatch='1')
        self.assertEqual(CategorizationRule.query.count(), 1)

    def test_validation_is_silent_when_erpnext_is_unconfigured(self):
        """No ERPNext to ask → no verdict → the save behaves exactly as it did
        before v0.4.0.9. A save-time check must never block on a network blip."""
        with mock.patch('app.erpnext_settings.is_configured', return_value=False):
            self.client.post('/admin/rules/save', data=dict(
                name='Interest', priority='10', active='1',
                match_type='description_regex', match_value='INTRST',
                offset_account='Interest Income - T', offset_direction='auto',
                party_type='Customer', party_name='Wells Fargo',
                description_template='', applies_to_company='Testing'),
                follow_redirects=True)
        self.assertEqual(CategorizationRule.query.count(), 1)


class RetroactiveMigration(Base):
    """Fix C — repair the incompatible rules already in the database."""

    def _run(self, erp=None):
        with mock.patch('app.erpnext_settings.is_configured', return_value=True), \
                mock.patch('app.erpnext_bank.get_client',
                           return_value=erp or _erp()):
            with self.assertLogs('bankbridge.migrations', level='INFO') as cm:
                migrations._migrate_incompatible_party_types()
        return cm.output

    def test_flips_incompatible_party_types(self):
        bad = self._rule(name='Interest Payment', party_type='Customer',
                         offset_account='Interest Income - T')
        also_bad = self._rule(name='Fuel', party_type='Supplier',
                              offset_account='Fuel Expense - T')

        self._run()

        db.session.refresh(bad)
        db.session.refresh(also_bad)
        self.assertIsNone(bad.party_type)
        self.assertIsNone(also_bad.party_type)

    def test_leaves_compatible_and_derived_party_types_alone(self):
        ok = self._rule(name='Sales', party_type='Customer',
                        offset_account='Grower Receivable - T')
        auto = self._rule(name='Auto', party_type='Auto',
                          offset_account='Interest Income - T')
        none = self._rule(name='None', party_type=None,
                          offset_account='Interest Income - T')

        with mock.patch('app.erpnext_settings.is_configured', return_value=True), \
                mock.patch('app.erpnext_bank.get_client', return_value=_erp()):
            migrations._migrate_incompatible_party_types()

        for r in (ok, auto, none):
            db.session.refresh(r)
        self.assertEqual(ok.party_type, 'Customer')
        self.assertEqual(auto.party_type, 'Auto')
        self.assertIsNone(none.party_type)

    def test_is_idempotent(self):
        """A second run flips nothing — the first run left no rule whose
        party_type contradicts its offset."""
        self._rule(name='Interest Payment', party_type='Customer',
                   offset_account='Interest Income - T')
        first = [ln for ln in self._run() if '→ none' in ln]
        self.assertEqual(len(first), 1)

        with mock.patch('app.erpnext_settings.is_configured', return_value=True), \
                mock.patch('app.erpnext_bank.get_client', return_value=_erp()):
            with self.assertNoLogs('bankbridge.migrations', level='WARNING'):
                migrations._migrate_incompatible_party_types()

    def test_logs_each_flip_visibly(self):
        """The operator has to be able to see WHICH rules changed under them
        and why, from the container log alone."""
        self._rule(name='Interest Payment', party_type='Customer',
                   offset_account='Interest Income - T')

        out = '\n'.join(self._run())

        self.assertIn('Interest Payment', out)
        self.assertIn('Customer', out)
        self.assertIn('Interest Income - T', out)
        self.assertIn('Income Account', out)
        self.assertIn('Receivable', out)

    def test_leaves_archived_rule_versions_untouched(self):
        """A rule version is archived so a past auto-JE decision stays
        reconstructible. Rewriting one falsifies that history, and an archived
        rule never fires again anyway."""
        old = self._rule(name='Interest Payment (v1)', party_type='Customer',
                         offset_account='Interest Income - T', archived=True,
                         active=False)

        with mock.patch('app.erpnext_settings.is_configured', return_value=True), \
                mock.patch('app.erpnext_bank.get_client', return_value=_erp()):
            migrations._migrate_incompatible_party_types()

        db.session.refresh(old)
        self.assertEqual(old.party_type, 'Customer')

    def test_leaves_rules_alone_when_the_account_cannot_be_read(self):
        """An offset ERPNext can't resolve yields no account_type and therefore
        no verdict. Stripping the operator's party choice because ERPNext was
        briefly unreachable would be worse than the bug being fixed."""
        rule = self._rule(name='Mystery', party_type='Customer',
                          offset_account='Nonexistent - T')

        with mock.patch('app.erpnext_settings.is_configured', return_value=True), \
                mock.patch('app.erpnext_bank.get_client', return_value=_erp()):
            migrations._migrate_incompatible_party_types()

        db.session.refresh(rule)
        self.assertEqual(rule.party_type, 'Customer')

    def test_is_a_no_op_when_erpnext_is_unconfigured(self):
        rule = self._rule(name='Interest Payment', party_type='Customer',
                          offset_account='Interest Income - T')

        with mock.patch('app.erpnext_settings.is_configured', return_value=False):
            migrations._migrate_incompatible_party_types()

        db.session.refresh(rule)
        self.assertEqual(rule.party_type, 'Customer')

    def test_runs_as_part_of_boot_migrations(self):
        """Wired into run_migrations, not just callable — the whole point is
        that Tim's stuck rules repair themselves on redeploy."""
        rule = self._rule(name='Interest Payment', party_type='Customer',
                          offset_account='Interest Income - T')

        with mock.patch('app.erpnext_settings.is_configured', return_value=True), \
                mock.patch('app.erpnext_bank.get_client', return_value=_erp()):
            migrations.run_migrations()

        db.session.refresh(rule)
        self.assertIsNone(rule.party_type)


class BackwardCompatibility(Base):
    """Regressions v0.4.0.9 must not break."""

    def test_v0408_auto_mapping_still_works_on_party_legal_accounts(self):
        """The sell-side derivation is refined, not withdrawn: a Receivable
        offset still books a Customer and a Payable one still books a
        Supplier."""
        erp = _erp()
        self.assertEqual(categorization.effective_party_type(
            erp, self._rule(offset_account='Grower Receivable - T'),
            'Testing'), 'Customer')
        self.assertEqual(categorization.effective_party_type(
            erp, self._rule(offset_account='Grower Payable - T'),
            'Testing'), 'Supplier')

    def test_v0407_skip_party_still_outranks_everything(self):
        rule = self._rule(party_type='Auto', skip_party=True,
                          offset_account='Grower Receivable - T')
        self.assertEqual(
            categorization.effective_party_type(_erp(), rule, 'Testing'), '')

    def test_root_type_for_account_still_answers_its_old_question(self):
        """Kept as a wrapper — v0.4.0.8 callers must not have to change."""
        self.assertEqual(erpnext_bank.root_type_for_account(
            _erp(), 'Interest Income - T', 'Testing'), 'Income')
        self.assertEqual(erpnext_bank.root_type_for_account(
            _erp(), 'Nonexistent - T', 'Testing'), '')


if __name__ == '__main__':
    unittest.main()
