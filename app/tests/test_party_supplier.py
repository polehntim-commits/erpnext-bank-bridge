# SPDX-License-Identifier: MIT
"""v0.4.0.7 · Auto-Supplier for EVERY party source + skip_party for transfers.

Root cause these cover: pre-v0.4.0.7 the auto-Supplier hung off Plaid's
`merchant_name`, so a merchant transaction (Uber, Starbucks) minted its Supplier
and its JE posted — but a DESCRIPTION-only transaction whose rule named a Party
("INTRST PYMNT" → Wells Fargo, "ACH Electronic CreditGUSTO PAY" → Gusto) put an
unbacked party on the Journal Entry and ERPNext refused the document:

    LinkValidationError: Could not find Row #1: Party: Wells Fargo
    POST /api/resource/Journal Entry -> 417

The fix moves the ensure onto the PARTY rather than the merchant field, and adds
`skip_party` for rules that book a transfer between two accounts you own (which
has no counterparty at all).

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import categorization, erpnext_bank  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        GeneratedJournalEntry, PlaidAccount, PlaidItem,
                        Supplier)
from scripts import backfill_missing_suppliers as backfill  # noqa: E402
from tests.fakes import FakeERPClient  # noqa: E402


class _PartyRejectingERP(FakeERPClient):
    """An ERPNext that refuses a Journal Entry naming a Party it has no Supplier
    for — exactly the 417 LinkValidationError Tim's dry run hit. This is what
    makes these tests a real regression net: with the fix reverted, every JE
    below fails here instead of silently passing."""

    def create_doc(self, doctype, doc):
        if doctype == 'Journal Entry':
            known = set(self.existing_suppliers) | set(self.created['Supplier'])
            for line in doc.get('accounts', []):
                party = line.get('party')
                if (line.get('party_type') == 'Supplier' and party
                        and party not in known):
                    from app.erpnext_client import ERPNextAPIError
                    raise ERPNextAPIError(
                        f'POST /api/resource/Journal Entry -> 417',
                        status_code=417,
                        response_body='{"exception": "LinkValidationError: Could '
                                      'not find Row #1: Party: ' + party + '"}')
        return super().create_doc(doctype, doc)


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
        self.client = self.app.test_client()
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
        """One linked Item + one imported, GL-mapped account — the shape every
        posted transaction has by the time the rules engine sees it."""
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

    def _txn(self, tid='t1', name='INTRST PYMNT', merchant='', amount=-12.5,
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
        vals = {'name': 'Interest', 'match_type': 'description_regex',
                'match_value': '.*', 'offset_account': 'Interest Income - T',
                'offset_direction': 'auto', 'party_type': 'Supplier',
                'applies_to_company': 'Testing', 'active': True,
                'archived': False, 'priority': 10}
        vals.update(kw)
        rule = CategorizationRule(**vals)
        db.session.add(rule)
        db.session.commit()
        return rule

    def _je_for(self, erp, gje):
        return erp.created['Journal Entry'][gje.erpnext_journal_entry_name]

    def _party_lines(self, doc):
        return [ln for ln in doc['accounts'] if ln.get('party')]


class AutoSupplierForDerivedParties(Base):
    """Fix A — any party source, not just merchant_name, ensures its Supplier."""

    def test_rule_party_name_creates_supplier_and_je_succeeds(self):
        """THE REPORTED BUG. A rule naming "Wells Fargo" on an interest payment
        (no merchant_name) used to 417; now the Supplier is minted first."""
        erp = _PartyRejectingERP(companies=['Testing'])
        rule = self._rule(party_name='Wells Fargo')
        row = self._txn(name='INTRST PYMNT')

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        self.assertEqual(gje.state, 'pending_review')
        self.assertIn('Wells Fargo', erp.created['Supplier'])
        line = self._party_lines(self._je_for(erp, gje))[0]
        self.assertEqual(line['party'], 'Wells Fargo')
        self.assertEqual(line['party_type'], 'Supplier')

    def test_payroll_processor_derived_from_description(self):
        """No merchant_name and no party_name on the rule: the party comes out
        of the run-on ACH description Plaid hands us."""
        erp = _PartyRejectingERP(companies=['Testing'])
        rule = self._rule(name='Payroll', party_name=None)
        row = self._txn(tid='t-gusto',
                        name='ACH Electronic CreditGUSTO PAY 123456',
                        amount=2400.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        self.assertEqual(gje.state, 'pending_review')
        self.assertIn('Gusto', erp.created['Supplier'])
        self.assertEqual(self._party_lines(self._je_for(erp, gje))[0]['party'],
                         'Gusto')

    def test_institution_name_fallback(self):
        """Nothing to go on but the account's own bank — the right counterparty
        for the bank's own postings ("CREDIT CARD 3333 PAYMENT")."""
        erp = _PartyRejectingERP(companies=['Testing'])
        rule = self._rule(name='CC Payment', party_name=None)
        row = self._txn(tid='t-cc', name='CREDIT CARD 3333 PAYMENT', amount=500.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        self.assertEqual(gje.state, 'pending_review')
        self.assertIn('Wells Fargo', erp.created['Supplier'])

    def test_supplier_group_per_source(self):
        """A bank institution files under Financial Institutions, a payroll
        processor under Payroll Providers — both groups auto-provisioned."""
        erp = FakeERPClient(companies=['Testing'])
        erpnext_bank.ensure_supplier(erp, 'Wells Fargo', source='institution')
        erpnext_bank.ensure_supplier(erp, 'Gusto', source='payroll')

        self.assertEqual(erp.created['Supplier']['Wells Fargo']['supplier_group'],
                         'Financial Institutions')
        self.assertEqual(erp.created['Supplier']['Gusto']['supplier_group'],
                         'Payroll Providers')
        self.assertIn('Financial Institutions', erp.created['Supplier Group'])
        self.assertIn('Payroll Providers', erp.created['Supplier Group'])

    def test_supplier_group_create_failure_still_creates_supplier(self):
        """The group is a nicety; failing to make one must not cost us the
        Supplier (which is what the JE actually needs)."""
        erp = FakeERPClient(companies=['Testing'],
                            fail_supplier_group_create=True)

        name = erpnext_bank.ensure_supplier(erp, 'Wells Fargo',
                                            source='institution')

        self.assertEqual(name, 'Wells Fargo')
        self.assertIn('Wells Fargo', erp.created['Supplier'])
        self.assertEqual(erp.created['Supplier']['Wells Fargo']['supplier_group'],
                         'All Supplier Groups')

    def test_ensure_supplier_is_idempotent(self):
        """Re-running never duplicates: the local mirror short-circuits, and a
        cold mirror still reuses the existing ERPNext Supplier by name."""
        erp = FakeERPClient(companies=['Testing'])
        first = erpnext_bank.ensure_supplier(erp, 'Gusto', source='payroll')
        second = erpnext_bank.ensure_supplier(erp, 'Gusto', source='payroll')
        self.assertEqual(first, second)
        self.assertEqual(len(erp.creates_of('Supplier')), 1)
        self.assertEqual(Supplier.query.filter_by(normalized_name='Gusto').count(), 1)

        # Cold mirror, Supplier already in ERPNext → matched, not re-created.
        Supplier.query.delete()
        db.session.commit()
        self.assertEqual(erpnext_bank.ensure_supplier(erp, 'Gusto'), 'Gusto')
        self.assertEqual(len(erp.creates_of('Supplier')), 1)

    def test_operator_party_name_is_not_normalized(self):
        """A literal "GUSTO" the operator typed stays "GUSTO" — normalizing it to
        "Gusto" would mint a duplicate beside the Supplier they already have."""
        erp = FakeERPClient(companies=['Testing'], existing_suppliers={'GUSTO'})
        rule = self._rule(party_name='GUSTO')
        row = self._txn(tid='t-g2', name='GUSTO PAY')

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        self.assertEqual(self._party_lines(self._je_for(erp, gje))[0]['party'],
                         'GUSTO')
        self.assertEqual(erp.creates_of('Supplier'), [])

    def test_unresolvable_supplier_drops_the_party_rather_than_failing(self):
        """A JE with no party beats no JE at all — so a Supplier we genuinely
        can't create must not put its name on the document anyway."""
        erp = _PartyRejectingERP(companies=['Testing'], fail_supplier_create=True)
        rule = self._rule(party_name='Wells Fargo')
        row = self._txn()

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        self.assertEqual(gje.state, 'pending_review')
        self.assertEqual(self._party_lines(self._je_for(erp, gje)), [])

    def test_a_failed_supplier_does_not_discard_the_generated_je_row(self):
        """The Supplier auto-create commits (success) or rolls the session back
        (failure), either of which would clobber a GeneratedJournalEntry staged
        but not yet committed — and a rollback expunges a never-flushed row
        outright. That would post the JE to ERPNext while the local row
        enforcing one-JE-per-transaction silently vanished, so the next "Rerun
        rules" would DOUBLE-POST. The party is therefore resolved before the row
        is staged; this pins that ordering."""
        erp = _PartyRejectingERP(companies=['Testing'], fail_supplier_create=True)
        rule = self._rule(party_name='Wells Fargo')
        row = self._txn()
        rule_id = rule.id

        categorization.generate_journal_entry(erp, row, rule=rule)

        db.session.expunge_all()
        persisted = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='t1').first()
        self.assertIsNotNone(persisted, 'JE row must survive the Supplier failure')
        self.assertEqual(persisted.state, 'pending_review')
        self.assertTrue(persisted.erpnext_journal_entry_name)

        # The surviving row is what makes the re-run idempotent.
        categorization.generate_journal_entry(
            erp, BankTransaction.query.filter_by(plaid_transaction_id='t1').first(),
            rule=db.session.get(CategorizationRule, rule_id))
        self.assertEqual(len(erp.created['Journal Entry']), 1)

    def test_customer_party_is_honoured_but_never_auto_created(self):
        """Minting Customers from bank descriptions isn't a call we should make."""
        erp = FakeERPClient(companies=['Testing'])
        rule = self._rule(party_type='Customer', party_name='Acme Farms')
        row = self._txn(tid='t-cust', amount=-900.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        line = self._party_lines(self._je_for(erp, gje))[0]
        self.assertEqual(line['party'], 'Acme Farms')
        self.assertEqual(erp.creates_of('Supplier'), [])

    def test_derive_prefers_merchant_then_payroll_then_institution(self):
        row = self._txn(tid='t-m', merchant='STARBUCKS 92104',
                        name='SQ *STARBUCKS')
        self.assertEqual(erpnext_bank.derive_party_from_transaction(row),
                         ('Starbucks', 'merchant'))
        row = self._txn(tid='t-p', name='ACH Electronic CreditGUSTO PAY')
        self.assertEqual(erpnext_bank.derive_party_from_transaction(row),
                         ('Gusto', 'payroll'))
        row = self._txn(tid='t-i', name='INTRST PYMNT')
        self.assertEqual(erpnext_bank.derive_party_from_transaction(row),
                         ('Wells Fargo', 'institution'))

    def test_derive_returns_nothing_without_an_institution(self):
        """An unmapped account with a generic description has no party to
        derive — better none than a wrong one."""
        db.session.query(PlaidItem).update({'institution_name': ''})
        db.session.commit()
        row = self._txn(tid='t-none', name='DEPOSIT')
        self.assertEqual(erpnext_bank.derive_party_from_transaction(row), ('', ''))


class SkipParty(Base):
    """Fix B — a rule can omit the Party entirely (transfers)."""

    def test_skip_party_rule_omits_party_from_the_je(self):
        erp = FakeERPClient(companies=['Testing'])
        rule = self._rule(name='CC Payment', party_name='Wells Fargo',
                          skip_party=True)
        row = self._txn(tid='t-skip', name='CREDIT CARD 3333 PAYMENT',
                        amount=500.0)

        gje = categorization.generate_journal_entry(erp, row, rule=rule)

        doc = self._je_for(erp, gje)
        self.assertEqual(gje.state, 'pending_review')
        self.assertEqual(self._party_lines(doc), [])
        for line in doc['accounts']:
            self.assertNotIn('party_type', line)
        self.assertEqual(erp.creates_of('Supplier'), [])

    def test_existing_rules_default_to_skip_party_false(self):
        """Backward compat: a rule saved before v0.4.0.7 keeps naming its
        party exactly as it did."""
        rule = self._rule(party_name='Wells Fargo')
        self.assertFalse(rule.skip_party)
        self.assertIs(rule.to_dict()['skip_party'], False)

        erp = FakeERPClient(companies=['Testing'])
        gje = categorization.generate_journal_entry(erp, self._txn(), rule=rule)
        self.assertEqual(self._party_lines(self._je_for(erp, gje))[0]['party'],
                         'Wells Fargo')

    def test_transfer_heuristic_suggests_skip_party(self):
        """The offset IS another of this Company's bank accounts → transfer."""
        self.assertTrue(categorization.suggest_skip_party('Checking 1111 - T',
                                                          'Testing'))
        # A logical (Mode B) offset name matches the same account logically.
        self.assertTrue(categorization.suggest_skip_party('Checking 1111'))
        # An expense offset is not a transfer.
        self.assertFalse(categorization.suggest_skip_party('Fuel Expense - T',
                                                           'Testing'))
        # Right account name, wrong Company → not a transfer of THIS entity's.
        self.assertFalse(categorization.suggest_skip_party('Checking 1111 - T',
                                                           'Other Co'))
        self.assertFalse(categorization.suggest_skip_party(''))

    def test_suggestion_api_reports_the_heuristic(self):
        r = self.client.get('/api/rules/skip_party_suggestion'
                            '?offset_account=Checking 1111 - T&company=Testing')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['skip_party'])

        r = self.client.get('/api/rules/skip_party_suggestion'
                            '?offset_account=Fuel Expense - T&company=Testing')
        self.assertFalse(r.get_json()['skip_party'])

    def test_rules_editor_saves_the_checkbox(self):
        self.client.post('/admin/rules/save', data={
            'name': 'CC Payment', 'match_type': 'description_regex',
            'match_value': 'CREDIT CARD .* PAYMENT',
            'offset_account': 'Checking 1111 - T', 'offset_direction': 'auto',
            'party_type': 'Supplier', 'active': 'on', 'priority': '10',
            'skip_party': 'on'})
        rule = CategorizationRule.query.filter_by(name='CC Payment').first()
        self.assertIsNotNone(rule)
        self.assertTrue(rule.skip_party)

        # Unchecked → False. The checkbox is authoritative; the heuristic only
        # ever pre-checks it in the editor.
        self.client.post('/admin/rules/save', data={
            'name': 'Fuel', 'match_type': 'merchant_contains',
            'match_value': 'CHEVRON', 'offset_account': 'Fuel Expense - T',
            'offset_direction': 'auto', 'party_type': 'Supplier',
            'active': 'on', 'priority': '20'})
        self.assertFalse(
            CategorizationRule.query.filter_by(name='Fuel').first().skip_party)


