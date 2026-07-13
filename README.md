# erpnext-bank-bridge

A self-hosted [Umbrel](https://umbrel.com) app that syncs bank transactions
from [Plaid](https://plaid.com) into [ERPNext](https://erpnext.com) for
automated accounting reconciliation. Designed for small businesses running
self-hosted ERP who want their bank feed in ERPNext without a SaaS accounting
middleman.

```
Bank (any Plaid-supported institution)
      │  Plaid Link (once)  +  /transactions/sync (every 6h)
      ▼
erpnext-bank-bridge  ──(local mirror; access tokens encrypted at rest)──►  Postgres
      │  ERPNext REST  (create + submit / cancel Bank Transaction)
      ▼
ERPNext  ──►  Bank Reconciliation Tool
```

## Features

- **Plaid Link OAuth flow** for secure bank connection — no bank credentials
  ever touch this app; Plaid holds them and returns a token.
- **6-hour automatic polling** via Plaid's cursor-based `/transactions/sync`,
  plus an on-demand **Sync now** button.
- **One-click account import** — create the matching ERPNext **Bank** +
  **Bank Account** records straight from the linked Plaid accounts (per row or
  all at once), deduped and auto-mapped, so there's no manual setup before
  transactions can post.
- **Creates ERPNext Bank Transaction records** ready for the built-in Bank
  Reconciliation Tool (deposit/withdrawal split from Plaid's amount sign).
- **Handles the full transaction lifecycle** — pending → posted transitions,
  category and merchant-name normalization, Plaid `modified` (re-post) and
  `removed` (cancel) events.
- **Multi-bank support** — Wells Fargo, Chase, Columbia Bank, Amex, PNC, and
  11,000+ Plaid institutions, including OAuth-only banks.
- **Access tokens encrypted at rest** with Fernet (AES-128-CBC + HMAC).
- **LAN-only admin UI** — no cloud dependency beyond Plaid itself.
- **Idempotent** — safe to re-run; never double-posts a transaction.
- **MIT licensed, open source.**

## Status

v0.1.1 — functional pilot. Runs the full Plaid Link → sync → ERPNext push loop
with a mocked-API test suite, plus one-click import of Plaid accounts into
ERPNext Bank / Bank Account records. See the roadmap notes at the bottom.

## How it works

1. **Link a bank once** through Plaid Link (`/admin/link_bank`). OAuth-only
   banks bounce through `/plaid/oauth_return` and back automatically.
2. The server exchanges the Link public token for a durable **access token**,
   stored **Fernet-encrypted** in Postgres.
3. **Import accounts into ERPNext** (`/admin/accounts`) — click **Import all
   supported accounts** (or the per-row **Create in ERPNext** button) and the
   bridge creates the matching **Bank** + **Bank Account** records for you and
   maps them. Or map to an existing ERPNext Bank Account with the dropdown.
4. A background job polls **`/transactions/sync`** every `SYNC_INTERVAL_HOURS`
   (default 6). For each transaction it upserts a local mirror row (idempotent
   on the Plaid transaction id) and posts an ERPNext **Bank Transaction**:
   - Plaid `added` → create + submit a Bank Transaction,
   - Plaid `modified` → cancel the stale doc, post a corrected replacement,
   - Plaid `removed` → cancel the doc (docstatus 2).
5. **Reconcile** in ERPNext's built-in **Bank Reconciliation Tool**.

Idempotency is enforced two ways: a unique local row per Plaid transaction id,
and an ERPNext find-or-create keyed on `reference_number` (the Plaid id). The
deposit/withdrawal split follows Plaid's convention (positive amount = money
out of the account → withdrawal; negative = money in → deposit).

## Data model

| Table | Purpose |
|-------|---------|
| `plaid_items` | one linked login/institution; encrypted access token + sync cursor |
| `plaid_accounts` | accounts within an item; ERPNext Bank Account mapping + sync toggle + import status |
| `bank_transactions` | local mirror of Plaid transactions + ERPNext docname/state |
| `plaid_sync_log` | audit trail (plaid_pull / erpnext_push, counts, errors) |
| `plaid_link_state` | short-lived link-token bookkeeping for the OAuth handoff |

## Admin UI (LAN-only, unauthenticated)

The admin UI carries no login — it is intended to run behind Umbrel on a
trusted LAN, never exposed to the Internet.

- `/admin` — dashboard: linked banks, health, counts, recent sync log
- `/admin/link_bank` — Plaid Link entry point
- `/admin/accounts` — one-click import Plaid accounts into ERPNext Bank
  Accounts, map to existing ones, toggle sync
- `/admin/transactions` — filterable list + per-row Retry
- `/admin/plaid_settings` — Client ID / secrets / environment / redirect / webhook
- `/admin/erpnext_settings` — ERPNext URL + API key/secret + Test / Verify doctype
- `/admin/sync_log` — recent sync activity

## Installation (Umbrel Community Store)

1. Add a Community Store that ships this app's manifest (Umbrel → App Store →
   ⋯ → Community App Stores). A ready-to-use `docker-compose.yml` +
   `umbrel-app.yml` are provided for packaging.
2. Install **Bank Bridge**. It launches a Flask server (port 5202) plus a
   Postgres sidecar.
3. Open the app; you'll land on `/admin`.

Docker image: `polehntim/bank-bridge:0.1.1`. To build it yourself:

```bash
docker build -t polehntim/bank-bridge:0.1.1 erpnext-bank-bridge_v0.1.0
```

## Configuration

All runtime settings can be entered in the admin UI (persisted to the data
volume) or seeded via environment variables.

### 1. Plaid (`/admin/plaid_settings`)

Create an app at [dashboard.plaid.com](https://dashboard.plaid.com):

- Copy your **Client ID** and per-environment **Secrets** (Developers → Keys).
- Register the redirect URI `http://umbrel.local:5202/plaid/oauth_return`
  (Developers → API → Allowed redirect URIs) — required for OAuth banks.
- Start in **sandbox** to rehearse the flow; switch to **production** once your
  Plaid app is approved for the institutions you need.

### 2. ERPNext (`/admin/erpnext_settings`)

- In ERPNext, open a System-Manager user → Settings → API Access → **Generate
  Keys**, and paste the API Key + Secret here.
- Point the URL at your ERPNext instance (on Umbrel, its app-proxy port).
- Use **Test Connection** and **Verify Bank Transaction doctype** to confirm.
  **Test Connection** (and the **Ensure Bank Account fields & types** button)
  also provision the Bank Account Type records + custom fields the importer
  needs — idempotently, so it's safe to click repeatedly.

### 3. Import accounts (`/admin/accounts`)

After linking a bank, bring its accounts into ERPNext without any manual setup:

- **Import all supported accounts to ERPNext** (top of the page) creates a
  **Bank** + **Bank Account** for every unmapped, supported account across all
  linked banks, maps each one, and enables sync — then lands you on the
  dashboard ready to **Sync now**.
- **Create in ERPNext** (per row) does the same for a single account.

Both are **idempotent**: a Bank is reused if one with the same name already
exists, and a Bank Account is deduped on the `plaid_account_id` custom field
(auto-provisioned on first import, alongside `last_4`), so clicking again never
creates duplicates. Bank Accounts are created with `is_company_account = 1` and
the **Default Company** from ERPNext settings.

**Company-account fallback.** ERPNext requires a company Bank Account to link a
specific GL account from your Chart of Accounts, which Plaid can't give us. On
instances that enforce this, the create fails with *"Company Account is
mandatory"* — so the importer automatically retries once as a **personal**
account (`is_company_account = 0`, no GL link) and the import still succeeds.
When that happens the sync log records the fallback with a note to **promote the
Bank Account to a company account manually in ERPNext** once your Chart of
Accounts is set up. Prefer to skip the retry entirely? Set
`ERPNEXT_DEFAULT_IS_COMPANY_ACCOUNT=false` to import every account as personal
from the start.

**No prerequisites on a stock ERPNext.** The bootstrap also creates the
`Current` and `Credit` **Bank Account Type** records if they're missing (stock
ERPNext ships without them), so the very first import can't fail on a missing
link target. Bank Account Types are provisioned on Test Connection, by the
**Ensure Bank Account fields & types** button on the ERPNext settings page, and
as the first step of every import — always idempotent.

**Supported** subtypes (get a button): `checking`, `savings`, `cd`,
`money market`, `cash management`, `paypal` → account type **Current**;
`credit card`, `line of credit` → account type **Credit**. Loans, investments,
brokerage, and retirement accounts (mortgage, student, auto, 401k, IRA, Roth,
HSA, brokerage) are **not** Bank Accounts in ERPNext's model and are skipped
with a "not supported" note. Set `ERPNEXT_DEFAULT_BANK_ACCOUNT_TYPE` to force a
single account type for every import if your Chart of Accounts names them
differently.

### Environment variables

| Var | Default | Notes |
|-----|---------|-------|
| `DATABASE_URL` | — (required) | Postgres only, no SQLite fallback |
| `SECRET_KEY` | `dev-not-for-production` | Flask secret |
| `PLAID_CLIENT_ID` / `PLAID_SECRET` | "" | seed; editable in the UI |
| `PLAID_ENV` | `sandbox` | `sandbox` \| `production` |
| `PLAID_REDIRECT_URI` | `http://umbrel.local:5202/plaid/oauth_return` | must match the Plaid dashboard |
| `PLAID_WEBHOOK_URL` | "" | optional; polling is enough |
| `ERPNEXT_URL` | `http://umbrel.local:5300` | ERPNext base URL |
| `ERPNEXT_API_KEY` / `ERPNEXT_API_SECRET` | "" | System Manager API pair |
| `ERPNEXT_DEFAULT_COMPANY` | "" | company set on imported Bank Accounts; transaction company comes from the Bank Account |
| `ERPNEXT_DEFAULT_BANK_ACCOUNT_TYPE` | "" | optional; force one Bank Account type for all imports (blank → inferred from Plaid subtype) |
| `ERPNEXT_DEFAULT_IS_COMPANY_ACCOUNT` | `true` | import Bank Accounts as company accounts; retries as personal on "Company Account is mandatory". Set `false` to import all as personal from the start |
| `FERNET_KEY` | "" | blank → autogenerated + persisted to `{DATA_DIR}/fernet.key` |
| `SYNC_INTERVAL_HOURS` | `6` | background poll cadence |
| `SCHEDULER_ENABLED` | `true` | set false to drive syncs by external cron |
| `DATA_DIR` | `/data` | Fernet key + settings JSON + scheduler lock |

> ⚠️ **Back up `{DATA_DIR}/fernet.key`.** It decrypts every stored Plaid
> access token. Losing it means re-linking every bank.

## Local development

```bash
cd erpnext-bank-bridge_v0.1.0
cp .env.example .env          # fill in POSTGRES_PASSWORD etc.
docker compose up --build     # admin UI at http://localhost:5202/admin
```

## Tests

```bash
cd erpnext-bank-bridge_v0.1.0
python3 -m unittest discover -s tests -v
```

Covers Fernet encryption round-trip + key persistence, Plaid response
normalization, sync idempotency, deposit/withdrawal mapping, modified →
cancel+replace, removed → cancel, unmapped/disabled account handling, failed
push → error + retry, and every admin page rendering. The Plaid SDK and ERPNext
are mocked (`tests/fakes.py`), so no network access or extra wheels are needed.

## Security notes

- Bank credentials never reach this app — Plaid Link handles authentication and
  returns a token.
- Plaid access tokens are encrypted at rest with Fernet; the key lives on the
  app's data volume (`fernet.key`, mode `0600`), never in the database or git.
- Least-privilege Plaid scope: `transactions` only.
- Calls to Plaid use HTTPS/TLS; ERPNext is reached over the local network at an
  operator-configured URL and never exposed to the Internet.
- No telemetry or analytics — outbound connections go only to Plaid and your
  ERPNext instance.
- The admin UI is unauthenticated by design and must stay on a trusted LAN
  behind Umbrel — do not expose it to the Internet.
- Secrets (`.env`, `fernet.key`, `*_settings.json`) are git-ignored; only
  `.env.example` placeholders are committed.

See [SECURITY.md](SECURITY.md) for the full security posture and how to report
a vulnerability.

## Roadmap

- Plaid webhook signature verification (currently the webhook is best-effort).
- Merchant → ERPNext Supplier auto-suggest + transaction auto-categorization
  (scaffolded in `app/erpnext_bank.py:_maybe_suggest_supplier`).
- Configurable per-account category → ERPNext party/ledger hints.

## License

MIT — see [LICENSE](LICENSE).
