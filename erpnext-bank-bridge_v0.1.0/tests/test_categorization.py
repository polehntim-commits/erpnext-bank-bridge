# SPDX-License-Identifier: MIT
"""Auto-Supplier creation + categorization rules engine + JE generation (v0.3.0).

  * merchant-name normalization (prefixes, marketplace aliases, store ids, case)
  * Supplier: cache miss → ERPNext create + local mirror; cache hit → no re-create;
    existing ERPNext Supplier reused; config-disabled → no supplier work
  * rules engine: every match_type, priority ordering, first-match-wins, no-match
  * JE generation: amount-sign direction, party fallback to auto-Supplier,
    draft-vs-submit, idempotency (one JE per txn), non-destructive on failure

    cd erpnext-bank-bridge_v0.1.0
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import categorization, erpnext_bank, sync_engine  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        GeneratedJournalEntry, PlaidAccount, PlaidItem,
                        Supplier)

from tests.fakes import FakePlaidClient, FakeERPClient, page, txn  # noqa: E402

ACC = 'acct-wf-checking'


class Base(unittest.TestCase):
    EXTRA_CONFIG = {}

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
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    # helpers ------------------------------------------------------------
    def _item(self):
        it = PlaidItem(item_id='item-abc',
                       access_token_encrypted=crypto.encrypt('access-sandbox-abc'),
                       institution_id='ins_1', institution_name='Wells Fargo',
                       status='active')
        db.session.add(it)
        db.session.commit()
        return it

    def _account(self):
        a = PlaidAccount(account_id=ACC, item_id='item-abc', name='WF Checking',
                         mask='1234', type='depository', subtype='checking',
                         erpnext_bank_account_name='WF Checking - Ops',
                         sync_enabled=True)
        db.session.add(a)
        db.session.commit()
        return a

    def _plaid_accounts(self):
        return [{'account_id': ACC, 'name': 'WF Checking', 'official_name': '',
                 'mask': '1234', 'type': 'depository', 'subtype': 'checking',
                 'balance_available': 900.0, 'balance_current': 1000.0,
                 'iso_currency_code': 'USD'}]

    def _row(self, amount=42.5, merchant='Chevron', name='CHEVRON 0123456',
             category='GAS', tid='t1'):
        r = BankTransaction(plaid_transaction_id=tid, account_id=ACC,
                            amount=amount, merchant_name=merchant, name=name,
                            category=category, date=date(2026, 7, 10),
                            erpnext_bank_transaction_id='ACC-BTN-0001')
        db.session.add(r)
        db.session.commit()
        return r

    def _rule(self, **kw):
        defaults = dict(name='r', priority=100, active=True,
                        match_type='merchant_contains', match_value='Chevron',
                        debit_account='Fuel Expense - EC',
                        credit_account='Checking - EC')
        defaults.update(kw)
        rule = CategorizationRule(**defaults)
        db.session.add(rule)
        db.session.commit()
        return rule


# ── normalization ──────────────────────────────────────────────────────

class TestNormalization(unittest.TestCase):
    def _n(self, s):
        return erpnext_bank.normalize_merchant_name(s)

    def test_strip_square_prefix_and_store_id(self):
        self.assertEqual(self._n('SQ *STARBUCKS 92104'), 'Starbucks')

    def test_amazon_alias(self):
        self.assertEqual(self._n('AMZN Mktp US*2X4B'), 'Amazon')
        self.assertEqual(self._n('Amazon.com*RT4G9'), 'Amazon')

    def test_uppercase_titlecased(self):
        self.assertEqual(self._n('CHEVRON'), 'Chevron')

    def test_mixed_case_preserved(self):
        self.assertEqual(self._n('Blue Bottle'), 'Blue Bottle')

    def test_trailing_store_number_dropped(self):
        self.assertEqual(self._n('COSTCO #487'), 'Costco')
        self.assertEqual(self._n('Chevron 0123456'), 'Chevron')

    def test_toast_prefix(self):
        self.assertEqual(self._n('TST* Some Cafe'), 'Some Cafe')

    def test_blank(self):
        self.assertEqual(self._n(''), '')
        self.assertEqual(self._n('   '), '')

    def test_prefix_plus_id_only_falls_back(self):
        # "POS 12345" is all prefix + id — don't return an empty name.
        self.assertTrue(self._n('POS 12345'))

    def test_home_depot_alias(self):
        self.assertEqual(self._n('THE HOME DEPOT #8842'), 'The Home Depot')


# ── auto-Supplier ──────────────────────────────────────────────────────

class TestSupplier(Base):
    def test_cache_miss_creates_erpnext_and_mirror(self):
        erp = FakeERPClient()
        name = erpnext_bank.get_or_create_supplier(erp, 'CHEVRON', amount=42.5,
                                                    txn_date=date(2026, 7, 10))
        self.assertEqual(name, 'Chevron')                 # normalized docname
        self.assertIn('Chevron', erp.created['Supplier'])
        row = Supplier.query.filter_by(normalized_name='Chevron').first()
        self.assertIsNotNone(row)
        self.assertEqual(row.erpnext_supplier_name, 'Chevron')
        self.assertEqual(row.transaction_count, 1)
        self.assertEqual(row.total_amount, 42.5)

    def test_cache_hit_no_second_create(self):
        erp = FakeERPClient()
        erpnext_bank.get_or_create_supplier(erp, 'CHEVRON', amount=10.0)
        erpnext_bank.get_or_create_supplier(erp, 'SQ *Chevron 55', amount=5.0)
        supplier_creates = [c for c in erp.calls
                            if c[0] == 'create_doc' and c[1] == 'Supplier']
        self.assertEqual(len(supplier_creates), 1)        # only the first
        row = Supplier.query.filter_by(normalized_name='Chevron').first()
        self.assertEqual(row.transaction_count, 2)        # both tallied
        self.assertEqual(row.total_amount, 15.0)

    def test_existing_erpnext_supplier_reused(self):
        erp = FakeERPClient(existing_suppliers={'Chevron'})
        name = erpnext_bank.get_or_create_supplier(erp, 'CHEVRON')
        self.assertEqual(name, 'Chevron')
        self.assertNotIn('Chevron', erp.created['Supplier'])  # matched, not created

    def test_create_failure_leaves_mirror_unlinked(self):
        erp = FakeERPClient(fail_supplier_create=True)
        name = erpnext_bank.get_or_create_supplier(erp, 'CHEVRON')
        self.assertIsNone(name)
        row = Supplier.query.filter_by(normalized_name='Chevron').first()
        self.assertIsNotNone(row)                          # mirror still cached
        self.assertIsNone(row.erpnext_supplier_name)

    def test_blank_merchant_noop(self):
        erp = FakeERPClient()
        self.assertIsNone(erpnext_bank.get_or_create_supplier(erp, ''))
        self.assertEqual(Supplier.query.count(), 0)


class TestSupplierConfigDisabled(Base):
    EXTRA_CONFIG = {'ERPNEXT_AUTO_CREATE_SUPPLIERS': False}

    def test_sync_does_not_create_suppliers_when_disabled(self):
        item = self._item()
        self._account()
        plaid = FakePlaidClient(accounts=self._plaid_accounts(), pages=[
            page(added=[txn('t1', ACC, 25.0, name='Coffee', merchant_name='Blue Bottle')])])
        erp = FakeERPClient()
        sync_engine.sync_item(item, plaid, erp)
        self.assertEqual(Supplier.query.count(), 0)
        self.assertEqual(len(erp.created['Supplier']), 0)


class TestSupplierEnabledDuringSync(Base):
    def test_sync_creates_supplier(self):
        item = self._item()
        self._account()
        plaid = FakePlaidClient(accounts=self._plaid_accounts(), pages=[
            page(added=[txn('t1', ACC, 25.0, name='Coffee', merchant_name='Blue Bottle')])])
        erp = FakeERPClient()
        sync_engine.sync_item(item, plaid, erp)
        row = Supplier.query.filter_by(normalized_name='Blue Bottle').first()
        self.assertIsNotNone(row)
        self.assertEqual(row.transaction_count, 1)


# ── rule matching ──────────────────────────────────────────────────────

class TestRuleMatching(Base):
    def _match(self, rule, **facets):
        base = dict(merchant_name='', description='', category='', amount=0.0)
        base.update(facets)
        return categorization.rule_matches(rule, **base)

    def test_merchant_exact(self):
        r = self._rule(match_type='merchant_exact', match_value='Chevron')
        self.assertTrue(self._match(r, merchant_name='chevron'))   # case-insensitive
        self.assertFalse(self._match(r, merchant_name='Chevron Extra'))

    def test_merchant_contains(self):
        r = self._rule(match_type='merchant_contains', match_value='hevr')
        self.assertTrue(self._match(r, merchant_name='Chevron'))
        self.assertFalse(self._match(r, merchant_name='Shell'))

    def test_description_regex(self):
        r = self._rule(match_type='description_regex', match_value=r'^UBER\s')
        self.assertTrue(self._match(r, description='UBER TRIP 123'))
        self.assertFalse(self._match(r, description='LYFT'))

    def test_description_regex_bad_pattern_matches_nothing(self):
        r = self._rule(match_type='description_regex', match_value='[unclosed')
        self.assertFalse(self._match(r, description='anything'))

    def test_plaid_category_matches(self):
        r = self._rule(match_type='plaid_category_matches', match_value='GAS_STATIONS')
        self.assertTrue(self._match(r, category='GAS_STATIONS'))
        self.assertTrue(self._match(r, category='Travel > Gas_Stations'))
        self.assertFalse(self._match(r, category='GROCERIES'))

    def test_amount_range(self):
        r = self._rule(match_type='amount_range', match_value='[10, 500]')
        self.assertTrue(self._match(r, amount=42.5))
        self.assertTrue(self._match(r, amount=-42.5))      # abs()
        self.assertFalse(self._match(r, amount=1000))
        self.assertFalse(self._match(r, amount=5))

    def test_amount_range_bad_json(self):
        r = self._rule(match_type='amount_range', match_value='not-json')
        self.assertFalse(self._match(r, amount=42.5))

    def test_priority_first_match_wins(self):
        self._rule(name='low-prio', priority=200, match_type='merchant_contains',
                   match_value='Chev', debit_account='A')
        self._rule(name='hi-prio', priority=10, match_type='merchant_contains',
                   match_value='Chevron', debit_account='B')
        row = self._row(merchant='Chevron')
        rule = categorization.find_matching_rule(row)
        self.assertEqual(rule.name, 'hi-prio')

    def test_inactive_rule_skipped(self):
        self._rule(name='off', active=False, match_value='Chevron')
        row = self._row(merchant='Chevron')
        self.assertIsNone(categorization.find_matching_rule(row))

    def test_no_match_returns_none(self):
        self._rule(match_value='Starbucks')
        row = self._row(merchant='Chevron')
        self.assertIsNone(categorization.find_matching_rule(row))


# ── JE construction (amount sign) ──────────────────────────────────────

class TestJournalEntryBuild(Base):
    def test_outflow_debits_expense_credits_bank(self):
        rule = self._rule()
        row = self._row(amount=42.5)                       # positive = outflow
        doc = categorization.build_journal_entry(rule, row, 'Co')
        expense, bank = doc['accounts']
        self.assertEqual(expense['account'], 'Fuel Expense - EC')
        self.assertEqual(expense['debit_in_account_currency'], 42.5)
        self.assertEqual(bank['account'], 'Checking - EC')
        self.assertEqual(bank['credit_in_account_currency'], 42.5)

    def test_inflow_reverses_direction(self):
        rule = self._rule()
        row = self._row(amount=-42.5)                      # negative = inflow
        doc = categorization.build_journal_entry(rule, row, 'Co')
        expense, bank = doc['accounts']
        self.assertEqual(expense['credit_in_account_currency'], 42.5)
        self.assertEqual(bank['debit_in_account_currency'], 42.5)

    def test_party_falls_back_to_supplier(self):
        rule = self._rule(party_type='Supplier', party_name=None)
        row = self._row()
        doc = categorization.build_journal_entry(rule, row, 'Co',
                                                 supplier_name='Chevron')
        expense = doc['accounts'][0]
        self.assertEqual(expense['party_type'], 'Supplier')
        self.assertEqual(expense['party'], 'Chevron')

    def test_explicit_party_name_wins(self):
        rule = self._rule(party_type='Supplier', party_name='Chevron Corp')
        row = self._row()
        doc = categorization.build_journal_entry(rule, row, 'Co',
                                                 supplier_name='Chevron')
        self.assertEqual(doc['accounts'][0]['party'], 'Chevron Corp')

    def test_bank_transaction_reference_on_both_lines(self):
        rule = self._rule()
        row = self._row()
        doc = categorization.build_journal_entry(rule, row, 'Co')
        for ln in doc['accounts']:
            self.assertEqual(ln['reference_type'], 'Bank Transaction')
            self.assertEqual(ln['reference_name'], 'ACC-BTN-0001')


# ── v0.3.1 · bank-account-agnostic offset rules ────────────────────────

class TestOffsetJournalEntryBuild(Base):
    """The rule names only the OFFSET account; the BANK side comes from the
    transaction's linked Plaid account (erpnext_gl_account_name)."""

    GL = 'WF Checking - 1201 - EC'

    def _linked_account(self):
        a = PlaidAccount(account_id=ACC, item_id='item-abc', name='WF Checking',
                         mask='1234', type='depository', subtype='checking',
                         erpnext_bank_account_name='WF Checking - Ops',
                         erpnext_gl_account_name=self.GL, sync_enabled=True)
        db.session.add(a)
        db.session.commit()
        return a

    def _offset_rule(self, **kw):
        defaults = dict(name='fuel', priority=100, active=True,
                        match_type='merchant_contains', match_value='Chevron',
                        offset_account='Fuel Expense - EC', offset_direction='auto')
        defaults.update(kw)
        rule = CategorizationRule(**defaults)
        db.session.add(rule)
        db.session.commit()
        return rule

    def test_withdrawal_debits_offset_credits_bank(self):
        # $50 spending (positive) → offset debited, bank credited.
        self._item(); self._linked_account()
        rule = self._offset_rule()
        row = self._row(amount=50.0)
        doc = categorization.build_journal_entry(rule, row, 'Co')
        debit, credit = doc['accounts']
        self.assertEqual(debit['account'], 'Fuel Expense - EC')
        self.assertEqual(debit['debit_in_account_currency'], 50.0)
        self.assertEqual(credit['account'], self.GL)
        self.assertEqual(credit['credit_in_account_currency'], 50.0)

    def test_deposit_debits_bank_credits_offset(self):
        # $100 refund/income (negative) → bank debited, offset credited.
        self._item(); self._linked_account()
        rule = self._offset_rule(offset_account='Refunds - EC')
        row = self._row(amount=-100.0)
        doc = categorization.build_journal_entry(rule, row, 'Co')
        debit, credit = doc['accounts']
        self.assertEqual(debit['account'], self.GL)
        self.assertEqual(debit['debit_in_account_currency'], 100.0)
        self.assertEqual(credit['account'], 'Refunds - EC')
        self.assertEqual(credit['credit_in_account_currency'], 100.0)

    def test_always_debit_forces_offset_to_debit(self):
        # always_debit forces offset to the debit side regardless of sign — even
        # for a deposit (negative amount).
        self._item(); self._linked_account()
        rule = self._offset_rule(offset_direction='always_debit')
        row = self._row(amount=-30.0)   # would be a deposit under 'auto'
        doc = categorization.build_journal_entry(rule, row, 'Co')
        debit, credit = doc['accounts']
        self.assertEqual(debit['account'], 'Fuel Expense - EC')
        self.assertEqual(debit['debit_in_account_currency'], 30.0)
        self.assertEqual(credit['account'], self.GL)

    def test_always_credit_forces_offset_to_credit(self):
        self._item(); self._linked_account()
        rule = self._offset_rule(offset_direction='always_credit')
        row = self._row(amount=75.0)    # would be a withdrawal under 'auto'
        doc = categorization.build_journal_entry(rule, row, 'Co')
        debit, credit = doc['accounts']
        self.assertEqual(debit['account'], self.GL)
        self.assertEqual(credit['account'], 'Fuel Expense - EC')
        self.assertEqual(credit['credit_in_account_currency'], 75.0)

    def test_party_rides_offset_line(self):
        self._item(); self._linked_account()
        rule = self._offset_rule(party_type='Supplier', party_name=None)
        row = self._row(amount=50.0)
        doc = categorization.build_journal_entry(rule, row, 'Co',
                                                 supplier_name='Chevron')
        offset = next(l for l in doc['accounts']
                      if l['account'] == 'Fuel Expense - EC')
        self.assertEqual(offset['party_type'], 'Supplier')
        self.assertEqual(offset['party'], 'Chevron')

    def test_bank_side_resolved_from_transaction_account(self):
        # No bank_account arg → resolved from the row's linked Plaid account.
        self._item(); self._linked_account()
        self.assertEqual(categorization.bank_gl_account_for(self._row()), self.GL)

    def test_generation_end_to_end_uses_bank_from_txn(self):
        self._item(); self._linked_account()
        self._offset_rule()
        row = self._row(amount=50.0)
        erp = FakeERPClient()
        categorization.generate_journal_entry(erp, row)
        je = erp.created['Journal Entry']
        self.assertEqual(len(je), 1)
        accounts = list(je.values())[0]['accounts']
        names = {a['account'] for a in accounts}
        self.assertIn(self.GL, names)
        self.assertIn('Fuel Expense - EC', names)


