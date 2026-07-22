# SPDX-License-Identifier: MIT
"""SQLAlchemy models — the local mirror of Plaid Items / Accounts /
Transactions plus a sync audit trail. Deliberately Maximal-Data-Science:
one wide, well-indexed table per concept rather than table sprawl.

The Plaid access_token is the only real secret here; it is stored ENCRYPTED
(Fernet) in `PlaidItem.access_token_encrypted` and only ever decrypted in
memory by the sync engine (see app/crypto.py, app/plaid_client.py). Nothing
in to_dict() ever emits it."""
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict

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
    # v0.4.0 · multi-entity L1: the ERPNext Company that owns this Item's
    # accounts, chosen at Plaid Link time. NULL on a pre-v0.4.0 Item (and when
    # the operator didn't pick one) — the push path then resolves it to the
    # ERPNext default Company, so existing single-company installs are unchanged.
    owning_company = db.Column(db.String(140), nullable=True)
    # v0.4.7 · operator-initiated disconnect (Plaid /item/remove). Set together:
    # once `disconnected` is true the access_token no longer exists at Plaid, so
    # every outbound call for this Item would fail — the sync loop skips it (see
    # sync_engine.sync_all) and the UI badges it.
    #
    # This is deliberately a SEPARATE flag rather than a fourth `status` value:
    # status describes the health of a LIVE link (active/error/revoked) and is
    # written by the sync path, while this records a permanent operator decision.
    # Overloading status would let a sync error silently clear the disconnect.
    #
    # The row itself is never deleted — the accounts, transactions and generated
    # Journal Entries under it stay queryable forever, which is the whole point:
    # disconnecting stops future pulls, it does not erase history.
    disconnected = db.Column(db.Boolean, default=False, nullable=False,
                             index=True)
    disconnected_at = db.Column(db.DateTime, nullable=True)
    # v0.4.11 · the bank wants the operator to log in again (Plaid's
    # ITEM_LOGIN_REQUIRED / PENDING_EXPIRATION). Until they do, every call for
    # this Item fails, so the sync loop skips it — the same economics that
    # already govern `disconnected`: don't burn a billable request on a link
    # that cannot answer.
    #
    # A separate flag rather than a fourth `status` value, for two reasons.
    # `status` carries a CHECK constraint, so a new value means a constraint
    # migration on a live database. And, as with `disconnected`, status is
    # written by the sync path on every poll — folding reauth into it would let
    # a transient error silently clear a state only a HUMAN can resolve.
    #
    # Set from two independent signals so neither is required: Plaid's ITEM
    # webhook (instant, free, needs a public webhook URL) and the error text of
    # a failed sync (always available, one poll late). Cleared only by a
    # successful update-mode reconnect or a successful sync.
    needs_reauth = db.Column(db.Boolean, default=False, nullable=False,
                             index=True)
    reauth_reason = db.Column(db.String(255), nullable=True)
    reauth_detected_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    # v0.4.28 · timestamp of the most recent successful /investments/*
    # sync. Kept separate from last_synced_at because the investments pull
    # is optional (feature-not-available Items skip it silently) and its
    # cadence differs from the transactions_sync loop. Nullable — a fresh
    # Item's investments_synced_at is NULL until Phase A step 2 runs.
    investments_synced_at = db.Column(db.DateTime, nullable=True)
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
            'owning_company': self.owning_company,
            'disconnected': bool(self.disconnected),
            'disconnected_at': (self.disconnected_at.isoformat()
                                if self.disconnected_at else None),
            'needs_reauth': bool(self.needs_reauth),
            'reauth_reason': self.reauth_reason,
            'reauth_detected_at': (self.reauth_detected_at.isoformat()
                                   if self.reauth_detected_at else None),
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
    # v0.4.0 · multi-entity L1: the ERPNext Company that owns THIS account.
    # Inherits from the parent Item at link time (set in refresh_accounts); the
    # per-account override is a correction-only escape hatch (see the Accounts
    # page). NULL falls back to the Item's owning_company, then the ERPNext
    # default Company — so pre-v0.4.0 accounts keep their current behavior.
    owning_company = db.Column(db.String(140), nullable=True)
    # v0.4.0 · balance-only investment support. True for Plaid investment
    # accounts (401k, IRA, brokerage, crypto, …): Bank Bridge creates a Bank
    # Account + GL leaf and mirrors the current balance, but skips
    # /transactions/sync (Plaid returns no transactions without the `investments`
    # product). Re-derived from type/subtype on every account refresh, so it
    # flips off if an account is ever reclassified to a depository type.
    balance_only = db.Column(db.Boolean, default=False, index=True)
    sync_enabled = db.Column(db.Boolean, default=True)
    # v0.4.4 · the GeneratedJournalEntry holding this account's opening balance —
    # what it already held on the day it was linked (see app/opening_balance.py).
    # Denormalized so the Accounts page can render an "Opening Balance" column
    # without a join; the authoritative lookup is still the synthetic
    # `opening-balance:<account_id>` key on that table, which is UNIQUE and is
    # therefore what actually prevents double-booking. NULL = never booked, which
    # is every account on a pre-v0.4.4 install (the backfill script fixes those).
    #
    # Deliberately NOT a db.ForeignKey, matching bank_transactions.
    # intercompany_pair_id: rejecting an opening balance KEEPS the row as the
    # record of that decision, so the two lifetimes are independent.
    opening_balance_je_id = db.Column(db.Integer, nullable=True, index=True)
    # v0.4.11 · when this account's configuration was HANDED OVER to a
    # re-linked replacement, the account_id that took it. Plaid mints new
    # account_ids under a new Item, so a re-link produces a fresh row for the
    # same real-world account; fingerprint adoption moves the mapping across
    # (see app/reconnect.py) and stamps the donor here.
    #
    # The mapping is MOVED, not copied, and this column is what records that.
    # Two rows pointing at one ERPNext Bank Account would both push the same
    # transactions into it — so the donor is retired as part of the same
    # operation, and this is the audit trail of where its identity went.
    superseded_by_account_id = db.Column(db.String(120), nullable=True,
                                         index=True)
    # v0.4.12 · mark-to-market for balance-only investment accounts. The GL leaf
    # only ever held the OPENING value — refreshed balances went to an
    # informational custom field on the Bank Account, never to a posting — so a
    # brokerage that grew from $50k to $65k showed $50k on the balance sheet
    # indefinitely.
    #
    # `last_revalued_balance` is the value the LEDGER currently reflects, not the
    # latest balance Plaid reported. That distinction is the whole mechanism:
    # each revaluation posts only the DELTA between the two, so the entries
    # compose instead of double-counting. NULL = never revalued, which is when
    # the baseline is seeded (see revaluation.baseline_for) rather than posted.
    last_revalued_balance = db.Column(db.Float, nullable=True)
    last_revalued_at = db.Column(db.DateTime, nullable=True)
    # v0.4.14 · Plaid /liabilities/get detail for a loan account, stored as the
    # JSON Plaid sent plus the two figures that drive accounting.
    #
    # ONE JSON COLUMN rather than fifteen promoted ones, deliberately. The three
    # liability shapes (mortgage / student / credit) each carry fields the others
    # don't — escrow balance, PMI, PSLF status, loan term, property address —
    # and promoting the union would be a wide, mostly-NULL table for data only a
    # human reads. Postgres can query into the JSON if a real use appears. That
    # is "maximal data science, avoid table sprawl" applied to a column, not
    # just to a table.
    liability_detail = db.Column(db.Text, nullable=True)
    liability_refreshed_at = db.Column(db.DateTime, nullable=True)
    # The year-to-date interest and principal figures already BOOKED, not the
    # latest ones Plaid reported. Same distinction as last_revalued_balance:
    # each accrual posts only the gap between the two, so entries compose
    # instead of re-posting the whole year every sync. NULL = never accrued,
    # which is when the baseline is seeded rather than posted (see
    # loans.accrue_interest).
    loan_ytd_interest_booked = db.Column(db.Float, nullable=True)
    loan_ytd_principal_seen = db.Column(db.Float, nullable=True)
    # One-click-import lifecycle (see app/erpnext_accounts.py):
    #   pending     — never auto-imported (the default / freshly linked)
    #   imported    — a matching ERPNext Bank Account was created/found + linked
    #   unsupported — the Plaid type/subtype isn't a Bank Account in ERPNext's
    #                 model (loans, investments, 401k, …) so no button is offered
    #   superseded  — v0.4.11: this row's configuration was handed to a
    #                 re-linked replacement (see superseded_by_account_id). The
    #                 row stays queryable as history; it just no longer syncs.
    # Left deliberately un-constrained (like plaid_sync_log) so a future status
    # never needs a migration — which is exactly what let 'superseded' be added
    # in v0.4.11 with no constraint change.
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
            'owning_company': self.owning_company,
            'balance_only': bool(self.balance_only),
            'sync_enabled': bool(self.sync_enabled),
            'opening_balance_je_id': self.opening_balance_je_id,
            'import_status': self.import_status or 'pending',
            'superseded_by_account_id': self.superseded_by_account_id,
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
    # v0.4.1 · the IntercompanyTransferPair this transaction belongs to, once the
    # detector has matched it against its counterparty in ANOTHER Company's linked
    # account (see app/intercompany.py). NULL = not paired, which is every
    # transaction on a single-Company install — the normal rules engine then runs
    # exactly as it did pre-v0.4.1. A non-NULL value makes the transaction
    # ineligible for ordinary categorization (rules with `ignore_for_paired`) and
    # routes it to the paired Due from / Due to Journal Entries instead.
    #
    # Deliberately NOT a db.ForeignKey: an Unpair clears this back to NULL while
    # the pair row is KEPT (state='rejected') as the suppression record that stops
    # the detector immediately re-pairing the same two rows, so the two lifetimes
    # are independent by design.
    intercompany_pair_id = db.Column(db.Integer, nullable=True, index=True)
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
            'intercompany_pair_id': self.intercompany_pair_id,
            'erpnext_bank_transaction_id': self.erpnext_bank_transaction_id,
            'posted_at': self.posted_at.isoformat() if self.posted_at else None,
            'removed': bool(self.removed), 'sync_error': self.sync_error,
        }

    # ── v0.3.2 · autocomplete feeds for the rule builder ──────────────
    # These aggregate the local transaction mirror so the /admin/rules form can
    # offer merchants + categories the operator has actually seen, instead of
    # asking them to type merchant names from memory.

    @classmethod
    def known_merchants(cls, limit: int = 200) -> list:
        """Distinct merchants seen locally, most-frequent first:
        [{'name', 'count', 'total_amount', 'category'}]. `total_amount` is the
        summed absolute spend (Plaid amounts are positive = outflow) so the UI
        can show a real dollar figure; `category` is the merchant's most common
        Plaid category (for the Name suggestion). Non-removed rows only."""
        from sqlalchemy import func
        rows = (db.session.query(
                    cls.merchant_name,
                    func.count(cls.id),
                    func.coalesce(func.sum(func.abs(cls.amount)), 0.0))
                .filter(cls.merchant_name.isnot(None),
                        cls.merchant_name != '',
                        cls.removed.is_(False))
                .group_by(cls.merchant_name)
                .order_by(func.count(cls.id).desc(), cls.merchant_name.asc())
                .limit(limit).all())
        merchants = [{'name': name, 'count': int(count or 0),
                      'total_amount': round(float(total or 0.0), 2),
                      'category': ''} for name, count, total in rows]
        if not merchants:
            return merchants
        # Dominant category per merchant (one extra grouped pass), then attach.
        names = {m['name'] for m in merchants}
        cat_rows = (db.session.query(
                        cls.merchant_name, cls.category, func.count(cls.id))
                    .filter(cls.merchant_name.in_(names),
                            cls.category.isnot(None), cls.category != '',
                            cls.removed.is_(False))
                    .group_by(cls.merchant_name, cls.category)
                    .order_by(func.count(cls.id).desc()).all())
        dominant = {}
        for name, cat, _cnt in cat_rows:
            dominant.setdefault(name, cat)     # first seen = highest count
        for m in merchants:
            m['category'] = dominant.get(m['name'], '')
        return merchants

    @classmethod
    def known_categories(cls, limit: int = 200) -> list:
        """Distinct Plaid categories seen locally, most-frequent first:
        [{'path', 'count'}]. The stored string (a 'A > B > C' path or a raw PFC
        label) is preserved verbatim — the UI shows the full hierarchy."""
        from sqlalchemy import func
        rows = (db.session.query(cls.category, func.count(cls.id))
                .filter(cls.category.isnot(None), cls.category != '',
                        cls.removed.is_(False))
                .group_by(cls.category)
                .order_by(func.count(cls.id).desc(), cls.category.asc())
                .limit(limit).all())
        return [{'path': path, 'count': int(count or 0)} for path, count in rows]


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


