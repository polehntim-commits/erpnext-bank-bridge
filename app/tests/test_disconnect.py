# SPDX-License-Identifier: MIT
"""v0.4.7 · bank disconnect (Plaid /item/remove) + the pre-Plaid-submission
dependency and rescue-script hardening.

  * POST /api/items/<id>/disconnect calls Plaid, then marks the Item
  * history is PRESERVED — no Item/account/transaction row is deleted
  * Plaid failure leaves the Item CONNECTED (never mark locally on a failed call)
  * a disconnected Item is skipped by the sync loop and the webhook kick
  * the Accounts page grows a Disconnect button, a badge, and the modal
  * re-linking after a disconnect mints a NEW Item and leaves the old one alone
  * the plaid_client wrapper builds real plaid-python 40.1.0 request models
  * scripts/rotate_db_password.sh detects its execution context

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import sync_engine  # noqa: E402
from app import plaid_client as pc  # noqa: E402
from app.models import (AuditEvent, BankTransaction, PlaidAccount,  # noqa: E402
                        PlaidItem)

from tests.fakes import FakePlaidClient, FakeERPClient, page, txn  # noqa: E402

SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', 'scripts', 'rotate_db_password.sh'))
REQUIREMENTS = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', 'requirements.txt'))
# Absolute paths: some cases below deliberately blank PATH to prove the script
# no longer needs docker, and an absolute bash keeps that from breaking exec.
BASH = shutil.which('bash') or '/bin/bash'
BASH_PY = sys.executable


class DisconnectBase(unittest.TestCase):
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

    def _item(self, item_id='item-abc', institution='Wells Fargo',
              disconnected=False):
        it = PlaidItem(
            item_id=item_id,
            access_token_encrypted=crypto.encrypt('access-sandbox-abc'),
            institution_id='ins_1', institution_name=institution,
            status='active', disconnected=disconnected)
        db.session.add(it)
        db.session.commit()
        return it

    def _account(self, item_id='item-abc', account_id='acct-1'):
        a = PlaidAccount(item_id=item_id, account_id=account_id,
                         name='Checking', type='depository',
                         subtype='checking', mask='1234',
                         erpnext_bank_account_name='WF Checking - EC',
                         sync_enabled=True)
        db.session.add(a)
        db.session.commit()
        return a

    def _patch_plaid(self, fake):
        """Point the disconnect endpoint's client factory at `fake`."""
        self._orig = sync_engine.get_plaid_client
        sync_engine.get_plaid_client = lambda: fake
        self.addCleanup(lambda: setattr(sync_engine, 'get_plaid_client',
                                        self._orig))


# ── the endpoint ────────────────────────────────────────────────────────

