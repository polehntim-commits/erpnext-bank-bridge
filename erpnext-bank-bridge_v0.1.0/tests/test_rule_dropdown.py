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


def run_filter(options, query):
    """Run the shipped filterOptions against `options`/`query`, return labels."""
    res = subprocess.run(
        [NODE, '-e', _HARNESS, MODULE_PATH,
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


if __name__ == '__main__':
    unittest.main()
