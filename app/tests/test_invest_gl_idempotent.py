# SPDX-License-Identifier: MIT
"""Idempotent GL-account provisioning for investment posting (v0.5.10).

The Phase-D backfill hit `ValidationError: Account Number 1320.1 already used`:
`marketable_securities_account` asked for the SAME account_number 1320.1 for
every per-security leaf, so once one leaf owned it, every other ticker's create
failed and half the batch died.

Two fixes, both covered here:
  * a per-ticker leaf no longer carries the shared number (only the 'Other'
    fallback keeps 1320.1);
  * `ensure_leaf` is resilient anyway — an existing name is reused, a number
    collision retries once without the number, and a check-then-create race
    re-reads and reuses the winner.

Synthetic tickers (TESTAA / TESTBB) only.
"""
import unittest

from app import invest_je
from app.erpnext_client import ERPNextAPIError
from app.erpnext_accounts import ACCOUNT_DT


class FakeAcctClient:
    """A minimal ERPNext stand-in for the Account doctype: list_docs + create_doc
    with Frappe's per-company account_number uniqueness enforced."""
    def __init__(self, seed=None, race_names=()):
        # each account: {name, account_name, account_number, is_group}
        self.accounts = list(seed or [])
        self.create_calls = []          # docs passed to create_doc
        self.race_names = set(race_names)  # names whose FIRST create races

    def list_docs(self, doctype, *, filters=None, fields=None,
                  limit_page_length=0, order_by=None):
        assert doctype == ACCOUNT_DT
        f = {c[0]: c[2] for c in (filters or [])}
        out = []
        for a in self.accounts:
            if 'account_name' in f and a.get('account_name') != f['account_name']:
                continue
            if 'is_group' in f and int(a.get('is_group', 0)) != int(f['is_group']):
                continue
            out.append(dict(a))
        return out

    def create_doc(self, doctype, doc):
        assert doctype == ACCOUNT_DT
        self.create_calls.append(dict(doc))
        name = f"{doc.get('account_number','') + ' - ' if doc.get('account_number') else ''}{doc['account_name']} - OML"
        rec = {'name': name, 'account_name': doc['account_name'],
               'account_number': doc.get('account_number', ''),
               'is_group': doc.get('is_group', 0)}
        # A check-then-create race: another worker already made this name.
        if doc['account_name'] in self.race_names:
            self.race_names.discard(doc['account_name'])
            self.accounts.append(rec)   # the winner's row is now present
            raise ERPNextAPIError('Duplicate name', status_code=409)
        # Frappe enforces per-company account_number uniqueness.
        num = doc.get('account_number')
        if num and any(a.get('account_number') == num for a in self.accounts):
            raise ERPNextAPIError(f'Account Number {num} already used',
                                  status_code=417)
        self.accounts.append(rec)
        return rec


class EnsureLeafTest(unittest.TestCase):
    def setUp(self):
        self._orig = invest_je._root_group
        invest_je._root_group = lambda c, company, rt: 'Assets - OML'

    def tearDown(self):
        invest_je._root_group = self._orig

    def test_existing_name_is_reused_without_creating(self):
        c = FakeAcctClient(seed=[{
            'name': '1320.2 - Marketable Securities - TESTAA - OML',
            'account_name': 'Marketable Securities - TESTAA',
            'account_number': '1320.2', 'is_group': 0}])
        name = invest_je.ensure_leaf(c, 'OML',
                                     'Marketable Securities - TESTAA', 'Asset')
        self.assertEqual(name, '1320.2 - Marketable Securities - TESTAA - OML')
        self.assertEqual(c.create_calls, [])          # no create attempted

    def test_number_collision_retries_without_the_number(self):
        # 'Other' already owns 1320.1; a new leaf that asks for 1320.1 must NOT
        # die — it retries numberless.
        c = FakeAcctClient(seed=[{
            'name': '1320.1 - Marketable Securities - Other - OML',
            'account_name': 'Marketable Securities - Other',
            'account_number': '1320.1', 'is_group': 0}])
        name = invest_je.ensure_leaf(c, 'OML', 'Marketable Securities - TESTBB',
                                     'Asset', account_number='1320.1')
        self.assertIn('Marketable Securities - TESTBB', name)
        # two create attempts: with number (failed), then without.
        self.assertEqual(len(c.create_calls), 2)
        self.assertNotIn('account_number', c.create_calls[1])

    def test_create_race_reuses_the_winner(self):
        c = FakeAcctClient(race_names={'Marketable Securities - TESTAA'})
        name = invest_je.ensure_leaf(c, 'OML',
                                     'Marketable Securities - TESTAA', 'Asset')
        self.assertIn('Marketable Securities - TESTAA', name)
        # one failed create; recovered by re-reading, not a second create.
        self.assertEqual(len(c.create_calls), 1)


class MarketableSecuritiesNumberingTest(unittest.TestCase):
    def setUp(self):
        self._orig = invest_je._root_group
        invest_je._root_group = lambda c, company, rt: 'Assets - OML'

    def tearDown(self):
        invest_je._root_group = self._orig

    def test_tickered_leaf_carries_no_shared_number(self):
        c = FakeAcctClient()
        invest_je.marketable_securities_account(c, 'OML', 'TESTAA')
        invest_je.marketable_securities_account(c, 'OML', 'TESTBB')
        # neither per-ticker create claims an account_number → no collision.
        for call in c.create_calls:
            self.assertNotIn('account_number', call)
        self.assertEqual(len(c.create_calls), 2)

    def test_no_ticker_fallback_keeps_1320_1(self):
        c = FakeAcctClient()
        invest_je.marketable_securities_account(c, 'OML', None)
        self.assertEqual(c.create_calls[0].get('account_number'), '1320.1')
        self.assertEqual(c.create_calls[0]['account_name'],
                         'Marketable Securities - Other')
