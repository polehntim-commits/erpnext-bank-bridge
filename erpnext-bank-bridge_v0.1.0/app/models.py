# SPDX-License-Identifier: MIT
"""SQLAlchemy models — the local mirror of Plaid Items / Accounts /
Transactions plus a sync audit trail. Deliberately Maximal-Data-Science:
one wide, well-indexed table per concept rather than table sprawl.

The Plaid access_token is the only real secret here; it is stored ENCRYPTED
(Fernet) in `PlaidItem.access_token_encrypted` and only ever decrypted in
memory by the sync engine (see app/crypto.py, app/plaid_client.py). Nothing
in to_dict() ever emits it."""
from datetime import datetime, timezone
from . import db


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PlaidItem(db.Model):
    """One linked Plaid Item = one login at one institution (Wells Fargo,
    Columbia Bank, …). An Item fans out to one or more PlaidAccounts. The
    `cursor` is Plaid's opaque /transactions/sync position — persisted so each
    poll only pulls the delta since last time. `access_token_encrypted` holds
    the Fernet-encrypted Plaid access_token; it is never logged or serialized."""
    __tablename__ = 'plaid_items'
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    access_token_encrypted = db.Column(db.Text, nullable=False)
    institution_id = db.Column(db.String(120), default='', index=True)
    institution_name = db.Column(db.String(255), default='')
    # Plaid /transactions/sync cursor. NULL/'' = never synced (full backfill
    # on first poll).
    cursor = db.Column(db.Text, nullable=True)
    # active | error | revoked
    status = db.Column(db.String(20), default='active', index=True)
    created_at = db.Column(db.DateTime, default=_now)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        db.CheckConstraint("status IN ('active', 'error', 'revoked')",
                           name='ck_plaid_items_status'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'item_id': self.item_id,
            'institution_id': self.institution_id,
            'institution_name': self.institution_name,
            'has_cursor': bool(self.cursor),
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_synced_at': self.last_synced_at.isoformat() if self.last_synced_at else None,
            'last_error': self.last_error,
        }


class PlaidAccount(db.Model):
    """One depository/credit/loan account inside a PlaidItem.
    `erpnext_bank_account_name` is the operator-assigned link to an ERPNext
    Bank Account docname — a transaction only pushes to ERPNext once its
    account is mapped (and `sync_enabled` is on). Balances are cached from the
    last accounts pull for the dashboard; they are informational, not
    authoritative."""
    __tablename__ = 'plaid_accounts'
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    item_id = db.Column(db.String(120), db.ForeignKey('plaid_items.item_id'),
                        nullable=False, index=True)
    name = db.Column(db.String(255), default='')
    official_name = db.Column(db.String(255), default='')
    mask = db.Column(db.String(10), default='')       # last 4
    type = db.Column(db.String(30), default='')        # depository/credit/loan
    subtype = db.Column(db.String(40), default='')     # checking/savings/credit card
    balance_available = db.Column(db.Float, nullable=True)
    balance_current = db.Column(db.Float, nullable=True)
    currency = db.Column(db.String(8), default='USD')
    iso_currency_code = db.Column(db.String(8), default='USD')
    # Operator-assigned ERPNext Bank Account docname (dropdown-fed). NULL =
    # unmapped → transactions are mirrored locally but not pushed.
    erpnext_bank_account_name = db.Column(db.String(255), nullable=True)
    # The ERPNext GL Account (Chart of Accounts, account_type 'Bank') that
    # one-click import auto-creates and links on the company Bank Account so
    # `is_company_account = 1` holds (see app/erpnext_accounts.py). NULL until an
    # import creates/links one — or stays NULL when the GL auto-create failed and
    # the import fell back to a personal account.
    erpnext_gl_account_name = db.Column(db.Text, nullable=True)
    sync_enabled = db.Column(db.Boolean, default=True)
    # One-click-import lifecycle (see app/erpnext_accounts.py):
    #   pending     — never auto-imported (the default / freshly linked)
    #   imported    — a matching ERPNext Bank Account was created/found + linked
    #   unsupported — the Plaid type/subtype isn't a Bank Account in ERPNext's
    #                 model (loans, investments, 401k, …) so no button is offered
    # Left deliberately un-constrained (like plaid_sync_log) so a future status
    # never needs a migration.
    import_status = db.Column(db.String(20), default='pending', index=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id, 'account_id': self.account_id, 'item_id': self.item_id,
            'name': self.name, 'official_name': self.official_name,
            'mask': self.mask, 'type': self.type, 'subtype': self.subtype,
            'balance_available': self.balance_available,
            'balance_current': self.balance_current,
            'currency': self.currency, 'iso_currency_code': self.iso_currency_code,
            'erpnext_bank_account_name': self.erpnext_bank_account_name,
            'erpnext_gl_account_name': self.erpnext_gl_account_name,
            'sync_enabled': bool(self.sync_enabled),
            'import_status': self.import_status or 'pending',
        }