class Customer(db.Model):
    """Local mirror / cache of a SELL-SIDE party seen on Plaid transactions →
    the ERPNext Customer it maps to (v0.4.0.8). The AR-side twin of `Supplier`,
    with the same shape and the same job: a cheap local lookup that
    short-circuits the auto-create before touching ERPNext.

    A separate table rather than a `party_type` discriminator on `suppliers`
    precisely BECAUSE of dual-role parties: Wells Fargo pays you interest (a
    Customer) and charges you fees (a Supplier), so the same normalized name has
    to exist on both sides at once. `suppliers.normalized_name` is UNIQUE, so
    one table could never hold both roles for one name — and each side keeps its
    own independent AR / AP ledger in ERPNext anyway.

    `erpnext_customer_name` is the ERPNext Customer docname once resolved (NULL
    until then, e.g. if ERPNext wasn't reachable at push time)."""
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True)
    merchant_name = db.Column(db.String(255), default='', index=True)
    normalized_name = db.Column(db.String(255), unique=True, nullable=False,
                                index=True)
    erpnext_customer_name = db.Column(db.String(255), nullable=True, index=True)
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
            'erpnext_customer_name': self.erpnext_customer_name,
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

    `match_type` + `match_value` describe the predicate.

    v0.3.1 · bank-account-agnostic rules: a rule only names the OFFSET side —
    `offset_account` (the expense/income/party GL account it categorizes to).
    The BANK side is taken from the transaction's own linked Plaid account
    (PlaidAccount.erpnext_gl_account_name), so one rule works across every
    account. `offset_direction` decides which side the offset lands on:
      * 'auto'          — infer from the Plaid amount sign (withdrawal → offset
                          is debited; deposit/refund → offset is credited);
      * 'always_debit'  — force the offset to the debit side (rare: reversals);
      * 'always_credit' — force the offset to the credit side (rare).

    `debit_account` / `credit_account` are the DEPRECATED pre-v0.3.1 pair (both
    accounts named on the rule). They're kept for one release cycle for
    backwards compatibility: a rule with no `offset_account` still generates a JE
    from the old pair (see app/categorization.py). The boot migration backfills
    `offset_account` from them (app/migrations.py).

    `party_type` / `party_name` optionally link a Supplier /
    Customer on the offset line (party_name blank → the auto-created Supplier
    for the transaction's merchant is used). `description_template` is a plain
    `{{variable}}` string rendered into the JE's user_remark (v0.4.0.4; auto-filled
    from the match type + offset account on the Rules editor). Deliberately
    un-constrained on match_type so a new predicate never needs a migration."""
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
    # v0.3.1 · the categorized (non-bank) side; the bank side comes from the txn.
    offset_account = db.Column(db.String(255), default='')
    # auto | always_debit | always_credit
    offset_direction = db.Column(db.String(20), default='auto')
    # DEPRECATED (pre-v0.3.1) — kept one release for backwards compat.
    debit_account = db.Column(db.String(255), default='')
    credit_account = db.Column(db.String(255), default='')
    # '' / NULL = no Party on the JE | Supplier | Customer | Auto.
    #
    # v0.4.0.8 · 'Auto' derives the side from the OFFSET ACCOUNT's root_type at
    # JE time — an Income offset is money coming IN, so the counterparty is a
    # Customer (a fruit buyer, USDA, a grant); an Expense offset is money going
    # OUT, so it's a Supplier. Any other root_type (Asset/Liability/Equity —
    # typically a transfer between accounts you own) books NO party. Deriving
    # from the account rather than the Plaid amount sign is deliberate: the sign
    # convention is ambiguous across refunds, reversals and the
    # always_debit/always_credit overrides, whereas the offset account is the
    # operator's own explicit statement of what the transaction IS.
    #
    # A literal 'Supplier' / 'Customer' remains an override that wins over the
    # derivation, so an operator can force the side when their chart doesn't
    # follow the usual root_type convention.
    party_type = db.Column(db.String(20), nullable=True)
    party_name = db.Column(db.String(255), nullable=True)
    # v0.4.0.7 · omit the Party from the generated JE entirely. ERPNext treats
    # Party as optional on a JE row, and a transfer between two accounts you own
    # (a credit-card payment, a deposit, an inter-account move) has no
    # counterparty worth booking — naming one just mints a junk Supplier. The
    # Rules editor pre-checks this when the offset resolves to another Bank
    # Account of the same Company (categorization.suggest_skip_party); the
    # operator can always override. Defaults False, so every pre-v0.4.0.7 rule
    # keeps its current party behaviour.
    skip_party = db.Column(db.Boolean, default=False)
    # v0.4.1 · don't fire this rule on a transaction the intercompany detector has
    # paired (see app/intercompany.py). Defaults TRUE — and that default is the
    # whole point of the flag: a transfer between two Companies you own is not
    # revenue or expense, so a generic "Transfer" rule matching it would book P&L
    # activity that isn't real. A paired transaction gets the Due from / Due to
    # entry pair instead.
    #
    # An operator can clear it per rule for the rare case where a rule really
    # should still fire on paired transactions (e.g. booking a wire fee that rides
    # along with the transfer). Defaulting to True rather than making it
    # unconditional keeps that escape hatch open without anyone having to opt in
    # to correct behaviour.
    ignore_for_paired = db.Column(db.Boolean, default=True)
    description_template = db.Column(db.Text, default='')
    # v0.4.0.1 · multi-entity rule scoping: when set, the rule only fires for a
    # transaction whose linked Plaid account resolves to this ERPNext Company.
    # NULL/'' = company-agnostic (applies to every Company) — the default, so
    # pre-multi-entity rules keep matching everywhere with no change.
    #
    # v0.4.0.3 · this field ALSO selects how `offset_account` is interpreted (the
    # rule's "offset mode" is inferred, not stored):
    #   * SCOPED  (applies_to_company set)  → Mode A: `offset_account` is a
    #     specific, fully-qualified GL account docname ('Meals & Entertainment -
    #     BBT'); used as-is at JE time.
    #   * AGNOSTIC (applies_to_company NULL) → Mode B: `offset_account` is a
    #     LOGICAL account name ('Meals & Entertainment') that is resolved to the
    #     transaction's own Company's chart at JE time (see
    #     categorization.resolve/build_journal_entry). One agnostic rule thus
    #     books to each Company's own Meals account. A boot migration converts a
    #     legacy agnostic rule's fully-qualified offset to a logical name
    #     (app/migrations._migrate_agnostic_offset_to_logical).
    applies_to_company = db.Column(db.String(140), nullable=True, index=True)
    # Non-destructive history (v0.3.0 audit): a rule is never mutated in place or
    # hard-deleted. An EDIT clones the rule and points the old row's
    # `superseded_by` at the new id (both archived=inactive); a DELETE just sets
    # `archived`. The rules engine only ever sees active + non-archived rules, so
    # a past auto-JE decision can always be reconstructed from the archived row.
    superseded_by = db.Column(db.Integer, nullable=True, index=True)
    archived = db.Column(db.Boolean, default=False, index=True)
    # v0.4.6 · cached count of the transactions this rule has actually matched,
    # refreshed by the daily rollup (app/rule_stats.rollup_match_counts) rather
    # than recomputed per page load. A zero here is the signal the Rules list is
    # built around: the rule is either dead or scoped too narrowly to ever fire.
    #
    # The rollup credits an ARCHIVED version's matches to the live rule that
    # superseded it (it walks `superseded_by` forward). Without that, every edit
    # — which clones the rule by design — would reset the column to 0 and make a
    # working rule look dead, which is the exact opposite of what it's for.
    match_count = db.Column(db.Integer, default=0, index=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    @classmethod
    def merchants_with_rules(cls) -> list:
        """The (match_type, match_value) of every ACTIVE, non-archived merchant
        rule — feeds the "already has a rule" badge on the merchant autocomplete
        (v0.3.2). Returns [{'match_type', 'match_value'}]; the caller decides
        overlap (exact vs contains) against each known merchant."""
        rows = (cls.query
                .filter(cls.active.is_(True), cls.archived.is_(False),
                        cls.match_type.in_(('merchant_exact', 'merchant_contains')))
                .all())
        return [{'match_type': r.match_type,
                 'match_value': (r.match_value or '')} for r in rows]

    def to_dict(self):
        return {
            'id': self.id, 'priority': self.priority, 'active': bool(self.active),
            'name': self.name, 'match_type': self.match_type,
            'match_value': self.match_value,
            'offset_account': self.offset_account or '',
            'offset_direction': self.offset_direction or 'auto',
            'debit_account': self.debit_account,
            'credit_account': self.credit_account,
            'party_type': self.party_type, 'party_name': self.party_name,
            'skip_party': bool(self.skip_party),
            'ignore_for_paired': bool(self.ignore_for_paired),
            'description_template': self.description_template,
            'applies_to_company': self.applies_to_company or None,
            'superseded_by': self.superseded_by,
            'archived': bool(self.archived),
            'match_count': int(self.match_count or 0),
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
    # pending_review | approved | rejected | error | blocked |
    # skipped_missing_account — left un-constrained (like plaid_sync_log) so a
    # future state never needs a migration. Widened to 40 in v0.4.0.3 because
    # 'skipped_missing_account' (23 chars) overflows the original VARCHAR(20)
    # (see SCHEMA/_widen_column in app/migrations.py).
    state = db.Column(db.String(40), default='pending_review', index=True)
    # Denormalized snapshot for the audit dashboard (so it renders without a
    # join back to bank_transactions / categorization_rules).
    amount = db.Column(db.Float, default=0.0)
    merchant_name = db.Column(db.String(255), default='')
    description = db.Column(db.Text, default='')
    rule_name = db.Column(db.String(255), default='')
    error_message = db.Column(db.Text, nullable=True)
    # v0.4.1 · when this JE is one half of an intercompany transfer, the
    # IntercompanyTransferPair it belongs to. Denormalized (the pair already names
    # both JE docnames) so the Generated JEs list and any report can tell a
    # Due-from/Due-to entry apart from a rules-engine one without a join, and so a
    # sibling lookup is one indexed query. NULL for every ordinary rule-generated
    # JE, which is all of them on a single-Company install.
    intercompany_pair_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id, 'plaid_transaction_id': self.plaid_transaction_id,
            'rule_id': self.rule_id,
            'erpnext_journal_entry_name': self.erpnext_journal_entry_name,
            'intercompany_pair_id': self.intercompany_pair_id,
            'state': self.state, 'amount': self.amount or 0.0,
            'merchant_name': self.merchant_name, 'description': self.description,
            'rule_name': self.rule_name, 'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class IntercompanyTransferPair(db.Model):
    """One detected transfer between two ERPNext Companies the operator owns
    (v0.4.1). See app/intercompany.py for the detector and the JE generator.

    When money moves from a Farm account to a Personal account, Plaid delivers
    TWO transactions — one on each side, equal magnitude and opposite sign. Left
    alone, a generic rule books the outflow as an expense on the Farm's P&L and
    the inflow as income on Personal's, so both sets of books show activity that
    isn't real revenue or expense. This row records that the two are one
    movement, and drives the paired Due from / Due to Journal Entries that book
    it correctly on the balance sheet instead.

    DIRECTION IS FIXED BY THE MONEY, not by discovery order:
    `from_transaction_id` is always the money-OUT side (Plaid amount > 0, the
    source Company) and `to_transaction_id` always the money-IN side. Every
    consumer relies on that — the generator debits `Due from {to_company}` on the
    source and credits `Due to {from_company}` on the target — so the detector
    normalizes the orientation before ever creating a row.

    Both transaction columns hold a `plaid_transaction_id`, matching the natural
    key `GeneratedJournalEntry` already uses, so the pair survives the local rows
    being re-upserted by a Plaid `modified` delivery.

    `state` moves pending → approved | rejected. `rejected` is TERMINAL and
    load-bearing: an Unpair keeps the row precisely so the detector can see that
    these two transactions were already considered and rejected, and doesn't
    re-pair them on the very next sync (see intercompany._suppressed_keys)."""
    __tablename__ = 'intercompany_transfer_pairs'
    id = db.Column(db.Integer, primary_key=True)
    # The money-OUT (source) transaction and its Company.
    from_transaction_id = db.Column(db.String(120), nullable=False, index=True)
    from_company = db.Column(db.String(140), default='')
    # The money-IN (target) transaction and its Company.
    to_transaction_id = db.Column(db.String(120), nullable=False, index=True)
    to_company = db.Column(db.String(140), default='')
    # Magnitude of the transfer (both sides share it, by construction).
    amount = db.Column(db.Float, default=0.0)
    # 0.0-1.0 — see intercompany.score_pair. Stored so the review UI can sort and
    # filter on it, and so a later threshold change doesn't rewrite history.
    confidence = db.Column(db.Float, default=0.0)
    # The two generated Journal Entries, once booked (both, or neither — see
    # intercompany.generate_pair_journal_entries).
    from_journal_entry = db.Column(db.String(255), nullable=True, index=True)
    to_journal_entry = db.Column(db.String(255), nullable=True, index=True)
    # pending | approved | rejected. Left un-constrained (like plaid_sync_log and
    # generated_journal_entries) so a future state never needs a migration.
    state = db.Column(db.String(20), default='pending', index=True)
    # Free-text operator-facing detail — a JE generation failure, or the reason a
    # detected pair couldn't supersede an already-approved rules-engine JE.
    note = db.Column(db.Text, nullable=True)
    detected_at = db.Column(db.DateTime, default=_now, index=True)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id,
            'from_transaction_id': self.from_transaction_id,
            'from_company': self.from_company or '',
            'to_transaction_id': self.to_transaction_id,
            'to_company': self.to_company or '',
            'amount': self.amount or 0.0,
            'confidence': round(float(self.confidence or 0.0), 4),
            'from_journal_entry': self.from_journal_entry,
            'to_journal_entry': self.to_journal_entry,
            'state': self.state, 'note': self.note,
            'detected_at': self.detected_at.isoformat() if self.detected_at else None,
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


class PlaidStatement(db.Model):
    """One bank-issued statement pulled from Plaid's /statements API (v0.4.9).

    A statement is the only thing in this system that carries a number the BANK
    asserts rather than one Bank Bridge derived. That makes it useful twice:

      1. as an ANCHOR for an opening balance. `opening_balance` is what the
         account held at `period_start` according to the institution — which
         beats `opening_balance.estimate_opening_balance`'s
         current-balance-minus-mirrored-movement arithmetic, because the
         estimate is only exact if the mirror is complete (see that function's
         docstring, and statements.choose_anchor_statement for the check that
         decides whether a given statement is safe to anchor on).
      2. as a RECONCILIATION checkpoint. `closing_balance` is a monthly
         assertion the mirror can be measured against without opening ERPNext —
         opening + Σ(mirrored movement in the period) should land on closing,
         and a delta means the mirror has a gap (see statements.reconcile).

    `statement_id` is Plaid's opaque identifier and is UNIQUE — that column IS
    the idempotency guard, so a re-run of the monthly job, the backfill script
    and the import path all converge on one row per statement rather than
    re-downloading a PDF we already hold.

    `opening_balance` / `closing_balance` are NULLABLE and routinely NULL: Plaid
    returns statement balances only inside the PDF, never as structured fields
    (/statements/list carries statement_id, month, year and date_posted, and
    nothing else), so both are recovered by regex over extracted PDF text and a
    layout we cannot parse yields NULL rather than a guess. Every consumer
    treats NULL as "no bank-issued figure available" and falls back — an
    unparseable statement degrades to v0.4.4 behaviour, it does not corrupt one.

    `pdf_path` is where the bytes landed on disk. It may point at a file that no
    longer exists (a wiped data volume); the fetch path re-downloads on that
    case rather than trusting the row alone."""
    __tablename__ = 'plaid_statements'
    id = db.Column(db.Integer, primary_key=True)
    # Plaid's opaque statement identifier — the natural key, and the reason a
    # duplicate fetch is a no-op instead of a second PDF.
    statement_id = db.Column(db.String(255), unique=True, nullable=False,
                             index=True)
    plaid_item_id = db.Column(db.String(120), db.ForeignKey('plaid_items.item_id'),
                              nullable=False, index=True)
    plaid_account_id = db.Column(db.String(120),
                                 db.ForeignKey('plaid_accounts.account_id'),
                                 nullable=False, index=True)
    # The statement period. Derived from Plaid's month + year (the only period
    # fields /statements/list returns) as the first and last day of that month.
    period_start = db.Column(db.Date, nullable=True, index=True)
    period_end = db.Column(db.Date, nullable=True, index=True)
    # Bank-asserted balances, parsed out of the PDF text. NULL = not parseable.
    # On a brokerage statement these are the CASH side (cash and sweep
    # balances), not total account value — that is the only figure the mirror
    # can reconcile against, since signed_movement sums cash events and has no
    # record of market appreciation. See statements._parse_wf_advisors.
    opening_balance = db.Column(db.Float, nullable=True)
    closing_balance = db.Column(db.Float, nullable=True)
    # v0.4.41 — total account value (cash plus securities at market) where the
    # statement states it. NULL on a depository/credit statement, which has no
    # such figure, and on any brokerage layout we couldn't read. Never anchored
    # on and never reconciled against; it is what an operator recognises from
    # the statement's front page, and the figure mark-to-market (v0.4.12) moves.
    portfolio_opening_value = db.Column(db.Float, nullable=True)
    portfolio_closing_value = db.Column(db.Float, nullable=True)
    # Which recognizer produced the balances above — 'wf_advisors', 'labels',
    # 'labels_flat', or '' for none. A surprising figure is only auditable if
    # the rule that found it is recorded next to it.
    parse_method = db.Column(db.String(40), default='')
    # Set when this statement's opening balance does NOT equal the previous
    # month's closing (see statements.flag_parse_continuity). A statement is a
    # chain — one month's close is the next month's open — so a break is either
    # a misparse or a real gap in the documents, and either way the number
    # should not be anchored on without a human looking. Never blocks anything
    # by itself; choose_anchor_statement's own reconciliation check still rules.
    parse_suspect = db.Column(db.Boolean, default=False)
    # EVERYTHING the parser recovered, as JSONB: every summary figure the
    # statement states (deposits, withdrawals, dividends, interest, fees,
    # realized/unrealized gains, securities bought and sold, minimum payment
    # due …) plus the parse's own provenance — `parser_version`, `layout`,
    # `verified`, `fields_failed`, and the period the document states for
    # itself. See statements.parse_statement for the full contract.
    #
    # ONE COLUMN, NOT A TABLE, and not thirty columns. The fields differ per
    # institution and per account type and will keep growing; modelling them
    # relationally would be table sprawl in exchange for nothing, since no
    # query joins on "dividends in March". The handful that ARE queried
    # (opening/closing balance, portfolio value) are promoted to real columns
    # above and written from this blob in statements.apply_parse, so the two
    # can never disagree.
    #
    # `parser_version` is what makes a re-parse safe to defer: an operator can
    # see which rows a newer recognizer has not reached yet, and re-read those
    # PDFs without re-downloading a thing.
    parsed_metadata = db.Column(
        MutableDict.as_mutable(
            db.JSON().with_variant(JSONB, 'postgresql')),
        nullable=True)
    # Absolute path of the stored PDF; '' when the download never succeeded.
    pdf_path = db.Column(db.Text, default='')
    # Size of the stored PDF in bytes — lets the admin list show "is there
    # really a file there" without stat-ing every row.
    pdf_bytes = db.Column(db.Integer, default=0)
    fetched_at = db.Column(db.DateTime, default=_now, index=True)
    # v0.4.10 — the ERPNext Bank Statement record this row was uploaded to, and
    # when it last synced. NULL/'' means "not in ERPNext yet", which is what the
    # sync pass selects on, so an install that upgrades mid-life picks up every
    # statement it already holds on the next scheduler tick.
    #
    # This is a CACHE of ERPNext's answer, not the authority: the authority is
    # the unique `plaid_statement_id` on the ERPNext side. A data volume
    # restored from a backup taken before an upload has this blank for records
    # that do exist, and the sync re-adopts them by probing rather than
    # creating a duplicate (see erpnext_statements.sync_statement).
    erpnext_docname = db.Column(db.String(255), nullable=True, index=True)
    erpnext_synced_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def period_label(self) -> str:
        """'2026-07' — the period key the PDF is filed under on disk."""
        if self.period_start:
            return f'{self.period_start.year:04d}-{self.period_start.month:02d}'
        return ''

    def metadata_dict(self) -> dict:
        """`parsed_metadata` as a plain dict, {} when absent or malformed.
        Never raises: this feeds a template, and a row written by a future
        version (or edited by hand) must not be able to 500 a page."""
        value = self.parsed_metadata
        return dict(value) if isinstance(value, dict) else {}

    def parser_version(self) -> str:
        """Which build of the parser produced this row's figures, '' if it
        predates versioning. What tells a stale parse from a current one."""
        return str(self.metadata_dict().get('parser_version') or '')

    def to_dict(self):
        return {
            'id': self.id, 'statement_id': self.statement_id,
            'plaid_item_id': self.plaid_item_id,
            'plaid_account_id': self.plaid_account_id,
            'period_start': self.period_start.isoformat() if self.period_start else None,
            'period_end': self.period_end.isoformat() if self.period_end else None,
            'period_label': self.period_label(),
            'opening_balance': self.opening_balance,
            'closing_balance': self.closing_balance,
            'portfolio_opening_value': self.portfolio_opening_value,
            'portfolio_closing_value': self.portfolio_closing_value,
            'parse_method': self.parse_method or '',
            'parse_suspect': bool(self.parse_suspect),
            'parsed_metadata': self.parsed_metadata or {},
            'pdf_path': self.pdf_path or '',
            'pdf_bytes': int(self.pdf_bytes or 0),
            'fetched_at': self.fetched_at.isoformat() if self.fetched_at else None,
            'erpnext_docname': self.erpnext_docname or '',
            'erpnext_synced_at': (self.erpnext_synced_at.isoformat()
                                  if self.erpnext_synced_at else None),
        }


class StatementAdjustment(db.Model):
    """One manual attribution of an unreconciled statement delta to a
    specific offset account (v0.4.39).

    A statement often shows a reconciliation delta ("off by $X") because
    Plaid didn't ship a transaction for something that DID move the bank
    balance — a wire transfer to a tax authority, a member distribution,
    a fee not captured in /transactions/sync, an inter-brokerage transfer,
    etc. This model lets the operator attribute portions of that delta to
    specific accounts + descriptions so the statement's residual delta
    approaches zero as adjustments are recorded.

    Multiple adjustments per statement are allowed — a single month's
    unreconciled amount might be composed of several distinct events (a
    tax payment PLUS a rebalance PLUS a fee).

    erpnext_je_name is optional: an adjustment can be recorded here as
    documentation-only (Tim knows what the delta was for) OR it can be
    tied to an actual ERPNext Journal Entry once created. The dashboard
    treats attributed-but-not-posted the same as posted for the delta
    math — the reconciliation reflects the operator's stated intent."""
    __tablename__ = 'statement_adjustments'
    id = db.Column(db.Integer, primary_key=True)
    statement_id = db.Column(db.Integer,
                             db.ForeignKey('plaid_statements.id'),
                             nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    offset_account = db.Column(db.String(255), nullable=False, default='')
    description = db.Column(db.Text, default='')
    erpnext_je_name = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id, 'statement_id': self.statement_id,
            'amount': self.amount, 'offset_account': self.offset_account,
            'description': self.description,
            'erpnext_je_name': self.erpnext_je_name,
            'created_at': (self.created_at.isoformat()
                           if self.created_at else None),
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


# ── v0.4.27 · investments (v0.5.0 Phase A) ─────────────────────────────────
#
# Three models power the investment portfolio tracker: Security (the
# instrument), SecurityHolding (a position at a specific Plaid account, snapshot
# per sync), and SecurityTransaction (a trade/dividend/split/transfer event
# from Plaid's /investments/transactions/get). Splitting Security from Holding
# mirrors Plaid's response shape and lets one Security row serve every account
# holding it — a single AAPL Security backs OM's brokerage and BBT's IRA
# without duplication.
#
# Option contracts are stored inline as flat columns on Security (is_option,
# option_strike_price, etc.) rather than a JSON blob or a separate options
# table, because every downstream query touches at least one of those fields
# and JSON access on Postgres, while capable, is slower and harder to index.


class Security(db.Model):
    """One instrument — an equity, ETF, mutual fund, bond, option contract,
    etc. — as identified by Plaid's stable security_id. Populated + updated on
    every /investments/holdings/get and /investments/transactions/get response
    (both endpoints ship the securities list redundantly).

    Options are stored INLINE via flat columns rather than a separate
    OptionsContract table because every Phase B / C query filters on the
    option-specific fields; JSON storage would work but requires GIN indexes
    for the same queries flat columns handle natively.

    `ticker_symbol` is a string and can be empty for securities where Plaid
    lacks that field (some mutual funds, some private placements). Downstream
    code MUST tolerate blank tickers — display code falls back to `name`."""
    __tablename__ = 'securities'
    id = db.Column(db.Integer, primary_key=True)
    security_id = db.Column(db.String(120), unique=True, nullable=False,
                            index=True)
    ticker_symbol = db.Column(db.String(50), default='', index=True)
    name = db.Column(db.String(500), default='')
    type = db.Column(db.String(60), default='')
    iso_currency_code = db.Column(db.String(8), default='USD')
    cusip = db.Column(db.String(30), default='')
    isin = db.Column(db.String(30), default='')
    sedol = db.Column(db.String(30), default='')
    # Plaid's own last-known institutional close price, refreshed on every
    # holdings pull. TradingView is authoritative for real-time; this field is
    # the "as-of the last sync" snapshot for balance-sheet purposes.
    close_price = db.Column(db.Float, nullable=True)
    close_price_as_of = db.Column(db.Date, nullable=True)
    # Options metadata, present only when Plaid classifies the security as an
    # options contract. is_option is denormalized off the presence of the
    # option fields so the '/admin/holdings' filter is a cheap index lookup.
    is_option = db.Column(db.Boolean, default=False, index=True)
    option_contract_type = db.Column(db.String(10), nullable=True)  # call|put
    option_strike_price = db.Column(db.Float, nullable=True)
    option_expiration_date = db.Column(db.Date, nullable=True)
    option_underlying_ticker = db.Column(db.String(50), nullable=True,
                                         index=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id, 'security_id': self.security_id,
            'ticker_symbol': self.ticker_symbol, 'name': self.name,
            'type': self.type,
            'iso_currency_code': self.iso_currency_code,
            'cusip': self.cusip, 'isin': self.isin, 'sedol': self.sedol,
            'close_price': self.close_price,
            'close_price_as_of': (self.close_price_as_of.isoformat()
                                  if self.close_price_as_of else None),
            'is_option': bool(self.is_option),
            'option_contract_type': self.option_contract_type,
            'option_strike_price': self.option_strike_price,
            'option_expiration_date': (self.option_expiration_date.isoformat()
                                       if self.option_expiration_date
                                       else None),
            'option_underlying_ticker': self.option_underlying_ticker,
        }


class SecurityHolding(db.Model):
    """One position at one Plaid account, from the last
    /investments/holdings/get response. Snapshot-per-sync: each sync replaces
    the row for a given (account_id, security_id) pair, so this table always
    reflects the current state. History lives in SecurityTransaction — this
    is 'what you have RIGHT NOW'.

    `quantity` can be negative: Plaid encodes short positions (written calls
    or puts) as negative quantity. The naked-position safety check in Phase B
    reads this field."""
    __tablename__ = 'security_holdings'
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.String(120),
                           db.ForeignKey('plaid_accounts.account_id'),
                           nullable=False, index=True)
    security_id = db.Column(db.String(120),
                            db.ForeignKey('securities.security_id'),
                            nullable=False, index=True)
    quantity = db.Column(db.Float, nullable=False, default=0.0)
    cost_basis = db.Column(db.Float, nullable=True)
    # Plaid's most recent price for this security at this institution + the
    # timestamp of that price. `institution_value` = quantity * price, cached
    # so the dashboard doesn't recompute per row.
    institution_price = db.Column(db.Float, nullable=True)
    institution_price_as_of = db.Column(db.Date, nullable=True)
    institution_value = db.Column(db.Float, nullable=True)
    iso_currency_code = db.Column(db.String(8), default='USD')
    refreshed_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    __table_args__ = (
        db.UniqueConstraint('account_id', 'security_id',
                            name='ux_security_holdings_account_security'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'account_id': self.account_id,
            'security_id': self.security_id,
            'quantity': self.quantity, 'cost_basis': self.cost_basis,
            'institution_price': self.institution_price,
            'institution_price_as_of': (
                self.institution_price_as_of.isoformat()
                if self.institution_price_as_of else None),
            'institution_value': self.institution_value,
            'iso_currency_code': self.iso_currency_code,
            'refreshed_at': (self.refreshed_at.isoformat()
                             if self.refreshed_at else None),
        }


class SecurityTransaction(db.Model):
    """One Plaid /investments/transactions/get row — a buy, sell, dividend,
    split, transfer, assignment, or any other investment event with a full
    security detail. Unique on `plaid_investment_transaction_id` so a re-pull
    is idempotent.

    Cash movements (`type='cash', subtype='dividend'` etc.) are stored here
    too, with `security_id` referencing the paying security. Portfolio cash
    balance tracking (Fee Account 6030) still runs through the regular
    BankTransaction mirror — this table adds security-attribution to those
    events, it doesn't replace them."""
    __tablename__ = 'security_transactions'
    id = db.Column(db.Integer, primary_key=True)
    plaid_investment_transaction_id = db.Column(
        db.String(120), unique=True, nullable=False, index=True)
    account_id = db.Column(db.String(120),
                           db.ForeignKey('plaid_accounts.account_id'),
                           nullable=False, index=True)
    security_id = db.Column(db.String(120),
                            db.ForeignKey('securities.security_id'),
                            nullable=True, index=True)
    date = db.Column(db.Date, nullable=True, index=True)
    name = db.Column(db.String(500), default='')
    # `quantity` is contracts for options, shares for equities; Plaid uses
    # positive for buys and negative for sells (consistent with the
    # transactions_sync convention on BankTransaction.amount).
    quantity = db.Column(db.Float, nullable=False, default=0.0)
    # Signed cash impact: positive when money left the account (a buy),
    # negative when it entered (a sell or dividend received).
    amount = db.Column(db.Float, nullable=False, default=0.0)
    price = db.Column(db.Float, nullable=False, default=0.0)
    fees = db.Column(db.Float, nullable=True)
    # Plaid's classification. `type` is one of {buy, sell, cash, transfer,
    # fee, cancel}; `subtype` names the event within that type
    # ('cash/dividend', 'buy/buy', 'sell/sell', 'transfer/deposit',
    # 'cash/interest', 'fee/miscellaneous fee', etc.). Both preserved verbatim
    # for the Phase B 5:4 detector.
    type = db.Column(db.String(60), default='', index=True)
    subtype = db.Column(db.String(80), default='')
    iso_currency_code = db.Column(db.String(8), default='USD')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id,
            'plaid_investment_transaction_id':
                self.plaid_investment_transaction_id,
            'account_id': self.account_id, 'security_id': self.security_id,
            'date': self.date.isoformat() if self.date else None,
            'name': self.name, 'quantity': self.quantity,
            'amount': self.amount, 'price': self.price, 'fees': self.fees,
            'type': self.type, 'subtype': self.subtype,
            'iso_currency_code': self.iso_currency_code,
        }


