# SPDX-License-Identifier: MIT
"""v0.4.0.8 · Sell-side support — auto-Customer for inflow + dual-role parties.

The gap these cover: everything through v0.4.0.7 was Accounts Payable. The only
party Bank Bridge would auto-create was a Supplier, so a rule categorizing money
coming IN — a fruit-buyer deposit, a USDA/FSA payment, a grant, lease revenue —
booked its counterparty on the AP ledger and seeded the 1099-NEC vendor list
with people who are actually customers.

v0.4.0.8 derives the side from the OFFSET ACCOUNT's root_type (Income →
Customer, Expense → Supplier, anything else → no party), auto-creates both
sides, and provisions BOTH records for a dual-role party — a bank pays you
interest and charges you fees, so it is a Customer and a Supplier at once.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import categorization, erpnext_bank  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        Customer, GeneratedJournalEntry, PlaidAccount,
                        PlaidItem, Supplier)
from scripts import backfill_customer_records as backfill  # noqa: E402
from tests.fakes import FakeERPClient  # noqa: E402

# A chart with one account of each root_type that matters, under 'Testing'.
# `account_name` is the bare LOGICAL name a Mode B (Company-agnostic) rule uses;
# the docname carries the Company abbreviation, like a real ERPNext chart.
CHART = [
    {'name': 'Fruit Sales - T', 'account_name': 'Fruit Sales',
     'company': 'Testing', 'root_type': 'Income'},
    {'name': 'Interest Income - T', 'account_name': 'Interest Income',
     'company': 'Testing', 'root_type': 'Income'},
    {'name': 'Fuel Expense - T', 'account_name': 'Fuel Expense',
     'company': 'Testing', 'root_type': 'Expense'},
    {'name': 'Bank Fees - T', 'account_name': 'Bank Fees',
     'company': 'Testing', 'root_type': 'Expense'},
    {'name': 'Checking 1111 - T', 'account_name': 'Checking 1111',
     'company': 'Testing', 'root_type': 'Asset'},
]


def _erp(**kw):
    kw.setdefault('companies', ['Testing'])
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
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._seed_account()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    # ── fixtures ─────────────────────────────────────────────────
    def _seed_account(self, institution='Wells Fargo', company='Testing'):
        db.session.add(PlaidItem(
            item_id='item-1', access_token_encrypted='x',
            institution_id='ins_1', institution_name=institution,
            owning_company=company))
        db.session.add(PlaidAccount(
            account_id='acct-1', item_id='item-1', name='Checking',
            mask='1111', type='depository', subtype='checking',
            erpnext_bank_account_name='Checking - WF',
            erpnext_gl_account_name='Checking 1111 - T',
            owning_company=company, import_status='imported'))
        db.session.commit()

    def _txn(self, tid='t1', name='DEPOSIT', merchant='', amount=-900.0,
             account_id='acct-1'):
        row = BankTransaction(
            plaid_transaction_id=tid, account_id=account_id, amount=amount,
            name=name, merchant_name=merchant, date=date(2026, 7, 10),
            erpnext_bank_transaction_id='ACC-BTN-0001',
            posted_at=categorization._now())
        db.session.add(row)
        db.session.commit()
        return row

    def _rule(self, **kw):
        vals = {'name': 'Sales', 'match_type': 'description_regex',
                'match_value': '.*', 'offset_account': 'Fruit Sales - T',
                'offset_direction': 'auto', 'party_type': 'Auto',
                'applies_to_company': 'Testing', 'active': True,
                'archived': False, 'priority': 10}
        vals.update(kw)
        rule = CategorizationRule(**vals)
        db.session.add(rule)
        db.session.commit()
        return rule

    def _je_for(self, erp, gje):
        return erp.created['Journal Entry'][gje.erpnext_journal_entry_name]

    def _party_line(self, doc):
        lines = [ln for ln in doc['accounts'] if ln.get('party')]
        return lines[0] if lines else None


class AutoPartyTypeFromOffset(Base):
    """Fix A + C — the side is derived from the offset account's root_type."""

    def test_income_offset_books_a_customer(self):
        """THE HEADLINE CASE. A fruit-buyer deposit categorized to an Income
        account books a Customer — and creates one, on the AR side."""
        erp = _erp()
        rule = self._rule(party_name='Valley Packing')
        row = self._txn(tid='t-sale', amount=-4200.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        self.assertEqual(gje.state, 'pending_review')
        line = self._party_line(self._je_for(erp, gje))
        self.assertEqual(line['party_type'], 'Customer')
        self.assertEqual(line['party'], 'Valley Packing')
        self.assertIn('Valley Packing', erp.created['Customer'])
        self.assertEqual(erp.created['Supplier'], {})

    def test_expense_offset_still_books_a_supplier(self):
        """The v0.4.0.7 behaviour, reached through the new derivation."""
        erp = _erp()
        rule = self._rule(name='Fuel', offset_account='Fuel Expense - T',
                          party_name='Tractor Supply')
        row = self._txn(tid='t-fuel', amount=120.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        line = self._party_line(self._je_for(erp, gje))
        self.assertEqual(line['party_type'], 'Supplier')
        self.assertIn('Tractor Supply', erp.created['Supplier'])
        self.assertEqual(erp.created['Customer'], {})

    def test_asset_offset_books_no_party_at_all(self):
        """An Asset offset is a transfer between accounts you own — the same
        answer v0.4.0.7's skip_party heuristic gives, reached from root_type."""
        erp = _erp()
        rule = self._rule(name='Transfer', offset_account='Checking 1111 - T',
                          party_name='Wells Fargo')
        row = self._txn(tid='t-xfer', amount=500.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        self.assertEqual(gje.state, 'pending_review')
        self.assertIsNone(self._party_line(self._je_for(erp, gje)))
        self.assertEqual(erp.created['Customer'], {})
        self.assertEqual(erp.created['Supplier'], {})

    def test_auto_resolves_a_logical_offset_for_an_agnostic_rule(self):
        """A Mode B (Company-agnostic) rule names a LOGICAL account, so the
        root_type lookup has to fall back from docname to account_name."""
        erp = _erp()
        rule = self._rule(applies_to_company=None,
                          offset_account='Fruit Sales', party_name='Broker Co')
        row = self._txn(tid='t-agnostic', amount=-800.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        line = self._party_line(self._je_for(erp, gje))
        self.assertEqual(line['party_type'], 'Customer')
        self.assertIn('Broker Co', erp.created['Customer'])

    def test_root_type_is_looked_up_once_per_offset_not_per_transaction(self):
        """sync_engine loops JE generation over EVERY row, so an un-memoized
        derivation would re-ask ERPNext for the same account's root_type once
        per transaction — hundreds of extra HTTP calls on a real sync."""
        erp = _erp()
        rule = self._rule(party_name='Valley Packing')
        seen = []
        real = erpnext_bank.root_type_for_account

        def counting(client, account, company=''):
            seen.append((account, company))
            return real(client, account, company)

        categorization.erpnext_bank.root_type_for_account = counting
        try:
            for i in range(10):
                categorization.generate_journal_entry(
                    erp, self._txn(tid=f't-cache{i}', amount=-100.0), rule=rule)
        finally:
            categorization.erpnext_bank.root_type_for_account = real

        self.assertEqual(len(erp.created['Journal Entry']), 10)
        self.assertEqual(seen, [('Fruit Sales - T', 'Testing')])

    def test_unknown_offset_root_type_books_no_party(self):
        """An account the chart doesn't have → no root_type → don't guess a
        party. A JE with no party still posts; a wrong one corrupts a ledger."""
        erp = _erp()
        rule = self._rule(offset_account='Nonexistent - T',
                          party_name='Mystery Co')
        row = self._txn(tid='t-unknown', amount=-100.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        self.assertEqual(gje.state, 'pending_review')
        self.assertIsNone(self._party_line(self._je_for(erp, gje)))


class ExplicitPartyTypeOverrides(Base):
    """Fix C/D — a literal party_type beats the derivation, both directions."""

    def test_customer_forced_even_on_an_expense_offset(self):
        erp = _erp()
        rule = self._rule(party_type='Customer',
                          offset_account='Fuel Expense - T',
                          party_name='Odd Co')
        row = self._txn(tid='t-force-c', amount=75.0)

        line = self._party_line(self._je_for(
            erp, categorization.generate_journal_entry(erp, row, rule=rule)))

        self.assertEqual(line['party_type'], 'Customer')
        self.assertIn('Odd Co', erp.created['Customer'])

    def test_supplier_forced_even_on_an_income_offset(self):
        erp = _erp()
        rule = self._rule(party_type='Supplier',
                          offset_account='Fruit Sales - T',
                          party_name='Rebate Co')
        row = self._txn(tid='t-force-s', amount=-75.0)

        line = self._party_line(self._je_for(
            erp, categorization.generate_journal_entry(erp, row, rule=rule)))

        self.assertEqual(line['party_type'], 'Supplier')
        self.assertIn('Rebate Co', erp.created['Supplier'])

    def test_party_type_none_omits_the_party(self):
        """'— none —' (NULL) has meant "no party" since v0.3.0 and still does."""
        erp = _erp()
        rule = self._rule(party_type=None, party_name='Ignored Co')
        row = self._txn(tid='t-none', amount=-50.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        self.assertIsNone(self._party_line(self._je_for(erp, gje)))
        self.assertEqual(erp.created['Customer'], {})

    def test_skip_party_still_wins_over_auto(self):
        """Regression · the v0.4.0.7 transfer override outranks the derivation,
        so an operator's saved "no party" is never second-guessed."""
        erp = _erp()
        rule = self._rule(party_type='Auto', skip_party=True,
                          party_name='Wells Fargo')
        row = self._txn(tid='t-skip', amount=500.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        self.assertIsNone(self._party_line(self._je_for(erp, gje)))
        self.assertEqual(erp.created['Customer'], {})
        self.assertEqual(erp.created['Supplier'], {})


class DualRoleParties(Base):
    """Fix B — banks and brokerages get BOTH records at first encounter."""

    def test_detection_covers_keywords_names_and_institution_source(self):
        self.assertTrue(erpnext_bank.is_dual_role_party('Columbia Bank'))
        self.assertTrue(erpnext_bank.is_dual_role_party('SELCO Credit Union'))
        self.assertTrue(erpnext_bank.is_dual_role_party('Northwest Federal Savings'))
        self.assertTrue(erpnext_bank.is_dual_role_party('Fidelity'))
        self.assertTrue(erpnext_bank.is_dual_role_party('Charles Schwab'))
        self.assertTrue(erpnext_bank.is_dual_role_party('Coinbase'))
        # Derived from a linked Plaid Item's institution → a bank by construction.
        self.assertTrue(erpnext_bank.is_dual_role_party('Anything',
                                                        source='institution'))
        # Ordinary vendors stay single-role.
        self.assertFalse(erpnext_bank.is_dual_role_party('Tractor Supply'))
        self.assertFalse(erpnext_bank.is_dual_role_party('Starbucks'))
        self.assertFalse(erpnext_bank.is_dual_role_party('Uber'))
        # A whole-word match, so a namesake business isn't mistaken for the
        # brokerage it shares a word with.
        self.assertFalse(erpnext_bank.is_dual_role_party('Valley Schwab Farms'))

    def test_config_can_force_a_party_to_either_role(self):
        """The escape hatch for a chart that disagrees with the heuristic."""
        self.app.config['BANKBRIDGE_SINGLE_ROLE_PARTIES'] = ['Federal Express']
        self.app.config['BANKBRIDGE_DUAL_ROLE_PARTIES'] = ['Valley Packing']
        self.assertFalse(erpnext_bank.is_dual_role_party('Federal Express'))
        self.assertTrue(erpnext_bank.is_dual_role_party('Valley Packing'))

    def test_interest_from_a_bank_creates_both_sides(self):
        """Wells Fargo pays interest today (Customer) and will charge a fee next
        month (Supplier). Both records exist after the first transaction, which
        is what keeps that second JE from failing on a missing party."""
        erp = _erp()
        rule = self._rule(name='Interest',
                          offset_account='Interest Income - T',
                          party_name='Wells Fargo')
        row = self._txn(tid='t-int', name='INTRST PYMNT', amount=-12.5)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        line = self._party_line(self._je_for(erp, gje))
        self.assertEqual(line['party_type'], 'Customer')
        self.assertIn('Wells Fargo', erp.created['Customer'])
        self.assertIn('Wells Fargo', erp.created['Supplier'])
        # Both local mirrors carry the name, each with its own ERPNext docname.
        self.assertIsNotNone(
            Customer.query.filter_by(normalized_name='Wells Fargo').first())
        self.assertIsNotNone(
            Supplier.query.filter_by(normalized_name='Wells Fargo').first())

    def test_non_dual_role_party_stays_single_role_until_it_reverses(self):
        """Tractor Supply sells to you (Supplier). Only when a reverse-direction
        transaction actually shows up — a refund booked to Income — does the
        other role get created."""
        erp = _erp()
        supplier_rule = self._rule(name='Fuel',
                                   offset_account='Fuel Expense - T',
                                   party_name='Tractor Supply')
        categorization.generate_journal_entry(
            erp, self._txn(tid='t-ts-buy', amount=120.0), rule=supplier_rule)

        self.assertIn('Tractor Supply', erp.created['Supplier'])
        self.assertEqual(erp.created['Customer'], {})

        customer_rule = self._rule(name='Rebate', priority=5,
                                   offset_account='Fruit Sales - T',
                                   party_name='Tractor Supply')
        categorization.generate_journal_entry(
            erp, self._txn(tid='t-ts-rebate', amount=-30.0), rule=customer_rule)

        self.assertIn('Tractor Supply', erp.created['Customer'])

    def test_a_failed_counterpart_never_costs_the_primary_party(self):
        """The dual-role create is best-effort: the party the JE actually needs
        must still post when its counterpart can't be made."""
        erp = _erp(fail_supplier_create=True)
        rule = self._rule(name='Interest',
                          offset_account='Interest Income - T',
                          party_name='Columbia Bank')
        row = self._txn(tid='t-int2', amount=-9.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        self.assertEqual(gje.state, 'pending_review')
        self.assertIn('Columbia Bank', erp.created['Customer'])
        self.assertEqual(erp.created['Supplier'], {})


class CustomerAutoCreate(Base):
    """ensure_customer — the AR twin of ensure_supplier."""

    def test_is_idempotent_and_reuses_an_existing_erpnext_customer(self):
        erp = _erp(existing_customers=['Valley Packing'])

        first = erpnext_bank.ensure_customer(erp, 'Valley Packing')
        second = erpnext_bank.ensure_customer(erp, 'Valley Packing')

        self.assertEqual(first, 'Valley Packing')
        self.assertEqual(second, 'Valley Packing')
        self.assertEqual(erp.created['Customer'], {})       # matched, not made
        self.assertEqual(
            Customer.query.filter_by(normalized_name='Valley Packing').count(), 1)

    def test_returns_none_when_erpnext_refuses_the_create(self):
        """A name we know ERPNext will reject must not reach the JE — the
        caller then books no party rather than a LinkValidationError."""
        erp = _erp(fail_customer_create=True)
        self.assertIsNone(erpnext_bank.ensure_customer(erp, 'Nope Co'))

    def test_a_bank_customer_is_filed_under_financial_institutions(self):
        erp = _erp()
        erpnext_bank.ensure_customer(erp, 'Wells Fargo', source='institution')
        self.assertEqual(
            erp.created['Customer']['Wells Fargo']['customer_group'],
            'Financial Institutions')


class BackfillCustomerRecords(Base):
    """Fix E — the cleanup script for JEs already booked the wrong way."""

    def _posted(self, erp, tid, rule, *, submitted=False):
        """Generate a JE the pre-v0.4.0.8 way (Supplier on an Income offset) and
        return its GeneratedJournalEntry."""
        row = self._txn(tid=tid, amount=-500.0)
        gje = categorization.generate_journal_entry(erp, row, rule=rule)
        if submitted:
            erp.created['Journal Entry'][
                gje.erpnext_journal_entry_name]['docstatus'] = 1
            erp.docs[gje.erpnext_journal_entry_name]['docstatus'] = 1
        return gje

    def _legacy_rule(self, **kw):
        # party_type='Supplier' on an Income offset is exactly the shape every
        # pre-v0.4.0.8 sell-side rule had.
        kw.setdefault('party_type', 'Supplier')
        kw.setdefault('offset_account', 'Fruit Sales - T')
        kw.setdefault('party_name', 'Valley Packing')
        return self._rule(**kw)

    def test_identifies_the_supplier_on_income_mismatch(self):
        erp = _erp()
        self._posted(erp, 't-bf1', self._legacy_rule())

        hits = backfill.mismatches(erp)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['party'], 'Valley Packing')
        self.assertEqual(hits[0]['offset_account'], 'Fruit Sales - T')

    def test_leaves_a_correct_expense_side_je_alone(self):
        """An Expense offset with a Supplier is right — not a mismatch."""
        erp = _erp()
        self._posted(erp, 't-bf-ok',
                     self._legacy_rule(offset_account='Fuel Expense - T'))

        self.assertEqual(backfill.mismatches(erp), [])

    def test_creates_the_customer_and_repoints_a_draft_je(self):
        erp = _erp()
        gje = self._posted(erp, 't-bf2', self._legacy_rule())

        result = backfill.run(erp)

        self.assertEqual(result['considered'], 1)
        self.assertEqual(result['customers_ensured'], ['Valley Packing'])
        self.assertEqual(result['repointed'], [gje.erpnext_journal_entry_name])
        self.assertIn('Valley Packing', erp.created['Customer'])
        line = self._party_line(erp.docs[gje.erpnext_journal_entry_name])
        self.assertEqual(line['party_type'], 'Customer')
        self.assertEqual(line['party'], 'Valley Packing')

    def test_reports_but_never_amends_a_submitted_je(self):
        """A submitted Journal Entry is immutable in Frappe. The Customer is
        still created; the posted entry is left for the operator."""
        erp = _erp()
        gje = self._posted(erp, 't-bf3', self._legacy_rule(), submitted=True)

        result = backfill.run(erp)

        self.assertEqual(result['repointed'], [])
        self.assertEqual([n for n, _ in result['submitted_skipped']],
                         [gje.erpnext_journal_entry_name])
        self.assertIn('Valley Packing', erp.created['Customer'])
        # Untouched: the posted books still name the Supplier.
        line = self._party_line(erp.docs[gje.erpnext_journal_entry_name])
        self.assertEqual(line['party_type'], 'Supplier')

    def test_dry_run_changes_nothing(self):
        erp = _erp()
        gje = self._posted(erp, 't-bf4', self._legacy_rule())

        result = backfill.run(erp, dry_run=True)

        self.assertEqual(result['considered'], 1)
        self.assertEqual(result['customers_ensured'], [])
        self.assertEqual(erp.created['Customer'], {})
        line = self._party_line(erp.docs[gje.erpnext_journal_entry_name])
        self.assertEqual(line['party_type'], 'Supplier')

    def test_is_idempotent(self):
        """A second run finds nothing left to fix — the repointed JE no longer
        reads as a Supplier mismatch."""
        erp = _erp()
        self._posted(erp, 't-bf5', self._legacy_rule())

        backfill.run(erp)
        second = backfill.run(erp)

        self.assertEqual(second['considered'], 0)
        self.assertEqual(second['repointed'], [])


class BackwardCompatibility(Base):
    """Regressions the release must not break."""

    def test_existing_supplier_rules_keep_working_unchanged(self):
        """A merchant rule (Uber, Starbucks) with a literal party_type of
        Supplier behaves exactly as it did in v0.4.0.7."""
        erp = _erp()
        rule = self._rule(name='Uber', party_type='Supplier',
                          offset_account='Fuel Expense - T', party_name=None)
        row = self._txn(tid='t-uber', merchant='UBER TRIP', amount=32.0)

        gje = categorization.generate_journal_entry(
            erp, row, rule=rule, supplier_name='Uber')

        line = self._party_line(self._je_for(erp, gje))
        self.assertEqual(line['party_type'], 'Supplier')
        self.assertEqual(line['party'], 'Uber')
        self.assertIn('Uber', erp.created['Supplier'])

    def test_a_rule_with_no_party_type_defaults_to_no_party(self):
        """Backward compat · the column has defaulted to NULL since v0.3.0 and
        NULL has always meant "no Party". v0.4.0.8 must not start inventing one
        for rules that never asked for it."""
        rule = self._rule(party_type=None)
        self.assertIsNone(rule.party_type)
        self.assertIsNone(rule.to_dict()['party_type'])
        self.assertEqual(
            categorization.effective_party_type(_erp(), rule, 'Testing'), '')

    def test_cross_company_push_time_block_still_applies(self):
        """Regression · the v0.4.0.2 guard fires before any party work matters."""
        erp = _erp(chart_accounts=CHART + [
            {'name': 'Other Sales - O', 'account_name': 'Other Sales',
             'company': 'Other Co', 'root_type': 'Income'}])
        rule = self._rule(offset_account='Other Sales - O',
                          party_name='Valley Packing')
        row = self._txn(tid='t-cross', amount=-100.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        self.assertEqual(gje.state, 'blocked')
        self.assertIn('cross-Company', gje.error_message)

    def test_party_types_are_the_four_the_editor_offers(self):
        self.assertEqual(categorization.PARTY_TYPES,
                         ('', 'Supplier', 'Customer', 'Auto'))


if __name__ == '__main__':
    unittest.main()