class Backfill(Base):
    """The retroactive cleanup for JEs already stuck in `error`."""

    def _stuck(self, tid='t1', party='Wells Fargo'):
        g = GeneratedJournalEntry(
            plaid_transaction_id=tid, rule_id=1, rule_name='Interest',
            state='error', amount=12.5,
            error_message='POST /api/resource/Journal Entry -> 417 · '
                          '{"exception": "LinkValidationError: Could not find '
                          f'Row #1: Party: {party}"}}')
        db.session.add(g)
        db.session.commit()
        return g

    def test_parses_the_party_out_of_the_error(self):
        self.assertEqual(
            backfill.party_name_from_error(
                'LinkValidationError: Could not find Row #1: Party: Wells Fargo'),
            'Wells Fargo')
        self.assertEqual(
            backfill.party_name_from_error('Could not find Party: GUSTO'), 'GUSTO')
        # An unrelated failure is not this script's business.
        self.assertEqual(
            backfill.party_name_from_error('ValidationError: Debit != Credit'), '')

    def test_creates_supplier_and_regenerates_to_pending_review(self):
        erp = _PartyRejectingERP(companies=['Testing'])
        rule = self._rule(party_name='Wells Fargo')
        row = self._txn()
        g = self._stuck(tid=row.plaid_transaction_id)
        g.rule_id = rule.id
        db.session.commit()

        result = backfill.run(erp)

        db.session.refresh(g)
        self.assertIn('Wells Fargo', erp.created['Supplier'])
        self.assertEqual(g.state, 'pending_review')
        self.assertTrue(g.erpnext_journal_entry_name)
        self.assertEqual(result['regenerated'], [row.plaid_transaction_id])

    def test_is_idempotent(self):
        """A second run finds nothing left in `error` and changes nothing."""
        erp = _PartyRejectingERP(companies=['Testing'])
        rule = self._rule(party_name='Wells Fargo')
        row = self._txn()
        g = self._stuck(tid=row.plaid_transaction_id)
        g.rule_id = rule.id
        db.session.commit()
        backfill.run(erp)

        again = backfill.run(erp)

        self.assertEqual(again['considered'], 0)
        self.assertEqual(again['regenerated'], [])
        self.assertEqual(len(erp.creates_of('Supplier')), 1)

    def test_leaves_a_row_whose_transaction_is_gone_in_error(self):
        erp = FakeERPClient(companies=['Testing'])
        g = self._stuck(tid='vanished')

        result = backfill.run(erp)

        db.session.refresh(g)
        self.assertEqual(g.state, 'error')
        self.assertEqual([t for t, _ in result['still_failing']], ['vanished'])