# ── v0.4.31 · v0.5.0 Phase B: 5:4 lot tracker + options overlay ─────────────
#
# Four models power the strategy tracker:
#
#   RetainedLot     — a lot the user intends to hold. The 20% of every 5:4
#                     cycle that stays after the sell, PLUS anything manually
#                     tagged as retained (a bought position with no matching
#                     sell yet, or an explicit "coffee can" purchase).
#   TradedCycle     — a matched buy/sell pair. Records the strategy at work:
#                     bought N, sold ~0.8N at ~25% profit, kept ~0.2N.
#   OptionsPosition — a written or held option contract with coverage tracking
#                     (covered_by_shares for short calls, covered_by_cash for
#                     short puts, is_naked flag when either is insufficient).
#   OptionsIncomeEntry — every premium-generating or -consuming option event
#                        (sell_to_open, buy_to_close, expired, assigned).
#
# All four are DERIVED from SecurityTransaction rows by the detector. The
# raw transactions are the ground truth; these tables are inferred state
# that can always be rebuilt from scratch by re-running detection.


class StrategyTracker(db.Model):
    """A saved detection run. Records when the detector ran, what settings it
    used (frozen JSON snapshot), and what it produced (counts + summary). One
    row per run. Prior runs are kept — the detector is idempotent-ish (same
    inputs + same settings = same outputs), so a re-run replaces derived rows
    but adds a new StrategyTracker for the audit trail."""
    __tablename__ = 'strategy_tracker_runs'
    id = db.Column(db.Integer, primary_key=True)
    ran_at = db.Column(db.DateTime, default=_now, index=True)
    # Frozen strategy config used for this run — JSON of the settings dict.
    # Kept even when config later changes, so an old TradedCycle's
    # classification is reproducible from the settings it was made under.
    config_snapshot = db.Column(db.Text, nullable=True)
    transactions_scanned = db.Column(db.Integer, default=0)
    cycles_created = db.Column(db.Integer, default=0)
    retained_lots_created = db.Column(db.Integer, default=0)
    options_positions_touched = db.Column(db.Integer, default=0)
    naked_positions_flagged = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text, default='')

    def to_dict(self):
        return {
            'id': self.id,
            'ran_at': self.ran_at.isoformat() if self.ran_at else None,
            'transactions_scanned': self.transactions_scanned,
            'cycles_created': self.cycles_created,
            'retained_lots_created': self.retained_lots_created,
            'options_positions_touched': self.options_positions_touched,
            'naked_positions_flagged': self.naked_positions_flagged,
            'notes': self.notes,
        }


