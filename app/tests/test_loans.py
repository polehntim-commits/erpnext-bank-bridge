# SPDX-License-Identifier: MIT
"""Loans as liabilities (v0.4.14).

THE GAP. `is_supported` refused every loan account, so a farm with a $200,000
orchard mortgage had a balance sheet overstating its net worth by $200,000 and
payments leaving the chequing account with nowhere honest to go. The natural
workaround is wrong twice over: categorizing the whole payment as an expense
overstates expenses by the principal AND still leaves the debt off the books.

THE SHAPE. The textbook entry is three lines (Dr Loan / Dr Interest / Cr Bank)
and nothing in this codebase can emit three lines — and a split stored on a rule
would be wrong from month two, because the ratio changes every payment. So it
decomposes into two entries that are each independently true:

    payment   Dr Loan Liability    Cr Bank             (an ordinary rule)
    accrual   Dr Interest Expense  Cr Loan Liability   (generated here)

Covered here:

  * classification, including the two cases that flip signs or split hairs: a
    mortgage misfiled by the institution as type='depository' (which would
    otherwise book six figures of debt as a chequing ASSET), and 'line of
    credit' living under both the credit and loan types
  * loans are BALANCE-ONLY, and are excluded from the push — posting a loan's
    own transactions would double-count every payment, since the money leaving
    chequing is already mirrored on the chequing side
  * interest accrual from the lender's own year-to-date figures, never an
    estimate, including the year rollover that would otherwise post a large
    NEGATIVE accrual every January
  * the seeding rule: first sight posts nothing, so an upgrade cannot book a
    year of interest as a single entry
  * graceful degradation when the `liabilities` product isn't approved — the
    loan still imports and its balance is still tracked
  * the link-token product ladder, so one unapproved product doesn't cost the
    other

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
import unittest.mock
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, crypto, db, loans, sync_engine  # noqa: E402
from app import erpnext_accounts as ea  # noqa: E402
from app.models import (BankTransaction, GeneratedJournalEntry,  # noqa: E402
                        PlaidAccount, PlaidItem)
from app.plaid_client import PlaidClient  # noqa: E402

from tests.fakes import FakeERPClient, FakePlaidClient  # noqa: E402

COMPANY = 'Testing'


def liability(account_id='loan-1', kind='mortgage', rate=6.125,
              ytd_interest=None, ytd_principal=None, **extra):
    """The normalized shape plaid_client._normalize_liability produces."""
    row = {
        'account_id': account_id, 'liability_type': kind,
        'interest_rate': rate,
        'ytd_interest_paid': ytd_interest,
        'ytd_principal_paid': ytd_principal,
        'next_payment_due_date': '2026-08-01',
        'last_payment_amount': 2000.0, 'last_payment_date': '2026-07-01',
        'origination_principal_amount': 250000.0,
        'origination_date': '2019-04-01', 'maturity_date': '2049-04-01',
        'minimum_payment_amount': 2000.0, 'raw': {},
    }
    row.update(extra)
    return row


class LoanBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': tempfile.mkdtemp(),
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.session.add(PlaidItem(
            item_id='item-abc', access_token_encrypted=crypto.encrypt('t'),
            institution_id='ins_1', institution_name='Farm Credit',
            status='active', owning_company=COMPANY))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _loan(self, account_id='loan-1', subtype='mortgage', balance=200000.0,
              gl='Orchard Mortgage - TEST', detail=None, ytd_booked=None):
        a = PlaidAccount(
            account_id=account_id, item_id='item-abc', name='Orchard Mortgage',
            mask='7788', type='loan', subtype=subtype,
            balance_current=balance, iso_currency_code='USD',
            erpnext_bank_account_name='BA-LOAN' if gl else None,
            erpnext_gl_account_name=gl, owning_company=COMPANY,
            balance_only=True, import_status='imported' if gl else 'pending',
            loan_ytd_interest_booked=ytd_booked)
        db.session.add(a)
        db.session.commit()
        if detail is not None:
            loans.store_detail(a, detail)
            db.session.commit()
        return a

    def _erp(self):
        return FakeERPClient(chart_accounts=[
            {'account_name': 'Expenses', 'name': 'Expenses - TEST',
             'is_group': 1, 'root_type': 'Expense', 'parent_account': '',
             'company': COMPANY},
        ])


# ── classification ──────────────────────────────────────────────────────────

class ClassificationTests(LoanBase):
    def _acct(self, type_, subtype):
        return PlaidAccount(account_id='x', item_id='item-abc', type=type_,
                            subtype=subtype)

    def test_loans_are_supported_now(self):
        """The gap: every one of these used to be refused outright."""
        for subtype in ('mortgage', 'student', 'auto', 'home equity'):
            with self.subTest(subtype=subtype):
                self.assertTrue(ea.is_supported(self._acct('loan', subtype)))

    def test_loans_book_on_the_liability_side(self):
        account = self._acct('loan', 'mortgage')
        self.assertEqual(ea._gl_side(account), 'loan')
        self.assertEqual(ea.erpnext_account_type(account), 'Loan')
        self.assertEqual(ea.erpnext_account_subtype(account), 'Mortgage')

    def test_a_misfiled_mortgage_is_still_a_liability(self):
        """An institution can serve a mortgage from its deposit platform, so it
        arrives as type='depository'. Trusting the type would book six figures
        of debt as a chequing ASSET — a sign flip on the largest number on the
        balance sheet."""
        account = self._acct('depository', 'mortgage')
        self.assertTrue(ea.is_loan(account))
        self.assertEqual(ea._gl_side(account), 'loan')
        self.assertEqual(ea.erpnext_account_type(account), 'Loan')

    def test_line_of_credit_follows_its_type(self):
        """It exists under both types and means different things: a revolving
        card-like line, or a term facility."""
        term = self._acct('loan', 'line of credit')
        revolving = self._acct('credit', 'line of credit')
        self.assertEqual(ea._gl_side(term), 'loan')
        self.assertEqual(ea._gl_side(revolving), 'credit')
        self.assertEqual(ea.erpnext_account_type(term), 'Loan')
        self.assertEqual(ea.erpnext_account_type(revolving), 'Credit')

    def test_the_type_and_the_gl_side_never_disagree(self):
        """These are two functions deriving the same fact; a disagreement puts
        a Loan-typed Bank Account under Credit Cards."""
        for type_, subtype in (('loan', 'mortgage'), ('loan', 'line of credit'),
                               ('credit', 'line of credit'),
                               ('depository', 'mortgage'),
                               ('credit', 'credit card'),
                               ('depository', 'checking')):
            with self.subTest(type=type_, subtype=subtype):
                account = self._acct(type_, subtype)
                side = ea._gl_side(account)
                bank_type = ea.erpnext_account_type(account)
                self.assertEqual(side == 'loan', bank_type == 'Loan')

    def test_other_is_still_unsupported(self):
        """The one type with no honest GL home — Plaid could not classify it."""
        self.assertFalse(ea.is_supported(self._acct('other', '')))

    def test_opening_balance_credits_a_loan(self):
        """A loan opens as something you OWE. opens_by_debit already knew how
        to handle 'loan'; it was simply never reachable."""
        from app import opening_balance as obal
        self.assertFalse(obal.opens_by_debit(self._acct('loan', 'mortgage')))
        self.assertTrue(obal.opens_by_debit(self._acct('depository', 'checking')))

    def test_every_loan_subtype_has_a_provisioned_master(self):
        for value in ea._SUBTYPE_MAP.values():
            self.assertIn(value, ea.DEFAULT_ACCOUNT_SUBTYPES, value)
        self.assertIn('Loan', ea.DEFAULT_BANK_ACCOUNT_TYPES)


# ── balance-only, and the double-count guard ────────────────────────────────

class BalanceOnlyTests(LoanBase):
    def test_a_refreshed_loan_is_marked_balance_only(self):
        item = PlaidItem.query.one()
        fake = FakePlaidClient(accounts=[{
            'account_id': 'loan-1', 'name': 'Mortgage', 'official_name': '',
            'mask': '7788', 'type': 'loan', 'subtype': 'mortgage',
            'balance_available': None, 'balance_current': 200000.0,
            'iso_currency_code': 'USD'}])
        sync_engine.refresh_accounts(item, fake, 't')
        self.assertTrue(PlaidAccount.query.filter_by(
            account_id='loan-1').one().balance_only)

    def test_loan_transactions_are_never_posted(self):
        """THE double-count guard. The payment leaving chequing is already
        mirrored on the chequing side, and that is where it is booked from.
        Posting the loan's own copy would book every payment twice."""
        self._loan()
        db.session.add(BankTransaction(
            plaid_transaction_id='mtg-1', account_id='loan-1', amount=2000.0,
            date=date(2026, 7, 1), name='PAYMENT'))
        db.session.commit()

        erp = FakeERPClient()
        stats = sync_engine.push_pending(erp)
        self.assertEqual(stats['posted'], 0)
        self.assertEqual(erp.creates_of('Bank Transaction'), [])

    def test_loan_transactions_are_not_reported_as_waiting(self):
        """v0.4.13 counts rows waiting on the operator. A loan's rows are not
        waiting on anything — telling them 40 mortgage rows are stuck would send
        them hunting a problem that doesn't exist."""
        self._loan()
        for i in range(4):
            db.session.add(BankTransaction(
                plaid_transaction_id=f'mtg-{i}', account_id='loan-1',
                amount=2000.0, date=date(2026, 7, 1), name='PAYMENT'))
        db.session.commit()
        self.assertEqual(sync_engine.unpostable_pending_count(), 0)
        self.assertEqual(sync_engine.unpostable_by_account(), {})

    def test_a_chequing_account_still_posts_normally(self):
        db.session.add(PlaidAccount(
            account_id='chk', item_id='item-abc', name='Checking', mask='1234',
            type='depository', subtype='checking',
            erpnext_bank_account_name='BA-chk', import_status='imported'))
        db.session.commit()
        db.session.add(BankTransaction(
            plaid_transaction_id='c-1', account_id='chk', amount=2000.0,
            date=date(2026, 7, 1), name='MORTGAGE PAYMENT'))
        db.session.commit()
        self.assertEqual(sync_engine.push_pending(FakeERPClient())['posted'], 1)


