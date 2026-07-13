# SPDX-License-Identifier: MIT
"""Sync engine: Plaid pull → local mirror → ERPNext push.

  * idempotency: same Plaid transaction twice → one local row, one ERPNext doc
  * added → local row + submitted ERPNext Bank Transaction, docname recorded
  * amount sign mapping: Plaid outflow → withdrawal, inflow → deposit
  * modified → ERPNext doc cancelled + replaced, local amount updated
  * removed → ERPNext doc cancelled (docstatus 2), row marked removed
  * unmapped account → mirrored locally but not pushed (stays pending)
  * failed push → sync_error recorded, row stays pending, retry re-posts

    cd erpnext-bank-bridge_v0.1.0
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import sync_engine  # noqa: E402
from app.models import (BankTransaction, PlaidAccount, PlaidItem,  # noqa: E402
                        PlaidSyncLog)
from app import erpnext_bank  # noqa: E402

from tests.fakes import FakePlaidClient, FakeERPClient, page, txn  # noqa: E402

ACC = 'acct-wf-checking'


class SyncBase(unittest.TestCase):
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
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _item(self, item_id='item-abc'):
        it = PlaidItem(item_id=item_id,
                       access_token_encrypted=crypto.encrypt('access-sandbox-abc'),
                       institution_id='ins_1', institution_name='Wells Fargo',
                       status='active')
        db.session.add(it)
        db.session.commit()
        return it

    def _account(self, mapped=True, enabled=True, account_id=ACC):
        a = PlaidAccount(account_id=account_id, item_id='item-abc',
                         name='WF Checking', mask='1234', type='depository',
                         subtype='checking',
                         erpnext_bank_account_name='WF Checking - Ops' if mapped else None,
                         sync_enabled=enabled)
        db.session.add(a)
        db.session.commit()
        return a

    def _plaid_accounts(self):
        return [{'account_id': ACC, 'name': 'WF Checking', 'official_name': '',
                 'mask': '1234', 'type': 'depository', 'subtype': 'checking',
                 'balance_available': 900.0, 'balance_current': 1000.0,
                 'iso_currency_code': 'USD'}]


class TestPullMirror(SyncBase):
    def test_added_creates_local_and_pushes(self):
        item = self._item()
        self._account()
        plaid = FakePlaidClient(accounts=self._plaid_accounts(), pages=[
            page(added=[txn('t1', ACC, 25.50, name='Coffee', merchant_name='Blue Bottle')]),
        ])
        erp = FakeERPClient()
        sync_engine.sync_item(item, plaid, erp)

        rows = BankTransaction.query.all()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.plaid_transaction_id, 't1')
        self.assertIsNotNone(r.posted_at)
        self.assertTrue(r.erpnext_bank_transaction_id)
        # ERPNext got one create + one submit; withdrawal mapping (outflow).
        creates = erp.creates_of()
        self.assertEqual(len(creates), 1)
        doc = creates[0][2]
        self.assertEqual(doc['withdrawal'], 25.50)
        self.assertEqual(doc['deposit'], 0.0)
        self.assertEqual(doc['reference_number'], 't1')
        self.assertEqual(doc['bank_account'], 'WF Checking - Ops')
        self.assertIn(r.erpnext_bank_transaction_id, erp.submitted)
        self.assertIn('Blue Bottle', doc['description'])

    def test_deposit_mapping_for_inflow(self):
        item = self._item()
        self._account()
        plaid = FakePlaidClient(accounts=self._plaid_accounts(), pages=[
            page(added=[txn('t-in', ACC, -500.0, name='Payroll deposit')]),
        ])
        erp = FakeERPClient()
        sync_engine.sync_item(item, plaid, erp)
        doc = erp.creates_of()[0][2]
        self.assertEqual(doc['deposit'], 500.0)
        self.assertEqual(doc['withdrawal'], 0.0)

    def test_idempotent_same_txn_twice(self):
        item = self._item()
        self._account()
        erp = FakeERPClient()
        # First sync delivers t1.
        plaid = FakePlaidClient(accounts=self._plaid_accounts(),
                                pages=[page(added=[txn('t1', ACC, 10.0)])])
        sync_engine.sync_item(item, plaid, erp)
        # Second sync re-delivers the SAME transaction (e.g. cursor replay).
        plaid.pages = [page(added=[txn('t1', ACC, 10.0)])]
        sync_engine.sync_item(item, plaid, erp)

        self.assertEqual(BankTransaction.query.count(), 1)
        # Only ONE ERPNext Bank Transaction created (find-or-create guarded it).
        self.assertEqual(len(erp.creates_of()), 1)

    def test_multi_page_pagination(self):
        item = self._item()
        self._account()
        plaid = FakePlaidClient(accounts=self._plaid_accounts(), pages=[
            page(added=[txn('p1', ACC, 1.0)], next_cursor='c1', has_more=True),
            page(added=[txn('p2', ACC, 2.0)], next_cursor='c2', has_more=False),
        ])
        erp = FakeERPClient()
        sync_engine.sync_item(item, plaid, erp)
        self.assertEqual(BankTransaction.query.count(), 2)
        db.session.refresh(item)
        self.assertEqual(item.cursor, 'c2')


class TestModifiedRemoved(SyncBase):
    def _post_initial(self, erp):
        item = self._item()
        self._account()
        plaid = FakePlaidClient(accounts=self._plaid_accounts(),
                                pages=[page(added=[txn('t1', ACC, 10.0, name='Orig')])])
        sync_engine.sync_item(item, plaid, erp)
        return item, plaid

    def test_modified_cancels_and_replaces(self):
        erp = FakeERPClient()
        item, plaid = self._post_initial(erp)
        first_name = BankTransaction.query.filter_by(plaid_transaction_id='t1').first(
        ).erpnext_bank_transaction_id
        # Now Plaid modifies t1 (amount + name change).
        plaid.pages = [page(modified=[txn('t1', ACC, 12.5, name='Corrected')])]
        sync_engine.sync_item(item, plaid, erp)

        r = BankTransaction.query.filter_by(plaid_transaction_id='t1').first()
        self.assertEqual(r.amount, 12.5)
        self.assertEqual(r.name, 'Corrected')
        # Old ERPNext doc cancelled; a new one created and repointed.
        self.assertIn(first_name, erp.cancelled)
        self.assertNotEqual(r.erpnext_bank_transaction_id, first_name)
        self.assertEqual(len(erp.creates_of()), 2)

    def test_removed_cancels_erpnext(self):
        erp = FakeERPClient()
        item, plaid = self._post_initial(erp)
        name = BankTransaction.query.filter_by(plaid_transaction_id='t1').first(
        ).erpnext_bank_transaction_id
        plaid.pages = [page(removed=['t1'])]
        sync_engine.sync_item(item, plaid, erp)

        r = BankTransaction.query.filter_by(plaid_transaction_id='t1').first()
        self.assertTrue(r.removed)
        self.assertIn(name, erp.cancelled)
        self.assertIsNotNone(r.posted_at)


class TestEligibility(SyncBase):
    def test_unmapped_account_mirrors_but_does_not_push(self):
        item = self._item()
        self._account(mapped=False)
        plaid = FakePlaidClient(accounts=self._plaid_accounts(),
                                pages=[page(added=[txn('t1', ACC, 10.0)])])
        erp = FakeERPClient()
        sync_engine.sync_item(item, plaid, erp)
        r = BankTransaction.query.filter_by(plaid_transaction_id='t1').first()
        self.assertIsNotNone(r)                 # mirrored locally
        self.assertIsNone(r.posted_at)          # still pending
        self.assertEqual(len(erp.creates_of()), 0)

    def test_disabled_account_not_pushed(self):
        item = self._item()
        self._account(mapped=True, enabled=False)
        plaid = FakePlaidClient(accounts=self._plaid_accounts(),
                                pages=[page(added=[txn('t1', ACC, 10.0)])])
        erp = FakeERPClient()
        sync_engine.sync_item(item, plaid, erp)
        self.assertEqual(len(erp.creates_of()), 0)


class TestFailureAndRetry(SyncBase):
    def test_failed_push_records_error_then_retry_succeeds(self):
        item = self._item()
        self._account()
        plaid = FakePlaidClient(accounts=self._plaid_accounts(),
                                pages=[page(added=[txn('t1', ACC, 10.0)])])
        bad = FakeERPClient(fail_create=True)
        sync_engine.sync_item(item, plaid, bad)
        r = BankTransaction.query.filter_by(plaid_transaction_id='t1').first()
        self.assertIsNone(r.posted_at)
        self.assertIsNotNone(r.sync_error)

        # A healthy ERPNext client + retry_row now posts it.
        good = FakeERPClient()
        import app.sync_engine as se
        orig = se.get_erp_client_or_none
        se.get_erp_client_or_none = lambda: good
        try:
            ok, msg = sync_engine.retry_row(r.id)
        finally:
            se.get_erp_client_or_none = orig
        self.assertTrue(ok, msg)
        db.session.refresh(r)
        self.assertIsNotNone(r.posted_at)
        self.assertIsNone(r.sync_error)


class TestLogging(SyncBase):
    def test_pull_and_push_write_log_rows(self):
        item = self._item()
        self._account()
        plaid = FakePlaidClient(accounts=self._plaid_accounts(),
                                pages=[page(added=[txn('t1', ACC, 10.0)])])
        sync_engine.sync_item(item, plaid, FakeERPClient())
        self.assertEqual(PlaidSyncLog.query.filter_by(direction='plaid_pull').count(), 1)
        self.assertEqual(PlaidSyncLog.query.filter_by(direction='erpnext_push').count(), 1)


class TestMapping(SyncBase):
    def test_deposit_withdrawal_split(self):
        self.assertEqual(erpnext_bank._deposit_withdrawal(10.0), (0.0, 10.0))
        self.assertEqual(erpnext_bank._deposit_withdrawal(-10.0), (10.0, 0.0))
        self.assertEqual(erpnext_bank._deposit_withdrawal(0.0), (0.0, 0.0))


if __name__ == '__main__':
    unittest.main()
