# SPDX-License-Identifier: MIT
"""Statement → books reconciliation: the categories, the learned threshold, the
kairotic firing (v0.9.0).

WHY THIS EXISTS. v0.8.4 shipped a settlement leg that never posted and the
ledger ran to -$1,011,119.41 before a human noticed. Both of v0.8.5's guards are
blind to it: the cash reconciliation asks whether Plaid MIRRORED what the bank
saw (a leg Bank Bridge failed to POST is not a mirroring gap, so the cash
identity balances perfectly), and the Bank-Transaction dedup catches an entry
written TWICE (a period that booked 24 of 26 dividends re-emits nothing).

The principles under test, not just the arithmetic:

  * DATA DRIVEN, NOT HAND-CODED — the drift line is the P95 of this account and
    category's own prior deltas once there are 20 of them, and 5% only until
    then. Drifted samples are excluded from the baseline, so an excursion cannot
    teach the alarm to tolerate excursions.
  * KAIROS OVER CHRONOS — `report` is a pure read. The ACTION fires when a
    finding first APPEARS or CHANGES verdict, and on no other reading, because a
    reconciled period's delta is settled.
  * FAIL SAFE — the report never writes to the ledger; it only reads.
  * FAIL FORWARD — every non-matched row carries a categorized reason, and every
    period that could not be compared is listed with why rather than omitted.
  * THE LEDGER IS THE AUTHORITY — booked amounts are read from ERPNext, never
    summed from Bank Bridge's own mirror of what it intended to post.

Synthetic amounts only.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, erpnext_settings, statement_recon  # noqa: E402
from app.erpnext_client import ERPNextAPIError  # noqa: E402
from app.models import (AuditEvent, BankTransaction,  # noqa: E402
                        GeneratedJournalEntry, PlaidAccount, PlaidItem,
                        PlaidStatement, SecurityTransaction, StatementAnchor,
                        StatementReconSample)

from tests.fakes import FakeERPClient  # noqa: E402

COMPANY = 'Orchard Example, LLC'
ACCOUNT_ID = 'acct-brokerage-1'
PERIOD_START = date(2026, 3, 1)
PERIOD_END = date(2026, 3, 31)


class Base(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir, 'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', COMPANY)
        self.erp = FakeERPClient()
        db.session.add(PlaidItem(item_id='item-1', institution_name='WF',
                                 access_token_encrypted='x'))
        db.session.add(PlaidAccount(
            account_id=ACCOUNT_ID, item_id='item-1', name='BUSINESS BROKERAGE',
            mask='6030', type='investment'))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        os.close(self._dbfd)
        os.unlink(self._dbpath)

    # ── fixtures ────────────────────────────────────────────────────────────

    def _statement(self, metadata, *, statement_id='stmt-1'):
        st = PlaidStatement(
            statement_id=statement_id, plaid_item_id='item-1',
            plaid_account_id=ACCOUNT_ID, period_start=PERIOD_START,
            period_end=PERIOD_END, parsed_metadata=metadata)
        db.session.add(st)
        db.session.commit()
        return st

    def _anchor(self, statement, *, variance=0.0, gap=False, mtm=None):
        anchor = StatementAnchor(
            account_id=ACCOUNT_ID, statement_id=statement.id,
            period_start=PERIOD_START, period_end=PERIOD_END,
            anchored_opening=1000.0, anchored_closing=1000.0,
            transaction_sum=0.0, computed_closing=1000.0,
            variance=variance, chain_gap_from_prior=gap,
            mark_to_market_delta=mtm)
        db.session.add(anchor)
        db.session.commit()
        return anchor

    def _booked(self, *, txn_id, ttype, subtype, amount, submitted=True,
                on=None):
        """One Plaid investment transaction plus the ERPNext Journal Entry Bank
        Bridge posted for it. The AMOUNT lives on the ERPNext doc, because that
        is the only place the report will look for it."""
        db.session.add(SecurityTransaction(
            plaid_investment_transaction_id=txn_id, account_id=ACCOUNT_ID,
            date=(on or date(2026, 3, 15)), type=ttype, subtype=subtype,
            amount=amount, quantity=0.0, price=0.0))
        self._je(f'ACC-JV-{txn_id}', abs(amount), submitted)
        db.session.add(GeneratedJournalEntry(
            plaid_transaction_id=f'pt-{txn_id}',
            plaid_investment_transaction_id=txn_id,
            erpnext_journal_entry_name=f'ACC-JV-{txn_id}', amount=abs(amount),
            state='approved' if submitted else 'pending_review'))
        db.session.commit()

    def _je(self, name, amount, submitted):
        """Register a Journal Entry where BOTH a get_doc and a filtered
        list_docs would find it — the report batches its ledger read, so a
        fixture that only populated `docs` would be invisible to it (and would
        not exist in a real ERPNext either)."""
        doc = {'name': name, 'total_debit': amount,
               'docstatus': 1 if submitted else 0,
               # `total_debit` is read off the row by the fake's JE list, which
               # derives it from the lines; give it one so both paths agree.
               'accounts': [{'account': 'X', 'debit_in_account_currency': amount}]}
        self.erp.docs[name] = doc
        self.erp.created['Journal Entry'][name] = doc
        if submitted:
            self.erp.submitted.add(name)

    def _bank_booked(self, *, txn_id, amount, submitted=True, on=None):
        """One ordinary Bank Transaction plus the Journal Entry booked for it.
        Plaid's sign convention: POSITIVE means money left the account."""
        db.session.add(BankTransaction(
            plaid_transaction_id=txn_id, account_id=ACCOUNT_ID,
            amount=amount, name=f'TXN {txn_id}',
            date=(on or date(2026, 3, 15)), removed=False))
        self._je(f'ACC-JV-{txn_id}', abs(amount), submitted)
        db.session.add(GeneratedJournalEntry(
            plaid_transaction_id=txn_id,
            erpnext_journal_entry_name=f'ACC-JV-{txn_id}', amount=abs(amount),
            state='approved' if submitted else 'pending_review'))
        db.session.commit()

    def _row(self, rep, category):
        for r in rep['rows']:
            if r['category'] == category:
                return r
        return None


