# SPDX-License-Identifier: MIT
"""Reconnecting a bank without losing your configuration (v0.4.11).

Two gaps this closes, one expensive and one destructive.

EXPENSIVE: nothing detected ITEM_LOGIN_REQUIRED, so the poll loop called a dead
Item on every cycle — a failed billable request per poll, forever — and the
operator's first clue was that transactions had stopped.

DESTRUCTIVE: a re-link mints new Plaid ids for the same real accounts, and
every mapping this app keys on account_id then misses. Not just "re-map it":
the ERPNext Bank Account dedup key misses too (so the import tries to create a
duplicate and fails), and the opening-balance idempotency key misses (so a
SECOND opening balance becomes eligible, silently double-counting).

Covered here:

  * re-auth detection from BOTH signals — the ITEM webhook and the error text of
    a failed sync — so an install with no public webhook URL still works
  * the cost fix: a parked Item is not polled, and a successful sync un-parks it
    so a transient warning can't strand a bank forever
  * update mode: the link token carries the access_token and DROPS `products`
    (Plaid rejects them together), and reconnect_complete exchanges nothing
  * fingerprint adoption: exact, institution-scoped, unambiguous-only, and it
    MOVES the mapping rather than copying it — two rows naming one ERPNext Bank
    Account would double-post every transaction in the overlap
  * the load-bearing bit: the opening-balance synthetic key is re-keyed, so an
    adopted account cannot book a second opening balance
  * regressions: v0.4.7 disconnect semantics and the v0.4.10 statement overlay
    are untouched

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
import unittest.mock
from datetime import date, datetime

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, crypto, db, reconnect  # noqa: E402
from app import opening_balance as obal  # noqa: E402
from app import sync_engine  # noqa: E402
from app.models import (GeneratedJournalEntry, PlaidAccount,  # noqa: E402
                        PlaidItem)
from app.plaid_client import PlaidClient, PlaidError  # noqa: E402

from tests.fakes import FakeERPClient, FakePlaidClient, page  # noqa: E402


class ReconnectBase(unittest.TestCase):
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

    def _item(self, item_id='item-abc', institution_id='ins_1',
              institution='Wells Fargo', disconnected=False,
              needs_reauth=False, company=None):
        it = PlaidItem(
            item_id=item_id,
            access_token_encrypted=crypto.encrypt(f'access-{item_id}'),
            institution_id=institution_id, institution_name=institution,
            status='active', disconnected=disconnected,
            needs_reauth=needs_reauth, owning_company=company)
        db.session.add(it)
        db.session.commit()
        return it

    def _account(self, account_id, item_id='item-abc', mask='1234',
                 type_='depository', subtype='checking', bank_account=None,
                 gl=None, company=None, import_status='pending',
                 opening_je=None):
        a = PlaidAccount(
            account_id=account_id, item_id=item_id, name='Checking',
            mask=mask, type=type_, subtype=subtype,
            erpnext_bank_account_name=bank_account, erpnext_gl_account_name=gl,
            owning_company=company, import_status=import_status,
            opening_balance_je_id=opening_je)
        db.session.add(a)
        db.session.commit()
        return a

    def _opening_entry(self, account_id, je_id=None):
        """A GeneratedJournalEntry standing in for a booked opening balance,
        keyed the way opening_balance.py keys them."""
        row = GeneratedJournalEntry(
            plaid_transaction_id=f'{obal.SYNTHETIC_PREFIX}{account_id}',
            rule_name=obal.RULE_LABEL, state='pending_review', amount=100.0,
            description='opening balance')
        db.session.add(row)
        db.session.commit()
        return row


# ── re-auth detection ───────────────────────────────────────────────────────

class ReauthDetectionTests(ReconnectBase):
    def test_recognizes_every_reauth_code_in_an_error(self):
        for code in reconnect.REAUTH_CODES:
            with self.subTest(code=code):
                err = PlaidError(f'transactions_sync failed: (500) {{"error_code": '
                                 f'"{code}", "error_message": "..."}}')
                self.assertEqual(reconnect.is_reauth_error(err), code)

    def test_an_ordinary_failure_is_not_a_reauth(self):
        err = PlaidError('transactions_sync failed: RATE_LIMIT_EXCEEDED')
        self.assertEqual(reconnect.is_reauth_error(err), '')

    def test_marking_is_idempotent(self):
        """The same dead link rediscovered must not re-audit every poll."""
        item = self._item()
        self.assertTrue(reconnect.mark_needs_reauth(item, 'ITEM_LOGIN_REQUIRED'))
        self.assertFalse(reconnect.mark_needs_reauth(item, 'ITEM_LOGIN_REQUIRED'))
        self.assertTrue(item.needs_reauth)
        self.assertEqual(item.reauth_reason, 'ITEM_LOGIN_REQUIRED')
        self.assertIsInstance(item.reauth_detected_at, datetime)

    def test_a_changed_reason_is_recorded(self):
        item = self._item()
        reconnect.mark_needs_reauth(item, 'PENDING_EXPIRATION')
        self.assertTrue(reconnect.mark_needs_reauth(item, 'ITEM_LOGIN_REQUIRED'))
        self.assertEqual(item.reauth_reason, 'ITEM_LOGIN_REQUIRED')

    def test_clearing(self):
        item = self._item(needs_reauth=True)
        item.reauth_reason = 'ITEM_LOGIN_REQUIRED'
        db.session.commit()
        self.assertTrue(reconnect.clear_reauth(item))
        self.assertFalse(item.needs_reauth)
        self.assertIsNone(item.reauth_reason)
        self.assertFalse(reconnect.clear_reauth(item))

    def test_every_code_has_operator_facing_help(self):
        for code in reconnect.REAUTH_CODES:
            self.assertTrue(reconnect.REAUTH_HELP.get(code), code)


# ── the cost fix ────────────────────────────────────────────────────────────

class PollSkipTests(ReconnectBase):
    def test_a_parked_item_is_not_polled(self):
        """The whole cost argument: a bank that cannot answer must not be
        called every cycle."""
        self._item('item-ok')
        self._item('item-parked', needs_reauth=True)
        fake = FakePlaidClient(accounts=[], pages=[page(), page()])
        res = sync_engine.sync_all(fake, FakeERPClient())
        self.assertEqual([r.get('item_id') for r in res['results']],
                         ['item-ok'])

    def test_a_failed_sync_parks_the_item_without_a_webhook(self):
        """Detection must not depend on a public webhook URL — most installs
        don't have one."""
        item = self._item()

        class Boom(FakePlaidClient):
            def transactions_sync(self, *a, **kw):
                raise PlaidError('transactions_sync failed: (400) '
                                 '{"error_code": "ITEM_LOGIN_REQUIRED"}')

        sync_engine.sync_all(Boom(accounts=[]), FakeERPClient())
        db.session.refresh(item)
        self.assertTrue(item.needs_reauth)
        self.assertEqual(item.reauth_reason, 'ITEM_LOGIN_REQUIRED')

    def test_an_ordinary_failure_does_not_park_the_item(self):
        item = self._item()

        class Boom(FakePlaidClient):
            def transactions_sync(self, *a, **kw):
                raise PlaidError('transactions_sync failed: INTERNAL_SERVER_ERROR')

        sync_engine.sync_all(Boom(accounts=[]), FakeERPClient())
        db.session.refresh(item)
        self.assertFalse(item.needs_reauth)
        self.assertEqual(item.status, 'error')

    def test_a_successful_sync_unparks_the_item(self):
        """Self-healing: a PENDING_EXPIRATION warning the bank later resolves
        must not strand the connection forever."""
        item = self._item(needs_reauth=True)
        item.reauth_reason = 'PENDING_EXPIRATION'
        db.session.commit()
        # sync_all skips parked items, so drive the Item directly — this is the
        # path a manual "Sync now" or a reconnect-then-sync takes.
        sync_engine.sync_item(item, FakePlaidClient(accounts=[], pages=[page()]),
                              FakeERPClient())
        db.session.refresh(item)
        self.assertFalse(item.needs_reauth)

    def test_a_disconnected_item_is_not_counted_as_parked(self):
        self._item('item-gone', disconnected=True, needs_reauth=True)
        self.assertEqual(reconnect.items_needing_reauth(), [])


