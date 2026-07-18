# SPDX-License-Identifier: MIT
"""Offset Account dropdown: full-chart coverage + cache freshness (v0.4.0.6).

Two guarantees, both regressions of a real report — an "Interest Income - BBT"
account created in ERPNext never showed up in the Rules-editor picker:

  1. COVERAGE · list_accounts must offer EVERY posting leaf in the Company, across
     all five root types (Asset, Liability, Equity, Income, Expense). The only
     filters allowed are company / is_group=0 / disabled=0 — a rule may legitimately
     offset to Interest Income, Meals, Owner's Draw or a Loan Payable.
  2. FRESHNESS · the feed is cached per Company for the session, and the v0.4.0.2
     invalidate-on-scope-change hook did NOT cover "operator creates the account in
     ERPNext while staying on the same Company". Opening the Rules editor now
     re-fetches, and an explicit refresh endpoint covers the another-tab case.

    cd app
    python3 -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import erpnext_bank, erpnext_settings  # noqa: E402
from app.blueprints import admin_ui  # noqa: E402

BBT = 'Bank Bridge Test'

# A chart that spans all five root types, plus a group parent and a disabled leaf
# that must NEVER be offered. Shape mirrors a real ERPNext Account row.
FULL_CHART = [
    ('Checking - BBT', 'Checking', 'Asset', 'Bank', 0, 0),
    ('Loan Payable - BBT', 'Loan Payable', 'Liability', '', 0, 0),
    ("Owner's Draw - BBT", "Owner's Draw", 'Equity', '', 0, 0),
    # The reported case: an Income leaf under Income → Indirect Income.
    ('Interest Income - BBT', 'Interest Income', 'Income', 'Income Account', 0, 0),
    ('Fuel Expense - BBT', 'Fuel Expense', 'Expense', 'Expense Account', 0, 0),
    # Must be filtered OUT by list_accounts:
    ('Indirect Income - BBT', 'Indirect Income', 'Income', '', 1, 0),   # group
    ('Old Card - BBT', 'Old Card', 'Liability', '', 0, 1),              # disabled
]


class RecordingClient:
    """Minimal ERPNextClient stand-in that records the exact list_docs filters and
    applies them the way Frappe would, so a stray root_type/account_type filter
    shows up as a missing account rather than passing silently."""

    def __init__(self, rows=FULL_CHART):
        self.rows = list(rows)
        self.calls = []

    def list_docs(self, doctype, *, filters=None, fields=None,
                  limit_page_length=0, order_by=None):
        self.calls.append({'doctype': doctype, 'filters': filters,
                           'limit_page_length': limit_page_length})
        out = []
        for name, acct, root, atype, is_group, disabled in self.rows:
            doc = {'name': name, 'account_name': acct, 'company': BBT,
                   'account_type': atype, 'root_type': root,
                   'is_group': is_group, 'disabled': disabled}
            if all(self._match(doc, f) for f in (filters or [])):
                out.append({k: doc[k] for k in (fields or doc)})
        return out

    @staticmethod
    def _match(doc, flt):
        field, op, val = flt
        assert op == '=', f'unexpected operator {op!r}'
        return doc.get(field) == val

    def filter_fields(self, call_index=-1):
        return {f[0] for f in (self.calls[call_index]['filters'] or [])}


class FreshnessBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', 'Default Co')

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _feed(self, company=BBT, fresh=False, path='/api/rules/known_accounts'):
        url = f'{path}?company={company.replace(" ", "+")}'
        if fresh:
            url += '&fresh=1'
        return json.loads(self.client.get(url).get_data(as_text=True))


# ── 1. coverage: every root type, no group/disabled leaks ───────────────────

class TestListAccountsCoverage(unittest.TestCase):
    def _rows(self, **kw):
        rec = RecordingClient()
        return rec, erpnext_bank.list_accounts(rec, company=BBT, **kw)

    def test_returns_every_root_type(self):
        # Asset, Liability, Equity, Income AND Expense — a rule may offset to any.
        _, rows = self._rows()
        self.assertEqual(
            {r['root_type'] for r in rows},
            {'Asset', 'Liability', 'Equity', 'Income', 'Expense'})

    def test_includes_the_reported_income_account(self):
        # The exact account Tim created and could not select.
        _, rows = self._rows()
        self.assertIn('Interest Income - BBT', [r['name'] for r in rows])

    def test_excludes_group_and_disabled_accounts(self):
        _, rows = self._rows()
        names = [r['name'] for r in rows]
        self.assertNotIn('Indirect Income - BBT', names)   # is_group=1
        self.assertNotIn('Old Card - BBT', names)          # disabled=1

    def test_filters_are_company_is_group_disabled_only(self):
        # Guards the fix directly: any root_type / account_type filter creeping
        # back in would silently amputate the picker.
        rec, _ = self._rows()
        self.assertEqual(rec.filter_fields(), {'company', 'is_group', 'disabled'})
        # …and no 20-row default cap hiding the tail of a large chart.
        self.assertEqual(rec.calls[-1]['limit_page_length'], 0)


# ── 2. freshness: the stale-cache regression ────────────────────────────────

class TestFeedFreshness(FreshnessBase):
    def test_cache_serves_repeat_requests(self):
        # Baseline: the per-Company cache still exists (one ERPNext hit for two
        # ordinary requests) — freshness must not become "no caching at all".
        rec = RecordingClient()
        with mock.patch('app.erpnext_bank.get_client', return_value=rec):
            self._feed()
            self._feed()
        self.assertEqual(len(rec.calls), 1)

    def test_fresh_param_bypasses_and_refills_the_cache(self):
        rec = RecordingClient()
        with mock.patch('app.erpnext_bank.get_client', return_value=rec):
            self._feed()                     # warms
            data = self._feed(fresh=True)    # must re-hit ERPNext
        self.assertEqual(len(rec.calls), 2)
        self.assertIn('Interest Income - BBT', data['accounts'])

    def test_new_account_appears_after_rules_editor_reload(self):
        # THE regression. Warm the cache from a chart WITHOUT the new account,
        # then "create" it in ERPNext while staying on the same Company — no
        # scope toggle, no restart — and reopen the Rules editor.
        without = [r for r in FULL_CHART if not r[0].startswith('Interest')]
        rec = RecordingClient(rows=without)
        with mock.patch('app.erpnext_bank.get_client', return_value=rec):
            stale = self._feed()
            self.assertNotIn('Interest Income - BBT', stale['accounts'])
            rec.rows.append(
                ('Interest Income - BBT', 'Interest Income', 'Income',
                 'Income Account', 0, 0))
            # Pre-fix this still served the stale list; now the editor load drops
            # the cache and the page's ?fresh=1 fetch re-reads the chart.
            self.client.get('/admin/rules')
            data = self._feed(fresh=True)
        self.assertIn('Interest Income - BBT', data['accounts'])

    def test_rules_editor_load_invalidates_the_cache(self):
        rec = RecordingClient()
        with mock.patch('app.erpnext_bank.get_client', return_value=rec):
            self._feed()
            self.assertTrue(admin_ui._accounts_cache())     # warm
            self.client.get('/admin/rules')
        self.assertEqual(admin_ui._accounts_cache(), {})    # dropped on load

    def test_refresh_endpoint_invalidates_every_key_and_returns_fresh(self):
        rec = RecordingClient()
        with mock.patch('app.erpnext_bank.get_client', return_value=rec):
            self._feed()                                    # scoped key
            self._feed(company='')                          # logical key
            self.assertEqual(len(admin_ui._accounts_cache()), 2)
            data = self._feed(path='/api/rules/refresh_accounts')
        # Only the just-refetched key survives; the logical feed was dropped too,
        # since the new account belongs in it as well.
        self.assertEqual(list(admin_ui._accounts_cache()), [BBT])
        self.assertEqual(data['mode'], 'specific')
        self.assertIn('Interest Income - BBT', data['accounts'])

    def test_refresh_endpoint_serves_logical_feed_for_agnostic_rules(self):
        rec = RecordingClient()
        with mock.patch('app.erpnext_bank.get_client', return_value=rec):
            data = self._feed(company='', path='/api/rules/refresh_accounts')
        self.assertEqual(data['mode'], 'logical')
        # v0.4.0.3 shape preserved: logical names, no Company suffix.
        self.assertIn('Interest Income', data['accounts'])
        self.assertNotIn('Interest Income - BBT', data['accounts'])


# ── 3. dropdown UX in both modes ────────────────────────────────────────────

class TestDropdownModes(FreshnessBase):
    def test_scoped_rule_offers_all_root_types_under_the_company(self):
        rec = RecordingClient()
        with mock.patch('app.erpnext_bank.get_client', return_value=rec):
            data = self._feed(fresh=True)
        self.assertEqual(data['mode'], 'specific')
        for name in ('Checking - BBT', 'Loan Payable - BBT', "Owner's Draw - BBT",
                     'Interest Income - BBT', 'Fuel Expense - BBT'):
            self.assertIn(name, data['accounts'])

    def test_rules_editor_page_offers_a_manual_refresh_affordance(self):
        with mock.patch('app.erpnext_bank.get_client',
                        return_value=RecordingClient()):
            html = self.client.get('/admin/rules').get_data(as_text=True)
        self.assertIn('id="oa-refresh"', html)
        self.assertIn('/api/rules/refresh_accounts', html)
        # The editor's own account fetch must ask for a fresh read.
        self.assertIn("'&fresh=1'", html)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
