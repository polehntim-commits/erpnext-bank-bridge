# SPDX-License-Identifier: MIT
"""The pending → posted → removed lifecycle (v1.0.4).

THE INCIDENT. Bank Bridge booked every Plaid PENDING charge the moment it saw
it (Bank Transaction + submitted rule JE), then booked the POSTED version —
which Plaid issues under a new transaction_id — as a second BT and a second
JE. The removal of the pending id was meant to cancel its BT, but ERPNext
refuses to cancel a BT a submitted JE references (LinkExistsError, HTTP 417)
and Bank Bridge read that 417 as "already cancelled".

The ERPNext double here (`StrictERP`) models the three ERPNext rules that made
that invisible, each verified against a live ERPNext 15 instance:

  * a submitted Bank Transaction cannot be cancelled while a submitted Journal
    Entry references it (LinkExistsError → 417);
  * after submit, only `reference_number` (and the reconciliation table) may
    change — date / description / amounts raise UpdateAfterSubmitError (417);
  * cancelling a JE removes it from any BT's `payment_entries`, after which
    the BT cancels cleanly.

What is covered, in the order the brief names it:

  * a pending→posted swap leaves ONE Bank Transaction and ONE Journal Entry —
    with pending held back (the default), with pending pushed as unmatched,
    and from the pre-v1.0.4 state where the pending row already has both
  * a removed transaction is cancelled — JE first, then BT — and one whose JE
    is reconciled elsewhere is flagged instead
  * a re-sync is idempotent
  * an amount change between pending and posted is flagged, never edited
  * the duplicate finder and the dry-run cleanup report correctly

Synthetic merchants and round amounts only.

    cd app
    python3 -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest
import unittest.mock
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import (create_app, crypto, db, bt_lifecycle, erpnext_bank,  # noqa: E402
                 erpnext_settings, mcp_settings, sync_engine)
from app.blueprints import mcp_server  # noqa: E402
from app.erpnext_client import ERPNextAPIError  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        GeneratedJournalEntry, PlaidAccount, PlaidItem)

from tests.fakes import FakeERPClient, FakePlaidClient, page, txn  # noqa: E402

ACC = 'acct-test-checking'
BA = 'Test Checking - Ops'
JE = 'Journal Entry'
BT = 'Bank Transaction'


def _err(status, exc_type, msg):
    return ERPNextAPIError(f'{exc_type}: {msg}', status_code=status,
                           response_body=json.dumps({'exc_type': exc_type}))


class StrictERP(FakeERPClient):
    """FakeERPClient plus the ERPNext rules the incident hinged on."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.payment_entries = {}     # bt name → [{payment_document, payment_entry}]
        self.je_meta = {}             # je name → extra header fields
        self.comments = []            # Comment docs created

    # ── state helpers ────────────────────────────────────────────────────
    def _ds(self, name):
        return 2 if name in self.cancelled else 1 if name in self.submitted else 0

    def je_docs(self):
        return self.created['Journal Entry']

    def live_jes(self):
        return [n for n in self.je_docs() if self._ds(n) in (0, 1)]

    def live_bts(self):
        return [n for n in self.docs if n.startswith('ACC-BTN')
                and self._ds(n) in (0, 1)]

    def jes_referencing(self, bt, submitted_only=True):
        out = []
        for n, d in self.je_docs().items():
            if self._ds(n) == 2 or (submitted_only and self._ds(n) != 1):
                continue
            refs = {ln.get('reference_name') for ln in d.get('accounts') or []
                    if ln.get('reference_type') == BT}
            if bt in refs or d.get('cheque_no') == bt:
                out.append(n)
        return out

    def stage_bt(self, ref, amount, day, desc='TEST', submitted=True):
        self._counter += 1
        name = f'ACC-BTN-{self._counter:04d}'
        self.docs[name] = {
            'date': day, 'bank_account': BA, 'description': desc,
            'deposit': round(-amount, 2) if amount < 0 else 0.0,
            'withdrawal': round(amount, 2) if amount > 0 else 0.0,
            'reference_number': ref, 'currency': 'USD'}
        self.by_ref[ref] = name
        if submitted:
            self.submitted.add(name)
        return name

    def stage_je(self, name, bt, amount, *, cheque_no=None, voucher_type=JE,
                 submitted=True, reference=True):
        line = {'reference_type': BT, 'reference_name': bt} if reference else {}
        self.je_docs()[name] = {
            'company': 'Test Co', 'voucher_type': voucher_type,
            'cheque_no': cheque_no, 'posting_date': '2026-09-28',
            'accounts': [
                {'account': 'Fuel - EC', 'debit_in_account_currency': amount,
                 **line},
                {'account': 'Checking - EC', 'credit_in_account_currency': amount,
                 **line}]}
        if submitted:
            self.submitted.add(name)
        return name

    # ── ERPNext surface ──────────────────────────────────────────────────
    def get_doc(self, doctype, name):
        if doctype == BT and name in self.docs:
            doc = super().get_doc(doctype, name)
            doc['payment_entries'] = [dict(p) for p in
                                      self.payment_entries.get(name, [])]
            return doc
        if doctype == JE and name in self.je_docs():
            self.calls.append(('get_doc', doctype, name))
            return {**self.je_docs()[name], **self.je_meta.get(name, {}),
                    'name': name, 'docstatus': self._ds(name)}
        return super().get_doc(doctype, name)

    def update_doc(self, doctype, name, doc):
        if doctype == BT and self._ds(name) == 2:
            raise _err(417, 'ValidationError', 'Cannot edit cancelled document')
        if doctype == BT and self._ds(name) == 1:
            illegal = set(doc) - {'reference_number', 'payment_entries',
                                  'is_duplicate', 'duplicate_of'}
            if illegal:
                self.calls.append(('update_doc', doctype, name, doc))
                raise _err(417, 'UpdateAfterSubmitError',
                           f'Not allowed to change {sorted(illegal)}')
        if doctype == BT and 'reference_number' in doc:
            self.by_ref = {k: v for k, v in self.by_ref.items() if v != name}
            self.by_ref[doc['reference_number']] = name
        return super().update_doc(doctype, name, doc)

    def call_method(self, method, params=None, http_method='GET',
                    json_body=None):
        body = json_body or {}
        if method == 'frappe.client.cancel':
            dt, name = body.get('doctype'), body.get('name')
            if dt == BT and self.jes_referencing(name):
                self.calls.append(('call_method', method, body))
                raise _err(417, 'LinkExistsError',
                           f'{name} is linked with {self.jes_referencing(name)}')
            if dt == JE:
                for pes in self.payment_entries.values():
                    pes[:] = [p for p in pes if p['payment_entry'] != name]
        if method == bt_lifecycle.RECONCILE_METHOD:
            self.calls.append(('call_method', method, body))
            bt = body['bank_transaction_name']
            v = json.loads(body['vouchers'])[0]
            pes = self.payment_entries.setdefault(bt, [])
            if pes:
                raise _err(417, 'ValidationError',
                           f'Bank Transaction {bt} is already fully reconciled')
            pes.append({'payment_document': v['payment_doctype'],
                        'payment_entry': v['payment_name']})
            return {'name': bt}
        return super().call_method(method, params=params,
                                   http_method=http_method, json_body=json_body)

    def create_doc(self, doctype, doc):
        if doctype == 'Comment':
            self.comments.append(dict(doc))
            return {'name': f'CMT-{len(self.comments)}'}
        return super().create_doc(doctype, doc)

    def list_docs(self, doctype, filters=None, fields=None,
                  limit_page_length=20, **kw):
        filters = filters or []
        if doctype == 'Custom Field':
            names = set()
            for f in filters:
                if f[0] == 'fieldname':
                    names = set(f[2]) if f[1] == 'in' else {f[2]}
            return [{'name': n, 'fieldname': d['fieldname']}
                    for n, d in self.created['Custom Field'].items()
                    if d.get('dt') == BT and d['fieldname'] in names]
        if doctype == JE and any(len(f) == 4 or f[0] == 'cheque_no'
                                 for f in filters):
            self.calls.append(('list_docs', doctype, filters))
            out = []
            for n, d in self.je_docs().items():
                if self._ds(n) == 2:
                    continue
                refs = {ln.get('reference_name') for ln in d.get('accounts') or []
                        if ln.get('reference_type') == BT}
                ok, hit = True, None
                for f in filters:
                    if len(f) == 4 and f[1] == 'reference_name':
                        want = f[3] if isinstance(f[3], list) else [f[3]]
                        hits = refs & set(want)
                        ok &= bool(hits)
                        hit = next(iter(hits), None)
                    elif f[0] == 'cheque_no':
                        want = f[2] if f[1] == 'in' else [f[2]]
                        ok &= d.get('cheque_no') in want
                if ok:
                    out.append({**d, 'name': n, 'docstatus': self._ds(n),
                                'total_debit': d['accounts'][0].get(
                                    'debit_in_account_currency'),
                                'bt': hit})
            return out
        if doctype == BT and any(f[0] in ('bank_account', 'Bank Transaction '
                                          'Payments') for f in filters):
            self.calls.append(('list_docs', doctype, filters))
            if filters[0][0] == 'Bank Transaction Payments':
                je = filters[0][3]
                return [{'name': b} for b, pes in self.payment_entries.items()
                        if any(p['payment_entry'] == je for p in pes)
                        and self._ds(b) < 2]
            return [{**d, 'name': n, 'docstatus': self._ds(n)}
                    for n, d in self.docs.items()
                    if n.startswith('ACC-BTN') and self._ds(n) < 2
                    and d.get('bank_account') == filters[0][2]]
        return super().list_docs(doctype, filters=filters, fields=fields,
                                 limit_page_length=limit_page_length, **kw)


