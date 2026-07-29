# SPDX-License-Identifier: MIT
"""The v0.9.0 Rules-editor party surface: the widened Party Type list, the
tri-state `auto_create_party`, and the LIVE party autocomplete.

The properties under test:

  * The party list offers all four ERPNext Party Types, and only three of them
    are ever created — an Employee needs a date of birth Bank Bridge does not
    have.
  * `auto_create_party` round-trips as True / False / **None**. None means
    "inherit the global gate", which is what every pre-v0.9.0 rule holds, so a
    plain admin save of an untouched rule must not silently turn creation off.
    Collapsing the tri-state to a bool is the specific regression guarded here,
    because an edit CLONES the rule from the form values (see save_rule) — a
    field mishandled on read is a field rewritten on every save.
  * The autocomplete queries ERPNext LIVE and is never cached, and it emits the
    DOCNAME alongside the label because those differ for the series-named
    doctypes.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import categorization, create_app, db, erpnext_settings  # noqa: E402
from app.models import CategorizationRule  # noqa: E402
from tests.fakes import FakeERPClient  # noqa: E402

COMPANY = 'Orchard Example, LLC'
PAYABLE = '2110 - Creditors - OML'

CHART = [
    {'name': PAYABLE, 'account_name': 'Creditors', 'company': COMPANY,
     'root_type': 'Liability', 'account_type': 'Payable'},
]


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
        self.http = self.app.test_client()
        self.erp = FakeERPClient(
            chart_accounts=CHART, companies=[COMPANY], company_abbr='OML',
            existing_suppliers=['Sorren', 'Sorren Advisors', 'Zoho'],
            existing_parties={'Employee': [
                {'name': 'HR-EMP-00001', 'employee_name': 'Mitchell Huru'}]})

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        os.close(self._dbfd)
        os.unlink(self._dbpath)

    def _save(self, **overrides):
        data = {
            'name': 'Sorren (accounting)',
            'match_type': 'merchant_contains',
            'match_value': 'Sorren',
            'offset_account': PAYABLE,
            'offset_direction': 'auto',
            'party_type': 'Supplier',
            'party_name': 'Sorren',
            'priority': '100',
            'active': '1',
            'ignore_for_paired': '1',
        }
        data.update(overrides)
        with mock.patch('app.erpnext_bank.get_client', return_value=self.erp):
            resp = self.http.post('/admin/rules/save', data=data)
        return resp

    def _rule(self):
        return (CategorizationRule.query
                .filter(CategorizationRule.archived.is_(False))
                .order_by(CategorizationRule.id.desc()).first())


class PartyTypeChoices(Base):
    def test_the_editor_offers_all_four_erpnext_party_types(self):
        with mock.patch('app.erpnext_bank.get_client', return_value=self.erp):
            body = self.http.get('/admin/rules').get_data(as_text=True)
        for option in ('Supplier', 'Customer', 'Employee', 'Shareholder'):
            self.assertIn(f'value="{option}"', body)

    def test_employee_and_shareholder_are_saved(self):
        for party_type in ('Employee', 'Shareholder'):
            self._save(party_type=party_type, party_name='Someone')
            self.assertEqual(self._rule().party_type, party_type)

    def test_the_page_states_that_an_employee_is_never_created(self):
        """The one thing an operator must not have to discover by watching a JE
        come out partyless."""
        with mock.patch('app.erpnext_bank.get_client', return_value=self.erp):
            body = self.http.get('/admin/rules').get_data(as_text=True)
        self.assertIn('Employee</b> never is', body)
        self.assertIn('date of birth', body)


class AutoCreatePartyIsTriState(Base):
    def test_blank_means_inherit_not_off(self):
        """THE REGRESSION THIS GUARDS. An edit clones the rule from the form
        values, so if '' read as False every admin save of an untouched rule
        would silently disable party creation on it."""
        self._save(auto_create_party='')
        self.assertIsNone(self._rule().auto_create_party)

    def test_a_missing_field_also_means_inherit(self):
        """A hand-rolled POST (or the MCP create_rule tool) that omits the field
        must land on "inherit", which is what every pre-v0.9.0 rule holds."""
        self._save()
        self.assertIsNone(self._rule().auto_create_party)

    def test_yes_and_no_round_trip(self):
        self._save(auto_create_party='1')
        self.assertIs(self._rule().auto_create_party, True)
        self._save(auto_create_party='0')
        self.assertIs(self._rule().auto_create_party, False)

    def test_to_dict_keeps_none_distinguishable_from_false(self):
        self._save(auto_create_party='')
        self.assertIsNone(self._rule().to_dict()['auto_create_party'])
        self._save(auto_create_party='0')
        self.assertIs(self._rule().to_dict()['auto_create_party'], False)

    def test_inherit_follows_the_global_gate(self):
        rule = CategorizationRule(auto_create_party=None)
        for gate in (True, False):
            self.app.config['ERPNEXT_AUTO_CREATE_SUPPLIERS'] = gate
            self.assertIs(
                categorization._auto_create_party_enabled(rule), gate)

    def test_a_per_rule_override_beats_the_global_gate(self):
        self.app.config['ERPNEXT_AUTO_CREATE_SUPPLIERS'] = False
        self.assertIs(categorization._auto_create_party_enabled(
            CategorizationRule(auto_create_party=True)), True)
        self.app.config['ERPNEXT_AUTO_CREATE_SUPPLIERS'] = True
        self.assertIs(categorization._auto_create_party_enabled(
            CategorizationRule(auto_create_party=False)), False)


class PartyFieldUsesTheSharedDropdown(Base):
    """v0.3.4 moved the offset-account field OFF a native <datalist> because
    Safari collapsed the list mid-type (non-matching chars, deletes, arrow
    keys). The party field is typed into just as much, so it must not
    re-introduce one — this test is the guard, because the first cut of v0.9.0
    did exactly that and only test_rule_dropdown caught it."""

    def test_the_rules_page_declares_no_native_datalist(self):
        # Checks for the real ELEMENT, not the bare word — an HTML comment on
        # the offset-account field still names `<datalist>` on purpose, to say
        # what it replaced. Same precise check test_rule_dropdown makes.
        with mock.patch('app.erpnext_bank.get_client', return_value=self.erp):
            body = self.http.get('/admin/rules').get_data(as_text=True)
        self.assertNotIn('<datalist id=', body)
        self.assertNotIn('list="party-name-options"', body)

    def test_the_party_field_is_wired_to_a_dropdown_menu(self):
        with mock.patch('app.erpnext_bank.get_client', return_value=self.erp):
            body = self.http.get('/admin/rules').get_data(as_text=True)
        self.assertIn('id="party-name"', body)
        self.assertIn('id="pn-dd"', body)
        self.assertIn('BankBridgeDropdown.createDropdown', body)


class PartyAutocomplete(Base):
    def _search(self, **params):
        qs = '&'.join(f'{k}={v}' for k, v in params.items())
        with mock.patch('app.erpnext_bank.get_client', return_value=self.erp):
            return self.http.get(f'/api/rules/party_search?{qs}').get_json()

    def test_it_matches_a_substring_of_the_party_name(self):
        data = self._search(party_type='Supplier', q='orren')
        labels = [p['label'] for p in data['parties']]
        self.assertIn('Sorren', labels)
        self.assertIn('Sorren Advisors', labels)
        self.assertNotIn('Zoho', labels)

    def test_it_emits_the_docname_alongside_the_label(self):
        """They differ for the series-named doctypes: a JE line needs
        'HR-EMP-00001' and an operator recognises 'Mitchell Huru'."""
        data = self._search(party_type='Employee', q='Mitchell')
        self.assertEqual(data['parties'],
                         [{'name': 'HR-EMP-00001', 'label': 'Mitchell Huru'}])

    def test_it_queries_erpnext_on_every_call_and_caches_nothing(self):
        """DATA DRIVEN · a Supplier created in ERPNext a minute ago must be
        selectable now. A cached party list is wrong the moment anyone edits it
        there."""
        self._search(party_type='Supplier', q='orren')
        before = len([c for c in self.erp.calls if c[1] == 'Supplier'])
        self.assertGreaterEqual(before, 1)

        self.erp.existing_suppliers.add('Sorrentino Farms')
        data = self._search(party_type='Supplier', q='orren')

        self.assertIn('Sorrentino Farms',
                      [p['label'] for p in data['parties']])
        self.assertGreater(
            len([c for c in self.erp.calls if c[1] == 'Supplier']), before)

    def test_auto_suggests_nothing_rather_than_erroring(self):
        """'Auto' resolves per transaction, so there is no single doctype to
        search — not an error, just nothing to offer."""
        data = self._search(party_type='Auto', q='x')
        self.assertEqual(data['parties'], [])

    def test_it_says_whether_the_type_can_be_created(self):
        self.assertTrue(self._search(party_type='Supplier')['creatable'])
        self.assertTrue(self._search(party_type='Shareholder')['creatable'])
        self.assertFalse(self._search(party_type='Employee')['creatable'])

    def test_an_unreachable_erpnext_offers_nothing_and_does_not_500(self):
        """The field stays free text, so an operator can always type a name."""
        with mock.patch('app.erpnext_bank.get_client',
                        side_effect=RuntimeError('down')):
            resp = self.http.get(
                '/api/rules/party_search?party_type=Supplier&q=x')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['parties'], [])


if __name__ == '__main__':
    unittest.main()
