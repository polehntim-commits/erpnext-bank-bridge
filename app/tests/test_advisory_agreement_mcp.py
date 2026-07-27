# SPDX-License-Identifier: MIT
"""Advisory-agreement registration over MCP (v0.7.4).

The write side of get_advisory_agreement_summary: create_advisory_agreement
records the signed terms against one managed brokerage account, and
update_advisory_agreement amends them by clone-and-supersede.

Covered here:

  * both switches default OFF and block the call; ON allows it
  * a registration stores every term, and converts fee_percent_of_aum (1.0)
    into the engine's total_base_fee_rate (0.01)
  * the returned summary is the SAME shape get_advisory_agreement_summary
    returns, with the agreement serialized rather than repr'd
  * REFUSALS: unknown mask, missing required field, bad vocabulary value, a
    fee_type with no amount, an inverted term, and a SECOND active agreement on
    an account that already has one
  * registration bills nothing: kill switches off, no settlement accounts, no
    bank/performance rate — and it says so in not_yet_configured
  * an amendment writes a NEW row, supersedes the old, moves the fee/AUM
    history forward, and refuses to amend a superseded version
  * the v0.7.4 columns migrate onto a v0.7.3-shaped table

Synthetic masks (9401 / 3194) and names (TEST CLIENT / TEST ADVISORS) only.

    cd app
    python3 -m unittest tests.test_advisory_agreement_mcp -v
"""
import json
from datetime import date

from sqlalchemy import inspect, text

from app import db, mcp_settings
from app.migrations import run_migrations
from app.models import (AdvisoryAgreement, AdvisoryFeeAccrual, DailyAUM,
                        PlaidAccount)

from tests.test_mcp_server import McpBase

CLIENT_ENTITY = 'Test Orchard, LLC'
ADVISOR_ENTITY = 'Test Advisors LLC'

# The WF Advisors shape the tool exists for: 1% of AUM, billed quarterly.
BASE_ARGS = {
    'agreement_name': 'Test Asset Advisor - TO - ••9401',
    'client_entity': CLIENT_ENTITY,
    'advisor_entity': ADVISOR_ENTITY,
    'plaid_account_mask': '9401',
    'objective': 'Moderate Growth',
    'investment_horizon_years': 10,
    'fee_type': 'Percent of AUM',
    'fee_percent_of_aum': 1.0,
    'billing_frequency': 'Quarterly',
    'effective_date': '2026-01-01',
    'applies_to_company': 'Test Company',
    'document_reference': 'GOV-DOC-0001.pdf',
}


class AdvisoryMcpBase(McpBase):
    def setUp(self):
        super().setUp()
        mcp_settings.save({'create_advisory_agreement': True,
                           'update_advisory_agreement': True})
        self.brokerage = PlaidAccount(
            account_id='brk-9401', item_id=self.item.item_id,
            name='TEST BROKERAGE', mask='9401', type='investment',
            subtype='brokerage', paired_account_id='cash-3194',
            balance_current=0.0)
        db.session.add(self.brokerage)
        db.session.add(PlaidAccount(
            account_id='cash-3194', item_id=self.item.item_id,
            name='TEST CASH', mask='3194', type='depository',
            subtype='cash management', balance_current=250_000.0))
        db.session.commit()

    def _payload(self, name, arguments=None):
        """(result, payload). An ERROR result's content is a plain message, not
        JSON, so `payload` is None for one — the refusal tests read the text off
        `result` instead."""
        _, body = self._call_tool(name, arguments or {})
        result = body['result']
        if result['isError']:
            return result, None
        return result, json.loads(result['content'][0]['text'])

    def _register(self, **overrides):
        args = dict(BASE_ARGS)
        args.update(overrides)
        return self._payload('create_advisory_agreement', args)


class KillSwitchTest(AdvisoryMcpBase):
    def test_both_switches_default_off(self):
        defaults = mcp_settings._DEFAULTS
        self.assertFalse(defaults['create_advisory_agreement'])
        self.assertFalse(defaults['update_advisory_agreement'])

    def test_create_blocked_when_switch_off(self):
        mcp_settings.save({'create_advisory_agreement': False})
        result, _ = self._payload('create_advisory_agreement', BASE_ARGS)
        self.assertTrue(result['isError'])
        self.assertIn('kill switch', result['content'][0]['text'])
        self.assertEqual(AdvisoryAgreement.query.count(), 0)

    def test_update_blocked_when_switch_off(self):
        _, created = self._register()
        mcp_settings.save({'update_advisory_agreement': False})
        result, _ = self._payload('update_advisory_agreement', {
            'agreement_id': created['created_agreement_id'],
            'fee_percent_of_aum': 1.25})
        self.assertTrue(result['isError'])
        self.assertIn('kill switch', result['content'][0]['text'])
        ag = db.session.get(AdvisoryAgreement,
                            created['created_agreement_id'])
        self.assertEqual(ag.total_base_fee_rate, 0.01)


