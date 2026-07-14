# SPDX-License-Identifier: MIT
"""v0.3.3 · custom rule-builder dropdown.

Two layers:
  * The pure filter core (`filterOptions`) is exercised by requiring the real
    static JS module under Node — no reimplementation, so the test tracks the
    shipped behavior. Skips cleanly if `node` isn't on PATH.
  * Server-side: the static module is served and the rules page wires it in.

    cd erpnext-bank-bridge_v0.1.0
    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402

MODULE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'app', 'static', 'rule_dropdown.js'))
NODE = shutil.which('node')

# Requires the real module and prints the filtered labels as JSON. argv[1] is
# the module path, argv[2] the {options, query} payload; filtering is by .name.
_HARNESS = (
    "const m = require(process.argv[1]);"
    "const p = JSON.parse(process.argv[2]);"
    "const out = m.filterOptions(p.options, p.query, function (o) { return o && o.name; });"
    "process.stdout.write(JSON.stringify(out.map(function (o) { return o && o.name; })));"
)

# Same as _HARNESS but treats each option as a plain string label — the shape the
# offset-account dropdown (v0.3.4) feeds in (ERPNext GL Account docnames), which
# use the module's default identity getLabel rather than an object `.name`.
_HARNESS_STR = (
    "const m = require(process.argv[1]);"
    "const p = JSON.parse(process.argv[2]);"
    "const out = m.filterOptions(p.options, p.query);"
    "process.stdout.write(JSON.stringify(out));"
)


def run_filter(options, query):
    """Run the shipped filterOptions against `options`/`query`, return labels."""
    res = subprocess.run(
        [NODE, '-e', _HARNESS, MODULE_PATH,
         json.dumps({'options': options, 'query': query})],
        capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise AssertionError('node harness failed: ' + res.stderr)
    return json.loads(res.stdout)


def run_filter_strings(options, query):
    """Run the shipped filterOptions over plain-string options (default label),
    mirroring how the offset-account dropdown filters GL Account docnames."""
    res = subprocess.run(
        [NODE, '-e', _HARNESS_STR, MODULE_PATH,
         json.dumps({'options': options, 'query': query})],
        capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise AssertionError('node harness failed: ' + res.stderr)
    return json.loads(res.stdout)


def _opts(*names):
    return [{'name': n} for n in names]


@unittest.skipIf(NODE is None, 'node not installed — JS filter test skipped')
class TestFilterOptions(unittest.TestCase):
    """The pure, tested core of the dropdown — the piece the v0.3.2 bug touched."""

    MERCHANTS = _opts('Chevron', 'Chevy Dealership', 'Whole Foods',
                      'Shell', 'CHEVELLE Auto')

    def test_substring_match(self):
        self.assertEqual(run_filter(self.MERCHANTS, 'che'),
                         ['Chevron', 'Chevy Dealership', 'CHEVELLE Auto'])

    def test_case_insensitive(self):
        # Upper-case query matches lower/mixed-case labels and vice-versa.
        self.assertEqual(run_filter(self.MERCHANTS, 'CHE'),
                         ['Chevron', 'Chevy Dealership', 'CHEVELLE Auto'])

    def test_matches_middle_of_label(self):
        self.assertEqual(run_filter(self.MERCHANTS, 'foods'), ['Whole Foods'])

    def test_empty_query_returns_all(self):
        self.assertEqual(run_filter(self.MERCHANTS, ''),
                         [m['name'] for m in self.MERCHANTS])

    def test_whitespace_query_returns_all(self):
        # A query of just spaces is trimmed to empty → show everything.
        self.assertEqual(run_filter(self.MERCHANTS, '   '),
                         [m['name'] for m in self.MERCHANTS])

    def test_no_match_returns_empty(self):
        self.assertEqual(run_filter(self.MERCHANTS, 'zzz'), [])

    def test_null_label_does_not_throw(self):
        # A merchant row with a null name must not blow up the whole filter.
        opts = [{'name': None}, {'name': 'Shell'}]
        self.assertEqual(run_filter(opts, 'she'), ['Shell'])

    def test_query_is_trimmed(self):
        self.assertEqual(run_filter(self.MERCHANTS, '  shell  '), ['Shell'])


@unittest.skipIf(NODE is None, 'node not installed — JS filter test skipped')
class TestAccountFilterOptions(unittest.TestCase):
    """The offset-account dropdown (v0.3.4, replacing the Safari-flaky native
    <datalist>) feeds plain GL Account docname strings through the same shared
    filterOptions core — here exercised with the module's default identity label
    instead of an object `.name`."""

    # ERPNext leaf-account docnames: `<number> - <name> - <company_abbr>`.
    ACCOUNTS = [
        '5100 - Fuel Expense - EC',
        '5110 - Repairs & Maintenance - EC',
        '4000 - Sales - EC',
        '1200 - Bank Accounts - EC',
        '5200 - Freight Expense - EC',
    ]

    def test_substring_match_on_docname(self):
        self.assertEqual(run_filter_strings(self.ACCOUNTS, 'expense'),
                         ['5100 - Fuel Expense - EC',
                          '5200 - Freight Expense - EC'])

    def test_match_on_account_number(self):
        self.assertEqual(run_filter_strings(self.ACCOUNTS, '511'),
                         ['5110 - Repairs & Maintenance - EC'])

    def test_case_insensitive(self):
        self.assertEqual(run_filter_strings(self.ACCOUNTS, 'FUEL'),
                         ['5100 - Fuel Expense - EC'])

    def test_empty_query_returns_all(self):
        self.assertEqual(run_filter_strings(self.ACCOUNTS, ''), self.ACCOUNTS)

    def test_no_match_returns_empty(self):
        # Drives the "use as new" empty state — free-text accounts still work.
        self.assertEqual(run_filter_strings(self.ACCOUNTS, 'zzz'), [])

    def test_query_is_trimmed(self):
        self.assertEqual(run_filter_strings(self.ACCOUNTS, '  sales  '),
                         ['4000 - Sales - EC'])


class _AppBase(unittest.TestCase):
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

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)


class TestDropdownAssetWiring(_AppBase):
    def test_static_module_is_served(self):
        r = self.client.get('/static/rule_dropdown.js')
        self.assertEqual(r.status_code, 200)
        self.assertIn('javascript', r.headers.get('Content-Type', ''))
        body = r.data.decode()
        self.assertIn('SPDX-License-Identifier: MIT', body)
        self.assertIn('filterOptions', body)
        self.assertIn('createDropdown', body)

    def test_rules_page_loads_the_module(self):
        r = self.client.get('/admin/rules')
        self.assertEqual(r.status_code, 200)
        html = r.data.decode()
        self.assertIn('/static/rule_dropdown.js', html)
        self.assertIn('BankBridgeDropdown.createDropdown', html)

    def test_rules_page_has_empty_state_copy(self):
        # The "use as new" empty-state string must reach the page.
        r = self.client.get('/admin/rules')
        self.assertIn('as new', r.data.decode())

    def test_offset_account_uses_custom_dropdown_not_datalist(self):
        # v0.3.4: the offset_account field moved off the native <datalist> (which
        # Safari collapsed mid-type) onto the shared BankBridgeDropdown. The
        # datalist markup must be gone and the plain input + menu present.
        # Check for the real element/attribute, not the bare word "datalist" —
        # the explanatory comments still name the old element on purpose.
        html = self.client.get('/admin/rules').data.decode()
        self.assertNotIn('<datalist id=', html)
        self.assertNotIn('list="acctlist"', html)
        self.assertIn('id="offset-account"', html)
        self.assertIn('id="oa-dd"', html)

    def test_known_accounts_endpoint_returns_accounts_shape(self):
        # The offset-account dropdown fetches from this endpoint. Unconfigured
        # ERPNext → an empty list (field stays free-text), never an error.
        r = self.client.get('/api/rules/known_accounts')
        self.assertEqual(r.status_code, 200)
        self.assertIn('json', r.headers.get('Content-Type', ''))
        self.assertEqual(r.get_json(), {'accounts': []})


if __name__ == '__main__':
    unittest.main()
