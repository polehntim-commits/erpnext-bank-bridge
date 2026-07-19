# SPDX-License-Identifier: MIT
"""Investment fidelity and mark-to-market (v0.4.12).

Two gaps, both from v0.4.0's balance-only investment support.

FIDELITY: an investment account was typed 'Current' (a chequing-class account)
with subtype 'Other' — losing exactly the precision v0.3.9 added for depository
accounts, on the class where "which kind of investment" is the whole question.

MARK-TO-MARKET: the GL leaf only ever held the OPENING value. Refreshed balances
went to an informational custom field on the Bank Account, never to a posting,
so a brokerage that grew from $50k to $65k read $50k on the balance sheet
forever.

Covered here:

  * the type/subtype mapping, and that depository/credit are untouched
  * the delta rule that makes revaluations COMPOSE: each entry posts the gap
    between the live balance and what the LEDGER reflects, not the gap since
    yesterday, so three revaluations leave the leaf at the right place
  * THE SEEDING RULE, which is what stops this being dangerous: a NULL baseline
    posts nothing, because posting would book the account's whole value as a
    fictional one-off gain — which is every investment account on an upgrade
  * direction: a gain debits the asset, a loss credits it, and unrealized
    movement lands in EQUITY so a market rally can't report a profit the farm
    never earned
  * idempotency per day, the threshold, and fail-open behaviour
  * regressions: v0.4.11 adoption, v0.4.4 opening balances and v0.4.0
    balance-only syncing all still behave

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
import unittest.mock
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, crypto, db, revaluation  # noqa: E402
from app import erpnext_accounts as ea  # noqa: E402
from app import opening_balance as obal  # noqa: E402
from app.models import GeneratedJournalEntry, PlaidAccount, PlaidItem  # noqa: E402

from tests.fakes import FakeERPClient  # noqa: E402

COMPANY = 'Testing'


class RevaluationBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': tempfile.mkdtemp(),
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        it = PlaidItem(item_id='item-abc',
                       access_token_encrypted=crypto.encrypt('t'),
                       institution_id='ins_1', institution_name='Schwab',
                       status='active', owning_company=COMPANY)
        db.session.add(it)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _account(self, account_id='inv-1', subtype='brokerage',
                 balance=50000.0, gl='Schwab Brokerage - TEST',
                 balance_only=True, last_revalued=None):
        a = PlaidAccount(
            account_id=account_id, item_id='item-abc', name='Brokerage',
            mask='4321', type='investment', subtype=subtype,
            balance_current=balance, iso_currency_code='USD',
            erpnext_bank_account_name='BA-INV',
            erpnext_gl_account_name=gl, owning_company=COMPANY,
            balance_only=balance_only, import_status='imported',
            last_revalued_balance=last_revalued)
        db.session.add(a)
        db.session.commit()
        return a

    def _booked_opening(self, account, amount=50000.0, state='approved'):
        row = GeneratedJournalEntry(
            plaid_transaction_id=obal.synthetic_transaction_id(account),
            rule_name=obal.RULE_LABEL, state=state, amount=amount,
            description='opening')
        db.session.add(row)
        db.session.commit()
        return row

    def _erp(self):
        """A FakeERPClient with an Equity branch, so the unrealized leaf can be
        found-or-created the way it would be against a real chart."""
        erp = FakeERPClient(chart_accounts=[
            {'account_name': 'Equity', 'name': 'Equity - TEST', 'is_group': 1,
             'root_type': 'Equity', 'parent_account': '', 'company': COMPANY},
        ])
        return erp


# ── fidelity ────────────────────────────────────────────────────────────────

class FidelityTests(RevaluationBase):
    def test_investments_are_typed_investment_not_current(self):
        """Calling a 401k a chequing-class account was wrong on its face and
        misleading in any ERPNext report that groups by account type."""
        for subtype in ('brokerage', 'ira', '401k', 'crypto_exchange'):
            with self.subTest(subtype=subtype):
                account = PlaidAccount(account_id=f'x-{subtype}',
                                       item_id='item-abc', type='investment',
                                       subtype=subtype)
                self.assertEqual(ea.erpnext_account_type(account), 'Investment')

    def test_investment_subtypes_map_one_to_one(self):
        cases = {'brokerage': 'Brokerage', 'ira': 'Ira', '401k': '401K',
                 'roth': 'Roth', 'hsa': 'Hsa', 'mutual fund': 'Mutual Fund',
                 'stock': 'Stock', 'bond': 'Bond'}
        for subtype, expected in cases.items():
            with self.subTest(subtype=subtype):
                account = PlaidAccount(account_id=f'x-{subtype}',
                                       item_id='item-abc', type='investment',
                                       subtype=subtype)
                self.assertEqual(ea.erpnext_account_subtype(account), expected)

    def test_plaid_underscore_subtypes_resolve(self):
        """Plaid sends `crypto_exchange`; a bare lower() would miss it and fall
        back to 'Other' — the bug this normalization prevents."""
        account = PlaidAccount(account_id='x', item_id='item-abc',
                               type='investment', subtype='crypto_exchange')
        self.assertEqual(ea.erpnext_account_subtype(account), 'Crypto Exchange')

    def test_every_mapped_subtype_has_a_provisioned_master(self):
        """A Bank Account create fails with a LinkValidationError if the subtype
        master doesn't exist — so the map and the provisioned list must agree."""
        for value in ea._SUBTYPE_MAP.values():
            self.assertIn(value, ea.DEFAULT_ACCOUNT_SUBTYPES, value)

    def test_the_investment_account_type_is_provisioned(self):
        self.assertIn('Investment', ea.DEFAULT_BANK_ACCOUNT_TYPES)

    def test_depository_and_credit_are_unchanged(self):
        """v0.3.9's mapping must survive verbatim."""
        cases = [('depository', 'checking', 'Current', 'Checking'),
                 ('depository', 'savings', 'Current', 'Savings'),
                 ('depository', 'money market', 'Current', 'Money Market'),
                 ('credit', 'credit card', 'Credit', 'Credit Card')]
        for type_, subtype, want_type, want_subtype in cases:
            with self.subTest(subtype=subtype):
                account = PlaidAccount(account_id=f'x-{subtype}',
                                       item_id='item-abc', type=type_,
                                       subtype=subtype)
                self.assertEqual(ea.erpnext_account_type(account), want_type)
                self.assertEqual(ea.erpnext_account_subtype(account),
                                 want_subtype)

    def test_an_explicit_override_still_wins(self):
        self.app.config['ERPNEXT_DEFAULT_BANK_ACCOUNT_TYPE'] = 'Current'
        account = PlaidAccount(account_id='x', item_id='item-abc',
                               type='investment', subtype='brokerage')
        self.assertEqual(ea.erpnext_account_type(account), 'Current')