# ── update mode ─────────────────────────────────────────────────────────────

class UpdateModeTests(ReconnectBase):
    def _client(self):
        return PlaidClient(client_id='cid', secret='sec', api=object())

    def test_update_mode_sends_the_access_token_and_drops_products(self):
        """Plaid rejects `products` alongside an access_token — an Item's
        products are fixed at creation."""
        client = self._client()
        kwargs = client._update_mode_kwargs('u', None, None, 'access-tok')
        self.assertEqual(kwargs['access_token'], 'access-tok')
        self.assertNotIn('products', kwargs)
        self.assertIn('user', kwargs)
        self.assertIn('country_codes', kwargs)

    def test_normal_mode_still_sends_products(self):
        client = self._client()
        kwargs = client._link_token_kwargs('u', None, None)
        self.assertTrue(kwargs['products'])
        self.assertNotIn('access_token', kwargs)

    def test_update_mode_keeps_the_redirect_uri_for_oauth_banks(self):
        client = self._client()
        kwargs = client._update_mode_kwargs('u', 'https://x.test/return', None,
                                            'access-tok')
        self.assertEqual(kwargs['redirect_uri'], 'https://x.test/return')

    def test_create_link_token_routes_to_update_mode(self):
        client = self._client()
        seen = {}

        def fake_create(api, kwargs):
            seen.update(kwargs)
            return 'link-update-token'

        with unittest.mock.patch.object(client, '_get_api', lambda: object()), \
             unittest.mock.patch.object(PlaidClient, '_link_token_create',
                                        staticmethod(fake_create)):
            token = client.create_link_token('u', access_token='access-tok',
                                             statements=True)
        self.assertEqual(token, 'link-update-token')
        self.assertEqual(seen['access_token'], 'access-tok')
        # statements=True must NOT smuggle a products list back in.
        self.assertNotIn('products', seen)
        self.assertNotIn('statements', seen)


