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


def activity_pdf(rows, period='JUNE 1, 2026 - JUNE 30, 2026', extra=None,
                 cash_open=None, cash_close=None):
    """A statement PDF with an Activity Detail section. `rows` are raw flattened
    lines exactly as pypdf would emit them (date glued into the middle).

    When `cash_open`/`cash_close` are given, the WF Advisors Cash-flow summary
    lines are prepended so a full re-parse recovers the anchored balances too —
    needed by tests that run reparse_stored (which re-parses the PDF)."""
    lines = ['Page 5 of 9', 'TEST CLIENT LLC', period]
    if cash_open is not None:
        lines += ['Cash flow summary THIS PERIOD THIS YEAR',
                  f'Opening value of cash and sweep balances ${cash_open:,.2f}',
                  f'Closing value of cash and sweep balances ${cash_close:,.2f}']
    lines += ['Activity detail', 'ATM and CheckCard activity',
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

    def test_relink_duplicate_twin_is_stamped_into_the_same_window(self):
        """v0.5.6 · a re-link mirrors the SAME purchase onto both account_ids of
        the chain. The matcher stamps one; its unstamped twin must be pulled
        into the same statement month (as 'duplicate') so dedupe collapses the
        pair — otherwise the effective-date filter splits them across two
        months and both get counted (the ••6030 May +$125.15 bug)."""
        cash2 = self._acct('cash2', 'BROKERAGE CASH 2', '3194', 'depository')
        self.cash.superseded_by_account_id = cash2.account_id
        db.session.commit()
        # The bank posts the 05/29 purchase on 06/01.
        st = self._statement(activity_pdf(
            ['VISA CHECK CARD MERCHANT A06/01 Cash -52.98']))
        a = self._bank('dupA', 52.98, date(2026, 5, 29), name='MERCHANT A',
                       account_id='cash')
        b = self._bank('dupB', 52.98, date(2026, 5, 29), name='MERCHANT A',
                       account_id='cash2')
        stmts.store_statement_transactions(st)
        result = stmts.match_statement_to_bank_transactions(st)
        self.assertEqual(result['duplicate'], 1)
        db.session.refresh(a); db.session.refresh(b)
        # Both copies now sit in June, and exactly one is flagged 'duplicate'.
        self.assertEqual(a.statement_posted_date, date(2026, 6, 1))
        self.assertEqual(b.statement_posted_date, date(2026, 6, 1))
        self.assertIn('duplicate',
                      {a.statement_match_status, b.statement_match_status})
        chain = ['cash', 'cash2']
        # dedupe collapses the pair: counted once in June, zero in May.
        self.assertEqual(
            stmts._bank_total(chain, date(2026, 6, 1), date(2026, 6, 30)), 52.98)
        self.assertEqual(
            stmts._bank_total(chain, date(2026, 5, 1), date(2026, 5, 31)), 0.0)

    def test_a_lone_match_stamps_no_duplicate(self):
        """No twin → no 'duplicate' side effect (guards against over-stamping
        genuine same-amount-but-distinct transactions)."""
        st = self._statement(activity_pdf(
            ['VISA CHECK CARD MERCHANT A06/03 Cash -274.99']))
        self._bank('b1', 274.99, date(2026, 6, 5), name='MERCHANT A')
        stmts.store_statement_transactions(st)
        result = stmts.match_statement_to_bank_transactions(st)
        self.assertEqual(result['duplicate'], 0)


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

    def test_reparse_stored_runs_the_whole_pipeline(self):
        """v0.5.4 regression: reparse_stored (the admin-button path) must itself
        store statement transactions, match them and rebuild anchors — the bug
        was that only reparse_stale did, so the button populated nothing."""
        dec_pdf = activity_pdf(
            [], period='DECEMBER 1, 2025 - DECEMBER 31, 2025',
            cash_open=10000.0, cash_close=9288.0,
            extra=['Withdrawals by check',
                   'DATE ACCOUNT TYPE CHECK NUMBER DESCRIPTION EXPENSE CODE AMOUNT',
                   '0001015 TEST PAYEE Unspecified12/31 Cash -712.00',
                   'Total Withdrawals by check: -$712.00'])
        self._statement('st-dec', date(2025, 12, 1), date(2025, 12, 31),
                        10000.0, 9288.0, dec_pdf)
        self._statement('st-jan', date(2026, 1, 1), date(2026, 1, 31),
                        9288.0, 9288.0,
                        activity_pdf([], period='JANUARY 1, 2026 - JANUARY 31, 2026',
                                     cash_open=9288.0, cash_close=9288.0))
        db.session.add(BankTransaction(
            plaid_transaction_id='wire', account_id='cash', amount=712.0,
            date=date(2026, 1, 2), name='TEST PAYEE'))
        db.session.commit()
        result = stmts.reparse_stored()
        # StatementTransaction populated, dates stamped, anchors collapsed.
        self.assertGreaterEqual(result['statement_transactions'], 1)
        self.assertEqual(result['matched'], 1)
        self.assertEqual(
            BankTransaction.query.filter_by(plaid_transaction_id='wire')
            .first().statement_posted_date, date(2025, 12, 31))
        by_period = {a.period_start: a
                     for a in stmts.anchors_for_account('brk')}
        self.assertEqual(by_period[date(2025, 12, 1)].variance, 0.0)
        self.assertEqual(by_period[date(2026, 1, 1)].variance, 0.0)

    def test_reparse_stored_survives_a_bad_pdf(self):
        """One corrupt PDF must not stall the batch (the 'hang' guard)."""
        good = self._statement('st-good', date(2026, 1, 1), date(2026, 1, 31),
                               100.0, 100.0, activity_pdf([]))
        bad = self._statement('st-bad', date(2026, 2, 1), date(2026, 2, 28),
                              100.0, 100.0, activity_pdf([]))
        with open(bad.pdf_path, 'wb') as fh:
            fh.write(b'not a pdf at all')
        result = stmts.reparse_stored()   # must not raise or hang
        self.assertGreaterEqual(result['examined'] + result['errors'], 1)

    def test_only_paired_brokerage_statements_are_activity_parsed(self):
        """The perf gate: a plain depository statement isn't activity-parsed."""
        solo = PlaidAccount(account_id='solo', item_id=self.item.item_id,
                            name='PLAIN CHECKING', mask='7777',
                            type='depository', subtype='checking')
        db.session.add(solo)
        db.session.commit()
        self.assertTrue(stmts.statement_needs_activity(
            PlaidStatement(plaid_account_id='brk')))
        self.assertFalse(stmts.statement_needs_activity(
            PlaidStatement(plaid_account_id='solo')))

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


class SynthesisTest(StatementsBase):
    """v0.5.5 · materializing a BankTransaction for a statement line Plaid
    dropped — but ONLY when it provably improves reconciliation.

    Synthetic merchants (TEST PAYEE) and round amounts only. The income line
    'INTEREST BANK DEPOSIT SWEEP' is the WF Advisors sweep-interest wording,
    not a party name."""

    def setUp(self):
        super().setUp()
        for a in (('brk', 'BROKERAGE', 'investment', 'cash'),
                  ('cash', 'BROKERAGE CASH', 'depository', None)):
            db.session.add(PlaidAccount(
                account_id=a[0], item_id=self.item.item_id, name=a[1],
                mask='9401', type=a[2],
                subtype='brokerage' if a[2] == 'investment' else 'checking',
                paired_account_id=a[3]))
        db.session.commit()

    def _statement(self, pdf, cash_open, cash_close, sid='st-jun',
                   start=date(2026, 6, 1), end=date(2026, 6, 30)):
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

    # An income line Plaid never returned: +$0.34 into the account (holder
    # view). cash_close is $0.34 above cash_open, so the period is short by
    # exactly that — the synthesized row should close it.
    _INCOME = ['Income and distributions',
               'DATE ACCOUNT TYPE TRANSACTION DESCRIPTION AMOUNT',
               'INTEREST BANK DEPOSIT SWEEP06/30 Cash 0.34']

    def _seed_income(self, cash_open=1000.0, cash_close=1000.34):
        st = self._statement(activity_pdf([], extra=self._INCOME),
                             cash_open, cash_close)
        stmts.store_statement_transactions(st)
        stmts.match_statement_to_bank_transactions(st)   # → no_match
        return st

    def test_synthesizes_a_dropped_line_that_reduces_variance(self):
        self._seed_income()
        result = stmts.synthesize_missing_transactions()
        self.assertEqual(result['created'], 1)
        synth = BankTransaction.query.filter_by(source='statement').one()
        self.assertEqual(synth.account_id, 'cash')       # the companion
        self.assertEqual(synth.amount, -0.34)            # holder +0.34 negated
        self.assertEqual(synth.date, date(2026, 6, 30))
        self.assertIsNotNone(synth.source_statement_txn_id)
        self.assertEqual(synth.statement_posted_date, date(2026, 6, 30))
        # The payoff: the period now reconciles.
        stmts.rebuild_statement_anchors()
        anc = {a.period_start: a for a in stmts.anchors_for_account('brk')}
        self.assertEqual(anc[date(2026, 6, 1)].variance, 0.0)

    def test_the_guard_declines_when_it_would_worsen_a_reconciled_period(self):
        """The internal-journal case: a big no_match line in a period that
        ALREADY balances is NOT synthesized — building it would swing variance
        from $0 to the line's size."""
        journal = ['Other additions',
                   'DATE ACCOUNT TYPE TRANSACTION DESCRIPTION AMOUNT',
                   'JOURNAL FROM ACCOUNT06/15 Cash 5,000.00']
        st = self._statement(activity_pdf([], extra=journal),
                             cash_open=1000.0, cash_close=1000.0)  # balanced
        stmts.store_statement_transactions(st)
        stmts.match_statement_to_bank_transactions(st)
        result = stmts.synthesize_missing_transactions()
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['skipped_guard'], 1)
        self.assertEqual(BankTransaction.query.filter_by(
            source='statement').count(), 0)

    def test_sign_flips_for_a_dropped_withdrawal_too(self):
        """A dropped WITHDRAWAL (holder view negative) becomes a positive Plaid
        amount — the negation holds in both directions."""
        card = ['VISA CHECK CARD TEST PAYEE06/10 Cash -200.00']
        st = self._statement(activity_pdf(card), cash_open=1000.0,
                             cash_close=800.0)   # $200 left, Plaid missed it
        stmts.store_statement_transactions(st)
        stmts.match_statement_to_bank_transactions(st)
        stmts.synthesize_missing_transactions()
        synth = BankTransaction.query.filter_by(source='statement').one()
        self.assertEqual(synth.amount, 200.0)    # holder −200 negated to +200

    def test_idempotent_no_duplicate_on_rerun(self):
        self._seed_income()
        stmts.synthesize_missing_transactions()
        stmts.synthesize_missing_transactions()
        self.assertEqual(BankTransaction.query.filter_by(
            source='statement').count(), 1)

    def test_plaid_wins_the_synth_row_is_reaped_when_the_feed_arrives(self):
        st = self._seed_income()
        stmts.synthesize_missing_transactions()
        self.assertEqual(BankTransaction.query.filter_by(
            source='statement').count(), 1)
        # The real feed finally delivers the interest, on the companion.
        db.session.add(BankTransaction(
            plaid_transaction_id='real-int', account_id='cash', amount=-0.34,
            date=date(2026, 6, 30), name='INTEREST BANK DEPOSIT SWEEP'))
        db.session.commit()
        # Re-match (st flips to matched) then re-synthesize (reaps the synth).
        stmts.match_statement_to_bank_transactions(st)
        result = stmts.synthesize_missing_transactions()
        self.assertEqual(result['reaped'], 1)
        self.assertEqual(BankTransaction.query.filter_by(
            source='statement').count(), 0)
        # Exactly one row for the event remains — the Plaid one.
        self.assertEqual(BankTransaction.query.filter_by(
            account_id='cash', amount=-0.34).count(), 1)

    def test_fingerprint_dedupe_never_builds_a_second_copy(self):
        """If a feed row with the identical (date, amount, name) fingerprint is
        already present, synthesis declines — count stays exactly 1."""
        st = self._statement(activity_pdf([], extra=self._INCOME),
                             cash_open=1000.0, cash_close=1000.34)
        stmts.store_statement_transactions(st)
        db.session.add(BankTransaction(
            plaid_transaction_id='real-int', account_id='cash', amount=-0.34,
            date=date(2026, 6, 30), name='INTEREST BANK DEPOSIT SWEEP'))
        db.session.commit()
        # Force the no_match state (the fingerprint guard is defence-in-depth
        # behind the matcher, which would normally pair this itself).
        stx = StatementTransaction.query.filter_by(statement_id=st.id).one()
        stx.match_status = 'no_match'
        db.session.commit()
        result = stmts.synthesize_missing_transactions()
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['skipped_dedupe'], 1)
        self.assertEqual(BankTransaction.query.filter_by(
            source='statement').count(), 0)

    def test_kill_switch_off_synthesizes_nothing(self):
        self.item.statement_derived_backfill_enabled = False
        db.session.commit()
        self._seed_income()
        result = stmts.synthesize_missing_transactions()
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['skipped_disabled'], 1)
        self.assertEqual(BankTransaction.query.filter_by(
            source='statement').count(), 0)

    def test_rules_engine_tags_a_synth_row(self):
        """A synth row is an ordinary BankTransaction: the categorization rules
        fire on it exactly as on a feed row."""
        from app.models import CategorizationRule
        db.session.add(CategorizationRule(
            name='Sweep interest', match_type='description_regex',
            match_value='INTEREST', bb_internal_tag='sweep-interest',
            priority=10))
        db.session.commit()
        self._seed_income()
        stmts.synthesize_missing_transactions()
        synth = BankTransaction.query.filter_by(source='statement').one()
        self.assertEqual(synth.bb_internal_tag, 'sweep-interest')

    def test_transactions_page_renders_the_badge_and_origin_filter(self):
        """Plumbing check: the page GETs 200 (no _page reserved-kwarg clash)
        and shows the synth row's badge under the origin filter."""
        self._seed_income()
        stmts.synthesize_missing_transactions()
        client = self.app.test_client()
        for url in ('/admin/transactions',
                    '/admin/transactions?source=statement',
                    '/admin/transactions?source=plaid'):
            resp = client.get(url)
            self.assertEqual(resp.status_code, 200, url)
        body = client.get('/admin/transactions?source=statement').data.decode()
        self.assertIn('📄 statement', body)        # the badge on the synth row
        self.assertIn('interest bank deposit sweep', body)  # parser lowercases


class MigrationDeclarationTest(StatementsBase):
    def test_columns_and_table_declared(self):
        from app.migrations import SCHEMA_MIGRATIONS
        cols = {(t, c) for t, c, _ in SCHEMA_MIGRATIONS}
        self.assertIn(('bank_transactions', 'statement_posted_date'), cols)
        self.assertIn(('bank_transactions', 'statement_match_status'), cols)
        self.assertIn(('plaid_items', 'statement_date_override_enabled'), cols)
        # v0.5.5 · synthesis columns + kill switch
        self.assertIn(('bank_transactions', 'source'), cols)
        self.assertIn(('bank_transactions', 'source_statement_txn_id'), cols)
        self.assertIn(('plaid_items', 'statement_derived_backfill_enabled'),
                      cols)
        # the new table is built by create_all — it exists on a fresh test DB
        self.assertEqual(StatementTransaction.query.count(), 0)
