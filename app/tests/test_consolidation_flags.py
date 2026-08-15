# SPDX-License-Identifier: MIT
"""The three consolidation source flags, and the rollback they buy (v1.0.0).

ANCHOR_SOURCE, RULES_SOURCE and ADVISORY_SOURCE each name where a READ comes
from: 'erpnext' (the default, and the point of the consolidation) or 'local'
(this app's own tables). The plan's §9 rollback rests entirely on them — no data
is deleted in either sprint, so reverting is a settings flip rather than a
restore — and this file is what proves the flip actually reverts anything.

The other half is the FALLBACK, which is what makes defaulting to 'erpnext'
safe before the ERPNext side is even deployed: an unreachable ERPNext reads
locally and SAYS SO. A reconciliation tool that quietly answered from the wrong
system would be worse than one that failed, because the two systems agree almost
all of the time — which is exactly what makes a silent divergence hard to catch.

    cd app
    python3 -m unittest tests.test_consolidation_flags -v
"""
import json
from datetime import date

from app import advisory, db, erpnext_push, erpnext_settings
from app.models import PlaidStatement, StatementAnchor

from tests.fakes import FakeERPClient, unwrap_tool_payload
from tests.test_mcp_server import McpBase


class FlagDefaultsTest(McpBase):
    def test_all_three_default_to_erpnext(self):
        self.assertEqual(erpnext_settings.anchor_source(), 'erpnext')
        self.assertEqual(erpnext_settings.rules_source(), 'erpnext')
        self.assertEqual(erpnext_settings.advisory_source(), 'erpnext')

    def test_a_persisted_flag_wins_over_the_env_default(self):
        erpnext_settings.set_source('anchor_source', 'local')
        self.assertEqual(erpnext_settings.anchor_source(), 'local')
        self.assertEqual(erpnext_settings.rules_source(), 'erpnext')

    def test_a_flag_survives_an_unrelated_settings_save(self):
        """The connection form re-saves url/key/company. A rollback that got
        undone by someone editing the ERPNext URL would be no rollback."""
        erpnext_settings.set_source('rules_source', 'local')
        erpnext_settings.save('http://erp.test', 'K2', 'SECRET2', 'Other Co')
        self.assertEqual(erpnext_settings.rules_source(), 'local')

    def test_an_unknown_value_is_refused_rather_than_silently_ignored(self):
        """This is the rollback switch; a typo that did nothing is the worst
        possible behaviour for it."""
        with self.assertRaises(ValueError):
            erpnext_settings.set_source('anchor_source', 'ERPNEXTT')
        with self.assertRaises(ValueError):
            erpnext_settings.set_source('nonsense_source', 'local')

    def test_a_hand_edited_garbage_value_resolves_forward_not_to_nothing(self):
        """'' is not a source, and a source-less read would answer from
        nothing. The file is operator-editable, so this is a real state."""
        import os
        path = os.path.join(self.app.config['DATA_DIR'],
                            'erpnext_settings.json')
        with open(path) as fh:
            blob = json.load(fh)
        blob['anchor_source'] = 'whatever'
        with open(path, 'w') as fh:
            json.dump(blob, fh)
        self.assertEqual(erpnext_settings.anchor_source(), 'erpnext')

    def test_a_settings_file_written_before_v1_0_0_migrates_on_read(self):
        """The DATA_DIR convention: migrate on read, idempotently, so a boot
        with a stale volume still produces correct values."""
        import os
        path = os.path.join(self.app.config['DATA_DIR'],
                            'erpnext_settings.json')
        with open(path, 'w') as fh:
            json.dump({'url': 'http://erp.test', 'api_key': 'K',
                       'api_secret': 'S', 'default_company': 'EC'}, fh)
        self.assertEqual(erpnext_settings.anchor_source(), 'erpnext')
        self.assertEqual(erpnext_settings.load()['url'], 'http://erp.test')