COMPANY = 'Test Co'


class Base(unittest.TestCase):
    EXTRA_CONFIG = {'ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES': True,
                    'ERPNEXT_JOURNAL_ENTRY_AUTO_SUBMIT': True}

    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': tempfile.mkdtemp(), 'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False, **self.EXTRA_CONFIG})
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.item = PlaidItem(
            item_id='item-abc', status='active', institution_id='ins_1',
            institution_name='Test Bank',
            access_token_encrypted=crypto.encrypt('access-sandbox-abc'),
            # v1.0.9 · rules fire only inside their own Company, so the Item
            # names one and the rule below is scoped to it.
            owning_company=COMPANY)
        db.session.add(self.item)
        db.session.add(PlaidAccount(
            account_id=ACC, item_id='item-abc', name='Test Checking',
            mask='1234', type='depository', subtype='checking',
            erpnext_bank_account_name=BA, sync_enabled=True))
        db.session.add(CategorizationRule(
            name='fuel', priority=100, active=True,
            match_type='description_regex', match_value='TEST FUEL',
            debit_account='Fuel - EC', credit_account='Checking - EC',
            applies_to_company=COMPANY))
        db.session.commit()
        self.erp = StrictERP()
        self.plaid = FakePlaidClient(accounts=[{
            'account_id': ACC, 'name': 'Test Checking', 'official_name': '',
            'mask': '1234', 'type': 'depository', 'subtype': 'checking',
            'balance_available': 1.0, 'balance_current': 1.0,
            'iso_currency_code': 'USD'}])

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    # ── helpers ──────────────────────────────────────────────────────────
    def sync(self, *pages):
        self.plaid.pages = list(pages)
        return sync_engine.sync_item(self.item, self.plaid, self.erp)

    @staticmethod
    def pending(tid='p1', amount=50.0, day='2026-09-24'):
        return txn(tid, ACC, amount, name='TEST FUEL S CUPERTINO CA CARD0845',
                   date=day, pending=True)

    @staticmethod
    def posted(tid='q1', amount=50.0, day='2026-09-28', pending_id='p1'):
        t = txn(tid, ACC, amount, name='PURCHASE AUTHORIZED ON 09/24 TEST FUEL',
                date=day)
        t['pending_transaction_id'] = pending_id
        return t

    def row(self, tid):
        return BankTransaction.query.filter_by(plaid_transaction_id=tid).first()

    def stage_legacy_pending(self, amount=50.0):
        """The pre-v1.0.4 state: the pending row was pushed (BT) and its rule
        JE submitted, but never allocated — exactly what OML carries."""
        bt = self.erp.stage_bt('p1', amount, '2026-09-24',
                               desc='TEST FUEL S CUPERTINO CA CARD0845')
        je = self.erp.stage_je('ACC-JV-0001', bt, amount)
        db.session.add(BankTransaction(
            plaid_transaction_id='p1', account_id=ACC, amount=amount,
            name='TEST FUEL S CUPERTINO CA CARD0845', date=date(2026, 9, 24),
            pending=True, erpnext_bank_transaction_id=bt,
            posted_at=sync_engine._now()))
        db.session.add(GeneratedJournalEntry(
            plaid_transaction_id='p1', erpnext_journal_entry_name=je,
            state='approved', amount=amount, rule_name='fuel'))
        db.session.commit()
        return bt, je


