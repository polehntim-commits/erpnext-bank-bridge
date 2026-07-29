# SPDX-License-Identifier: MIT
"""Per-rule ERPNext Cost Center (v0.7.3; both legs since v0.8.5).

A categorization rule may name a Cost Center, which is written onto every line
of every Journal Entry the rule generates — the value-chain segment the cost
belongs to (Harvest, Irrigation, Crop Protection …). An unset cost_center writes
NO key at all so ERPNext applies the Account's or Company's own default
server-side.

v0.8.5 moved the BANK leg from "never gets one" to "mirrors the offset leg";
the bank-leg override that makes the old behaviour reachable per-rule lives in
tests/test_je_cost_center_both_legs.py, which owns the new contract. The three
JE-build cases below assert the v0.7.3 half of it that survives unchanged: the
offset line always carries the rule's own value, and an unset rule stamps
nothing anywhere.

Covered here:
  * schema — the column is added on an upgrading database, idempotently, and
    every existing rule migrates to NULL (= write none)
  * JE build — the offset line under both directions, the deprecated
    debit/credit pair, and no key at all when unset
  * MCP create_rule — accepts cost_center, backwards-compatible without it
  * MCP update_rule — kill switch (its OWN, separate from create_rule),
    partial patch, clone-and-supersede, archived refusal, validation refusals
  * MCP list_rules — reports cost_center on every row
  * validation — refuses a Cost Center ERPNext positively denies, accepts one
    it merely can't check (unconfigured / unreachable)
  * the admin rule editor round-trips it, so an edit there cannot silently
    clear a cost center set through MCP

    cd app
    python3 -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from sqlalchemy import inspect, text  # noqa: E402

from app import categorization, create_app, crypto, db  # noqa: E402
from app import erpnext_bank, mcp_settings, migrations  # noqa: E402
from app.models import CategorizationRule  # noqa: E402

from tests.test_categorization import Base  # noqa: E402
from tests.test_mcp_server import McpBase  # noqa: E402


# ── a Cost Center oracle, the only ERPNext surface validation touches ────────
class FakeCostCenterClient:
    """The two calls erpnext_bank.cost_center_exists makes. `leaves` are
    bookable Cost Centers, `groups` are group nodes ERPNext refuses to book
    against, and `raises` models an ERPNext that answers with an error (which
    must yield NO verdict rather than a denial)."""

    def __init__(self, leaves=(), groups=(), raises=None):
        self.leaves = set(leaves)
        self.groups = set(groups)
        self.raises = raises
        self.calls = []

    def get_doc(self, doctype, name):
        self.calls.append(('get_doc', doctype, name))
        if self.raises is not None:
            raise self.raises
        if name in self.leaves:
            return {'name': name, 'is_group': 0}
        if name in self.groups:
            return {'name': name, 'is_group': 1}
        return None                      # the real client's 404

    def list_docs(self, doctype, filters=None, fields=None,
                  limit_page_length=0, order_by=None):
        self.calls.append(('list_docs', doctype, filters))
        if self.raises is not None:
            raise self.raises
        want = ''
        for f in (filters or []):
            if f[0] == 'cost_center_name' and f[1] == '=':
                want = f[2]
        # A bare cost_center_name resolves only if some leaf docname carries it
        # as its first segment ('Harvest' → 'Harvest - OML').
        hits = [n for n in self.leaves if n.split(' - ')[0] == want]
        return [{'name': n} for n in hits]


# ── schema ──────────────────────────────────────────────────────────────────
class CostCenterMigrationTest(unittest.TestCase):
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
        self.assertIn('cost_center', self._columns())
        migrations.run_migrations()          # second boot
        migrations.run_migrations()          # third
        self.assertIn('cost_center', self._columns())

    def test_upgrading_database_gains_the_column_with_existing_rules_null(self):
        """The v0.7.2 → v0.7.3 upgrade: a rule saved before the column existed
        must survive it and read as "write no cost center"."""
        with db.engine.begin() as conn:
            conn.execute(text(
                'ALTER TABLE categorization_rules DROP COLUMN cost_center'))
            # Company-SCOPED, so the unrelated agnostic-offset→logical-name
            # migration leaves the offset alone and this test asserts only what
            # the cost_center ADD does.
            conn.execute(text(
                "INSERT INTO categorization_rules "
                "(name, priority, active, match_type, match_value, "
                " offset_account, applies_to_company, archived) "
                "VALUES ('legacy', 100, 1, 'merchant_contains', 'NAPA', "
                "'Repairs - OML', 'Orchard Meadow, LLC', 0)"))
        self.assertNotIn('cost_center', self._columns())

        migrations.run_migrations()

        self.assertIn('cost_center', self._columns())
        rule = CategorizationRule.query.filter_by(name='legacy').one()
        self.assertIsNone(rule.cost_center)
        self.assertEqual(rule.to_dict()['cost_center'], '')
        # And the rule still posts exactly as it did before the upgrade.
        self.assertEqual(rule.offset_account, 'Repairs - OML')


# ── JE construction ─────────────────────────────────────────────────────────
class CostCenterJournalEntryTest(Base):
    def test_the_offset_line_carries_it(self):
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Perennial Care - OML')
        doc = categorization.build_journal_entry(rule, self._row(amount=42.5),
                                                 'OML')
        offset, _bank = doc['accounts']
        self.assertEqual(offset['account'], 'Repairs - OML')
        self.assertEqual(offset['cost_center'], 'Perennial Care - OML')

    def test_it_follows_the_offset_line_when_the_direction_reverses(self):
        """An inflow puts the offset line SECOND in the accounts list — the
        cost center has to travel with the line, not with the position."""
        rule = self._rule(offset_account='Fruit Sales - OML',
                          cost_center='Sales & Marketing - OML')
        doc = categorization.build_journal_entry(rule, self._row(amount=-42.5),
                                                 'OML')
        _bank, offset = doc['accounts']
        self.assertEqual(offset['account'], 'Fruit Sales - OML')
        self.assertEqual(offset['credit_in_account_currency'], 42.5)
        self.assertEqual(offset['cost_center'], 'Sales & Marketing - OML')

    def test_unset_writes_no_key_at_all(self):
        """Not '', not the Company default — NO key, so ERPNext's own
        Account/Company default is what fills the field."""
        rule = self._rule(offset_account='Repairs - OML')
        doc = categorization.build_journal_entry(rule, self._row(), 'OML')
        for line in doc['accounts']:
            self.assertNotIn('cost_center', line)

    def test_blank_and_whitespace_are_treated_as_unset(self):
        for i, value in enumerate(('', '   ')):
            with self.subTest(value=repr(value)):
                rule = self._rule(offset_account='Repairs - OML',
                                  cost_center=value, match_value=f'x{i}')
                doc = categorization.build_journal_entry(
                    rule, self._row(tid=f'blank-{i}'), 'OML')
                for line in doc['accounts']:
                    self.assertNotIn('cost_center', line)

    def test_the_deprecated_debit_credit_pair_costs_its_non_bank_side(self):
        rule = self._rule(offset_account='', cost_center='Harvest - OML')
        doc = categorization.build_journal_entry(rule, self._row(amount=42.5),
                                                 'OML')
        categorized, _bank = doc['accounts']
        self.assertEqual(categorized['account'], 'Fuel Expense - EC')
        self.assertEqual(categorized['cost_center'], 'Harvest - OML')

    def test_it_coexists_with_a_party_on_the_same_line(self):
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML', party_type='Supplier',
                          party_name='NAPA')
        doc = categorization.build_journal_entry(rule, self._row(), 'OML')
        offset = doc['accounts'][0]
        self.assertEqual(offset['party'], 'NAPA')
        self.assertEqual(offset['cost_center'], 'Harvest - OML')

    def test_an_overridden_offset_account_still_gets_the_rules_cost_center(self):
        """Mode B: the offset account is resolved per-Company by the caller,
        but the cost center is the rule's own and rides along unchanged."""
        rule = self._rule(offset_account='Repairs', applies_to_company=None,
                          cost_center='Harvest - OML')
        doc = categorization.build_journal_entry(
            rule, self._row(), 'OML', offset_account_override='Repairs - OML')
        self.assertEqual(doc['accounts'][0]['account'], 'Repairs - OML')
        self.assertEqual(doc['accounts'][0]['cost_center'], 'Harvest - OML')