# ── the baseline ────────────────────────────────────────────────────────────

class BaselineTests(RevaluationBase):
    def test_a_booked_opening_balance_is_the_baseline(self):
        account = self._account()
        self._booked_opening(account, 50000.0)
        self.assertEqual(revaluation.baseline_for(account), (50000.0, 'opening'))

    def test_a_tracked_baseline_wins(self):
        account = self._account(last_revalued=61000.0)
        self._booked_opening(account, 50000.0)
        self.assertEqual(revaluation.baseline_for(account), (61000.0, 'tracked'))

    def test_an_unapproved_opening_balance_is_not_a_baseline(self):
        """A Draft has not moved the ledger yet; a rejected one never will.
        Treating either as booked would post a delta against a value the leaf
        does not hold."""
        for state in ('pending_review', 'rejected'):
            with self.subTest(state=state):
                account = self._account(account_id=f'inv-{state}')
                self._booked_opening(account, 50000.0, state=state)
                self.assertEqual(revaluation.baseline_for(account),
                                 (None, 'seed'))

    def test_no_opening_balance_at_all_means_seed(self):
        self.assertEqual(revaluation.baseline_for(self._account()),
                         (None, 'seed'))


# ── the seeding rule ────────────────────────────────────────────────────────

class SeedingTests(RevaluationBase):
    def test_the_first_pass_without_a_baseline_posts_nothing(self):
        """THE assertion that makes this safe to ship. Every investment account
        on an upgrading install has a NULL baseline; posting would book the
        account's entire value as a fictional one-off gain."""
        account = self._account(balance=65000.0)
        erp = self._erp()
        result = revaluation.revalue_account(erp, account)

        self.assertEqual(result['status'], 'seeded')
        self.assertEqual(erp.creates_of('Journal Entry'), [])
        self.assertEqual(account.last_revalued_balance, 65000.0)

    def test_after_seeding_only_later_movement_is_posted(self):
        account = self._account(balance=65000.0)
        erp = self._erp()
        revaluation.revalue_account(erp, account)      # seeds at 65,000
        account.balance_current = 67000.0
        db.session.commit()

        result = revaluation.revalue_account(erp, account,
                                             posting_date=date(2026, 7, 2))
        self.assertEqual(result['status'], 'posted')
        self.assertEqual(result['delta'], 2000.0)

    def test_an_upgrading_install_books_no_phantom_gain(self):
        """End to end for the upgrade path: an account with a booked opening
        balance revalues against THAT, not against zero."""
        account = self._account(balance=65000.0)
        self._booked_opening(account, 50000.0)
        result = revaluation.revalue_account(self._erp(), account)
        self.assertEqual(result['status'], 'posted')
        self.assertEqual(result['delta'], 15000.0)     # not 65,000