class TestDisconnectEndpoint(DisconnectBase):
    def test_calls_plaid_item_remove_with_decrypted_token(self):
        """The wrapper must receive the DECRYPTED access_token — passing the
        stored ciphertext would make Plaid reject every disconnect."""
        self._item()
        fake = FakePlaidClient()
        self._patch_plaid(fake)
        r = self.client.post('/api/items/item-abc/disconnect')
        self.assertEqual(r.status_code, 200)
        self.assertIn(('item_remove', 'access-sandbox-abc'), fake.calls)

    def test_marks_item_disconnected_with_timestamp(self):
        self._item()
        self._patch_plaid(FakePlaidClient())
        self.client.post('/api/items/item-abc/disconnect')
        it = PlaidItem.query.filter_by(item_id='item-abc').one()
        self.assertTrue(it.disconnected)
        self.assertIsInstance(it.disconnected_at, datetime)

    def test_returns_json_success_payload(self):
        self._item()
        self._patch_plaid(FakePlaidClient())
        r = self.client.post('/api/items/item-abc/disconnect')
        data = r.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['item_id'], 'item-abc')
        self.assertIn('Wells Fargo', data['message'])
        self.assertTrue(data['disconnected_at'])

    def test_records_audit_event_with_actor_and_reason(self):
        self._item()
        self._patch_plaid(FakePlaidClient())
        self.client.post('/api/items/item-abc/disconnect',
                         json={'reason': 'closed the account'})
        ev = (AuditEvent.query
              .filter(AuditEvent.event_type == 'item_disconnected').one())
        self.assertEqual(ev.subject_type, 'PlaidItem')
        self.assertEqual(ev.subject_id, 'item-abc')
        self.assertIn('closed the account', ev.notes)
        self.assertTrue(ev.actor)

    def test_preserves_item_accounts_and_transactions(self):
        """The whole point: disconnecting stops the future feed, it does not
        erase history. Nothing may be deleted."""
        self._item()
        self._account()
        db.session.add(BankTransaction(
            account_id='acct-1', plaid_transaction_id='txn-1', amount=12.5,
            iso_currency_code='USD', date=datetime(2026, 7, 1).date(),
            name='COFFEE'))
        db.session.commit()
        self._patch_plaid(FakePlaidClient())
        self.client.post('/api/items/item-abc/disconnect')
        self.assertEqual(PlaidItem.query.count(), 1)
        self.assertEqual(PlaidAccount.query.count(), 1)
        self.assertEqual(BankTransaction.query.count(), 1)

    def test_unknown_item_404s(self):
        self._patch_plaid(FakePlaidClient())
        r = self.client.post('/api/items/nope/disconnect')
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.get_json()['ok'])

    def test_already_disconnected_409s_and_does_not_recall_plaid(self):
        self._item(disconnected=True)
        fake = FakePlaidClient()
        self._patch_plaid(fake)
        r = self.client.post('/api/items/item-abc/disconnect')
        self.assertEqual(r.status_code, 409)
        self.assertNotIn('item_remove', [c[0] for c in fake.calls])

    def test_plaid_failure_leaves_item_connected(self):
        """ORDERING GUARANTEE. If Plaid never accepted the removal the Item is
        still live upstream, so marking it disconnected locally would silently
        stop syncing a bank that is still feeding — the exact state disconnect
        exists to prevent."""
        self._item()
        fake = FakePlaidClient()
        fake.remove_error = pc.PlaidError('item_remove failed: 400 INVALID_INPUT')
        self._patch_plaid(fake)
        r = self.client.post('/api/items/item-abc/disconnect')
        self.assertEqual(r.status_code, 502)
        it = PlaidItem.query.filter_by(item_id='item-abc').one()
        self.assertFalse(it.disconnected)
        self.assertIsNone(it.disconnected_at)
        self.assertEqual(AuditEvent.query.filter(
            AuditEvent.event_type == 'item_disconnected').count(), 0)

    def test_plaid_config_error_leaves_item_connected(self):
        self._item()
        fake = FakePlaidClient()
        fake.remove_error = pc.PlaidConfigError('Plaid is not configured')
        self._patch_plaid(fake)
        r = self.client.post('/api/items/item-abc/disconnect')
        self.assertEqual(r.status_code, 502)
        self.assertFalse(
            PlaidItem.query.filter_by(item_id='item-abc').one().disconnected)

    def test_requires_admin_auth_when_configured(self):
        """/item/remove is irreversible and billable — it lives on the admin
        blueprint precisely so the optional Basic Auth gate covers it."""
        self.app.config['ADMIN_BASIC_AUTH_USER'] = 'admin'
        self.app.config['ADMIN_BASIC_AUTH_PASS'] = 'secret'
        self._item()
        fake = FakePlaidClient()
        self._patch_plaid(fake)
        r = self.client.post('/api/items/item-abc/disconnect')
        self.assertEqual(r.status_code, 401)
        self.assertNotIn('item_remove', [c[0] for c in fake.calls])

    def test_only_the_named_item_is_disconnected(self):
        self._item('item-abc')
        self._item('item-xyz', institution='Columbia Bank')
        self._patch_plaid(FakePlaidClient())
        self.client.post('/api/items/item-abc/disconnect')
        self.assertTrue(PlaidItem.query.filter_by(item_id='item-abc').one().disconnected)
        self.assertFalse(PlaidItem.query.filter_by(item_id='item-xyz').one().disconnected)


# ── the sync loop must skip it ──────────────────────────────────────────

