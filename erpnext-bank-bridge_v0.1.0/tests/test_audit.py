# SPDX-License-Identifier: MIT
"""Append-only audit trail (v0.3.0).

  * every state-changing op writes an AuditEvent (rule create/edit/delete/toggle,
    supplier auto-create, JE generate/approve/reject, sync run, rules rerun)
  * the event count only grows — no soft-delete of audit rows
  * rule supersede-vs-delete preserves history + links versions
  * CSV export includes all fields
  * the audit UI is filterable down to an individual subject

    cd erpnext-bank-bridge_v0.1.0
    python3 -m unittest discover -s tests -v
"""
import csv
import io
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import audit, categorization, erpnext_bank, sync_engine  # noqa: E402
from app.models import (AuditEvent, BankTransaction, CategorizationRule,  # noqa: E402
                        GeneratedJournalEntry, PlaidAccount, PlaidItem, Supplier)

from tests.fakes import FakePlaidClient, FakeERPClient, page, txn  # noqa: E402

ACC = 'acct-wf-checking'


class Base(unittest.TestCase):
    EXTRA_CONFIG = {}

    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        cfg = {'TESTING': True,
               'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
               'DATA_DIR': self._datadir, 'FERNET_KEY': '',
               'SCHEDULER_ENABLED': False}
        cfg.update(self.EXTRA_CONFIG)
        self.app = create_app(cfg)
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        audit.set_context('system', None)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _count(self, event_type=None):
        q = AuditEvent.query
        if event_type:
            q = q.filter_by(event_type=event_type)
        return q.count()

    def _item(self):
        it = PlaidItem(item_id='item-abc',
                       access_token_encrypted=crypto.encrypt('access-sandbox-abc'),
                       institution_id='ins_1', institution_name='WF', status='active')
        db.session.add(it)
        db.session.commit()
        return it

    def _account(self):
        a = PlaidAccount(account_id=ACC, item_id='item-abc', name='Chk',
                         mask='1234', type='depository', subtype='checking',
                         erpnext_bank_account_name='WF - Ops', sync_enabled=True)
        db.session.add(a)
        db.session.commit()
        return a

    def _plaid_accounts(self):
        return [{'account_id': ACC, 'name': 'Chk', 'official_name': '',
                 'mask': '1234', 'type': 'depository', 'subtype': 'checking',
                 'balance_available': 1.0, 'balance_current': 2.0,
                 'iso_currency_code': 'USD'}]

    def _row(self, tid='t1', amount=42.5, merchant='Chevron'):
        r = BankTransaction(plaid_transaction_id=tid, account_id=ACC,
                            amount=amount, merchant_name=merchant,
                            name='CHEVRON 01', category='GAS',
                            date=date(2026, 7, 10),
                            erpnext_bank_transaction_id='ACC-BTN-0001')
        db.session.add(r)
        db.session.commit()
        return r


# ── recorder basics ────────────────────────────────────────────────────

class TestRecorder(Base):
    def test_record_writes_row_with_context(self):
        audit.set_context('admin_ui', '10.0.0.5')
        audit.record('rule_created', subject_type='CategorizationRule',
                     subject_id=7, after={'name': 'x'})
        e = AuditEvent.query.one()
        self.assertEqual(e.event_type, 'rule_created')
        self.assertEqual(e.actor, 'admin_ui')
        self.assertEqual(e.source_ip, '10.0.0.5')
        self.assertEqual(e.subject_id, '7')
        self.assertEqual(e.to_dict()['payload_after'], {'name': 'x'})

    def test_all_event_types_defined(self):
        # 17 distinct event types implemented (15 from the brief + supplier_edited
        # + rules_rerun).
        self.assertEqual(len(set(audit.EVENT_TYPES)), len(audit.EVENT_TYPES))
        self.assertGreaterEqual(len(audit.EVENT_TYPES), 17)


# ── state-changing operations each write an event ──────────────────────

