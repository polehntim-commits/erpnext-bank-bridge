# SPDX-License-Identifier: MIT
"""The ERPNext consolidation — the outward leg (v1.0.0).

ERPNext is the source of truth for reconciliation, pairings and account
topology; Bank Bridge pushes them to it. What is tested here is not "does the
HTTP call happen" but the three properties that make that arrangement
trustworthy:

  * A PUSH NEVER FAILS ITS CALLER. A rebuild against an ERPNext that is down,
    refusing, or not deployed at all still rebuilds — and still commits.
  * A FAILED PUSH IS NOT A LOST FACT. It is queued with exponential backoff,
    deduplicated per target, and drained on the next sync, at which point the
    source row is stamped so it is not pushed a second time.
  * AN UNCHANGED FACT IS NOT PUSHED. The fingerprint is what stops
    rebuild_statement_anchors — which runs after every reparse and every
    pairing change — from re-writing 27 identical periods every time.

Plus the sign convention (plan §7), which must survive the trip verbatim: a
flip introduced here would corrupt the reconciliation truth in the system that
is now authoritative for it.

Synthetic masks (4242 / 4200) and names only — no real account data.

    cd app
    python3 -m unittest tests.test_erpnext_push -v
"""
import json
from datetime import date, datetime, timedelta

from app import db, erpnext_push, erpnext_settings
from app import statements as stmts
from app.models import (ErpnextPushQueue, PlaidAccount, PlaidStatement,
                        StatementAnchor)

from tests.fakes import FakeERPClient
from tests.test_statements import StatementsBase


class PushBase(StatementsBase):
    def setUp(self):
        super().setUp()
        # No real sleeping between the one in-call retry.
        erpnext_push.PUSH_RETRY_BACKOFFS = ()
        self.addCleanup(setattr, erpnext_push, 'PUSH_RETRY_BACKOFFS', (2,))
        self.erp = FakeERPClient()

    def _account(self, mask='4242', *, bank_account='WF Brokerage - EC',
                 account_id=None, subtype='brokerage'):
        acct = PlaidAccount(
            account_id=account_id or f'acc-{mask}', item_id=self.item.item_id,
            name=f'TEST {subtype.upper()}', mask=mask, type='investment',
            subtype=subtype, owning_company='Example Company LLC',
            erpnext_bank_account_name=bank_account)
        db.session.add(acct)
        db.session.commit()
        return acct

    def _anchor(self, acct, *, period=(date(2026, 6, 1), date(2026, 6, 30)),
                opening=1000.0, closing=1200.0, txn_sum=150.0):
        st = PlaidStatement(statement_id=f'st-{acct.mask}-{period[0]}',
                            plaid_item_id=self.item.item_id,
                            plaid_account_id=acct.account_id,
                            period_start=period[0], period_end=period[1])
        db.session.add(st)
        db.session.commit()
        anchor = StatementAnchor(
            account_id=acct.account_id, statement_id=st.id,
            period_start=period[0], period_end=period[1],
            anchored_opening=opening, anchored_closing=closing,
            transaction_sum=txn_sum, computed_closing=opening + txn_sum,
            variance=round(closing - (opening + txn_sum), 2),
            parser_version='0.5.3')
        db.session.add(anchor)
        db.session.commit()
        return anchor

    def _pushes(self, method=erpnext_push.ANCHOR_METHOD):
        return [payload for m, payload in self.erp.method_calls if m == method]


# ── the payload ─────────────────────────────────────────────────────────────