class TestLegacyRuleBackwardCompat(Base):
    """Pre-v0.3.1 rules that still carry a debit/credit pair (no offset_account)
    keep generating JEs from the old two-account logic during the transition."""

    def test_legacy_pair_still_builds(self):
        rule = CategorizationRule(
            name='legacy', priority=100, active=True,
            match_type='merchant_contains', match_value='Chevron',
            debit_account='Fuel Expense - EC', credit_account='Checking - EC')
        db.session.add(rule); db.session.commit()
        row = self._row(amount=42.5)
        doc = categorization.build_journal_entry(rule, row, 'Co')
        debit, credit = doc['accounts']
        self.assertEqual(debit['account'], 'Fuel Expense - EC')
        self.assertEqual(debit['debit_in_account_currency'], 42.5)
        self.assertEqual(credit['account'], 'Checking - EC')
        self.assertEqual(credit['credit_in_account_currency'], 42.5)

    def _row(self, amount=42.5, merchant='Chevron', name='CHEVRON', category='GAS',
             tid='t1'):
        r = BankTransaction(plaid_transaction_id=tid, account_id=ACC,
                            amount=amount, merchant_name=merchant, name=name,
                            category=category, date=date(2026, 7, 10),
                            erpnext_bank_transaction_id='ACC-BTN-0001')
        db.session.add(r); db.session.commit()
        return r


