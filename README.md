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
- **Auto-creates ERPNext Suppliers** from merchant names (v0.3.0) — a never-seen
  merchant is normalized ("SQ \*STARBUCKS 92104" → "Starbucks") and find-or-
  created as a Supplier so the transaction is instantly linkable. On by default.
- **Rules-based Journal Entry generation** (v0.3.0) — user-configured rules
  (`/admin/rules`) match on merchant / description / Plaid category / amount and
  auto-generate a Journal Entry (debit expense, credit bank), inserted as a
  Draft for review. **Off by default** — opt in once your rules are trusted.
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

v0.3.1 — functional pilot. Runs the full Plaid Link → sync → ERPNext push loop
with a mocked-API test suite, one-click import of Plaid accounts into ERPNext
Bank / Bank Account records, auto-Supplier creation from merchant names, and a
rules engine that auto-generates Journal Entries. v0.3.1 polish: auto-created GL
Accounts get sequential `account_number`s under their numbered parent group, the
rule debit/credit dropdowns list every enabled leaf account (Bank included), and
a fuzzy-match check reuses an existing near-duplicate GL Account instead of
creating a new one (with an operator confirm on the accounts page). See the
roadmap at the bottom.

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
5. **Auto-categorize** (v0.3.0) — right after a Bank Transaction posts, the
   bridge (a) find-or-creates the merchant's ERPNext **Supplier** so the row is
   linkable, and (b) if the rules engine is enabled, runs the transaction
   through your **CategorizationRules** and generates a **Journal Entry** for
   the first match (Draft by default). Both steps are non-destructive — a
   failure is logged and never unwinds the posted Bank Transaction.
6. **Reconcile** in ERPNext's built-in **Bank Reconciliation Tool** (or review
   the generated Journal Entries at `/admin/generated_entries`).

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
| `suppliers` | merchant → ERPNext Supplier cache (normalized name, tallies) — v0.3.0 |
| `categorization_rules` | user rules: match predicate → debit/credit accounts + party + template — v0.3.0 |
| `generated_journal_entries` | per-JE state record (state, rule, JE docname) — v0.3.0 |
| `audit_events` | **permanent, append-only** audit trail of every action (before/after JSON, actor, IP) — v0.3.0 |
| `plaid_sync_log` | HTTP-level action log (plaid_pull / erpnext_push / erpnext_supplier_auto_create, counts, errors); `subject_id` cross-links to `audit_events` |
| `plaid_link_state` | short-lived link-token bookkeeping for the OAuth handoff |

## Admin UI (LAN-only, unauthenticated)

The admin UI carries no login — it is intended to run behind Umbrel on a
trusted LAN, never exposed to the Internet.

- `/admin` — dashboard: linked banks, health, counts, recent sync log
- `/admin/link_bank` — Plaid Link entry point
- `/admin/accounts` — one-click import Plaid accounts into ERPNext Bank
  Accounts, map to existing ones, toggle sync
- `/admin/transactions` — filterable list + per-row Retry
- `/admin/rules` — CRUD for categorization rules + a **Test a rule** sandbox
  (paste a sample merchant/amount → see which rule matches and the JE preview)
- `/admin/suppliers` — auto-created Suppliers (tallies, ERPNext link); fix a bad
  normalization or re-point the ERPNext Supplier
- `/admin/generated_entries` — rules-generated Journal Entries; approve (submit)
  / reject (cancel) individually or in bulk
- `/admin/audit` — the permanent audit trail: filter by event type / subject /
  actor / date, drill into any event's before→after JSON, group by subject to
  see a rule's or JE's full lifecycle, and **Export CSV**
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

**Automatic Chart of Accounts wiring.** A company Bank Account
(`is_company_account = 1`) must link a specific GL account of type **Bank** from
that company's Chart of Accounts — something Plaid can't give us. So the importer
creates it for you: it finds (or creates) the **Bank Accounts** group under
*Assets → Current Assets*, then creates a per-account GL leaf under it named to
match the Bank Account (e.g. `Wells Fargo Checking - 0000`, auto-named by Frappe
to `Wells Fargo Checking - 0000 - <CompanyAbbr>`), and links it on the Bank
Account. The GL account's currency comes from the Plaid account's
`iso_currency_code`. This is **idempotent** — an existing group or GL leaf is
reused, never duplicated — and the created GL docname is recorded on the local
account (`erpnext_gl_account_name`). Set `ERPNEXT_BANK_ACCOUNT_GROUP_NAME` if
your Chart of Accounts names the bank group differently ("Bank", "Cash and
Bank"), and `ERPNEXT_BANK_ACCOUNT_CURRENCY` for the fallback currency.

**Graceful fallback.** The GL wiring is best-effort. If the Chart of Accounts
can't be walked or created (a fresh install with no Assets branch, a permission
error, an unusual template), or ERPNext still rejects the company account with
*"Company Account is mandatory"*, the importer retries once as a **personal**
account (`is_company_account = 0`, no GL link) so the import still succeeds. When
that happens the sync log records the fallback with a note to **promote the Bank
Account to a company account manually in ERPNext** once your Chart of Accounts is
set up. Prefer to skip the company path entirely? Set
`ERPNEXT_DEFAULT_IS_COMPANY_ACCOUNT=false` to import every account as personal
from the start (no GL account is created).

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