class ReconnectEndpointTests(ReconnectBase):
    def test_link_token_for_an_item_uses_update_mode(self):
        self._item('item-abc')
        captured = {}

        class Cap(FakePlaidClient):
            def create_link_token(self, user_id, redirect_uri=None,
                                  webhook=None, statements=False,
                                  statements_months=24, access_token=None):
                captured['access_token'] = access_token
                return 'tok'

        with unittest.mock.patch.object(sync_engine, 'get_plaid_client',
                                        return_value=Cap()), \
             unittest.mock.patch('app.plaid_settings.is_configured',
                                 return_value=True):
            resp = self.client.post('/bankbridge/api/plaid/create_link_token',
                                    json={'item_id': 'item-abc'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['update_mode'])
        self.assertEqual(captured['access_token'], 'access-item-abc')

    def test_link_token_without_an_item_is_a_normal_link(self):
        captured = {}

        class Cap(FakePlaidClient):
            def create_link_token(self, user_id, redirect_uri=None,
                                  webhook=None, statements=False,
                                  statements_months=24, access_token=None):
                captured['access_token'] = access_token
                return 'tok'

        with unittest.mock.patch.object(sync_engine, 'get_plaid_client',
                                        return_value=Cap()), \
             unittest.mock.patch('app.plaid_settings.is_configured',
                                 return_value=True):
            resp = self.client.post('/bankbridge/api/plaid/create_link_token',
                                    json={})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()['update_mode'])
        self.assertIsNone(captured['access_token'])

    def test_update_mode_refuses_a_disconnected_item(self):
        """Its access_token was invalidated at Plaid — update mode cannot
        repair it, and pretending otherwise wastes a call and confuses the
        operator."""
        self._item('item-gone', disconnected=True)
        with unittest.mock.patch('app.plaid_settings.is_configured',
                                 return_value=True):
            resp = self.client.post('/bankbridge/api/plaid/create_link_token',
                                    json={'item_id': 'item-gone'})
        self.assertEqual(resp.status_code, 409)

    def test_update_mode_404s_an_unknown_item(self):
        with unittest.mock.patch('app.plaid_settings.is_configured',
                                 return_value=True):
            resp = self.client.post('/bankbridge/api/plaid/create_link_token',
                                    json={'item_id': 'nope'})
        self.assertEqual(resp.status_code, 404)

    def test_reconnect_complete_unparks_the_item(self):
        item = self._item(needs_reauth=True)
        with unittest.mock.patch.object(sync_engine, 'get_plaid_client',
                                        return_value=FakePlaidClient(accounts=[])):
            resp = self.client.post(
                '/bankbridge/api/plaid/reconnect_complete',
                json={'item_id': 'item-abc'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['was_parked'])
        db.session.refresh(item)
        self.assertFalse(item.needs_reauth)

    def test_reconnect_complete_is_idempotent(self):
        self._item(needs_reauth=True)
        with unittest.mock.patch.object(sync_engine, 'get_plaid_client',
                                        return_value=FakePlaidClient(accounts=[])):
            self.client.post('/bankbridge/api/plaid/reconnect_complete',
                             json={'item_id': 'item-abc'})
            resp = self.client.post('/bankbridge/api/plaid/reconnect_complete',
                                    json={'item_id': 'item-abc'})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()['was_parked'])

    def test_reconnect_complete_survives_a_plaid_outage(self):
        """The flag is cleared even if the courtesy account refresh fails."""
        item = self._item(needs_reauth=True)
        with unittest.mock.patch.object(sync_engine, 'get_plaid_client',
                                        side_effect=PlaidError('down')):
            resp = self.client.post(
                '/bankbridge/api/plaid/reconnect_complete',
                json={'item_id': 'item-abc'})
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(item)
        self.assertFalse(item.needs_reauth)


