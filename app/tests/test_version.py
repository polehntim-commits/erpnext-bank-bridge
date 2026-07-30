# SPDX-License-Identifier: MIT
"""The ONE place the current version is pinned.

    ┌─────────────────────────────────────────────────────────────────────┐
    │  Bumping the release? Edit EXPECTED_VERSION below, and nothing else. │
    └─────────────────────────────────────────────────────────────────────┘

This file exists because the pin used to live in whichever test suite was newest
(test_funnel.py at 0.7.0, test_tailscale_sidecar.py at 0.7.1). Every release then
left a stale assertion behind in the previous release's file, and the bump broke
a suite that had nothing to do with the change — twice, before it was worth
fixing properly. A release-independent home costs one file and ends it.

The pin is deliberately a literal. Comparing `__version__` to anything derived
from itself would assert nothing; the whole value here is catching a release
where the bump was forgotten, and only a hand-written expectation does that.
"""
import unittest

EXPECTED_VERSION = '0.9.1'


class VersionTest(unittest.TestCase):
    def test_version_is_bumped(self):
        import app as app_pkg
        self.assertEqual(
            EXPECTED_VERSION, app_pkg.__version__,
            'app/app/__init__.py __version__ and tests/test_version.py '
            'EXPECTED_VERSION disagree — bump both.')

    def test_the_version_is_well_formed(self):
        """A malformed bump ('0.7.2 ', 'v0.7.2') would ship a broken
        /api/health payload and a broken MCP serverInfo.version."""
        self.assertRegex(EXPECTED_VERSION, r'^\d+\.\d+\.\d+$')

    def test_the_app_reports_it_on_the_health_endpoint(self):
        """The version is not decoration — /api/health is how a deploy is
        verified, and MCP `initialize` hands it to every AI client."""
        import os
        import tempfile
        os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')
        from app import create_app, crypto
        fd, path = tempfile.mkstemp(suffix='.sqlite')
        self.addCleanup(os.remove, path)
        self.addCleanup(os.close, fd)
        self.addCleanup(crypto.reset_cache)
        app = create_app({
            'TESTING': True, 'SCHEDULER_ENABLED': False, 'FERNET_KEY': '',
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{path}',
            'DATA_DIR': tempfile.mkdtemp(),
            'TAILSCALE_SIDECAR_ENABLED': False,
        })
        body = app.test_client().get('/api/health').get_json()
        self.assertEqual(EXPECTED_VERSION, body['version'])
        self.assertEqual(EXPECTED_VERSION, app.config['APP_VERSION'])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