# ── the ERPNext lookup itself ───────────────────────────────────────────────
class CostCenterLookupTest(Base):
    def test_a_leaf_cost_center_resolves(self):
        client = FakeCostCenterClient(leaves=('Harvest - OML',))
        self.assertIs(True,
                      erpnext_bank.cost_center_exists(client, 'Harvest - OML'))

    def test_a_group_node_is_refused(self):
        client = FakeCostCenterClient(groups=('Farm - OML',))
        self.assertIs(False,
                      erpnext_bank.cost_center_exists(client, 'Farm - OML'))

    def test_an_unknown_name_is_a_positive_denial(self):
        client = FakeCostCenterClient(leaves=('Harvest - OML',))
        self.assertIs(False,
                      erpnext_bank.cost_center_exists(client, 'Typo - OML'))

    def test_a_bare_name_resolves_through_the_fallback(self):
        client = FakeCostCenterClient(leaves=('Harvest - OML',))
        self.assertIs(True, erpnext_bank.cost_center_exists(
            client, 'Harvest', 'Orchard Meadow, LLC'))

    def test_an_erpnext_error_yields_no_verdict(self):
        from app.erpnext_client import ERPNextAPIError
        client = FakeCostCenterClient(
            leaves=('Harvest - OML',),
            raises=ERPNextAPIError('boom', status_code=500))
        self.assertIsNone(
            erpnext_bank.cost_center_exists(client, 'Harvest - OML'))

    def test_no_client_or_no_name_yields_no_verdict(self):
        self.assertIsNone(erpnext_bank.cost_center_exists(None, 'Harvest - OML'))
        self.assertIsNone(
            erpnext_bank.cost_center_exists(FakeCostCenterClient(), ''))