# ── webhooks ────────────────────────────────────────────────────────────────

class WebhookTests(ReconnectBase):
    def _post(self, payload):
        return self.client.post('/bankbridge/api/plaid/webhook', json=payload)

    def test_item_login_required_parks_the_item(self):
        item = self._item()
        resp = self._post({'webhook_type': 'ITEM',
                           'webhook_code': 'ITEM_LOGIN_REQUIRED',
                           'item_id': 'item-abc'})
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(item)
        self.assertTrue(item.needs_reauth)
        self.assertEqual(item.reauth_reason, 'ITEM_LOGIN_REQUIRED')

    def test_pending_expiration_parks_the_item(self):
        """The only advance notice Plaid gives — an operator who reconnects in
        the warning window never has a failed sync at all."""
        item = self._item()
        self._post({'webhook_type': 'ITEM',
                    'webhook_code': 'PENDING_EXPIRATION',
                    'item_id': 'item-abc'})
        db.session.refresh(item)
        self.assertTrue(item.needs_reauth)

    def test_an_error_webhook_reads_the_nested_code(self):
        item = self._item()
        self._post({'webhook_type': 'ITEM', 'webhook_code': 'ERROR',
                    'item_id': 'item-abc',
                    'error': {'error_code': 'ITEM_LOGIN_REQUIRED'}})
        db.session.refresh(item)
        self.assertTrue(item.needs_reauth)
        self.assertEqual(item.reauth_reason, 'ITEM_LOGIN_REQUIRED')

    def test_an_unrelated_item_webhook_changes_nothing(self):
        item = self._item()
        self._post({'webhook_type': 'ITEM', 'webhook_code': 'WEBHOOK_UPDATE_ACKNOWLEDGED',
                    'item_id': 'item-abc'})
        db.session.refresh(item)
        self.assertFalse(item.needs_reauth)

    def test_an_item_webhook_makes_no_plaid_calls(self):
        """The cost claim, asserted: handling this is free."""
        self._item()
        called = []
        with unittest.mock.patch.object(sync_engine, 'sync_item',
                                        side_effect=lambda *a, **k: called.append(1)):
            self._post({'webhook_type': 'ITEM',
                        'webhook_code': 'ITEM_LOGIN_REQUIRED',
                        'item_id': 'item-abc'})
        self.assertEqual(called, [])

    def test_a_webhook_for_a_disconnected_item_is_ignored(self):
        item = self._item(disconnected=True)
        self._post({'webhook_type': 'ITEM',
                    'webhook_code': 'ITEM_LOGIN_REQUIRED',
                    'item_id': 'item-abc'})
        db.session.refresh(item)
        self.assertFalse(item.needs_reauth)

    def test_transactions_webhook_still_kicks_a_sync_by_default(self):
        self._item()
        called = []
        with unittest.mock.patch.object(sync_engine, 'sync_item',
                                        side_effect=lambda *a, **k: called.append(1)):
            self._post({'webhook_type': 'TRANSACTIONS',
                        'webhook_code': 'SYNC_UPDATES_AVAILABLE',
                        'item_id': 'item-abc'})
        self.assertEqual(len(called), 1)

    def test_the_transactions_sync_kick_can_be_turned_off_for_cost(self):
        """The one webhook behaviour that costs Plaid calls beyond the poll."""
        self.app.config['PLAID_WEBHOOK_TRIGGERS_SYNC'] = False
        self._item()
        called = []
        with unittest.mock.patch.object(sync_engine, 'sync_item',
                                        side_effect=lambda *a, **k: called.append(1)):
            self._post({'webhook_type': 'TRANSACTIONS',
                        'webhook_code': 'SYNC_UPDATES_AVAILABLE',
                        'item_id': 'item-abc'})
        self.assertEqual(called, [])

    def test_turning_the_sync_kick_off_keeps_item_handling(self):
        self.app.config['PLAID_WEBHOOK_TRIGGERS_SYNC'] = False
        item = self._item()
        self._post({'webhook_type': 'ITEM',
                    'webhook_code': 'ITEM_LOGIN_REQUIRED',
                    'item_id': 'item-abc'})
        db.session.refresh(item)
        self.assertTrue(item.needs_reauth)


