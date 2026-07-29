# SPDX-License-Identifier: MIT
"""The bank leg gets a cost center too (v0.8.5).

    THE BUG. Through v0.8.4 a rule's Cost Center rode the OFFSET line only.
    ERPNext's own fallback then filled the bank line with the COMPANY DEFAULT,
    so ACC-JV-2026-02312 (Sorren, $2,030) booked 6400 Professional Services to
    "310 - G and A Administration - OML" and 1261 Wells Fargo Checking to
    "Main - OML" — the cost in one segment, the cash that paid it in another,
    and no Cost-Center-wise report that balanced.

v0.8.5 mirrors the rule's cost center onto BOTH legs by default, and adds
`bank_cost_center` for the rare rule that genuinely wants a different one there
(or none at all). NOTHING IS HARD-CODED per account: the value always comes
from the rule that matched.

Covered here:
  * schema — the column is added on an upgrading database, idempotently, and
    every existing rule migrates to NULL (= mirror), which is what makes the
    upgrade itself the fix
  * the tri-state — mirror / explicit override / '(none)' sentinel
  * JE build — both legs, both directions, the deprecated debit/credit pair,
    the Mode B resolved offset, and nothing at all when the rule is uncosted
  * Party still rides the offset leg alone (ERPNext refuses one on a bank
    account) — the eligibility answer the future per-rule Party wiring inherits
  * MCP create_rule / update_rule / list_rules carry the new field, and the
    '(none)' sentinel is never sent to ERPNext for validation
  * the admin rule editor round-trips it, so an edit cannot silently clear it

    cd app
    python3 -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from sqlalchemy import inspect, text  # noqa: E402

from app import categorization, create_app, crypto, db  # noqa: E402
from app import mcp_settings, migrations  # noqa: E402
from app.models import CategorizationRule  # noqa: E402

from tests.test_categorization import Base  # noqa: E402
from tests.test_rule_cost_center import (CostCenterMcpBase,  # noqa: E402
                                         FakeCostCenterClient)

NONE = categorization.BANK_LEG_NO_COST_CENTER


# ── schema ──────────────────────────────────────────────────────────────────
class BankCostCenterMigrationTest(unittest.TestCase):
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

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _columns(self):
        return {c['name']
                for c in inspect(db.engine).get_columns('categorization_rules')}

    def test_fresh_database_has_the_column_and_re_running_is_a_noop(self):
        self.assertIn('bank_cost_center', self._columns())
        migrations.run_migrations()          # second boot
        migrations.run_migrations()          # third
        self.assertIn('bank_cost_center', self._columns())

    def test_an_upgrading_rule_migrates_to_mirror_which_is_the_fix(self):
        """The v0.8.4 → v0.8.5 upgrade. A rule saved with a cost center before
        the column existed must come out MIRRORING it — the migration adding
        NULL is precisely what starts stamping both legs."""
        with db.engine.begin() as conn:
            conn.execute(text('ALTER TABLE categorization_rules '
                              'DROP COLUMN bank_cost_center'))
            conn.execute(text(
                "INSERT INTO categorization_rules "
                "(name, priority, active, match_type, match_value, "
                " offset_account, cost_center, applies_to_company, archived) "
                "VALUES ('sorren', 100, 1, 'merchant_contains', 'SORREN', "
                "'6400 Professional Services - OML', "
                "'310 - G and A Administration - OML', "
                "'Orchard Meadow, LLC', 0)"))
        self.assertNotIn('bank_cost_center', self._columns())

        migrations.run_migrations()

        self.assertIn('bank_cost_center', self._columns())
        rule = CategorizationRule.query.filter_by(name='sorren').one()
        self.assertIsNone(rule.bank_cost_center)
        self.assertEqual(rule.to_dict()['bank_cost_center'], '')
        # NULL means mirror, so the very next JE this rule generates costs its
        # bank leg — no backfill of the column, no operator action.
        self.assertEqual(categorization.bank_leg_cost_center(rule),
                         '310 - G and A Administration - OML')


# ── the tri-state ───────────────────────────────────────────────────────────
class BankLegCostCenterTest(Base):
    def test_unset_mirrors_the_offset_cost_center(self):
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML')
        self.assertEqual(categorization.bank_leg_cost_center(rule),
                         'Harvest - OML')

    def test_blank_and_whitespace_also_mirror(self):
        for value in ('', '   '):
            with self.subTest(value=repr(value)):
                rule = self._rule(offset_account='Repairs - OML',
                                  cost_center='Harvest - OML',
                                  bank_cost_center=value)
                self.assertEqual(categorization.bank_leg_cost_center(rule),
                                 'Harvest - OML')

    def test_the_sentinel_means_write_nothing(self):
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML', bank_cost_center=NONE)
        self.assertEqual(categorization.bank_leg_cost_center(rule), '')

    def test_an_explicit_docname_wins(self):
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML',
                          bank_cost_center='Treasury - OML')
        self.assertEqual(categorization.bank_leg_cost_center(rule),
                         'Treasury - OML')

    def test_an_uncosted_rule_mirrors_nothing(self):
        rule = self._rule(offset_account='Repairs - OML')
        self.assertEqual(categorization.bank_leg_cost_center(rule), '')

    def test_the_sentinel_is_not_a_plausible_docname(self):
        """A real Cost Center is Company-suffixed, so the sentinel can never
        collide with one an operator might actually type."""
        self.assertNotIn(' - ', NONE)


# ── JE construction ─────────────────────────────────────────────────────────
class BothLegsJournalEntryTest(Base):
    def test_the_reported_bug_both_legs_now_carry_the_rules_cost_center(self):
        """ACC-JV-2026-02312, rebuilt: the cash leg no longer falls through to
        the Company default."""
        rule = self._rule(offset_account='6400 Professional Services - OML',
                          cost_center='310 - G and A Administration - OML')
        doc = categorization.build_journal_entry(
            rule, self._row(amount=2030.0), 'Orchard Meadow, LLC')
        offset, bank = doc['accounts']
        self.assertEqual(offset['debit_in_account_currency'], 2030.0)
        self.assertEqual(bank['credit_in_account_currency'], 2030.0)
        for line in (offset, bank):
            self.assertEqual(line['cost_center'],
                             '310 - G and A Administration - OML')

    def test_it_holds_when_the_direction_reverses(self):
        """A deposit puts the bank line FIRST — the stamping follows the line,
        not its position in the list."""
        rule = self._rule(offset_account='Fruit Sales - OML',
                          cost_center='Sales & Marketing - OML')
        doc = categorization.build_journal_entry(rule, self._row(amount=-42.5),
                                                 'OML')
        bank, offset = doc['accounts']
        self.assertEqual(bank['debit_in_account_currency'], 42.5)
        for line in (offset, bank):
            self.assertEqual(line['cost_center'], 'Sales & Marketing - OML')

    def test_the_deprecated_debit_credit_pair_costs_both_sides(self):
        rule = self._rule(offset_account='', cost_center='Harvest - OML')
        doc = categorization.build_journal_entry(rule, self._row(amount=42.5),
                                                 'OML')
        for line in doc['accounts']:
            self.assertEqual(line['cost_center'], 'Harvest - OML')

    def test_a_mode_b_resolved_offset_still_costs_both_legs(self):
        rule = self._rule(offset_account='Repairs', applies_to_company=None,
                          cost_center='Harvest - OML')
        doc = categorization.build_journal_entry(
            rule, self._row(), 'OML', offset_account_override='Repairs - OML')
        self.assertEqual(doc['accounts'][0]['account'], 'Repairs - OML')
        for line in doc['accounts']:
            self.assertEqual(line['cost_center'], 'Harvest - OML')

    def test_an_override_sends_the_bank_leg_somewhere_else(self):
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML',
                          bank_cost_center='Treasury - OML')
        doc = categorization.build_journal_entry(rule, self._row(amount=42.5),
                                                 'OML')
        offset, bank = doc['accounts']
        self.assertEqual(offset['cost_center'], 'Harvest - OML')
        self.assertEqual(bank['cost_center'], 'Treasury - OML')

    def test_the_sentinel_restores_the_pre_v0_8_5_shape_per_rule(self):
        """The old behaviour stays REACHABLE — but only as a deliberate choice,
        never again as the silent default."""
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML', bank_cost_center=NONE)
        doc = categorization.build_journal_entry(rule, self._row(amount=42.5),
                                                 'OML')
        offset, bank = doc['accounts']
        self.assertEqual(offset['cost_center'], 'Harvest - OML')
        self.assertNotIn('cost_center', bank)

    def test_an_uncosted_rule_still_writes_no_key_on_either_leg(self):
        """ERPNext's Account/Company defaults must keep running when the rule
        claims to know nothing — v0.8.5 does not start guessing."""
        rule = self._rule(offset_account='Repairs - OML')
        doc = categorization.build_journal_entry(rule, self._row(), 'OML')
        for line in doc['accounts']:
            self.assertNotIn('cost_center', line)

    def test_a_bank_override_on_an_uncosted_rule_still_reaches_the_bank_leg(self):
        """The two fields are independent: a rule may segment only its cash
        side (a treasury-only rule) without claiming an offset segment."""
        rule = self._rule(offset_account='Repairs - OML',
                          bank_cost_center='Treasury - OML')
        doc = categorization.build_journal_entry(rule, self._row(amount=42.5),
                                                 'OML')
        offset, bank = doc['accounts']
        self.assertNotIn('cost_center', offset)
        self.assertEqual(bank['cost_center'], 'Treasury - OML')


class PartyStaysOnTheOffsetLegTest(Base):
    """The eligibility answer the Sprint 5 per-rule Party wiring inherits.

    Cost center goes on both legs; Party does NOT, and that is ERPNext's rule
    rather than a preference of ours — JournalEntry.validate_party refuses a
    Party on any account that is not Receivable/Payable, and a bank line never
    is. Both answers now live in one function (apply_rule_dimensions), so the
    next dimension added declares its own legs there instead of each caller
    re-deriving them."""

    def test_the_party_is_on_the_offset_leg_and_the_bank_leg_has_none(self):
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML', party_type='Supplier',
                          party_name='Sorren')
        doc = categorization.build_journal_entry(rule, self._row(amount=42.5),
                                                 'OML')
        offset, bank = doc['accounts']
        self.assertEqual(offset['party'], 'Sorren')
        self.assertEqual(offset['party_type'], 'Supplier')
        self.assertNotIn('party', bank)
        self.assertNotIn('party_type', bank)
        # …while the cost center reached both.
        self.assertEqual(offset['cost_center'], 'Harvest - OML')
        self.assertEqual(bank['cost_center'], 'Harvest - OML')

    def test_apply_rule_dimensions_is_the_single_stamping_choke_point(self):
        """Named directly so the future Party wiring has one place to extend —
        if this stops being the function build_journal_entry calls, the next
        dimension will get bolted on somewhere else."""
        offset, bank = {'account': 'Repairs - OML'}, {'account': 'Bank - OML'}
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML')
        categorization.apply_rule_dimensions(rule, offset, bank)
        self.assertEqual(offset['cost_center'], 'Harvest - OML')
        self.assertEqual(bank['cost_center'], 'Harvest - OML')


# ── MCP ─────────────────────────────────────────────────────────────────────
class CreateRuleBankCostCenterTest(CostCenterMcpBase):
    def setUp(self):
        super().setUp()
        mcp_settings.save({'create_rule': True})

    def test_omitting_it_stores_null_which_means_mirror(self):
        self._stub_erpnext(FakeCostCenterClient(leaves=('Harvest - OML',)))
        _, body = self._call_tool('create_rule', {
            'match_type': 'merchant_contains', 'match_value': 'SORREN',
            'offset_account': '6400 Professional Services - OML',
            'cost_center': 'Harvest - OML'})
        payload = self._payload(body)
        self.assertEqual(payload['created_rule']['bank_cost_center'], '')
        rule = CategorizationRule.query.one()
        self.assertIsNone(rule.bank_cost_center)
        self.assertEqual(categorization.bank_leg_cost_center(rule),
                         'Harvest - OML')

    def test_an_explicit_bank_cost_center_is_persisted_and_validated(self):
        self._stub_erpnext(FakeCostCenterClient(
            leaves=('Harvest - OML', 'Treasury - OML')))
        _, body = self._call_tool('create_rule', {
            'match_type': 'merchant_contains', 'match_value': 'SORREN',
            'offset_account': 'Repairs - OML', 'cost_center': 'Harvest - OML',
            'bank_cost_center': 'Treasury - OML'})
        self.assertEqual(self._payload(body)['created_rule']['bank_cost_center'],
                         'Treasury - OML')
        self.assertEqual(CategorizationRule.query.one().bank_cost_center,
                         'Treasury - OML')

    def test_a_denied_bank_cost_center_is_refused_and_no_rule_is_created(self):
        self._stub_erpnext(FakeCostCenterClient(leaves=('Harvest - OML',)))
        _, body = self._call_tool('create_rule', {
            'match_type': 'merchant_contains', 'match_value': 'SORREN',
            'offset_account': 'Repairs - OML', 'cost_center': 'Harvest - OML',
            'bank_cost_center': 'Treasurey - OML'})       # typo
        self.assertIn('not a Cost Center', self._error_text(body))
        self.assertEqual(CategorizationRule.query.count(), 0)

    def test_the_sentinel_is_accepted_without_asking_erpnext(self):
        """'(none)' is not a docname. Looking it up would draw a positive
        denial and refuse a perfectly legitimate call."""
        client = FakeCostCenterClient(leaves=('Harvest - OML',))
        self._stub_erpnext(client)
        _, body = self._call_tool('create_rule', {
            'match_type': 'merchant_contains', 'match_value': 'SORREN',
            'offset_account': 'Repairs - OML', 'cost_center': 'Harvest - OML',
            'bank_cost_center': NONE})
        self.assertEqual(self._payload(body)['created_rule']['bank_cost_center'],
                         NONE)
        self.assertNotIn(NONE, [c[-1] for c in client.calls])


class UpdateRuleBankCostCenterTest(CostCenterMcpBase):
    def setUp(self):
        super().setUp()
        mcp_settings.save({'update_rule': True})

    def test_it_can_be_patched_and_supersedes_like_every_other_field(self):
        rule = self._rule(cost_center='Harvest - OML')
        self._stub_erpnext(FakeCostCenterClient(
            leaves=('Harvest - OML', 'Treasury - OML')))
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'bank_cost_center': 'Treasury - OML'})
        payload = self._payload(body)
        new = db.session.get(CategorizationRule, payload['updated_rule']['id'])
        self.assertEqual(new.bank_cost_center, 'Treasury - OML')
        self.assertEqual(new.cost_center, 'Harvest - OML')   # carried across
        self.assertIsNone(db.session.get(CategorizationRule,
                                         rule.id).bank_cost_center)

    def test_an_empty_value_restores_the_mirror_default(self):
        rule = self._rule(cost_center='Harvest - OML',
                          bank_cost_center='Treasury - OML')
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'bank_cost_center': ''})
        new = db.session.get(CategorizationRule,
                             self._payload(body)['updated_rule']['id'])
        self.assertIsNone(new.bank_cost_center)
        self.assertEqual(categorization.bank_leg_cost_center(new),
                         'Harvest - OML')

    def test_a_denied_value_is_refused_and_nothing_is_written(self):
        self._stub_erpnext(FakeCostCenterClient(leaves=('Harvest - OML',)))
        rule = self._rule(cost_center='Harvest - OML')
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'bank_cost_center': 'Nope - OML'})
        self.assertIn('not a Cost Center', self._error_text(body))
        self.assertEqual(CategorizationRule.query.count(), 1)
        self.assertFalse(db.session.get(CategorizationRule, rule.id).archived)

    def test_an_unrelated_patch_carries_it_across_untouched(self):
        rule = self._rule(cost_center='Harvest - OML', bank_cost_center=NONE)
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'priority': 42})
        new = db.session.get(CategorizationRule,
                             self._payload(body)['updated_rule']['id'])
        self.assertEqual(new.bank_cost_center, NONE)


class ListRulesBankCostCenterTest(CostCenterMcpBase):
    def test_every_row_reports_it(self):
        self._rule(match_value='SORREN', cost_center='Harvest - OML')
        self._rule(match_value='NAPA', priority=110,
                   cost_center='Harvest - OML', bank_cost_center=NONE)
        _, body = self._call_tool('list_rules')
        rows = {r['match_value']: r for r in self._payload(body)['rules']}
        self.assertEqual(rows['SORREN']['bank_cost_center'], '')
        self.assertEqual(rows['NAPA']['bank_cost_center'], NONE)

    def test_the_tool_schemas_advertise_it(self):
        tools = {t['name']: t
                 for t in self._rpc('tools/list').get_json()['result']['tools']}
        for name in ('create_rule', 'update_rule'):
            with self.subTest(tool=name):
                self.assertIn('bank_cost_center',
                              tools[name]['inputSchema']['properties'])


# ── the admin editor must not silently drop it ──────────────────────────────
class AdminRuleEditorBankCostCenterTest(Base):
    """A rule edit CLONES the rule from the posted form, so a field the form
    doesn't carry is cleared on every save (the v0.7.3 lesson, re-learned for
    every column added since)."""

    def setUp(self):
        super().setUp()
        self.http = self.app.test_client()

    def test_the_field_is_on_the_form(self):
        page = self.http.get('/admin/rules')
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="bank_cost_center"', page.get_data(as_text=True))

    def test_an_edit_round_trips_it(self):
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML',
                          bank_cost_center='Treasury - OML')
        page = self.http.get(f'/admin/rules?edit={rule.id}')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Treasury - OML', page.get_data(as_text=True))

        resp = self.http.post('/admin/rules/save', data={
            'id': str(rule.id), 'name': 'napa', 'priority': '100',
            'active': 'on', 'match_type': 'merchant_contains',
            'match_value': 'NAPA', 'offset_account': 'Repairs - OML',
            'offset_direction': 'auto', 'cost_center': 'Harvest - OML',
            'bank_cost_center': 'Treasury - OML'})
        self.assertEqual(resp.status_code, 302)
        live = CategorizationRule.query.filter_by(archived=False).one()
        self.assertEqual(live.bank_cost_center, 'Treasury - OML')

    def test_clearing_the_field_restores_the_mirror_default(self):
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML',
                          bank_cost_center='Treasury - OML')
        self.http.post('/admin/rules/save', data={
            'id': str(rule.id), 'name': 'napa', 'priority': '100',
            'active': 'on', 'match_type': 'merchant_contains',
            'match_value': 'NAPA', 'offset_account': 'Repairs - OML',
            'offset_direction': 'auto', 'cost_center': 'Harvest - OML',
            'bank_cost_center': ''})
        live = CategorizationRule.query.filter_by(archived=False).one()
        self.assertIsNone(live.bank_cost_center)

    def test_the_sentinel_survives_a_save(self):
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML')
        self.http.post('/admin/rules/save', data={
            'id': str(rule.id), 'name': 'napa', 'priority': '100',
            'active': 'on', 'match_type': 'merchant_contains',
            'match_value': 'NAPA', 'offset_account': 'Repairs - OML',
            'offset_direction': 'auto', 'cost_center': 'Harvest - OML',
            'bank_cost_center': NONE})
        live = CategorizationRule.query.filter_by(archived=False).one()
        self.assertEqual(live.bank_cost_center, NONE)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