class BankTransaction(db.Model):
    """Local mirror of one Plaid transaction — NOT the ERPNext Bank
    Transaction (that lives in Frappe; we store its docname in
    `erpnext_bank_transaction_id` once posted). Unique on
    `plaid_transaction_id` so re-running a sync is idempotent. `amount` keeps
    Plaid's raw convention (positive = money OUT of the account); the deposit /
    withdrawal split for ERPNext is derived at push time (see erpnext_bank)."""
    __tablename__ = 'bank_transactions'
    id = db.Column(db.Integer, primary_key=True)
    plaid_transaction_id = db.Column(db.String(120), unique=True,
                                     nullable=False, index=True)
    account_id = db.Column(db.String(120), db.ForeignKey('plaid_accounts.account_id'),
                          nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False, default=0.0)  # Plaid convention
    iso_currency_code = db.Column(db.String(8), default='USD')
    date = db.Column(db.Date, nullable=True, index=True)
    name = db.Column(db.String(500), default='')
    merchant_name = db.Column(db.String(255), default='')
    category = db.Column(db.String(255), default='')
    pending = db.Column(db.Boolean, default=False, index=True)
    # ERPNext bookkeeping — the returned Bank Transaction docname + when posted.
    erpnext_bank_transaction_id = db.Column(db.String(255), nullable=True, index=True)
    posted_at = db.Column(db.DateTime, nullable=True)
    # removed | (blank). Plaid's `removed` list marks a transaction gone; we
    # keep the row for the audit trail and cancel the ERPNext doc.
    removed = db.Column(db.Boolean, default=False, index=True)
    sync_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id, 'plaid_transaction_id': self.plaid_transaction_id,
            'account_id': self.account_id, 'amount': self.amount,
            'iso_currency_code': self.iso_currency_code,
            'date': self.date.isoformat() if self.date else None,
            'name': self.name, 'merchant_name': self.merchant_name,
            'category': self.category, 'pending': bool(self.pending),
            'erpnext_bank_transaction_id': self.erpnext_bank_transaction_id,
            'posted_at': self.posted_at.isoformat() if self.posted_at else None,
            'removed': bool(self.removed), 'sync_error': self.sync_error,
        }


class PlaidSyncLog(db.Model):
    """One row per logical sync ACTION (a plaid pull, or an erpnext push batch)
    — an audit / debug ledger surfaced at /admin/sync_log. Deliberately
    un-constrained on direction/status so a future action type never needs a
    migration to log."""
    __tablename__ = 'plaid_sync_log'
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, default=_now, index=True)
    # Nullable: batch actions (e.g. bulk account import) legitimately have no
    # single owning item_id and log with ''/NULL.
    item_id = db.Column(db.String(120), default='', nullable=True, index=True)
    # plaid_pull | erpnext_push | erpnext_account_import — kept wide (64) because
    # some action labels ('erpnext_account_import' = 22 chars) overflow a
    # VARCHAR(20); Postgres enforces the limit and would reject the INSERT.
    direction = db.Column(db.String(64), nullable=False, index=True)
    count = db.Column(db.Integer, default=0)          # transactions handled
    status = db.Column(db.String(12), default='success', index=True)  # success | failed
    error_message = db.Column(db.Text, nullable=True)
    # v0.3.0 audit cross-link: the AuditEvent.subject_id this action pertains to
    # (e.g. a Supplier id for an erpnext_supplier_auto_create) so the audit
    # detail view can surface the underlying HTTP-level log lines alongside the
    # higher-level event. NULL for batch actions with no single owning subject.
    subject_id = db.Column(db.String(120), nullable=True, index=True)

    def to_dict(self):
        return {
            'id': self.id,
            'at': self.at.isoformat() if self.at else None,
            'item_id': self.item_id, 'direction': self.direction,
            'count': self.count, 'status': self.status,
            'error_message': self.error_message, 'subject_id': self.subject_id,
        }