# ── fingerprinting ──────────────────────────────────────────────────────────

class FingerprintTests(ReconnectBase):
    def test_identity_is_mask_type_subtype(self):
        a = self._account('a-1', mask='1234')
        self.assertEqual(reconnect.fingerprint(a),
                         ('1234', 'depository', 'checking'))

    def test_a_blank_mask_cannot_be_fingerprinted(self):
        """Without a mask, 'depository/checking' matches every checking account
        at the bank — and adoption would attach one account's ledger to
        another."""
        a = self._account('a-1', mask='')
        self.assertIsNone(reconnect.fingerprint(a))

    def test_an_account_with_no_configuration_is_not_a_donor(self):
        self.assertFalse(reconnect.has_configuration(self._account('a-1')))

    def test_any_configured_field_makes_a_donor(self):
        self.assertTrue(reconnect.has_configuration(
            self._account('a-2', bank_account='BA-1')))
        self.assertTrue(reconnect.has_configuration(
            self._account('a-3', company='Testing')))
        self.assertTrue(reconnect.has_configuration(
            self._account('a-4', import_status='imported')))


class AdoptionTests(ReconnectBase):
    def _relink_scenario(self, **donor_kwargs):
        """The shape of a real re-link: an old Item with a configured account,
        and a new Item at the SAME institution whose account is fresh."""
        self._item('item-old', disconnected=True)
        defaults = dict(bank_account='BA-1', gl='Wells Checking - EC',
                        company='Testing', import_status='imported')
        defaults.update(donor_kwargs)
        donor = self._account('old-acct', item_id='item-old', **defaults)
        new_item = self._item('item-new')
        return donor, new_item

    def test_adopts_the_mapping_onto_the_relinked_account(self):
        donor, new_item = self._relink_scenario()
        heir = self._account('new-acct', item_id='item-new')
        report = reconnect.adopt_if_unambiguous(heir, new_item)

        self.assertIsNotNone(report)
        self.assertEqual(heir.erpnext_bank_account_name, 'BA-1')
        self.assertEqual(heir.erpnext_gl_account_name, 'Wells Checking - EC')
        self.assertEqual(heir.owning_company, 'Testing')
        self.assertEqual(heir.import_status, 'imported')

    def test_the_mapping_is_moved_not_copied(self):
        """Two rows naming one ERPNext Bank Account would both push into it and
        duplicate every transaction in the overlap."""
        donor, new_item = self._relink_scenario()
        heir = self._account('new-acct', item_id='item-new')
        reconnect.adopt_if_unambiguous(heir, new_item)

        db.session.refresh(donor)
        self.assertIsNone(donor.erpnext_bank_account_name)
        self.assertIsNone(donor.erpnext_gl_account_name)
        self.assertFalse(donor.sync_enabled)
        self.assertEqual(donor.import_status, 'superseded')
        self.assertEqual(donor.superseded_by_account_id, 'new-acct')

    def test_the_opening_balance_key_is_rekeyed(self):
        """THE load-bearing assertion. The synthetic key is what actually stops
        a second opening balance being booked; leaving it on the dead
        account_id would silently double-count the starting position."""
        donor, new_item = self._relink_scenario()
        self._opening_entry('old-acct')
        heir = self._account('new-acct', item_id='item-new')
        reconnect.adopt_if_unambiguous(heir, new_item)

        old_key = f'{obal.SYNTHETIC_PREFIX}old-acct'
        new_key = f'{obal.SYNTHETIC_PREFIX}new-acct'
        self.assertIsNone(GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id=old_key).first())
        self.assertIsNotNone(GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id=new_key).first())

    def test_an_adopted_account_will_not_book_a_second_opening_balance(self):
        """The consequence of the re-key, asserted through the REAL guard.

        `opening_balance.existing_entry` is what every booking path checks
        before writing. Before adoption re-keys the row, the heir looks to it
        like an account that has never had an opening balance — and books a
        second one, double-counting the starting position."""
        donor, new_item = self._relink_scenario()
        self._opening_entry('old-acct')
        heir = self._account('new-acct', item_id='item-new')

        # Before: the guard cannot see the donor's entry.
        self.assertIsNone(obal.existing_entry(heir))
        reconnect.adopt_if_unambiguous(heir, new_item)
        # After: it can, so nothing will book a second one.
        self.assertIsNotNone(obal.existing_entry(heir))
        self.assertEqual(obal.opening_balance_status(heir), 'pending')

    def test_does_not_adopt_across_institutions(self):
        """A Wells Fargo ...1234 is not a Columbia Bank ...1234."""
        self._item('item-old', institution_id='ins_1', disconnected=True)
        self._account('old-acct', item_id='item-old', bank_account='BA-1',
                      import_status='imported')
        other = self._item('item-new', institution_id='ins_999')
        heir = self._account('new-acct', item_id='item-new')
        self.assertIsNone(reconnect.adopt_if_unambiguous(heir, other))
        self.assertIsNone(heir.erpnext_bank_account_name)

    def test_does_not_adopt_on_a_different_mask(self):
        self._relink_scenario()
        new_item = PlaidItem.query.filter_by(item_id='item-new').one()
        heir = self._account('new-acct', item_id='item-new', mask='9999')
        self.assertIsNone(reconnect.adopt_if_unambiguous(heir, new_item))

    def test_does_not_adopt_on_a_different_subtype(self):
        self._relink_scenario()
        new_item = PlaidItem.query.filter_by(item_id='item-new').one()
        heir = self._account('new-acct', item_id='item-new', subtype='savings')
        self.assertIsNone(reconnect.adopt_if_unambiguous(heir, new_item))

    def test_ambiguity_is_reported_never_guessed(self):
        """Two retired accounts sharing a fingerprint is rare enough that a
        human should look — guessing would attach the wrong ledger."""
        self._item('item-old', disconnected=True)
        self._account('old-a', item_id='item-old', bank_account='BA-1',
                      import_status='imported')
        self._account('old-b', item_id='item-old', bank_account='BA-2',
                      import_status='imported')
        new_item = self._item('item-new')
        heir = self._account('new-acct', item_id='item-new')
        self.assertIsNone(reconnect.adopt_if_unambiguous(heir, new_item))
        self.assertIsNone(heir.erpnext_bank_account_name)

    def test_an_already_superseded_donor_is_not_adopted_twice(self):
        donor, new_item = self._relink_scenario()
        heir1 = self._account('new-acct-1', item_id='item-new')
        reconnect.adopt_if_unambiguous(heir1, new_item)
        heir2 = self._account('new-acct-2', item_id='item-new')
        self.assertIsNone(reconnect.adopt_if_unambiguous(heir2, new_item))

    def test_an_explicit_company_choice_outranks_the_old_row(self):
        """The operator may have picked a Company on the Link page for this very
        reconnect; their explicit choice wins."""
        self._relink_scenario()
        new_item = PlaidItem.query.filter_by(item_id='item-new').one()
        heir = self._account('new-acct', item_id='item-new',
                             company='Orchard LLC')
        reconnect.adopt_if_unambiguous(heir, new_item)
        self.assertEqual(heir.owning_company, 'Orchard LLC')
        self.assertEqual(heir.erpnext_bank_account_name, 'BA-1')

    def test_the_switch_disables_adoption(self):
        self.app.config['RECONNECT_ADOPT_ENABLED'] = False
        self._relink_scenario()
        new_item = PlaidItem.query.filter_by(item_id='item-new').one()
        heir = self._account('new-acct', item_id='item-new')
        self.assertIsNone(reconnect.adopt_if_unambiguous(heir, new_item))

    def test_adoption_runs_from_the_account_refresh(self):
        """End to end: the path a real re-link actually takes."""
        self._item('item-old', disconnected=True)
        self._account('old-acct', item_id='item-old', bank_account='BA-1',
                      gl='Wells Checking - EC', import_status='imported')
        new_item = self._item('item-new')
        fake = FakePlaidClient(accounts=[{
            'account_id': 'new-acct', 'name': 'Checking', 'official_name': '',
            'mask': '1234', 'type': 'depository', 'subtype': 'checking',
            'balance_available': 10.0, 'balance_current': 10.0,
            'iso_currency_code': 'USD'}])
        sync_engine.refresh_accounts(new_item, fake, 'access-item-new')

        heir = PlaidAccount.query.filter_by(account_id='new-acct').one()
        self.assertEqual(heir.erpnext_bank_account_name, 'BA-1')
        self.assertEqual(heir.import_status, 'imported')


