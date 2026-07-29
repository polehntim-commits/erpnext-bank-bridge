# SPDX-License-Identifier: MIT
"""Ask ERPNext whether this Journal Entry already exists, before writing it
again (v0.8.5).

WHAT THIS IS FOR. Every JE pipeline in Bank Bridge is already idempotent
against its OWN local table — `GeneratedJournalEntry` is unique on the Plaid
transaction id, and a row whose `erpnext_journal_entry_name` is set short-
circuits the next run. That guard is exactly as durable as the local row, and
the local row is the thing operators delete: `reset_investment_drafts` removes
it by design, a restore from an older volume loses it, a migration marker
arrives late. When it goes, ERPNext is still holding the JE and Bank Bridge no
longer knows — so it writes a second one.

This module is the belt to that suspenders: the ledger itself is asked. Two
identity keys, because the two pipelines have two different ones:

  * BANK-SIDE (categorization) — the ERPNext **Bank Transaction** docname, which
    already rides every generated JE line as `reference_type` /
    `reference_name`. Nothing new is stamped; the link has been there since
    v0.3.1 and this only reads it back.

  * INVESTMENT-SIDE (invest_je settlement legs) — there is no Bank Transaction
    to reference. A settlement leg is built from a `SecurityTransaction`, so the
    JE carries a compact identity **marker** in its `user_remark`
    (`[BB:inv:<plaid_investment_transaction_id>]`) and the lookup matches on
    that. The marker is appended, never prepended: `_sweep_orphan_drafts`
    recognizes an investment draft by what its remark STARTS with.

THE TWO PRINCIPLES THIS MODULE IS BUILT ON.

  **Fail Safe.** Every lookup returns None on ANY difficulty — an unreachable
  ERPNext, a filter the server rejects, a malformed row. None means "no
  duplicate found", which means the caller creates the JE. A dedup check that
  went the other way would turn a network blip into silently missing books, and
  a missing JE is far more expensive than a duplicate draft: the duplicate is
  visible in `/admin/draft_health` and deletable in one call, while the absence
  is invisible until a statement doesn't reconcile.

  **Fail Forward.** A skip is not a shrug. `record_skip` writes an AuditEvent
  and a log line naming the pipeline, the identity key, the JE that already
  exists and its docstatus — enough to answer "should this pipeline have
  re-emitted at all?" the next time it happens, which is the question the v0.8.4
  incident could not answer from its own logs.

WHAT IS DELIBERATELY NOT DEDUPED. The investment TRADE pipeline. Trade JEs were
re-emitted by the v0.8.4 sync and we do not yet know whether any of that
re-emission was legitimate (a corrected cost basis, a re-pull with better
security metadata). Blocking a trade JE on a guess would be the exact
fail-unsafe direction this module refuses to take. Settlement legs are deduped
because their content is fixed by the cash movement — the same leg posted twice
is always wrong.
"""
from __future__ import annotations

import logging

from . import audit
from .erpnext_client import ERPNextAPIError, ERPNextError

log = logging.getLogger('bankbridge.je_dedup')

JOURNAL_ENTRY_DT = 'Journal Entry'
BANK_TRANSACTION_DT = 'Bank Transaction'

# Draft (0) and Submitted (1). A CANCELLED entry (2) is deliberately NOT a
# duplicate: cancelling is how an operator says "this posting was wrong, replace
# it", and treating it as an occupant would leave them unable to. That is the
# whole difference between this check and `is there any JE at all`.
LIVE_DOCSTATUS = (0, 1)

_DOCSTATUS_LABEL = {0: 'draft', 1: 'submitted', 2: 'cancelled'}

# The fields worth carrying back to the caller's log line. `total_debit` is the
# JE's dollar size, which is what makes a skip line readable at a glance.
_FIELDS = ['name', 'docstatus', 'posting_date', 'user_remark', 'total_debit']


# ── the identity marker (investment side) ───────────────────────────────────

def marker(kind: str, key: str) -> str:
    """The identity token Bank Bridge stamps into a JE's `user_remark`.

    Deliberately bracket-delimited and prefix-namespaced (`[BB:`) so a `like`
    match cannot collide with a merchant name, and so an operator reading the
    voucher can see at a glance which Bank Bridge record produced it — the
    audit-trail half of the same change."""
    return f'[BB:{(kind or "").strip()}:{(key or "").strip()}]'


def stamp(remark: str, kind: str, key: str) -> str:
    """`remark` with the identity marker appended (idempotent).

    Appended, not prepended: `invest_je._sweep_orphan_drafts` matches an
    investment draft on `user_remark.startswith(...)`, and a marker in front
    would make every future draft invisible to the sweep that cleans up after a
    reset."""
    token = marker(kind, key)
    text = (remark or '').strip()
    if token in text:
        return text
    return f'{text} {token}'.strip()


# ── the lookups ─────────────────────────────────────────────────────────────