# ── the comparison rule, in isolation ───────────────────────────────────────

class ClassifyRule(unittest.TestCase):
    """`classify` is pure, and it is the one place a judgement is made — so it
    is tested without a database or an ERPNext anywhere near it."""

    def test_equal_amounts_match(self):
        status, reason, delta, pct = statement_recon.classify(
            1000.0, 1000.0, 0.0, 0.05, 'default')
        self.assertEqual(status, 'matched')
        self.assertEqual((reason, delta, pct), ('', 0.0, 0.0))

    def test_a_sub_cent_difference_is_rounding_not_a_finding(self):
        status, _, _, _ = statement_recon.classify(
            1000.0, 1000.005, 0.0, 0.05, 'default')
        self.assertEqual(status, 'matched')

    def test_statement_activity_with_nothing_booked_is_unexplained(self):
        """The v0.8.4 shape, and deliberately NOT called 'drifted': one side is
        empty, which is a different finding from "the numbers differ by more
        than usual"."""
        status, reason, delta, _ = statement_recon.classify(
            2030.0, 0.0, 0.0, 0.05, 'default')
        self.assertEqual(status, 'unexplained')
        self.assertEqual(reason, 'nothing_booked')
        self.assertEqual(delta, -2030.0)

    def test_booked_activity_the_statement_never_mentions_is_unexplained(self):
        status, reason, _, pct = statement_recon.classify(
            0.0, 500.0, 0.0, 0.05, 'default')
        self.assertEqual(status, 'unexplained')
        self.assertEqual(reason, 'nothing_on_statement')
        # A percentage of zero is undefined and must not be reported as 0%.
        self.assertIsNone(pct)

    def test_drafts_closing_the_gap_is_a_submit_backlog_not_a_drift(self):
        """The distinction that stops an operator hunting for a transaction that
        is sitting in the approval queue."""
        status, reason, _, _ = statement_recon.classify(
            1000.0, 400.0, 600.0, 0.05, 'baseline_p95')
        self.assertEqual(status, 'drifted')
        self.assertEqual(reason, 'drafts_would_match')

    def test_a_delta_inside_the_threshold_matches(self):
        status, _, _, pct = statement_recon.classify(
            1000.0, 1020.0, 0.0, 0.05, 'baseline_p95')
        self.assertEqual(status, 'matched')
        self.assertAlmostEqual(pct, 0.02, places=4)

    def test_a_delta_over_the_threshold_drifts(self):
        status, reason, _, pct = statement_recon.classify(
            1000.0, 1200.0, 0.0, 0.05, 'baseline_p95')
        self.assertEqual(status, 'drifted')
        self.assertEqual(reason, 'over_threshold')
        self.assertAlmostEqual(pct, 0.20, places=4)

    def test_no_baseline_yet_is_named_as_such(self):
        """An early flag must read as "no history yet", not as "this install
        normally does better than this"."""
        _, reason, _, _ = statement_recon.classify(
            1000.0, 1200.0, 0.0, statement_recon.DEFAULT_THRESHOLD_PCT,
            'default')
        self.assertEqual(reason, 'no_baseline_yet')

    def test_every_reason_is_documented(self):
        """FAIL FORWARD · a reason with no explanation is a reason an operator
        cannot act on."""
        for reason in ('nothing_booked', 'nothing_on_statement',
                       'drafts_would_match', 'over_threshold',
                       'no_baseline_yet', 'not_booked_by_design'):
            self.assertIn(reason, statement_recon.DRIFT_REASONS)
            self.assertTrue(statement_recon.DRIFT_REASONS[reason].strip())

    def test_an_informational_category_with_nothing_booked_is_not_a_finding(self):
        """Mark-to-market has no booked counterpart BY DESIGN — the cash
        reconciliation is cash-only on purpose. Calling that `unexplained` would
        put an identical, permanent, unactionable finding on every brokerage
        period, and a report that always shows the same finding is one an
        operator stops reading."""
        status, reason, delta, _ = statement_recon.classify(
            28000.0, 0.0, 0.0, 0.05, 'default', informational=True)
        self.assertEqual(status, 'matched')
        self.assertEqual(reason, 'not_booked_by_design')
        # The delta is still REPORTED — it is the period's price movement, and
        # suppressing the number would lose the visibility the row is for.
        self.assertEqual(delta, -28000.0)

    def test_the_same_shape_IS_a_finding_for_a_normal_category(self):
        """The exemption is per category, not a general softening."""
        status, reason, _, _ = statement_recon.classify(
            28000.0, 0.0, 0.0, 0.05, 'default', informational=False)
        self.assertEqual(status, 'unexplained')
        self.assertEqual(reason, 'nothing_booked')

    def test_an_informational_category_that_IS_booked_compares_normally(self):
        """Once a revaluation has posted something the two numbers genuinely
        should agree, so the exemption stops applying."""
        status, reason, _, _ = statement_recon.classify(
            28000.0, 14000.0, 0.0, 0.05, 'baseline_p95', informational=True)
        self.assertEqual(status, 'drifted')
        self.assertEqual(reason, 'over_threshold')