class RetainedLot(db.Model):
    """A lot the user intends to hold long-term. Created by the 5:4 detector
    when a matched cycle completes (the 20% left over), OR manually tagged
    by the operator on a bought-but-not-sold position.

    `shares_remaining` decrements as the retained shares are eventually sold
    (via the 400% diversification trigger, an assignment on a covered call,
    or a manual liquidation). `strategy_tag` distinguishes '5_4' (auto-
    classified from a cycle) from 'manual' (operator-tagged) so a report
    can partition the portfolio by intent."""
    __tablename__ = 'retained_lots'
    id = db.Column(db.Integer, primary_key=True)
    security_id = db.Column(db.String(120),
                            db.ForeignKey('securities.security_id'),
                            nullable=False, index=True)
    account_id = db.Column(db.String(120),
                           db.ForeignKey('plaid_accounts.account_id'),
                           nullable=False, index=True)
    purchase_date = db.Column(db.Date, nullable=False, index=True)
    cost_basis_per_share = db.Column(db.Float, nullable=False)
    shares_original = db.Column(db.Float, nullable=False)
    shares_remaining = db.Column(db.Float, nullable=False)
    # Link back to the TradedCycle that produced this lot, when applicable.
    # NULL = manual tag with no cycle context (e.g. a coffee-can buy).
    source_cycle_id = db.Column(db.Integer,
                                db.ForeignKey('traded_cycles.id'),
                                nullable=True, index=True)
    strategy_tag = db.Column(db.String(40), default='5_4', index=True)
    notes = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id, 'security_id': self.security_id,
            'account_id': self.account_id,
            'purchase_date': self.purchase_date.isoformat()
            if self.purchase_date else None,
            'cost_basis_per_share': self.cost_basis_per_share,
            'shares_original': self.shares_original,
            'shares_remaining': self.shares_remaining,
            'source_cycle_id': self.source_cycle_id,
            'strategy_tag': self.strategy_tag,
            'notes': self.notes,
        }