class TestStateChangesLogged(Base):
    def test_rule_create_edit_delete_logged(self):
        data = dict(name='Fuel', priority='10', active='1',
                    match_type='merchant_contains', match_value='Chevron',
                    debit_account='Fuel - EC', credit_account='Bank - EC',
                    party_type='', party_name='', description_template='')
        self.client.post('/admin/rules/save', data=data)
        self.assertEqual(self._count('rule_created'), 1)
        rid = CategorizationRule.query.first().id
        self.client.post('/admin/rules/save', data=dict(data, id=str(rid),
                                                        name='Fuel v2'))
        self.assertEqual(self._count('rule_updated'), 1)
        new_id = CategorizationRule.query.filter_by(name='Fuel v2').first().id
        self.client.post('/admin/rules/delete', data={'id': str(new_id)})
        self.assertEqual(self._count('rule_deleted'), 1)

    def test_supplier_auto_create_logged(self):
        erp = FakeERPClient()
        erpnext_bank.get_or_create_supplier(erp, 'CHEVRON', amount=10.0)
        self.assertEqual(self._count('supplier_auto_created'), 1)
        e = AuditEvent.query.filter_by(event_type='supplier_auto_created').first()
        self.assertEqual(e.subject_type, 'Supplier')
        self.assertIn('CHEVRON', e.notes)
        # Second sighting of same merchant does NOT re-log a create.
        erpnext_bank.get_or_create_supplier(erp, 'Chevron', amount=5.0)
        self.assertEqual(self._count('supplier_auto_created'), 1)

    def test_je_generate_logs_rule_matched_and_generated(self):
        db.session.add(CategorizationRule(
            name='Fuel', priority=10, active=True, archived=False,
            match_type='merchant_contains', match_value='Chevron',
            debit_account='Fuel - EC', credit_account='Bank - EC'))
        db.session.commit()
        erp = FakeERPClient()
        categorization.generate_journal_entry(erp, self._row())
        self.assertEqual(self._count('rule_matched'), 1)
        self.assertEqual(self._count('journal_entry_generated'), 1)

    def test_je_no_match_logs_rule_matched_only(self):
        db.session.add(CategorizationRule(
            name='Groceries', priority=10, active=True, archived=False,
            match_type='merchant_contains', match_value='Safeway',
            debit_account='X', credit_account='Y'))
        db.session.commit()
        categorization.generate_journal_entry(FakeERPClient(), self._row())
        self.assertEqual(self._count('rule_matched'), 1)
        self.assertEqual(self._count('journal_entry_generated'), 0)

    def test_je_failure_logged(self):
        db.session.add(CategorizationRule(
            name='Fuel', priority=10, active=True, archived=False,
            match_type='merchant_contains', match_value='Chevron',
            debit_account='Fuel - EC', credit_account='Bank - EC'))
        db.session.commit()
        categorization.generate_journal_entry(
            FakeERPClient(fail_je_create=True), self._row())
        self.assertEqual(self._count('journal_entry_failed'), 1)

    def test_je_approve_logged(self):
        g = GeneratedJournalEntry(plaid_transaction_id='t1', rule_name='Fuel',
                                  erpnext_journal_entry_name='ACC-JV-0001',
                                  state='pending_review', amount=42.5,
                                  merchant_name='Chevron')
        db.session.add(g)
        db.session.commit()
        # Approve without ERPNext configured fails to submit → no approve event.
        self.client.post('/admin/generated_entries/approve', data={'id': str(g.id)})
        self.assertEqual(self._count('journal_entry_approved'), 0)
        # Reject always logs.
        self.client.post('/admin/generated_entries/reject', data={'id': str(g.id)})
        self.assertEqual(self._count('journal_entry_rejected'), 1)

    def test_sync_run_and_txn_synced_logged(self):
        item = self._item()
        self._account()
        plaid = FakePlaidClient(accounts=self._plaid_accounts(),
                                pages=[page(added=[txn('t1', ACC, 12.0,
                                                       merchant_name='Blue Bottle')])])
        sync_engine.sync_all(plaid, FakeERPClient())
        self.assertEqual(self._count('sync_run_started'), 1)
        self.assertEqual(self._count('sync_run_completed'), 1)
        self.assertEqual(self._count('bank_transaction_synced'), 1)


# ── permanence: audit never shrinks ────────────────────────────────────

class TestPermanence(Base):
    def test_count_monotonic_across_delete(self):
        data = dict(name='Fuel', priority='10', active='1',
                    match_type='merchant_contains', match_value='Chevron',
                    debit_account='Fuel - EC', credit_account='Bank - EC',
                    party_type='', party_name='', description_template='')
        self.client.post('/admin/rules/save', data=data)
        after_create = AuditEvent.query.count()
        rid = CategorizationRule.query.first().id
        self.client.post('/admin/rules/delete', data={'id': str(rid)})
        after_delete = AuditEvent.query.count()
        # Deleting a rule ADDS an audit row; it never removes any.
        self.assertGreater(after_delete, after_create)


# ── supersede vs delete preserves history ──────────────────────────────

