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


def _csv(name: str) -> tuple:
    """A comma-separated env var as a tuple of trimmed, non-empty values.
    Missing / blank → (), so a caller can iterate unconditionally."""
    return tuple(part.strip() for part in os.environ.get(name, '').split(',')
                 if part.strip())


class Config:
    # Flask
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-not-for-production')

    # ── Optional admin HTTP Basic Auth ────────────────────────────────────
    # The admin UI is unauthenticated by design — it assumes Umbrel's app_proxy
    # (LAN trust boundary) sits in front. When you expose the OAuth callback
    # over public HTTPS (see the README "Production Deployment" section), set
    # BOTH of these to add a belt-and-suspenders Basic Auth prompt on every
    # /admin route. If EITHER is blank, no auth is enforced (backward-compatible
    # — stock LAN deployments keep working untouched). The password may be a
    # plaintext value or a werkzeug password hash (`pbkdf2:…`/`scrypt:…` from
    # `generate_password_hash`) — a hash is verified as one, so you never have to
    # store the cleartext. See app/blueprints/admin_ui.py.
    ADMIN_BASIC_AUTH_USER = os.environ.get('ADMIN_BASIC_AUTH_USER', '').strip()
    ADMIN_BASIC_AUTH_PASS = os.environ.get('ADMIN_BASIC_AUTH_PASS', '')

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
    #   * v0.4.0 balance-only investments — the Assets-side groups their GL
    #     leaves are created under (Non-current Assets → Investments →
    #     Retirement / Marketable Securities / Digital Assets / Other). Set these
    #     to match your Chart of Accounts if it names them differently.
    ERPNEXT_INVESTMENTS_GROUP_NAME = os.environ.get(
        'ERPNEXT_INVESTMENTS_GROUP_NAME', 'Investments').strip() or 'Investments'
    ERPNEXT_NONCURRENT_ASSETS_GROUP_NAME = os.environ.get(
        'ERPNEXT_NONCURRENT_ASSETS_GROUP_NAME',
        'Non-current Assets').strip() or 'Non-current Assets'
    # ── v0.3.1 · fuzzy dedup of auto-created GL Accounts ───────────────────
    # Before auto-creating a Bank GL Account, the importer fuzzy-matches its
    # intended account_name against the company's existing leaf Accounts
    # (stdlib difflib similarity + last-4 mask signal). A candidate whose
    # similarity percentage is at or above this threshold is reused instead of
    # creating a near-duplicate (see app/erpnext_accounts.py). Range 0-100;
    # raise it to demand closer matches, lower it to reuse more aggressively.
    _fuzzy_threshold_raw = os.environ.get('ERPNEXT_FUZZY_MATCH_THRESHOLD', '85').strip()
    try:
        ERPNEXT_FUZZY_MATCH_THRESHOLD = min(100, max(0, int(_fuzzy_threshold_raw)))
    except ValueError:
        ERPNEXT_FUZZY_MATCH_THRESHOLD = 85

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
    # v0.4.0.8 · the sell-side equivalents, for a newly-created ERPNext Customer
    # (money IN — a fruit buyer, USDA, a grant). Stock ERPNext ships "All
    # Customer Groups" and "All Territories"; the create retries once without
    # them if yours differ.
    ERPNEXT_DEFAULT_CUSTOMER_GROUP = os.environ.get(
        'ERPNEXT_DEFAULT_CUSTOMER_GROUP', 'All Customer Groups').strip() \
        or 'All Customer Groups'
    ERPNEXT_DEFAULT_TERRITORY = os.environ.get(
        'ERPNEXT_DEFAULT_TERRITORY', 'All Territories').strip() \
        or 'All Territories'
    # v0.4.0.8 · overrides for the dual-role heuristic (a party that both bills
    # you and pays you gets BOTH an ERPNext Customer and Supplier — see
    # erpnext_bank.is_dual_role_party). Comma-separated party names.
    # SINGLE wins over DUAL, so a false positive can always be pinned back.
    BANKBRIDGE_DUAL_ROLE_PARTIES = _csv('BANKBRIDGE_DUAL_ROLE_PARTIES')
    BANKBRIDGE_SINGLE_ROLE_PARTIES = _csv('BANKBRIDGE_SINGLE_ROLE_PARTIES')

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

    # ── v0.3.5 · self-healing DB auth ─────────────────────────────────────
    # Postgres bakes the `bankbridge` role's password in at first volume init.
    # If a later deploy hands the app a different password (e.g. an earlier init
    # ran while APP_SEED was blank), every connection fails with "password
    # authentication failed for user 'bankbridge'" and the old workaround was to
    # wipe the volume. Instead, at boot we detect that specific failure and use
    # a superuser connection to ALTER the role's password to match the current
    # value — no wipe, no manual step (see app/db_recovery.py).
    #   * master switch — set False to disable the self-heal entirely.
    AUTO_RECOVER_DB_AUTH = _bool('AUTO_RECOVER_DB_AUTH', True)
    #   * an OPTIONAL explicitly-provisioned superuser, tried first. The stock
    #     compose runs the db with POSTGRES_USER=bankbridge, which makes
    #     `bankbridge` the SOLE superuser — there is no stock `postgres` role
    #     (psql -U postgres → "role does not exist"). So this usually can't
    #     connect; the deterministic rescue user below is the real recovery path.
    #     Leave DB_SUPERUSER blank to skip this attempt, or set it if you
    #     provisioned a distinct superuser yourself.
    DB_SUPERUSER = os.environ.get('DB_SUPERUSER', '').strip()
    #   * password for DB_SUPERUSER. Falls back to POSTGRES_PASSWORD, then to
    #     SECRET_KEY (our app-side alias for APP_SEED).
    DB_SUPERUSER_PASSWORD = (
        os.environ.get('DB_SUPERUSER_PASSWORD', '').strip()
        or os.environ.get('POSTGRES_PASSWORD', '').strip()
        or SECRET_KEY)
    #   * the deterministic RESCUE superuser. Fresh installs create this second
    #     superuser at init (scripts/initdb.d/10-create-rescue-superuser.sh) with
    #     a password derived as HMAC-SHA256(key=APP_SEED, msg=DB_RESCUE_SALT).
    #     Because the same APP_SEED + salt reproduce the same password every
    #     boot, the app re-derives it to log in and reset a drifted `bankbridge`
    #     password. Existing installs predate this user and must run
    #     scripts/rotate_db_password.sh once (which creates it).
    DB_RESCUE_USER = os.environ.get('DB_RESCUE_USER', 'bridgeadmin').strip() \
        or 'bridgeadmin'
    DB_RESCUE_SALT = os.environ.get('DB_RESCUE_SALT', 'bankbridge-rescue-v1').strip() \
        or 'bankbridge-rescue-v1'
    #   * the APP_SEED the rescue password is derived from — must match what the
    #     db container used at init. Falls back to POSTGRES_PASSWORD (which the
    #     compose sets to APP_SEED), then SECRET_KEY (also APP_SEED).
    DB_RESCUE_SEED = (os.environ.get('DB_RESCUE_SEED', '').strip()
                      or os.environ.get('POSTGRES_PASSWORD', '').strip()
                      or SECRET_KEY)

    # ── Sync cadence + cost guardrails ────────────────────────────────────
    # Poll cadence (hours) for the background transactions/sync loop. Default
    # DAILY (24): most reconciliation workflows don't need fresher-than-daily
    # bank data, and daily is ~4× cheaper on Plaid /transactions/sync calls than
    # the old 6h default. Extended semantics: 0 or negative = MANUAL ONLY — the
    # scheduler adds no auto-poll job and syncs run only from the dashboard
    # "Sync now" button. Editable per-install at /admin/plaid_settings (which
    # persists a value that WINS over this seed); the admin picker exposes it as
    # cost-aware presets (see app/sync_config.py).
    try:
        SYNC_INTERVAL_HOURS = int(os.environ.get('SYNC_INTERVAL_HOURS', '24'))
    except ValueError:
        SYNC_INTERVAL_HOURS = 24
    # Optional per-Item safety brake: the max number of Plaid pull calls allowed
    # per Item per UTC day. 0 (default) = no limit. When set, a pull that would
    # exceed it is skipped with a logged warning + a `plaid_pull`/`skipped`
    # sync-log row — a backstop against a misconfigured short interval or a
    # retry loop running up the Plaid bill (see app/sync_engine.py).
    try:
        PLAID_MAX_CALLS_PER_DAY = max(
            0, int(os.environ.get('PLAID_MAX_CALLS_PER_DAY', '0')))
    except ValueError:
        PLAID_MAX_CALLS_PER_DAY = 0
    # How often the sync engine spends a billable Plaid /accounts/get to refresh
    # cached balances. Those balances feed the dashboard only — ERPNext
    # reconciles on transaction amounts, not balances — so refreshing them every
    # poll pays for data no logic consumes. Default 24 (at most one balance
    # refresh per Item per day, plus always on an Item's first sync). Set to 0 to
    # refresh every poll if balance freshness matters more than call cost. See
    # app/sync_engine.py:_should_refresh_accounts.
    try:
        ACCOUNT_REFRESH_INTERVAL_HOURS = max(
            0, int(os.environ.get('ACCOUNT_REFRESH_INTERVAL_HOURS', '24')))
    except ValueError:
        ACCOUNT_REFRESH_INTERVAL_HOURS = 24
    # Indicative price per Plaid /transactions/sync call — used ONLY to render
    # the cost estimate next to the admin sync-frequency picker. Not billing,
    # just a planning aid; override if your Plaid contract differs.
    try:
        PLAID_PRICE_PER_CALL = float(os.environ.get('PLAID_PRICE_PER_CALL', '0.30'))
    except ValueError:
        PLAID_PRICE_PER_CALL = 0.30
    # ── v0.4.1 · intercompany transfer detection ────────────────────────────
    # A transfer between two ERPNext Companies the operator owns shows up as two
    # Plaid transactions of equal magnitude and opposite sign, one per Company.
    # These four knobs tune how confidently Bank Bridge matches them up; see
    # app/intercompany.py for the scoring model. Detection is inert until linked
    # accounts resolve to more than one Company, so a single-Company install is
    # unaffected whatever these are set to.
    #
    # ± window, in days, the two legs may be dated apart. A same-bank move clears
    # the same day; an ACH between institutions often takes two or three.
    try:
        INTERCOMPANY_DATE_TOLERANCE_DAYS = max(
            0, int(os.environ.get('INTERCOMPANY_DATE_TOLERANCE_DAYS', '3')))
    except ValueError:
        INTERCOMPANY_DATE_TOLERANCE_DAYS = 3
    # Minimum description similarity (0.0-1.0, stdlib difflib) for two
    # transactions to be considered a candidate pair at all. 0.6 accepts the
    # canonical 'Transfer to Personal' / 'Transfer from Farm' shape (0.63) while
    # rejecting unrelated merchants (below 0.3).
    try:
        INTERCOMPANY_DESCRIPTION_THRESHOLD = float(os.environ.get(
            'INTERCOMPANY_DESCRIPTION_THRESHOLD', '0.6'))
    except ValueError:
        INTERCOMPANY_DESCRIPTION_THRESHOLD = 0.6
    # Minimum overall confidence to pair AUTOMATICALLY. Below this the candidate
    # is logged and left alone rather than booked — weak evidence should reach a
    # human, not the ledger. Raise it to make auto-pairing stricter.
    try:
        INTERCOMPANY_CONFIDENCE_THRESHOLD = float(os.environ.get(
            'INTERCOMPANY_CONFIDENCE_THRESHOLD', '0.75'))
    except ValueError:
        INTERCOMPANY_CONFIDENCE_THRESHOLD = 0.75
    # How far back a detection pass looks for a counterparty. Generous, because
    # each Plaid Item advances its own cursor and the two legs routinely arrive
    # on different syncs — but bounded, so the pass stays a small query.
    try:
        INTERCOMPANY_LOOKBACK_DAYS = max(
            0, int(os.environ.get('INTERCOMPANY_LOOKBACK_DAYS', '30')))
    except ValueError:
        INTERCOMPANY_LOOKBACK_DAYS = 30
    # The Chart-of-Accounts group intercompany 'Due from …' receivables are
    # created under, on the Assets side. The 'Due to …' payables use
    # ERPNEXT_CURRENT_LIABILITIES_GROUP_NAME (above).
    ERPNEXT_LOANS_ADVANCES_GROUP_NAME = os.environ.get(
        'ERPNEXT_LOANS_ADVANCES_GROUP_NAME', 'Loans and Advances (Assets)')

    # Set false to disable the in-process scheduler entirely (e.g. drive syncs by
    # cron hitting /api/sync/plaid_now instead). Distinct from MANUAL-ONLY above:
    # this stops the scheduler process; MANUAL-ONLY runs it with no poll job.
    SCHEDULER_ENABLED = _bool('SCHEDULER_ENABLED', True)