class Regressions(Base):
    """Guards for the two behaviours most at risk from this change."""

    def test_v0405_approve_still_works_with_the_new_auto_supplier(self):
        erp = _PartyRejectingERP(companies=['Testing'])
        rule = self._rule(party_name='Wells Fargo')
        gje = categorization.generate_journal_entry(erp, self._txn(), rule=rule)
        name = gje.erpnext_journal_entry_name
        erp.docs[name] = {'doctype': 'Journal Entry', 'docstatus': 0,
                          'company': 'Testing',
                          'accounts': erp.created['Journal Entry'][name]['accounts']}

        with patch('app.sync_engine.get_erp_client_or_none', return_value=erp):
            r = self.client.post('/admin/generated_entries/approve',
                                 data={'id': str(gje.id)},
                                 follow_redirects=True)

        self.assertEqual(r.status_code, 200)
        db.session.refresh(gje)
        self.assertEqual(gje.state, 'approved')
        self.assertIn(name, erp.submitted)

    def test_v0402_cross_company_block_still_applies(self):
        """The push-time guard must fire BEFORE anything else — a cross-Company
        rule is blocked, and we don't mint a Supplier for a JE we won't post."""
        erp = FakeERPClient(companies=['Testing', 'Other Co'],
                            chart_accounts=[{'account_name': 'Fuel Expense',
                                             'name': 'Fuel Expense - OC',
                                             'company': 'Other Co'}])
        rule = self._rule(name='Cross', offset_account='Fuel Expense - OC',
                          party_name='Wells Fargo')

        gje = categorization.generate_journal_entry(erp, self._txn(), rule=rule)

        self.assertEqual(gje.state, 'blocked')
        self.assertIn('cross-Company', gje.error_message)
        self.assertEqual(erp.creates_of('Journal Entry'), [])


if __name__ == '__main__':
    unittest.main()
