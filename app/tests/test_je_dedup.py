# SPDX-License-Identifier: MIT
"""The Bank-Transaction / marker dedup guard (v0.8.5).

THE INCIDENT. The v0.8.4 sync re-emitted 112 pre-2024-12-01 settlement-leg
drafts that were duplicates-by-effect of the aggregate reconciliation JEs, and
they were cleaned up by hand, one delete at a time. The local
`GeneratedJournalEntry` row is supposed to stop that, but it is exactly what
`reset_investment_drafts` deletes on purpose — so when the tracker is gone,
ERPNext is the only thing that still knows.

What these cover, in the order the requirements name them:

  * dedup SKIPS when the existing JE is a DRAFT (docstatus 0)
  * dedup SKIPS when the existing JE is SUBMITTED (docstatus 1)
  * dedup ALLOWS creation when the existing JE was CANCELLED (docstatus 2) —
    cancelling is how an operator says "replace this", and a guard that treated
    a cancelled entry as an occupant would make that impossible
  * FAIL SAFE: an ERPNext that cannot answer allows the write, every time
  * FAIL FORWARD: a skip leaves an AuditEvent naming the pipeline, the identity
    key and the entry that already existed
  * the trade pipeline is deliberately NOT deduped

Synthetic tickers and round amounts only.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import db, invest_je, je_dedup  # noqa: E402
from app.erpnext_client import ERPNextAPIError  # noqa: E402
from app.models import AuditEvent, GeneratedJournalEntry  # noqa: E402

from tests.test_invest_je import COMPANY, InvestJEBase  # noqa: E402
from tests.test_settlement_leg import SettlementLegBase  # noqa: E402


# ── the marker itself ───────────────────────────────────────────────────────

class MarkerTests(unittest.TestCase):
    def test_the_marker_is_bracketed_and_namespaced(self):
        """The `like` lookup wraps this in wildcards, so it has to be a shape
        no merchant name or security label can accidentally contain."""
        self.assertEqual('[BB:inv:abc123]', je_dedup.marker('inv', 'abc123'))

    def test_stamping_appends_and_never_prepends(self):
        """`invest_je._sweep_orphan_drafts` recognizes an investment draft by
        what its remark STARTS with. A marker in front would hide every future
        draft from the sweep that cleans up after a reset."""
        out = je_dedup.stamp('Bought 100 TEST-AAPL at $10.00', 'inv', 'x1')
        self.assertTrue(out.startswith('Bought '))
        self.assertTrue(out.endswith('[BB:inv:x1]'))

    def test_stamping_twice_stamps_once(self):
        once = je_dedup.stamp('Cash: sweep', 'inv', 'x1')
        self.assertEqual(once, je_dedup.stamp(once, 'inv', 'x1'))


# ── the lookups, against a fake ledger ──────────────────────────────────────

class LookupTests(InvestJEBase):
    def _je(self, name, *, accounts=None, remark='', docstatus=0):
        self.client = getattr(self, 'client', None) or self._client()
        self.client.created['Journal Entry'][name] = {
            'company': COMPANY, 'user_remark': remark,
            'accounts': accounts or []}
        if docstatus == 1:
            self.client.submitted.add(name)
        elif docstatus == 2:
            self.client.cancelled.add(name)
        return name

    def _bt_line(self, bt):
        return {'account': 'Cash - EC', 'reference_type': 'Bank Transaction',
                'reference_name': bt}

    def test_a_draft_referencing_the_bank_transaction_is_found(self):
        self._je('ACC-JV-7001', accounts=[self._bt_line('ACC-BTN-0001')])
        hit = je_dedup.find_by_bank_transaction(self.client, 'ACC-BTN-0001')
        self.assertIsNotNone(hit)
        self.assertEqual('ACC-JV-7001', hit['journal_entry'])
        self.assertEqual('draft', hit['state'])

    def test_a_submitted_entry_is_found_too(self):
        """A submitted duplicate is worse than a draft one — it is already in
        the GL — so it must be at least as visible to the guard."""
        self._je('ACC-JV-7002', accounts=[self._bt_line('ACC-BTN-0002')],
                 docstatus=1)
        hit = je_dedup.find_by_bank_transaction(self.client, 'ACC-BTN-0002')
        self.assertIsNotNone(hit)
        self.assertEqual('submitted', hit['state'])

    def test_a_cancelled_entry_is_not_a_duplicate(self):
        """Cancelling is the operator saying 'that posting was wrong, replace
        it'. Counting it as an occupant leaves them no way to."""
        self._je('ACC-JV-7003', accounts=[self._bt_line('ACC-BTN-0003')],
                 docstatus=2)
        self.assertIsNone(
            je_dedup.find_by_bank_transaction(self.client, 'ACC-BTN-0003'))

    def test_a_different_bank_transaction_does_not_match(self):
        self._je('ACC-JV-7004', accounts=[self._bt_line('ACC-BTN-0004')])
        self.assertIsNone(
            je_dedup.find_by_bank_transaction(self.client, 'ACC-BTN-9999'))

    def test_an_empty_key_never_queries(self):
        """A transaction with no ERPNext Bank Transaction yet has no identity to
        dedup on, and asking ERPNext for '' would match arbitrary rows."""
        client = self._client()
        self.assertIsNone(je_dedup.find_by_bank_transaction(client, ''))
        self.assertEqual([], [c for c in client.calls if c[0] == 'list_docs'])

    def test_the_marker_lookup_finds_a_live_draft(self):
        self._je('ACC-JV-7005', remark='Cash: sweep $100.00 [BB:inv:itx-1]')
        hit = je_dedup.find_by_marker(self.client, 'inv', 'itx-1')
        self.assertIsNotNone(hit)
        self.assertEqual('ACC-JV-7005', hit['journal_entry'])

    def test_the_marker_lookup_ignores_a_cancelled_one(self):
        self._je('ACC-JV-7006', remark='Cash: sweep [BB:inv:itx-2]',
                 docstatus=2)
        self.assertIsNone(je_dedup.find_by_marker(self.client, 'inv', 'itx-2'))

    def test_an_unreachable_erpnext_allows_the_write(self):
        """FAIL SAFE, stated as a test. A duplicate draft is visible and
        deletable; a Journal Entry that was never written because the network
        blipped is invisible until a statement fails to reconcile."""
        client = self._client()
        with mock.patch.object(client, 'list_docs',
                               side_effect=ERPNextAPIError('down',
                                                           status_code=None)):
            self.assertIsNone(
                je_dedup.find_by_bank_transaction(client, 'ACC-BTN-0001'))
            self.assertIsNone(je_dedup.find_by_marker(client, 'inv', 'itx-1'))

    def test_even_an_unexpected_exception_allows_the_write(self):
        """Same principle, one layer wider: the guard must not be able to block
        a legitimate entry for ANY reason, including one we didn't anticipate."""
        client = self._client()
        with mock.patch.object(client, 'list_docs',
                               side_effect=RuntimeError('boom')):
            self.assertIsNone(
                je_dedup.find_by_bank_transaction(client, 'ACC-BTN-0001'))


