# SPDX-License-Identifier: MIT
"""The pending → posted → removed lifecycle of one ERPNext Bank Transaction
(v1.0.4).

THE INCIDENT. Plaid reports a card charge twice over its life: first as a
PENDING authorization, then — days later — as a POSTED transaction under a NEW
transaction_id, whose `pending_transaction_id` points back at the first. The
same /transactions/sync response lists the pending id under `removed`.

Before v1.0.4 Bank Bridge booked the pending row the moment it saw it (Bank
Transaction + a submitted rule Journal Entry), then booked the posted row as a
brand-new transaction (a second BT + a second JE). The removal of the pending
row was meant to cancel its BT, but ERPNext refuses to cancel a Bank
Transaction that a submitted Journal Entry still references (LinkExistsError,
HTTP 417) — and `cancel_bank_transaction` read every 417 as "already
cancelled". So nothing was cancelled, nothing said so, and every card charge
was expensed twice.

WHAT THIS MODULE OWNS.

  * `link_posted_to_pending` (pull time) — a posted row whose
    pending_transaction_id names a row that was already pushed TAKES OVER that
    row's ERPNext Bank Transaction and its Journal Entry record, so the push
    updates one BT instead of creating a second, and the rules engine finds the
    existing JE instead of writing another.
  * `refresh_bank_transaction` (push time) — bring an existing BT in line with
    the row: a draft is rewritten; a submitted one gets its reference number
    (the only identity field ERPNext lets change after submit), or is cancelled
    and replaced when nothing links to it. An amount that changed under a live
    Journal Entry is FLAGGED, never edited — a submitted JE is the ledger.
  * `cancel_removed` (push time) — Plaid removed a transaction: cancel Bank
    Bridge's own unreconciled JE first (ERPNext then lets the BT go), then the
    BT. Anything reconciled, or not Bank Bridge's to touch, is flagged.
  * `allocate_je` — record a rule JE against its BT (the BT's
    `payment_entries`), the way ERPNext's Bank Reconciliation Tool does, so the
    BT reads Reconciled and no other job books it again.
  * `find_duplicates` / `cleanup_duplicates` — the after-the-fact repair for
    installs that already carry pending/posted duplicate pairs.

THE RULE THIS MODULE KEEPS. One Plaid transaction, one live Bank Transaction,
one live Journal Entry. When keeping that rule would mean editing or deleting
something already on the books, it stops and says so instead (`review_reason`).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from . import audit
from . import db
from . import erpnext_bank
from . import je_dedup
from .erpnext_client import ERPNextAPIError, ERPNextError
from .models import BankTransaction, GeneratedJournalEntry, PlaidAccount

log = logging.getLogger('bankbridge.bt_lifecycle')

BT_DT = 'Bank Transaction'
JE_DT = 'Journal Entry'
RECONCILE_METHOD = ('erpnext.accounts.doctype.bank_reconciliation_tool.'
                    'bank_reconciliation_tool.reconcile_vouchers')

# The GeneratedJournalEntry states that mean "Bank Bridge's rules engine wrote
# this entry and still stands behind it" — the only JEs a removal may cancel.
AUTO_JE_STATES = ('approved', 'pending_review')

# How far after a pending authorization its posted twin may be dated, for the
# duplicate finder's amount-and-date pairing (installs whose history predates
# pending_transaction_id). Card holds typically post in 1–5 business days.
PAIR_WINDOW_DAYS = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _docstatus(doc) -> int:
    try:
        return int((doc or {}).get('docstatus') or 0)
    except (TypeError, ValueError):
        return 0


def _money(v) -> float:
    try:
        return round(float(v or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _flag(row: BankTransaction, reason: str) -> None:
    """Set the row's review flag (keeping an earlier, different reason) and log
    it. The flag is the whole hand-off to a human, so it says what to do."""
    reason = (reason or '').strip()
    if not reason:
        return
    if row.review_reason and reason not in row.review_reason:
        row.review_reason = f'{row.review_reason}\n{reason}'[:4000]
    else:
        row.review_reason = reason[:4000]
    row.updated_at = _now()
    log.warning('[review] %s: %s', row.plaid_transaction_id, reason)


# ── pull side ───────────────────────────────────────────────────────────────

def link_posted_to_pending(row: BankTransaction) -> str:
    """Let a POSTED row take over what its pending predecessor already booked.

    Called from the pull for every non-pending row carrying a
    pending_transaction_id. Returns '' (nothing to link), 'linked' (the
    predecessor exists but holds nothing live in ERPNext) or 'superseded' (the
    predecessor's Bank Transaction and Journal Entry record now belong to this
    row). Local-only: the ERPNext side is brought in line by the push.

    Idempotent — a cursor replay finds the predecessor already emptied and
    answers 'linked'."""
    pid = (row.pending_transaction_id or '').strip()
    if row.pending or not pid or pid == row.plaid_transaction_id:
        return ''
    pred = BankTransaction.query.filter_by(plaid_transaction_id=pid).first()
    if pred is None:
        return ''
    if row.erpnext_bank_transaction_id:
        return 'linked'
    bt = pred.erpnext_bank_transaction_id
    # A predecessor whose removal was already pushed has had its BT cancelled
    # (and its JE with it) — there is nothing live left to inherit. One that
    # was FLAGGED instead still holds a live BT, so it is inherited too.
    settled_removal = (pred.removed and pred.posted_at is not None
                       and not pred.review_reason)
    if not bt or settled_removal:
        return 'linked'

    pending_amount = pred.amount
    row.erpnext_bank_transaction_id = bt
    pred.erpnext_bank_transaction_id = None
    pred.removed = True
    pred.posted_at = _now()       # nothing left to do in ERPNext for it
    pred.sync_error = None
    pred.updated_at = _now()

    je = None
    gje_pred = GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=pid).first()
    gje_row = GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=row.plaid_transaction_id).first()
    if gje_pred is not None and gje_row is None:
        # Re-key, so generate_journal_entry's one-JE-per-transaction guard
        # finds the existing entry under the posted id.
        gje_pred.plaid_transaction_id = row.plaid_transaction_id
        gje_pred.updated_at = _now()
        je = gje_pred.erpnext_journal_entry_name
    elif gje_row is not None:
        je = gje_row.erpnext_journal_entry_name

    if _money(pending_amount) != _money(row.amount):
        _flag(row, (
            f'Posted amount {row.amount:.2f} differs from the pending amount '
            f'{pending_amount:.2f} already booked on Bank Transaction {bt}'
            + (f' and Journal Entry {je}' if je else '')
            + '. Bank Bridge did not edit them: correct the Bank Transaction '
              'and Journal Entry in ERPNext (cancel and amend).'))
    audit.record('pending_transaction_superseded',
                 subject_type='BankTransaction',
                 subject_id=row.plaid_transaction_id,
                 before={'plaid_transaction_id': pid, 'amount': pending_amount},
                 after={'plaid_transaction_id': row.plaid_transaction_id,
                        'amount': row.amount, 'bank_transaction': bt,
                        'journal_entry': je,
                        'amount_changed': bool(row.review_reason)},
                 notes=f'posted {row.plaid_transaction_id} takes over {bt} '
                       f'from pending {pid}', commit=False)
    return 'superseded'


# ── push side: an existing Bank Transaction brought up to date ──────────────

def _set_reference(client, name: str, doc: dict, reference: str) -> None:
    """Point a Bank Transaction's reference_number at `reference`, and confirm
    ERPNext actually stored it — a 200 is not a write."""
    if (doc.get('reference_number') or '') == reference:
        return
    echoed = client.update_doc(BT_DT, name, {'reference_number': reference})
    got = (echoed or {}).get('reference_number') if isinstance(echoed, dict) \
        else None
    if got is None:
        got = (client.get_doc(BT_DT, name) or {}).get('reference_number')
    if got != reference:
        raise ERPNextError(f'Bank Transaction {name} did not take reference '
                           f'{reference!r} (ERPNext holds {got!r})')


def _has_live_links(client, name: str, doc: dict) -> bool:
    """Whether anything on the books hangs off this Bank Transaction — a
    reconciled voucher or a live Journal Entry referencing it. Fails SAFE
    toward "linked": the caller's alternative is a cancel."""
    if doc.get('payment_entries'):
        return True
    try:
        rows = client.list_docs(
            JE_DT, filters=[['Journal Entry Account', 'reference_type', '=',
                             BT_DT],
                            ['Journal Entry Account', 'reference_name', '=',
                             name],
                            ['docstatus', 'in', [0, 1]]],
            fields=['name'], limit_page_length=1)
    except (ERPNextAPIError, ERPNextError):
        return True
    return bool(rows)