class PendingToPostedTests(Base):
    def test_default_holds_pending_and_books_the_posted_once(self):
        self.sync(page(added=[self.pending()]))
        self.assertEqual([], self.erp.creates_of(BT), 'pending was pushed')
        self.assertEqual([], self.erp.creates_of(JE))
        self.assertIsNone(self.row('p1').posted_at)

        self.sync(page(added=[self.posted()], removed=['p1']))
        self.assertEqual(1, len(self.erp.live_bts()))
        self.assertEqual(1, len(self.erp.live_jes()))
        self.assertTrue(self.row('p1').removed)
        self.assertEqual('p1', self.row('q1').pending_transaction_id)
        bt = self.row('q1').erpnext_bank_transaction_id
        je = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='q1').one().erpnext_journal_entry_name
        self.assertEqual([{'payment_document': JE, 'payment_entry': je}],
                         self.erp.payment_entries[bt],
                         'rule JE not allocated to its BT')

    def test_pushed_pending_gets_no_je_and_the_posted_updates_that_bt(self):
        erpnext_settings.set_push_pending(True)
        self.sync(page(added=[self.pending()]))
        [bt] = self.erp.live_bts()
        self.assertEqual([], self.erp.creates_of(JE),
                         'a pending row must never get a Journal Entry')

        self.sync(page(added=[self.posted()], removed=['p1']))
        self.assertEqual([bt], self.erp.live_bts())
        self.assertEqual(1, len(self.erp.live_jes()))
        self.assertEqual(bt, self.row('q1').erpnext_bank_transaction_id)
        self.assertIsNone(self.row('p1').erpnext_bank_transaction_id)
        self.assertEqual('q1', self.erp.docs[bt]['reference_number'])

    def test_legacy_pending_bt_and_je_are_reused_not_duplicated(self):
        bt, je = self.stage_legacy_pending()
        self.sync(page(added=[self.posted()], removed=['p1']))
        self.assertEqual([bt], self.erp.live_bts())
        self.assertEqual([je], self.erp.live_jes())
        self.assertEqual([], self.erp.creates_of(BT))
        self.assertEqual([], self.erp.creates_of(JE))
        self.assertEqual('q1', self.erp.docs[bt]['reference_number'])
        self.assertIsNone(self.row('q1').review_reason)
        self.assertEqual(je, GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='q1').one().erpnext_journal_entry_name)
        self.assertEqual([{'payment_document': JE, 'payment_entry': je}],
                         self.erp.payment_entries[bt])
        self.assertNotIn(bt, self.erp.cancelled)

    def test_an_inherited_cancelled_je_is_replaced_not_reused(self):
        """OML's 10/3 cleanup cancelled the pending rows' JEs by hand. A posted
        twin arriving later must book a fresh JE, not inherit a dead one."""
        bt, je = self.stage_legacy_pending()
        self.erp.cancelled.add(je)
        self.sync(page(added=[self.posted()], removed=['p1']))
        self.assertEqual([bt], self.erp.live_bts())
        live = self.erp.live_jes()
        self.assertEqual(1, len(live))
        self.assertNotEqual(je, live[0])
        self.assertEqual(live[0], GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='q1').one().erpnext_journal_entry_name)

    def test_removed_page_before_added_page_still_links(self):
        bt, je = self.stage_legacy_pending()
        self.sync(page(removed=['p1'], has_more=True, next_cursor='c1'),
                  page(added=[self.posted()], next_cursor='c2'))
        self.assertEqual([bt], self.erp.live_bts())
        self.assertEqual([je], self.erp.live_jes())

    def test_resync_is_idempotent(self):
        self.stage_legacy_pending()
        swap = page(added=[self.posted()], removed=['p1'])
        self.sync(swap)
        calls = len(self.erp.calls)
        self.sync(page(added=[self.posted()], removed=['p1']))
        self.sync()
        self.assertEqual(1, len(self.erp.live_bts()))
        self.assertEqual(1, len(self.erp.live_jes()))
        self.assertEqual([], self.erp.creates_of(BT))
        self.assertEqual([], self.erp.creates_of(JE))
        self.assertEqual(1, GeneratedJournalEntry.query.count())
        writes = [c for c in self.erp.calls[calls:]
                  if c[0] in ('create_doc', 'update_doc')
                  or (c[0] == 'call_method'
                      and c[1] in ('frappe.client.cancel',
                                   'frappe.client.submit'))]
        self.assertEqual([], writes)

    def test_amount_change_is_flagged_and_nothing_is_edited(self):
        bt, je = self.stage_legacy_pending(amount=50.0)
        self.sync(page(added=[self.posted(amount=58.0)], removed=['p1']))
        q = self.row('q1')
        self.assertIn('58.00', q.review_reason)
        self.assertIn('50.00', q.review_reason)
        self.assertIn(je, q.review_reason)
        self.assertEqual([bt], self.erp.live_bts())
        self.assertEqual([je], self.erp.live_jes(), 'a second JE was booked')
        self.assertEqual(50.0, self.erp.je_docs()[je]['accounts'][0][
            'debit_in_account_currency'], 'the submitted JE was edited')
        self.assertFalse([c for c in self.erp.calls
                          if c[0] == 'update_doc' and c[1] == JE])
        self.assertNotIn(bt, self.erp.payment_entries,
                         'a JE that disagrees with its BT was allocated')
        self.assertEqual('q1', self.erp.docs[bt]['reference_number'])