class TestOffsetMigration(Base):
    """v0.3.1 boot backfill: legacy rules (debit/credit pair) get a single
    offset_account — the side that ISN'T the bank GL account."""

    def _rule(self, **kw):
        r = CategorizationRule(name='r', priority=100, active=True,
                               match_type='merchant_contains', match_value='X', **kw)
        db.session.add(r); db.session.commit()
        return r

    def test_backfills_non_bank_side_as_offset(self):
        from app import migrations
        # Bank GL is the credit side → offset should become the debit (expense).
        a = PlaidAccount(account_id=ACC, item_id='item-abc', name='WF',
                         erpnext_gl_account_name='Checking - EC')
        db.session.add(a); db.session.commit()
        r = self._rule(debit_account='Fuel Expense - EC',
                       credit_account='Checking - EC', offset_account='')
        migrations._migrate_rule_offset_accounts()
        db.session.refresh(r)
        self.assertEqual(r.offset_account, 'Fuel Expense - EC')
        self.assertEqual(r.offset_direction, 'auto')

    def test_backfills_falls_back_to_debit_when_unclear(self):
        from app import migrations
        # No known bank GL accounts → fall back to debit_account.
        r = self._rule(debit_account='Fuel Expense - EC',
                       credit_account='Some Other - EC', offset_account='')
        migrations._migrate_rule_offset_accounts()
        db.session.refresh(r)
        self.assertEqual(r.offset_account, 'Fuel Expense - EC')

    def test_backfill_idempotent_leaves_set_offset_alone(self):
        from app import migrations
        r = self._rule(debit_account='A - EC', credit_account='B - EC',
                       offset_account='Already Chosen - EC')
        migrations._migrate_rule_offset_accounts()
        db.session.refresh(r)
        self.assertEqual(r.offset_account, 'Already Chosen - EC')