class AnchorPayloadTest(PushBase):
    def test_the_sign_convention_survives_the_trip(self):
        """Plan §7: positive is money IN, computed = opening + sum, variance =
        closing - computed. ERPNext's amount_signed already agrees, so there is
        nothing to flip — and this is the one place a flip could be introduced
        by accident."""
        acct = self._account()
        anchor = self._anchor(acct, opening=1000.0, closing=1200.0,
                              txn_sum=150.0)
        p = erpnext_push.anchor_payload(anchor, acct)
        self.assertEqual(p['anchored_opening'], 1000.0)
        self.assertEqual(p['transaction_sum'], 150.0)
        self.assertEqual(p['computed_closing'], 1150.0)
        self.assertEqual(p['variance'], 50.0)
        self.assertAlmostEqual(
            p['anchored_opening'] + p['transaction_sum'], p['computed_closing'])
        self.assertAlmostEqual(
            p['anchored_closing'] - p['computed_closing'], p['variance'])

    def test_the_payload_carries_the_erpnext_link_and_the_plaid_identifiers(self):
        """`bank_account` is what gives the record its Company scope; the Plaid
        ids let ERPNext re-resolve the account and make a mask greppable."""
        acct = self._account(mask='4242')
        p = erpnext_push.anchor_payload(self._anchor(acct), acct)
        self.assertEqual(p['bank_account'], 'WF Brokerage - EC')
        self.assertEqual(p['company'], 'Example Company LLC')
        self.assertEqual(p['plaid_account_id'], acct.account_id)
        self.assertEqual(p['plaid_account_mask'], '4242')
        self.assertEqual(p['period_start'], '2026-06-01')
        self.assertEqual(p['period_end'], '2026-06-30')

    def test_the_tolerance_uses_erpnexts_fieldname(self):
        """`variance_tolerance`, never `tolerance`. Frappe drops kwargs a
        whitelisted method doesn't declare, so the wrong name is not an error —
        it is a 200 with the tolerance silently missing, after which the doctype
        recomputes `reconciled` against its own 0.01 default and disagrees with
        the `reconciled` in this same payload."""
        acct = self._account()
        p = erpnext_push.anchor_payload(self._anchor(acct), acct)
        self.assertIn('variance_tolerance', p)
        self.assertNotIn('tolerance', p)
        from app import statements as stmts
        self.assertEqual(p['variance_tolerance'], stmts.reconcile_tolerance())

    def test_the_fingerprint_is_stable_across_key_order(self):
        """If it weren't, every rebuild would look like a change and the whole
        point of the fingerprint would be lost."""
        a = {'b': 2, 'a': 1, 'c': [1, 2]}
        b = {'c': [1, 2], 'a': 1, 'b': 2}
        self.assertEqual(erpnext_push.fingerprint(a),
                         erpnext_push.fingerprint(b))
        self.assertNotEqual(erpnext_push.fingerprint(a),
                            erpnext_push.fingerprint({**a, 'a': 9}))


class MetadataPayloadTest(PushBase):
    def test_pairing_type_is_derived_from_both_directions(self):
        """The link is one-directional (only the brokerage carries it), so the
        companion's own record can only be labelled by asking who points AT it.
        Without the reverse lookup every cash-services account would push a
        blank pairing_type and ERPNext would show the relationship half-set."""
        brk = self._account(mask='4242')
        cash = self._account(mask='4200', account_id='acc-4200',
                             subtype='checking',
                             bank_account='WF Cash Services - EC')
        brk.paired_account_id = cash.account_id
        db.session.commit()
        self.assertEqual(
            erpnext_push.metadata_payload(brk)['pairing_type'], 'Brokerage')
        self.assertEqual(
            erpnext_push.metadata_payload(cash)['pairing_type'],
            'Cash Services')

    def test_an_unpaired_account_has_no_pairing_type(self):
        acct = self._account()
        p = erpnext_push.metadata_payload(acct)
        self.assertEqual(p['pairing_type'], '')
        self.assertEqual(p['paired_bank_account'], '')

    def test_the_plaid_metadata_the_plan_asks_for_is_all_present(self):
        acct = self._account(mask='4242')
        p = erpnext_push.metadata_payload(acct)
        for key in ('plaid_account_id', 'plaid_account_mask',
                    'plaid_account_type', 'plaid_account_subtype',
                    'sync_enabled'):
            self.assertIn(key, p, f'{key} missing from the metadata push')
        self.assertEqual(p['plaid_account_type'], 'investment')
        self.assertEqual(p['plaid_account_subtype'], 'brokerage')
        self.assertTrue(p['sync_enabled'])


# ── pushing ─────────────────────────────────────────────────────────────────

