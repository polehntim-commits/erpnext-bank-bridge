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
    installs that already carry pending/posted duplicate pairs. Since v1.0.6 a
    stale Bank Transaction is FLAGGED (is_duplicate / duplicate_of), prefixed,
    cancelled and KEPT — never deleted.

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

def drop_dead_inherited_je(client, row: BankTransaction) -> bool:
    """Forget a Journal Entry this row inherited from its pending twin when that
    JE is no longer live in ERPNext (cancelled, or gone). Returns True when it
    was dropped, so the rules engine books a fresh one for the posted charge.

    Without this, a posted charge whose pending twin's JE had already been
    cancelled by hand (OML's 10/3 cleanup did exactly that) would inherit the
    cancelled entry and end up with no live JE at all — the local
    one-JE-per-transaction record would claim one existed."""
    gje = GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=row.plaid_transaction_id).first()
    if gje is None or not gje.erpnext_journal_entry_name:
        return False
    try:
        doc = client.get_doc(JE_DT, gje.erpnext_journal_entry_name)
    except (ERPNextAPIError, ERPNextError):
        return False                       # can't tell: keep it, never double-book
    if doc and _docstatus(doc) in (0, 1):
        return False
    audit.record('inherited_journal_entry_dropped',
                 subject_type='GeneratedJournalEntry', subject_id=gje.id,
                 before={'journal_entry': gje.erpnext_journal_entry_name,
                         'state': gje.state},
                 after={'plaid_transaction_id': row.plaid_transaction_id},
                 notes='inherited JE is cancelled in ERPNext — booking afresh',
                 commit=False)
    gje.erpnext_journal_entry_name = None
    gje.state = 'superseded'
    gje.error_message = 'The inherited Journal Entry was cancelled in ERPNext.'
    gje.updated_at = _now()
    db.session.commit()
    return True


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
            'keep': keep, 'remove': remove, 'notes': notes,
            # v1.0.6 · what cleanup_duplicate_bank_transactions takes for this
            # pair, once the Journal Entries on the pending side are cancelled.
            'cleanup_entry': ({'bank_transaction': p['name'],
                               'duplicate_of': q['name']} if same else None)})
    removable = [d for pr in out for d in pr['remove']]
    return {'since': since.isoformat(), 'until': until.isoformat(),
            'bank_accounts': targets, 'bank_transactions_scanned': scanned,
            'pair_count': len(out),
            'journal_entries_to_remove': sum(
                1 for d in removable if d['doctype'] == JE_DT),
            'bank_transactions_to_remove': sum(
                1 for d in removable if d['doctype'] == BT_DT),
            'pairs': out, 'unmatched_pending': unmatched}


# ── v1.0.6 · flag-and-keep: the duplicate marker fields ────────────────────
#
# A stale duplicate Bank Transaction is KEPT — it is the audit trail of what
# Bank Bridge once booked — but flagged and cancelled so it leaves Pending and
# never reaches bank reconciliation. Two Custom Fields carry the flag.
#
# WHY BANK BRIDGE PROVISIONS THEM (and not erpnext_mcp): erpnext_mcp promises
# in its hooks.py that it adds no field to a doctype it did not create, with one
# argued exception (compliance). Bank Bridge already provisions Custom Fields on
# ERPNext doctypes over REST, idempotently (erpnext_statements.
# ensure_status_custom_fields), and these two describe Bank Bridge's own
# dedup decision. No cross-repo deploy is needed.
#
# allow_on_submit=1 so the flag can be written onto an already-SUBMITTED Bank
# Transaction before it is cancelled (a cancelled doc can no longer be edited).
DUPLICATE_FIELDS = (
    {'fieldname': 'is_duplicate', 'fieldtype': 'Check',
     'label': 'Is Duplicate', 'default': '0', 'read_only': 1,
     'allow_on_submit': 1, 'in_standard_filter': 1,
     'insert_after': 'reference_number',
     'description': 'Set by Bank Bridge: this Bank Transaction duplicates '
                    'another one for the same charge and was cancelled and '
                    'kept for the audit trail.'},
    {'fieldname': 'duplicate_of', 'fieldtype': 'Link',
     'options': 'Bank Transaction', 'label': 'Duplicate Of', 'read_only': 1,
     'allow_on_submit': 1, 'depends_on': 'eval:doc.is_duplicate',
     'insert_after': 'is_duplicate',
     'description': 'The Bank Transaction kept for this charge.'},
)
CUSTOM_FIELD_DT = 'Custom Field'