# ── the investment settlement-leg pipeline ──────────────────────────────────

class SettlementLegDedupTests(SettlementLegBase):
    """The pipeline the v0.8.4 incident came out of."""

    def _forget_the_tracker(self):
        """What `reset_investment_drafts` does — and the exact state in which
        the local idempotency guard stops guarding anything."""
        GeneratedJournalEntry.query.delete()
        db.session.commit()

    def test_a_re_emission_is_skipped_when_the_draft_still_exists(self):
        client = self._client()
        self._security()
        txn = self._settlement('t-settle', 9590.00, 'withdrawal')
        first = invest_je.generate_investment_je(client, txn)
        self.assertIsNotNone(first.erpnext_journal_entry_name)
        created_first = len(client.created['Journal Entry'])

        self._forget_the_tracker()
        again = invest_je.generate_investment_je(client, txn)
        self.assertEqual('dedup_skipped', again.state)
        self.assertIsNone(again.erpnext_journal_entry_name)
        self.assertEqual(created_first, len(client.created['Journal Entry']))

    def test_a_re_emission_is_skipped_when_the_entry_is_submitted(self):
        client = self._client()
        self._security()
        txn = self._settlement('t-settle-sub', 1000.00, 'deposit')
        first = invest_je.generate_investment_je(client, txn)
        client.submitted.add(first.erpnext_journal_entry_name)

        self._forget_the_tracker()
        again = invest_je.generate_investment_je(client, txn)
        self.assertEqual('dedup_skipped', again.state)
        self.assertIn('submitted', again.error_message)

    def test_a_cancelled_entry_lets_the_leg_post_again(self):
        """The recovery path. An operator who cancelled a wrong settlement leg
        must be able to get a correct one."""
        client = self._client()
        self._security()
        txn = self._settlement('t-settle-can', 2500.00, 'withdrawal')
        first = invest_je.generate_investment_je(client, txn)
        client.cancelled.add(first.erpnext_journal_entry_name)

        self._forget_the_tracker()
        again = invest_je.generate_investment_je(client, txn)
        self.assertNotEqual('dedup_skipped', again.state)
        self.assertIsNotNone(again.erpnext_journal_entry_name)
        self.assertNotEqual(first.erpnext_journal_entry_name,
                            again.erpnext_journal_entry_name)

    def test_the_skip_leaves_an_audit_row_naming_the_existing_entry(self):
        """FAIL FORWARD. The drafts get deleted and the log rotates; this row is
        what is left to answer 'should that pipeline have re-emitted at all?'."""
        client = self._client()
        self._security()
        txn = self._settlement('t-settle-audit', 4000.00, 'withdrawal')
        first = invest_je.generate_investment_je(client, txn)
        original = first.erpnext_journal_entry_name
        self._forget_the_tracker()
        invest_je.generate_investment_je(client, txn)

        ev = (AuditEvent.query
              .filter_by(event_type='journal_entry_dedup_skipped').first())
        self.assertIsNotNone(ev)
        self.assertIn(original, ev.payload_after)
        self.assertIn('invest_je settlement leg', ev.payload_after)
        self.assertIn('t-settle-audit', ev.payload_after)

    def test_a_trade_is_deliberately_not_deduped(self):
        """Trade JEs were re-emitted by the same v0.8.4 sync and we do not yet
        know whether any of that was legitimate. Blocking one on that guess is
        the fail-UNSAFE direction, so the guard stays off until we know."""
        client = self._client()
        self._security()
        txn = self._txn('t-buy', 'buy', 9590.00, qty=100, price=95.90)
        first = invest_je.generate_investment_je(client, txn).\
            erpnext_journal_entry_name
        self._forget_the_tracker()
        again = invest_je.generate_investment_je(client, txn)
        self.assertNotEqual('dedup_skipped', again.state)
        self.assertNotEqual(first, again.erpnext_journal_entry_name)
        self.assertEqual(2, len(client.created['Journal Entry']))

    def test_the_settlement_leg_predicate_agrees_with_the_builder(self):
        """One definition, two callers. A predicate that drifted from the
        builder would either dedup dividends or miss real legs."""
        self._security()
        cases = [('withdrawal', True), ('deposit', True),
                 ('dividend', False), ('interest', False), ('', False)]
        for subtype, expected in cases:
            with self.subTest(subtype=subtype):
                txn = self._settlement(f't-p-{subtype or "none"}', 100.0,
                                       subtype)
                self.assertEqual(
                    expected, invest_je.is_settlement_leg(txn, self.brk))

    def test_an_unpaired_brokerage_has_no_settlement_legs_to_dedup(self):
        self.brk.paired_account_id = ''
        db.session.commit()
        self._security()
        txn = self._settlement('t-unpaired', 100.0, 'withdrawal')
        self.assertFalse(invest_je.is_settlement_leg(txn, self.brk))

    def test_the_post_pass_reports_the_skip_separately(self):
        """`skipped` means 'a row type we don't post' and is routine.
        `dedup_skipped` means the tracker and the ledger had drifted apart,
        which is the line worth reading a summary for."""
        client = self._client()
        self._security()
        self._settlement('t-sum', 700.00, 'withdrawal')
        invest_je.post_investments_for_account(client, 'brk')
        self._forget_the_tracker()
        stats = invest_je.post_investments_for_account(client, 'brk')
        self.assertEqual(1, stats['dedup_skipped'])
        self.assertEqual(0, stats['posted'])

    def test_an_unreadable_ledger_still_posts_the_leg(self):
        """FAIL SAFE on the pipeline, not just on the lookup."""
        client = self._client()
        self._security()
        txn = self._settlement('t-safe', 300.00, 'withdrawal')
        with mock.patch.object(je_dedup, 'find_by_marker',
                               return_value=None) as guard:
            gje = invest_je.generate_investment_je(client, txn)
        self.assertTrue(guard.called)
        self.assertIsNotNone(gje.erpnext_journal_entry_name)

    def test_the_marker_rides_the_remark_of_every_investment_je(self):
        """Trades carry it too. A marker only some entries have is one nothing
        can rely on later."""
        client = self._client()
        self._security()
        buy = self._txn('t-mark', 'buy', 100.0, qty=1, price=100.0)
        gje = invest_je.generate_investment_je(client, buy)
        remark = self._je_for(client, gje)['user_remark']
        self.assertIn('[BB:inv:t-mark]', remark)
        self.assertTrue(remark.startswith('Bought '))