# ── liability detail ────────────────────────────────────────────────────────

class LiabilityDetailTests(LoanBase):
    def test_detail_is_stored_and_read_back(self):
        account = self._loan(detail=liability(ytd_interest=8000.0))
        self.assertEqual(loans.detail_for(account)['ytd_interest_paid'], 8000.0)
        self.assertIsNotNone(account.liability_refreshed_at)

    def test_a_corrupt_blob_does_not_raise(self):
        """This feeds a UI panel; a bad string must not 500 the page."""
        account = self._loan()
        account.liability_detail = 'not json{'
        db.session.commit()
        self.assertEqual(loans.detail_for(account), {})

    def test_refresh_stores_what_plaid_reports(self):
        self._loan()
        item = PlaidItem.query.one()
        fake = FakePlaidClient(liabilities={
            'loan-1': liability(ytd_interest=8000.0, ytd_principal=2400.0)})
        self.assertEqual(loans.refresh_liabilities(item, fake, 't'), 1)
        account = PlaidAccount.query.filter_by(account_id='loan-1').one()
        self.assertEqual(loans.detail_for(account)['ytd_interest_paid'], 8000.0)

    def test_no_loans_costs_no_plaid_call(self):
        """Don't spend a billable call on an Item with nothing to ask about."""
        db.session.add(PlaidAccount(
            account_id='chk', item_id='item-abc', type='depository',
            subtype='checking'))
        db.session.commit()
        item = PlaidItem.query.one()
        fake = FakePlaidClient()
        loans.refresh_liabilities(item, fake, 't')
        self.assertEqual([c for c in fake.calls if c[0] == 'liabilities_get'],
                         [])

    def test_an_unavailable_product_is_not_an_error(self):
        """The common case: `liabilities` isn't approved on the application. The
        loan still imports and its balance is still tracked."""
        account = self._loan()
        item = PlaidItem.query.one()
        self.assertEqual(loans.refresh_liabilities(item, FakePlaidClient(), 't'),
                         0)
        db.session.refresh(account)
        self.assertEqual(account.balance_current, 200000.0)
        self.assertFalse(loans.summary(account)['interest_split_available'])

    def test_a_raising_client_is_swallowed(self):
        from app.plaid_client import PlaidError
        self._loan()
        item = PlaidItem.query.one()
        fake = FakePlaidClient(liabilities_error=PlaidError('boom'))
        self.assertEqual(loans.refresh_liabilities(item, fake, 't'), 0)


