# SPDX-License-Identifier: MIT
"""Re-link twin visibility in user-facing aggregations (v0.5.8).

A re-link mirrors the same purchase onto both account_ids of a supersede chain
(same date/amount/name, different plaid_transaction_id). The anchor engine
already collapses that pair; v0.5.8 gives the same collapse to UI aggregations
(the rules-editor merchant counter Tim saw showing "2 transactions" for one
purchase, and the dashboard total), by excluding rows on the RETIRED account.

Synthetic merchants (TESTMART / TESTFUEL) and masks only.
"""
from datetime import date

from app import account_visibility as av, db
from app.models import BankTransaction, PlaidAccount

from tests.test_statements import StatementsBase


class RelinkVisibilityTest(StatementsBase):
    def setUp(self):
        super().setUp()
        # 'old' was re-linked → 'new' (old is the retired/superseded row).
        self.old = PlaidAccount(account_id='old', item_id=self.item.item_id,
                                name='CHECKING (old link)', mask='3158',
                                type='depository', subtype='checking',
                                superseded_by_account_id='new')
        self.new = PlaidAccount(account_id='new', item_id=self.item.item_id,
                                name='CHECKING', mask='3158',
                                type='depository', subtype='checking')
        db.session.add_all([self.old, self.new])
        db.session.commit()

    def _txn(self, tid, account_id, merchant, amount=9289.04):
        db.session.add(BankTransaction(
            plaid_transaction_id=tid, account_id=account_id, amount=amount,
            date=date(2026, 6, 4), name=merchant, merchant_name=merchant))
        db.session.commit()

    def test_visible_query_excludes_superseded_account_rows(self):
        self._txn('a', 'old', 'TESTMART')
        self._txn('b', 'new', 'TESTMART')      # the twin on the active account
        self.assertEqual(BankTransaction.query.count(), 2)          # both exist
        self.assertEqual(av.visible_bank_transactions_query().count(), 1)
        survivor = av.visible_bank_transactions_query().one()
        self.assertEqual(survivor.account_id, 'new')

    def test_known_merchants_counts_a_twin_once(self):
        self._txn('a', 'old', 'TESTMART')
        self._txn('b', 'new', 'TESTMART')
        merchants = {m['name']: m for m in BankTransaction.known_merchants()}
        self.assertEqual(merchants['TESTMART']['count'], 1)

    def test_non_relinked_repeat_still_counts_twice(self):
        # Two REAL purchases on the active account — not a twin — must both count.
        self._txn('a', 'new', 'TESTFUEL')
        self._txn('b', 'new', 'TESTFUEL')
        merchants = {m['name']: m for m in BankTransaction.known_merchants()}
        self.assertEqual(merchants['TESTFUEL']['count'], 2)

    def test_known_categories_excludes_superseded(self):
        db.session.add(BankTransaction(
            plaid_transaction_id='a', account_id='old', amount=1.0,
            date=date(2026, 6, 4), name='X', category='GENERAL'))
        db.session.add(BankTransaction(
            plaid_transaction_id='b', account_id='new', amount=1.0,
            date=date(2026, 6, 4), name='X', category='GENERAL'))
        db.session.commit()
        cats = {c['path']: c['count'] for c in BankTransaction.known_categories()}
        self.assertEqual(cats['GENERAL'], 1)

    def test_no_supersession_leaves_counts_untouched(self):
        # With nothing retired, the filter is a no-op: every row still counts.
        self.old.superseded_by_account_id = None
        db.session.commit()
        self._txn('a', 'old', 'TESTMART')
        self._txn('b', 'new', 'TESTMART')
        self.assertEqual(av.visible_bank_transactions_query().count(), 2)
        merchants = {m['name']: m for m in BankTransaction.known_merchants()}
        self.assertEqual(merchants['TESTMART']['count'], 2)
