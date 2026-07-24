# SPDX-License-Identifier: MIT
"""Consolidated asset-type Marketable Securities accounts (v0.5.11).

v0.5.10 created one leaf per ticker (103 of them) stranded at the chart root.
v0.5.11 buckets holdings by security_type into six leaves (1321-1326) under the
EXISTING 1320 - Marketable Securities group (under 1800 - Investments), and adds
a one-shot rebuild that deletes the draft JEs + orphan per-ticker leaves.

Covered:
  * security_type → 132X asset-type leaf mapping (every value + default)
  * ensure_leaf honours an explicit parent_account (never falls back to root)
  * ensure_group creates an is_group=1 account, not a leaf
  * rebuild deletes draft JEs + empty orphan leaves; NEVER touches the 1320 group
  * rebuild skips a non-empty account and ABORTS on a submitted JE

Synthetic tickers (TESTAA) and company (Testco) only.
"""
import unittest

from app import db, invest_je
from app.erpnext_accounts import ACCOUNT_DT
from app.invest_je import JOURNAL_ENTRY_DT, GL_ENTRY_DT
from app.models import GeneratedJournalEntry, PlaidAccount

from tests.test_statements import StatementsBase

ABBR = 'TCO'
COMPANY = 'Testco'


class FakeChart:
    """A minimal ERPNext for the Account / Journal Entry / GL Entry doctypes."""
    def __init__(self):
        root = self._acct('Application of Funds', '1000', is_group=1,
                          parent='', root_type='Asset')
        inv = self._acct('Investments', '1800', is_group=1,
                         parent=root['name'], root_type='Asset')
        self.accounts = {a['name']: a for a in (root, inv)}
        self.jes = {}          # name -> {docstatus}
        self.gl = {}           # account docname -> [rows]
        self.deleted = []

    def _acct(self, account_name, number='', *, is_group=0, parent='',
              root_type='Asset'):
        name = f"{number + ' - ' if number else ''}{account_name} - {ABBR}"
        return {'name': name, 'account_name': account_name,
                'account_number': number, 'is_group': int(is_group),
                'parent_account': parent, 'root_type': root_type}

    # -- ERPNextClient surface --
    def list_docs(self, doctype, *, filters=None, fields=None,
                  limit_page_length=0, order_by=None):
        f = {c[0]: c[2] for c in (filters or [])}
        if doctype == GL_ENTRY_DT:
            return [dict(r) for r in self.gl.get(f.get('account'), [])]
        out = []
        for a in self.accounts.values():
            if 'account_name' in f and a['account_name'] != f['account_name']:
                continue
            if 'is_group' in f and a['is_group'] != int(f['is_group']):
                continue
            if 'root_type' in f and a['root_type'] != f['root_type']:
                continue
            out.append(dict(a))
        return out

    def create_doc(self, doctype, doc):
        assert doctype == ACCOUNT_DT
        num = doc.get('account_number') or ''
        rec = {'name': f"{num + ' - ' if num else ''}{doc['account_name']} - {ABBR}",
               'account_name': doc['account_name'], 'account_number': num,
               'is_group': int(doc.get('is_group', 0)),
               'parent_account': doc.get('parent_account', ''),
               'root_type': doc.get('root_type', 'Asset')}
        self.accounts[rec['name']] = rec
        return rec

    def get_doc(self, doctype, name):
        if doctype == JOURNAL_ENTRY_DT:
            return self.jes.get(name)
        return self.accounts.get(name)

    def delete_doc(self, doctype, name):
        self.deleted.append((doctype, name))
        if doctype == JOURNAL_ENTRY_DT:
            self.jes.pop(name, None)
        else:
            self.accounts.pop(name, None)
        return True


class MappingTest(unittest.TestCase):
    def _leaf(self, security_type):
        c = FakeChart()
        name = invest_je.marketable_securities_account(c, COMPANY, 'TESTAA',
                                                       security_type)
        acct = c.accounts[name]
        return acct, c

    def test_each_security_type_maps_to_its_asset_leaf(self):
        cases = {
            'equity': ('1321', 'Stocks'),
            'etf': ('1322', 'ETFs'),
            'mutual fund': ('1323', 'Mutual Funds'),     # Plaid spelling (space)
            'mutual_fund': ('1323', 'Mutual Funds'),
            'fixed income': ('1324', 'Fixed Income'),
            'preferred': ('1325', 'Preferreds'),
            'derivative': ('1326', 'Options'),
            'option': ('1326', 'Options'),
        }
        for stype, (number, leaf) in cases.items():
            acct, _ = self._leaf(stype)
            self.assertEqual(acct['account_name'], leaf, stype)
            self.assertEqual(acct['account_number'], number, stype)

    def test_unknown_type_defaults_to_stocks(self):
        for stype in (None, '', 'cash', 'weird'):
            acct, _ = self._leaf(stype)
            self.assertEqual(acct['account_name'], 'Stocks')

    def test_leaf_is_nested_under_1320_under_1800(self):
        acct, c = self._leaf('equity')
        parent = c.accounts[acct['parent_account']]
        self.assertEqual(parent['account_name'], 'Marketable Securities')
        self.assertEqual(parent['account_number'], '1320')
        self.assertEqual(parent['is_group'], 1)
        grandparent = c.accounts[parent['parent_account']]
        self.assertEqual(grandparent['account_name'], 'Investments')

    def test_reuse_is_idempotent(self):
        c = FakeChart()
        n1 = invest_je.marketable_securities_account(c, COMPANY, 'AA', 'equity')
        n2 = invest_je.marketable_securities_account(c, COMPANY, 'BB', 'equity')
        self.assertEqual(n1, n2)            # both equities share one Stocks leaf