### 4. Auto-Supplier + categorization rules (`/admin/rules`, v0.3.0)

Once transactions are posting, two layers automate the reconciliation that used
to be manual.

**Auto-Supplier creation** (on by default). When a pushed transaction carries a
merchant name we've never seen, the bridge normalizes it and find-or-creates a
matching ERPNext **Supplier**, caching the mapping locally so it's a one-time
cost per merchant. Normalization strips payment-processor / POS prefixes,
collapses marketplace aliases, drops trailing store IDs, and title-cases
ALL-CAPS names:

| Raw Plaid string | Normalized Supplier |
|------------------|---------------------|
| `SQ *STARBUCKS 92104` | `Starbucks` |
| `AMZN Mktp US*2X4B9` | `Amazon` |
| `TST* Some Cafe` | `Some Cafe` |
| `CHEVRON 0123456` | `Chevron` |
| `THE HOME DEPOT #8842` | `The Home Depot` |
| `Blue Bottle` | `Blue Bottle` (already clean) |

Review / correct the cache at `/admin/suppliers`. Turn it off with
`ERPNEXT_AUTO_CREATE_SUPPLIERS=false`.

**Categorization rules → Journal Entries** (off by default). Author rules at
`/admin/rules`; each has a **priority** (lower wins), a **match type**, a
**debit** + **credit** account, an optional **party**, and a Jinja description
template. On each newly-posted transaction the engine walks active rules in
priority order and the **first match** generates a Journal Entry — debit the
expense account, credit the bank (reversed for a deposit/refund) — inserted as a
**Draft** for review. Nothing fires until you set
`ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES=true`; until then you can still author and
**Test a rule** against a sample transaction. Match types:

- `merchant_exact` — `merchant_name` equals `match_value` (case-insensitive)
- `merchant_contains` — `match_value` is a substring of `merchant_name`
- `description_regex` — `re.search(match_value, description)` matches
- `plaid_category_matches` — `match_value` matches the Plaid category label
- `amount_range` — `min ≤ abs(amount) ≤ max`, with `match_value = [min, max]`

**Sample rules for common categories** (credit account is your Bank Account
docname; debit accounts are examples — match your Chart of Accounts):

| Priority | Name | Match type | Match value | Debit account | Party |
|---|---|---|---|---|---|
| 10 | Fuel | `merchant_contains` | `Chevron` | `Fuel Expenses - EC` | Supplier (auto) |
| 10 | Fuel (Shell) | `merchant_contains` | `Shell` | `Fuel Expenses - EC` | Supplier (auto) |
| 20 | Groceries | `plaid_category_matches` | `GROCERIES` | `Groceries - EC` | Supplier (auto) |
| 20 | Utilities | `plaid_category_matches` | `UTILITIES` | `Utilities - EC` | — |
| 30 | Rent | `description_regex` | `(?i)\brent\b` | `Rent - EC` | — |
| 40 | Payroll | `plaid_category_matches` | `PAYROLL` | `Salaries and Wages - EC` | — |

A rule with an empty **party name** but a **party type** of `Supplier` links the
auto-created Supplier for that transaction's merchant, so "Fuel → Chevron"
automatically parties the JE to the `Chevron` Supplier. Review generated JEs at
`/admin/generated_entries` (approve = submit in ERPNext, reject = cancel).

> ⚠️ Auto-JE generation is powerful and can write **bad ledger data** if a rule
> is wrong. It stays OFF until you explicitly opt in, and defaults to inserting
> **Drafts** (not submitted) so a human reviews before anything hits the books.

## Audit trail (`/admin/audit`, v0.3.0)

Everything the bridge does that changes state is written to a **permanent,
append-only** `audit_events` table — no TTL, never updated, never purged, so the
count only grows and the full lifecycle of any rule, Journal Entry, supplier, or
transaction is reconstructable after the fact.

**What's logged** (17 event types):

