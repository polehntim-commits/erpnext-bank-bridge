# SPDX-License-Identifier: MIT
"""Statement-derived transaction date override (v0.5.3).

Plaid and the bank routinely file the same transaction in DIFFERENT statement
windows — a wire the bank dates Dec 31 and Plaid dates Jan 2 lands in opposite
reconciliation periods, leaving +$712 in one month and −$712 in the next. This
release parses the bank's own Activity Detail, matches each line to the Plaid
BankTransaction, and stamps the bank's posted date so the anchor engine counts
it in the month the statement assigns.

Covered here:

  * the Activity Detail parser: glued MM/DD dates, two-line rows, the sweep
    section excluded, year resolved against the period
  * the matcher: amount (sign-flipped) + ±5 days + description overlap;
    matched / ambiguous / no_match
  * the anchor engine reading the effective date
  * the Dec/Jan ±$712 mismatch collapsing to $0
  * the kill switch leaving Plaid's dates untouched when off

Synthetic merchants (MERCHANT A / TEST PAYEE) and round amounts only.

    cd app
    python3 -m unittest discover -s tests -v
"""
from datetime import date

from app import db, statements as stmts
from app.models import (BankTransaction, PlaidAccount, PlaidItem,
                        PlaidStatement, StatementTransaction)

from tests.test_statements import StatementsBase, make_pdf


def activity_pdf(rows, period='JUNE 1, 2026 - JUNE 30, 2026', extra=None):
    """A statement PDF with an Activity Detail section. `rows` are raw flattened
    lines exactly as pypdf would emit them (date glued into the middle)."""
    lines = ['Page 5 of 9', 'TEST CLIENT LLC', period, 'Activity detail',
             'ATM and CheckCard activity',
             'DATE ACCOUNT TYPE TRANSACTION DESCRIPTION AMOUNT']
    lines += rows
    lines.append('Total ATM and CheckCard activity: -$0.00')
    lines += (extra or [])
    return make_pdf(lines)


class ActivityDetailParserTest(StatementsBase):
    def test_extracts_rows_with_the_banks_posted_date(self):
        pdf = activity_pdf([
            'VISA CHECK CARD MERCHANT A06/03 Cash -274.99',
            '06/02', 'S585150556137221', 'PURCHASE', 'AUTHORIZED ON 05/30',
            'VISA CHECK CARD MERCHANT B06/15 Cash -52.98',
            '06/14', 'S586148774338988'])
        rows = stmts.parse_activity_detail(pdf, date(2026, 6, 1),
                                           date(2026, 6, 30))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['posted_date'], date(2026, 6, 3))
        self.assertEqual(rows[0]['amount'], -274.99)
        self.assertIn('merchant a', rows[0]['description'])
        self.assertEqual(rows[1]['posted_date'], date(2026, 6, 15))

    def test_check_rows_parse(self):
        pdf = activity_pdf([], extra=[
            'Withdrawals by check',
            'DATE ACCOUNT TYPE CHECK NUMBER DESCRIPTION EXPENSE CODE AMOUNT',
            '0001015 TEST PAYEE Unspecified06/20 Cash -712.00',
            'Total Withdrawals by check: -$712.00'])
        rows = stmts.parse_activity_detail(pdf, date(2026, 6, 1),
                                           date(2026, 6, 30))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['amount'], -712.00)
        self.assertEqual(rows[0]['posted_date'], date(2026, 6, 20))
        self.assertEqual(rows[0]['section'], 'checks')

    def test_the_sweep_running_balance_rows_are_excluded(self):
        """The v0.4.41 trap: 'Cash sweep activity' rows are running balances,
        not transactions, and hitting that section ends parsing."""
        pdf = activity_pdf(
            ['VISA CHECK CARD MERCHANT A06/03 Cash -274.99'],
            extra=['Cash sweep activity',
                   'BEGINNING BALANCE TRANSFER FROM BANK DEPOSIT SWEEP06/01 '
                   '15,434.91 06/09 -80.00',
                   '06/01 BEGINNING BALANCE $0.00',
                   '06/30 ENDING BALANCE $0.00'])
        rows = stmts.parse_activity_detail(pdf, date(2026, 6, 1),
                                           date(2026, 6, 30))
        self.assertEqual(len(rows), 1)   # only the real card transaction

    def test_summary_figures_are_not_mistaken_for_transactions(self):
        """The Cash-flow-summary labels ('Income and distributions', 'Other
        additions') collide with Activity Detail section names — but carry
        column amounts, not a MM/DD posted date, so none is parsed."""
        summary = make_pdf([
            'Cash flow summary THIS PERIOD THIS YEAR',
            'Opening value of cash and sweep balances $9,467.48',
            'Income and distributions 0.34 1.60',
            'Other additions 0.00 37,786.80',
            'Net additions to cash $0.34 $37,788.40',
            'ATM and CheckCard activity -849.53 -15,716.84',
            'Closing value of cash and sweep balances $7,793.51'])
        rows = stmts.parse_activity_detail(summary, date(2025, 6, 1),
                                           date(2025, 6, 30))
        self.assertEqual(rows, [])

    def test_year_resolves_across_a_december_boundary(self):
        pdf = activity_pdf(['VISA CHECK CARD MERCHANT A12/31 Cash -100.00'],
                           period='DECEMBER 1, 2025 - DECEMBER 31, 2025')
        rows = stmts.parse_activity_detail(pdf, date(2025, 12, 1),
                                           date(2025, 12, 31))
        self.assertEqual(rows[0]['posted_date'], date(2025, 12, 31))