# ── the category table ──────────────────────────────────────────────────────

class CategoryTable(unittest.TestCase):
    def test_the_eight_categories_tim_asked_for(self):
        self.assertEqual([c.key for c in statement_recon.CATEGORIES],
                         ['dividends', 'interest', 'buys', 'sells', 'fees',
                          'deposits', 'withdrawals', 'mark_to_market'])

    def test_plaid_classification_decides_the_category(self):
        """Keyed on the EVENT (Plaid's type/subtype), not on the offset account —
        so the report keeps working after a chart edit, which is exactly when a
        drift detector is most needed."""
        def txn(ttype, subtype):
            return SecurityTransaction(type=ttype, subtype=subtype)

        by_key = statement_recon.CATEGORIES_BY_KEY
        self.assertTrue(by_key['dividends'].matches_security(
            txn('cash', 'cash/dividend')))
        self.assertTrue(by_key['interest'].matches_security(
            txn('cash', 'cash/interest')))
        self.assertTrue(by_key['buys'].matches_security(txn('buy', 'buy/buy')))
        self.assertTrue(by_key['sells'].matches_security(
            txn('sell', 'sell/sell')))
        self.assertTrue(by_key['fees'].matches_security(
            txn('fee', 'fee/miscellaneous fee')))
        self.assertTrue(by_key['deposits'].matches_security(
            txn('transfer', 'transfer/deposit')))
        self.assertTrue(by_key['withdrawals'].matches_security(
            txn('transfer', 'transfer/withdrawal')))
        # A dividend is not a buy, and mark-to-market matches no transaction at
        # all — no Plaid event corresponds to a price change.
        self.assertFalse(by_key['buys'].matches_security(
            txn('cash', 'cash/dividend')))
        self.assertFalse(by_key['mark_to_market'].matches_security(
            txn('cash', 'cash/dividend')))

    def test_only_mark_to_market_is_informational(self):
        """The exemption is deliberately narrow — one category, for one reason.
        Widening it would turn the report into a list of things not to worry
        about."""
        informational = [c.key for c in statement_recon.CATEGORIES
                         if c.informational]
        self.assertEqual(informational, ['mark_to_market'])

    def test_a_brokerage_period_does_not_report_mtm_as_a_finding(self):
        """End to end: the permanent-noise regression this guards against."""
        self.assertTrue(
            statement_recon.CATEGORIES_BY_KEY['mark_to_market'].informational)

    def test_qualified_dividends_are_not_added_to_ordinary(self):
        """They are a TAX CHARACTERISATION of a subset of the same money.
        Summing them would double-count every qualified dividend."""
        self.assertEqual(
            statement_recon.CATEGORIES_BY_KEY['dividends'].statement_keys,
            ('ordinary_dividends',))


# ── the learned threshold ───────────────────────────────────────────────────