# ── MCP tools ───────────────────────────────────────────────────────────────
class CostCenterMcpBase(McpBase):
    def _rule(self, **kw):
        defaults = dict(name='napa', priority=100, active=True,
                        match_type='merchant_contains', match_value='NAPA',
                        offset_account='Repairs - OML')
        defaults.update(kw)
        rule = CategorizationRule(**defaults)
        db.session.add(rule)
        db.session.commit()
        return rule

    def _payload(self, body):
        self.assertFalse(body['result']['isError'],
                         body['result']['content'][0]['text'])
        return json.loads(body['result']['content'][0]['text'])

    def _error_text(self, body):
        self.assertTrue(body['result']['isError'])
        return body['result']['content'][0]['text']

    def _stub_erpnext(self, client):
        """Point the cost-center validation at `client` (None = unconfigured)."""
        from app import erpnext_settings
        real_configured = erpnext_settings.is_configured
        real_get_client = erpnext_bank.get_client
        erpnext_settings.is_configured = lambda: client is not None
        erpnext_bank.get_client = lambda **kw: client
        self.addCleanup(setattr, erpnext_settings, 'is_configured',
                        real_configured)
        self.addCleanup(setattr, erpnext_bank, 'get_client', real_get_client)


class CreateRuleCostCenterTest(CostCenterMcpBase):
    def setUp(self):
        super().setUp()
        mcp_settings.save({'create_rule': True})

    def test_cost_center_is_persisted(self):
        self._stub_erpnext(FakeCostCenterClient(leaves=('Harvest - OML',)))
        _, body = self._call_tool('create_rule', {
            'match_type': 'merchant_contains', 'match_value': 'COASTAL FARM',
            'offset_account': 'Crop Protection - OML',
            'cost_center': 'Harvest - OML'})
        payload = self._payload(body)
        self.assertEqual(payload['created_rule']['cost_center'],
                         'Harvest - OML')
        self.assertEqual(CategorizationRule.query.one().cost_center,
                         'Harvest - OML')

    def test_omitting_it_still_works_and_stores_null(self):
        """Backwards compatibility: the pre-v0.7.3 call shape is unchanged."""
        _, body = self._call_tool('create_rule', {
            'match_type': 'merchant_contains', 'match_value': 'TESTVENDOR',
            'offset_account': '5000 - Test', 'tag': 'test'})
        payload = self._payload(body)
        self.assertEqual(payload['created_rule']['cost_center'], '')
        self.assertIsNone(CategorizationRule.query.one().cost_center)

    def test_a_denied_cost_center_is_refused_and_no_rule_is_created(self):
        self._stub_erpnext(FakeCostCenterClient(leaves=('Harvest - OML',)))
        _, body = self._call_tool('create_rule', {
            'match_type': 'merchant_contains', 'match_value': 'COASTAL FARM',
            'offset_account': 'Crop Protection - OML',
            'cost_center': 'Harvset - OML'})     # typo
        self.assertIn('not a Cost Center', self._error_text(body))
        self.assertEqual(CategorizationRule.query.count(), 0)

    def test_an_unreachable_erpnext_accepts_the_value(self):
        """No verdict is not a denial — a transient outage must not reject an
        operator's input."""
        from app.erpnext_client import ERPNextAPIError
        self._stub_erpnext(FakeCostCenterClient(
            raises=ERPNextAPIError('down', status_code=500)))
        _, body = self._call_tool('create_rule', {
            'match_type': 'merchant_contains', 'match_value': 'COASTAL FARM',
            'offset_account': 'Crop Protection - OML',
            'cost_center': 'Harvest - OML'})
        self.assertEqual(self._payload(body)['created_rule']['cost_center'],
                         'Harvest - OML')

    def test_an_unconfigured_erpnext_accepts_the_value(self):
        self._stub_erpnext(None)
        _, body = self._call_tool('create_rule', {
            'match_type': 'merchant_contains', 'match_value': 'ANTHROPIC',
            'offset_account': 'Software - OML',
            'cost_center': 'Support - OML'})
        self.assertEqual(self._payload(body)['created_rule']['cost_center'],
                         'Support - OML')