class RegistrationTest(AdvisoryMcpBase):
    def test_registration_stores_every_term(self):
        result, payload = self._register()
        self.assertFalse(result['isError'])
        ag = db.session.get(AdvisoryAgreement,
                            payload['created_agreement_id'])
        self.assertEqual(ag.name, BASE_ARGS['agreement_name'])
        self.assertEqual(ag.client_entity, CLIENT_ENTITY)
        self.assertEqual(ag.advisor_entity, ADVISOR_ENTITY)
        self.assertEqual(ag.manager_name, ADVISOR_ENTITY)
        self.assertEqual(ag.objective, 'Moderate Growth')
        self.assertEqual(ag.investment_horizon_years, 10)
        self.assertEqual(ag.fee_type, 'Percent of AUM')
        self.assertEqual(ag.billing_frequency, 'Quarterly')
        self.assertEqual(ag.effective_date, date(2026, 1, 1))
        self.assertIsNone(ag.termination_date)
        self.assertEqual(ag.client_company, 'Test Company')
        self.assertEqual(ag.document_reference, 'GOV-DOC-0001.pdf')
        self.assertEqual(ag.account_ids(), ['brk-9401'])
        self.assertEqual(ag.status, 'active')
        self.assertTrue(ag.is_active())

    def test_percent_becomes_the_engine_rate(self):
        """1.0 (as a document states it) → 0.01 (as the accrual multiplies)."""
        _, payload = self._register(fee_percent_of_aum=1.0)
        ag = db.session.get(AdvisoryAgreement,
                            payload['created_agreement_id'])
        self.assertEqual(ag.total_base_fee_rate, 0.01)
        self.assertEqual(ag.to_dict()['fee_percent_of_aum'], 1.0)

    def test_registration_bills_nothing(self):
        _, payload = self._register()
        ag = db.session.get(AdvisoryAgreement,
                            payload['created_agreement_id'])
        self.assertFalse(ag.fee_accrual_enabled)
        self.assertFalse(ag.performance_fee_enabled)
        self.assertFalse(ag.risk_control_alerts_enabled)
        self.assertEqual(ag.bank_fee_rate, 0.0)
        self.assertEqual(ag.performance_fee_rate, 0.0)
        self.assertEqual(ag.advisory_expense_account or '', '')
        notes = ' '.join(payload['not_yet_configured'])
        self.assertIn('fee_accrual_enabled is OFF', notes)
        self.assertIn('settlement accounts', notes)

    def test_a_flat_fee_says_it_accrues_nothing_automatically(self):
        """The accrual engine is percent-of-AUM only. A flat basis registers
        correctly and must SAY it will not accrue on its own."""
        _, payload = self._register(fee_type='Flat Annual',
                                    fee_percent_of_aum=None,
                                    fee_flat_annual=6000.0)
        ag = db.session.get(AdvisoryAgreement,
                            payload['created_agreement_id'])
        self.assertEqual(ag.fee_flat_annual, 6000.0)
        self.assertEqual(ag.total_base_fee_rate, 0.0)
        self.assertIn('accrues nothing automatically',
                      ' '.join(payload['not_yet_configured']))

    def test_summary_matches_the_read_tool_shape(self):
        _, payload = self._register()
        aid = payload['created_agreement_id']
        _, read = self._payload('get_advisory_agreement_summary',
                                {'agreement_id': aid})
        self.assertEqual(sorted(payload['summary']), sorted(read))
        # The agreement is serialized, not repr'd into a string.
        self.assertIsInstance(read['agreement'], dict)
        self.assertEqual(read['agreement']['id'], aid)
        self.assertEqual(read['agreement']['client_entity'], CLIENT_ENTITY)

    def test_listing_reports_masks_and_activity(self):
        self._register()
        _, listing = self._payload('get_advisory_agreement_summary')
        self.assertEqual(listing['count'], 1)
        row = listing['agreements'][0]
        self.assertEqual(row['managed_account_masks'], ['9401'])
        self.assertTrue(row['is_active'])
        self.assertEqual(row['advisor_entity'], ADVISOR_ENTITY)

    def test_audit_row_is_written(self):
        self._register()
        from app.models import AuditEvent
        ev = AuditEvent.query.filter_by(
            event_type='advisory_agreement_registered').first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.actor, 'mcp')