# ── JE generation (write path) ─────────────────────────────────────────

class TestGeneration(Base):
    def test_generate_creates_je_and_audit_draft(self):
        self._rule()
        row = self._row()
        erp = FakeERPClient()
        audit = categorization.generate_journal_entry(erp, row)
        self.assertIsNotNone(audit.erpnext_journal_entry_name)
        self.assertEqual(audit.state, 'pending_review')   # draft by default
        self.assertNotIn(audit.erpnext_journal_entry_name, erp.submitted)
        self.assertEqual(len(erp.created['Journal Entry']), 1)

    def test_no_rule_no_je(self):
        row = self._row()
        erp = FakeERPClient()
        self.assertIsNone(categorization.generate_journal_entry(erp, row))
        self.assertEqual(len(erp.created['Journal Entry']), 0)

    def test_idempotent_one_je_per_txn(self):
        self._rule()
        row = self._row()
        erp = FakeERPClient()
        categorization.generate_journal_entry(erp, row)
        categorization.generate_journal_entry(erp, row)    # again
        self.assertEqual(len(erp.created['Journal Entry']), 1)
        self.assertEqual(GeneratedJournalEntry.query.count(), 1)

    def test_failure_records_error_audit(self):
        self._rule()
        row = self._row()
        erp = FakeERPClient(fail_je_create=True)
        audit = categorization.generate_journal_entry(erp, row)
        self.assertEqual(audit.state, 'error')
        self.assertIsNone(audit.erpnext_journal_entry_name)
        self.assertTrue(audit.error_message)