class PushOutcomeTest(PushBase):
    def test_a_successful_push_stamps_the_anchor(self):
        acct = self._account()
        anchor = self._anchor(acct)
        verdict = erpnext_push.push_anchor(
            anchor, session=erpnext_push.PushSession(self.erp))
        self.assertEqual(verdict['status'], 'pushed')
        self.assertEqual(len(self._pushes()), 1)
        self.assertIsNotNone(anchor.erpnext_pushed_at)
        self.assertTrue(anchor.erpnext_push_fingerprint)
        self.assertEqual(ErpnextPushQueue.query.count(), 0)

    def test_an_unchanged_anchor_is_not_pushed_twice(self):
        """rebuild_statement_anchors runs after every reparse and every pairing
        change. Without this, each run would re-write every unchanged period."""
        acct = self._account()
        anchor = self._anchor(acct)
        session = erpnext_push.PushSession(self.erp)
        erpnext_push.push_anchor(anchor, session=session)
        second = erpnext_push.push_anchor(anchor, session=session)
        self.assertEqual(second['status'], 'unchanged')
        self.assertEqual(len(self._pushes()), 1)

    def test_a_changed_anchor_is_pushed_again(self):
        acct = self._account()
        anchor = self._anchor(acct)
        erpnext_push.push_anchor(anchor,
                                 session=erpnext_push.PushSession(self.erp))
        anchor.variance_reason = 'off_plaid_deposit'
        db.session.commit()
        verdict = erpnext_push.push_anchor(
            anchor, session=erpnext_push.PushSession(self.erp))
        self.assertEqual(verdict['status'], 'pushed')
        self.assertEqual(len(self._pushes()), 2)

    def test_force_pushes_an_unchanged_anchor(self):
        acct = self._account()
        anchor = self._anchor(acct)
        session = erpnext_push.PushSession(self.erp)
        erpnext_push.push_anchor(anchor, session=session)
        verdict = erpnext_push.push_anchor(anchor, session=session, force=True)
        self.assertEqual(verdict['status'], 'pushed')
        self.assertEqual(len(self._pushes()), 2)

    def test_an_unmapped_account_is_skipped_not_queued(self):
        """An anchor ERPNext cannot attribute to a Bank Account has nothing to
        attach to. Queueing it would retry forever against a mapping decision
        only an operator can make."""
        acct = self._account(bank_account=None)
        anchor = self._anchor(acct)
        verdict = erpnext_push.push_anchor(
            anchor, session=erpnext_push.PushSession(self.erp))
        self.assertEqual(verdict['status'], 'skipped')
        self.assertIn('/admin/accounts', verdict['error'])
        self.assertEqual(ErpnextPushQueue.query.count(), 0)
        self.assertEqual(len(self._pushes()), 0)

    def test_an_unconfigured_erpnext_skips_rather_than_queueing(self):
        """Unconfigured is a state an operator chose, not a failure to recover
        from — a queue that filled up on an install with no ERPNext at all
        would be pure noise."""
        erpnext_settings.save('', '', '', '')
        acct = self._account()
        anchor = self._anchor(acct)
        verdict = erpnext_push.push_anchor(anchor)
        self.assertEqual(verdict['status'], 'skipped')
        self.assertEqual(ErpnextPushQueue.query.count(), 0)


