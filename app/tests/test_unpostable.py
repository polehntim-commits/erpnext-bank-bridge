# SPDX-License-Identifier: MIT
"""Mirrored transactions that cannot post (v0.4.13).

THE BUG. A Plaid Item can carry accounts ERPNext has no home for — a mortgage or
student loan alongside a checking account is the realistic case, since
`is_supported` refuses to give a loan a Bank Account (it is not one in ERPNext's
model) while /transactions/sync keeps returning its payments. Those transactions
mirror locally and can never post.

That part is correct: the transactions really happened, and dropping them would
lose history the account's own import would want. What was wrong is that the
push path LOADED every one of them on every run just to skip it — so the work
grew without bound, forever, and their ids were also fed into the intercompany
pair detector on each pass. It also incremented `skipped`, which made the number
meaningless for the case it was supposed to describe.

THE FIX. Restrict the scan to accounts that can actually receive a posting, in
SQL. The rows are untouched, and the behaviour stays SELF-HEALING — mapping the
account (or re-enabling sync) makes its whole backlog eligible on the very next
push, with no flag to set and no migration.

Covered here:

  * unpostable rows are not loaded, not counted as skipped, and not handed to
    the intercompany detector
  * the self-healing property, which is what makes filtering safe rather than
    merely cheap: map the account and the backlog posts
  * the same holds for a deliberately sync-disabled account, in both directions
  * the count is reported rather than silent — in the push stats, the sync log
    and per-account on the Accounts page
  * regressions: a normal push still posts, and an install with nothing mapped
    doesn't construct a degenerate query

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
import unittest.mock
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, crypto, db, sync_engine  # noqa: E402
from app.models import BankTransaction, PlaidAccount, PlaidItem  # noqa: E402

from tests.fakes import FakeERPClient  # noqa: E402


class UnpostableBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': tempfile.mkdtemp(),
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.session.add(PlaidItem(
            item_id='item-abc', access_token_encrypted=crypto.encrypt('t'),
            institution_id='ins_1', institution_name='Wells Fargo',
            status='active'))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _account(self, account_id, *, mapped=True, sync_enabled=True,
                 type_='depository', subtype='checking'):
        a = PlaidAccount(
            account_id=account_id, item_id='item-abc', name=subtype,
            mask='1234', type=type_, subtype=subtype,
            erpnext_bank_account_name=('BA-' + account_id) if mapped else None,
            sync_enabled=sync_enabled, import_status='imported' if mapped
            else 'pending')
        db.session.add(a)
        db.session.commit()
        return a

    def _mortgage(self, account_id='acct-loan'):
        """The account this bug is actually about."""
        return self._account(account_id, mapped=False, type_='loan',
                             subtype='mortgage')

    def _txns(self, account_id, count, start_id=0):
        for i in range(count):
            db.session.add(BankTransaction(
                plaid_transaction_id=f'{account_id}-t{start_id + i}',
                account_id=account_id, amount=100.0 + i,
                date=date(2026, 7, 1), name='PAYMENT'))
        db.session.commit()


# ── the fix ─────────────────────────────────────────────────────────────────

class ScanScopeTests(UnpostableBase):
    def test_unpostable_rows_are_not_loaded(self):
        """The bug: every push used to load these and skip them, forever."""
        self._account('acct-ok')
        self._txns('acct-ok', 2)
        self._mortgage()
        self._txns('acct-loan', 40)

        stats = sync_engine.push_pending(FakeERPClient())
        # The mortgage rows are neither posted nor counted as skipped — they
        # were never in the scan at all. `skipped == 0` alongside
        # `unpostable == 40` is the whole fix in two numbers: before, those 40
        # were loaded and skipped on every single push.
        self.assertEqual(stats['posted'], 2)
        self.assertEqual(stats['skipped'], 0)
        self.assertEqual(stats['unpostable'], 40)

    def test_unpostable_rows_are_not_fed_to_the_pair_detector(self):
        """The second, quieter cost: their ids went into intercompany detection
        on every push, so that input grew without bound too."""
        self._account('acct-ok')
        self._txns('acct-ok', 2)
        self._mortgage()
        self._txns('acct-loan', 30)

        seen = {}

        def capture(limit_transaction_ids=None, **kw):
            seen['ids'] = list(limit_transaction_ids or [])
            return []

        with unittest.mock.patch('app.intercompany.detect_pairs',
                                 side_effect=capture):
            sync_engine.push_pending(FakeERPClient())
        self.assertEqual(len(seen['ids']), 2)
        self.assertTrue(all(i.startswith('acct-ok') for i in seen['ids']))

    def test_the_rows_themselves_are_kept(self):
        """They are real mirrored transactions — dropping them would lose
        history the account's own import would want."""
        self._mortgage()
        self._txns('acct-loan', 5)
        sync_engine.push_pending(FakeERPClient())
        self.assertEqual(BankTransaction.query.count(), 5)
        self.assertTrue(all(t.posted_at is None
                            for t in BankTransaction.query.all()))

    def test_nothing_mapped_posts_nothing_and_does_not_explode(self):
        """The degenerate case: an `IN ()` over an empty eligible set."""
        self._mortgage()
        self._txns('acct-loan', 3)
        stats = sync_engine.push_pending(FakeERPClient())
        self.assertEqual(stats['posted'], 0)
        self.assertEqual(stats['unpostable'], 3)


# ── self-healing ────────────────────────────────────────────────────────────