class TestRuleHistory(Base):
    def test_edit_preserves_old_version_and_links(self):
        r = CategorizationRule(name='Fuel', priority=10, active=True,
                               archived=False, match_type='merchant_contains',
                               match_value='Chevron', debit_account='A',
                               credit_account='B')
        db.session.add(r)
        db.session.commit()
        old_id = r.id
        self.client.post('/admin/rules/save', data=dict(
            id=str(old_id), name='Fuel v2', priority='5', active='1',
            match_type='merchant_exact', match_value='Chevron',
            debit_account='A', credit_account='B', party_type='',
            party_name='', description_template=''))
        old = db.session.get(CategorizationRule, old_id)
        self.assertTrue(old.archived)
        self.assertIsNotNone(old.superseded_by)
        # History reconstructable: old row still holds the ORIGINAL config.
        self.assertEqual(old.match_type, 'merchant_contains')
        new = db.session.get(CategorizationRule, old.superseded_by)
        self.assertEqual(new.match_type, 'merchant_exact')

    def test_engine_ignores_archived_rules(self):
        # An archived rule must not match, even though its fields would.
        db.session.add(CategorizationRule(
            name='old', priority=1, active=False, archived=True,
            match_type='merchant_contains', match_value='Chevron',
            debit_account='A', credit_account='B'))
        db.session.commit()
        self.assertIsNone(categorization.find_matching_rule(self._row()))


# ── rerun rules is explicit + logged, not automatic ────────────────────

class TestRerun(Base):
    def test_rerun_generates_and_logs(self):
        self._account()
        db.session.add(CategorizationRule(
            name='Fuel', priority=10, active=True, archived=False,
            match_type='merchant_contains', match_value='Chevron',
            debit_account='Fuel - EC', credit_account='Bank - EC'))
        # A posted transaction with no JE yet.
        r = self._row()
        r.posted_at = categorization._now()
        db.session.commit()
        with mock.patch('app.sync_engine.get_erp_client_or_none',
                        return_value=FakeERPClient()):
            self.client.post('/admin/transactions/rerun_rules')
        self.assertEqual(GeneratedJournalEntry.query.count(), 1)
        self.assertEqual(self._count('rules_rerun'), 1)


# ── audit UI: filterable, detail, CSV ──────────────────────────────────

class TestAuditUi(Base):
    def _seed(self):
        audit.record('rule_created', subject_type='CategorizationRule',
                     subject_id=1, after={'name': 'A'}, actor='admin_ui')
        audit.record('supplier_auto_created', subject_type='Supplier',
                     subject_id=99, notes='CHEVRON', actor='system')
        audit.record('rule_updated', subject_type='CategorizationRule',
                     subject_id=1, after={'name': 'A2'}, actor='admin_ui')

    def test_page_renders_and_filters_to_subject(self):
        self._seed()
        r = self.client.get('/admin/audit')
        self.assertEqual(r.status_code, 200)
        # Filter to a single subject → only that subject's events show as rows.
        # (Assert on the <code> cell, which is table-row-only, not the filter
        # dropdown's <option> list.)
        r = self.client.get('/admin/audit?subject_type=CategorizationRule&subject_id=1')
        body = r.get_data(as_text=True)
        self.assertIn('<code>rule_created</code>', body)
        self.assertIn('<code>rule_updated</code>', body)
        self.assertNotIn('<code>supplier_auto_created</code>', body)

    def test_filter_by_event_type_and_actor(self):
        self._seed()
        r = self.client.get('/admin/audit?event_type=supplier_auto_created')
        body = r.get_data(as_text=True)
        self.assertIn('<code>supplier_auto_created</code>', body)
        self.assertNotIn('<code>rule_created</code>', body)
        r = self.client.get('/admin/audit?actor=system')
        body = r.get_data(as_text=True)
        self.assertIn('<code>supplier_auto_created</code>', body)  # only system actor
        self.assertNotIn('<code>rule_created</code>', body)        # admin_ui actor

    def test_detail_view_shows_payload(self):
        self._seed()
        eid = AuditEvent.query.filter_by(event_type='rule_updated').first().id
        r = self.client.get(f'/admin/audit?id={eid}')
        body = r.get_data(as_text=True)
        self.assertIn('Audit event #', body)
        self.assertIn('A2', body)              # payload_after rendered

    def test_csv_export_all_fields(self):
        self._seed()
        r = self.client.get('/admin/audit?format=csv')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'text/csv')
        reader = csv.reader(io.StringIO(r.get_data(as_text=True)))
        rows = list(reader)
        self.assertEqual(rows[0], ['id', 'at', 'event_type', 'actor',
                                   'subject_type', 'subject_id', 'payload_before',
                                   'payload_after', 'notes', 'source_ip'])
        self.assertEqual(len(rows) - 1, AuditEvent.query.count())  # every event

    def test_csv_export_honors_filter(self):
        self._seed()
        r = self.client.get('/admin/audit?format=csv&subject_type=Supplier')
        rows = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
        self.assertEqual(len(rows) - 1, 1)     # only the supplier event


if __name__ == '__main__':
    unittest.main()