class LearnedThreshold(Base):
    def _sample(self, pct, *, drifted=False, period):
        db.session.add(StatementReconSample(
            account_id=ACCOUNT_ID, account_mask='6030', period_start=period,
            category='dividends', delta_pct=pct, drifted=drifted,
            status='drifted' if drifted else 'matched'))
        db.session.commit()

    def test_the_default_applies_until_there_is_enough_history(self):
        threshold, source, count = statement_recon.learned_threshold(
            ACCOUNT_ID, 'dividends')
        self.assertEqual(threshold, statement_recon.DEFAULT_THRESHOLD_PCT)
        self.assertEqual(source, 'default')
        self.assertEqual(count, 0)

    def test_below_the_minimum_sample_count_it_is_still_the_default(self):
        for i in range(statement_recon.MIN_SAMPLES_FOR_BASELINE - 1):
            self._sample(0.01, period=date(2024, 1, 1) + __import__(
                'datetime').timedelta(days=32 * i))
        threshold, source, _ = statement_recon.learned_threshold(
            ACCOUNT_ID, 'dividends')
        self.assertEqual(source, 'default')
        self.assertEqual(threshold, statement_recon.DEFAULT_THRESHOLD_PCT)

    def test_the_threshold_becomes_this_installs_own_p95(self):
        import datetime
        for i in range(statement_recon.MIN_SAMPLES_FOR_BASELINE):
            self._sample(0.02, period=date(2024, 1, 1)
                         + datetime.timedelta(days=32 * i))
        threshold, source, count = statement_recon.learned_threshold(
            ACCOUNT_ID, 'dividends')
        self.assertEqual(source, 'baseline_p95')
        self.assertEqual(count, statement_recon.MIN_SAMPLES_FOR_BASELINE)
        # P95 of a flat 2% history, plus the slack factor.
        self.assertAlmostEqual(
            threshold, 0.02 * statement_recon.BASELINE_SLACK, places=6)

    def test_drifted_samples_are_excluded_from_the_baseline(self):
        """The guard that keeps an adaptive alarm alive. A threshold that learned
        from its own excursions would ratchet up until it never fired again."""
        import datetime
        for i in range(statement_recon.MIN_SAMPLES_FOR_BASELINE):
            self._sample(0.02, period=date(2024, 1, 1)
                         + datetime.timedelta(days=32 * i))
        clean, _, clean_count = statement_recon.learned_threshold(
            ACCOUNT_ID, 'dividends')
        # Twenty 90% excursions must not move the line at all.
        for i in range(20):
            self._sample(0.90, drifted=True, period=date(2026, 1, 1)
                         + datetime.timedelta(days=32 * i))
        after, _, after_count = statement_recon.learned_threshold(
            ACCOUNT_ID, 'dividends')
        self.assertEqual(after, clean)
        self.assertEqual(after_count, clean_count)

    def test_a_flat_zero_history_cannot_learn_a_zero_threshold(self):
        """An account whose deltas are always exactly 0 would otherwise compute a
        P95 of 0 and report a one-cent rounding difference as a finding, which
        teaches the operator to ignore the report."""
        import datetime
        for i in range(statement_recon.MIN_SAMPLES_FOR_BASELINE):
            self._sample(0.0, period=date(2024, 1, 1)
                         + datetime.timedelta(days=32 * i))
        threshold, source, _ = statement_recon.learned_threshold(
            ACCOUNT_ID, 'dividends')
        self.assertEqual(source, 'baseline_p95')
        self.assertEqual(threshold, statement_recon.MIN_LEARNED_THRESHOLD_PCT)


# ── the report ──────────────────────────────────────────────────────────────

class ReportGating(Base):
    """The kairotic GATE: a period is compared only when the statement has
    arrived, is anchored, and reconciles. Every exclusion is REPORTED."""

    def test_an_unreconciled_period_is_skipped_with_its_reason(self):
        st = self._statement({'ordinary_dividends': 1000.0})
        self._anchor(st, variance=-5000.0)

        rep = statement_recon.report(self.erp)

        self.assertEqual(rep['rows'], [])
        self.assertEqual(len(rep['skipped']), 1)
        self.assertEqual(rep['skipped'][0]['reason'], 'not_reconciled')
        self.assertTrue(rep['skipped'][0]['detail'].strip())

    def test_a_chain_gap_is_skipped_with_its_reason(self):
        st = self._statement({'ordinary_dividends': 1000.0})
        self._anchor(st, gap=True)

        rep = statement_recon.report(self.erp)

        self.assertEqual(rep['rows'], [])
        self.assertEqual(rep['skipped'][0]['reason'], 'chain_gap')

    def test_skipped_periods_are_never_silently_omitted(self):
        """FAIL FORWARD · a month absent without explanation reads as a month
        that is fine."""
        st = self._statement({'ordinary_dividends': 1000.0})
        self._anchor(st, variance=-5000.0)

        rep = statement_recon.report(self.erp, include_skipped=False)
        self.assertEqual(rep['skipped'], [])
        rep = statement_recon.report(self.erp)
        self.assertEqual(len(rep['skipped']), 1)

    def test_a_reconciled_period_is_compared(self):
        st = self._statement({'ordinary_dividends': 1000.0})
        self._anchor(st)
        self._booked(txn_id='d1', ttype='cash', subtype='cash/dividend',
                     amount=-1000.0)

        rep = statement_recon.report(self.erp)

        row = self._row(rep, 'dividends')
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'matched')
        self.assertEqual(row['statement_amount'], 1000.0)
        self.assertEqual(row['booked_amount'], 1000.0)


