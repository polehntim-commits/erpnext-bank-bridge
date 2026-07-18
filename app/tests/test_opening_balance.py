# SPDX-License-Identifier: MIT
"""Opening balances (v0.4.4).

The gap this closes: Bank Bridge recorded transactions but never what an account
ALREADY HELD when it was linked, so ERPNext reported movement-since-link rather
than the account's position — a Money Market holding $50 showed as -$17,550.

Covered here:

  * the direction rule, which is the whole subtlety — asset vs liability, and
    the negative-balance flip (overdrawn checking / overpaid card) on each
  * Plaid's per-type sign convention (positive = HAVE for depository, positive
    = OWE for credit) survives the round trip into debit/credit lines
  * the Opening Balance Equity leaf is auto-created under the Equity root, once
    per Company, and reused thereafter
  * auto-booking at initial import, and the AUTO_BOOK_OPENING_BALANCE opt-out
  * OPENING_BALANCE_DATE — default today, ISO backdate, garbage falls back
  * idempotency: re-importing never books a second opening balance
  * the backfill script's estimate, its idempotency, and its dry run
  * the manual endpoint's amount / date overrides
  * the approve/reject workflow (v0.4.0.5) drives these entries unchanged
  * multi-Company: the entry books to the account's OWNING Company (v0.4.0)
  * no Party on either leg — an opening balance has no counterparty

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
import unittest.mock
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, crypto, db  # noqa: E402
from app import erpnext_accounts, erpnext_settings  # noqa: E402
from app import opening_balance as obal  # noqa: E402
from app.models import (BankTransaction, GeneratedJournalEntry,  # noqa: E402
                        PlaidAccount, PlaidItem)

from tests.fakes import FakeERPClient  # noqa: E402

COMPANY = 'Example Company LLC'
OTHER_COMPANY = 'Personal Holdings LLC'
EQUITY = 'Opening Balance Equity - EC'


class OpeningBalanceBase(unittest.TestCase):
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
        self.ctx = self.app.app_context()
        self.ctx.push()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', COMPANY)
        self._item()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _item(self, item_id='item-abc', owning_company=None):
        it = PlaidItem(item_id=item_id,
                       access_token_encrypted=crypto.encrypt('access-x'),
                       institution_id='ins_1', institution_name='Wells Fargo',
                       status='active', owning_company=owning_company)
        db.session.add(it)
        db.session.commit()
        return it

    def _account(self, account_id='acct-1', type_='depository',
                 subtype='checking', balance=1000.0, mask='1234',
                 gl='Wells Fargo Checking - EC', item_id='item-abc',
                 owning_company=None):
        a = PlaidAccount(
            account_id=account_id, item_id=item_id,
            name=f'{subtype} {mask}', mask=mask, type=type_, subtype=subtype,
            balance_current=balance, iso_currency_code='USD',
            erpnext_gl_account_name=gl, owning_company=owning_company,
            erpnext_bank_account_name='BA-1' if gl else None,
            import_status='imported' if gl else 'pending')
        db.session.add(a)
        db.session.commit()
        return a

    def _txn(self, transaction_id, account_id='acct-1', amount=0.0,
             pending=False, removed=False):
        t = BankTransaction(plaid_transaction_id=transaction_id,
                            account_id=account_id, amount=amount,
                            date=date(2026, 7, 10), name='TXN',
                            pending=pending, removed=removed)
        db.session.add(t)
        db.session.commit()
        return t

    def _equity_chart(self, extra=None):
        """A chart with an Equity root the Opening Balance Equity leaf can
        anchor under — what stock ERPNext ships."""
        chart = [{'account_name': 'Equity', 'is_group': 1,
                  'root_type': 'Equity', 'parent_account': ''}]
        chart.extend(extra or [])
        return chart

    def _erp(self, **kw):
        kw.setdefault('chart_accounts', self._equity_chart())
        kw.setdefault('companies', [COMPANY, OTHER_COMPANY])
        return FakeERPClient(**kw)

    @staticmethod
    def _je(erp):
        """The single Journal Entry payload the fake was asked to create."""
        creates = erp.creates_of('Journal Entry')
        assert len(creates) == 1, f'expected 1 JE create, got {len(creates)}'
        return creates[0][2]


# ── the direction rule ──────────────────────────────────────────────────────

class TestDirection(OpeningBalanceBase):
    """The four cases the module docstring collapses into one flip."""

    def test_depository_positive_debits_the_bank_account(self):
        # Plaid: positive on a depository account = money you HAVE (an asset).
        a = self._account(type_='depository', subtype='checking', balance=1000)
        self.assertTrue(obal.opening_balance_direction(a, 1000))

    def test_credit_positive_credits_the_liability(self):
        # Plaid: positive on a credit account = money you OWE. The card's GL leaf
        # lives under Current Liabilities, which opens by CREDIT.
        a = self._account(type_='credit', subtype='credit card', balance=2400)
        self.assertFalse(obal.opening_balance_direction(a, 2400))

    def test_overdrawn_depository_flips(self):
        # An overdrawn checking account is a negative asset — book it the other
        # way rather than emitting a negative debit.
        a = self._account(type_='depository', subtype='checking', balance=-120)
        self.assertFalse(obal.opening_balance_direction(a, -120))

    def test_overpaid_credit_card_flips(self):
        # A credit balance in your favour on a card: you're owed, not owing.
        a = self._account(type_='credit', subtype='credit card', balance=-75)
        self.assertTrue(obal.opening_balance_direction(a, -75))

    def test_investment_opens_by_debit(self):
        # Investments live under Non-current Assets → also an asset.
        a = self._account(type_='investment', subtype='401k', balance=50000)
        self.assertTrue(obal.opens_by_debit(a))

    def test_direction_follows_the_gl_side_it_was_filed_under(self):
        # The rule delegates to _gl_side, so it can't drift from the group the
        # leaf was actually created under.
        for type_, subtype, expect_debit in (
                ('depository', 'checking', True),
                ('depository', 'money market', True),
                ('depository', 'savings', True),
                ('credit', 'credit card', False),
                ('credit', 'line of credit', False),
                ('investment', 'brokerage', True)):
            a = self._account(account_id=f'{type_}-{subtype}', type_=type_,
                              subtype=subtype, balance=100)
            self.assertEqual(obal.opens_by_debit(a), expect_debit,
                             f'{type_}/{subtype}')


class TestDocumentShape(OpeningBalanceBase):
    def test_asset_document_debits_bank_credits_equity(self):
        a = self._account(balance=17600)
        doc = obal.build_opening_balance_document(
            a, 'WF Money Market - EC', EQUITY, COMPANY, 17600,
            date(2026, 1, 1))
        self.assertEqual(doc['accounts'][0]['account'], 'WF Money Market - EC')
        self.assertEqual(doc['accounts'][0]['debit_in_account_currency'], 17600)
        self.assertEqual(doc['accounts'][1]['account'], EQUITY)
        self.assertEqual(doc['accounts'][1]['credit_in_account_currency'], 17600)
        self.assertEqual(doc['posting_date'], '2026-01-01')
        self.assertEqual(doc['company'], COMPANY)

    def test_liability_document_debits_equity_credits_the_card(self):
        a = self._account(type_='credit', subtype='credit card', balance=2400)
        doc = obal.build_opening_balance_document(
            a, 'Amex - EC', EQUITY, COMPANY, 2400, date(2026, 1, 1))
        self.assertEqual(doc['accounts'][0]['account'], EQUITY)
        self.assertEqual(doc['accounts'][0]['debit_in_account_currency'], 2400)
        self.assertEqual(doc['accounts'][1]['account'], 'Amex - EC')
        self.assertEqual(doc['accounts'][1]['credit_in_account_currency'], 2400)

    def test_amount_is_always_positive_the_sign_is_the_direction(self):
        a = self._account(balance=-120)
        doc = obal.build_opening_balance_document(
            a, 'Checking - EC', EQUITY, COMPANY, -120, date(2026, 1, 1))
        # Overdrawn: equity is debited, the bank account credited — and BOTH
        # numbers are positive, which is what ERPNext accepts.
        self.assertEqual(doc['accounts'][0]['account'], EQUITY)
        self.assertEqual(doc['accounts'][0]['debit_in_account_currency'], 120)
        self.assertEqual(doc['accounts'][1]['credit_in_account_currency'], 120)

    def test_no_party_on_either_leg(self):
        # An opening balance is an equity event — there is no counterparty.
        a = self._account(balance=1000)
        doc = obal.build_opening_balance_document(
            a, 'Checking - EC', EQUITY, COMPANY, 1000, date(2026, 1, 1))
        for line in doc['accounts']:
            self.assertNotIn('party_type', line)
            self.assertNotIn('party', line)

    def test_description_names_the_account(self):
        a = self._account(balance=1000)
        doc = obal.build_opening_balance_document(
            a, 'Checking - EC', EQUITY, COMPANY, 1000, date(2026, 1, 1))
        self.assertEqual(doc['user_remark'],
                         f'Opening Balance for {a.name} at initial link')


# ── the equity account ──────────────────────────────────────────────────────

class TestEquityAccount(OpeningBalanceBase):
    def test_auto_created_under_the_equity_root(self):
        erp = self._erp()
        name = obal.ensure_opening_balance_equity_account(erp, COMPANY)
        self.assertEqual(name, EQUITY)
        created = erp.creates_of('Account')[0][2]
        self.assertEqual(created['account_name'], 'Opening Balance Equity')
        self.assertEqual(created['parent_account'], 'Equity - EC')
        self.assertEqual(created['account_type'], 'Equity')
        self.assertEqual(created['is_group'], 0)

    def test_existing_account_is_reused_not_duplicated(self):
        erp = self._erp(chart_accounts=self._equity_chart([
            {'account_name': 'Opening Balance Equity', 'is_group': 0,
             'root_type': 'Equity', 'parent_account': 'Equity'}]))
        name = obal.ensure_opening_balance_equity_account(erp, COMPANY)
        self.assertEqual(name, EQUITY)
        self.assertEqual(erp.creates_of('Account'), [])

    def test_second_account_shares_the_one_equity_leaf(self):
        erp = self._erp()
        self._account('acct-1', balance=100)
        b = self._account('acct-2', balance=200, gl='Savings - EC')
        obal.book_opening_balance(erp, PlaidAccount.query.filter_by(
            account_id='acct-1').first())
        obal.book_opening_balance(erp, b)
        equity_creates = [c for c in erp.creates_of('Account')
                          if c[2]['account_name'] == 'Opening Balance Equity']
        self.assertEqual(len(equity_creates), 1)

    def test_no_equity_branch_skips_rather_than_inventing_a_root(self):
        # Creating a ROOT account is a chart-of-accounts decision Bank Bridge has
        # no business making — report it instead.
        erp = self._erp(chart_accounts=[])
        a = self._account(balance=1000)
        result = obal.book_opening_balance(erp, a)
        self.assertEqual(result['status'], 'skipped')
        self.assertIn('Equity branch', result['message'])
        self.assertEqual(erp.creates_of('Journal Entry'), [])

    def test_reserved_3000_number_on_a_numbered_chart(self):
        erp = self._erp(chart_accounts=[
            {'account_name': 'Equity', 'is_group': 1, 'root_type': 'Equity',
             'parent_account': '', 'account_number': '3000'}])
        obal.ensure_opening_balance_equity_account(erp, COMPANY)
        created = erp.creates_of('Account')[0][2]
        # 3000 is taken by the root, so the reserved slot is bumped past it.
        self.assertEqual(created['account_number'], '3001')

    def test_unnumbered_chart_stays_unnumbered(self):
        erp = self._erp()
        obal.ensure_opening_balance_equity_account(erp, COMPANY)
        self.assertNotIn('account_number', erp.creates_of('Account')[0][2])

    def test_configurable_account_name(self):
        self.app.config['OPENING_BALANCE_EQUITY_ACCOUNT_NAME'] = 'Owner Capital'
        erp = self._erp()
        obal.ensure_opening_balance_equity_account(erp, COMPANY)
        self.assertEqual(erp.creates_of('Account')[0][2]['account_name'],
                         'Owner Capital')


# ── booking ─────────────────────────────────────────────────────────────────

class TestBooking(OpeningBalanceBase):
    def test_books_a_pending_review_draft(self):
        erp = self._erp()
        a = self._account(balance=17600)
        result = obal.book_opening_balance(erp, a)
        self.assertEqual(result['status'], 'booked')
        entry = obal.existing_entry(a)
        self.assertEqual(entry.state, 'pending_review')
        self.assertEqual(entry.amount, 17600.0)
        self.assertEqual(entry.rule_name, 'Opening balance')
        self.assertIsNone(entry.rule_id)
        self.assertEqual(a.opening_balance_je_id, entry.id)

    def test_synthetic_key_makes_it_identifiable(self):
        erp = self._erp()
        a = self._account(balance=1000)
        obal.book_opening_balance(erp, a)
        entry = obal.existing_entry(a)
        self.assertEqual(entry.plaid_transaction_id, 'opening-balance:acct-1')
        self.assertTrue(obal.is_opening_balance_entry(entry))

    def test_zero_balance_books_nothing(self):
        erp = self._erp()
        a = self._account(balance=0.0)
        result = obal.book_opening_balance(erp, a)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(erp.creates_of('Journal Entry'), [])
        self.assertIsNone(obal.existing_entry(a))

    def test_unmapped_account_books_nothing(self):
        erp = self._erp()
        a = self._account(balance=1000, gl=None)
        result = obal.book_opening_balance(erp, a)
        self.assertEqual(result['status'], 'skipped')
        self.assertIn('GL Account', result['message'])

    def test_second_call_is_a_no_op(self):
        erp = self._erp()
        a = self._account(balance=1000)
        obal.book_opening_balance(erp, a)
        result = obal.book_opening_balance(erp, a)
        self.assertEqual(result['status'], 'skipped')
        self.assertIn('already booked', result['message'])
        self.assertEqual(len(erp.creates_of('Journal Entry')), 1)
        self.assertEqual(GeneratedJournalEntry.query.count(), 1)

    def test_erpnext_refusal_records_an_error_row_and_never_raises(self):
        erp = self._erp(fail_je_create=True)
        a = self._account(balance=1000)
        result = obal.book_opening_balance(erp, a)
        self.assertEqual(result['status'], 'error')
        entry = obal.existing_entry(a)
        self.assertEqual(entry.state, 'error')
        self.assertTrue(entry.error_message)

    def test_books_to_the_owning_company_not_the_default(self):
        self._item('item-2', owning_company=OTHER_COMPANY)
        erp = self._erp()
        a = self._account('acct-2', balance=500, item_id='item-2',
                          gl='Savings - PH')
        obal.book_opening_balance(erp, a)
        self.assertEqual(self._je(erp)['company'], OTHER_COMPANY)

    def test_per_account_company_override_wins(self):
        erp = self._erp()
        a = self._account(balance=500, owning_company=OTHER_COMPANY)
        obal.book_opening_balance(erp, a)
        self.assertEqual(self._je(erp)['company'], OTHER_COMPANY)

    def test_rejected_entry_is_not_silently_rebooked(self):
        erp = self._erp()
        a = self._account(balance=1000)
        obal.book_opening_balance(erp, a)
        entry = obal.existing_entry(a)
        entry.state = 'rejected'
        db.session.commit()
        result = obal.book_opening_balance(erp, a)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(len(erp.creates_of('Journal Entry')), 1)

    def test_force_replaces_a_rejected_entry_on_the_same_row(self):
        erp = self._erp()
        a = self._account(balance=1000)
        obal.book_opening_balance(erp, a)
        entry = obal.existing_entry(a)
        entry.state = 'rejected'
        db.session.commit()
        result = obal.book_opening_balance(erp, a, amount=2500, force=True)
        self.assertEqual(result['status'], 'booked')
        # Re-pointed, not duplicated — the synthetic key is UNIQUE.
        self.assertEqual(GeneratedJournalEntry.query.count(), 1)
        self.assertEqual(obal.existing_entry(a).amount, 2500.0)


class TestStatus(OpeningBalanceBase):
    def test_status_transitions(self):
        erp = self._erp()
        a = self._account(balance=1000)
        self.assertEqual(obal.opening_balance_status(a), 'none')
        obal.book_opening_balance(erp, a)
        self.assertEqual(obal.opening_balance_status(a), 'pending')
        entry = obal.existing_entry(a)
        entry.state = 'approved'
        db.session.commit()
        self.assertEqual(obal.opening_balance_status(a), 'booked')
        entry.state = 'rejected'
        db.session.commit()
        self.assertEqual(obal.opening_balance_status(a), 'rejected')


# ── configuration ───────────────────────────────────────────────────────────

class TestConfiguration(OpeningBalanceBase):
    def test_posting_date_defaults_to_today(self):
        erp = self._erp()
        a = self._account(balance=1000)
        obal.book_opening_balance(erp, a)
        self.assertEqual(self._je(erp)['posting_date'],
                         date.today().isoformat())

    def test_opening_balance_date_backdates(self):
        self.app.config['OPENING_BALANCE_DATE'] = '2026-01-01'
        erp = self._erp()
        a = self._account(balance=1000)
        obal.book_opening_balance(erp, a)
        self.assertEqual(self._je(erp)['posting_date'], '2026-01-01')

    def test_literal_today_is_accepted(self):
        self.app.config['OPENING_BALANCE_DATE'] = 'today'
        self.assertEqual(obal.opening_balance_date(), date.today())

    def test_garbage_date_falls_back_to_today_rather_than_failing(self):
        # A typo in an env var must not block linking a bank.
        self.app.config['OPENING_BALANCE_DATE'] = 'first of January'
        self.assertEqual(obal.opening_balance_date(), date.today())

    def test_auto_book_opt_out(self):
        self.app.config['AUTO_BOOK_OPENING_BALANCE'] = False
        erp = self._erp()
        a = self._account(balance=1000)
        self.assertIsNone(obal.book_if_enabled(erp, a))
        self.assertEqual(erp.creates_of('Journal Entry'), [])

    def test_auto_book_on_by_default(self):
        erp = self._erp()
        a = self._account(balance=1000)
        self.assertEqual(obal.book_if_enabled(erp, a)['status'], 'booked')


# ── the import path ─────────────────────────────────────────────────────────

class TestImportHook(OpeningBalanceBase):
    def _importable(self, balance=17600.0, type_='depository',
                    subtype='money market'):
        a = PlaidAccount(account_id='acct-imp', item_id='item-abc',
                         name='Money Market 9999', mask='9999', type=type_,
                         subtype=subtype, balance_current=balance,
                         iso_currency_code='USD', import_status='pending')
        db.session.add(a)
        db.session.commit()
        return a

    def _full_chart(self):
        return self._equity_chart([
            {'account_name': 'Bank Accounts', 'is_group': 1,
             'root_type': 'Asset', 'parent_account': 'Current Assets'},
            {'account_name': 'Current Assets', 'is_group': 1,
             'root_type': 'Asset', 'parent_account': ''},
            {'account_name': 'Credit Cards', 'is_group': 1,
             'root_type': 'Liability', 'parent_account': 'Current Liabilities'},
            {'account_name': 'Current Liabilities', 'is_group': 1,
             'root_type': 'Liability', 'parent_account': ''},
        ])

    def test_import_books_the_opening_balance(self):
        erp = self._erp(chart_accounts=self._full_chart())
        self._importable()
        result = erpnext_accounts.import_plaid_account_to_erpnext(
            'acct-imp', client=erp)
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['opening_balance']['status'], 'booked')
        a = PlaidAccount.query.filter_by(account_id='acct-imp').first()
        self.assertEqual(obal.opening_balance_status(a), 'pending')
        self.assertIn('Opening balance', result['message'])

    def test_reimport_does_not_book_a_second_opening_balance(self):
        erp = self._erp(chart_accounts=self._full_chart())
        self._importable()
        erpnext_accounts.import_plaid_account_to_erpnext('acct-imp', client=erp)
        before = len(erp.creates_of('Journal Entry'))
        # Second call short-circuits on 'already mapped'.
        result = erpnext_accounts.import_plaid_account_to_erpnext(
            'acct-imp', client=erp)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(len(erp.creates_of('Journal Entry')), before)
        self.assertEqual(GeneratedJournalEntry.query.count(), 1)

    def test_credit_card_import_books_the_liability_side(self):
        erp = self._erp(chart_accounts=self._full_chart())
        self._importable(balance=2400.0, type_='credit', subtype='credit card')
        erpnext_accounts.import_plaid_account_to_erpnext('acct-imp', client=erp)
        je = self._je(erp)
        # Positive Plaid balance on a card = owed → Dr Equity, Cr the card.
        self.assertEqual(je['accounts'][0]['account'], EQUITY)
        self.assertEqual(je['accounts'][0]['debit_in_account_currency'], 2400.0)

    def test_import_survives_a_chart_with_no_equity_branch(self):
        # The opening balance is best-effort: losing it must not unwind the
        # import that just linked the operator's bank.
        chart = [c for c in self._full_chart() if c['account_name'] != 'Equity']
        erp = self._erp(chart_accounts=chart)
        self._importable()
        result = erpnext_accounts.import_plaid_account_to_erpnext(
            'acct-imp', client=erp)
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['opening_balance']['status'], 'skipped')
        a = PlaidAccount.query.filter_by(account_id='acct-imp').first()
        self.assertTrue(a.erpnext_bank_account_name)

    def test_bulk_import_counts_opening_balances(self):
        erp = self._erp(chart_accounts=self._full_chart())
        self._importable()
        stats = erpnext_accounts.import_all_supported_accounts(client=erp)
        self.assertEqual(stats['opening_balances'], 1)
        self.assertIn('opening balance', stats['summary'])


# ── the estimate + backfill script ──────────────────────────────────────────

class TestEstimate(OpeningBalanceBase):
    def test_depository_estimate_adds_back_net_outflow(self):
        # Tim's Money Market: holds $50 now, $17,550 has flowed OUT since link
        # (Plaid amounts are positive for money out) → it opened at $17,600.
        a = self._account(balance=50.0)
        self._txn('t1', amount=17550.0)
        self.assertEqual(obal.estimate_opening_balance(a), 17600.0)

    def test_depository_estimate_subtracts_net_inflow(self):
        a = self._account(balance=1200.0)
        self._txn('t1', amount=-200.0)      # a deposit
        self.assertEqual(obal.estimate_opening_balance(a), 1000.0)

    def test_credit_estimate_flips_the_sign(self):
        # A card owing $2,400 now, after $400 of purchases, opened owing $2,000.
        a = self._account(type_='credit', subtype='credit card', balance=2400.0)
        self._txn('t1', amount=400.0)
        self.assertEqual(obal.estimate_opening_balance(a), 2000.0)

    def test_pending_and_removed_transactions_are_excluded(self):
        a = self._account(balance=50.0)
        self._txn('t1', amount=17550.0)
        self._txn('t2', amount=999.0, pending=True)
        self._txn('t3', amount=888.0, removed=True)
        self.assertEqual(obal.estimate_opening_balance(a), 17600.0)

    def test_no_transactions_means_the_current_balance_is_the_opening_one(self):
        a = self._account(balance=1000.0)
        self.assertEqual(obal.estimate_opening_balance(a), 1000.0)


class TestBackfillScript(OpeningBalanceBase):
    def _run(self, erp, dry_run=False):
        from scripts import backfill_opening_balances as script
        return script.run(erp, dry_run=dry_run)

    def test_books_the_estimated_opening_balance(self):
        erp = self._erp()
        self._account(balance=50.0)
        self._txn('t1', amount=17550.0)
        result = self._run(erp)
        self.assertEqual(len(result['booked']), 1)
        self.assertEqual(result['booked'][0][1], 17600.0)
        self.assertEqual(self._je(erp)['accounts'][0]
                         ['debit_in_account_currency'], 17600.0)

    def test_dry_run_creates_nothing(self):
        erp = self._erp()
        self._account(balance=50.0)
        self._txn('t1', amount=17550.0)
        result = self._run(erp, dry_run=True)
        self.assertEqual(result['considered'], 1)
        self.assertEqual(result['booked'], [])
        self.assertEqual(erp.creates_of('Journal Entry'), [])
        self.assertEqual(GeneratedJournalEntry.query.count(), 0)
        # What --dry-run printed is what a real run would book.
        self.assertEqual(result['plans'][0]['estimate'], 17600.0)

    def test_is_idempotent(self):
        erp = self._erp()
        self._account(balance=50.0)
        self._txn('t1', amount=17550.0)
        self._run(erp)
        second = self._run(erp)
        self.assertEqual(second['booked'], [])
        self.assertEqual(second['considered'], 0)
        self.assertEqual(len(erp.creates_of('Journal Entry')), 1)

    def test_skips_accounts_that_already_have_one(self):
        erp = self._erp()
        a = self._account(balance=1000.0)
        obal.book_opening_balance(erp, a)
        result = self._run(erp)
        self.assertEqual(result['booked'], [])
        self.assertEqual(len(result['skipped']), 1)
        self.assertIn('already has', result['skipped'][0][1])

    def test_does_not_overturn_a_rejection(self):
        # Rejecting an opening balance is a decision; re-running must respect it.
        erp = self._erp()
        a = self._account(balance=1000.0)
        obal.book_opening_balance(erp, a)
        obal.existing_entry(a).state = 'rejected'
        db.session.commit()
        result = self._run(erp)
        self.assertEqual(result['booked'], [])
        self.assertEqual(len(erp.creates_of('Journal Entry')), 1)

    def test_skips_unimported_accounts(self):
        erp = self._erp()
        self._account(balance=1000.0, gl=None)
        result = self._run(erp)
        self.assertEqual(result['considered'], 0)
        self.assertIn('no GL Account', result['skipped'][0][1])

    def test_zero_estimate_books_nothing(self):
        erp = self._erp()
        self._account(balance=1000.0)
        self._txn('t1', amount=-1000.0)   # every dollar arrived after link
        result = self._run(erp)
        self.assertEqual(result['booked'], [])
        self.assertEqual(erp.creates_of('Journal Entry'), [])


# ── the admin UI ────────────────────────────────────────────────────────────

class TestOpeningBalanceUI(OpeningBalanceBase):
    def setUp(self):
        super().setUp()
        self.client = self.app.test_client()

    def test_accounts_page_shows_not_booked(self):
        self._account(balance=1000.0)
        html = self.client.get('/admin/accounts').get_data(as_text=True)
        self.assertIn('Opening Balance', html)
        self.assertIn('not booked', html)

    def test_accounts_page_shows_pending_with_the_amount(self):
        erp = self._erp()
        a = self._account(balance=17600.0)
        obal.book_opening_balance(erp, a)
        html = self.client.get('/admin/accounts').get_data(as_text=True)
        self.assertIn('pending review', html)
        self.assertIn('17,600.00', html)

    def test_accounts_page_shows_booked_once_approved(self):
        erp = self._erp()
        a = self._account(balance=1000.0)
        obal.book_opening_balance(erp, a)
        obal.existing_entry(a).state = 'approved'
        db.session.commit()
        html = self.client.get('/admin/accounts').get_data(as_text=True)
        self.assertIn('>booked', html)

    def test_generated_entries_page_badges_opening_balances(self):
        erp = self._erp()
        a = self._account(balance=1000.0)
        obal.book_opening_balance(erp, a)
        html = self.client.get(
            '/admin/generated_entries').get_data(as_text=True)
        self.assertIn('opening balance', html)

    def test_manual_endpoint_books_with_the_plaid_balance(self):
        erp = self._erp()
        a = self._account(balance=1000.0)
        with unittest.mock.patch(
                'app.sync_engine.get_erp_client_or_none', return_value=erp):
            resp = self.client.post(
                '/api/accounts/acct-1/book_opening_balance', data={})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(obal.existing_entry(a).amount, 1000.0)

    def test_manual_endpoint_accepts_a_custom_amount_and_date(self):
        erp = self._erp()
        a = self._account(balance=1000.0)
        with unittest.mock.patch(
                'app.sync_engine.get_erp_client_or_none', return_value=erp):
            self.client.post('/api/accounts/acct-1/book_opening_balance',
                             data={'amount': '2,500.75',
                                   'posting_date': '2026-01-01'})
        self.assertEqual(obal.existing_entry(a).amount, 2500.75)
        self.assertEqual(self._je(erp)['posting_date'], '2026-01-01')

    def test_manual_endpoint_rejects_a_non_numeric_amount(self):
        self._account(balance=1000.0)
        resp = self.client.post('/api/accounts/acct-1/book_opening_balance',
                                data={'amount': 'lots'})
        self.assertIn('not+a+number', resp.headers['Location'])

    def test_manual_endpoint_can_replace_a_rejected_entry(self):
        erp = self._erp()
        a = self._account(balance=1000.0)
        obal.book_opening_balance(erp, a)
        obal.existing_entry(a).state = 'rejected'
        db.session.commit()
        with unittest.mock.patch(
                'app.sync_engine.get_erp_client_or_none', return_value=erp):
            self.client.post('/api/accounts/acct-1/book_opening_balance',
                             data={'amount': '900'})
        self.assertEqual(obal.existing_entry(a).state, 'pending_review')
        self.assertEqual(obal.existing_entry(a).amount, 900.0)


# ── the v0.4.0.5 approve/reject workflow drives these unchanged ─────────────

class TestApprovalWorkflow(OpeningBalanceBase):
    def setUp(self):
        super().setUp()
        self.client = self.app.test_client()

    def _booked(self):
        erp = self._erp()
        a = self._account(balance=17600.0)
        obal.book_opening_balance(erp, a)
        return erp, a, obal.existing_entry(a)

    def test_approve_submits_the_opening_balance_je(self):
        erp, a, entry = self._booked()
        with unittest.mock.patch(
                'app.sync_engine.get_erp_client_or_none', return_value=erp):
            self.client.post('/admin/generated_entries/approve',
                             data={'id': entry.id})
        self.assertEqual(obal.existing_entry(a).state, 'approved')
        self.assertIn(entry.erpnext_journal_entry_name, erp.submitted)

    def test_reject_abandons_the_draft(self):
        erp, a, entry = self._booked()
        with unittest.mock.patch(
                'app.sync_engine.get_erp_client_or_none', return_value=erp):
            self.client.post('/admin/generated_entries/reject',
                             data={'id': entry.id})
        self.assertEqual(obal.existing_entry(a).state, 'rejected')
        self.assertEqual(erp.submitted, set())


# ── regression: nothing about the pre-v0.4.4 flows changed ─────────────────

class TestBackwardCompatibility(OpeningBalanceBase):
    def test_existing_accounts_report_no_opening_balance(self):
        # A pre-v0.4.4 row has opening_balance_je_id NULL and no entry — which
        # must read as 'none', not as an error.
        a = self._account(balance=1000.0)
        self.assertIsNone(a.opening_balance_je_id)
        self.assertEqual(obal.opening_balance_status(a), 'none')

    def test_rules_engine_entries_are_not_mistaken_for_opening_balances(self):
        g = GeneratedJournalEntry(plaid_transaction_id='txn-real',
                                  state='pending_review', amount=25.0,
                                  rule_name='Coffee')
        db.session.add(g)
        db.session.commit()
        self.assertFalse(obal.is_opening_balance_entry(g))

    def test_opening_balance_column_is_in_to_dict(self):
        a = self._account(balance=1000.0)
        self.assertIn('opening_balance_je_id', a.to_dict())


if __name__ == '__main__':
    unittest.main()