class AnchorSourceTest(McpBase):
    """The read path, end to end through the MCP tool an operator actually
    calls — because the flag's whole job is to change what that tool answers."""

    def setUp(self):
        super().setUp()
        self.acct = self._seed_account('4242')
        self.acct.erpnext_bank_account_name = 'WF Brokerage - EC'
        st = PlaidStatement(statement_id='s1',
                            plaid_item_id=self.item.item_id,
                            plaid_account_id=self.acct.account_id,
                            period_start=date(2026, 6, 1),
                            period_end=date(2026, 6, 30))
        db.session.add(st)
        db.session.commit()
        db.session.add(StatementAnchor(
            account_id=self.acct.account_id, statement_id=st.id,
            period_start=date(2026, 6, 1), period_end=date(2026, 6, 30),
            anchored_opening=100.0, anchored_closing=176.37,
            transaction_sum=0.0, computed_closing=100.0, variance=76.37))
        db.session.commit()

    def _status(self):
        _, body = self._call_tool('get_reconciliation_status',
                                  {'account_mask': '4242'})
        return unwrap_tool_payload(
            json.loads(body['result']['content'][0]['text']))

    def _serve(self, rows):
        erp = FakeERPClient()
        erp.method_returns[erpnext_push.ANCHOR_CHAIN_METHOD] = {'anchors': rows}
        original = erpnext_push._client_or_none
        erpnext_push._client_or_none = lambda: erp
        self.addCleanup(setattr, erpnext_push, '_client_or_none', original)
        return erp

    def test_an_unreachable_erpnext_falls_back_and_labels_the_fallback(self):
        """The state the entire migration window lives in: the erpnext_mcp app
        is not deployed yet. The tool must still answer, correctly, and must
        say which system answered."""
        payload = self._status()
        self.assertEqual(payload['anchor_source'], 'local')
        self.assertEqual(len(payload['anchors']), 1)
        self.assertEqual(payload['anchors'][0]['variance'], 76.37)

    def test_a_reachable_erpnext_is_the_source(self):
        self._serve([{'period_start': '2026-06-01', 'period_end': '2026-06-30',
                      'variance': 999.0}])
        payload = self._status()
        self.assertEqual(payload['anchor_source'], 'erpnext')
        self.assertEqual(payload['anchors'][0]['variance'], 999.0)

    def test_the_summary_describes_the_rows_beside_it(self):
        """Not the local chain. A total that summed a different set of rows
        than the ones next to it would be at its most misleading exactly when
        the two systems diverge — which is when someone calls this tool."""
        self._serve([{'period_start': '2026-06-01', 'period_end': '2026-06-30',
                      'variance': 999.0, 'chain_gap_from_prior': True}])
        payload = self._status()
        self.assertEqual(payload['summary']['variance'], 999.0)
        self.assertEqual(payload['summary']['periods'], 1)
        self.assertEqual(payload['summary']['unexplained'], 1)
        self.assertEqual(payload['summary']['gaps'], 1)

    def test_the_local_summary_is_unchanged_by_the_rewrite(self):
        """The dict-based summary must be arithmetically identical to the
        model-based one it replaced, or every reconciled account starts
        reporting a different headline on upgrade."""
        payload = self._status()
        self.assertEqual(payload['anchor_source'], 'local')
        self.assertEqual(payload['summary']['variance'], 76.37)
        self.assertEqual(payload['summary']['unexplained'], 1)
        self.assertEqual(payload['summary']['gaps'], 0)
        self.assertEqual(payload['summary']['periods'], 1)

    def test_anchor_source_local_reads_locally_even_when_erpnext_answers(self):
        """The rollback, exercised: ERPNext is up and holding a different
        number, and the flag still wins."""
        self._serve([{'period_start': '2026-06-01', 'variance': 999.0}])
        erpnext_settings.set_source('anchor_source', 'local')
        payload = self._status()
        self.assertEqual(payload['anchor_source'], 'local')
        self.assertEqual(payload['anchors'][0]['variance'], 76.37)

    def test_the_date_window_filters_an_erpnext_chain_the_same_way(self):
        """A filter implemented twice is a filter that disagrees."""
        self._serve([
            {'period_start': '2026-05-01', 'period_end': '2026-05-31',
             'variance': 1.0},
            {'period_start': '2026-06-01', 'period_end': '2026-06-30',
             'variance': 2.0}])
        _, body = self._call_tool('get_reconciliation_status',
                                  {'account_mask': '4242',
                                   'period_start': '2026-06-01'})
        payload = unwrap_tool_payload(
            json.loads(body['result']['content'][0]['text']))
        self.assertEqual(len(payload['anchors']), 1)
        self.assertEqual(payload['anchors'][0]['variance'], 2.0)

    def test_the_unreconciled_worklist_reports_its_source_too(self):
        _, body = self._call_tool('list_unreconciled_statements')
        payload = unwrap_tool_payload(
            json.loads(body['result']['content'][0]['text']))
        self.assertIn(payload['anchor_source'], ('local', 'erpnext', 'mixed'))
        self.assertEqual(payload['count'], 1)