class ReportFindings(Base):
    def test_the_headline_case_two_dividends_never_booked(self):
        """26 dividends on the statement, 24 in the books. The cash reconciles
        and nothing was re-emitted — this is the only guard that sees it."""
        st = self._statement({'ordinary_dividends': 2600.0})
        self._anchor(st)
        for i in range(24):
            self._booked(txn_id=f'd{i}', ttype='cash',
                         subtype='cash/dividend', amount=-100.0)

        rep = statement_recon.report(self.erp)
        row = self._row(rep, 'dividends')

        self.assertEqual(row['booked_amount'], 2400.0)
        self.assertEqual(row['delta'], -200.0)
        self.assertAlmostEqual(row['delta_pct'], -200.0 / 2600.0, places=6)
        self.assertEqual(row['status'], 'drifted')
        self.assertEqual(rep['status'], 'warn')
        self.assertEqual(len(rep['findings']), 1)

    def test_the_booked_amount_comes_from_erpnext_not_our_own_mirror(self):
        """THE LEDGER IS THE AUTHORITY. A report whose job is detecting that the
        books diverged from reality cannot take Bank Bridge's record of what it
        INTENDED to post as evidence of what the books hold."""
        st = self._statement({'ordinary_dividends': 1000.0})
        self._anchor(st)
        self._booked(txn_id='d1', ttype='cash', subtype='cash/dividend',
                     amount=-1000.0)
        # Someone edited the entry in ERPNext down to 600. The local
        # GeneratedJournalEntry.amount still says 1000.
        self.erp.docs['ACC-JV-d1']['total_debit'] = 600.0

        row = self._row(statement_recon.report(self.erp), 'dividends')

        self.assertEqual(row['booked_amount'], 600.0)
        self.assertEqual(row['delta'], -400.0)
        self.assertEqual(row['status'], 'drifted')

    def test_drafts_are_counted_separately_from_the_gl(self):
        st = self._statement({'ordinary_dividends': 1000.0})
        self._anchor(st)
        self._booked(txn_id='d1', ttype='cash', subtype='cash/dividend',
                     amount=-400.0)
        self._booked(txn_id='d2', ttype='cash', subtype='cash/dividend',
                     amount=-600.0, submitted=False)

        row = self._row(statement_recon.report(self.erp), 'dividends')

        self.assertEqual(row['booked_amount'], 400.0)
        self.assertEqual(row['booked_draft_amount'], 600.0)
        self.assertEqual(row['reason'], 'drafts_would_match')

    def test_a_cancelled_entry_counts_toward_neither(self):
        st = self._statement({'ordinary_dividends': 1000.0})
        self._anchor(st)
        self._booked(txn_id='d1', ttype='cash', subtype='cash/dividend',
                     amount=-1000.0)
        self.erp.docs['ACC-JV-d1']['docstatus'] = 2

        row = self._row(statement_recon.report(self.erp), 'dividends')

        self.assertEqual(row['booked_amount'], 0.0)
        self.assertEqual(row['booked_draft_amount'], 0.0)
        self.assertEqual(row['reason'], 'nothing_booked')

    def test_magnitudes_are_compared_not_signs(self):
        """A statement prints 'securities purchased' negative; a Journal Entry
        carries unsigned debits. Reconciling the two conventions per field per
        layout would be a hand-coded guess per field."""
        st = self._statement({'securities_purchased': -50000.0})
        self._anchor(st)
        self._booked(txn_id='b1', ttype='buy', subtype='buy/buy',
                     amount=50000.0)

        row = self._row(statement_recon.report(self.erp), 'buys')

        self.assertEqual(row['statement_amount'], 50000.0)
        self.assertEqual(row['booked_amount'], 50000.0)
        self.assertEqual(row['status'], 'matched')

    def test_interest_sums_its_two_disjoint_statement_lines(self):
        st = self._statement({'interest_income': 12.0, 'sweep_income': 8.0})
        self._anchor(st)
        self._booked(txn_id='i1', ttype='cash', subtype='cash/interest',
                     amount=-20.0)

        row = self._row(statement_recon.report(self.erp), 'interest')

        self.assertEqual(row['statement_amount'], 20.0)
        self.assertEqual(row['status'], 'matched')

    def test_a_category_absent_from_both_sides_produces_no_row(self):
        """A depository statement has no 'securities purchased' line and the
        books did nothing — that is not a finding and not worth a row."""
        st = self._statement({'ordinary_dividends': 100.0})
        self._anchor(st)
        self._booked(txn_id='d1', ttype='cash', subtype='cash/dividend',
                     amount=-100.0)

        rep = statement_recon.report(self.erp)

        self.assertIsNone(self._row(rep, 'buys'))
        self.assertIsNotNone(self._row(rep, 'dividends'))

    def test_an_unreadable_journal_entry_biases_toward_reporting_drift(self):
        """Losing an amount understates the booked total, which reports a drift
        that may not exist — the safe direction for a detector, and logged."""
        st = self._statement({'ordinary_dividends': 1000.0})
        self._anchor(st)
        self._booked(txn_id='d1', ttype='cash', subtype='cash/dividend',
                     amount=-1000.0)
        # The local row still points at a JE ERPNext no longer returns.
        self.erp.docs.pop('ACC-JV-d1')
        self.erp.created['Journal Entry'].pop('ACC-JV-d1')

        row = self._row(statement_recon.report(self.erp), 'dividends')

        self.assertEqual(row['booked_amount'], 0.0)
        self.assertNotEqual(row['status'], 'matched')

    def test_mark_to_market_is_shown_but_never_counted_as_a_finding(self):
        """A brokerage period whose portfolio moved $28k reports the number for
        visibility and does NOT add a finding — unrealized movement is
        deliberately not booked (the cash reconciliation is cash-only), so an
        `unexplained` row here would repeat on every period forever."""
        st = self._statement({'ordinary_dividends': 1000.0})
        self._anchor(st, mtm=28000.0)
        self._booked(txn_id='d1', ttype='cash', subtype='cash/dividend',
                     amount=-1000.0)

        rep = statement_recon.report(self.erp)
        row = self._row(rep, 'mark_to_market')

        self.assertIsNotNone(row, 'the MtM row must still be shown')
        self.assertEqual(row['statement_amount'], 28000.0)
        self.assertEqual(row['status'], 'matched')
        self.assertEqual(row['reason'], 'not_booked_by_design')
        self.assertEqual(rep['findings'], [])
        self.assertEqual(rep['status'], 'ok')

    def test_a_depository_period_has_no_mark_to_market_row_at_all(self):
        """No portfolio figures on the statement and nothing booked — not a
        finding and not worth a row."""
        st = self._statement({'ordinary_dividends': 1000.0})
        self._anchor(st, mtm=None)
        self._booked(txn_id='d1', ttype='cash', subtype='cash/dividend',
                     amount=-1000.0)

        self.assertIsNone(
            self._row(statement_recon.report(self.erp), 'mark_to_market'))

    def test_the_ledger_read_is_batched_not_one_call_per_entry(self):
        """A per-entry fetch is ~900 round-trips on this install every time the
        admin page loads — and the sync calls it too. One filtered list per
        period (chunked at 100 docnames) keeps it in single digits."""
        st = self._statement({'ordinary_dividends': 2400.0})
        self._anchor(st)
        for i in range(24):
            self._booked(txn_id=f'd{i}', ttype='cash',
                         subtype='cash/dividend', amount=-100.0)
        self.erp.calls.clear()

        statement_recon.report(self.erp)

        je_reads = [c for c in self.erp.calls if c[1] == 'Journal Entry']
        self.assertEqual(len(je_reads), 1,
                         f'24 entries must cost ONE list call, got {je_reads}')
        self.assertEqual(je_reads[0][0], 'list_docs')
        # And no per-document fetch crept back in.
        self.assertEqual(
            [c for c in self.erp.calls
             if c[0] == 'get_doc' and c[1] == 'Journal Entry'], [])

    def test_a_depository_period_counts_its_bank_transactions(self):
        """With no investment activity, deposits and withdrawals come from
        ordinary Bank Transactions — Plaid's sign convention is positive = out."""
        st = self._statement({'deposits_total': 5000.0,
                              'withdrawals_total': -1200.0})
        self._anchor(st)
        self._bank_booked(txn_id='b1', amount=-5000.0)      # money IN
        self._bank_booked(txn_id='b2', amount=1200.0)       # money OUT

        rep = statement_recon.report(self.erp)

        self.assertEqual(self._row(rep, 'deposits')['booked_amount'], 5000.0)
        self.assertEqual(self._row(rep, 'withdrawals')['booked_amount'], 1200.0)
        self.assertEqual(rep['findings'], [])

    def test_an_investment_period_does_not_also_count_the_bank_side(self):
        """ONE SIDE OR THE OTHER. A paired brokerage pulls in its cash-services
        companion, whose Bank Transactions ARE the same settlement flows under
        another Plaid id — counting both would put a permanent false drift on
        deposits and withdrawals for every WFA period, i.e. cry wolf on the
        account this feature was built to watch."""
        st = self._statement({'deposits_total': 5000.0})
        self._anchor(st)
        # The investment side reports the deposit...
        self._booked(txn_id='t1', ttype='transfer', subtype='transfer/deposit',
                     amount=-5000.0)
        # ...and the companion's Bank Transaction mirrors the very same money.
        self._bank_booked(txn_id='b1', amount=-5000.0)

        row = self._row(statement_recon.report(self.erp), 'deposits')

        self.assertEqual(row['booked_amount'], 5000.0,
                         'the same $5,000 must be counted once, not twice')
        self.assertEqual(row['status'], 'matched')

    def test_the_report_writes_nothing_to_the_ledger(self):
        """FAIL SAFE · this is a read. Not one create, submit or cancel."""
        st = self._statement({'ordinary_dividends': 2600.0})
        self._anchor(st)
        self._booked(txn_id='d1', ttype='cash', subtype='cash/dividend',
                     amount=-100.0)

        # Snapshot, because the FIXTURE legitimately pre-registers entries —
        # what must not change is anything the report itself did.
        before_jes = dict(self.erp.created['Journal Entry'])
        before_submitted = set(self.erp.submitted)

        statement_recon.report(self.erp)

        self.assertEqual(self.erp.created['Journal Entry'], before_jes)
        self.assertEqual(self.erp.submitted, before_submitted)
        self.assertEqual(self.erp.cancelled, set())
        self.assertEqual(self.erp.deleted, set())
        # And no write call of any kind reached ERPNext.
        self.assertEqual(
            [c for c in self.erp.calls if c[0] not in ('list_docs', 'get_doc')],
            [])