def _first_live(client, filters: list, *, what: str) -> dict | None:
    """Run one Journal Entry query and return the first live match, or None.

    The single choke point for Fail Safe: every ERPNext difficulty is caught
    here and answered with None (= "create it"), loudly in the log so a dedup
    check that has stopped working is visible rather than merely absent."""
    try:
        rows = client.list_docs(JOURNAL_ENTRY_DT, filters=filters,
                                fields=_FIELDS, limit_page_length=0)
    except (ERPNextAPIError, ERPNextError):
        log.warning('dedup lookup for %s could not query ERPNext — allowing '
                    'creation (fail safe)', what, exc_info=True)
        return None
    except Exception:  # noqa: BLE001 — see the module note: never block a write
        log.warning('dedup lookup for %s raised unexpectedly — allowing '
                    'creation (fail safe)', what, exc_info=True)
        return None
    for row in rows or []:
        if not isinstance(row, dict) or not row.get('name'):
            continue
        try:
            docstatus = int(row.get('docstatus') or 0)
        except (TypeError, ValueError):
            continue
        if docstatus not in LIVE_DOCSTATUS:
            continue
        try:
            amount = float(row.get('total_debit') or 0.0)
        except (TypeError, ValueError):
            # The amount is decoration on a skip message. Losing it must not
            # cost the match — this function has one job and it is not currency
            # parsing.
            amount = 0.0
        return {'journal_entry': row.get('name'),
                'docstatus': docstatus,
                'state': _DOCSTATUS_LABEL.get(docstatus, str(docstatus)),
                'posting_date': row.get('posting_date'),
                'user_remark': row.get('user_remark') or '',
                'amount': amount}
    return None


def find_by_bank_transaction(client, bank_transaction: str, *,
                             company: str = '') -> dict | None:
    """The live JE already referencing this ERPNext Bank Transaction, or None.

    The filter is a CHILD-TABLE filter (`['Journal Entry Account', field, …]`) —
    the reference lives on the JE's account rows, not the JE header. Frappe
    supports the 4-element form on a parent list query, which keeps this one
    round-trip instead of listing every JE and reading each one."""
    bt = (bank_transaction or '').strip()
    if client is None or not bt:
        return None
    filters = [['Journal Entry Account', 'reference_type', '=',
                BANK_TRANSACTION_DT],
               ['Journal Entry Account', 'reference_name', '=', bt],
               ['docstatus', 'in', list(LIVE_DOCSTATUS)]]
    if (company or '').strip():
        filters.append(['company', '=', company.strip()])
    return _first_live(client, filters, what=f'Bank Transaction {bt}')


def find_by_marker(client, kind: str, key: str, *,
                   company: str = '') -> dict | None:
    """The live JE already carrying this identity marker, or None.

    A `like` on `user_remark`. The token is bracketed and namespaced so the
    wildcards on either side cannot match anything but a marker Bank Bridge
    itself wrote."""
    token = marker(kind, key)
    if client is None or not (key or '').strip():
        return None
    filters = [['user_remark', 'like', f'%{token}%'],
               ['docstatus', 'in', list(LIVE_DOCSTATUS)]]
    if (company or '').strip():
        filters.append(['company', '=', company.strip()])
    return _first_live(client, filters, what=token)


# ── Fail Forward: what a skip leaves behind ─────────────────────────────────

def skip_reason(pipeline: str, identity: str, existing: dict) -> str:
    """The human sentence stored on the local row and shown in the admin UI.

    Names all four things an operator (or an AI reading the row back) needs to
    decide whether the skip was right: which pipeline declined, what it matched
    on, which JE already holds it, and whether that JE is a draft or already
    submitted."""
    return (f'Skipped (duplicate): {pipeline} already has {identity} on '
            f"Journal Entry {existing.get('journal_entry')} "
            f"({existing.get('state')}, "
            f"${float(existing.get('amount') or 0.0):,.2f}, posted "
            f"{existing.get('posting_date') or 'n/a'}). No second entry was "
            'created. Cancel that entry if this transaction genuinely needs a '
            'fresh one.')


def record_skip(pipeline: str, identity: str, existing: dict, *,
                subject_type: str = 'GeneratedJournalEntry',
                subject_id=None, extra: dict | None = None) -> str:
    """Log + audit one dedup skip and return the reason string.

    Every field a later investigation would want is in the AuditEvent payload,
    because the drafts themselves get deleted and the log rotates — the audit
    row is the only thing that survives long enough to answer "was this
    pipeline right to try again?"."""
    reason = skip_reason(pipeline, identity, existing)
    payload = {'pipeline': pipeline, 'identity': identity,
               'existing_journal_entry': existing.get('journal_entry'),
               'existing_docstatus': existing.get('docstatus'),
               'existing_state': existing.get('state'),
               'existing_amount': existing.get('amount'),
               'existing_posting_date': existing.get('posting_date')}
    if extra:
        payload.update(extra)
    log.warning('[dedup] %s skipped %s — %s already carries it (%s)',
                pipeline, identity, existing.get('journal_entry'),
                existing.get('state'))
    try:
        audit.record('journal_entry_dedup_skipped', subject_type=subject_type,
                     subject_id=subject_id, after=payload, notes=reason)
    except Exception:  # noqa: BLE001 — an audit failure must not block the sync
        log.warning('could not audit dedup skip for %s', identity, exc_info=True)
    return reason
