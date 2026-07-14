# SPDX-License-Identifier: MIT
"""Env-var config — a deliberately minimal single Config class.

Every knob has an env-var default; the runtime settings that an operator is
likely to change without a redeploy (Plaid keys, ERPNext connection) are ALSO
editable via the admin UI and persisted to JSON under DATA_DIR, which wins over
these seeds at call time (see app/plaid_settings.py, app/erpnext_settings.py)."""
import os


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, '').strip().lower()
    if not v:
        return default
    return v in ('1', 'true', 'yes', 'on')


class Config:
    # Flask
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-not-for-production')

    # Persistent data dir — Fernet key file, plaid_settings.json,
    # erpnext_settings.json, scheduler lock all live here.
    DATA_DIR = os.environ.get('APP_DATA_DIR_INNER') or \
        os.environ.get('DATA_DIR', os.path.join(os.getcwd(), 'data'))

    # ── PostgreSQL — REQUIRED (no SQLite fallback).
    # The 6-hourly poll thread + admin writes are a concurrent-write pattern;
    # Postgres MVCC avoids SQLite lock storms. Failing fast at boot beats a
    # silent landing on a dev-only backing store. Tests override
    # SQLALCHEMY_DATABASE_URI via create_app(test_config=...), so they set a
    # throwaway DATABASE_URL env just to clear this guard.
    _db_url = os.environ.get('DATABASE_URL', '').strip()
    if not _db_url:
        raise RuntimeError(
            'DATABASE_URL is required. bank-bridge is Postgres-only. '
            'Example: postgresql://bankbridge:PASSWORD@db:5432/bankbridge')
    if not _db_url.startswith(('postgresql://', 'postgres://')):
        raise RuntimeError(
            f'DATABASE_URL must be a PostgreSQL URL (got: {_db_url.split("://")[0]}://…).')
    SQLALCHEMY_DATABASE_URI = _db_url.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_size': 5,
        'max_overflow': 10,
        'pool_pre_ping': True,
        'pool_recycle': 1800,
    }

    # ── Plaid ─────────────────────────────────────────────────────────────
    # These seed the defaults; /admin/plaid_settings persists edits to
    # {DATA_DIR}/plaid_settings.json, which WINS. PLAID_ENV is sandbox until
    # it's flipped to production after the Plaid app is approved for the
    # institutions you need.
    PLAID_CLIENT_ID = os.environ.get('PLAID_CLIENT_ID', '').strip()
    PLAID_SECRET = os.environ.get('PLAID_SECRET', '').strip()
    _plaid_env = os.environ.get('PLAID_ENV', 'sandbox').strip().lower()
    PLAID_ENV = _plaid_env if _plaid_env in ('sandbox', 'production') else 'sandbox'
    # OAuth redirect target for OAuth-only banks (Wells Fargo). Must EXACTLY
    # match a redirect URI registered in the Plaid dashboard.
    PLAID_REDIRECT_URI = os.environ.get(
        'PLAID_REDIRECT_URI', 'http://umbrel.local:5202/plaid/oauth_return').strip()
    # Optional — leave blank for the polling pilot. When set, Plaid POSTs
    # transaction-update webhooks here so a sync can fire sooner than 6h.
    PLAID_WEBHOOK_URL = os.environ.get('PLAID_WEBHOOK_URL', '').strip()

    # ── ERPNext / Bank Transaction bridge ─────────────────────────────────
    # ERPNEXT_URL is Umbrel's app-proxy port for the ERPNext app (both apps on
    # the same host, isolated Docker networks — reached over the host proxy).
    # API key/secret are a per-user (System Manager) pair; reuse an existing
    # key or mint a dedicated one.
    ERPNEXT_URL = os.environ.get('ERPNEXT_URL', 'http://umbrel.local:5300').strip()
    ERPNEXT_API_KEY = os.environ.get('ERPNEXT_API_KEY', '').strip()
    ERPNEXT_API_SECRET = os.environ.get('ERPNEXT_API_SECRET', '').strip()
    ERPNEXT_DEFAULT_COMPANY = os.environ.get('ERPNEXT_DEFAULT_COMPANY', '').strip()
    # Optional global override for the ERPNext Bank Account `account_type` used
    # by one-click account import (see app/erpnext_accounts.py). Blank → inferred
    # per account from the Plaid subtype: `Current` for depository-style
    # accounts (checking/savings/CD/money market/…), `Credit` for credit cards
    # and lines of credit. Set this if your Chart of Accounts names Bank Account
    # Types differently and you want one value applied to every import.
    ERPNEXT_DEFAULT_BANK_ACCOUNT_TYPE = os.environ.get(
        'ERPNEXT_DEFAULT_BANK_ACCOUNT_TYPE', '').strip()
    # Whether one-click account import marks Bank Accounts as company accounts
    # (`is_company_account = 1`). ERPNext requires a company account to link a
    # specific GL account from the Chart of Accounts; since Plaid gives us no GL
    # link, a company-account create fails with "Company Account is mandatory" on
    # instances that enforce it. Default True (with a create-side retry that
    # falls back to a personal account on that error); set False to import every
    # account as personal from the start, skipping the retry entirely.
    ERPNEXT_DEFAULT_IS_COMPANY_ACCOUNT = _bool(
        'ERPNEXT_DEFAULT_IS_COMPANY_ACCOUNT', True)
    # One-click account import auto-creates a matching GL Account in ERPNext's
    # Chart of Accounts (account_type "Bank") so a company Bank Account can link
    # a real `account` and keep `is_company_account = 1` (see
    # app/erpnext_accounts.py). These two knobs tune that:
    #   * the parent group the per-account GL Accounts are created under. Stock
    #     ERPNext ships "Bank Accounts" (under Current Assets → Assets); some CoA
    #     templates name it differently ("Bank", "Cash and Bank"). Set this to
    #     match yours so the importer finds the existing group instead of making
    #     a new one.
    ERPNEXT_BANK_ACCOUNT_GROUP_NAME = os.environ.get(
        'ERPNEXT_BANK_ACCOUNT_GROUP_NAME', 'Bank Accounts').strip() or 'Bank Accounts'
    #   * the fallback currency for a created GL Account when the Plaid account
    #     doesn't report an iso_currency_code.
    ERPNEXT_BANK_ACCOUNT_CURRENCY = os.environ.get(
        'ERPNEXT_BANK_ACCOUNT_CURRENCY', 'USD').strip() or 'USD'

    # ── v0.3.0 · auto-Supplier creation ───────────────────────────────────
    # When a pushed Plaid transaction carries a merchant_name we've never seen,
    # find-or-create a matching ERPNext Supplier so the Bank Transaction is
    # linkable during reconciliation (see app/erpnext_bank.py). Default ON — it
    # is non-destructive (a Supplier is a reference record, not a posting).
    ERPNEXT_AUTO_CREATE_SUPPLIERS = _bool('ERPNEXT_AUTO_CREATE_SUPPLIERS', True)
    # Defaults for a newly-created ERPNext Supplier. The supplier_group must be
    # an existing Supplier Group docname; stock ERPNext ships "All Supplier
    # Groups". If yours differs, set this so the create doesn't fail on the link
    # (the create retries once without the group as a fallback).
    ERPNEXT_DEFAULT_SUPPLIER_GROUP = os.environ.get(
        'ERPNEXT_DEFAULT_SUPPLIER_GROUP', 'All Supplier Groups').strip() \
        or 'All Supplier Groups'
    ERPNEXT_DEFAULT_SUPPLIER_COUNTRY = os.environ.get(
        'ERPNEXT_DEFAULT_SUPPLIER_COUNTRY', 'United States').strip() \
        or 'United States'

    # ── v0.3.0 · categorization rules → Journal Entry generation ───────────
    # Master switch for the rules engine. Default OFF: rules can be authored and
    # tested at /admin/rules without firing, because an incorrect auto-generated
    # Journal Entry is worse than none. Flip to True only once your rules are
    # trusted — then every newly-pushed transaction is run through the rules.
    ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES = _bool(
        'ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES', False)
    # Submit generated Journal Entries (docstatus 1) vs leave them as Draft
    # (docstatus 0) for human review before they hit the ledger. Default False
    # (Draft) — the conservative choice.
    ERPNEXT_JOURNAL_ENTRY_AUTO_SUBMIT = _bool(
        'ERPNEXT_JOURNAL_ENTRY_AUTO_SUBMIT', False)
    # Initial GeneratedJournalEntry.state for a freshly generated (Draft) JE.
    ERPNEXT_JOURNAL_ENTRY_REVIEW_STATE = os.environ.get(
        'ERPNEXT_JOURNAL_ENTRY_REVIEW_STATE', 'pending_review').strip() \
        or 'pending_review'

    # ── Encryption at rest ────────────────────────────────────────────────
    # Fernet key for the stored Plaid access_tokens. Blank → app autogenerates
    # one on first boot and persists it to {DATA_DIR}/fernet.key (see
    # app/crypto.py). Set explicitly to rotate / share a known key.
    FERNET_KEY = os.environ.get('FERNET_KEY', '').strip()

    # Poll cadence (hours) for the background transactions/sync loop.
    SYNC_INTERVAL_HOURS = int(os.environ.get('SYNC_INTERVAL_HOURS', '6'))
    # Set false to disable the in-process scheduler (e.g. drive syncs by cron
    # hitting /api/sync/plaid_now instead).
    SCHEDULER_ENABLED = _bool('SCHEDULER_ENABLED', True)
