# SPDX-License-Identifier: MIT
"""Append-only audit trail (v0.3.0).

A single `record()` entry point writes one `AuditEvent` per auditable action.
Events are PERMANENT — never updated, never deleted, no TTL — so the trail is a
faithful, monotonically-growing history and the full lifecycle of any subject
(a rule, a JE, a transaction) can be reconstructed by filtering on
(subject_type, subject_id).

The `actor` + `source_ip` for an event usually don't vary within a call chain
(one HTTP request is 'admin_ui' from one IP; the poll thread is 'scheduler'), so
rather than thread them through every function they ride a contextvar the entry
points set once (see admin_ui.before_request / the scheduler job). An explicit
`actor=`/`source_ip=` on record() still wins when a caller knows better.

record() is best-effort: a failure to write an audit row is logged but never
propagates, because losing an audit line must not break the action being
audited. It commits by default (each event is durable on its own); pass
commit=False to batch it into the caller's transaction."""
from __future__ import annotations

import contextvars
import json
import logging

from . import db
from .models import AuditEvent

log = logging.getLogger('bankbridge.audit')

# ── the canonical event vocabulary ──────────────────────────────────────
EVENT_TYPES = (
    'supplier_auto_created',
    'supplier_edited',
    'rule_created',
    'rule_updated',
    'rule_deleted',
    'rule_matched',
    'journal_entry_generated',
    'journal_entry_approved',
    'journal_entry_rejected',
    'journal_entry_edited',
    'journal_entry_submitted_to_erpnext',
    'journal_entry_failed',
    'bank_transaction_synced',
    'bank_transaction_reconciled',
    'sync_run_started',
    'sync_run_completed',
    'rules_rerun',
    # v0.3.1 — auto-CoA numbering + fuzzy dedup of GL Accounts
    'gl_account_number_assigned',
    'fuzzy_match_found',
    'fuzzy_match_rejected_by_user',
)

SUBJECT_TYPES = ('Supplier', 'CategorizationRule', 'GeneratedJournalEntry',
                 'BankTransaction', 'PlaidItem', 'Account')

_actor: contextvars.ContextVar = contextvars.ContextVar('audit_actor',
                                                         default='system')
_source_ip: contextvars.ContextVar = contextvars.ContextVar('audit_source_ip',
                                                            default=None)


def set_context(actor: str = 'system', source_ip: str | None = None) -> None:
    """Set the ambient actor + source IP for subsequent record() calls in this
    execution context (one HTTP request, or the scheduler job)."""
    _actor.set(actor or 'system')
    _source_ip.set(source_ip)


def current_actor() -> str:
    return _actor.get()


def _dump(payload) -> str | None:
    """JSON-encode a payload (dict/list) to text for storage, or None."""
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return json.dumps({'_unserializable': str(payload)})


def record(event_type: str, *, subject_type: str | None = None,
           subject_id=None, before=None, after=None, notes: str | None = None,
           actor: str | None = None, source_ip: str | None = None,
           commit: bool = True) -> AuditEvent | None:
    """Write one AuditEvent. Returns the row (or None if the write failed).

    `before` / `after` are JSON-able snapshots (typically a model's to_dict()).
    `actor` / `source_ip` default to the contextvar set by set_context()."""
    try:
        ev = AuditEvent(
            event_type=event_type,
            actor=(actor or _actor.get() or 'system')[:120],
            subject_type=subject_type,
            subject_id=(str(subject_id)[:120] if subject_id is not None else None),
            payload_before=_dump(before),
            payload_after=_dump(after),
            notes=notes,
            source_ip=(source_ip if source_ip is not None else _source_ip.get()),
        )
        db.session.add(ev)
        if commit:
            db.session.commit()
        return ev
    except Exception:  # noqa: BLE001 - audit must never break the audited action
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        log.warning('failed to write AuditEvent %s', event_type, exc_info=True)
        return None