class RemovedTests(Base):
    def _posted_and_booked(self):
        self.sync(page(added=[txn('t1', ACC, 30.0, name='TEST FUEL 01')]))
        r = self.row('t1')
        je = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='t1').one().erpnext_journal_entry_name
        return r.erpnext_bank_transaction_id, je

    def test_removed_cancels_the_rule_je_then_the_bt(self):
        bt, je = self._posted_and_booked()
        self.sync(page(removed=['t1']))
        self.assertIn(je, self.erp.cancelled)
        self.assertIn(bt, self.erp.cancelled)
        self.assertEqual('cancelled', GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='t1').one().state)
        self.assertIsNone(self.row('t1').review_reason)

    def test_removed_legacy_pending_with_no_posted_twin_is_cancelled(self):
        bt, je = self.stage_legacy_pending()
        self.sync(page(removed=['p1']))
        self.assertEqual([], self.erp.live_bts())
        self.assertEqual([], self.erp.live_jes())

    def test_reconciled_je_is_flagged_not_cancelled(self):
        bt, je = self._posted_and_booked()
        self.erp.payment_entries[bt] = [
            {'payment_document': 'Payment Entry', 'payment_entry': 'ACC-PAY-1'}]
        stats = self.sync(page(removed=['t1']))
        self.assertNotIn(je, self.erp.cancelled)
        self.assertNotIn(bt, self.erp.cancelled)
        self.assertIn('ACC-PAY-1', self.row('t1').review_reason)
        self.assertEqual(1, stats['push']['review'])

    def test_a_link_exists_refusal_is_not_read_as_cancelled(self):
        """The root cause, pinned: ERPNext's 417 LinkExistsError used to be
        swallowed as 'already cancelled'."""
        bt = self.erp.stage_bt('x1', 10.0, '2026-09-01')
        self.erp.stage_je('ACC-JV-0099', bt, 10.0)
        self.assertFalse(erpnext_bank.cancel_bank_transaction(self.erp, bt))
        self.assertNotIn(bt, self.erp.cancelled)