class TradedCycle(db.Model):
    """A matched buy/sell pair. The 5:4 detector creates one when it finds a
    sell of ~0.8× a buy quantity at ~1.25× the buy price within the matching
    window. The 20% not sold becomes a RetainedLot.

    `cycle_status` is 'open' when the buy has no matching sell yet (a lot
    waiting for the 25% profit target), 'complete' when the matching sell
    landed, 'partial' when the sell qty was outside the 5:4 tolerance
    (operator sold different fraction than the rule)."""
    __tablename__ = 'traded_cycles'
    id = db.Column(db.Integer, primary_key=True)
    security_id = db.Column(db.String(120),
                            db.ForeignKey('securities.security_id'),
                            nullable=False, index=True)
    buy_transaction_id = db.Column(
        db.String(120),
        db.ForeignKey('security_transactions.plaid_investment_transaction_id'),
        nullable=False, index=True)
    sell_transaction_id = db.Column(
        db.String(120),
        db.ForeignKey('security_transactions.plaid_investment_transaction_id'),
        nullable=True, index=True)
    buy_date = db.Column(db.Date, nullable=False, index=True)
    buy_qty = db.Column(db.Float, nullable=False)
    buy_price = db.Column(db.Float, nullable=False)
    sell_date = db.Column(db.Date, nullable=True)
    sell_qty = db.Column(db.Float, nullable=True)
    sell_price = db.Column(db.Float, nullable=True)
    realized_pnl = db.Column(db.Float, nullable=True)
    # Retain ratio actually observed on this cycle (not the config default).
    # 0.20 = kept 20% of the buy quantity; 0.30 = kept 30%; and so on. Lets
    # a report show how strictly the strategy was followed.
    retain_ratio_actual = db.Column(db.Float, nullable=True)
    cycle_status = db.Column(db.String(20), default='open', index=True)
    detected_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        return {
            'id': self.id, 'security_id': self.security_id,
            'buy_transaction_id': self.buy_transaction_id,
            'sell_transaction_id': self.sell_transaction_id,
            'buy_date': self.buy_date.isoformat() if self.buy_date else None,
            'buy_qty': self.buy_qty, 'buy_price': self.buy_price,
            'sell_date': self.sell_date.isoformat() if self.sell_date else None,
            'sell_qty': self.sell_qty, 'sell_price': self.sell_price,
            'realized_pnl': self.realized_pnl,
            'retain_ratio_actual': self.retain_ratio_actual,
            'cycle_status': self.cycle_status,
        }