class QueueTest(PushBase):
    def test_a_refused_push_is_queued_with_its_payload(self):
        """404 is the state the whole migration window lives in: the
        erpnext_mcp app is not deployed yet. The fact must survive it."""
        self.erp.method_failures[erpnext_push.ANCHOR_METHOD] = (
            404, 'no such method')
        acct = self._account()
        anchor = self._anchor(acct)
        verdict = erpnext_push.push_anchor(
            anchor, session=erpnext_push.PushSession(self.erp))
        self.assertEqual(verdict['status'], 'queued')
        row = ErpnextPushQueue.query.one()
        self.assertEqual(row.kind, erpnext_push.KIND_ANCHOR)
        self.assertEqual(row.dedupe_key, f'anchor:{anchor.id}')
        self.assertEqual(row.method, erpnext_push.ANCHOR_METHOD)
        self.assertEqual(json.loads(row.payload)['anchored_closing'], 1200.0)
        self.assertEqual(row.attempts, 1)
        self.assertIsNone(anchor.erpnext_pushed_at)

    def test_requeueing_the_same_target_replaces_rather_than_appends(self):
        """The queue holds at most one pending write per target, always the
        latest — otherwise a week of failed rebuilds is a week of duplicates."""
        self.erp.method_failures[erpnext_push.ANCHOR_METHOD] = (500, 'boom')
        acct = self._account()
        anchor = self._anchor(acct)
        for _ in range(3):
            erpnext_push.push_anchor(
                anchor, session=erpnext_push.PushSession(self.erp), force=True)
        row = ErpnextPushQueue.query.one()
        self.assertEqual(row.attempts, 3)

    def test_the_backoff_is_exponential_and_capped(self):
        self.assertEqual(erpnext_push._backoff_seconds(1), 60)
        self.assertEqual(erpnext_push._backoff_seconds(2), 120)
        self.assertEqual(erpnext_push._backoff_seconds(3), 240)
        self.assertEqual(erpnext_push._backoff_seconds(99),
                         erpnext_push.QUEUE_BACKOFF_CAP_SECONDS)
        self.assertLessEqual(erpnext_push._backoff_seconds(50),
                             erpnext_push.QUEUE_BACKOFF_CAP_SECONDS)

    def test_the_circuit_breaker_stops_a_batch_re_proving_one_outage(self):
        """A transport failure generalizes; a 4xx does not. With ERPNext
        unreachable the first anchor proves it and the other two queue
        unattempted — the difference between a 13-second sync and a 6-minute
        one."""
        self.erp.method_failures[erpnext_push.ANCHOR_METHOD] = (
            None, 'connection refused')
        acct = self._account()
        anchors = [self._anchor(acct, period=(date(2026, m, 1),
                                              date(2026, m, 28)))
                   for m in (4, 5, 6)]
        stats = erpnext_push.push_anchors(anchors, client=self.erp)
        self.assertEqual(stats['queued'], 3)
        self.assertEqual(len(self._pushes()), 1)
        self.assertEqual(ErpnextPushQueue.query.count(), 3)

    def test_a_rejection_does_not_trip_the_breaker(self):
        """A 4xx is about that document. The next one may well succeed, and a
        breaker that tripped on it would strand a whole chain over one bad
        row."""
        acct = self._account()
        anchors = [self._anchor(acct, period=(date(2026, m, 1),
                                              date(2026, m, 28)))
                   for m in (4, 5, 6)]
        self.erp.method_failures[erpnext_push.ANCHOR_METHOD] = (417, 'nope')
        erpnext_push.push_anchors(anchors, client=self.erp)
        self.assertEqual(len(self._pushes()), 3)


class DrainTest(PushBase):
    def _queue_one(self):
        self.erp.method_failures[erpnext_push.ANCHOR_METHOD] = (404, 'gone')
        acct = self._account()
        anchor = self._anchor(acct)
        erpnext_push.push_anchor(anchor,
                                 session=erpnext_push.PushSession(self.erp))
        self.erp.method_failures.clear()
        return anchor

    def test_a_drain_replays_the_queue_and_stamps_the_source_row(self):
        """Without the stamp the anchor would still read as never-pushed, so
        the next rebuild would push it again: the queue would drain and
        immediately refill."""
        anchor = self._queue_one()
        # A queued row is not eligible until its backoff elapses.
        result = erpnext_push.drain(self.erp, force=True)
        self.assertEqual(result['attempted'], 1)
        self.assertEqual(result['pushed'], 1)
        self.assertEqual(result['remaining'], 0)
        db.session.refresh(anchor)
        self.assertIsNotNone(anchor.erpnext_pushed_at)
        self.assertEqual(ErpnextPushQueue.query.count(), 0)

    def test_the_drained_anchor_is_not_pushed_again_by_the_next_rebuild(self):
        anchor = self._queue_one()
        erpnext_push.drain(self.erp, force=True)
        before = len(self._pushes())
        verdict = erpnext_push.push_anchor(
            anchor, session=erpnext_push.PushSession(self.erp))
        self.assertEqual(verdict['status'], 'unchanged')
        self.assertEqual(len(self._pushes()), before)

    def test_an_unelapsed_backoff_is_honoured_unless_forced(self):
        """Otherwise the backoff means nothing: a sync every ten minutes would
        retry a dead endpoint every ten minutes forever."""
        self._queue_one()
        self.assertEqual(erpnext_push.drain(self.erp)['attempted'], 0)
        self.assertEqual(erpnext_push.drain(self.erp, force=True)['attempted'], 1)

    def test_an_elapsed_backoff_is_picked_up_by_an_ordinary_drain(self):
        self._queue_one()
        row = ErpnextPushQueue.query.one()
        row.next_attempt_at = datetime.utcnow() - timedelta(minutes=5)
        db.session.commit()
        self.assertEqual(erpnext_push.drain(self.erp)['attempted'], 1)

    def test_an_unparseable_row_is_dropped_rather_than_blocking_the_queue(self):
        """A permanently-stuck head would starve everything behind it."""
        self._queue_one()
        row = ErpnextPushQueue.query.one()
        row.payload = 'not json'
        db.session.commit()
        result = erpnext_push.drain(self.erp, force=True)
        self.assertEqual(result['attempted'], 0)
        self.assertEqual(ErpnextPushQueue.query.count(), 0)

    def test_a_still_failing_drain_leaves_the_row_queued(self):
        self._queue_one()
        self.erp.method_failures[erpnext_push.ANCHOR_METHOD] = (404, 'still')
        result = erpnext_push.drain(self.erp, force=True)
        self.assertEqual(result['pushed'], 0)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(ErpnextPushQueue.query.count(), 1)