def duplicate_fields_present(client) -> bool:
    try:
        rows = client.list_docs(
            CUSTOM_FIELD_DT,
            filters=[['dt', '=', BT_DT],
                     ['fieldname', 'in',
                      [f['fieldname'] for f in DUPLICATE_FIELDS]]],
            fields=['fieldname'], limit_page_length=0) or []
    except (ERPNextAPIError, ERPNextError):
        return False
    return {r.get('fieldname') for r in rows} >= {
        f['fieldname'] for f in DUPLICATE_FIELDS}


def ensure_duplicate_fields(client) -> bool:
    """Idempotently add is_duplicate / duplicate_of to Bank Transaction.
    Returns True when both exist afterwards."""
    for spec in DUPLICATE_FIELDS:
        try:
            have = client.list_docs(
                CUSTOM_FIELD_DT, filters=[['dt', '=', BT_DT],
                                          ['fieldname', '=', spec['fieldname']]],
                fields=['name'], limit_page_length=1)
            if not have:
                client.create_doc(CUSTOM_FIELD_DT, {'dt': BT_DT, **spec})
                log.info('provisioned Bank Transaction field %s',
                         spec['fieldname'])
        except (ERPNextAPIError, ERPNextError) as e:
            log.warning('could not provision Bank Transaction field %s: %s',
                        spec['fieldname'], e)
            return False
    return duplicate_fields_present(client)


DUPLICATE_PREFIX = 'DUPLICATE of '


def _live_links(client, name: str) -> list:
    """Live (draft/submitted) Journal Entries tied to a Bank Transaction by
    either route. Raises on an ERPNext error — this guards a cancel."""
    out = []
    for filters in ([['Journal Entry Account', 'reference_type', '=', BT_DT],
                     ['Journal Entry Account', 'reference_name', '=', name],
                     ['docstatus', 'in', [0, 1]]],
                    [['cheque_no', '=', name], ['docstatus', 'in', [0, 1]]]):
        for r in client.list_docs(JE_DT, filters=filters, fields=['name'],
                                  limit_page_length=0) or []:
            if r.get('name') and r['name'] not in out:
                out.append(r['name'])
    return out


def _check_item(client, stale: str, keep: str, doc: dict, kdoc: dict,
                item: dict | None = None) -> str:
    """Why this (stale, keep) pair must NOT be flagged, or ''.

    Two explicit, per-item escapes, each for a case a human has already ruled
    on — neither is ever inferred:
      * `no_twin: true` (+ a `reason`) — a stale pending row with no posted
        twin to keep: a reversed authorization, or one whose twin cannot be
        identified uniquely. Flagged with duplicate_of left empty.
      * `allow_amount_difference: true` — the posted amount legitimately
        differs (a tip added to a restaurant charge)."""
    item = item or {}
    if item.get('no_twin'):
        if not stale or not doc:
            return f'Bank Transaction {stale} not found'
        if keep:
            return 'no_twin and duplicate_of are mutually exclusive'
        if not (item.get('reason') or '').strip():
            return 'no_twin needs a reason saying why there is no posted twin'
        return _check_stale_side(client, stale, doc)
    if not stale or not keep:
        return ('both bank_transaction and duplicate_of are required '
                '(duplicate_of could not be resolved from the duplicate '
                'finder — name the kept Bank Transaction explicitly)')
    if stale == keep:
        return 'a Bank Transaction cannot be a duplicate of itself'
    if not doc:
        return f'Bank Transaction {stale} not found'
    if not kdoc:
        return f'kept Bank Transaction {keep} not found'
    if _docstatus(kdoc) == 2 or kdoc.get('is_duplicate'):
        return (f'{keep} is cancelled or itself flagged as a duplicate — '
                'choose the live Bank Transaction to keep')
    if (doc.get('bank_account') or '') != (kdoc.get('bank_account') or ''):
        return (f'different Bank Accounts ({doc.get("bank_account")} vs '
                f'{kdoc.get("bank_account")})')
    if _bt_amount(doc) != _bt_amount(kdoc) and not (
            item.get('allow_amount_difference')
            and (_bt_amount(doc) > 0) == (_bt_amount(kdoc) > 0)):
        return (f'amounts differ ({_bt_amount(doc):.2f} vs '
                f'{_bt_amount(kdoc):.2f}) — not the same charge (pass '
                'allow_amount_difference: true if a human ruled it is, e.g. '
                'a tip)')
    return _check_stale_side(client, stale, doc)