def refresh_bank_transaction(client, row: BankTransaction,
                             bank_account: str) -> str:
    """Bring the row's existing ERPNext Bank Transaction in line with the row.

    Returns what happened: 'created' (the doc was gone or cancelled, so a fresh
    one was booked), 'updated' (draft rewritten, or only the reference moved),
    'replaced' (a Plaid modification of a submitted doc nothing links to:
    cancelled and re-created, as before v1.0.4), or 'reference_only'.

    'reference_only' is the submitted case ERPNext constrains: after submit
    only `reference_number` may change — date, description and amounts raise
    UpdateAfterSubmitError. A posted row TAKING OVER its pending twin's BT
    always lands here, never 'replaced': the same document carries the charge
    from authorization to posting (it keeps the pending date and description),
    so whatever an operator already did with it stays attached. An amount
    difference is flagged for review rather than edited."""
    name = row.erpnext_bank_transaction_id
    doc = client.get_doc(BT_DT, name) if name else None
    if not doc or _docstatus(doc) == 2:
        row.erpnext_bank_transaction_id = erpnext_bank.create_bank_transaction(
            client, row, bank_account)
        return 'created'
    want = erpnext_bank.build_doc(row, bank_account)
    if _docstatus(doc) == 0:
        client.update_doc(BT_DT, name, {
            k: want[k] for k in ('date', 'deposit', 'withdrawal',
                                 'description', 'reference_number')
            if k in want})
        try:
            erpnext_bank._submit(client, name)
        except ERPNextAPIError:
            log.warning('submit failed for %s; left as draft', name)
        return 'updated'

    amount_changed = (_money(doc.get('deposit')) != _money(want['deposit'])
                      or _money(doc.get('withdrawal'))
                      != _money(want['withdrawal']))
    material = (amount_changed
                or str(doc.get('date') or '')[:10] != (want.get('date') or '')
                or (doc.get('description') or '') != want['description'])
    if not material:
        _set_reference(client, name, doc, row.plaid_transaction_id)
        return 'updated'
    taking_over = (doc.get('reference_number') or '') not in (
        '', row.plaid_transaction_id)
    if not taking_over and not _has_live_links(client, name, doc):
        if erpnext_bank.cancel_bank_transaction(client, name):
            row.erpnext_bank_transaction_id = \
                erpnext_bank.create_bank_transaction(client, row, bank_account)
            return 'replaced'
    _set_reference(client, name, doc, row.plaid_transaction_id)
    if amount_changed:
        _flag(row, (
            f'Amount is now {row.amount:.2f} but Bank Transaction {name} '
            f'(submitted, with a Journal Entry or reconciliation attached) '
            f'still carries {_money(doc.get("withdrawal")) or -_money(doc.get("deposit")):.2f}. '
            'Bank Bridge did not edit it: cancel and amend it in ERPNext.'))
    return 'reference_only'