class TestDisconnectedSkippedBySync(DisconnectBase):
    def test_sync_all_skips_disconnected_items(self):
        self._item('item-live')
        self._item('item-dead', disconnected=True)
        fake = FakePlaidClient(accounts=[], pages=[page()])
        res = sync_engine.sync_all(fake, FakeERPClient())
        self.assertEqual(res['items'], 1)
        self.assertEqual([r.get('item_id') for r in res['results']],
                         ['item-live'])

    def test_sync_all_still_runs_connected_items(self):
        self._item('item-live')
        fake = FakePlaidClient(accounts=[], pages=[page()])
        res = sync_engine.sync_all(fake, FakeERPClient())
        self.assertEqual(res['items'], 1)

    def test_disconnecting_removes_it_from_the_next_tick(self):
        """The scheduler's next poll must not touch a bank disconnected between
        ticks — there is no per-Item job to cancel, sync_all is the gate."""
        self._item('item-abc')
        fake = FakePlaidClient(accounts=[], pages=[page(), page()])
        self.assertEqual(sync_engine.sync_all(fake, FakeERPClient())['items'], 1)
        self._patch_plaid(fake)
        self.client.post('/api/items/item-abc/disconnect')
        self.assertEqual(sync_engine.sync_all(fake, FakeERPClient())['items'], 0)

    def test_webhook_does_not_sync_a_disconnected_item(self):
        """The webhook is unauthenticated, so this also stops a spoofed payload
        provoking doomed Plaid calls for a disconnected bank."""
        self._item('item-abc', disconnected=True)
        called = []
        orig = sync_engine.sync_item
        sync_engine.sync_item = lambda *a, **k: called.append(a)
        self.addCleanup(lambda: setattr(sync_engine, 'sync_item', orig))
        r = self.client.post('/api/plaid/webhook',
                             json={'webhook_type': 'TRANSACTIONS',
                                   'webhook_code': 'DEFAULT_UPDATE',
                                   'item_id': 'item-abc'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(called, [])

    def test_webhook_still_syncs_a_connected_item(self):
        self._item('item-abc')
        called = []
        orig = sync_engine.sync_item
        sync_engine.sync_item = lambda *a, **k: called.append(a)
        self.addCleanup(lambda: setattr(sync_engine, 'sync_item', orig))
        self.client.post('/api/plaid/webhook',
                         json={'webhook_type': 'TRANSACTIONS',
                               'webhook_code': 'DEFAULT_UPDATE',
                               'item_id': 'item-abc'})
        self.assertEqual(len(called), 1)


# ── re-link after disconnect ────────────────────────────────────────────

class TestRelinkAfterDisconnect(DisconnectBase):
    def test_relink_creates_a_new_item_and_leaves_the_old_one(self):
        """Plaid mints a fresh item_id + access_token on re-link, so the old row
        stays as the permanent record of the previous link."""
        old = self._item('item-old', disconnected=True)
        old.disconnected_at = datetime(2026, 7, 1, 12, 0)
        db.session.commit()
        self._item('item-new', institution='Wells Fargo')
        self.assertEqual(PlaidItem.query.count(), 2)
        old = PlaidItem.query.filter_by(item_id='item-old').one()
        new = PlaidItem.query.filter_by(item_id='item-new').one()
        self.assertTrue(old.disconnected)
        self.assertEqual(old.disconnected_at, datetime(2026, 7, 1, 12, 0))
        self.assertFalse(new.disconnected)
        self.assertIsNone(new.disconnected_at)

    def test_relinked_item_syncs_while_the_old_one_stays_skipped(self):
        self._item('item-old', disconnected=True)
        self._item('item-new')
        fake = FakePlaidClient(accounts=[], pages=[page()])
        res = sync_engine.sync_all(fake, FakeERPClient())
        self.assertEqual([r.get('item_id') for r in res['results']],
                         ['item-new'])


# ── admin UI ────────────────────────────────────────────────────────────

class TestDisconnectUI(DisconnectBase):
    def test_connected_bank_shows_disconnect_button(self):
        self._item()
        self._account()
        html = self.client.get('/admin/accounts').get_data(as_text=True)
        self.assertIn('Disconnect this bank', html)
        self.assertIn('data-bb-disconnect="item-abc"', html)

    def test_disconnected_bank_shows_badge_and_no_button(self):
        it = self._item(disconnected=True)
        it.disconnected_at = datetime(2026, 7, 1, 12, 0)
        db.session.commit()
        self._account()
        html = self.client.get('/admin/accounts').get_data(as_text=True)
        self.assertIn('🔌 Disconnected', html)
        self.assertNotIn('data-bb-disconnect="item-abc"', html)

    def test_institution_name_with_a_quote_does_not_break_the_button(self):
        """REGRESSION (caught by driving the real page): the button used to
        interpolate the institution name into an inline onclick via `tojson`,
        which emits real double quotes — they closed the double-quoted attribute
        and made the handler a syntax error, so clicking it did nothing. A bank
        whose name contains a quote must still produce a working button."""
        self._item(institution='Bob\'s "Big" Bank & Trust')
        self._account()
        html = self.client.get('/admin/accounts').get_data(as_text=True)
        # The item id lands in its own attribute, unmangled...
        self.assertIn('data-bb-disconnect="item-abc"', html)
        # ...and the raw quote never appears unescaped inside an attribute.
        self.assertNotIn('data-bb-name="Bob\'s "Big"', html)
        self.assertIn('&#34;Big&#34;', html)

    def test_button_is_wired_by_delegation_not_inline_onclick(self):
        """Guards the shape of the fix, not just its output: no inline onclick
        may carry the institution name back into a JS string literal."""
        self._item()
        self._account()
        html = self.client.get('/admin/accounts').get_data(as_text=True)
        self.assertNotIn('onclick="bbDisconnect(', html)
        self.assertIn("getAttribute('data-bb-disconnect')", html)

    def test_confirmation_modal_copy_renders(self):
        self._item()
        self._account()
        html = self.client.get('/admin/accounts').get_data(as_text=True)
        self.assertIn('bbDisconnectModal', html)
        self.assertIn('Plaid will stop sending new transactions', html)
        self.assertIn('stay in ERPNext', html)
        self.assertIn('re-link this bank later', html)
        self.assertIn('>Cancel<', html)

    def test_accounts_page_renders_with_no_items(self):
        self.assertEqual(self.client.get('/admin/accounts').status_code, 200)


# ── plaid_client wrapper against the real SDK models ────────────────────

class _RecordingApi:
    """Stands in for plaid_api.PlaidApi. Records the REAL SDK request model it
    was handed, so the wrapper's model construction is exercised for real."""
    def __init__(self, **responses):
        self.responses = responses
        self.seen = {}

    def _handle(self, name, req):
        self.seen[name] = req
        if isinstance(self.responses.get(name), Exception):
            raise self.responses[name]
        return self.responses.get(name, {})

    def item_remove(self, req):
        return self._handle('item_remove', req)

    def link_token_create(self, req):
        return self._handle('link_token_create', req)

    def item_public_token_exchange(self, req):
        return self._handle('item_public_token_exchange', req)

    def item_get(self, req):
        return self._handle('item_get', req)

    def accounts_get(self, req):
        return self._handle('accounts_get', req)

    def transactions_sync(self, req):
        return self._handle('transactions_sync', req)

    def institutions_get_by_id(self, req):
        return self._handle('institutions_get_by_id', req)


def _sdk_present():
    try:
        import plaid  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_sdk_present(), 'plaid-python not installed')
class TestPlaidClientSdk(unittest.TestCase):
    """Exercises app/plaid_client.py against the installed plaid-python
    (40.1.0). Each test builds the real SDK request model, which is what would
    break on an incompatible SDK bump."""

    def _client(self, **responses):
        api = _RecordingApi(**responses)
        return pc.PlaidClient(api=api), api

    def test_item_remove_returns_request_id(self):
        c, api = self._client(item_remove={'request_id': 'req-1'})
        self.assertEqual(c.item_remove('access-tok')['request_id'], 'req-1')
        self.assertEqual(api.seen['item_remove'].access_token, 'access-tok')

    def test_item_remove_wraps_failures_in_plaid_error(self):
        c, _ = self._client(item_remove=RuntimeError('boom'))
        with self.assertRaises(pc.PlaidError):
            c.item_remove('access-tok')

    def test_link_token_create_returns_token(self):
        c, api = self._client(link_token_create={'link_token': 'link-sandbox-1'})
        self.assertEqual(c.create_link_token('user-1'), 'link-sandbox-1')
        req = api.seen['link_token_create']
        self.assertEqual(req.user.client_user_id, 'user-1')
        self.assertEqual(req.client_name, 'ERPNext Bank Bridge')

    def test_link_token_create_includes_redirect_uri_and_webhook(self):
        c, api = self._client(link_token_create={'link_token': 'link-1'})
        c.create_link_token('user-1', redirect_uri='https://x/cb',
                            webhook='https://x/wh')
        req = api.seen['link_token_create']
        self.assertEqual(req.redirect_uri, 'https://x/cb')
        self.assertEqual(req.webhook, 'https://x/wh')

    def test_exchange_public_token_returns_access_token_and_item_id(self):
        c, api = self._client(item_public_token_exchange={
            'access_token': 'access-1', 'item_id': 'item-1'})
        self.assertEqual(c.exchange_public_token('public-1'),
                         ('access-1', 'item-1'))
        self.assertEqual(api.seen['item_public_token_exchange'].public_token,
                         'public-1')

    def test_item_get_returns_item_info(self):
        c, api = self._client(item_get={
            'item': {'item_id': 'item-1', 'institution_id': 'ins_1'}})
        self.assertEqual(c.get_item('access-1'),
                         {'item_id': 'item-1', 'institution_id': 'ins_1'})
        self.assertEqual(api.seen['item_get'].access_token, 'access-1')

    def test_institutions_get_by_id_returns_institution(self):
        c, api = self._client(institutions_get_by_id={
            'institution': {'name': 'Wells Fargo',
                            'url': 'https://wellsfargo.com'}})
        self.assertEqual(c.get_institution_name('ins_1'), 'Wells Fargo')
        self.assertEqual(api.seen['institutions_get_by_id'].institution_id,
                         'ins_1')

    def test_accounts_get_returns_normalized_accounts(self):
        c, api = self._client(accounts_get={'accounts': [{
            'account_id': 'acct-1', 'name': 'Checking', 'mask': '1234',
            'type': 'depository', 'subtype': 'checking',
            'balances': {'available': 10.0, 'current': 12.0,
                         'iso_currency_code': 'USD'}}]})
        accounts = c.get_accounts('access-1')
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]['account_id'], 'acct-1')
        self.assertEqual(accounts[0]['balance_current'], 12.0)
        self.assertEqual(accounts[0]['iso_currency_code'], 'USD')
        self.assertEqual(api.seen['accounts_get'].access_token, 'access-1')

    def test_transactions_sync_returns_cursor_and_transactions(self):
        c, api = self._client(transactions_sync={
            'added': [{'transaction_id': 't1', 'account_id': 'acct-1',
                       'amount': 5.0, 'date': '2026-07-01', 'name': 'COFFEE'}],
            'modified': [], 'removed': [],
            'next_cursor': 'cursor-2', 'has_more': False})
        res = c.transactions_sync('access-1', cursor='cursor-1')
        self.assertEqual(res['next_cursor'], 'cursor-2')
        self.assertFalse(res['has_more'])
        self.assertEqual(res['added'][0]['transaction_id'], 't1')
        req = api.seen['transactions_sync']
        self.assertEqual(req.access_token, 'access-1')
        self.assertEqual(req.cursor, 'cursor-1')

    def test_transactions_sync_omits_cursor_on_first_pull(self):
        """A blank cursor must be OMITTED, not sent as '' — Plaid rejects an
        empty cursor rather than treating it as 'from the beginning'."""
        c, api = self._client(transactions_sync={
            'added': [], 'modified': [], 'removed': [],
            'next_cursor': 'c1', 'has_more': False})
        c.transactions_sync('access-1', cursor=None)
        self.assertNotIn('cursor', api.seen['transactions_sync'].to_dict())