# ── the callers ─────────────────────────────────────────────────────────────

class RebuildPushesTest(PushBase):
    def _statement(self, acct, start, end, opening, closing):
        st = PlaidStatement(statement_id=f'st-{start}',
                            plaid_item_id=self.item.item_id,
                            plaid_account_id=acct.account_id,
                            period_start=start, period_end=end,
                            opening_balance=opening, closing_balance=closing,
                            parser_version=stmts.PARSER_VERSION)
        db.session.add(st)
        db.session.commit()
        return st

    def test_a_rebuild_pushes_what_it_wrote(self):
        acct = self._account()
        self._statement(acct, date(2026, 5, 1), date(2026, 5, 31), 100.0, 100.0)
        result = stmts.rebuild_statement_anchors(acct.account_id)
        self.assertEqual(result['written'], 1)
        self.assertIn('erpnext', result)
        # No client was injected, so the module resolves its own — which in a
        # test means the real ERPNextClient against an unroutable host. What
        # matters is the SHAPE of the report, and that the rebuild survived.
        self.assertIsInstance(result['erpnext'], dict)

    def test_a_rebuild_survives_an_erpnext_that_refuses_everything(self):
        """The property that matters most: a push can never fail a rebuild."""
        acct = self._account()
        self._statement(acct, date(2026, 5, 1), date(2026, 5, 31), 100.0, 100.0)
        self.erp.method_failures[erpnext_push.ANCHOR_METHOD] = (500, 'down')

        def _boom(*a, **kw):
            raise RuntimeError('ERPNext exploded')

        original = erpnext_push.push_anchors
        erpnext_push.push_anchors = _boom
        self.addCleanup(setattr, erpnext_push, 'push_anchors', original)
        result = stmts.rebuild_statement_anchors(acct.account_id)
        self.assertEqual(result['written'], 1)
        self.assertIn('error', result['erpnext'])
        self.assertEqual(StatementAnchor.query.count(), 1)

    def test_a_rebuild_with_nothing_written_reports_zeroes_not_an_absence(self):
        acct = self._account()
        result = stmts.rebuild_statement_anchors(acct.account_id)
        self.assertEqual(result['erpnext']['pushed'], 0)