class MatcherTest(StatementsBase):
    def setUp(self):
        super().setUp()
        self.brk = self._acct('brk', 'BROKERAGE', '9401', 'investment',
                              paired='cash')
        self.cash = self._acct('cash', 'BROKERAGE CASH', '3194', 'depository')

    def _acct(self, account_id, name, mask, type_, paired=None):
        a = PlaidAccount(account_id=account_id, item_id=self.item.item_id,
                         name=name, mask=mask, type=type_,
                         subtype='brokerage' if type_ == 'investment'
                         else 'checking', paired_account_id=paired)
        db.session.add(a)
        db.session.commit()
        return a

    def _statement(self, pdf, sid='st-jun', start=date(2026, 6, 1),
                   end=date(2026, 6, 30)):
        path = stmts.pdf_path_for('item-abc', 'brk', 'lbl', sid)
        stmts.store_pdf(path, pdf)
        st = PlaidStatement(statement_id=sid, plaid_item_id=self.item.item_id,
                            plaid_account_id='brk', period_start=start,
                            period_end=end, pdf_path=path, pdf_bytes=len(pdf))
        db.session.add(st)
        db.session.commit()
        return st

    def _bank(self, tid, amount, when, name='TXN', account_id='cash'):
        t = BankTransaction(plaid_transaction_id=tid, account_id=account_id,
                            amount=amount, date=when, name=name)
        db.session.add(t)
        db.session.commit()
        return t

    def test_matches_by_amount_sign_flip_and_proximity(self):
        st = self._statement(activity_pdf(
            ['VISA CHECK CARD MERCHANT A06/03 Cash -274.99']))
        # Plaid records the same withdrawal at +274.99, two days later.
        bank = self._bank('b1', 274.99, date(2026, 6, 5), name='MERCHANT A')
        stmts.store_statement_transactions(st)
        result = stmts.match_statement_to_bank_transactions(st)
        self.assertEqual(result['matched'], 1)
        db.session.refresh(bank)
        self.assertEqual(bank.statement_posted_date, date(2026, 6, 3))
        self.assertEqual(bank.statement_match_status, 'matched')

    def test_no_candidate_is_flagged_no_match(self):
        st = self._statement(activity_pdf(
            ['VISA CHECK CARD MERCHANT A06/03 Cash -274.99']))
        stmts.store_statement_transactions(st)
        result = stmts.match_statement_to_bank_transactions(st)
        self.assertEqual(result['no_match'], 1)
        stx = StatementTransaction.query.first()
        self.assertEqual(stx.match_status, 'no_match')

    def test_multiple_candidates_are_ambiguous_closest_wins(self):
        st = self._statement(activity_pdf(
            ['VISA CHECK CARD MERCHANT A06/10 Cash -100.00']))
        near = self._bank('near', 100.0, date(2026, 6, 11), name='MERCHANT A')
        self._bank('far', 100.0, date(2026, 6, 14), name='MERCHANT A')
        stmts.store_statement_transactions(st)
        result = stmts.match_statement_to_bank_transactions(st)
        self.assertEqual(result['ambiguous'], 1)
        db.session.refresh(near)
        self.assertEqual(near.statement_posted_date, date(2026, 6, 10))

    def test_the_kill_switch_off_stamps_nothing(self):
        self.item.statement_date_override_enabled = False
        db.session.commit()
        st = self._statement(activity_pdf(
            ['VISA CHECK CARD MERCHANT A06/03 Cash -274.99']))
        bank = self._bank('b1', 274.99, date(2026, 6, 5), name='MERCHANT A')
        stmts.store_statement_transactions(st)
        result = stmts.match_statement_to_bank_transactions(st)
        self.assertEqual(result['skipped'], 1)
        db.session.refresh(bank)
        self.assertIsNone(bank.statement_posted_date)

    def test_storing_is_idempotent(self):
        st = self._statement(activity_pdf(
            ['VISA CHECK CARD MERCHANT A06/03 Cash -274.99']))
        stmts.store_statement_transactions(st)
        stmts.store_statement_transactions(st)
        self.assertEqual(StatementTransaction.query.count(), 1)