# ── the kairotic firing ─────────────────────────────────────────────────────

class KairoticFiring(Base):
    def _drifting(self):
        st = self._statement({'ordinary_dividends': 2600.0})
        self._anchor(st)
        for i in range(24):
            self._booked(txn_id=f'd{i}', ttype='cash',
                         subtype='cash/dividend', amount=-100.0)

    def _events(self, event_type='statement_recon_drift'):
        return AuditEvent.query.filter_by(event_type=event_type).all()

    @staticmethod
    def _payload(event):
        import json
        return json.loads(event.payload_after or '{}')

    def test_a_new_finding_fires_once(self):
        self._drifting()

        result = statement_recon.observe(self.erp)

        self.assertEqual(len(result['fired']), 1)
        self.assertEqual(len(self._events()), 1)
        sample = StatementReconSample.query.filter_by(
            category='dividends').one()
        self.assertIsNotNone(sample.fired_at)
        self.assertTrue(sample.drifted)

    def test_re_reading_the_same_settled_finding_does_not_re_alert(self):
        """KAIROS · a reconciled period's delta is settled, so there is no
        "still drifted" state to re-alert on. The admin page and the MCP tool
        can be read as often as anyone likes."""
        self._drifting()
        statement_recon.observe(self.erp)

        for _ in range(4):
            result = statement_recon.observe(self.erp)

        self.assertEqual(result['fired'], [])
        self.assertEqual(len(result['already_known']), 1)
        self.assertEqual(len(self._events()), 1)

    def test_resolving_a_finding_fires_a_recovery_once(self):
        """The other meaningful moment: the books moved under a settled period —
        someone posted the missing leg. Closing the loop is what makes the audit
        trail answer "and was it fixed?"."""
        self._drifting()
        statement_recon.observe(self.erp)
        self.assertEqual(len(self._events()), 1)

        # The two missing dividends are posted.
        self._booked(txn_id='d24', ttype='cash', subtype='cash/dividend',
                     amount=-100.0)
        self._booked(txn_id='d25', ttype='cash', subtype='cash/dividend',
                     amount=-100.0)
        result = statement_recon.observe(self.erp)

        self.assertEqual(result['fired'], [])
        self.assertEqual(len(result['recovered']), 1)
        self.assertEqual(len(self._events('statement_recon_resolved')), 1)
        sample = StatementReconSample.query.filter_by(
            category='dividends').one()
        self.assertEqual(sample.status, 'matched')
        self.assertFalse(sample.drifted)

        # And the recovery is a ONE-SHOT too: reading again says nothing.
        again = statement_recon.observe(self.erp)
        self.assertEqual(again['recovered'], [])
        self.assertEqual(len(self._events('statement_recon_resolved')), 1)

    def test_a_finding_that_changes_shape_fires_again(self):
        """drifted → unexplained is a different finding, not the same one, so it
        earns its own line."""
        self._drifting()
        statement_recon.observe(self.erp)

        # Every booked dividend is cancelled: now NOTHING is booked.
        for i in range(24):
            self.erp.docs[f'ACC-JV-d{i}']['docstatus'] = 2
        result = statement_recon.observe(self.erp)

        self.assertEqual(len(result['fired']), 1)
        self.assertEqual(result['fired'][0]['status'], 'unexplained')
        self.assertEqual(result['fired'][0]['reason'], 'nothing_booked')
        self.assertEqual(len(self._events()), 2)

    def test_a_matched_period_never_fires(self):
        st = self._statement({'ordinary_dividends': 1000.0})
        self._anchor(st)
        self._booked(txn_id='d1', ttype='cash', subtype='cash/dividend',
                     amount=-1000.0)

        result = statement_recon.observe(self.erp)

        self.assertEqual(result['fired'], [])
        self.assertEqual(self._events(), [])

    def test_one_row_per_account_period_category_not_an_append(self):
        """The sample table is the settled verdict, updated in place — so the
        baseline is a history of PERIODS, not a history of page loads."""
        self._drifting()
        for _ in range(5):
            statement_recon.observe(self.erp)

        self.assertEqual(
            StatementReconSample.query.filter_by(category='dividends').count(),
            1)

    def test_the_audit_event_names_the_statement_and_the_numbers(self):
        """MAKE THE BANK HAPPY · a finding an auditor can trace to one document
        and one arithmetic."""
        self._drifting()
        statement_recon.observe(self.erp)

        after = self._payload(self._events()[0])
        self.assertEqual(after['statement_id'], 'stmt-1')
        self.assertEqual(after['category'], 'dividends')
        self.assertEqual(after['statement_amount'], 2600.0)
        self.assertEqual(after['booked_amount'], 2400.0)
        self.assertEqual(after['delta'], -200.0)
        self.assertEqual(after['account_mask'], '6030')
        self.assertIn('reason', after)

    def test_observe_quietly_swallows_an_unreachable_erpnext(self):
        """A recon read that failed must not turn a successful sync into a
        failed one."""
        self._drifting()
        self.erp.fail_list = {'Journal Entry': (500, 'boom')}

        class Boom(FakeERPClient):
            def get_doc(self, doctype, name):
                raise ERPNextAPIError('down', status_code=503)

        self.assertIsNotNone(statement_recon.observe_quietly(Boom()))
        self.assertIsNone(statement_recon.observe_quietly(None))