class PairingPushTest(PushBase):
    def test_both_sides_of_a_pairing_are_pushed(self):
        """ERPNext renders the relationship from each Bank Account; pushing
        only the brokerage leaves the companion claiming it is unpaired."""
        brk = self._account(mask='4242')
        cash = self._account(mask='4200', account_id='acc-4200',
                             subtype='checking',
                             bank_account='WF Cash Services - EC')
        brk.paired_account_id = cash.account_id
        db.session.commit()
        out = erpnext_push.push_pairing(brk, cash, client=self.erp)
        self.assertEqual(out['brokerage']['status'], 'pushed')
        self.assertEqual(out['cash_services']['status'], 'pushed')
        payloads = self._pushes(erpnext_push.PAIRING_METHOD)
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]['paired_bank_account'],
                         'WF Cash Services - EC')
        self.assertEqual(payloads[1]['pairing_type'], 'Cash Services')

    def test_the_metadata_refresh_pushes_only_what_changed(self):
        acct = self._account()
        first = erpnext_push.push_metadata_for([acct], client=self.erp)
        self.assertEqual(first['pushed'], 1)
        second = erpnext_push.push_metadata_for([acct], client=self.erp)
        self.assertEqual(second['pushed'], 0)
        self.assertEqual(second['unchanged'], 1)
        acct.sync_enabled = False
        db.session.commit()
        third = erpnext_push.push_metadata_for([acct], client=self.erp)
        self.assertEqual(third['pushed'], 1)

    def test_an_unmapped_account_is_skipped_by_the_metadata_refresh(self):
        acct = self._account(bank_account='')
        stats = erpnext_push.push_metadata_for([acct], client=self.erp)
        self.assertEqual(stats['skipped'], 1)
        self.assertEqual(len(self._pushes(erpnext_push.PAIRING_METHOD)), 0)