class SelfHealingTests(UnpostableBase):
    def test_mapping_the_account_posts_its_whole_backlog(self):
        """This is what makes filtering safe rather than merely cheap. No flag
        is set on the rows, so nothing has to be unwound when the account
        becomes mappable — the very next push picks them all up."""
        account = self._mortgage()
        self._txns('acct-loan', 6)
        erp = FakeERPClient()
        self.assertEqual(sync_engine.push_pending(erp)['posted'], 0)

        account.erpnext_bank_account_name = 'BA-loan'
        db.session.commit()

        stats = sync_engine.push_pending(erp)
        self.assertEqual(stats['posted'], 6)
        self.assertEqual(stats['unpostable'], 0)

    def test_disabling_sync_parks_a_backlog_and_re_enabling_releases_it(self):
        account = self._account('acct-1')
        self._txns('acct-1', 4)
        account.sync_enabled = False
        db.session.commit()
        erp = FakeERPClient()
        self.assertEqual(sync_engine.push_pending(erp)['posted'], 0)
        self.assertEqual(sync_engine.unpostable_pending_count(), 4)

        account.sync_enabled = True
        db.session.commit()
        self.assertEqual(sync_engine.push_pending(erp)['posted'], 4)

    def test_an_already_posted_row_is_not_counted_as_waiting(self):
        self._account('acct-1')
        self._txns('acct-1', 3)
        sync_engine.push_pending(FakeERPClient())
        self.assertEqual(sync_engine.unpostable_pending_count(), 0)

    def test_a_removed_row_is_not_counted_as_waiting(self):
        """Plaid took it back; it is not waiting on anything."""
        self._mortgage()
        self._txns('acct-loan', 2)
        BankTransaction.query.first().removed = True
        db.session.commit()
        self.assertEqual(sync_engine.unpostable_pending_count(), 1)


# ── visibility ──────────────────────────────────────────────────────────────

class VisibilityTests(UnpostableBase):
    def test_the_count_is_grouped_per_account(self):
        self._mortgage('acct-loan')
        self._txns('acct-loan', 7)
        self._account('acct-student', mapped=False, type_='loan',
                      subtype='student')
        self._txns('acct-student', 3)
        self._account('acct-ok')
        self._txns('acct-ok', 5)

        by_account = sync_engine.unpostable_by_account()
        self.assertEqual(by_account.get('acct-loan'), 7)
        self.assertEqual(by_account.get('acct-student'), 3)
        self.assertNotIn('acct-ok', by_account)

    def test_the_push_log_records_the_count(self):
        from app.models import PlaidSyncLog
        self._account('acct-ok')
        self._txns('acct-ok', 1)
        self._mortgage()
        self._txns('acct-loan', 9)
        sync_engine.push_pending(FakeERPClient(), 'item-abc')
        row = (PlaidSyncLog.query
               .filter_by(direction='erpnext_push')
               .order_by(PlaidSyncLog.id.desc()).first())
        self.assertIn('unpostable=9', row.error_message or '')

    def test_the_accounts_page_says_how_many_are_waiting(self):
        """'Where did my mortgage payments go?' deserves an answer on the page,
        not silence."""
        self._mortgage()
        self._txns('acct-loan', 12)
        body = self.client.get('/admin/accounts').data.decode()
        self.assertIn('12 txns waiting', body)

    def test_a_single_waiting_transaction_reads_naturally(self):
        self._mortgage()
        self._txns('acct-loan', 1)
        body = self.client.get('/admin/accounts').data.decode()
        self.assertIn('1 txn waiting', body)

    def test_a_healthy_account_shows_no_hint(self):
        self._account('acct-ok')
        self._txns('acct-ok', 3)
        sync_engine.push_pending(FakeERPClient())
        body = self.client.get('/admin/accounts').data.decode()
        self.assertNotIn('waiting', body)


# ── regressions ─────────────────────────────────────────────────────────────

class RegressionTests(UnpostableBase):
    def test_a_normal_push_is_unaffected(self):
        self._account('acct-1')
        self._account('acct-2')
        self._txns('acct-1', 3)
        self._txns('acct-2', 2)
        stats = sync_engine.push_pending(FakeERPClient())
        self.assertEqual(stats['posted'], 5)
        self.assertEqual(stats['failed'], 0)
        self.assertEqual(stats['unpostable'], 0)

    def test_a_removed_row_on_a_mapped_account_still_cancels(self):
        self._account('acct-1')
        self._txns('acct-1', 1)
        erp = FakeERPClient()
        sync_engine.push_pending(erp)
        row = BankTransaction.query.one()
        row.removed = True
        row.posted_at = None
        db.session.commit()
        self.assertEqual(sync_engine.push_pending(erp)['cancelled'], 1)

    def test_no_erpnext_client_is_still_a_no_op(self):
        self._account('acct-1')
        self._txns('acct-1', 2)
        self.assertEqual(sync_engine.push_pending(None)['posted'], 0)

    def test_repush_of_a_single_unmapped_row_still_reports_why(self):
        """The manual per-row path keeps its own explanation — it is the one
        place an operator asks about a specific transaction."""
        self._mortgage()
        self._txns('acct-loan', 1)
        row = BankTransaction.query.one()
        ok, message = sync_engine.retry_row(row.id)
        self.assertFalse(ok)
        self.assertIn('not mapped', message)


if __name__ == '__main__':
    unittest.main()