class EnsureHelpersTest(unittest.TestCase):
    def test_ensure_leaf_uses_explicit_parent_not_root(self):
        c = FakeChart()
        parent = invest_je.ensure_group(c, COMPANY, 'Investments',
                                        'Application of Funds - TCO')  # exists
        name = invest_je.ensure_leaf(c, COMPANY, 'Stocks', 'Asset',
                                     account_number='1811', parent_account=parent)
        self.assertEqual(c.accounts[name]['parent_account'], parent)
        self.assertEqual(c.accounts[name]['is_group'], 0)

    def test_ensure_group_creates_a_group(self):
        c = FakeChart()
        inv = '1800 - Investments - TCO'
        name = invest_je.ensure_group(c, COMPANY, 'Marketable Securities', inv,
                                      account_number='1320')
        self.assertEqual(c.accounts[name]['is_group'], 1)
        self.assertEqual(c.accounts[name]['parent_account'], inv)


class RebuildTest(StatementsBase):
    def _draft_gje(self, itx, je_name, docstatus=0, state='pending_review'):
        self.chart.jes[je_name] = {'docstatus': docstatus}
        g = GeneratedJournalEntry(plaid_transaction_id=f'inv:{itx}',
                                  plaid_investment_transaction_id=itx,
                                  erpnext_journal_entry_name=je_name, state=state)
        db.session.add(g); db.session.commit()
        return g

    def setUp(self):
        super().setUp()
        self.chart = FakeChart()
        # The KEPT 1320 group lives under 1800 - Investments (holds the real
        # balance). The orphan per-ticker leaf + 'Other' are stranded at the
        # root of 1000 - Application of Funds — matching the live install.
        self.grp = self.chart._acct('Marketable Securities', '1320', is_group=1,
                                    parent='1800 - Investments - TCO')
        leaf = self.chart._acct('Marketable Securities - TESTAA',
                                parent='1000 - Application of Funds - TCO')
        other = self.chart._acct('Marketable Securities - Other', '1320.1',
                                 parent='1000 - Application of Funds - TCO')
        for a in (self.grp, leaf, other):
            self.chart.accounts[a['name']] = a
        db.session.add(PlaidAccount(account_id='inv1', item_id=self.item.item_id,
                                    name='B', mask='9401', type='investment',
                                    subtype='brokerage', owning_company=COMPANY))
        db.session.commit()

    def test_rebuild_deletes_drafts_and_orphan_accounts(self):
        self._draft_gje('t1', 'JE-0001')
        self._draft_gje('t2', 'JE-0002')
        res = invest_je.rebuild_investment_accounts(self.chart, company=COMPANY)
        self.assertFalse(res['aborted'])
        self.assertEqual(res['drafts_deleted'], 2)
        self.assertEqual(res['accounts_deleted'], 2)   # TESTAA leaf + Other
        self.assertEqual(res['groups_deleted'], 0)     # groups are NEVER reaped
        self.assertEqual(GeneratedJournalEntry.query.count(), 0)
        # The 1320 group (real balance) survives; only the orphan leaves go.
        self.assertIn('1320 - Marketable Securities - TCO', self.chart.accounts)
        self.assertNotIn('Marketable Securities - TESTAA - TCO',
                         self.chart.accounts)
        self.assertNotIn('1320.1 - Marketable Securities - Other - TCO',
                         self.chart.accounts)

    def test_rebuild_aborts_on_a_submitted_je(self):
        self._draft_gje('t1', 'JE-0001', docstatus=1)   # submitted!
        res = invest_je.rebuild_investment_accounts(self.chart, company=COMPANY)
        self.assertTrue(res['aborted'])
        self.assertEqual(res['drafts_deleted'], 0)
        self.assertEqual(GeneratedJournalEntry.query.count(), 1)  # untouched
        self.assertEqual(self.chart.deleted, [])         # nothing deleted

    def test_rebuild_skips_a_non_empty_account(self):
        self._draft_gje('t1', 'JE-0001')
        # give the TESTAA leaf a GL entry → it must be kept, not deleted
        self.chart.gl['Marketable Securities - TESTAA - TCO'] = [{'name': 'GL1'}]
        res = invest_je.rebuild_investment_accounts(self.chart, company=COMPANY)
        self.assertGreaterEqual(res['skipped_nonzero'], 1)
        self.assertIn('Marketable Securities - TESTAA - TCO', self.chart.accounts)

    def test_rebuild_is_idempotent(self):
        self._draft_gje('t1', 'JE-0001')
        invest_je.rebuild_investment_accounts(self.chart, company=COMPANY)
        res2 = invest_je.rebuild_investment_accounts(self.chart, company=COMPANY)
        self.assertEqual(res2['drafts_deleted'], 0)
        self.assertEqual(res2['accounts_deleted'], 0)