# ── push side: Plaid removed the transaction ────────────────────────────────

def _reconciled_elsewhere(client, je: str, jdoc: dict, bt: str) -> str:
    """Why `je` is NOT safe to cancel as part of removing `bt`, or ''.

    Safe means: the only reconciliation it carries is its allocation to its own
    Bank Transaction. Any failure to find out answers "not safe" — this guards a
    cancel."""
    try:
        btdoc = client.get_doc(BT_DT, bt) or {}
        others = [f"{p.get('payment_document')} {p.get('payment_entry')}"
                  for p in (btdoc.get('payment_entries') or [])
                  if not (p.get('payment_document') == JE_DT
                          and p.get('payment_entry') == je)]
        if others:
            return (f'Bank Transaction {bt} is reconciled against '
                    + ', '.join(others))
        own = any(p.get('payment_entry') == je
                  for p in (btdoc.get('payment_entries') or []))
        allocated = client.list_docs(
            BT_DT, filters=[['Bank Transaction Payments', 'payment_entry', '=',
                             je],
                            ['docstatus', '<', 2]],
            fields=['name'], limit_page_length=0) or []
        elsewhere = sorted({r.get('name') for r in allocated
                            if r.get('name') and r.get('name') != bt})
        if elsewhere:
            return (f'Journal Entry {je} is reconciled to other Bank '
                    f'Transaction(s) {", ".join(elsewhere)}')
        if jdoc.get('clearance_date') and not own:
            return (f'Journal Entry {je} was cleared on '
                    f'{jdoc.get("clearance_date")}')
    except (ERPNextAPIError, ERPNextError) as e:
        return f'could not confirm Journal Entry {je} is unreconciled ({e})'
    return ''