class UpdateRuleTest(CostCenterMcpBase):
    def test_blocked_when_its_kill_switch_is_off(self):
        rule = self._rule()
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'cost_center': 'Harvest - OML'})
        self.assertIn('kill switch', self._error_text(body))
        self.assertIsNone(db.session.get(CategorizationRule, rule.id).cost_center)

    def test_create_rules_switch_does_not_unlock_it(self):
        """The two switches are independent by design: trusting an AI to
        propose new rules is not trusting it to rewrite the working ones."""
        mcp_settings.save({'create_rule': True})
        rule = self._rule()
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'cost_center': 'Harvest - OML'})
        self.assertIn('kill switch', self._error_text(body))

    def test_it_supersedes_rather_than_mutating(self):
        mcp_settings.save({'update_rule': True})
        rule = self._rule()
        old_id = rule.id
        _, body = self._call_tool('update_rule', {
            'rule_id': old_id, 'cost_center': 'Perennial Care - OML'})
        payload = self._payload(body)
        new_id = payload['updated_rule']['id']

        self.assertNotEqual(new_id, old_id)
        self.assertEqual(payload['superseded_rule_id'], old_id)
        old = db.session.get(CategorizationRule, old_id)
        self.assertTrue(old.archived)
        self.assertFalse(old.active)
        self.assertEqual(old.superseded_by, new_id)
        self.assertIsNone(old.cost_center)          # history is not rewritten
        new = db.session.get(CategorizationRule, new_id)
        self.assertEqual(new.cost_center, 'Perennial Care - OML')
        self.assertFalse(new.archived)

    def test_unpassed_fields_are_carried_across_untouched(self):
        mcp_settings.save({'update_rule': True})
        rule = self._rule(match_value='NAPA AUTO', priority=42,
                          party_type='Supplier', party_name='NAPA',
                          bb_internal_tag='shop_supplies',
                          description_template='{{merchant_name}} parts',
                          offset_direction='always_debit', skip_party=False,
                          applies_to_company='Orchard Meadow, LLC')
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'cost_center': 'Harvest - OML'})
        new = db.session.get(CategorizationRule,
                             self._payload(body)['updated_rule']['id'])
        self.assertEqual(new.match_value, 'NAPA AUTO')
        self.assertEqual(new.priority, 42)
        self.assertEqual(new.party_type, 'Supplier')
        self.assertEqual(new.party_name, 'NAPA')
        self.assertEqual(new.bb_internal_tag, 'shop_supplies')
        self.assertEqual(new.description_template, '{{merchant_name}} parts')
        self.assertEqual(new.offset_direction, 'always_debit')
        self.assertEqual(new.applies_to_company, 'Orchard Meadow, LLC')
        self.assertEqual(new.cost_center, 'Harvest - OML')

    def test_every_documented_field_can_be_patched(self):
        mcp_settings.save({'update_rule': True})
        rule = self._rule()
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'name': 'NAPA parts',
            'match_type': 'merchant_exact', 'match_value': 'NAPA Auto Parts',
            'offset_account': 'Repairs & Maintenance - OML',
            'cost_center': 'Perennial Care - OML', 'active': False,
            'priority': 20, 'tag': 'shop_supplies',
            'applies_to_company': 'Orchard Meadow, LLC'})
        new = db.session.get(CategorizationRule,
                             self._payload(body)['updated_rule']['id'])
        self.assertEqual(new.name, 'NAPA parts')
        self.assertEqual(new.match_type, 'merchant_exact')
        self.assertEqual(new.match_value, 'NAPA Auto Parts')
        self.assertEqual(new.offset_account, 'Repairs & Maintenance - OML')
        self.assertEqual(new.cost_center, 'Perennial Care - OML')
        self.assertFalse(new.active)
        self.assertEqual(new.priority, 20)
        self.assertEqual(new.bb_internal_tag, 'shop_supplies')
        self.assertEqual(new.applies_to_company, 'Orchard Meadow, LLC')

    def test_an_empty_cost_center_clears_it(self):
        mcp_settings.save({'update_rule': True})
        rule = self._rule(cost_center='Harvest - OML')
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'cost_center': ''})
        new = db.session.get(CategorizationRule,
                             self._payload(body)['updated_rule']['id'])
        self.assertIsNone(new.cost_center)

    def test_an_archived_rule_is_refused(self):
        mcp_settings.save({'update_rule': True})
        rule = self._rule(archived=True, active=False)
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'cost_center': 'Harvest - OML'})
        self.assertIn('archived', self._error_text(body))

    def test_an_unknown_rule_is_refused(self):
        mcp_settings.save({'update_rule': True})
        _, body = self._call_tool('update_rule', {'rule_id': 9999,
                                                  'active': False})
        self.assertIn('no rule id 9999', self._error_text(body))

    def test_an_unknown_match_type_is_refused_and_nothing_is_written(self):
        mcp_settings.save({'update_rule': True})
        rule = self._rule()
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'match_type': 'vibes'})
        self.assertIn('unknown match_type', self._error_text(body))
        self.assertEqual(CategorizationRule.query.count(), 1)
        self.assertFalse(db.session.get(CategorizationRule, rule.id).archived)

    def test_a_denied_cost_center_is_refused_and_nothing_is_written(self):
        mcp_settings.save({'update_rule': True})
        self._stub_erpnext(FakeCostCenterClient(leaves=('Harvest - OML',)))
        rule = self._rule()
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'cost_center': 'Nope - OML'})
        self.assertIn('not a Cost Center', self._error_text(body))
        self.assertEqual(CategorizationRule.query.count(), 1)
        self.assertFalse(db.session.get(CategorizationRule, rule.id).archived)

    def test_an_empty_patch_is_refused(self):
        mcp_settings.save({'update_rule': True})
        rule = self._rule()
        _, body = self._call_tool('update_rule', {'rule_id': rule.id})
        self.assertIn('nothing to update', self._error_text(body))
        self.assertFalse(db.session.get(CategorizationRule, rule.id).archived)

    def test_a_non_integer_rule_id_is_refused(self):
        mcp_settings.save({'update_rule': True})
        _, body = self._call_tool('update_rule', {'rule_id': 'the napa one'})
        self.assertIn('rule_id must be an integer', self._error_text(body))

    def test_it_writes_a_rule_updated_audit_event(self):
        from app.models import AuditEvent
        mcp_settings.save({'update_rule': True})
        rule = self._rule()
        self._call_tool('update_rule', {'rule_id': rule.id,
                                        'cost_center': 'Harvest - OML'})
        ev = (AuditEvent.query
              .filter(AuditEvent.event_type == 'rule_updated').one())
        self.assertIn(f'#{rule.id} superseded by', ev.notes)
        self.assertIn('cost_center', ev.notes)

    def test_it_is_registered_as_a_mutating_tool_with_a_kill_switch(self):
        tools = {t['name']: t
                 for t in self._rpc('tools/list').get_json()['result']['tools']}
        self.assertIn('update_rule', tools)
        self.assertIn('MUTATING', tools['update_rule']['description'])
        self.assertIn('update_rule', mcp_settings.load())
        self.assertFalse(mcp_settings.load()['update_rule'])   # default OFF