class AdvisorySourceTest(McpBase):
    """Fee TERMS move to ERPNext; the fee COMPUTATION stays here. This is the
    seam — the one place a rate is read."""

    def _agreement(self):
        from app.models import AdvisoryAgreement
        acct = self._seed_account('4242')
        ag = AdvisoryAgreement(
            name='OML Asset Advisor', client_entity='Orchard Meadow, LLC',
            advisor_entity='Wells Fargo Advisors LLC',
            managed_account_ids=[acct.account_id],
            fee_type='Percent of AUM', total_base_fee_rate=0.01,
            bank_fee_rate=0.004, status='active',
            effective_date=date(2026, 1, 1))
        db.session.add(ag)
        db.session.commit()
        return ag

    def _serve(self, doc):
        erp = FakeERPClient()
        erp.method_returns[erpnext_push.ADVISORY_METHOD] = doc
        original = erpnext_push._client_or_none
        erpnext_push._client_or_none = lambda: erp
        self.addCleanup(setattr, erpnext_push, '_client_or_none', original)
        advisory.reset_terms_cache()
        self.addCleanup(advisory.reset_terms_cache)

    def test_an_unreachable_erpnext_uses_the_local_terms(self):
        advisory.reset_terms_cache()
        self.addCleanup(advisory.reset_terms_cache)
        terms = advisory.fee_terms(self._agreement())
        self.assertEqual(terms['source'], 'local')
        self.assertAlmostEqual(terms['total_base_fee_rate'], 0.01)

    def test_erpnexts_percent_becomes_the_engine_rate(self):
        """A document states 1.0 for 1%; the accrual multiplies by 0.01. One
        conversion, in one place — a rate converted at each read drifts."""
        self._serve({'agreement': {'fee_percent_of_aum': 1.25,
                                   'fee_type': 'Percent of AUM'}})
        terms = advisory.fee_terms(self._agreement())
        self.assertEqual(terms['source'], 'erpnext')
        self.assertAlmostEqual(terms['total_base_fee_rate'], 0.0125)

    def test_the_bank_cut_is_never_taken_from_erpnext(self):
        """It is a Bank-Bridge-side split of a fee the advisor's agreement does
        not itemize. Reading a missing field as zero would move the custodian's
        whole cut onto the Manager's payable."""
        self._serve({'agreement': {'fee_percent_of_aum': 1.0}})
        terms = advisory.fee_terms(self._agreement())
        self.assertAlmostEqual(terms['bank_fee_rate'], 0.004)

    def test_advisory_source_local_ignores_erpnext(self):
        self._serve({'agreement': {'fee_percent_of_aum': 9.0}})
        erpnext_settings.set_source('advisory_source', 'local')
        terms = advisory.fee_terms(self._agreement())
        self.assertEqual(terms['source'], 'local')
        self.assertAlmostEqual(terms['total_base_fee_rate'], 0.01)

    def test_the_daily_accrual_uses_the_erpnext_rate(self):
        """The computation is what the terms are FOR; a seam that stopped at
        `fee_terms` would be decorative."""
        ag = self._agreement()
        self._serve({'agreement': {'fee_percent_of_aum': 2.0}})
        original = advisory.agreement_aum
        advisory.agreement_aum = lambda a: 365000.0
        self.addCleanup(setattr, advisory, 'agreement_aum', original)
        row = advisory.sample_daily_aum(ag, on=date(2026, 6, 1))
        # 365,000 × 0.02 / 365 = 20.00 a day.
        self.assertAlmostEqual(row.fee_accrual_daily, 20.0)

    def test_an_error_envelope_is_not_mistaken_for_an_agreement(self):
        """Recognized by carrying a term the engine uses, not by being a dict —
        otherwise a Frappe error blob reads as an agreement with every rate at
        zero, and the quarter accrues nothing."""
        self._serve({'exc_type': 'ValidationError', 'message': 'no such doc'})
        terms = advisory.fee_terms(self._agreement())
        self.assertEqual(terms['source'], 'local')
        self.assertAlmostEqual(terms['total_base_fee_rate'], 0.01)


if __name__ == '__main__':  # pragma: no cover
    import unittest
    unittest.main()
