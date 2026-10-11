# SPDX-License-Identifier: MIT
"""Company-scoped categorization rules (v1.0.9).

THE RISK. OML's rules were written for Orchard Meadow, LLC, and 65 of 67 had no
Company — "applies everywhere". Linking Constancy Farms, LLC's bank account
would have sent its cash withdrawals to OML's Member Distributions (rule 182,
regex "CASH"), a payment to Tim to OML's management fees (171/172), and its
Oregon DOR payroll tax to a licence fee (168).

Covered:
  * a Constancy transaction matching "CASH" / "TIM POLEHN" hits NO OML rule —
    whether Constancy's Item names its Company or nobody assigned one
  * the same transaction on OML's account still matches
  * an unscoped rule matches nothing
  * generate_journal_entry refuses a rule handed in across Companies
  * the boot migration: dry-run report → apply → nothing left unscoped, with
    the target taken from the rules already scoped
  * the one-time Item pin, and that a LATER unassigned Item is never pinned
  * rules_source: a stored 'erpnext' with no mirrored rules reads 'local'

Synthetic ids; the Company names are the real ones the brief is about.

    cd app
    python3 -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest
import unittest.mock
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import (categorization, create_app, crypto, db,  # noqa: E402
                 erpnext_settings, mcp_settings, rule_scope)
from app.blueprints import mcp_server  # noqa: E402
from app.models import (AuditEvent, BankTransaction,  # noqa: E402
                        CategorizationRule, GeneratedJournalEntry,
                        PlaidAccount, PlaidItem)

from tests.fakes import FakeERPClient  # noqa: E402

OML = 'Orchard Meadow, LLC'
CFL = 'Constancy Farms, LLC'


class ScopeBase(unittest.TestCase):
    EXTRA_CONFIG = {'ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES': True}

    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self.app = create_app({
            'TESTING': True, 'SCHEDULER_ENABLED': False, 'FERNET_KEY': '',
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': tempfile.mkdtemp(), **self.EXTRA_CONFIG})
        self.ctx = self.app.app_context()
        self.ctx.push()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', OML)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def item(self, item_id, company=None):
        it = PlaidItem(item_id=item_id, status='active',
                       access_token_encrypted=crypto.encrypt('x'),
                       institution_name='Wells Fargo', owning_company=company)
        db.session.add(it)
        db.session.commit()
        return it

    def account(self, account_id, item_id, mask):
        a = PlaidAccount(account_id=account_id, item_id=item_id, mask=mask,
                         name=f'Checking {mask}', type='depository',
                         subtype='checking', sync_enabled=True,
                         erpnext_bank_account_name=f'Checking {mask}')
        db.session.add(a)
        db.session.commit()
        return a

    def rule(self, rid_name, regex, offset, company):
        r = CategorizationRule(name=rid_name, priority=100, active=True,
                               match_type='description_regex',
                               match_value=regex, offset_account=offset,
                               applies_to_company=company)
        db.session.add(r)
        db.session.commit()
        return r

    def txn(self, tid, account_id, name, amount=100.0):
        t = BankTransaction(plaid_transaction_id=tid, account_id=account_id,
                            amount=amount, name=name, date=date(2026, 10, 9),
                            erpnext_bank_transaction_id=f'BT-{tid}')
        db.session.add(t)
        db.session.commit()
        return t

    def oml_rules(self, company=OML):
        self.cash = self.rule('Cash withdrawal — family petty cash', 'CASH',
                              'Member Distributions', company)
        self.tim = self.rule('Tim Polehn short — Manager Fee/draw',
                             'TIM POLEHN', 'Advisory & Management Fees',
                             company)


class ConstancyIsolationTests(ScopeBase):
    def setUp(self):
        super().setUp()
        self.item('item-oml', OML)
        self.account('acct-3158', 'item-oml', '3158')
        self.oml_rules()

    def test_a_constancy_transaction_hits_no_oml_rule(self):
        self.item('item-cfl', CFL)
        self.account('acct-cfl', 'item-cfl', '7001')
        for tid, name in (('c1', 'ATM CASH WITHDRAWAL 10/08'),
                          ('c2', 'ZELLE PAYMENT TO TIM POLEHN')):
            with self.subTest(name=name):
                winner, trace = categorization.evaluate_rules(
                    self.txn(tid, 'acct-cfl', name))
                self.assertIsNone(winner)
                self.assertFalse(any(t['matched'] for t in trace))

    def test_an_unassigned_constancy_item_does_not_borrow_oml_rules(self):
        """The case the brief worries about most: Constancy linked WITHOUT
        picking its Company. It must not fall back to the default (OML)."""
        self.item('item-cfl', None)
        self.account('acct-cfl', 'item-cfl', '7001')
        self.assertEqual('', rule_scope.matching_company('acct-cfl'))
        winner, _ = categorization.evaluate_rules(
            self.txn('c3', 'acct-cfl', 'ATM CASH WITHDRAWAL'))
        self.assertIsNone(winner)

    def test_the_same_text_on_oml_still_matches(self):
        winner, _ = categorization.evaluate_rules(
            self.txn('o1', 'acct-3158', 'ATM CASH WITHDRAWAL'))
        self.assertEqual(self.cash.id, winner.id)

    def test_a_constancy_rule_fires_for_constancy_only(self):
        self.item('item-cfl', CFL)
        self.account('acct-cfl', 'item-cfl', '7001')
        payroll = self.rule('Oregon DOR — payroll tax', 'OR DEPT OF REV',
                            'Payroll Taxes', CFL)
        self.rule('Oregon SoS annual report fee', 'OR DEPT OF REV',
                  'Licenses & Permits', OML)
        winner, _ = categorization.evaluate_rules(
            self.txn('c4', 'acct-cfl', 'OR DEPT OF REV PAYROLL'))
        self.assertEqual(payroll.id, winner.id)

    def test_an_unscoped_rule_matches_nothing(self):
        self.cash.applies_to_company = None
        db.session.commit()
        winner, _ = categorization.evaluate_rules(
            self.txn('o2', 'acct-3158', 'ATM CASH WITHDRAWAL'))
        self.assertNotEqual(self.cash.id, getattr(winner, 'id', None))

    def test_generation_refuses_a_rule_handed_in_across_companies(self):
        self.item('item-cfl', CFL)
        self.account('acct-cfl', 'item-cfl', '7001')
        erp = FakeERPClient()
        row = self.txn('c5', 'acct-cfl', 'ATM CASH WITHDRAWAL')
        self.assertIsNone(categorization.generate_journal_entry(
            erp, row, rule=self.cash))
        self.assertEqual([], erp.creates_of('Journal Entry'))
        self.assertEqual(0, GeneratedJournalEntry.query.count())
        self.assertEqual(1, AuditEvent.query.filter_by(
            event_type='rule_refused_cross_company').count())


class MigrationTests(ScopeBase):
    def test_dry_run_lists_every_unscoped_rule_and_its_new_scope(self):
        self.item('item-oml', None)
        self.rule('Bob\'s', 'BOBS', 'Member Distributions', OML)   # like 110/184
        self.oml_rules(company=None)
        report = rule_scope.apply_scope(dry_run=True)
        self.assertEqual(2, report['unscoped_count'])
        self.assertEqual(OML, report['target_company'])
        self.assertIn('already uses', report['target_reason'])
        self.assertEqual({(self.cash.id, OML), (self.tim.id, OML)},
                         {(r['id'], r['new_scope']) for r in report['rules']})
        self.assertEqual([('item-oml', OML)],
                         [(i['item_id'], i['new_company'])
                          for i in report['unassigned_items']])
        self.assertIsNone(db.session.get(CategorizationRule,
                                         self.cash.id).applies_to_company)

    def test_apply_scopes_everything_and_pins_items_once(self):
        self.item('item-oml', None)
        self.account('acct-3158', 'item-oml', '3158')
        self.rule('Bob\'s', 'BOBS', 'Member Distributions', OML)
        self.oml_rules(company=None)
        out = rule_scope.apply_scope(dry_run=False)
        self.assertEqual(2, out['applied'])
        self.assertEqual(1, out['items_pinned'])
        self.assertEqual(0, rule_scope.scope_report()['unscoped_count'])
        self.assertEqual(OML, db.session.get(PlaidItem, 1).owning_company)
        winner, _ = categorization.evaluate_rules(
            self.txn('o3', 'acct-3158', 'ATM CASH WITHDRAWAL'))
        self.assertEqual(self.cash.id, winner.id)
        # Constancy arrives LATER, unassigned: never pinned, matches nothing.
        self.item('item-cfl', None)
        self.account('acct-cfl', 'item-cfl', '7001')
        again = rule_scope.apply_scope(dry_run=False)
        self.assertEqual(0, again['items_pinned'])
        self.assertIsNone(PlaidItem.query.filter_by(
            item_id='item-cfl').one().owning_company)
        winner, _ = categorization.evaluate_rules(
            self.txn('c6', 'acct-cfl', 'ATM CASH WITHDRAWAL'))
        self.assertIsNone(winner)

    def test_an_ambiguous_target_assigns_nothing(self):
        self.rule('a', 'A', 'X', OML)
        self.rule('b', 'B', 'Y', CFL)
        self.oml_rules(company=None)
        out = rule_scope.apply_scope(dry_run=False)
        self.assertIsNone(out['target_company'])
        self.assertEqual(0, out['applied'])
        self.assertIn('RULE_SCOPE_MIGRATION_COMPANY', out['target_reason'])

    def test_the_env_override_wins(self):
        self.rule('a', 'A', 'X', OML)
        self.rule('b', 'B', 'Y', CFL)
        self.oml_rules(company=None)
        self.app.config['RULE_SCOPE_MIGRATION_COMPANY'] = OML
        out = rule_scope.apply_scope(dry_run=False)
        self.assertEqual(2, out['applied'])

    def test_the_boot_migration_runs_it(self):
        from app import migrations
        self.item('item-oml', None)
        self.oml_rules(company=None)
        self.rule('Bob\'s', 'BOBS', 'Member Distributions', OML)
        migrations.run_migrations()
        self.assertEqual(0, rule_scope.scope_report()['unscoped_count'])


class SurfaceTests(ScopeBase):
    def tool(self, name, args):
        with self.app.test_request_context(), unittest.mock.patch.object(
                mcp_server, '_erp_client_or_error',
                return_value=FakeERPClient(companies=[OML, CFL])):
            res = mcp_server.dispatch_tool(name, args)
        text = res['content'][0]['text']
        return res, (text if res.get('isError') else json.loads(text))

    def test_the_scope_report_tool_reads_zero_once_scoped(self):
        self.oml_rules()
        res, out = self.tool('get_rule_scope_report', {})
        self.assertFalse(res.get('isError'))
        self.assertEqual(0, out['unscoped_count'])

    def test_create_rule_requires_a_known_company(self):
        mcp_settings.save({'create_rule': True})
        res, text = self.tool('create_rule', {'match_type': 'merchant_exact',
                                              'match_value': 'Chevron'})
        self.assertTrue(res['isError'])
        self.assertIn('applies_to_company is required', text)
        res, text = self.tool('create_rule', {'match_type': 'merchant_exact',
                                              'match_value': 'Chevron',
                                              'applies_to_company': 'Nope LLC'})
        self.assertIn('unknown Company', text)
        res, out = self.tool('create_rule', {'match_type': 'merchant_exact',
                                             'match_value': 'Chevron',
                                             'applies_to_company': CFL})
        self.assertFalse(res.get('isError'), out)
        self.assertEqual(CFL, out['created_rule']['applies_to_company'])

    def test_list_rules_filters_by_company(self):
        self.oml_rules()
        self.rule('Oregon DOR — payroll tax', 'OR DEPT OF REV',
                  'Payroll Taxes', CFL)
        _, out = self.tool('list_rules', {'company': CFL})
        self.assertEqual(['Oregon DOR — payroll tax'],
                         [r['name'] for r in out['rules']])
        self.assertNotIn('deprecated', out)

    def test_a_stored_erpnext_source_with_no_mirror_reads_local(self):
        path = os.path.join(self.app.config['DATA_DIR'], 'erpnext_settings.json')
        with open(path) as fh:
            data = json.load(fh)
        data['rules_source'] = 'erpnext'
        data.pop('rules_source_migrated', None)
        with open(path, 'w') as fh:
            json.dump(data, fh)
        self.assertEqual('local', erpnext_settings.rules_source())
        self.assertEqual('local', categorization.rule_source_in_force())
        erpnext_settings.set_source('rules_source', 'erpnext')
        self.assertEqual('erpnext', erpnext_settings.rules_source(),
                         'an explicit opt-in must stick')


if __name__ == '__main__':
    unittest.main()