# ── interest accrual ────────────────────────────────────────────────────────

class AccrualTests(LoanBase):
    def test_the_first_sight_posts_nothing(self):
        """Booking the reported year-to-date figure on first sight would post
        interest that accrued before Bank Bridge was watching — and on an
        upgrade, for a loan that was never on the books at all."""
        account = self._loan(detail=liability(ytd_interest=8000.0))
        erp = self._erp()
        result = loans.accrue_interest(erp, account)

        self.assertEqual(result['status'], 'seeded')
        self.assertEqual(erp.creates_of('Journal Entry'), [])
        self.assertEqual(account.loan_ytd_interest_booked, 8000.0)

    def test_only_new_interest_is_booked(self):
        account = self._loan(detail=liability(ytd_interest=9600.0),
                             ytd_booked=8000.0)
        result = loans.accrue_interest(erp := self._erp(), account,
                                       posting_date=date(2026, 8, 1))
        self.assertEqual(result['status'], 'posted')
        self.assertEqual(result['amount'], 1600.0)
        self.assertEqual(len(erp.creates_of('Journal Entry')), 1)

    def test_the_entry_debits_interest_and_credits_the_loan(self):
        account = self._loan(detail=liability(ytd_interest=9600.0),
                             ytd_booked=8000.0)
        doc = loans.build_interest_document(
            account, 'Orchard Mortgage - TEST', 'Interest Expense - TEST',
            COMPANY, 1600.0, date(2026, 8, 1))
        debit, credit = doc['accounts']
        self.assertEqual(debit['account'], 'Interest Expense - TEST')
        self.assertEqual(debit['debit_in_account_currency'], 1600.0)
        self.assertEqual(credit['account'], 'Orchard Mortgage - TEST')
        self.assertEqual(credit['credit_in_account_currency'], 1600.0)

    def test_accruals_compose_over_months(self):
        """Each entry books only the gap since the last, so a year of monthly
        accruals sums to the lender's own year-to-date figure."""
        account = self._loan(detail=liability(ytd_interest=1600.0))
        erp = self._erp()
        loans.accrue_interest(erp, account, posting_date=date(2026, 1, 31))
        booked = []
        for month, ytd in ((2, 3200.0), (3, 4800.0), (4, 6400.0)):
            loans.store_detail(account, liability(ytd_interest=ytd))
            db.session.commit()
            r = loans.accrue_interest(erp, account,
                                      posting_date=date(2026, month, 28))
            booked.append(r['amount'])
        self.assertEqual(booked, [1600.0, 1600.0, 1600.0])
        # seeded at 1,600 + 4,800 booked = the lender's 6,400.
        self.assertEqual(account.loan_ytd_interest_booked, 6400.0)

    def test_the_year_rollover_does_not_post_a_negative_accrual(self):
        """January resets ytd_interest_paid to near zero. A naive difference
        would post a large NEGATIVE accrual — a credit to Interest Expense
        wiping out the whole year's cost."""
        account = self._loan(detail=liability(ytd_interest=150.0),
                             ytd_booked=19200.0)
        amount, source = loans.interest_delta(account)
        self.assertEqual(source, 'rollover')
        self.assertEqual(amount, 150.0)

        result = loans.accrue_interest(self._erp(), account,
                                       posting_date=date(2027, 1, 31))
        self.assertEqual(result['status'], 'posted')
        self.assertGreater(result['amount'], 0)

    def test_a_lender_without_ytd_figures_gets_no_guess(self):
        """An amortization estimate was considered and rejected: it diverges
        silently on an extra payment, an escrow adjustment or a rate change,
        and nobody reconciles a number they weren't told was approximate."""
        account = self._loan(detail=liability(ytd_interest=None))
        result = loans.accrue_interest(self._erp(), account)
        self.assertEqual(result['status'], 'skipped')
        self.assertIn('does not report', result['message'])
        self.assertFalse(loans.summary(account)['interest_split_available'])

    def test_is_idempotent_within_a_day(self):
        account = self._loan(detail=liability(ytd_interest=9600.0),
                             ytd_booked=8000.0)
        erp = self._erp()
        loans.accrue_interest(erp, account, posting_date=date(2026, 8, 1))
        again = loans.accrue_interest(erp, account, posting_date=date(2026, 8, 1))
        self.assertEqual(again['status'], 'unchanged')
        self.assertEqual(len(erp.creates_of('Journal Entry')), 1)

    def test_a_movement_under_the_threshold_posts_nothing(self):
        account = self._loan(detail=liability(ytd_interest=8000.40),
                             ytd_booked=8000.0)
        self.assertEqual(loans.accrue_interest(self._erp(), account)['status'],
                         'unchanged')

    def test_entries_land_pending_review(self):
        """Nothing posts to the books unreviewed."""
        account = self._loan(detail=liability(ytd_interest=9600.0),
                             ytd_booked=8000.0)
        loans.accrue_interest(self._erp(), account,
                              posting_date=date(2026, 8, 1))
        entry = loans.existing_entry(account, date(2026, 8, 1))
        self.assertEqual(entry.state, 'pending_review')
        self.assertEqual(entry.rule_name, loans.RULE_LABEL)
        self.assertTrue(loans.is_interest_entry(entry))

    def test_the_interest_account_is_created_once_under_expenses(self):
        erp = self._erp()
        for i in (1, 2):
            account = self._loan(f'loan-{i}',
                                 detail=liability(f'loan-{i}',
                                                  ytd_interest=9600.0),
                                 ytd_booked=8000.0)
            loans.accrue_interest(erp, account,
                                  posting_date=date(2026, 8, i))
        created = [c[2] for c in erp.creates_of('Account')
                   if 'Interest' in str(c[2].get('account_name'))]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]['parent_account'], 'Expenses - TEST')
        self.assertEqual(created[0]['account_type'], 'Expense Account')

    def test_an_existing_interest_account_is_adopted(self):
        """Most stock charts already have one; duplicating it would split the
        year's interest across two accounts."""
        erp = self._erp()
        erp.chart_accounts['Interest Expense - TEST'] = {
            'name': 'Interest Expense - TEST',
            'account_name': 'Interest Expense', 'is_group': 0,
            'company': COMPANY, 'disabled': 0}
        account = self._loan(detail=liability(ytd_interest=9600.0),
                             ytd_booked=8000.0)
        loans.accrue_interest(erp, account, posting_date=date(2026, 8, 1))
        self.assertEqual([c for c in erp.creates_of('Account')
                          if 'Interest' in str(c[2].get('account_name'))], [])

    def test_an_erpnext_refusal_is_recorded_not_raised(self):
        account = self._loan(detail=liability(ytd_interest=9600.0),
                             ytd_booked=8000.0)
        erp = self._erp()
        erp.fail_je_create = True
        result = loans.accrue_interest(erp, account,
                                       posting_date=date(2026, 8, 1))
        self.assertEqual(result['status'], 'error')
        # The counter must NOT advance, or that interest is lost forever.
        self.assertEqual(account.loan_ytd_interest_booked, 8000.0)

    def test_an_unimported_loan_is_skipped(self):
        account = self._loan(gl=None, detail=liability(ytd_interest=9600.0))
        self.assertEqual(loans.accrue_interest(self._erp(), account)['status'],
                         'skipped')

    def test_the_switches_disable_it(self):
        account = self._loan(detail=liability(ytd_interest=9600.0),
                             ytd_booked=8000.0)
        erp = self._erp()
        self.app.config['LOAN_INTEREST_ACCRUAL_ENABLED'] = False
        self.assertEqual(loans.accrue_interest(erp, account)['status'],
                         'skipped')
        self.app.config['LOAN_INTEREST_ACCRUAL_ENABLED'] = True
        self.app.config['LOANS_ENABLED'] = False
        self.assertEqual(loans.accrue_interest(erp, account)['status'],
                         'skipped')
        self.assertEqual(erp.creates_of('Journal Entry'), [])