class AnchorEffectiveDateTest(StatementsBase):
    """The payoff: a transaction the bank and Plaid date into different months
    is counted in the bank's, collapsing a ±$712 boundary mismatch to $0."""

    def setUp(self):
        super().setUp()
        for a in (('brk', 'BROKERAGE', 'investment', 'cash'),
                  ('cash', 'BROKERAGE CASH', 'depository', None)):
            acct = PlaidAccount(
                account_id=a[0], item_id=self.item.item_id, name=a[1],
                mask='9401', type=a[2],
                subtype='brokerage' if a[2] == 'investment' else 'checking',
                paired_account_id=a[3])
            db.session.add(acct)
        db.session.commit()

    def _statement(self, sid, start, end, cash_open, cash_close, pdf):
        path = stmts.pdf_path_for('item-abc', 'brk', sid, sid)
        stmts.store_pdf(path, pdf)
        st = PlaidStatement(statement_id=sid, plaid_item_id=self.item.item_id,
                            plaid_account_id='brk', period_start=start,
                            period_end=end, pdf_path=path, pdf_bytes=len(pdf))
        db.session.add(st)
        db.session.commit()
        st.parsed_metadata = {'cash_opening': cash_open,
                              'cash_closing': cash_close}
        db.session.commit()
        return st

    def test_the_dec_jan_712_mismatch_collapses(self):
        # December: opening 10,000 → closing 9,288, i.e. the $712 went out in
        # December according to the bank's statement.
        dec_pdf = activity_pdf(
            [], period='DECEMBER 1, 2025 - DECEMBER 31, 2025',
            extra=['Withdrawals by check',
                   'DATE ACCOUNT TYPE CHECK NUMBER DESCRIPTION EXPENSE CODE AMOUNT',
                   '0001015 TEST PAYEE Unspecified12/31 Cash -712.00',
                   'Total Withdrawals by check: -$712.00'])
        self._statement('st-dec', date(2025, 12, 1), date(2025, 12, 31),
                        10000.0, 9288.0, dec_pdf)
        jan_pdf = activity_pdf([], period='JANUARY 1, 2026 - JANUARY 31, 2026')
        self._statement('st-jan', date(2026, 1, 1), date(2026, 1, 31),
                        9288.0, 9288.0, jan_pdf)
        # Plaid mirrored the $712 withdrawal on Jan 2 (positive = out).
        db.session.add(BankTransaction(
            plaid_transaction_id='wire', account_id='cash', amount=712.0,
            date=date(2026, 1, 2), name='TEST PAYEE'))
        db.session.commit()

        # BEFORE any date override: Dec is short, Jan is over.
        stmts.rebuild_statement_anchors()
        by_period = {a.period_start: a
                     for a in stmts.anchors_for_account('brk')}
        self.assertEqual(by_period[date(2025, 12, 1)].variance, -712.0)
        self.assertEqual(by_period[date(2026, 1, 1)].variance, 712.0)

        # Run the matcher (stamps the bank's Dec 31 date) and rebuild.
        for sid in ('st-dec', 'st-jan'):
            st = PlaidStatement.query.filter_by(statement_id=sid).first()
            stmts.store_statement_transactions(st)
            stmts.match_statement_to_bank_transactions(st)
        db.session.commit()
        stmts.rebuild_statement_anchors()
        by_period = {a.period_start: a
                     for a in stmts.anchors_for_account('brk')}
        self.assertEqual(by_period[date(2025, 12, 1)].variance, 0.0)
        self.assertEqual(by_period[date(2026, 1, 1)].variance, 0.0)

    def test_match_all_and_reparse_runs_end_to_end(self):
        dec_pdf = activity_pdf(
            [], period='DECEMBER 1, 2025 - DECEMBER 31, 2025',
            extra=['Withdrawals by check',
                   'DATE ACCOUNT TYPE CHECK NUMBER DESCRIPTION EXPENSE CODE AMOUNT',
                   '0001015 TEST PAYEE Unspecified12/31 Cash -712.00',
                   'Total Withdrawals by check: -$712.00'])
        self._statement('st-dec', date(2025, 12, 1), date(2025, 12, 31),
                        10000.0, 9288.0, dec_pdf)
        db.session.add(BankTransaction(
            plaid_transaction_id='wire', account_id='cash', amount=712.0,
            date=date(2026, 1, 2), name='TEST PAYEE'))
        db.session.commit()
        totals = stmts.match_all_statements()
        self.assertEqual(totals['matched'], 1)


class MigrationDeclarationTest(StatementsBase):
    def test_columns_and_table_declared(self):
        from app.migrations import SCHEMA_MIGRATIONS
        cols = {(t, c) for t, c, _ in SCHEMA_MIGRATIONS}
        self.assertIn(('bank_transactions', 'statement_posted_date'), cols)
        self.assertIn(('bank_transactions', 'statement_match_status'), cols)
        self.assertIn(('plaid_items', 'statement_date_override_enabled'), cols)
        # the new table is built by create_all — it exists on a fresh test DB
        self.assertEqual(StatementTransaction.query.count(), 0)