class DuplicateBase(Base):
    """OML's shape: a pending BT + rule JE, the posted BT + its own rule JE,
    and a 9/24-style 'Bank Entry' (cheque_no = BT name) on the posted BT."""

    def setUp(self):
        super().setUp()
        e = self.erp
        self.p_bt = e.stage_bt('p1', 50.0, '2026-09-24')
        self.q_bt = e.stage_bt('q1', 50.0, '2026-09-28')
        e.stage_je('ACC-JV-0001', self.p_bt, 50.0)
        e.stage_je('ACC-JV-0002', self.q_bt, 50.0)
        e.stage_je('ACC-JV-0003', self.q_bt, 50.0, cheque_no=self.q_bt,
                   voucher_type='Bank Entry', reference=False)
        e.payment_entries[self.q_bt] = [{'payment_document': JE,
                                         'payment_entry': 'ACC-JV-0003'}]
        db.session.add_all([
            BankTransaction(plaid_transaction_id='p1', account_id=ACC,
                            amount=50.0, date=date(2026, 9, 24), pending=True,
                            removed=True, erpnext_bank_transaction_id=self.p_bt,
                            posted_at=sync_engine._now()),
            BankTransaction(plaid_transaction_id='q1', account_id=ACC,
                            amount=50.0, date=date(2026, 9, 28),
                            erpnext_bank_transaction_id=self.q_bt,
                            posted_at=sync_engine._now()),
            GeneratedJournalEntry(plaid_transaction_id='p1', state='approved',
                                  erpnext_journal_entry_name='ACC-JV-0001'),
            GeneratedJournalEntry(plaid_transaction_id='q1', state='approved',
                                  erpnext_journal_entry_name='ACC-JV-0002')])
        db.session.commit()

    def tool(self, name, args):
        with self.app.test_request_context(), \
                unittest.mock.patch.object(mcp_server, '_erp_client_or_error',
                                           return_value=self.erp):
            res = mcp_server.dispatch_tool(name, args)
        return res, json.loads(res['content'][0]['text']) \
            if not res.get('isError') else res['content'][0]['text']



