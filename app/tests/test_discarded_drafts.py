# SPDX-License-Identifier: MIT
"""Discarded drafts (erpnext_mcp v0.334.0), from Bank Bridge's side (v1.0.11).

  * draft health leaves a discarded draft out of the queue and counts it apart —
    and on an ERPNext without the column counts every draft, as before;
  * a discarded draft still occupies its identity, so it is not drafted again;
  * a discarded draft that was PURGED still occupies it through its Discarded
    Draft Log row, unless that row's Allow Recreate is ticked;
  * every difficulty with the log is Fail Safe: create.

    cd app
    python3 -m unittest tests.test_discarded_drafts -v
"""
import os
import unittest

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import draft_health, je_dedup  # noqa: E402

from tests.fakes import FakeERPClient  # noqa: E402
from tests.test_draft_health import COMPANY, DraftHealthBase  # noqa: E402


class DraftHealth(DraftHealthBase):
    def test_a_discarded_draft_is_counted_apart(self):
        self._draft('ACC-JV-0001', 'Cash: sweep', 100.0)
        self._draft('ACC-JV-0002', 'Cash: sweep', 40.0)
        self.client.created['Journal Entry']['ACC-JV-0002']['discarded'] = 1
        snap = draft_health.snapshot(self.client, COMPANY)
        self.assertEqual((snap['draft_count'], snap['total_amount']), (1, 100.0))
        self.assertEqual((snap['discarded_count'], snap['discarded_amount']), (1, 40.0))
        self.assertTrue(snap['discard_aware'])

    def test_an_erpnext_without_the_column_counts_every_draft(self):
        self.client.no_discard_field = True
        self._draft('ACC-JV-0001', 'Cash: sweep', 100.0)
        self._draft('ACC-JV-0002', 'Cash: sweep', 40.0)
        snap = draft_health.snapshot(self.client, COMPANY)
        self.assertEqual(snap['draft_count'], 2)
        self.assertFalse(snap['discard_aware'])
        self.assertIsNone(snap['discarded_count'])


class Dedup(unittest.TestCase):
    def setUp(self):
        self.client = FakeERPClient()

    def _je(self, name, remark='', bt='', discarded=0):
        accounts = [{'account': 'Cash - EC', 'reference_type': 'Bank Transaction',
                     'reference_name': bt}] if bt else []
        self.client.created['Journal Entry'][name] = {
            'company': COMPANY, 'user_remark': remark, 'accounts': accounts,
            'discarded': discarded}

    def _log(self, name, *, document='ACC-JV-0009', remark='', bts='', allow=0):
        self.client.created['Discarded Draft Log'][name] = {
            'document_name': document, 'company': COMPANY, 'amount': 12.5,
            'posting_date': '2026-03-01', 'user_remark': remark,
            'bank_transactions': bts, 'allow_recreate': allow}

    def test_a_discarded_draft_still_occupies_its_marker(self):
        self._je('ACC-JV-0001', remark='Cash [BB:inv:x1]', discarded=1)
        found = je_dedup.find_by_marker(self.client, 'inv', 'x1', company=COMPANY)
        self.assertEqual(found['journal_entry'], 'ACC-JV-0001')

    def test_a_purged_discard_still_occupies_its_marker_and_its_bank_transaction(self):
        self._log('DDL-1', remark='Cash [BB:inv:x1]', bts='ACC-BTN-0001, ACC-BTN-0002')
        by_marker = je_dedup.find_by_marker(self.client, 'inv', 'x1', company=COMPANY)
        by_bt = je_dedup.find_by_bank_transaction(self.client, 'ACC-BTN-0002', company=COMPANY)
        self.assertEqual((by_marker['purge_log'], by_bt['purge_log']), ('DDL-1', 'DDL-1'))
        self.assertEqual(by_bt['journal_entry'], 'ACC-JV-0009')
        self.assertIn('Allow Recreate', je_dedup.skip_reason('bank', 'ACC-BTN-0002', by_bt))

    def test_allow_recreate_releases_it(self):
        self._log('DDL-1', remark='Cash [BB:inv:x1]', allow=1)
        self.assertIsNone(je_dedup.find_by_marker(self.client, 'inv', 'x1', company=COMPANY))

    def test_a_longer_name_containing_this_one_is_not_a_match(self):
        self._log('DDL-1', bts='ACC-BTN-00012')
        self.assertIsNone(je_dedup.find_by_bank_transaction(self.client, 'ACC-BTN-0001', company=COMPANY))

    def test_an_erpnext_without_the_log_creates(self):
        client = FakeERPClient(missing_doctypes=['Discarded Draft Log'])
        self.assertIsNone(je_dedup.find_by_marker(client, 'inv', 'x1', company=COMPANY))