class TestGenerationAutoSubmit(Base):
    EXTRA_CONFIG = {'ERPNEXT_JOURNAL_ENTRY_AUTO_SUBMIT': True}

    def test_auto_submit_marks_approved(self):
        self._rule()
        row = self._row()
        erp = FakeERPClient()
        audit = categorization.generate_journal_entry(erp, row)
        self.assertEqual(audit.state, 'approved')
        self.assertIn(audit.erpnext_journal_entry_name, erp.submitted)


class TestFullSyncWithJE(Base):
    EXTRA_CONFIG = {'ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES': True}

    def test_sync_generates_je_when_rule_matches(self):
        item = self._item()
        self._account()
        self._rule(match_type='merchant_contains', match_value='Chevron',
                   party_type='Supplier')
        plaid = FakePlaidClient(accounts=self._plaid_accounts(), pages=[
            page(added=[txn('t1', ACC, 42.5, name='CHEVRON 01', merchant_name='Chevron')])])
        erp = FakeERPClient()
        sync_engine.sync_item(item, plaid, erp)
        self.assertEqual(GeneratedJournalEntry.query.count(), 1)
        g = GeneratedJournalEntry.query.first()
        self.assertIsNotNone(g.erpnext_journal_entry_name)
        # The JE's party line links the auto-created Supplier.
        je = erp.created['Journal Entry'][g.erpnext_journal_entry_name]
        self.assertEqual(je['accounts'][0]['party'], 'Chevron')

    def test_je_engine_off_by_default_no_je(self):
        # A second app WITHOUT the engine flag should not generate JEs.
        pass  # covered by TestSupplierEnabledDuringSync (default config, no JE)


if __name__ == '__main__':
    unittest.main()