class AcknowledgementTest(PushBase):
    """v1.0.2 — a 200 is not a confirmation.

    THE PRODUCTION BUG. `sync_now` reported `erpnext_push.metadata.unchanged: 4`
    while all four ERPNext Bank Accounts held null for plaid_account_mask,
    plaid_account_type, plaid_account_subtype and sync_enabled=false.

    Several things produce a 200 that writes nothing — Frappe drops kwargs a
    whitelisted method does not DECLARE, a custom field may be absent from the
    doctype, a validation may rewrite the value — and v1.0.1 could tell none of
    them from a success. It stamped the fingerprint on the 200, after which the
    fact was never pushed again. These tests stage the first of those, because
    it is the cheapest to reproduce; the fix is indifferent to which it was."""

    def test_a_200_that_wrote_nothing_is_queued_not_stamped(self):
        acct = self._account()
        self.erp.drops_metadata_fields = True     # the old erpnext_mcp build
        stats = erpnext_push.push_metadata_for([acct], client=self.erp)
        self.assertEqual(stats['pushed'], 0)
        self.assertEqual(stats['queued'], 1)
        self.assertIsNone(acct.erpnext_metadata_fingerprint)
        self.assertIsNone(acct.erpnext_metadata_pushed_at)

    def test_the_queued_reason_names_the_fields_that_did_not_land(self):
        acct = self._account()
        self.erp.drops_metadata_fields = True
        erpnext_push.push_metadata_for([acct], client=self.erp)
        row = ErpnextPushQueue.query.one()
        self.assertIn('plaid_account_mask', row.last_error)
        self.assertIn('older', row.last_error)

    def test_the_next_sync_pushes_it_again_rather_than_reporting_unchanged(self):
        """The whole point: an unconfirmed fact stays pending. Reporting
        `unchanged` is what made this invisible for a release."""
        acct = self._account()
        self.erp.drops_metadata_fields = True
        erpnext_push.push_metadata_for([acct], client=self.erp)
        again = erpnext_push.push_metadata_for([acct], client=self.erp)
        self.assertEqual(again['unchanged'], 0)
        self.assertEqual(again['queued'], 1)
        # …and it converges the moment the ERPNext side gains the fields.
        self.erp.drops_metadata_fields = False
        fixed = erpnext_push.push_metadata_for([acct], client=self.erp)
        self.assertEqual(fixed['pushed'], 1)
        self.assertIsNotNone(acct.erpnext_metadata_fingerprint)
        self.assertEqual(
            self.erp.pushed_bank_accounts['WF Brokerage - EC']
            ['plaid_account_mask'], '4242')

    def test_a_confirmed_push_stamps_and_the_echo_is_what_confirms_it(self):
        acct = self._account()
        stats = erpnext_push.push_metadata_for([acct], client=self.erp)
        self.assertEqual(stats['pushed'], 1)
        held = self.erp.pushed_bank_accounts['WF Brokerage - EC']
        self.assertEqual(held['plaid_account_type'], 'investment')
        self.assertEqual(held['plaid_account_subtype'], 'brokerage')
        self.assertEqual(held['plaid_account_id'], 'acc-4242')

    def test_sync_enabled_false_is_a_value_and_must_be_confirmed(self):
        """False is not an absence. 'this account is no longer synced' is
        exactly the fact a write-only-what-is-truthy endpoint loses."""
        acct = self._account()
        acct.sync_enabled = False
        db.session.commit()
        self.assertEqual(
            erpnext_push._ack_metadata(
                {'updated': [{'bank_account': 'WF Brokerage - EC',
                              'account': {'plaid_account_mask': '4242',
                                          'sync_enabled': True}}]},
                {'bank_account': 'WF Brokerage - EC',
                 'plaid_account_mask': '4242', 'sync_enabled': False})[0],
            erpnext_push.ACK_CONTRADICTED)

    def test_a_field_we_did_not_send_is_not_demanded_back(self):
        """ERPNext writes only the keys a push carried, so a blank subtype is
        left alone — demanding an echo for it would report every ordinary
        account as contradicted."""
        verdict, _ = erpnext_push._ack_metadata(
            {'updated': [{'bank_account': 'B',
                          'account': {'plaid_account_mask': '4242',
                                      'plaid_account_subtype': 'typed by hand',
                                      'sync_enabled': True}}]},
            {'bank_account': 'B', 'plaid_account_mask': '4242',
             'plaid_account_subtype': '', 'sync_enabled': True})
        self.assertEqual(verdict, erpnext_push.ACK_CONFIRMED)

    def test_an_unrecognized_reply_is_neither_stamped_nor_queued(self):
        """A shape this build cannot read is not a failure to retry — a
        re-push would meet the same silence and the queue would never drain.
        It is simply not claimed, so the next sync sends it again."""
        acct = self._account()
        self.erp.method_returns[erpnext_push.PAIRING_METHOD] = {'ok': True}
        stats = erpnext_push.push_metadata_for([acct], client=self.erp)
        self.assertEqual(stats['unconfirmed'], 1)
        self.assertEqual(stats['queued'], 0)
        self.assertEqual(ErpnextPushQueue.query.count(), 0)
        self.assertIsNone(acct.erpnext_metadata_fingerprint)

    def test_an_anchor_that_created_and_updated_nothing_is_queued(self):
        acct = self._account()
        anchor = self._anchor(acct)
        self.erp.method_returns[erpnext_push.ANCHOR_METHOD] = {
            'created_count': 0, 'updated_count': 0, 'failed_count': 1,
            'failed': [{'error': 'no Bank Account named ... on this site.'}]}
        verdict = erpnext_push.push_anchor(anchor, acct,
                                           session=erpnext_push.PushSession(
                                               self.erp))
        self.assertEqual(verdict['status'], 'queued')
        self.assertIn('no Bank Account named', verdict['error'])
        self.assertIsNone(anchor.erpnext_pushed_at)

    def test_a_v1_0_1_fingerprint_does_not_suppress_the_first_verified_push(self):
        """The contract version is what invalidates every stamp written when a
        stamp meant 'Frappe returned 200'. Without it the four accounts on the
        live install would go on reporting `unchanged` forever."""
        acct = self._account()
        payload = erpnext_push.metadata_payload(acct)
        acct.erpnext_metadata_fingerprint = erpnext_push.fingerprint(payload)
        acct.erpnext_metadata_pushed_at = datetime(2026, 8, 1)
        db.session.commit()
        stats = erpnext_push.push_metadata_for([acct], client=self.erp)
        self.assertEqual(stats['unchanged'], 0)
        self.assertEqual(stats['pushed'], 1)


