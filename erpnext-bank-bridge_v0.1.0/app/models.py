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
    sync_enabled = db.Column(db.Boolean, default=True)
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
            'sync_enabled': bool(self.sync_enabled),
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
    item_id = db.Column(db.String(120), default='', index=True)
    # plaid_pull | erpnext_push
    direction = db.Column(db.String(20), nullable=False, index=True)
    count = db.Column(db.Integer, default=0)          # transactions handled
    status = db.Column(db.String(12), default='success', index=True)  # success | failed
    error_message = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'at': self.at.isoformat() if self.at else None,
            'item_id': self.item_id, 'direction': self.direction,
            'count': self.count, 'status': self.status,
            'error_message': self.error_message,
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