class AccrueAllTests(LoanBase):
    def test_aggregates_across_loans(self):
        self._loan('loan-1', detail=liability('loan-1', ytd_interest=9600.0),
                   ytd_booked=8000.0)
        self._loan('loan-2', subtype='student',
                   detail=liability('loan-2', kind='student',
                                    ytd_interest=500.0),
                   ytd_booked=400.0)
        stats = loans.accrue_all(self._erp(), posting_date=date(2026, 8, 1))
        self.assertEqual(stats['posted'], 2)
        self.assertEqual(stats['total_interest'], 1700.0)

    def test_a_superseded_loan_is_skipped(self):
        """v0.4.11: its identity moved to a re-linked heir."""
        account = self._loan('loan-old',
                             detail=liability('loan-old', ytd_interest=9600.0),
                             ytd_booked=8000.0)
        account.superseded_by_account_id = 'loan-new'
        db.session.commit()
        self.assertEqual(loans.accrue_all(self._erp())['scanned'], 0)

    def test_a_settled_install_posts_nothing(self):
        self._loan(detail=liability(ytd_interest=8000.0), ytd_booked=8000.0)
        erp = self._erp()
        stats = loans.accrue_all(erp, posting_date=date(2026, 8, 1))
        self.assertEqual(stats['posted'], 0)
        self.assertEqual(erp.creates_of('Journal Entry'), [])