def _check_stale_side(client, stale: str, doc: dict) -> str:
    """The checks on the stale Bank Transaction alone: not reconciled, and no
    live Journal Entry on it."""
    if _docstatus(doc) == 1 and doc.get('payment_entries'):
        vouchers = ', '.join(f"{p.get('payment_document')} "
                             f"{p.get('payment_entry')}"
                             for p in doc['payment_entries'])
        return f'{stale} is reconciled against {vouchers} — unreconcile first'
    live = _live_links(client, stale)
    if live:
        return (f'{stale} still has live Journal Entr'
                f'{"y" if len(live) == 1 else "ies"} {", ".join(live)} — '
                'cancel them first (this tool flags Bank Transactions whose '
                'Journal Entries are already cancelled)')
    return ''


def resolve_kept(client, items: list, report: dict | None = None) -> list:
    """Fill in `duplicate_of` for items that name only the stale Bank
    Transaction, from the duplicate finder's pairs (pending → its posted twin).
    An item the finder cannot pair keeps an empty `duplicate_of` and is then
    refused — the kept Bank Transaction is never guessed."""
    if all((it or {}).get('duplicate_of') or (it or {}).get('no_twin')
           for it in items or []):
        return items
    report = report if report is not None else find_duplicates(client)
    twin = {pr['cleanup_entry']['bank_transaction']:
            pr['cleanup_entry']['duplicate_of']
            for pr in report.get('pairs', []) if pr.get('cleanup_entry')}
    out = []
    for it in items or []:
        it = dict(it or {})
        stale = (it.get('bank_transaction') or '').strip()
        if not (it.get('duplicate_of') or '').strip() and not it.get('no_twin') \
                and stale in twin:
            it['duplicate_of'] = twin[stale]
            it['resolved_by'] = 'find_duplicates'
        out.append(it)
    return out