# ── the bank-side categorization pipeline ───────────────────────────────────

class CategorizationDedupTests(InvestJEBase):
    """The other half of the requirement: the same guard on the rules engine."""

    def _rule_and_row(self, bt='ACC-BTN-5001'):
        from app.models import BankTransaction, CategorizationRule
        rule = CategorizationRule(
            name='Fuel', match_type='merchant_exact', match_value='TESTCO FUEL',
            offset_account='Fuel - EC', offset_direction='always_debit',
            priority=10, active=True)
        db.session.add(rule)
        row = BankTransaction(
            plaid_transaction_id='ptx-1', account_id='cash', amount=120.0,
            date=date(2026, 7, 10), name='TESTCO FUEL', merchant_name='TESTCO FUEL',
            erpnext_bank_transaction_id=bt, posted_at=date(2026, 7, 10))
        db.session.add(row)
        db.session.commit()
        return rule, row

    def _stage_existing_je(self, client, bt, *, docstatus=0):
        client.created['Journal Entry']['ACC-JV-8001'] = {
            'company': COMPANY, 'user_remark': 'aggregate reconciliation',
            'accounts': [{'account': 'Fuel - EC',
                          'reference_type': 'Bank Transaction',
                          'reference_name': bt}]}
        if docstatus == 1:
            client.submitted.add('ACC-JV-8001')
        elif docstatus == 2:
            client.cancelled.add('ACC-JV-8001')

    def _generate(self, client, row):
        from app import categorization
        return categorization.generate_journal_entry(client, row)

    def test_it_skips_when_a_draft_already_references_the_bank_transaction(self):
        client = self._client()
        _, row = self._rule_and_row()
        self._stage_existing_je(client, 'ACC-BTN-5001')
        gje = self._generate(client, row)
        self.assertEqual('dedup_skipped', gje.state)
        self.assertIsNone(gje.erpnext_journal_entry_name)
        self.assertIn('ACC-JV-8001', gje.error_message)

    def test_it_skips_when_the_existing_entry_is_submitted(self):
        client = self._client()
        _, row = self._rule_and_row('ACC-BTN-5002')
        self._stage_existing_je(client, 'ACC-BTN-5002', docstatus=1)
        gje = self._generate(client, row)
        self.assertEqual('dedup_skipped', gje.state)
        self.assertIn('submitted', gje.error_message)

    def test_it_creates_when_the_existing_entry_was_cancelled(self):
        client = self._client()
        _, row = self._rule_and_row('ACC-BTN-5003')
        self._stage_existing_je(client, 'ACC-BTN-5003', docstatus=2)
        gje = self._generate(client, row)
        self.assertNotEqual('dedup_skipped', gje.state)
        self.assertIsNotNone(gje.erpnext_journal_entry_name)

    def test_it_creates_when_nothing_references_the_bank_transaction(self):
        client = self._client()
        _, row = self._rule_and_row('ACC-BTN-5004')
        gje = self._generate(client, row)
        self.assertEqual('pending_review', gje.state)
        self.assertIsNotNone(gje.erpnext_journal_entry_name)

    def test_a_skip_is_reversible_by_the_operator_who_disagrees(self):
        """A guard the operator cannot overrule is a wall. `rerun_rules` treats
        a settled skip as done, so without Retry the row would be a dead end
        even after the duplicate was cancelled."""
        client = self._client()
        _, row = self._rule_and_row('ACC-BTN-5006')
        self._stage_existing_je(client, 'ACC-BTN-5006')
        gje = self._generate(client, row)
        self.assertEqual('dedup_skipped', gje.state)
        gid = gje.id

        http = self.app.test_client()
        from app import sync_engine
        with mock.patch.object(sync_engine, 'get_erp_client_or_none',
                               return_value=client):
            # Still duplicated → the retry declines, and says why.
            resp = http.post('/admin/generated_entries/retry',
                             data={'id': gid},
                             headers={'Accept': 'application/json'})
            self.assertEqual(409, resp.status_code)
            self.assertIn('Cancel that entry', resp.get_json()['message'])

            # Operator cancels the duplicate → the retry now writes the JE.
            client.cancelled.add('ACC-JV-8001')
            resp = http.post('/admin/generated_entries/retry',
                             data={'id': gid},
                             headers={'Accept': 'application/json'})
            self.assertEqual(200, resp.status_code)
        refreshed = db.session.get(GeneratedJournalEntry, gid)
        self.assertEqual('pending_review', refreshed.state)
        self.assertIsNotNone(refreshed.erpnext_journal_entry_name)

    def test_a_rerun_does_not_re_ask_about_a_settled_skip(self):
        """A dedup_skipped row is DONE. Without that, every rerun would re-query
        ERPNext about the same transaction forever and re-audit the same skip —
        chronos where the decision has already been made."""
        from app import categorization, erpnext_settings
        client = self._client()
        _, row = self._rule_and_row('ACC-BTN-5005')
        self._stage_existing_je(client, 'ACC-BTN-5005')
        self._generate(client, row)
        erpnext_settings.set_je_generation(True)
        stats = categorization.rerun_rules(client)
        self.assertEqual(0, stats['considered'])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