# ── direction and composition ───────────────────────────────────────────────

class DirectionTests(RevaluationBase):
    _seq = 0

    def _doc(self, delta):
        # build_revaluation_document is pure, but the account row still has to
        # be unique — each call gets its own.
        DirectionTests._seq += 1
        account = self._account(account_id=f'inv-dir-{DirectionTests._seq}')
        return revaluation.build_revaluation_document(
            account, 'Schwab Brokerage - TEST', 'Unrealized - TEST', COMPANY,
            delta, date(2026, 7, 1))

    def test_a_gain_debits_the_asset_and_credits_equity(self):
        doc = self._doc(15000.0)
        debit, credit = doc['accounts']
        self.assertEqual(debit['account'], 'Schwab Brokerage - TEST')
        self.assertEqual(debit['debit_in_account_currency'], 15000.0)
        self.assertEqual(credit['account'], 'Unrealized - TEST')
        self.assertEqual(credit['credit_in_account_currency'], 15000.0)

    def test_a_loss_reverses_it(self):
        doc = self._doc(-8000.0)
        debit, credit = doc['accounts']
        self.assertEqual(debit['account'], 'Unrealized - TEST')
        self.assertEqual(credit['account'], 'Schwab Brokerage - TEST')

    def test_the_entry_balances_and_carries_magnitudes(self):
        for delta in (15000.0, -8000.0, 0.5):
            with self.subTest(delta=delta):
                doc = self._doc(delta)
                debit, credit = doc['accounts']
                self.assertEqual(debit['debit_in_account_currency'],
                                 credit['credit_in_account_currency'])
                self.assertGreaterEqual(debit['debit_in_account_currency'], 0)

    def test_the_remark_says_gain_or_loss(self):
        self.assertIn('gain', self._doc(100.0)['user_remark'])
        self.assertIn('loss', self._doc(-100.0)['user_remark'])

    def test_revaluations_compose_rather_than_double_count(self):
        """The delta is measured against what the LEDGER reflects, not against
        the previous balance reading. Three moves must land the leaf at the sum,
        not re-post the whole gain each time."""
        account = self._account(balance=50000.0)
        self._booked_opening(account, 50000.0)
        erp = self._erp()
        deltas = []
        for day, balance in ((1, 55000.0), (2, 53000.0), (3, 54000.0)):
            account.balance_current = balance
            db.session.commit()
            result = revaluation.revalue_account(
                erp, account, posting_date=date(2026, 7, day))
            deltas.append(result['delta'])
        self.assertEqual(deltas, [5000.0, -2000.0, 1000.0])
        # Opening 50,000 + 5,000 - 2,000 + 1,000 = 54,000 = the live balance.
        self.assertEqual(account.last_revalued_balance, 54000.0)
        self.assertEqual(round(50000.0 + sum(deltas), 2), 54000.0)


# ── posting ─────────────────────────────────────────────────────────────────