# ── repointing ERPNext ──────────────────────────────────────────────────────

class RepointTests(ReconnectBase):
    def test_rewrites_the_dedup_key_on_the_bank_account(self):
        """Without this the adoption is half done: ERPNext's dedup filters on
        `plaid_account_id`, so a stale id makes the next import try to create a
        duplicate Bank Account and fail."""
        heir = self._account('new-acct', bank_account='BA-1')
        erp = FakeERPClient()
        erp.created['Bank Account']['BA-1'] = {'plaid_account_id': 'old-acct'}
        self.assertTrue(reconnect.repoint_erpnext_bank_account(erp, heir))
        self.assertEqual(erp.created['Bank Account']['BA-1']['plaid_account_id'],
                         'new-acct')

    def test_an_already_correct_record_costs_no_write(self):
        heir = self._account('new-acct', bank_account='BA-1')
        erp = FakeERPClient()
        erp.created['Bank Account']['BA-1'] = {'plaid_account_id': 'new-acct'}
        self.assertFalse(reconnect.repoint_erpnext_bank_account(erp, heir))
        self.assertEqual([c for c in erp.calls if c[0] == 'update_doc'], [])

    def test_converges_after_an_erpnext_outage(self):
        """Adoption commits locally even when ERPNext is down; the sync path
        finishes the job later."""
        donor = self._account('old-acct', bank_account='BA-1',
                              import_status='imported')
        donor.superseded_by_account_id = 'new-acct'
        donor.erpnext_bank_account_name = None
        db.session.commit()
        self._account('new-acct', bank_account='BA-1')
        erp = FakeERPClient()
        erp.created['Bank Account']['BA-1'] = {'plaid_account_id': 'old-acct'}
        self.assertEqual(reconnect.repoint_adopted_accounts(erp), 1)
        self.assertEqual(erp.created['Bank Account']['BA-1']['plaid_account_id'],
                         'new-acct')

    def test_is_a_no_op_without_erpnext(self):
        self._account('new-acct', bank_account='BA-1')
        self.assertEqual(reconnect.repoint_adopted_accounts(None), 0)