class DuplicateToolTests(DuplicateBase):
    def test_finder_reports_the_pair_and_recommends(self):
        res, rep = self.tool('find_duplicate_bank_transactions', {})
        self.assertFalse(res.get('isError'))
        self.assertEqual(1, rep['pair_count'])
        pr = rep['pairs'][0]
        self.assertEqual('amount_date', pr['match'])
        self.assertEqual('remove_pending', pr['recommendation'])
        self.assertEqual(self.p_bt, pr['pending_bank_transaction']['name'])
        self.assertEqual(self.q_bt, pr['posted_bank_transaction']['name'])
        self.assertEqual({(BT, self.q_bt), (JE, 'ACC-JV-0002')},
                         {(d['doctype'], d['name']) for d in pr['keep']})
        self.assertEqual({(BT, self.p_bt), (JE, 'ACC-JV-0001'),
                          (JE, 'ACC-JV-0003')},
                         {(d['doctype'], d['name']) for d in pr['remove']})
        bank_entry = [j for j in pr['posted_bank_transaction'][
            'journal_entries'] if j['journal_entry'] == 'ACC-JV-0003'][0]
        self.assertEqual('cheque_no', bank_entry['link'])
        self.assertEqual('Bank Entry', bank_entry['voucher_type'])
        self.assertTrue(bank_entry['allocated_to_bank_transaction'])
        self.assertEqual(2, rep['journal_entries_to_remove'])
        self.assertEqual(1, rep['bank_transactions_to_remove'])

    def test_finder_prefers_the_pending_link_when_known(self):
        self.row('q1').pending_transaction_id = 'p1'
        db.session.commit()
        _, rep = self.tool('find_duplicate_bank_transactions', {})
        self.assertEqual('pending_link', rep['pairs'][0]['match'])

    def test_finder_marks_an_amount_mismatch_for_review(self):
        self.erp.docs[self.q_bt]['withdrawal'] = 55.0
        self.row('q1').pending_transaction_id = 'p1'
        db.session.commit()
        _, rep = self.tool('find_duplicate_bank_transactions', {})
        self.assertEqual('review', rep['pairs'][0]['recommendation'])
        self.assertEqual([], rep['pairs'][0]['remove'])

    def test_finder_hands_over_a_cleanup_entry(self):
        _, rep = self.tool('find_duplicate_bank_transactions', {})
        self.assertEqual({'bank_transaction': self.p_bt,
                          'duplicate_of': self.q_bt},
                         rep['pairs'][0]['cleanup_entry'])