class PostingTests(RevaluationBase):
    def test_posts_a_draft_and_records_the_entry(self):
        account = self._account(balance=65000.0)
        self._booked_opening(account, 50000.0)
        erp = self._erp()
        result = revaluation.revalue_account(erp, account,
                                             posting_date=date(2026, 7, 1))

        self.assertEqual(result['status'], 'posted')
        self.assertEqual(len(erp.creates_of('Journal Entry')), 1)
        entry = revaluation.existing_entry(account, date(2026, 7, 1))
        self.assertIsNotNone(entry)
        # Nothing posts unreviewed — same contract as an opening balance.
        self.assertEqual(entry.state, 'pending_review')
        self.assertEqual(entry.amount, 15000.0)
        self.assertEqual(entry.rule_name, revaluation.RULE_LABEL)

    def test_the_equity_leaf_is_created_once_and_reused(self):
        erp = self._erp()
        for i, balance in ((1, 65000.0), (2, 66000.0)):
            account = self._account(account_id=f'inv-{i}', balance=balance)
            self._booked_opening(account, 50000.0)
            revaluation.revalue_account(erp, account,
                                        posting_date=date(2026, 7, i))
        created = [c for c in erp.creates_of('Account')
                   if 'Unrealized' in str(c[2].get('account_name'))]
        self.assertEqual(len(created), 1)

    def test_the_equity_leaf_lands_under_equity(self):
        account = self._account(balance=65000.0)
        self._booked_opening(account, 50000.0)
        erp = self._erp()
        revaluation.revalue_account(erp, account)
        created = [c[2] for c in erp.creates_of('Account')
                   if 'Unrealized' in str(c[2].get('account_name'))][0]
        self.assertEqual(created['parent_account'], 'Equity - TEST')
        self.assertEqual(created['account_type'], 'Equity')
        self.assertEqual(created['is_group'], 0)

    def test_is_idempotent_within_a_day(self):
        """A sync that runs twice on Tuesday revalues once."""
        account = self._account(balance=65000.0)
        self._booked_opening(account, 50000.0)
        erp = self._erp()
        revaluation.revalue_account(erp, account, posting_date=date(2026, 7, 1))
        result = revaluation.revalue_account(erp, account,
                                             posting_date=date(2026, 7, 1))
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(len(erp.creates_of('Journal Entry')), 1)

    def test_a_movement_under_the_threshold_posts_nothing(self):
        account = self._account(balance=50000.50)
        self._booked_opening(account, 50000.0)
        result = revaluation.revalue_account(self._erp(), account)
        self.assertEqual(result['status'], 'unchanged')

    def test_the_threshold_is_configurable(self):
        self.app.config['INVESTMENT_REVALUATION_MIN_DELTA'] = 10000.0
        account = self._account(balance=55000.0)
        self._booked_opening(account, 50000.0)
        result = revaluation.revalue_account(self._erp(), account)
        self.assertEqual(result['status'], 'unchanged')

    def test_an_erpnext_refusal_is_recorded_not_raised(self):
        account = self._account(balance=65000.0)
        self._booked_opening(account, 50000.0)
        erp = self._erp()
        erp.fail_je_create = True
        result = revaluation.revalue_account(erp, account,
                                             posting_date=date(2026, 7, 1))

        self.assertEqual(result['status'], 'error')
        entry = revaluation.existing_entry(account, date(2026, 7, 1))
        self.assertEqual(entry.state, 'error')
        self.assertTrue(entry.error_message)
        # The baseline must NOT advance on a failure, or the movement is lost.
        self.assertIsNone(account.last_revalued_balance)

    def test_a_failed_revaluation_retries_the_next_day(self):
        account = self._account(balance=65000.0)
        self._booked_opening(account, 50000.0)
        erp = self._erp()
        erp.fail_je_create = True
        revaluation.revalue_account(erp, account, posting_date=date(2026, 7, 1))
        erp.fail_je_create = False
        result = revaluation.revalue_account(erp, account,
                                             posting_date=date(2026, 7, 2))
        self.assertEqual(result['status'], 'posted')
        self.assertEqual(result['delta'], 15000.0)


# ── eligibility ─────────────────────────────────────────────────────────────

class EligibilityTests(RevaluationBase):
    def test_a_depository_account_is_never_revalued(self):
        """A chequing account's balance is the sum of its transactions, which
        are already posted. Revaluing it would double-count every one."""
        account = self._account(balance_only=False)
        result = revaluation.revalue_account(self._erp(), account)
        self.assertEqual(result['status'], 'skipped')

    def test_an_investment_without_a_gl_leaf_is_skipped(self):
        account = self._account(gl=None)
        result = revaluation.revalue_account(self._erp(), account)
        self.assertEqual(result['status'], 'skipped')

    def test_an_account_with_no_cached_balance_is_skipped(self):
        account = self._account(balance=None)
        self.assertEqual(revaluation.revalue_account(self._erp(), account)['status'],
                         'skipped')

    def test_a_superseded_account_is_excluded(self):
        """v0.4.11: its mapping belongs to the heir now, and revaluing both
        would post the same movement twice."""
        donor = self._account('inv-old')
        donor.superseded_by_account_id = 'inv-new'
        db.session.commit()
        self._account('inv-new')
        ids = [a.account_id for a in revaluation.eligible_accounts()]
        self.assertEqual(ids, ['inv-new'])

    def test_the_switch_disables_everything(self):
        self.app.config['INVESTMENT_REVALUATION_ENABLED'] = False
        account = self._account(balance=65000.0)
        self._booked_opening(account, 50000.0)
        erp = self._erp()
        self.assertEqual(revaluation.revalue_account(erp, account)['status'],
                         'skipped')
        self.assertEqual(revaluation.revalue_all(erp)['posted'], 0)
        self.assertEqual(erp.creates_of('Journal Entry'), [])

    def test_no_erpnext_is_a_no_op(self):
        self._account()
        self.assertEqual(revaluation.revalue_all(None)['scanned'], 0)