| Event | When |
|-------|------|
| `supplier_auto_created` / `supplier_edited` | a merchant's Supplier is first minted / manually relinked |
| `rule_created` / `rule_updated` / `rule_deleted` | rule CRUD, with before→after snapshots |
| `rule_matched` | per transaction: which rules were **evaluated** and which one **won** |
| `journal_entry_generated` / `_failed` | a JE is created (full accounts+amounts payload) or generation fails |
| `journal_entry_approved` / `_rejected` / `_submitted_to_erpnext` | review actions on a generated JE |
| `journal_entry_edited` | reserved for a future JE-edit flow |
| `bank_transaction_synced` / `bank_transaction_reconciled` | a transaction posts to ERPNext / is reconciled |
| `sync_run_started` / `sync_run_completed` | each poll, with aggregate counts |
| `rules_rerun` | an explicit "rerun rules" admin action |

Each event records the **actor** (`system`, `scheduler`, `admin_ui`, or a user
id), the **source IP** for UI actions, `subject_type` + `subject_id`, and JSON
`payload_before` / `payload_after`. The `/admin/audit` page is filterable by
every field and down to an individual subject (`?subject_type=…&subject_id=…`
shows just that record's history), each event has a **detail view** with the
full before→after JSON, and there's a filter-preserving **Export CSV** button.
`plaid_sync_log` rows carry the same `subject_id`, so an event's detail page also
surfaces the underlying ERPNext HTTP calls.

**Rules are non-destructive.** Editing a rule never overwrites it: the old
version is archived (`active=False`, `archived=True`) and linked forward via
`superseded_by` to a freshly-created row; deleting archives rather than removes.
The rules engine only ever sees live (active, non-archived) rules, so a past
auto-JE decision can always be traced back to the exact rule text that produced
it. View archived rules with **show archived / history** on `/admin/rules`.

**Rule changes are not retroactive.** Editing a rule affects only future
transactions — it never silently re-runs against history. To apply current rules
to past posted transactions that never matched, use the explicit **Rerun rules
on eligible transactions** button on `/admin/transactions`; it's logged as a
`rules_rerun` event.

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
| `ERPNEXT_DEFAULT_IS_COMPANY_ACCOUNT` | `true` | import Bank Accounts as company accounts; auto-creates + links a GL account, retries as personal on "Company Account is mandatory". Set `false` to import all as personal from the start |
| `ERPNEXT_BANK_ACCOUNT_GROUP_NAME` | `Bank Accounts` | Chart-of-Accounts group the auto-created GL accounts go under; set to match your CoA template ("Bank", "Cash and Bank") |
| `ERPNEXT_BANK_ACCOUNT_CURRENCY` | `USD` | fallback currency for a created GL account when Plaid reports no `iso_currency_code` |
| `ERPNEXT_FUZZY_MATCH_THRESHOLD` | `85` | v0.3.1 · name-similarity % (0–100) at/above which the importer reuses an existing GL account instead of creating a near-duplicate |
| `ERPNEXT_AUTO_CREATE_SUPPLIERS` | `true` | v0.3.0 · find-or-create the merchant's ERPNext Supplier on push so the transaction is linkable |
| `ERPNEXT_DEFAULT_SUPPLIER_GROUP` | `All Supplier Groups` | Supplier Group docname assigned to auto-created Suppliers (create retries without it on a link error) |
| `ERPNEXT_DEFAULT_SUPPLIER_COUNTRY` | `United States` | country assigned to auto-created Suppliers |
| `ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES` | `false` | v0.3.0 · master switch for the rules engine — **off by default** (a wrong auto-JE is worse than none) |
| `ERPNEXT_JOURNAL_ENTRY_AUTO_SUBMIT` | `false` | submit generated JEs (docstatus 1) vs leave as Draft for review |
| `ERPNEXT_JOURNAL_ENTRY_REVIEW_STATE` | `pending_review` | initial audit state for a freshly generated Draft JE |
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

154 tests cover Fernet encryption round-trip + key persistence, Plaid response
normalization, sync idempotency, deposit/withdrawal mapping, modified →
cancel+replace, removed → cancel, unmapped/disabled account handling, failed
push → error + retry, one-click account import, merchant-name normalization,
auto-Supplier cache hit/miss/config-disabled, the rules engine (every match
type, priority ordering, JE sign handling, one-JE-per-transaction idempotency,
non-destructive failure), the rules admin CRUD + test endpoint, the audit trail
(every state change writes an event, count grows monotonically, rule
supersede-vs-delete preserves history, CSV export, subject filtering), and every
admin page rendering. The Plaid SDK and ERPNext are mocked (`tests/fakes.py`), so
no network access or extra wheels are needed.

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
- Reconcile generated Journal Entries directly against the Bank Transaction (a
  one-click "reconcile" from `/admin/generated_entries`).
- Fuzzy merchant → Supplier matching (beyond exact-name find-or-create).
- Rule-authoring conveniences: clone a rule, import/export a rule set.

**Done:** ~~Merchant → ERPNext Supplier auto-create + rules-based transaction
categorization~~ + ~~full append-only audit trail with non-destructive rule
history~~ (v0.3.0).

## License

MIT — see [LICENSE](LICENSE).