class FlagAndKeepTests(DuplicateBase):
    """v1.0.6 · a stale duplicate is FLAGGED, cancelled and KEPT — never
    deleted. OML's 50 are the shape staged here: the pending Bank Transaction's
    Journal Entry is already cancelled."""

    def setUp(self):
        super().setUp()
        self.erp.cancelled.add('ACC-JV-0001')     # Tim's earlier cleanup
        mcp_settings.save({'cleanup_duplicate_bank_transactions': True})
        self.items = [{'bank_transaction': self.p_bt,
                       'duplicate_of': self.q_bt}]

    def run_tool(self, **kw):
        return self.tool('cleanup_duplicate_bank_transactions',
                         {'items': self.items, **kw})

    def test_gated_off_by_default(self):
        mcp_settings.save({'cleanup_duplicate_bank_transactions': False})
        self.assertFalse(mcp_settings._DEFAULTS[
            'cleanup_duplicate_bank_transactions'])
        res, text = self.run_tool()
        self.assertTrue(res['isError'])
        self.assertIn('kill switch', text)

    def test_dry_run_is_the_default_and_changes_nothing(self):
        res, out = self.run_tool()
        self.assertFalse(res.get('isError'), out)
        self.assertTrue(out['dry_run'])
        self.assertEqual([self.p_bt],
                         [p['bank_transaction'] for p in out['would_flag']])
        self.assertEqual('submitted → flag → cancel',
                         out['would_flag'][0]['route'])
        self.assertFalse(out['fields_present'])
        self.assertEqual({'ACC-JV-0001'}, self.erp.cancelled)
        self.assertEqual({}, self.erp.created['Custom Field'])
        self.assertEqual([], self.erp.comments)

    def test_flags_cancels_comments_and_never_deletes(self):
        res, out = self.run_tool(dry_run=False, reason='OML Aug–Sep dedupe')
        self.assertFalse(res.get('isError'), out)
        self.assertEqual([], out['failed'], out)
        self.assertEqual([self.p_bt],
                         [p['bank_transaction'] for p in out['flagged']])
        self.assertEqual({'Bank Transaction-is_duplicate',
                          'Bank Transaction-duplicate_of'},
                         set(self.erp.created['Custom Field']))
        doc = self.erp.docs[self.p_bt]
        self.assertEqual(1, doc['is_duplicate'])
        self.assertEqual(self.q_bt, doc['duplicate_of'])
        self.assertIn(self.p_bt, self.erp.cancelled)
        self.assertNotIn(self.q_bt, self.erp.cancelled)
        self.assertEqual(set(), self.erp.deleted)
        self.assertEqual({'ACC-JV-0001'}, self.erp.cancelled - {self.p_bt},
                         'a Journal Entry was touched')
        [c] = self.erp.comments
        self.assertEqual(self.p_bt, c['reference_name'])
        self.assertIn(f'DUPLICATE of {self.q_bt}', c['content'])
        self.assertIn('OML Aug–Sep dedupe', c['content'])
        self.assertIn('UTC', c['content'])
        from app.models import AuditEvent
        self.assertEqual(1, AuditEvent.query.filter_by(
            event_type='bank_transaction_flagged_duplicate').count())

    def test_a_draft_is_prefixed_submitted_then_cancelled(self):
        self.erp.submitted.discard(self.p_bt)          # OML's are Pending
        self.erp.docs[self.p_bt]['description'] = 'TRANSFER APPLE CASH'
        _, out = self.run_tool(dry_run=False, reason='dedupe')
        self.assertEqual([], out['failed'], out)
        doc = self.erp.docs[self.p_bt]
        self.assertEqual(f'DUPLICATE of {self.q_bt} — TRANSFER APPLE CASH',
                         doc['description'])
        self.assertIn(self.p_bt, self.erp.submitted)
        self.assertIn(self.p_bt, self.erp.cancelled)
        self.assertEqual(set(), self.erp.deleted)

    def test_rerun_is_idempotent(self):
        self.run_tool(dry_run=False, reason='dedupe')
        _, out = self.run_tool(dry_run=False, reason='dedupe')
        self.assertEqual([], out['flagged'])
        self.assertEqual([self.p_bt], [r['bank_transaction']
                                       for r in out['already_flagged']])
        self.assertEqual(1, len(self.erp.comments))

    def test_refuses_a_stale_bt_whose_je_is_still_live(self):
        self.erp.cancelled.discard('ACC-JV-0001')
        _, out = self.run_tool(dry_run=False, reason='dedupe')
        self.assertEqual([], out['flagged'])
        self.assertIn('ACC-JV-0001', out['refused'][0]['reason'])
        self.assertNotIn(self.p_bt, self.erp.cancelled)

    def test_refuses_mismatches(self):
        cases = {
            'amounts differ': lambda: self.erp.docs[self.q_bt].update(
                withdrawal=51.0),
            'different Bank Accounts': lambda: self.erp.docs[self.q_bt].update(
                bank_account='Other - Ops'),
            'is cancelled': lambda: self.erp.cancelled.add(self.q_bt),
        }
        for needle, mutate in cases.items():
            with self.subTest(needle=needle):
                saved = (dict(self.erp.docs[self.q_bt]),
                         set(self.erp.cancelled))
                mutate()
                _, out = self.run_tool()
                self.assertIn(needle, out['refused'][0]['reason'])
                self.erp.docs[self.q_bt], self.erp.cancelled = saved
        self.items = [{'bank_transaction': self.p_bt,
                       'duplicate_of': self.p_bt}]
        _, out = self.run_tool()
        self.assertIn('itself', out['refused'][0]['reason'])

    def test_a_bare_stale_name_resolves_its_kept_twin(self):
        self.items = [self.p_bt]
        _, out = self.run_tool()
        self.assertEqual([(self.p_bt, self.q_bt)],
                         [(p['bank_transaction'], p['duplicate_of'])
                          for p in out['would_flag']])

    def test_an_unpairable_bare_name_is_refused_not_guessed(self):
        self.items = ['ACC-BTN-9999']
        _, out = self.run_tool()
        self.assertEqual([], out['would_flag'])
        self.assertIn('duplicate_of', out['refused'][0]['reason'])

    def test_an_approved_amount_difference_needs_the_explicit_flag(self):
        self.erp.docs[self.q_bt]['withdrawal'] = 58.0          # tip added
        _, out = self.run_tool()
        self.assertIn('amounts differ', out['refused'][0]['reason'])
        self.items[0]['allow_amount_difference'] = True
        _, out = self.run_tool()
        self.assertEqual([self.p_bt],
                         [p['bank_transaction'] for p in out['would_flag']])

    def test_no_twin_flags_without_duplicate_of_and_needs_a_reason(self):
        self.erp.submitted.discard(self.p_bt)
        self.erp.docs[self.p_bt]['description'] = 'AMAZON AUTH'
        self.items = [{'bank_transaction': self.p_bt, 'no_twin': True}]
        _, out = self.run_tool()
        self.assertIn('reason', out['refused'][0]['reason'])
        self.items[0]['reason'] = 'authorization reversed (A39/A40)'
        _, out = self.run_tool(dry_run=False, reason='dedupe')
        self.assertEqual([], out['failed'], out)
        doc = self.erp.docs[self.p_bt]
        self.assertEqual(1, doc['is_duplicate'])
        self.assertIsNone(doc['duplicate_of'])
        self.assertTrue(doc['description'].startswith(
            'DUPLICATE (stale pending, no posted twin) — AMAZON AUTH'))
        self.assertIn(self.p_bt, self.erp.cancelled)
        self.assertIn('no posted twin', self.erp.comments[0]['content'])
        _, again = self.run_tool(dry_run=False, reason='dedupe')
        self.assertEqual(1, len(again['already_flagged']))

    def test_reason_is_required_for_a_real_run(self):
        res, text = self.run_tool(dry_run=False)
        self.assertTrue(res['isError'])
        self.assertIn('reason', text)

    def test_a_live_local_row_on_the_stale_bt_is_repointed(self):
        self.row('p1').removed = False
        db.session.commit()
        _, out = self.run_tool(dry_run=False, reason='dedupe')
        self.assertEqual(['p1'], out['flagged'][0]['local_rows_repointed'])
        self.assertEqual(self.q_bt, self.row('p1').erpnext_bank_transaction_id)