class RefusalTest(AdvisoryMcpBase):
    def _refusal(self, **overrides):
        result, _ = self._register(**overrides)
        self.assertTrue(result['isError'])
        return result['content'][0]['text']

    def test_unknown_mask(self):
        msg = self._refusal(plaid_account_mask='0000')
        self.assertIn('no account with mask', msg)
        self.assertEqual(AdvisoryAgreement.query.count(), 0)

    def test_missing_client_entity(self):
        self.assertIn('client_entity is required',
                      self._refusal(client_entity=''))

    def test_bad_objective(self):
        msg = self._refusal(objective='Wildly Aggressive')
        self.assertIn('objective must be one of', msg)

    def test_bad_billing_frequency(self):
        self.assertIn('billing_frequency must be one of',
                      self._refusal(billing_frequency='Fortnightly'))

    def test_fee_type_without_its_amount(self):
        msg = self._refusal(fee_type='Flat Annual', fee_percent_of_aum=None)
        self.assertIn('fee_flat_annual', msg)

    def test_hybrid_needs_both_amounts(self):
        msg = self._refusal(fee_type='Hybrid', fee_percent_of_aum=1.0)
        self.assertIn('fee_flat_annual', msg)

    def test_termination_before_effective(self):
        msg = self._refusal(termination_date='2025-06-01')
        self.assertIn('on or before', msg)

    def test_bad_date_format(self):
        self.assertIn('ISO date', self._refusal(effective_date='01/01/2026'))

    def test_second_active_agreement_on_one_account(self):
        result, first = self._register()
        self.assertFalse(result['isError'])
        msg = self._refusal(agreement_name='A second agreement')
        self.assertIn('already governed by active advisory agreement', msg)
        self.assertIn(f"#{first['created_agreement_id']}", msg)
        self.assertEqual(AdvisoryAgreement.query.count(), 1)

    def test_a_terminated_agreement_frees_the_account(self):
        _, first = self._register()
        self._payload('update_advisory_agreement', {
            'agreement_id': first['created_agreement_id'],
            'status': 'terminated', 'termination_date': '2026-06-30'})
        result, second = self._register(agreement_name='Successor agreement')
        self.assertFalse(result['isError'], result['content'][0]['text'])
        self.assertNotEqual(second['created_agreement_id'],
                            first['created_agreement_id'])


class AmendmentTest(AdvisoryMcpBase):
    def test_amendment_supersedes_rather_than_mutates(self):
        _, created = self._register()
        old_id = created['created_agreement_id']
        result, payload = self._payload('update_advisory_agreement', {
            'agreement_id': old_id, 'fee_percent_of_aum': 1.25,
            'document_reference': 'GOV-DOC-0002.pdf'})
        self.assertFalse(result['isError'], result['content'][0]['text'])
        new_id = payload['updated_agreement_id']
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(payload['superseded_agreement_id'], old_id)
        self.assertEqual(payload['amended_fields'],
                         ['document_reference', 'fee_percent_of_aum'])

        old = db.session.get(AdvisoryAgreement, old_id)
        new = db.session.get(AdvisoryAgreement, new_id)
        # The old terms are untouched and reconstructible.
        self.assertEqual(old.total_base_fee_rate, 0.01)
        self.assertEqual(old.document_reference, 'GOV-DOC-0001.pdf')
        self.assertEqual(old.status, 'superseded')
        self.assertEqual(old.superseded_by, new_id)
        self.assertFalse(old.is_active())
        # The new version carries the amendment and inherits everything else.
        self.assertEqual(new.total_base_fee_rate, 0.0125)
        self.assertEqual(new.document_reference, 'GOV-DOC-0002.pdf')
        self.assertEqual(new.client_entity, CLIENT_ENTITY)
        self.assertEqual(new.account_ids(), ['brk-9401'])
        self.assertTrue(new.is_active())

    def test_history_follows_the_live_agreement(self):
        _, created = self._register()
        old_id = created['created_agreement_id']
        db.session.add(DailyAUM(agreement_id=old_id, date=date(2026, 4, 1),
                                total_market_value=250_000.0,
                                fee_accrual_daily=6.85,
                                cumulative_fee_accrual_qtd=6.85))
        db.session.add(AdvisoryFeeAccrual(
            agreement_id=old_id, accrual_date=date(2026, 3, 31),
            fee_type='base', period_label='2026-Q1', amount=612.5))
        db.session.commit()

        _, payload = self._payload('update_advisory_agreement', {
            'agreement_id': old_id, 'fee_percent_of_aum': 1.25})
        new_id = payload['updated_agreement_id']
        self.assertEqual(DailyAUM.query.filter_by(agreement_id=old_id).count(), 0)
        self.assertEqual(DailyAUM.query.filter_by(agreement_id=new_id).count(), 1)
        self.assertEqual(
            AdvisoryFeeAccrual.query.filter_by(agreement_id=new_id).count(), 1)
        self.assertEqual(payload['summary']['accruals'][0]['amount'], 612.5)

    def test_amending_a_superseded_version_is_refused(self):
        _, created = self._register()
        old_id = created['created_agreement_id']
        self._payload('update_advisory_agreement',
                      {'agreement_id': old_id, 'fee_percent_of_aum': 1.25})
        result, _ = self._payload('update_advisory_agreement',
                                  {'agreement_id': old_id,
                                   'fee_percent_of_aum': 1.5})
        self.assertTrue(result['isError'])
        msg = result['content'][0]['text']
        self.assertIn('superseded', msg)
        self.assertIn('Amend the one that superseded it', msg)

    def test_empty_patch_is_refused(self):
        _, created = self._register()
        result, _ = self._payload(
            'update_advisory_agreement',
            {'agreement_id': created['created_agreement_id']})
        self.assertTrue(result['isError'])
        self.assertIn('nothing to amend', result['content'][0]['text'])

    def test_unknown_id_is_a_tool_error_not_a_500(self):
        result, _ = self._payload('update_advisory_agreement',
                                  {'agreement_id': 9999, 'objective': 'Income'})
        self.assertTrue(result['isError'])
        self.assertIn('no advisory agreement id 9999',
                      result['content'][0]['text'])

    def test_status_superseded_cannot_be_passed_in(self):
        _, created = self._register()
        result, _ = self._payload(
            'update_advisory_agreement',
            {'agreement_id': created['created_agreement_id'],
             'status': 'superseded'})
        self.assertTrue(result['isError'])
        self.assertIn('set by an amendment', result['content'][0]['text'])

    def test_managed_account_cannot_be_amended(self):
        """plaid_account_mask is not amendable — passing it changes nothing and,
        alone, is an empty patch."""
        _, created = self._register()
        result, _ = self._payload(
            'update_advisory_agreement',
            {'agreement_id': created['created_agreement_id'],
             'plaid_account_mask': '3194'})
        self.assertTrue(result['isError'])
        self.assertIn('nothing to amend', result['content'][0]['text'])