# ── admin page ──────────────────────────────────────────────────────────────

class AccountsPageTests(ReconnectBase):
    def test_a_parked_bank_shows_a_reconnect_button(self):
        item = self._item(needs_reauth=True)
        item.reauth_reason = 'ITEM_LOGIN_REQUIRED'
        db.session.commit()
        body = self.client.get('/admin/accounts').data.decode()
        self.assertIn('Needs reconnect', body)
        self.assertIn('data-bb-reconnect="item-abc"', body)
        self.assertIn('sign in again', body)
        # The Link script is needed for the button to work at all.
        self.assertIn('link-initialize.js', body)

    def test_a_healthy_bank_loads_no_third_party_script(self):
        self._item()
        body = self.client.get('/admin/accounts').data.decode()
        self.assertNotIn('Needs reconnect', body)
        self.assertNotIn('link-initialize.js', body)

    def test_the_disconnect_modal_no_longer_promises_a_lossless_relink(self):
        """The v0.4.10 copy said 'You can re-link this bank later' with no
        caveat, which was a cheque the code did not honour."""
        self._item()
        body = self.client.get('/admin/accounts').data.decode()
        self.assertIn('new account ids', body)
        self.assertIn('Reconnect instead', body)


# ── regressions ─────────────────────────────────────────────────────────────