# ── revalue_all ─────────────────────────────────────────────────────────────

class RevalueAllTests(RevaluationBase):
    def test_aggregates_across_accounts(self):
        a1 = self._account('inv-1', balance=65000.0)
        self._booked_opening(a1, 50000.0)
        a2 = self._account('inv-2', balance=8000.0)
        self._booked_opening(a2, 10000.0)
        stats = revaluation.revalue_all(self._erp(),
                                        posting_date=date(2026, 7, 1))
        self.assertEqual(stats['posted'], 2)
        self.assertEqual(stats['total_delta'], 13000.0)   # +15,000 - 2,000

    def test_one_unusable_account_does_not_stop_the_others(self):
        # No cached balance: eligible by the query (it has a GL leaf) but
        # skipped inside, which is the case that has to not abort the pass.
        self._account('inv-1', balance=None)
        a2 = self._account('inv-2', balance=65000.0)
        self._booked_opening(a2, 50000.0)
        stats = revaluation.revalue_all(self._erp(),
                                        posting_date=date(2026, 7, 1))
        self.assertEqual(stats['scanned'], 2)
        self.assertEqual(stats['skipped'], 1)
        self.assertEqual(stats['posted'], 1)

    def test_an_account_with_no_gl_leaf_is_never_scanned(self):
        """Filtered out by eligible_accounts rather than skipped inside — so a
        chart without an investment leaf costs no work at all."""
        self._account('inv-nogl', gl=None)
        self.assertEqual(revaluation.revalue_all(self._erp())['scanned'], 0)

    def test_a_settled_install_posts_nothing(self):
        account = self._account(balance=50000.0)
        self._booked_opening(account, 50000.0)
        erp = self._erp()
        stats = revaluation.revalue_all(erp, posting_date=date(2026, 7, 1))
        self.assertEqual(stats['posted'], 0)
        self.assertEqual(stats['unchanged'], 1)
        self.assertEqual(erp.creates_of('Journal Entry'), [])


# ── regressions ─────────────────────────────────────────────────────────────

class RegressionTests(RevaluationBase):
    def test_revaluation_entries_are_distinguishable(self):
        """The audit dashboard has to tell a revaluation from an opening
        balance, an intercompany leg and a rules-engine entry."""
        account = self._account()
        opening = self._booked_opening(account, 50000.0)
        self.assertFalse(revaluation.is_revaluation_entry(opening))
        self.assertTrue(obal.is_opening_balance_entry(opening))

        account.balance_current = 65000.0
        db.session.commit()
        revaluation.revalue_account(self._erp(), account,
                                    posting_date=date(2026, 7, 1))
        entry = revaluation.existing_entry(account, date(2026, 7, 1))
        self.assertTrue(revaluation.is_revaluation_entry(entry))
        self.assertFalse(obal.is_opening_balance_entry(entry))

    def test_v044_opening_balance_is_untouched(self):
        account = self._account(balance=65000.0)
        opening = self._booked_opening(account, 50000.0)
        revaluation.revalue_account(self._erp(), account)
        db.session.refresh(opening)
        self.assertEqual(opening.amount, 50000.0)
        self.assertEqual(opening.state, 'approved')
        self.assertEqual(obal.opening_balance_status(account), 'booked')

    def test_the_migration_declares_both_columns(self):
        from app.migrations import SCHEMA_MIGRATIONS
        added = {(t, c) for t, c, _ in SCHEMA_MIGRATIONS}
        self.assertIn(('plaid_accounts', 'last_revalued_balance'), added)
        self.assertIn(('plaid_accounts', 'last_revalued_at'), added)

    def test_existing_accounts_default_to_never_revalued(self):
        account = self._account()
        self.assertIsNone(account.last_revalued_balance)
        self.assertIsNone(account.last_revalued_at)


if __name__ == '__main__':
    unittest.main()