def cleanup_duplicates(client, items: list, *, dry_run: bool = True,
                       reason: str = '', actor: str = 'mcp',
                       report: dict | None = None) -> dict:
    """Flag-and-keep an explicit list of stale duplicate Bank Transactions.

    `items` is [{'bank_transaction': stale, 'duplicate_of': kept,
    'reason'?: str}] — e.g. the list an earlier cleanup already identified,
    or the `cleanup_entry` of each find_duplicates pair. For each stale BT that
    passes every check (_check_item), and only when `dry_run` is False:

      1. is_duplicate=1, duplicate_of=<kept>; on a DRAFT also the description
         is prefixed "DUPLICATE of <kept> — " (a submitted Bank Transaction's
         description is locked by ERPNext, so the flag fields and the comment
         below carry the marker there);
      2. it is CANCELLED — a draft is submitted first, because Frappe cancels
         only submitted documents — ending at docstatus 2, status Cancelled,
         out of Pending and out of the Bank Reconciliation Tool for good;
      3. a Comment on the document records who (the ERPNext API user the
         Comment is owned by, plus `actor`), when and why — it survives on a
         cancelled document;
      4. any local row still pointing at the stale BT is repointed at the kept
         one, so a later Plaid modification cannot resurrect the duplicate.

    NEVER deletes anything, and never touches a Journal Entry: a stale BT with
    a live JE is refused. Idempotent — an item already flagged and cancelled is
    reported as `already_flagged`."""
    items = resolve_kept(client, items, report)
    seen, plan, refused = set(), [], []
    stale_set = {((it or {}).get('bank_transaction') or '').strip()
                 for it in items or []}
    for it in items or []:
        stale = ((it or {}).get('bank_transaction') or '').strip()
        keep = ((it or {}).get('duplicate_of') or '').strip()
        why_item = ((it or {}).get('reason') or reason or '').strip()
        if stale in seen:
            continue
        seen.add(stale)
        doc = client.get_doc(BT_DT, stale) if stale else None
        kdoc = client.get_doc(BT_DT, keep) if keep else None
        if doc and _docstatus(doc) == 2:
            if doc.get('is_duplicate') and (doc.get('duplicate_of') or '') \
                    == keep:
                refused.append({'bank_transaction': stale,
                                'duplicate_of': keep,
                                'outcome': 'already_flagged'})
            else:
                refused.append({'bank_transaction': stale, 'duplicate_of': keep,
                                'outcome': 'refused',
                                'reason': 'already cancelled (not flagged) — '
                                          'a cancelled document cannot be '
                                          'edited'})
            continue
        why = ('kept Bank Transaction is itself in the list'
               if keep in stale_set else '')
        try:
            why = why or _check_item(client, stale, keep, doc or {},
                                     kdoc or {}, it)
        except (ERPNextAPIError, ERPNextError) as e:
            why = f'could not check live Journal Entries: {e}'
        if why:
            refused.append({'bank_transaction': stale, 'duplicate_of': keep,
                            'outcome': 'refused', 'reason': why})
            continue
        plan.append({'bank_transaction': stale, 'duplicate_of': keep,
                     'no_twin': bool(it.get('no_twin')),
                     'reason': why_item,
                     'route': ('draft → flag + prefix → submit → cancel'
                               if _docstatus(doc) == 0
                               else 'submitted → flag → cancel'),
                     'amount': _bt_amount(doc), 'date': str(doc.get('date')),
                     'description': doc.get('description') or '',
                     'docstatus': _docstatus(doc)})
    result = {'dry_run': dry_run,
              'fields_present': duplicate_fields_present(client),
              'would_flag': plan if dry_run else [],
              'flagged': [], 'failed': [],
              'already_flagged': [r for r in refused
                                  if r['outcome'] == 'already_flagged'],
              'refused': [r for r in refused if r['outcome'] == 'refused']}
    if dry_run or not plan:
        return result
    if not ensure_duplicate_fields(client):
        result['failed'] = [{**p, 'reason': 'could not provision the '
                             'is_duplicate / duplicate_of fields'}
                            for p in plan]
        return result
    result['fields_present'] = True
    for p in plan:
        stale, keep = p['bank_transaction'], p['duplicate_of']
        stage = 'flag'
        try:
            patch = {'is_duplicate': 1, 'duplicate_of': keep or None}
            prefix = (f'{DUPLICATE_PREFIX}{keep} — ' if keep else
                      'DUPLICATE (stale pending, no posted twin) — ')
            if p['docstatus'] == 0 and not p['description'].startswith(
                    'DUPLICATE'):
                patch['description'] = prefix + p['description']
            client.update_doc(BT_DT, stale, patch)
            got = client.get_doc(BT_DT, stale) or {}
            if not got.get('is_duplicate') \
                    or (got.get('duplicate_of') or '') != keep:
                raise ERPNextError('ERPNext did not store the duplicate flag')
            stage = 'cancel'
            if not erpnext_bank.cancel_bank_transaction(client, stale):
                raise ERPNextError('ERPNext refused to cancel it')
            stage = 'comment'
            when = _now().strftime('%Y-%m-%d %H:%M UTC')
            client.create_doc('Comment', {
                'comment_type': 'Comment', 'reference_doctype': BT_DT,
                'reference_name': stale,
                'content': ((f'Flagged as DUPLICATE of {keep}' if keep else
                             'Flagged as DUPLICATE (stale pending, no posted '
                             'twin)') + ' and cancelled by '
                            f'Bank Bridge (cleanup_duplicate_bank_transactions,'
                            f' requested by {actor}) at {when}. Reason: '
                            f'{p["reason"] or "pending/posted duplicate of the same charge"}. '
                            'Kept for the audit trail; nothing was deleted.')})
            moved = []
            for row in BankTransaction.query.filter_by(
                    erpnext_bank_transaction_id=stale).all():
                if not row.removed and keep:
                    row.erpnext_bank_transaction_id = keep
                    row.updated_at = _now()
                    moved.append(row.plaid_transaction_id)
            audit.record('bank_transaction_flagged_duplicate',
                         subject_type=BT_DT, subject_id=stale,
                         after={'bank_transaction': stale,
                                'duplicate_of': keep, 'reason': p['reason'],
                                'route': p['route'],
                                'local_rows_repointed': moved},
                         notes=f'{stale} → DUPLICATE of {keep or "(no twin)"}',
                         actor=actor,
                         commit=False)
            db.session.commit()
            result['flagged'].append({**p, 'local_rows_repointed': moved})
        except (ERPNextAPIError, ERPNextError) as e:
            db.session.rollback()
            result['failed'].append({**p, 'stage': stage,
                                     'reason': str(e)[:500]})
    return result