class RelinkChainPushTest(PushBase):
    """v1.0.2 — Plaid reassigns account ids; the books must not notice."""

    def _relinked_pair(self):
        """A retired account holding history, and the live row that adopted its
        mapping — the state `reconnect.adopt` leaves behind."""
        old = self._account(mask='6030', account_id='ZE4Zold',
                            bank_account='WF Brokerage - EC')
        new = self._account(mask='6030', account_id='jN7xnew',
                            bank_account='WF Brokerage - EC')
        old.erpnext_bank_account_name = None      # adopt() strips the donor
        old.sync_enabled = False
        old.import_status = 'superseded'
        old.superseded_by_account_id = new.account_id
        db.session.commit()
        return old, new

    def test_an_anchor_on_a_dead_plaid_id_still_reaches_erpnext(self):
        """THE BUG. Anchors built before a re-link keep the old account_id, and
        adopt() nulls that row's Bank Account on purpose. Resolving the mapping
        from that one row finds nothing, so the whole pre-relink half of the
        history read as an unmapped account and never left the building."""
        old, new = self._relinked_pair()
        anchor = self._anchor(old)
        verdict = erpnext_push.push_anchor(
            anchor, session=erpnext_push.PushSession(self.erp))
        self.assertEqual(verdict['status'], 'pushed')
        self.assertEqual(self._pushes()[0]['bank_account'],
                         'WF Brokerage - EC')
        # The anchor keeps its OWN Plaid id — ERPNext is idempotent on the
        # period, so the two halves land in one chain under one Bank Account.
        self.assertEqual(self._pushes()[0]['plaid_account_id'], 'ZE4Zold')

    def test_a_genuinely_unmapped_account_is_still_skipped(self):
        """The chain walk must not turn 'nobody mapped this' into a push at a
        neighbouring account's books."""
        acct = self._account(mask='9999', account_id='orphan',
                             bank_account='')
        anchor = self._anchor(acct)
        verdict = erpnext_push.push_anchor(
            anchor, session=erpnext_push.PushSession(self.erp))
        self.assertEqual(verdict['status'], 'skipped')
        self.assertIn('re-linked', verdict['error'])

    def test_the_metadata_push_repoints_erpnext_at_the_current_plaid_id(self):
        """ERPNext holds the dead id; the Bank Account DOCNAME is what both
        systems agree on, so the push keyed by docname is the repoint."""
        old, new = self._relinked_pair()
        self.erp.pushed_bank_accounts['WF Brokerage - EC'] = {
            'name': 'WF Brokerage - EC', 'plaid_account_id': 'ZE4Zold'}
        stats = erpnext_push.push_metadata_for([new], client=self.erp)
        self.assertEqual(stats['pushed'], 1)
        self.assertEqual(
            self.erp.pushed_bank_accounts['WF Brokerage - EC']
            ['plaid_account_id'], 'jN7xnew')


class AnchorReadTest(PushBase):
    def test_a_read_tolerates_three_envelope_shapes(self):
        """The ERPNext side is a sibling codebase under active development. A
        read that hard-failed on an envelope rename would take the whole
        reconciliation surface down with it."""
        rows = [{'period_start': '2026-06-01', 'variance': 1.0}]
        for envelope in (rows, {'anchors': rows}, {'data': rows},
                         {'message': rows}):
            self.assertEqual(erpnext_push._anchor_rows(envelope), rows)
        for bad in ({'error': 'nope'}, None, 'text'):
            self.assertIsNone(erpnext_push._anchor_rows(bad))

    def test_an_unreachable_erpnext_reads_as_none_not_as_an_empty_chain(self):
        """None means 'fall back and say so'; [] would mean 'this account has
        no anchors', and a reconciled account would be reported unreconciled."""
        acct = self._account()
        self.erp.method_failures[erpnext_push.ANCHOR_CHAIN_METHOD] = (
            404, 'not deployed')
        self.assertIsNone(erpnext_push.fetch_anchor_chain(acct,
                                                          client=self.erp))

    def test_a_reachable_erpnext_returns_its_own_chain(self):
        acct = self._account()
        self.erp.method_returns[erpnext_push.ANCHOR_CHAIN_METHOD] = {
            'anchors': [{'period_start': '2026-06-01', 'variance': 76.37}]}
        rows = erpnext_push.fetch_anchor_chain(acct, client=self.erp)
        self.assertEqual(rows[0]['variance'], 76.37)


if __name__ == '__main__':  # pragma: no cover
    import unittest
    unittest.main()