class DraftRemovalTests(Base):
    def test_a_removed_draft_bt_is_submitted_then_cancelled_not_deleted(self):
        """OML's Bank Transactions are drafts (Pending). Frappe cancels only
        submitted documents, and deleting would lose the audit trail."""
        bt = self.erp.stage_bt('d1', 20.0, '2026-09-20', submitted=False)
        db.session.add(BankTransaction(
            plaid_transaction_id='d1', account_id=ACC, amount=20.0,
            date=date(2026, 9, 20), erpnext_bank_transaction_id=bt,
            posted_at=sync_engine._now()))
        db.session.commit()
        self.sync(page(removed=['d1']))
        self.assertIn(bt, self.erp.submitted)
        self.assertIn(bt, self.erp.cancelled)
        self.assertEqual(set(), self.erp.deleted)


class SettingsPageTests(Base):
    def test_default_is_hold_and_the_page_toggles_it(self):
        self.assertFalse(erpnext_settings.push_pending_enabled())
        client = self.app.test_client()
        page_ = client.get('/admin/erpnext_settings')
        self.assertEqual(200, page_.status_code)
        self.assertIn(b'Pending transactions', page_.data)
        self.assertIn(b'waiting for transactions to post', page_.data)
        resp = client.post('/admin/erpnext_settings/push_pending',
                           data={'enabled': '1'})
        self.assertEqual(302, resp.status_code)
        self.assertTrue(erpnext_settings.push_pending_enabled())
        self.assertIn(b'pushing pending transactions',
                      client.get('/admin/erpnext_settings').data)

    def test_the_env_var_seeds_the_default(self):
        self.app.config['ERPNEXT_PUSH_PENDING_TRANSACTIONS'] = True
        self.assertTrue(erpnext_settings.push_pending_enabled())

    def test_retry_refuses_a_held_pending_row(self):
        self.sync(page(added=[self.pending()]))
        ok, msg = sync_engine.retry_row(self.row('p1').id)
        self.assertFalse(ok)
        self.assertIn('pending', msg)
        self.assertEqual([], self.erp.creates_of(BT))


if __name__ == '__main__':
    unittest.main()
