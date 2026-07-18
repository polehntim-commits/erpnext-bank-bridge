# SPDX-License-Identifier: MIT
"""v0.4.0.5 · Generated-JE Approve / Reject / Reverse / Retry workflow.

Root cause these cover: before v0.4.0.5, Approve submitted a bare
``{doctype, name}`` stub to ``frappe.client.submit`` (which submits the object
it is handed, not the stored record), so ERPNext rejected it, the JE stayed
Draft and the local row stayed ``pending_review`` — a silent no-op. The fix
fetches the real document before submitting and surfaces every failure.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import categorization  # noqa: E402
from app.erpnext_client import ERPNextError  # noqa: E402
from app.models import AuditEvent, GeneratedJournalEntry  # noqa: E402
from tests.fakes import FakeERPClient  # noqa: E402


class _SubmitFails(FakeERPClient):
    """An ERPNext whose Journal Entry submit always fails — models ERPNext
    refusing the submit (e.g. an out-of-band change, a validation error)."""
    def call_method(self, method, params=None, http_method='GET', json_body=None):
        if method == 'frappe.client.submit':
            raise ERPNextError('ValidationError: Total Debit must equal Total Credit')
        return super().call_method(method, params=params,
                                   http_method=http_method, json_body=json_body)


class Base(unittest.TestCase):
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

    # ── helpers ──────────────────────────────────────────────────
    def _entry(self, state='pending_review', je='ACC-JV-0001', tid='t1'):
        g = GeneratedJournalEntry(
            plaid_transaction_id=tid, rule_id=1, rule_name='Fuel',
            erpnext_journal_entry_name=je, state=state, amount=42.5,
            merchant_name='Chevron', description='Fuel purchase')
        db.session.add(g)
        db.session.commit()
        return g

    def _erp_with(self, *je_names, cls=FakeERPClient):
        """A fake ERPNext that already holds the given Journal Entries as Drafts,
        so ``_submit_je``'s get_doc + submit both find a real record."""
        erp = cls()
        for name in je_names:
            erp.docs[name] = {'doctype': 'Journal Entry', 'docstatus': 0,
                              'company': 'Bank Bridge Test Company',
                              'accounts': [{'account': 'Fuel - BBT', 'debit': 42.5},
                                           {'account': 'Bank - BBT', 'credit': 42.5}]}
        return erp

    def _count(self, event_type):
        return AuditEvent.query.filter_by(event_type=event_type).count()

    def _patch_erp(self, erp):
        return patch('app.sync_engine.get_erp_client_or_none', return_value=erp)

    def _post(self, path, **kw):
        follow = kw.pop('follow_redirects', True)
        return self.client.post(path, follow_redirects=follow, **kw)


class TestApprove(Base):
    def test_approve_marks_state_and_submits(self):
        g = self._entry()
        erp = self._erp_with('ACC-JV-0001')
        with self._patch_erp(erp):
            self._post('/admin/generated_entries/approve', data={'id': str(g.id)})
        db.session.refresh(g)
        self.assertEqual(g.state, 'approved')
        # The real document was actually submitted in ERPNext (Draft → Submitted).
        self.assertIn('ACC-JV-0001', erp.submitted)

    def test_approve_records_audit_event(self):
        g = self._entry()
        erp = self._erp_with('ACC-JV-0001')
        with self._patch_erp(erp):
            self._post('/admin/generated_entries/approve', data={'id': str(g.id)})
        self.assertEqual(self._count('journal_entry_approved'), 1)
        self.assertEqual(self._count('journal_entry_submitted_to_erpnext'), 1)

    def test_approve_json_refreshes_row_without_reload(self):
        # A fetch/XHR caller gets JSON with the new state so the JS can refresh
        # just that one row — no full-page redirect.
        g = self._entry()
        erp = self._erp_with('ACC-JV-0001')
        with self._patch_erp(erp):
            r = self.client.post('/admin/generated_entries/approve',
                                 data={'id': str(g.id)},
                                 headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['id'], g.id)
        self.assertEqual(body['state'], 'approved')

    def test_erpnext_submit_failure_surfaces_error(self):
        # ERPNext refuses the submit → the reason is surfaced (JSON 502), the
        # row stays pending — never a silent success.
        g = self._entry()
        erp = self._erp_with('ACC-JV-0001', cls=_SubmitFails)
        with self._patch_erp(erp):
            r = self.client.post('/admin/generated_entries/approve',
                                 data={'id': str(g.id)},
                                 headers={'X-Requested-With': 'fetch'})
        db.session.refresh(g)
        self.assertEqual(g.state, 'pending_review')
        self.assertEqual(r.status_code, 502)
        self.assertIn('refused the submit', r.get_json()['message'])
        self.assertEqual(self._count('journal_entry_approved'), 0)

    def test_approve_idempotent_no_double_submit(self):
        g = self._entry()
        erp = self._erp_with('ACC-JV-0001')
        with self._patch_erp(erp):
            self._post('/admin/generated_entries/approve', data={'id': str(g.id)})
            # Second approve of an already-approved row is a no-op, no re-submit.
            r = self.client.post('/admin/generated_entries/approve',
                                 data={'id': str(g.id)},
                                 headers={'X-Requested-With': 'fetch'})
        submits = [c for c in erp.calls if c[1] == 'frappe.client.submit']
        self.assertEqual(len(submits), 1)
        self.assertEqual(r.get_json()['state'], 'approved')
        self.assertTrue(r.get_json()['ok'])

    def test_cannot_approve_rejected_entry(self):
        # State-machine guard: a rejected entry cannot be approved (409).
        g = self._entry(state='rejected')
        erp = self._erp_with('ACC-JV-0001')
        with self._patch_erp(erp):
            r = self.client.post('/admin/generated_entries/approve',
                                 data={'id': str(g.id)},
                                 headers={'X-Requested-With': 'fetch'})
        db.session.refresh(g)
        self.assertEqual(g.state, 'rejected')
        self.assertEqual(r.status_code, 409)
        self.assertNotIn('ACC-JV-0001', erp.submitted)


