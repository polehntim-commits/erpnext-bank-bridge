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
- **Configurable automatic polling** via Plaid's cursor-based
  `/transactions/sync` — pick a cadence from cost-aware presets (hourly →
  monthly, or **manual only**) in the admin UI; **daily by default**. Plus an
  on-demand **Sync now** button, and an optional per-account daily call brake.
- **One-click account import** — create the matching ERPNext **Bank** +
  **Bank Account** records straight from the linked Plaid accounts (per row or
  all at once), deduped and auto-mapped, so there's no manual setup before
  transactions can post. Auto-created GL accounts are placed on the correct side
  of the chart by Plaid `type` — depository under **Bank Accounts** (Assets),
  credit cards under **Credit Cards** (Current Liabilities), loans under
  **Loans** — with precise subtypes and account numbers matching your chart's
  scheme (v0.3.9).
- **Creates ERPNext Bank Transaction records** ready for the built-in Bank
  Reconciliation Tool (deposit/withdrawal split from Plaid's amount sign).
- **Auto-creates ERPNext Suppliers** from merchant names (v0.3.0) — a never-seen
  merchant is normalized ("SQ \*STARBUCKS 92104" → "Starbucks") and find-or-
  created as a Supplier so the transaction is instantly linkable. On by default.
- **Rules-based Journal Entry generation** (v0.3.0) — user-configured rules
  (`/admin/rules`) match on merchant / description / Plaid category / amount and
  auto-generate a Journal Entry, inserted as a Draft for review. Rules name only
  the categorized **offset** account; the bank side comes from the transaction's
  linked account (v0.3.1). **Off by default** — opt in once your rules are trusted.
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

v0.3.9 — functional pilot. Runs the full Plaid Link → sync → ERPNext push loop
with a mocked-API test suite, one-click import of Plaid accounts into ERPNext
Bank / Bank Account records, auto-Supplier creation from merchant names, and a
rules engine that auto-generates Journal Entries. v0.3.1 polish: auto-created GL
Accounts get sequential `account_number`s under their numbered parent group; the
rule account dropdown lists every enabled leaf account (Bank included); a
fuzzy-match check reuses an existing near-duplicate GL Account instead of
creating a new one (with an operator confirm on the accounts page); and rules are
now **bank-account-agnostic** — a rule names only the categorized offset account
while the bank side is taken from the transaction's linked account. v0.3.2 makes
rule building easier: the match-value field autocompletes from merchants and
Plaid categories already seen locally (with per-merchant txn counts, dollar
totals and an "already has rule" badge), the Name field suggests a short name
from the merchant's category (`Fuel — Chevron`), and saving a rule warns when a
higher-priority active rule already shadows it. v0.3.3 fixes the merchant picker:
the custom dropdown now stays open and live-filters (case-insensitive substring)
as you type instead of collapsing, shows a "use as new" row when nothing matches,
and supports arrow-key navigation — its filter logic moved into a small tested
static JS module (`app/static/rule_dropdown.js`). v0.3.5 makes the app
**self-heal postgres app-role password drift** at boot: the recurring "password
authentication failed for user 'bankbridge'" failure that used to force a volume
wipe is now detected and auto-repaired via a deterministic **rescue superuser**
(`bridgeadmin`, created at init with an APP_SEED-derived password), logged as a
`db_auth_recovered` audit event; existing installs get a one-time manual rescue
script (see *Self-healing DB auth* below). v0.3.6 is a docs + hardening polish:
a **Production Deployment** guide for serving the Plaid OAuth callback over
HTTPS (Tailscale Funnel / Cloudflare Tunnel / nginx + Let's Encrypt); an
**optional HTTP Basic Auth** layer for `/admin` (`ADMIN_BASIC_AUTH_*`, off by
default) for public-facing installs; and a **user-configurable sync frequency**
with cost-aware presets (hourly → monthly, or manual only) — the default drops
from 6-hourly to **daily** (~4× cheaper on Plaid calls) with an optional
per-account daily call brake. v0.3.7 trims Plaid cost further by **throttling the
balance refresh** (`/accounts/get`) to at most once per Item per day
(`ACCOUNT_REFRESH_INTERVAL_HOURS`, default `24`) — those balances feed the
dashboard only, so on a sub-daily poll that's ~40% fewer Plaid calls/month; it
also documents Plaid **webhooks** as a way to drop polling cost to zero. v0.3.8
fixes the **Bank Account Subtype** bootstrap: the doctype `Bank Account
.account_subtype` links to is named `Bank Account Subtype` in ERPNext v15, but
Bank Bridge was probing `Account Subtype` (which doesn't exist — ERPNext answers
with an ImportError), so it wrongly marked the doctype unavailable, dropped the
`account_subtype` field from every imported Bank Account, and showed a persistent
*bootstrap partially failed* warning. Pointing the bootstrap at the real doctype
name provisions the subtype masters and populates the field. v0.3.9 corrects the
**Chart-of-Accounts placement** of imported accounts: a credit card is a
liability, not an asset, so credit-card GL accounts now land under **Current
Liabilities → Credit Cards** (auto-created) instead of the Assets-side *Bank
Accounts* group — the leaf stays `account_type = Bank` so Bank Reconciliation
still works on it; loans map to a **Loans** group on the Liabilities side, and
depository accounts are unchanged. The GL parent is chosen by Plaid `type`. Bank
Account **subtypes** are now mapped 1:1 onto the ten provisioned masters
(Checking / Savings / Cd / Money Market / Cash Management / Paypal / Credit Card
/ Line Of Credit / Current / Other) instead of the old coarse *Current / Other*
buckets. **Account numbering** is applied to auto-created groups and leaves,
matching the chart's existing scheme (groups on hundreds; range-numbered parents
like `2100-2400` handled) and skipped silently when a chart doesn't use numbers.
Leaves are ordered by **liquidity** (standard balance-sheet convention, most
liquid first): within a group each account slots into a reserved band keyed on
its Plaid subtype — Cash Management/Paypal → 1201.., Checking → 1211.., Savings/
Money Market → 1221.., CD → 1231.. under group 1200; Credit Card → 2501.., Line
of Credit → 2511.. on the Liabilities side. A new account always lands in its
rank's band without disturbing the others. Three idempotent one-shot bench
scripts under `app/scripts/` bring an existing install in line:
`migrate_credit_cards_to_liabilities.py`, `backfill_account_subtypes.py`, and
`backfill_account_numbers.py`. See the roadmap at the bottom.

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
   (**default 24 — daily**; set the cadence in the admin UI, or `0` for manual
   only). For each transaction it upserts a local mirror row (idempotent
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
| `categorization_rules` | user rules: match predicate → offset account + direction + party + template (bank side from the txn; v0.3.1) — v0.3.0 |
| `generated_journal_entries` | per-JE state record (state, rule, JE docname) — v0.3.0 |
| `audit_events` | **permanent, append-only** audit trail of every action (before/after JSON, actor, IP) — v0.3.0 |
| `plaid_sync_log` | HTTP-level action log (plaid_pull / erpnext_push / erpnext_supplier_auto_create, counts, errors); `subject_id` cross-links to `audit_events` |
| `plaid_link_state` | short-lived link-token bookkeeping for the OAuth handoff |

## Admin UI (LAN-only by default)

The admin UI carries no login by default — it is intended to run behind Umbrel
on a trusted LAN, never exposed to the Internet. If you expose the app publicly
to serve the Plaid OAuth callback, keep `/admin` on the LAN and optionally turn
on an HTTP Basic Auth layer (`ADMIN_BASIC_AUTH_*`) — see
[Deployment models](#deployment-models) and
[Production Deployment](#production-deployment-https-for-plaid-oauth).

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
- `/admin/plaid_settings` — Client ID / secrets / environment / redirect /
  webhook + **sync frequency** (cost-aware presets with a live cost estimate)
- `/admin/erpnext_settings` — ERPNext URL + API key/secret + Test / Verify doctype
- `/admin/sync_log` — recent sync activity

## Installation (Umbrel Community Store)

1. Add a Community Store that ships this app's manifest (Umbrel → App Store →
   ⋯ → Community App Stores). A ready-to-use `docker-compose.yml` is included;
   pair it with an `umbrel-app.yml` manifest in your Community Store to package
   the app.
2. Install **Bank Bridge**. It launches a Flask server (port 5202) plus a
   Postgres sidecar.
3. Open the app; you'll land on `/admin`.

### Docker image

There is **no prebuilt public image assumed** — the supported path is to build
from source, which always works from a clone of this repo:

```bash
docker build -t bank-bridge:0.3.9 app
```

(`app/` is the build context; the `Dockerfile` lives inside it. The stock
`docker-compose.yml` in `app/` already does this via `build: .`, so
`docker compose up --build` needs no separate `docker build`.)

If a prebuilt image has been published to Docker Hub you can pull it instead of
building:

```bash
docker pull polehntim/bank-bridge:0.3.6   # only if the tag exists on Docker Hub
```

Availability is not guaranteed by this README — if the pull 404s, build from
source with the command above.

## Deployment models

Bank Bridge supports two deployment postures. Pick one before you configure
Plaid, because it determines your redirect URI.

**1. LAN-only (default, simplest).** The whole app — admin UI *and* the Plaid
callback — is reachable only on your trusted LAN behind Umbrel's app proxy.
Works out of the box with a `http://umbrel.local:5202/...` redirect URI. This is
fine for **Plaid sandbox** and for production banks that don't use OAuth. No
extra setup, no auth needed.

**2. Public HTTPS callback (required for production OAuth banks).** Production
OAuth institutions (Wells Fargo, Chase, etc.) require an **`https://`** redirect
URI that Plaid can reach from the public Internet. You expose **only** the
callback paths — `/plaid/*` and `/api/plaid/*` — over HTTPS, while `/admin` and
everything else stay on the LAN. See
[Production Deployment](#production-deployment-https-for-plaid-oauth) for three
ways to do this.

> **When you expose the app publicly, enable admin auth.** Set
> `ADMIN_BASIC_AUTH_USER` and `ADMIN_BASIC_AUTH_PASS` as a belt-and-suspenders
> layer so that even if a proxy misconfiguration leaks `/admin`, it still
> requires credentials. Setting **both** turns it on; leaving **either** blank
> keeps the stock unauthenticated LAN behavior. Generate a strong password with:
>
> ```bash
> python3 -c "import secrets; print(secrets.token_urlsafe(24))"
> ```
>
> The password may be stored as plaintext or as a `werkzeug` hash
> (`generate_password_hash`). The Plaid callback and JSON API are **never** gated
> by this — only `/admin` is.

## Production Deployment (HTTPS for Plaid OAuth)

Production OAuth banks require an `https://` redirect URI reachable from the
public Internet. The goal of every pattern below is the same: **terminate TLS
and forward only `/plaid/*` and `/api/plaid/*` to port 5202**, keeping the admin
UI on the LAN. After setting one up, register the resulting HTTPS URL as the
redirect URI in **both** the Plaid dashboard (Developers → API → Allowed
redirect URIs) **and** the `PLAID_REDIRECT_URI` env var / `/admin/plaid_settings`.

Placeholders below use `<your-host>`, `<your-umbrel>`, `<your-tailnet>`, and
`<your-domain>` — substitute your own.

### Option A — Tailscale Funnel (recommended for Umbrel / self-hosters)

Free HTTPS on a `*.ts.net` name with no port forwarding, no DNS, and no
certificate management (Tailscale provisions the cert). Funnel can be
**path-restricted** so only the Plaid callback is public.

**Prerequisites**
- A [Tailscale](https://tailscale.com) account with the Umbrel host on your
  tailnet (Umbrel ships a Tailscale app; or install the daemon on the host).
- Funnel enabled for your tailnet (Tailscale admin console → *Access controls*;
  Funnel node attribute must be granted).

**Setup**
```bash
# Expose ONLY the two callback path prefixes over HTTPS → local port 5202.
tailscale funnel --set-path /plaid       http://localhost:5202/plaid
tailscale funnel --set-path /api/plaid   http://localhost:5202/api/plaid
tailscale funnel status          # shows the public https://<your-umbrel>.<your-tailnet>.ts.net URL
```
Your redirect URI is then
`https://<your-umbrel>.<your-tailnet>.ts.net/plaid/oauth_return`.

**Verify**
```bash
curl -sI https://<your-umbrel>.<your-tailnet>.ts.net/plaid/oauth_return
# Expect: HTTP/2 200 (the OAuth return page renders).
curl -sI https://<your-umbrel>.<your-tailnet>.ts.net/admin
# Expect: 404 — /admin is NOT funneled, so it stays LAN-only.
```

**Security note.** Only `/plaid/*` and `/api/plaid/*` are on the public
`ts.net` hostname; `/admin` is unreachable through Funnel and stays on the LAN.
Even so, set `ADMIN_BASIC_AUTH_*` as defense-in-depth.

### Option B — Cloudflare Tunnel

Free HTTPS via a Cloudflare-managed hostname, again with no port forwarding.
Good if you already use Cloudflare for DNS.

**Prerequisites**
- A Cloudflare account with a domain on Cloudflare DNS (`<your-domain>`).
- [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
  installed on the Umbrel host.

**Setup**
```bash
cloudflared tunnel login
cloudflared tunnel create bank-bridge
# Route a subdomain to the tunnel:
cloudflared tunnel route dns bank-bridge bank-bridge.<your-domain>
```
Create `~/.cloudflared/config.yml` that forwards only the callback paths and
returns 404 for everything else:
```yaml
tunnel: bank-bridge
credentials-file: /root/.cloudflared/bank-bridge.json
ingress:
  - hostname: bank-bridge.<your-domain>
    path: ^/plaid/.*
    service: http://localhost:5202
  - hostname: bank-bridge.<your-domain>
    path: ^/api/plaid/.*
    service: http://localhost:5202
  - service: http_status:404          # /admin and all else → 404 (not exposed)
```
Then run it (or install as a service):
```bash
cloudflared tunnel run bank-bridge
```
Your redirect URI is `https://bank-bridge.<your-domain>/plaid/oauth_return`.

**Verify**
```bash
curl -sI https://bank-bridge.<your-domain>/plaid/oauth_return   # Expect 200
curl -sI https://bank-bridge.<your-domain>/admin                # Expect 404
```

**Security note.** The `ingress` rules publish only the two callback prefixes;
the catch-all `http_status:404` keeps `/admin` off the public hostname. Enable
`ADMIN_BASIC_AUTH_*` as an extra layer.

### Option C — Nginx reverse proxy + Let's Encrypt

The classic self-hosted setup if you have your own domain and a host with ports
80/443 reachable. You manage DNS and a certificate (via Certbot).

**Prerequisites**
- A domain `<your-domain>` with an `A`/`AAAA` record pointing at your host.
- Ports 80 and 443 open to the host; `nginx` and `certbot` installed.

**Setup** — an nginx server block that proxies **only** the callback paths and
`444`s everything else:
```nginx
server {
    listen 443 ssl;
    server_name <your-domain>;

    ssl_certificate     /etc/letsencrypt/live/<your-domain>/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/<your-domain>/privkey.pem;

    location /plaid/ {
        proxy_pass http://127.0.0.1:5202;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
    location /api/plaid/ {
        proxy_pass http://127.0.0.1:5202;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
    location / { return 444; }        # drop everything else, incl. /admin
}
```
Provision the certificate:
```bash
sudo certbot --nginx -d <your-domain>
```
Your redirect URI is `https://<your-domain>/plaid/oauth_return`.

**Verify**
```bash
curl -sI https://<your-domain>/plaid/oauth_return   # Expect 200
curl -sI https://<your-domain>/admin                # Expect connection closed (444)
```

**Security note.** Only `/plaid/` and `/api/plaid/` are proxied; `location /`
returns `444` so `/admin` is never served publicly. Because a reverse proxy is
the easiest place to accidentally widen scope, set `ADMIN_BASIC_AUTH_*` too.

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

**Production OAuth needs HTTPS.** Plaid accepts a plain `http://…` redirect URI
in sandbox, but **production OAuth banks require an `https://` redirect URI**
reachable from the public Internet. You only need to expose the two callback
paths (`/plaid/*` and `/api/plaid/*`), not `/admin`. See
[Production Deployment](#production-deployment-https-for-plaid-oauth) for three
ready-to-use patterns (Tailscale Funnel, Cloudflare Tunnel, or nginx +
Let's Encrypt), then register the resulting HTTPS URL (e.g.
`https://<your-host>/plaid/oauth_return`) as the redirect URI in both the Plaid
dashboard and `PLAID_REDIRECT_URI`.

**Sync frequency & cost.** The Plaid settings page has a **Sync frequency**
picker with cost-aware presets — **Hourly**, **Every 6 hours**, **Daily**
(default), **Every 3 days**, **Weekly**, **Monthly**, or **Manual only**. A live
estimate shows the approximate Plaid calls/month and dollar cost for your linked
accounts (at `PLAID_PRICE_PER_CALL`, default `$0.30`). Because Plaid bills per
`/transactions/sync` call, cadence is the main cost lever: **daily** is ~4×
cheaper than the old 6-hourly default for the same practical value in most
reconciliation workflows. **Manual only** disables the background poll entirely —
transactions refresh only when you click **Sync now** on the dashboard (which
also shows a "last synced" reminder in that mode). For belt-and-suspenders cost
protection, set `PLAID_MAX_CALLS_PER_DAY` to cap pulls per account per day.

Each poll historically spent **two** billable Plaid calls per Item:
`/transactions/sync` (the transaction delta ERPNext actually reconciles on) and
`/accounts/get` (cached balances that feed the dashboard only). Since v0.3.7 the
balance call is throttled by `ACCOUNT_REFRESH_INTERVAL_HOURS` (default `24`) — it
fires at most once per Item per day, plus always on an Item's first sync, so a
sub-daily schedule stops paying for balance data no logic consumes. On the
6-hourly cadence that's ~8 → ~5 calls/day per Item (**~40% fewer Plaid calls per
month**); hourly saves closer to half. Set `ACCOUNT_REFRESH_INTERVAL_HOURS=0` to
restore an every-poll balance refresh if dashboard freshness matters more than
call cost. Daily/weekly/monthly cadences are unaffected (their polls are already
spaced past the refresh interval).

**Eliminate polling entirely with webhooks (optional).** Instead of polling on a
timer, you can set `PLAID_WEBHOOK_URL` to a public HTTPS endpoint on this app;
Plaid then *pushes* a notification the moment new transactions are available and
the bridge pulls the delta in near-real-time. With webhooks wired up you can set
the sync frequency to **Manual only** and drop the recurring poll cost to zero —
you keep only the on-demand `/transactions/sync` calls triggered by actual bank
activity. The trade-off is that webhooks require a public HTTPS URL Plaid can
reach (the same Tailscale Funnel / Cloudflare Tunnel / nginx setup documented in
[Production Deployment](#production-deployment-https-for-plaid-oauth) — just
register the app's webhook path as `PLAID_WEBHOOK_URL`), whereas timer-based
polling works fine on a purely LAN-only install.

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
`Current` and `Credit` **Bank Account Type** records and the **Bank Account
Subtype** records (Checking, Savings, Current, Other, …) if they're missing, so
the very first import can't fail on a missing link target. (`Bank Account
.account_subtype` is a `Link → Bank Account Subtype` in ERPNext v15; v0.3.8
fixed a bug where Bank Bridge probed the wrong doctype name — `Account Subtype`
— which ERPNext answers with an ImportError, so the subtype was silently dropped
from every import and the settings footer showed *Bank Account Subtypes:
unavailable ⚠*.) These masters are provisioned on Test Connection, by the
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
`/admin/rules`; each has a **priority** (lower wins), a **match type**, a single
**offset account** (the categorized, non-bank side) + a **direction**, an
optional **party**, and a Jinja description template. Rules are
**bank-account-agnostic** (v0.3.1): the bank side of the JE is taken
automatically from the transaction's own linked Plaid account, so one rule works
across every account. **Direction** defaults to `auto` (a withdrawal debits the
offset, a deposit/refund credits it); `always_debit` / `always_credit` force the
side for the rare reversal case. On each newly-posted transaction the engine
walks active rules in priority order and the **first match** generates a Journal
Entry, inserted as a **Draft** for review. Nothing fires until you set
`ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES=true`; until then you can still author and
**Test a rule** against a sample transaction. Match types:

- `merchant_exact` — `merchant_name` equals `match_value` (case-insensitive)
- `merchant_contains` — `match_value` is a substring of `merchant_name`
- `description_regex` — `re.search(match_value, description)` matches
- `plaid_category_matches` — `match_value` matches the Plaid category label
- `amount_range` — `min ≤ abs(amount) ≤ max`, with `match_value = [min, max]`

**Sample rules for common categories** (the offset account is the categorized
side; the bank side is filled in automatically from the transaction — examples,
match your Chart of Accounts):

| Priority | Name | Match type | Match value | Offset account | Direction | Party |
|---|---|---|---|---|---|---|
| 10 | Fuel | `merchant_contains` | `Chevron` | `Fuel Expenses - EC` | `auto` | Supplier (auto) |
| 10 | Fuel (Shell) | `merchant_contains` | `Shell` | `Fuel Expenses - EC` | `auto` | Supplier (auto) |
| 20 | Groceries | `plaid_category_matches` | `GROCERIES` | `Groceries - EC` | `auto` | Supplier (auto) |
| 20 | Utilities | `plaid_category_matches` | `UTILITIES` | `Utilities - EC` | `auto` | — |
| 30 | Rent | `description_regex` | `(?i)\brent\b` | `Rent - EC` | `auto` | — |
| 40 | Payroll | `plaid_category_matches` | `PAYROLL` | `Salaries and Wages - EC` | `auto` | — |

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
| `ADMIN_BASIC_AUTH_USER` | "" | v0.3.6 · optional Basic Auth username for `/admin`; set together with the password to enable |
| `ADMIN_BASIC_AUTH_PASS` | "" | v0.3.6 · optional Basic Auth password for `/admin` (plaintext or a `werkzeug` hash). **Both** vars set → auth on; **either** blank → off (LAN mode). Never gates `/plaid/*` or `/api/plaid/*` |
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
| `SYNC_INTERVAL_HOURS` | `24` | v0.3.6 · background poll cadence in hours (**daily by default**). `0` or negative = **manual only** (no auto-poll; use "Sync now"). Editable in the admin UI, which persists a value that wins over this seed |
| `PLAID_MAX_CALLS_PER_DAY` | `0` | v0.3.6 · optional per-Item safety brake — max Plaid pull calls per Item per UTC day (`0` = no limit). A pull that would exceed it is skipped with a logged warning |
| `PLAID_PRICE_PER_CALL` | `0.30` | v0.3.6 · indicative $/Plaid call used only to render the admin cost estimate — not billing |
| `ACCOUNT_REFRESH_INTERVAL_HOURS` | `24` | v0.3.7 · min hours between billable `/accounts/get` balance refreshes (dashboard-only data). Refreshes at most once per Item per day, plus always on first sync — ~40% fewer Plaid calls/month on sub-daily schedules. `0` = refresh every poll |
| `SCHEDULER_ENABLED` | `true` | set false to disable the in-process scheduler entirely (drive syncs by external cron instead) |
| `AUTO_RECOVER_DB_AUTH` | `true` | v0.3.5 · self-heal postgres app-role password drift on boot (see below); set `false` to disable |
| `DB_RESCUE_USER` | `bridgeadmin` | v0.3.5 · deterministic rescue superuser created at init; used to reset a drifted app-role password |
| `DB_RESCUE_SALT` | `bankbridge-rescue-v1` | v0.3.5 · HMAC salt for the rescue password (`HMAC-SHA256(key=APP_SEED, msg=salt)`); don't change post-init |
| `DB_RESCUE_SEED` | `POSTGRES_PASSWORD` → `SECRET_KEY` | v0.3.5 · the APP_SEED the rescue password derives from (override only to pin it) |
| `DB_SUPERUSER` | "" | v0.3.5 · OPTIONAL extra superuser tried first (the stock db has none; rescue user is the real path) |
| `DB_SUPERUSER_PASSWORD` | `POSTGRES_PASSWORD` → `SECRET_KEY` | v0.3.5 · password for `DB_SUPERUSER` |
| `DATA_DIR` | `/data` | Fernet key + settings JSON + scheduler lock |

> ⚠️ **Back up `{DATA_DIR}/fernet.key`.** It decrypts every stored Plaid
> access token. Losing it means re-linking every bank.

### Self-healing DB auth (v0.3.5)

Postgres persists the `bankbridge` login role's password from the **first** time
its data volume was initialized. If a later deploy hands the app a *different*
password than that first-init value — e.g. an early init ran while `APP_SEED`
was blank because compose was started by hand over SSH — every connection then
fails with:

```
psycopg2.OperationalError: password authentication failed for user "bankbridge"
```

Previously the only fix was to wipe the postgres volume and reinstall, losing
Plaid tokens and local history. And because the compose runs the db with
`POSTGRES_USER=bankbridge`, `bankbridge` is the **sole** superuser — there is no
stock `postgres` account to fall back to (`psql -U postgres` →
`role "postgres" does not exist`), and when *its* password has drifted you can't
log in as it either. So v0.3.5 introduces a deterministic **rescue superuser**.

**Fresh installs** create a second superuser, `bridgeadmin`, at first volume init
(`scripts/initdb.d/10-create-rescue-superuser.sh`, mounted into the db in
`docker-compose.yml`). Its password is derived deterministically as
`HMAC-SHA256(key=APP_SEED, msg=DB_RESCUE_SALT)` — so the same APP_SEED reproduces
the same password on every boot, and the app can always re-derive it. At boot:

1. The app probes the DB with `SELECT 1`.
2. On success → normal boot (the common path; one cheap query).
3. On a **password authentication** failure specifically, it opens a superuser
   connection — trying an optional `DB_SUPERUSER` first, then re-deriving the
   `bridgeadmin` password from APP_SEED — and `ALTER USER`s the app role's
   password to match the value the app is already authenticating with (the
   password in `DATABASE_URL`), then re-probes. Success → it logs
   `auto-recovered DB auth drift` and continues; the fix is transparent.
4. If no superuser candidate is reachable it logs a loud warning pointing at the
   manual script (below) and continues without crashing.

Every successful recovery writes a `db_auth_recovered` **audit event** (actor
`system`, noting which superuser was used) so the heal is visible at
`/admin/audit`. The actual `APP_SEED` / password values are never logged.

**Existing installs** predate `bridgeadmin` (their volume only has the drifted
`bankbridge`), so boot recovery has no superuser to reach. Run the one-time
manual rescue on the Umbrel host:

```bash
cd app
./scripts/rotate_db_password.sh        # auto-detects the db + server containers
```

It temporarily enables trust auth on the db container's own loopback, resets the
`bankbridge` password to the current APP_SEED, **creates `bridgeadmin`** (so
future drift self-heals), restores the original `pg_hba.conf`, and restarts the
server. No volume wipe; Plaid tokens and history are preserved.

> Set `AUTO_RECOVER_DB_AUTH=false` to opt out of the boot self-heal entirely.
> Changing `DB_RESCUE_SALT` after init makes the derived password stop matching —
> don't, unless you also re-run the rescue script.

## Local development

```bash
cd app
cp .env.example .env          # fill in POSTGRES_PASSWORD etc.
docker compose up --build     # admin UI at http://localhost:5202/admin
```

## Tests

```bash
cd app
python3 -m unittest discover -s tests -v
```

247 tests cover Fernet encryption round-trip + key persistence, Plaid response
normalization, sync idempotency, deposit/withdrawal mapping, modified →
cancel+replace, removed → cancel, unmapped/disabled account handling, failed
push → error + retry, one-click account import, merchant-name normalization,
auto-Supplier cache hit/miss/config-disabled, the rules engine (every match
type, priority ordering, JE sign handling, one-JE-per-transaction idempotency,
non-destructive failure), the rules admin CRUD + test endpoint, the audit trail
(every state change writes an event, count grows monotonically, rule
supersede-vs-delete preserves history, CSV export, subject filtering), every
admin page rendering, the optional admin Basic Auth gate (enforced only when
both env vars are set, correct vs wrong credentials, plaintext + hashed
passwords, and the Plaid callback / JSON API staying ungated), and the
configurable sync frequency (preset cost math, manual-only disabling the
scheduler, interval persistence, and the per-Item daily call brake). The Plaid
SDK and ERPNext are mocked (`tests/fakes.py`), so no network access or extra
wheels are needed.

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
- Multi-select of Plaid categories on a single rule (currently one category per
  rule; the picker is single-select).

**Done:** ~~Merchant → ERPNext Supplier auto-create + rules-based transaction
categorization~~ + ~~full append-only audit trail with non-destructive rule
history~~ (v0.3.0). ~~Rule-builder autocomplete (merchants + Plaid categories),
category-based Name suggestions, and shadow-conflict warnings~~ (v0.3.2).

## License

MIT — see [LICENSE](LICENSE).