class Supplier(db.Model):
    """Local mirror / cache of merchants seen on Plaid transactions → the
    ERPNext Supplier they map to (v0.3.0). One row per `normalized_name` so the
    auto-create path is a cheap local lookup before touching ERPNext.

    `merchant_name` keeps the raw Plaid string that first minted the row;
    `normalized_name` is the cleaned, title-cased key (see
    erpnext_bank.normalize_merchant_name) and is what we search / create in
    ERPNext. `erpnext_supplier_name` is the ERPNext Supplier docname once
    resolved (NULL until then, e.g. if ERPNext wasn't reachable at push time).
    The three tally columns power the /admin/suppliers dashboard; they are a
    running best-effort count, not an authoritative ledger."""
    __tablename__ = 'suppliers'
    id = db.Column(db.Integer, primary_key=True)   # the spec's local_id
    merchant_name = db.Column(db.String(255), default='', index=True)
    normalized_name = db.Column(db.String(255), unique=True, nullable=False,
                                index=True)
    erpnext_supplier_name = db.Column(db.String(255), nullable=True, index=True)
    first_seen_at = db.Column(db.DateTime, default=_now)
    last_transaction_at = db.Column(db.DateTime, nullable=True)
    transaction_count = db.Column(db.Integer, default=0)
    total_amount = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id, 'merchant_name': self.merchant_name,
            'normalized_name': self.normalized_name,
            'erpnext_supplier_name': self.erpnext_supplier_name,
            'first_seen_at': self.first_seen_at.isoformat() if self.first_seen_at else None,
            'last_transaction_at': (self.last_transaction_at.isoformat()
                                    if self.last_transaction_at else None),
            'transaction_count': self.transaction_count or 0,
            'total_amount': self.total_amount or 0.0,
        }