class TestReject(Base):
    def test_reject_marks_state(self):
        g = self._entry()
        # No ERPNext needed for a never-submitted Draft — just abandoned.
        self._post('/admin/generated_entries/reject', data={'id': str(g.id)})
        db.session.refresh(g)
        self.assertEqual(g.state, 'rejected')

    def test_reject_records_audit_event(self):
        g = self._entry()
        self._post('/admin/generated_entries/reject', data={'id': str(g.id)})
        self.assertEqual(self._count('journal_entry_rejected'), 1)

    def test_reject_submitted_je_cancels_and_audits(self):
        # An already-approved (submitted) JE must be CANCELLED in ERPNext.
        g = self._entry(state='approved')
        erp = self._erp_with('ACC-JV-0001')
        with self._patch_erp(erp):
            self._post('/admin/generated_entries/reject', data={'id': str(g.id)})
        db.session.refresh(g)
        self.assertEqual(g.state, 'rejected')
        self.assertIn('ACC-JV-0001', erp.cancelled)
        self.assertEqual(self._count('journal_entry_rejected'), 1)


class TestBulk(Base):
    def test_bulk_approve_partial_success(self):
        ok1 = self._entry(je='ACC-JV-0001', tid='t1')
        ok2 = self._entry(je='ACC-JV-0002', tid='t2')
        # A pending row with no ERPNext JE cannot be submitted → it fails.
        bad = self._entry(je=None, tid='t3')
        erp = self._erp_with('ACC-JV-0001', 'ACC-JV-0002')
        with self._patch_erp(erp):
            r = self.client.post('/admin/generated_entries/bulk',
                                 data={'action': 'approve'},
                                 headers={'X-Requested-With': 'fetch'})
        body = r.get_json()
        self.assertEqual(body['done'], 2)
        self.assertEqual(body['failed'], 1)
        self.assertFalse(body['ok'])
        self.assertEqual(body['failures'][0]['id'], bad.id)
        # The two good rows still committed despite the third failing.
        for g in (ok1, ok2):
            db.session.refresh(g)
            self.assertEqual(g.state, 'approved')
        db.session.refresh(bad)
        self.assertEqual(bad.state, 'pending_review')

    def test_bulk_approve_idempotent_on_already_approved(self):
        g = self._entry(state='approved', je='ACC-JV-0001')
        erp = self._erp_with('ACC-JV-0001')
        with self._patch_erp(erp):
            # Bulk approve targeting the already-approved row → no-op success,
            # and never re-submits it to ERPNext.
            r = self.client.post('/admin/generated_entries/bulk',
                                 data={'action': 'approve', 'ids': str(g.id)},
                                 headers={'X-Requested-With': 'fetch'})
        self.assertTrue(r.get_json()['ok'])
        self.assertEqual([c for c in erp.calls if c[1] == 'frappe.client.submit'], [])


class TestReverseAndRetryGuards(Base):
    def test_reverse_only_from_approved(self):
        g = self._entry(state='pending_review')
        erp = self._erp_with('ACC-JV-0001')
        with self._patch_erp(erp):
            r = self.client.post('/admin/generated_entries/reverse',
                                 data={'id': str(g.id)},
                                 headers={'X-Requested-With': 'fetch'})
        self.assertEqual(r.status_code, 409)
        db.session.refresh(g)
        self.assertEqual(g.state, 'pending_review')


class TestPageWiring(Base):
    def test_js_click_handler_posts_to_endpoint(self):
        # The page ships the JS that intercepts a row action and POSTs it to the
        # approve endpoint tagged as a fetch call (so the server returns JSON).
        self._entry()
        html = self.client.get('/admin/generated_entries').data.decode()
        self.assertIn("class=\"je-action\"", html)
        self.assertIn("/admin/generated_entries/approve", html)
        self.assertIn("'X-Requested-With': 'fetch'", html)
        self.assertIn('fetch(form.action', html)

    def test_bulk_selection_controls_present(self):
        self._entry()
        html = self.client.get('/admin/generated_entries').data.decode()
        self.assertIn('id="je-bulk"', html)
        self.assertIn('name="action" value="approve"', html)
        self.assertIn('id="je-check-all"', html)


if __name__ == '__main__':
    unittest.main()