class ListRulesCostCenterTest(CostCenterMcpBase):
    def test_every_row_reports_its_cost_center(self):
        self._rule(match_value='NAPA', cost_center='Perennial Care - OML')
        self._rule(match_value='ANTHROPIC', priority=110)
        _, body = self._call_tool('list_rules')
        rows = {r['match_value']: r for r in self._payload(body)['rules']}
        self.assertEqual(rows['NAPA']['cost_center'], 'Perennial Care - OML')
        self.assertEqual(rows['ANTHROPIC']['cost_center'], '')


# ── the admin editor must not silently drop it ──────────────────────────────
class AdminRuleEditorCostCenterTest(Base):
    """A rule edit on /admin/rules CLONES the rule from the posted form, so a
    cost center the form doesn't carry would be cleared on every save — losing
    the value an operator (or MCP) had set."""

    def setUp(self):
        super().setUp()
        self.http = self.app.test_client()

    def test_an_edit_round_trips_the_cost_center(self):
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML')
        page = self.http.get(f'/admin/rules?edit={rule.id}')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Harvest - OML', page.get_data(as_text=True))

        resp = self.http.post('/admin/rules/save', data={
            'id': str(rule.id), 'name': 'napa', 'priority': '100',
            'active': 'on', 'match_type': 'merchant_contains',
            'match_value': 'NAPA', 'offset_account': 'Repairs - OML',
            'offset_direction': 'auto', 'cost_center': 'Harvest - OML'})
        self.assertEqual(resp.status_code, 302)
        live = CategorizationRule.query.filter_by(archived=False).one()
        self.assertEqual(live.cost_center, 'Harvest - OML')

    def test_clearing_the_field_clears_the_cost_center(self):
        rule = self._rule(offset_account='Repairs - OML',
                          cost_center='Harvest - OML')
        self.http.post('/admin/rules/save', data={
            'id': str(rule.id), 'name': 'napa', 'priority': '100',
            'active': 'on', 'match_type': 'merchant_contains',
            'match_value': 'NAPA', 'offset_account': 'Repairs - OML',
            'offset_direction': 'auto', 'cost_center': ''})
        live = CategorizationRule.query.filter_by(archived=False).one()
        self.assertIsNone(live.cost_center)