def cancel_removed(client, row: BankTransaction) -> str:
    """Plaid removed this transaction: take it off the books.

    Returns 'noop' (nothing was ever booked), 'cancelled' (the BT is cancelled
    — and Bank Bridge's own JE with it, when there was one), or 'review'
    (something on the books is not Bank Bridge's to cancel; flagged)."""
    bt = row.erpnext_bank_transaction_id
    if not bt:
        return 'noop'
    gje = GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=row.plaid_transaction_id).first()
    je = gje.erpnext_journal_entry_name if gje is not None else None
    leftover = ''
    if je:
        jdoc = client.get_doc(JE_DT, je) or {}
        ds = _docstatus(jdoc) if jdoc else 2
        is_auto = (gje.intercompany_pair_id is None
                   and (gje.state or '') in AUTO_JE_STATES)
        if ds == 1:
            why = '' if is_auto else (
                f'Journal Entry {je} is not an active Bank Bridge rule entry '
                f'(state {gje.state!r})')
            why = why or _reconciled_elsewhere(client, je, jdoc, bt)
            if why:
                _flag(row, (f'Plaid removed this transaction, but {why}. '
                            f'Bank Bridge left Journal Entry {je} and Bank '
                            f'Transaction {bt} alone: review and cancel them '
                            'in ERPNext if the charge really is gone.'))
                return 'review'
            client.call_method('frappe.client.cancel', http_method='POST',
                               json_body={'doctype': JE_DT, 'name': je})
            gje.state = 'cancelled'
            gje.error_message = ('Cancelled: Plaid removed the transaction '
                                 f'and Bank Transaction {bt} was cancelled.')
            gje.updated_at = _now()
            audit.record('journal_entry_cancelled_on_removal',
                         subject_type='GeneratedJournalEntry',
                         subject_id=gje.id,
                         after={'journal_entry': je, 'bank_transaction': bt,
                                'plaid_transaction_id':
                                    row.plaid_transaction_id},
                         notes=f'Plaid removed {row.plaid_transaction_id}',
                         commit=False)
        elif ds == 0:
            leftover = (f'Draft Journal Entry {je} was written for a '
                        'transaction Plaid later removed; it was not '
                        'submitted, so nothing is on the ledger — delete the '
                        'draft in ERPNext.')
    if not erpnext_bank.cancel_bank_transaction(client, bt):
        _flag(row, (f'Plaid removed this transaction, but ERPNext would not '
                    f'cancel Bank Transaction {bt} — another document still '
                    'links to it (a Journal Entry or Payment Entry Bank '
                    'Bridge did not create). Review it in ERPNext.'))
        return 'review'
    if leftover:
        _flag(row, leftover)
    return 'cancelled'


# ── allocation: the rule JE recorded against its Bank Transaction ───────────

def allocate_je(client, bank_transaction: str, journal_entry: str,
                amount: float = 0.0) -> bool:
    """Reconcile a SUBMITTED Journal Entry against its Bank Transaction, through
    ERPNext's own Bank Reconciliation Tool method — so the BT's
    `payment_entries` carries it, its allocated amount is filled and its status
    reads Reconciled. That is what makes every other job (ERPNext's own tool, an
    AI's Bank Entry run) see the BT as done instead of booking it again.

    Fail-soft: answers False (and logs) on any refusal — ERPNext says "already
    fully reconciled" for a BT something else got to first, which is fine."""
    if client is None or not bank_transaction or not journal_entry:
        return False
    try:
        client.call_method(RECONCILE_METHOD, http_method='POST', json_body={
            'bank_transaction_name': bank_transaction,
            'vouchers': json.dumps([{'payment_doctype': JE_DT,
                                     'payment_name': journal_entry,
                                     'amount': abs(float(amount or 0.0))}])})
    except (ERPNextAPIError, ERPNextError) as e:
        log.info('allocation of %s to %s declined: %s', journal_entry,
                 bank_transaction, e)
        return False
    return True


def allocate_for_row(client, row: BankTransaction) -> bool:
    """Allocate the row's own submitted rule JE to its BT, if it has one and no
    review flag says the two disagree."""
    if row is None or row.review_reason or not row.erpnext_bank_transaction_id:
        return False
    gje = GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=row.plaid_transaction_id).first()
    if gje is None or gje.state != 'approved' \
            or not gje.erpnext_journal_entry_name \
            or gje.intercompany_pair_id is not None:
        return False
    return allocate_je(client, row.erpnext_bank_transaction_id,
                       gje.erpnext_journal_entry_name, row.amount)