# ── the link-token product ladder ───────────────────────────────────────────

class ProductLadderTests(LoanBase):
    def test_one_unapproved_product_does_not_cost_the_other(self):
        """An install approved for statements but not liabilities must still
        get statements — all-or-nothing would silently lose it."""
        ladder = PlaidClient._product_ladder(['statements', 'liabilities'])
        self.assertEqual(ladder, [['statements', 'liabilities'],
                                  ['statements'], ['liabilities'], []])

    def test_the_ladder_always_ends_at_transactions_only(self):
        for wanted in ([], ['statements'], ['statements', 'liabilities']):
            self.assertEqual(PlaidClient._product_ladder(wanted)[-1], [])

    # WHERE A REQUESTED PRODUCT ACTUALLY LANDS, which these tests have to read
    # from both keys rather than just `products`:
    #
    #   products           transactions (always) + statements
    #   optional_products  liabilities (v0.4.23) + investments (v0.4.26)
    #
    # Both moved OUT of `products` because a required product Link cannot fill
    # is a hard failure at the account-selection screen — 'No liability
    # accounts' on a deposit-only bank, 'No investment accounts' on a bank-only
    # user — while an optional one is granted where eligible and silent
    # otherwise. The LADDER still matters either way: an optional product the
    # Plaid application has not been approved for is still rejected when the
    # token is minted, which is what it degrades through.
    @staticmethod
    def _requested(kwargs) -> list:
        """Every product a link-token call asked for, required or optional."""
        return [str(p) for p in (list(kwargs.get('products') or [])
                                 + list(kwargs.get('optional_products') or []))]

    def test_a_fully_approved_link_makes_one_call(self):
        client = PlaidClient(client_id='c', secret='s', api=object())
        calls = []

        def ok(api, kwargs):
            calls.append(kwargs)
            return 'tok'

        with unittest.mock.patch.object(client, '_get_api', lambda: object()), \
             unittest.mock.patch.object(PlaidClient, '_link_token_create',
                                        staticmethod(ok)):
            client.create_link_token('u', statements=True, liabilities=True,
                                     investments=True)
        self.assertEqual(len(calls), 1)
        asked = self._requested(calls[0])
        for product in ('transactions', 'statements', 'liabilities',
                        'investments'):
            self.assertTrue(any(product in p for p in asked), product)
        # …and each landed on the side that keeps Link from hard-failing.
        self.assertTrue(any('statements' in str(p)
                            for p in calls[0]['products']))
        optional = [str(p) for p in calls[0]['optional_products']]
        self.assertTrue(any('liabilities' in p for p in optional))
        self.assertTrue(any('investments' in p for p in optional))

    def test_a_rejected_product_degrades_to_the_next_rung(self):
        """An unapproved product is rejected at token creation whichever key it
        sits under, so the ladder still has to drop it and retry."""
        client = PlaidClient(client_id='c', secret='s', api=object())
        from app.plaid_client import PlaidError
        seen = []

        def flaky(api, kwargs):
            asked = self._requested(kwargs)
            seen.append(asked)
            if any('liabilities' in p for p in asked):
                raise PlaidError('PRODUCTS_NOT_SUPPORTED: liabilities')
            return 'tok'

        with unittest.mock.patch.object(client, '_get_api', lambda: object()), \
             unittest.mock.patch.object(PlaidClient, '_link_token_create',
                                        staticmethod(flaky)):
            token = client.create_link_token('u', statements=True,
                                             liabilities=True)
        self.assertEqual(token, 'tok')
        self.assertEqual(len(seen), 2)
        # The surviving attempt kept statements — losing one unapproved product
        # must not cost an approved one.
        self.assertTrue(any('statements' in p for p in seen[-1]))
        self.assertFalse(any('liabilities' in p for p in seen[-1]))