class McpUpdateRuleParty(CostCenterMcpBase):
    """v0.9.0 · the party fields reach update_rule.

    Without them an AI operator could set a rule's cost center but not WHO the
    money went to — which is the half of ACC-JV-2026-02312 that actually
    mattered, and closing it would have meant a human in the Rules editor for
    every rule."""

    def test_it_sets_the_party_type_and_name(self):
        mcp_settings.save({'update_rule': True})
        rule = self._rule()
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'party_type': 'Supplier',
            'party_name': 'Sorren'})
        new = db.session.get(CategorizationRule,
                             self._payload(body)['updated_rule']['id'])
        self.assertEqual(new.party_type, 'Supplier')
        self.assertEqual(new.party_name, 'Sorren')

    def test_it_accepts_the_two_party_types_v0_9_0_added(self):
        mcp_settings.save({'update_rule': True})
        for party_type in ('Employee', 'Shareholder'):
            rule = self._rule()
            _, body = self._call_tool('update_rule', {
                'rule_id': rule.id, 'party_type': party_type})
            new = db.session.get(CategorizationRule,
                                 self._payload(body)['updated_rule']['id'])
            self.assertEqual(new.party_type, party_type)

    def test_it_normalizes_the_case(self):
        mcp_settings.save({'update_rule': True})
        rule = self._rule()
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'party_type': 'supplier'})
        new = db.session.get(CategorizationRule,
                             self._payload(body)['updated_rule']['id'])
        self.assertEqual(new.party_type, 'Supplier')

    def test_it_refuses_a_party_type_erpnext_does_not_know(self):
        """This value is handed to ERPNext AS A DOCTYPE. An unchecked 'Vendor'
        would produce Journal Entries that fail at submit — the v0.4.0.9 failure
        mode arriving through a new door."""
        mcp_settings.save({'update_rule': True})
        rule = self._rule()
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'party_type': 'Vendor'})
        text = self._error_text(body)
        self.assertIn('unknown party_type', text)
        self.assertIn('Shareholder', text)      # the message lists the valid set

    def test_an_empty_party_type_clears_it(self):
        mcp_settings.save({'update_rule': True})
        rule = self._rule(party_type='Supplier', party_name='Sorren')
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'party_type': ''})
        new = db.session.get(CategorizationRule,
                             self._payload(body)['updated_rule']['id'])
        self.assertIsNone(new.party_type)

    def test_auto_create_party_stays_tri_state(self):
        """None means "inherit the global gate", which is what every pre-v0.9.0
        rule holds. Collapsing it to a bool would turn creation OFF on every
        rule an AI operator touched."""
        mcp_settings.save({'update_rule': True})
        rule = self._rule()
        self.assertIsNone(rule.auto_create_party)

        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'auto_create_party': True})
        rid = self._payload(body)['updated_rule']['id']
        self.assertIs(db.session.get(CategorizationRule, rid)
                      .auto_create_party, True)

        _, body = self._call_tool('update_rule', {
            'rule_id': rid, 'auto_create_party': False})
        rid = self._payload(body)['updated_rule']['id']
        self.assertIs(db.session.get(CategorizationRule, rid)
                      .auto_create_party, False)

    def test_not_passing_auto_create_party_carries_it_across(self):
        """The clone-everything-not-listed rule (_RULE_CLONE_SKIP) must keep the
        flag, so an unrelated edit does not reset it."""
        mcp_settings.save({'update_rule': True})
        rule = self._rule(auto_create_party=False)
        _, body = self._call_tool('update_rule', {
            'rule_id': rule.id, 'priority': 7})
        new = db.session.get(CategorizationRule,
                             self._payload(body)['updated_rule']['id'])
        self.assertIs(new.auto_create_party, False)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