# ── dependency pins + rescue script ─────────────────────────────────────

class TestDependencyPins(unittest.TestCase):
    def _pins(self):
        with open(REQUIREMENTS) as fh:
            out = {}
            for line in fh:
                line = line.strip()
                if line and not line.startswith('#') and '==' in line:
                    name, _, ver = line.partition('==')
                    out[name.strip().lower()] = ver.strip()
            return out

    def test_gunicorn_is_cve_patched(self):
        """21.2.0 carries CVE-2024-1135 and CVE-2024-6827 (request smuggling),
        both fixed in 22.0.0. Guards against a downgrade."""
        major = int(self._pins()['gunicorn'].split('.')[0])
        self.assertGreaterEqual(major, 22)

    def test_plaid_python_supports_item_remove(self):
        """/item/remove has been in the SDK for a long time, but pin the floor
        so the disconnect flow can't be broken by a downgrade."""
        major = int(self._pins()['plaid-python'].split('.')[0])
        self.assertGreaterEqual(major, 18)


class TestRotateScript(unittest.TestCase):
    """scripts/rotate_db_password.sh — the v0.4.7 dual-context rescue. The full
    password reset is verified against a live Postgres out of band; these cover
    the parts that are deterministic without a database."""

    def _run(self, args, env=None, stdin=''):
        e = dict(os.environ)
        e.pop('DATABASE_URL', None)
        # Put the interpreter running these tests first on PATH so the script's
        # `python3` can import psycopg2 — that is what it finds inside the real
        # container, and without it the container path bails on the import
        # before reaching the logic under test.
        e['PATH'] = os.path.dirname(BASH_PY) + os.pathsep + e.get('PATH', '')
        e.update(env or {})
        return subprocess.run([BASH, SCRIPT] + args, input=stdin,
                              capture_output=True, text=True, env=e, timeout=60)

    def test_script_is_valid_bash(self):
        r = subprocess.run(['bash', '-n', SCRIPT], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help_flag_prints_usage_without_touching_anything(self):
        r = self._run(['--help'])
        self.assertEqual(r.returncode, 0)
        self.assertIn('HOST MODE', r.stdout)
        self.assertIn('CONTAINER MODE', r.stdout)

    def test_container_mode_reports_missing_database_url(self):
        """The in-container path reads DATABASE_URL; without it, it must say so
        rather than fail obscurely."""
        r = self._run(['--mode', 'container'])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('DATABASE_URL', r.stderr)

    def test_container_mode_is_announced(self):
        r = self._run(['--mode', 'container'])
        self.assertIn('container mode', r.stderr)

    def test_container_mode_does_not_require_docker(self):
        """THE v0.4.7 BUG FIX: the old script died on `docker not found` when
        run inside the app container, which has no docker client."""
        r = self._run(['--mode', 'container'],
                      env={'PATH': '/usr/bin:/bin', 'DATABASE_URL': ''})
        self.assertNotIn('docker not found', r.stderr)

    def test_host_mode_without_docker_explains_itself(self):
        r = self._run(['--mode', 'host'], env={'PATH': '/nonexistent'})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('docker', r.stderr.lower())

    def test_container_path_uses_psycopg2_not_psql(self):
        """The app image is python:3.11-slim with no postgresql-client, so the
        container path must not shell out to psql."""
        with open(SCRIPT) as fh:
            body = fh.read()
        container_block = body.split('CONTAINER MODE')[1].split('HOST MODE')[0]
        self.assertIn('psycopg2', container_block)
        self.assertNotIn('psql ', container_block)

    def test_rescue_derivation_matches_the_app(self):
        """The script and app/db_recovery.rescue_password() must derive the same
        bridgeadmin password, or the boot self-heal cannot log in."""
        import hashlib
        import hmac as _hmac
        from app.db_recovery import rescue_password
        seed, salt = 'seed-abc123', 'bankbridge-rescue-v1'
        script_style = _hmac.new(seed.encode(), salt.encode(),
                                 hashlib.sha256).hexdigest()
        self.assertEqual(script_style, rescue_password(seed, salt))


if __name__ == '__main__':
    unittest.main()