class OptionsPosition(db.Model):
    """One open (or historically-closed) option contract position at a Plaid
    account. Positive `contracts_open` = long option (owner of the contract);
    negative = short option (writer of the contract). Coverage tracking
    (`covered_by_shares` for short calls, `covered_by_cash` for short puts)
    powers the naked-position guard, which enforces the user's stated 'no
    margin, no naked options' rule.

    Unique on (account_id, security_id) so a re-run of the detector updates
    the position rather than duplicating it. Closing (buy_to_close, expired,
    assigned, exercised) sets contracts_open to 0 and stamps `closed_at`."""
    __tablename__ = 'options_positions'
    id = db.Column(db.Integer, primary_key=True)
    security_id = db.Column(db.String(120),
                            db.ForeignKey('securities.security_id'),
                            nullable=False, index=True)
    account_id = db.Column(db.String(120),
                           db.ForeignKey('plaid_accounts.account_id'),
                           nullable=False, index=True)
    contract_type = db.Column(db.String(10), nullable=True)  # call | put
    # Denormalized from Security for query cheapness on strategy dashboard.
    underlying_ticker = db.Column(db.String(50), nullable=True, index=True)
    strike_price = db.Column(db.Float, nullable=True)
    expiration_date = db.Column(db.Date, nullable=True, index=True)
    contracts_open = db.Column(db.Float, nullable=False, default=0.0)
    premium_received_total = db.Column(db.Float, default=0.0)
    premium_paid_total = db.Column(db.Float, default=0.0)
    net_premium = db.Column(db.Float, default=0.0)
    covered_by_shares = db.Column(db.Integer, default=0)
    covered_by_cash = db.Column(db.Float, default=0.0)
    is_naked = db.Column(db.Boolean, default=False, index=True)
    # open | closed | expired | assigned | exercised
    status = db.Column(db.String(20), default='open', index=True)
    opened_at = db.Column(db.Date, nullable=True)
    closed_at = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    __table_args__ = (
        db.UniqueConstraint('account_id', 'security_id',
                            name='ux_options_positions_account_security'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'security_id': self.security_id,
            'account_id': self.account_id,
            'contract_type': self.contract_type,
            'underlying_ticker': self.underlying_ticker,
            'strike_price': self.strike_price,
            'expiration_date': self.expiration_date.isoformat()
            if self.expiration_date else None,
            'contracts_open': self.contracts_open,
            'premium_received_total': self.premium_received_total,
            'premium_paid_total': self.premium_paid_total,
            'net_premium': self.net_premium,
            'covered_by_shares': self.covered_by_shares,
            'covered_by_cash': self.covered_by_cash,
            'is_naked': bool(self.is_naked), 'status': self.status,
            'opened_at': self.opened_at.isoformat()
            if self.opened_at else None,
            'closed_at': self.closed_at.isoformat()
            if self.closed_at else None,
            'notes': self.notes,
        }


class OptionsIncomeEntry(db.Model):
    """One premium-generating or -consuming event on an option position.
    Rows accumulate over the life of the position — a sell_to_open + a
    buy_to_close is two rows on the same OptionsPosition, netting to a
    realized P&L when the position closes.

    `plaid_investment_transaction_id` is the source SecurityTransaction so
    the entry can be reconciled back to Plaid's raw event."""
    __tablename__ = 'options_income_entries'
    id = db.Column(db.Integer, primary_key=True)
    options_position_id = db.Column(db.Integer,
                                    db.ForeignKey('options_positions.id'),
                                    nullable=False, index=True)
    plaid_investment_transaction_id = db.Column(
        db.String(120),
        db.ForeignKey('security_transactions.plaid_investment_transaction_id'),
        nullable=True, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    # sell_to_open | buy_to_close | expired_worthless | assigned | exercised
    action = db.Column(db.String(30), nullable=False)
    contracts = db.Column(db.Float, nullable=False)
    premium_per_contract = db.Column(db.Float, nullable=True)
    # contracts × premium × 100 for standard equity options.
    total_premium = db.Column(db.Float, nullable=True)
    # True once the position is fully closed/expired — until then, this
    # premium is unrealized (waiting on the contract to expire or close).
    realized = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=_now)

    def to_dict(self):
        return {
            'id': self.id, 'options_position_id': self.options_position_id,
            'plaid_investment_transaction_id':
                self.plaid_investment_transaction_id,
            'date': self.date.isoformat() if self.date else None,
            'action': self.action, 'contracts': self.contracts,
            'premium_per_contract': self.premium_per_contract,
            'total_premium': self.total_premium,
            'realized': bool(self.realized),
        }