# ── the page ────────────────────────────────────────────────────────────────

class LoanPanelTests(LoanBase):
    def test_the_accounts_page_shows_the_loan(self):
        self._loan(detail=liability(ytd_interest=8000.0))
        body = self.client.get('/admin/accounts').data.decode()
        self.assertIn('Orchard Mortgage', body)
        self.assertIn('6.125', body)
        self.assertIn('8000.00', body)

    def test_a_lender_without_ytd_is_labelled_manual(self):
        """Saying so beats a silently missing accrual."""
        self._loan(detail=liability(ytd_interest=None))
        body = self.client.get('/admin/accounts').data.decode()
        self.assertIn('manual', body)

    def test_a_reporting_lender_is_labelled_automatic(self):
        self._loan(detail=liability(ytd_interest=8000.0))
        body = self.client.get('/admin/accounts').data.decode()
        self.assertIn('automatic', body)

    def test_an_install_with_no_loans_shows_no_panel(self):
        body = self.client.get('/admin/accounts').data.decode()
        self.assertNotIn('>Loans<', body)


# ── regressions ─────────────────────────────────────────────────────────────

class RegressionTests(LoanBase):
    def test_investments_are_unaffected(self):
        account = PlaidAccount(account_id='inv', item_id='item-abc',
                               type='investment', subtype='brokerage')
        self.assertFalse(ea.is_loan(account))
        self.assertEqual(ea._gl_side(account), 'investment')
        self.assertEqual(ea.erpnext_account_type(account), 'Investment')

    def test_depository_and_credit_are_unaffected(self):
        for type_, subtype, want in (('depository', 'checking', 'Current'),
                                     ('depository', 'savings', 'Current'),
                                     ('credit', 'credit card', 'Credit')):
            with self.subTest(subtype=subtype):
                account = PlaidAccount(account_id='x', item_id='item-abc',
                                       type=type_, subtype=subtype)
                self.assertEqual(ea.erpnext_account_type(account), want)
                self.assertFalse(ea.is_loan(account))

    def test_loan_entries_are_distinguishable(self):
        from app import opening_balance as obal
        from app import revaluation
        account = self._loan(detail=liability(ytd_interest=9600.0),
                             ytd_booked=8000.0)
        loans.accrue_interest(self._erp(), account,
                              posting_date=date(2026, 8, 1))
        entry = loans.existing_entry(account, date(2026, 8, 1))
        self.assertTrue(loans.is_interest_entry(entry))
        self.assertFalse(obal.is_opening_balance_entry(entry))
        self.assertFalse(revaluation.is_revaluation_entry(entry))

    def test_the_migration_declares_every_new_column(self):
        from app.migrations import SCHEMA_MIGRATIONS
        added = {(t, c) for t, c, _ in SCHEMA_MIGRATIONS}
        for column in ('liability_detail', 'liability_refreshed_at',
                       'loan_ytd_interest_booked', 'loan_ytd_principal_seen'):
            self.assertIn(('plaid_accounts', column), added)

    def test_existing_accounts_default_to_no_liability_data(self):
        account = self._loan()
        self.assertIsNone(account.liability_detail)
        self.assertIsNone(account.loan_ytd_interest_booked)
        self.assertEqual(loans.detail_for(account), {})


if __name__ == '__main__':
    unittest.main()