class ToolAdvertisementTest(AdvisoryMcpBase):
    def test_both_tools_are_advertised_as_mutating(self):
        body = self._rpc('tools/list').get_json()
        tools = {t['name']: t for t in body['result']['tools']}
        for name in ('create_advisory_agreement', 'update_advisory_agreement'):
            self.assertIn(name, tools)
            self.assertIn('MUTATING', tools[name]['description'])
            self.assertIn('kill switch', tools[name]['description'])
        create = tools['create_advisory_agreement']
        self.assertEqual(
            sorted(create['inputSchema']['required']),
            ['advisor_entity', 'agreement_name', 'client_entity',
             'effective_date', 'plaid_account_mask'])

    def test_admin_page_lists_the_new_switches(self):
        body = self.client_.get('/admin/mcp').data.decode()
        self.assertIn('create_advisory_agreement', body)
        self.assertIn('update_advisory_agreement', body)


class MigrationTest(AdvisoryMcpBase):
    """A v0.7.3 install already HAS advisory_agreements, so create_all() will
    not add the v0.7.4 columns — each has to arrive as an inspected ADD."""

    V074_COLUMNS = ('termination_date', 'client_entity', 'advisor_entity',
                    'objective', 'investment_horizon_years', 'fee_type',
                    'fee_flat_annual', 'billing_frequency',
                    'document_reference', 'superseded_by')

    def test_columns_are_added_to_a_pre_v074_table(self):
        with db.engine.begin() as conn:
            for col in self.V074_COLUMNS:
                conn.execute(text(
                    f'ALTER TABLE advisory_agreements DROP COLUMN {col}'))
        cols = {c['name'] for c in
                inspect(db.engine).get_columns('advisory_agreements')}
        self.assertFalse(cols & set(self.V074_COLUMNS))

        run_migrations()

        cols = {c['name'] for c in
                inspect(db.engine).get_columns('advisory_agreements')}
        for col in self.V074_COLUMNS:
            self.assertIn(col, cols)

    def test_re_running_is_a_no_op(self):
        run_migrations()
        run_migrations()
        cols = {c['name'] for c in
                inspect(db.engine).get_columns('advisory_agreements')}
        self.assertIn('client_entity', cols)

    def test_a_pre_v074_agreement_reads_as_active_and_unamended(self):
        """The backfill contract: blank terms, open-ended, never superseded."""
        ag = AdvisoryAgreement(name='Legacy agreement', client_company='X',
                               manager_name='Y', effective_date=date(2025, 1, 1),
                               managed_account_ids=['brk-9401'])
        db.session.add(ag)
        db.session.commit()
        self.assertTrue(ag.is_active())
        self.assertIsNone(ag.superseded_by)
        self.assertEqual(ag.to_dict()['client_entity'], '')
        self.assertIsNone(ag.to_dict()['termination_date'])