class CategorizationRule(db.Model):
    """A user-configured rule that maps a Bank Transaction onto a Journal Entry
    (v0.3.0). Rules are evaluated in `priority` ascending order (lower wins) and
    the FIRST active rule that matches generates the JE — see
    app/categorization.py.

    `match_type` + `match_value` describe the predicate; `debit_account` /
    `credit_account` are ERPNext Account docnames (the credit is usually the
    Bank Account). `party_type` / `party_name` optionally link a Supplier /
    Customer on the expense line (party_name blank → the auto-created Supplier
    for the transaction's merchant is used). `description_template` is a Jinja
    string rendered into the JE's user_remark. Deliberately un-constrained on
    match_type so a new predicate never needs a migration."""
    __tablename__ = 'categorization_rules'
    id = db.Column(db.Integer, primary_key=True)
    priority = db.Column(db.Integer, default=100, index=True)
    active = db.Column(db.Boolean, default=True, index=True)
    name = db.Column(db.String(255), default='')
    # merchant_exact | merchant_contains | description_regex |
    # plaid_category_matches | amount_range
    match_type = db.Column(db.String(40), nullable=False, default='merchant_contains')
    # Pattern / exact match, or a JSON array '[min, max]' for amount_range.
    match_value = db.Column(db.Text, default='')
    debit_account = db.Column(db.String(255), default='')
    credit_account = db.Column(db.String(255), default='')
    party_type = db.Column(db.String(20), nullable=True)    # Supplier | Customer
    party_name = db.Column(db.String(255), nullable=True)
    description_template = db.Column(db.Text, default='')
    # Non-destructive history (v0.3.0 audit): a rule is never mutated in place or
    # hard-deleted. An EDIT clones the rule and points the old row's
    # `superseded_by` at the new id (both archived=inactive); a DELETE just sets
    # `archived`. The rules engine only ever sees active + non-archived rules, so
    # a past auto-JE decision can always be reconstructed from the archived row.
    superseded_by = db.Column(db.Integer, nullable=True, index=True)
    archived = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id, 'priority': self.priority, 'active': bool(self.active),
            'name': self.name, 'match_type': self.match_type,
            'match_value': self.match_value,
            'debit_account': self.debit_account,
            'credit_account': self.credit_account,
            'party_type': self.party_type, 'party_name': self.party_name,
            'description_template': self.description_template,
            'superseded_by': self.superseded_by,
            'archived': bool(self.archived),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class GeneratedJournalEntry(db.Model):
    """Audit trail of a rules-engine-generated ERPNext Journal Entry (v0.3.0).

    Unique on `plaid_transaction_id` so a transaction generates AT MOST ONE JE —
    the row's existence is the idempotency guard the sync path checks before
    generating again. `rule_id` is the winning CategorizationRule (nullable so a
    future manual/unmatched entry can still be audited). `state` moves
    pending_review → approved | rejected; `error` records a generation failure
    (with `error_message`) without blocking the underlying Bank Transaction."""
    __tablename__ = 'generated_journal_entries'
    id = db.Column(db.Integer, primary_key=True)
    plaid_transaction_id = db.Column(db.String(120), unique=True,
                                     nullable=False, index=True)
    rule_id = db.Column(db.Integer, nullable=True, index=True)
    erpnext_journal_entry_name = db.Column(db.String(255), nullable=True, index=True)
    # pending_review | approved | rejected | error — left un-constrained (like
    # plaid_sync_log) so a future state never needs a migration.
    state = db.Column(db.String(20), default='pending_review', index=True)
    # Denormalized snapshot for the audit dashboard (so it renders without a
    # join back to bank_transactions / categorization_rules).
    amount = db.Column(db.Float, default=0.0)
    merchant_name = db.Column(db.String(255), default='')
    description = db.Column(db.Text, default='')
    rule_name = db.Column(db.String(255), default='')
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id, 'plaid_transaction_id': self.plaid_transaction_id,
            'rule_id': self.rule_id,
            'erpnext_journal_entry_name': self.erpnext_journal_entry_name,
            'state': self.state, 'amount': self.amount or 0.0,
            'merchant_name': self.merchant_name, 'description': self.description,
            'rule_name': self.rule_name, 'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class AuditEvent(db.Model):
    """Append-only, permanent audit trail of every auditable action (v0.3.0).

    One row per meaningful event — a supplier auto-created, a rule
    created/updated/deleted, a rule evaluated against a transaction, a Journal
    Entry generated / approved / rejected / submitted / failed, a sync run
    starting and completing. NEVER updated or deleted (no TTL), so the count only
    grows and the full lifecycle of any subject is reconstructable by filtering
    on (subject_type, subject_id).

    `payload_before` / `payload_after` hold JSON snapshots (stored as text for
    Postgres+SQLite portability) so a change is fully diff-able after the fact.
    `event_type` / `subject_type` are deliberately un-constrained strings so a
    new event kind never needs a migration."""
    __tablename__ = 'audit_events'
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, default=_now, index=True)
    event_type = db.Column(db.String(48), nullable=False, index=True)
    # 'system' | 'scheduler' | 'admin_ui' | a user identifier.
    actor = db.Column(db.String(120), default='system', index=True)
    # Supplier | CategorizationRule | GeneratedJournalEntry | BankTransaction |
    # PlaidItem  (nullable for run-level events with no single subject).
    subject_type = db.Column(db.String(40), nullable=True, index=True)
    subject_id = db.Column(db.String(120), nullable=True, index=True)
    payload_before = db.Column(db.Text, nullable=True)   # JSON string | NULL
    payload_after = db.Column(db.Text, nullable=True)    # JSON string | NULL
    notes = db.Column(db.Text, nullable=True)
    source_ip = db.Column(db.String(64), nullable=True)

    def _parse(self, blob):
        if not blob:
            return None
        try:
            import json
            return json.loads(blob)
        except (ValueError, TypeError):
            return blob

    def to_dict(self):
        return {
            'id': self.id,
            'at': self.at.isoformat() if self.at else None,
            'event_type': self.event_type, 'actor': self.actor,
            'subject_type': self.subject_type, 'subject_id': self.subject_id,
            'payload_before': self._parse(self.payload_before),
            'payload_after': self._parse(self.payload_after),
            'notes': self.notes, 'source_ip': self.source_ip,
        }


class PlaidLinkState(db.Model):
    """Ephemeral link-token bookkeeping for the OAuth handoff. When Plaid Link
    is initialized for an OAuth-only bank (Wells Fargo), the redirect back to
    /plaid/oauth_return must re-initialize Link with the SAME link_token; we
    persist it here keyed by a random state id we round-trip through the
    `oauth_state_id` so the return page can look it up. Rows are short-lived and
    pruned opportunistically."""
    __tablename__ = 'plaid_link_state'
    id = db.Column(db.Integer, primary_key=True)
    link_token = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=_now, index=True)

    def to_dict(self):
        return {'id': self.id, 'created_at':
                self.created_at.isoformat() if self.created_at else None}