# ── the surfaces ────────────────────────────────────────────────────────────

class AdminAndMcpSurfaces(Base):
    """A new admin route has 500'd this UI before by colliding with a `_page()`
    context kwarg, so every one of them gets a real GET."""

    def setUp(self):
        super().setUp()
        st = self._statement({'ordinary_dividends': 2600.0})
        self._anchor(st)
        for i in range(24):
            self._booked(txn_id=f'd{i}', ttype='cash',
                         subtype='cash/dividend', amount=-100.0)
        self.http = self.app.test_client()

    def _patch_client(self):
        from unittest import mock
        return mock.patch('app.sync_engine.get_erp_client_or_none',
                          return_value=self.erp)

    def test_the_html_page_renders(self):
        with self._patch_client():
            resp = self.http.get('/admin/statement_recon.html')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('Statement → books reconciliation', body)
        self.assertIn('drifted', body)
        self.assertIn('2,600.00', body)
        # The non-additivity warning must be on the page, not only in the code.
        self.assertIn('not additive', body)

    def test_the_html_page_renders_when_erpnext_is_unconfigured(self):
        """A diagnostic page that 500s tells the operator less than one that
        says what is wrong."""
        from unittest import mock
        with mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=None):
            resp = self.http.get('/admin/statement_recon.html')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('ERPNext is not configured',
                      resp.get_data(as_text=True))

    def test_the_json_route_returns_the_report(self):
        with self._patch_client():
            resp = self.http.get('/admin/statement_recon')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['status'], 'warn')
        self.assertEqual(len(data['findings']), 1)
        self.assertTrue(data['categories_are_not_additive'])

    def test_the_json_route_says_so_when_erpnext_is_unconfigured(self):
        from unittest import mock
        with mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=None):
            resp = self.http.get('/admin/statement_recon')
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(resp.get_json()['ok'])

    def test_the_mcp_tool_is_read_only_and_ungated(self):
        from app.blueprints import mcp_server
        spec = mcp_server.TOOLS['get_statement_recon_report']
        self.assertFalse(spec['mutating'])
        # Reading a diagnostic cannot change the books, so it must not be
        # behind a kill switch — an AI operator that cannot see the drift is the
        # operator the v0.8.4 incident had.
        self.assertIn('Read-only', spec['description'])

    def test_the_mcp_tool_returns_the_report(self):
        from app.blueprints import mcp_server
        from unittest import mock
        with mock.patch.object(mcp_server, '_erp_client_or_error',
                               return_value=self.erp):
            payload, line = mcp_server.TOOLS[
                'get_statement_recon_report']['handler']({})
        self.assertEqual(payload['status'], 'warn')
        self.assertIn('diverge', line)
        self.assertIn('NEW finding', line)

    def test_findings_only_drops_the_matched_rows(self):
        from app.blueprints import mcp_server
        from unittest import mock
        with mock.patch.object(mcp_server, '_erp_client_or_error',
                               return_value=self.erp):
            payload, _ = mcp_server.TOOLS[
                'get_statement_recon_report']['handler'](
                    {'findings_only': True})
        self.assertTrue(payload['rows'])
        self.assertTrue(all(r['status'] != 'matched' for r in payload['rows']))


if __name__ == '__main__':
    unittest.main()