class RegressionTests(ReconnectBase):
    def test_v047_disconnect_still_skips_the_old_item(self):
        self._item('item-old', disconnected=True)
        self._item('item-new')
        res = sync_engine.sync_all(FakePlaidClient(accounts=[], pages=[page()]),
                                   FakeERPClient())
        self.assertEqual([r.get('item_id') for r in res['results']],
                         ['item-new'])

    def test_a_healthy_install_syncs_exactly_as_before(self):
        self._item()
        res = sync_engine.sync_all(FakePlaidClient(accounts=[], pages=[page()]),
                                   FakeERPClient())
        self.assertEqual(len(res['results']), 1)

    def test_the_migration_declares_every_new_column(self):
        from app.migrations import SCHEMA_MIGRATIONS
        added = {(t, c) for t, c, _ in SCHEMA_MIGRATIONS}
        for column in ('needs_reauth', 'reauth_reason', 'reauth_detected_at'):
            self.assertIn(('plaid_items', column), added)
        self.assertIn(('plaid_accounts', 'superseded_by_account_id'), added)

    def test_existing_accounts_default_to_never_superseded(self):
        account = self._account('a-1')
        self.assertIsNone(account.superseded_by_account_id)
        self.assertFalse(self._item().needs_reauth)


if __name__ == '__main__':
    unittest.main()