# ── the repair: existing pending/posted duplicate pairs ─────────────────────

def _bt_amount(doc: dict) -> float:
    """Signed in Plaid's convention: positive = money out."""
    return round(_money(doc.get('withdrawal')) - _money(doc.get('deposit')), 2)


def _date(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _linked_jes(client, bt_names: list) -> dict:
    """bt name → live Journal Entries tied to it, by either route: a line
    referencing the BT (Bank Bridge's rule JEs) or `cheque_no` = BT name (the
    Bank Reconciliation Tool's "Bank Entry" vouchers)."""
    out = {n: {} for n in bt_names}
    if not bt_names:
        return out
    fields = ['name', 'docstatus', 'voucher_type', 'posting_date',
              'total_debit', 'cheque_no', 'user_remark', 'owner', 'creation',
              'clearance_date']
    by_ref = client.list_docs(
        JE_DT, filters=[['Journal Entry Account', 'reference_type', '=', BT_DT],
                        ['Journal Entry Account', 'reference_name', 'in',
                         bt_names],
                        ['docstatus', 'in', [0, 1]]],
        fields=fields + ['`tabJournal Entry Account`.reference_name as bt'],
        limit_page_length=0) or []
    by_cheque = client.list_docs(
        JE_DT, filters=[['cheque_no', 'in', bt_names],
                        ['docstatus', 'in', [0, 1]]],
        fields=fields, limit_page_length=0) or []
    for r in by_ref:
        if r.get('bt') in out:
            out[r['bt']].setdefault(r['name'], {**r, 'link': 'reference'})
    for r in by_cheque:
        if r.get('cheque_no') in out:
            out[r['cheque_no']].setdefault(r['name'], {**r, 'link': 'cheque_no'})
    return {k: list(v.values()) for k, v in out.items()}


def _je_view(je: dict, bb_names: set, allocated: set) -> dict:
    return {'journal_entry': je['name'],
            'docstatus': _docstatus(je),
            'voucher_type': je.get('voucher_type') or '',
            'posting_date': str(je.get('posting_date') or ''),
            'amount': _money(je.get('total_debit')),
            'owner': je.get('owner') or '',
            'link': je.get('link'),
            'bank_bridge_rule_je': je['name'] in bb_names,
            'allocated_to_bank_transaction': je['name'] in allocated}


def find_duplicates(client, *, bank_accounts: list | None = None,
                    since: date | None = None,
                    until: date | None = None) -> dict:
    """Pending/posted duplicate pairs still live in ERPNext. READ-ONLY.

    A pair is two live Bank Transactions for one real charge: the one booked
    from Plaid's PENDING authorization and the one booked when it POSTED.
    Matched two ways, strongest first:

      * `pending_link` — the posted row's pending_transaction_id names the
        pending row (exact; only rows pulled since v1.0.4 carry it).
      * `amount_date` — the pending row is known locally as pending (or as
        removed by Plaid), and exactly one unpaired posted BT on the same Bank
        Account carries the same amount within PAIR_WINDOW_DAYS after it.

    Each pair carries every live JE on either BT and a recommendation:
    keep the POSTED Bank Transaction and ONE Journal Entry on it; remove the
    pending BT and every JE on it, plus any extra JE on the posted BT. A pair
    whose amounts differ is recommended for review, never removal."""
    until = until or date.today()
    since = since or (until - timedelta(days=120))
    mapped = {a.erpnext_bank_account_name: a for a in PlaidAccount.query.filter(
        PlaidAccount.erpnext_bank_account_name.isnot(None)).all()
        if a.erpnext_bank_account_name}
    targets = [b for b in (bank_accounts or sorted(mapped)) if b]
    rows_by_tid = {}
    pairs, unmatched = [], []
    scanned = 0
    for ba in targets:
        bts = client.list_docs(
            BT_DT, filters=[['bank_account', '=', ba],
                            ['docstatus', '<', 2],
                            ['date', '>=', since.isoformat()],
                            ['date', '<=', until.isoformat()]],
            fields=['name', 'date', 'deposit', 'withdrawal', 'description',
                    'reference_number', 'status', 'unallocated_amount',
                    'docstatus'],
            limit_page_length=0) or []
        scanned += len(bts)
        refs = [b.get('reference_number') for b in bts
                if b.get('reference_number')]
        for r in (BankTransaction.query.filter(
                BankTransaction.plaid_transaction_id.in_(refs)).all()
                if refs else []):
            rows_by_tid[r.plaid_transaction_id] = r
        by_ref = {b.get('reference_number'): b for b in bts
                  if b.get('reference_number')}

        def is_pending(b):
            r = rows_by_tid.get(b.get('reference_number'))
            return r is not None and bool(r.pending or r.removed)

        pending_bts = [b for b in bts if is_pending(b)]
        posted_bts = [b for b in bts if not is_pending(b)]
        used = set()
        for p in sorted(pending_bts, key=lambda b: str(b.get('date'))):
            ptid = p.get('reference_number')
            match, how = None, ''
            linked = BankTransaction.query.filter_by(
                pending_transaction_id=ptid).first()
            if linked is not None and linked.plaid_transaction_id in by_ref:
                cand = by_ref[linked.plaid_transaction_id]
                if cand['name'] != p['name'] and cand['name'] not in used:
                    match, how = cand, 'pending_link'
            if match is None:
                pd = _date(p.get('date'))
                cands = [q for q in posted_bts if q['name'] not in used
                         and _bt_amount(q) == _bt_amount(p)
                         and pd and _date(q.get('date'))
                         and pd <= _date(q.get('date'))
                         <= pd + timedelta(days=PAIR_WINDOW_DAYS)]
                if len(cands) == 1:
                    match, how = cands[0], 'amount_date'
                elif len(cands) > 1:
                    unmatched.append({'bank_account': ba,
                                      'pending_bank_transaction': p['name'],
                                      'reason': f'{len(cands)} posted '
                                                'candidates — ambiguous'})
                    continue
            if match is None:
                unmatched.append({'bank_account': ba,
                                  'pending_bank_transaction': p['name'],
                                  'reason': 'no posted twin found'})
                continue
            used.add(match['name'])
            pairs.append({'bank_account': ba, 'match': how,
                          'pending': p, 'posted': match})

    names = [x for pr in pairs for x in (pr['pending']['name'],
                                         pr['posted']['name'])]
    jes = _linked_jes(client, names) if names else {}
    bb_names = {g.erpnext_journal_entry_name for g in
                GeneratedJournalEntry.query.filter(
                    GeneratedJournalEntry.erpnext_journal_entry_name.isnot(None))
                .all()}
    allocated = set()
    for n in names:
        doc = client.get_doc(BT_DT, n) or {}
        for pe in doc.get('payment_entries') or []:
            if pe.get('payment_document') == JE_DT:
                allocated.add(pe.get('payment_entry'))

    out = []
    for i, pr in enumerate(pairs, 1):
        p, q = pr['pending'], pr['posted']
        pj = [_je_view(j, bb_names, allocated) for j in jes.get(p['name'], [])]
        qj = [_je_view(j, bb_names, allocated) for j in jes.get(q['name'], [])]
        same = _bt_amount(p) == _bt_amount(q)
        remove, keep, notes = [], [], []
        if not same:
            rec = 'review'
            notes.append('pending and posted amounts differ — fix by hand')
        else:
            rec = 'remove_pending'
            keep.append({'doctype': BT_DT, 'name': q['name']})
            # Keep exactly one JE on the posted BT: Bank Bridge's own rule JE
            # (its one-JE-per-transaction record points at it), else one
            # already reconciled to the BT, else the earliest.
            live_q = sorted(qj, key=lambda j: (not j['bank_bridge_rule_je'],
                                               not j['allocated_to_bank_transaction'],
                                               j['posting_date'],
                                               j['journal_entry']))
            if live_q:
                keep.append({'doctype': JE_DT,
                             'name': live_q[0]['journal_entry']})
                remove += [{'doctype': JE_DT, 'name': j['journal_entry']}
                           for j in live_q[1:]]
            else:
                notes.append('the posted Bank Transaction has no Journal '
                             'Entry — after cleanup, run rerun_rules (or book '
                             'it by hand)')
            remove += [{'doctype': JE_DT, 'name': j['journal_entry']}
                       for j in pj]
            remove.append({'doctype': BT_DT, 'name': p['name']})
            if any(j['allocated_to_bank_transaction'] for j in qj[1:] + pj):
                notes.append('a Journal Entry recommended for removal is '
                             'reconciled; cancelling it un-reconciles its '
                             'Bank Transaction')
        out.append({
            'pair_id': i, 'bank_account': pr['bank_account'],
            'match': pr['match'], 'recommendation': rec,
            'pending_bank_transaction': {
                'name': p['name'], 'date': str(p.get('date') or ''),
                'amount': _bt_amount(p), 'description': p.get('description'),
                'reference_number': p.get('reference_number'),
                'journal_entries': pj},
            'posted_bank_transaction': {
                'name': q['name'], 'date': str(q.get('date') or ''),
                'amount': _bt_amount(q), 'description': q.get('description'),
                'reference_number': q.get('reference_number'),
                'journal_entries': qj},
            'keep': keep, 'remove': remove, 'notes': notes})
    removable = [d for pr in out for d in pr['remove']]
    return {'since': since.isoformat(), 'until': until.isoformat(),
            'bank_accounts': targets, 'bank_transactions_scanned': scanned,
            'pair_count': len(out),
            'journal_entries_to_remove': sum(
                1 for d in removable if d['doctype'] == JE_DT),
            'bank_transactions_to_remove': sum(
                1 for d in removable if d['doctype'] == BT_DT),
            'pairs': out, 'unmatched_pending': unmatched}


def cleanup_duplicates(client, approved: list, *, dry_run: bool = True,
                       report: dict | None = None) -> dict:
    """Cancel an explicitly approved list of duplicate documents. CANCELS
    ONLY — never deletes, never edits.

    `approved` is [{'doctype': 'Journal Entry'|'Bank Transaction', 'name'}].
    Every entry must appear under some pair's `remove` in `report` (a fresh
    find_duplicates run): anything else is refused, so an approval list cannot
    be used to cancel an arbitrary document. Journal Entries go first — ERPNext
    will not cancel a Bank Transaction a live JE still references."""
    report = report if report is not None else find_duplicates(client)
    removable = {(d['doctype'], d['name']) for pr in report['pairs']
                 for d in pr['remove']}
    seen, jes, bts, refused = set(), [], [], []
    for item in approved or []:
        dt = (item or {}).get('doctype')
        name = ((item or {}).get('name') or '').strip()
        key = (dt, name)
        if key in seen:
            continue
        seen.add(key)
        if key not in removable:
            refused.append({'doctype': dt, 'name': name,
                            'reason': 'not recommended for removal in the '
                                      'current duplicate report'})
            continue
        (jes if dt == JE_DT else bts).append(name)
    result = {'dry_run': dry_run, 'refused': refused, 'cancelled': [],
              'would_cancel': [], 'already_cancelled': [], 'failed': []}
    for dt, names in ((JE_DT, jes), (BT_DT, bts)):
        for name in names:
            doc = client.get_doc(dt, name) or {}
            if not doc or _docstatus(doc) == 2:
                result['already_cancelled'].append({'doctype': dt,
                                                    'name': name})
                continue
            if _docstatus(doc) == 0:
                result['failed'].append({
                    'doctype': dt, 'name': name,
                    'reason': 'draft — cannot be cancelled, and this tool '
                              'never deletes; delete it in ERPNext'})
                continue
            if dry_run:
                result['would_cancel'].append({'doctype': dt, 'name': name})
                continue
            try:
                if dt == BT_DT:
                    if not erpnext_bank.cancel_bank_transaction(client, name):
                        raise ERPNextError('still linked to another document')
                else:
                    client.call_method('frappe.client.cancel',
                                       http_method='POST',
                                       json_body={'doctype': dt, 'name': name})
                    for g in GeneratedJournalEntry.query.filter_by(
                            erpnext_journal_entry_name=name).all():
                        g.state = 'cancelled'
                        g.error_message = ('Cancelled as a pending/posted '
                                           'duplicate by '
                                           'cleanup_duplicate_bank_transactions.')
                        g.updated_at = _now()
                result['cancelled'].append({'doctype': dt, 'name': name})
                audit.record('duplicate_bank_document_cancelled',
                             subject_type=dt, subject_id=name,
                             after={'doctype': dt, 'name': name},
                             notes='cleanup_duplicate_bank_transactions',
                             actor='mcp', commit=False)
            except (ERPNextAPIError, ERPNextError) as e:
                result['failed'].append({'doctype': dt, 'name': name,
                                         'reason': str(e)[:500]})
    db.session.commit()
    return result
