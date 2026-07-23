# erpnext-bank-bridge

A self-hosted [Umbrel](https://umbrel.com) app that syncs bank transactions
from [Plaid](https://plaid.com) into [ERPNext](https://erpnext.com) for
automated accounting reconciliation. Designed for small businesses running
self-hosted ERP who want their bank feed in ERPNext without a SaaS accounting
middleman.

> **Self-hosted and private.** Your data stays on your own hardware — see
> [PRIVACY.md](PRIVACY.md). The software is provided as-is under the MIT
> License with no warranty — see [TERMS.md](TERMS.md).

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
- **Bank statement PDFs + monthly reconciliation** (v0.4.9) — pulls the
  statements your bank issued from Plaid, stores the PDFs, and checks each month
  against what Bank Bridge mirrored: *does the statement's opening balance plus
  our transactions land on the bank's closing balance?* A mismatch is shown as a
  dollar delta for a named month at `/admin/statements`. Statement balances can
  also replace the estimated opening balance on accounts linked before v0.4.4 —
  but only for a statement that reconciles. See
  [Bank statements](#bank-statements-v049).
- **Statements inside ERPNext** (v0.4.10) — every statement is also uploaded to
  ERPNext as a `Bank Statement` record with the PDF attached, so the bookkeeper
  and the CPA can read March's statement from the account they are already
  looking at instead of logging into Bank Bridge. The doctype provisions itself
  at startup, the same way the Counterparty overlay does. See
  [Bank Statement doctype](#bank-statement-doctype-v0410).
- **Auto-creates ERPNext Suppliers** from merchant names (v0.3.0) — a never-seen
  merchant is normalized ("SQ \*STARBUCKS 92104" → "Starbucks") and find-or-
  created as a Supplier so the transaction is instantly linkable. On by default.
- **Rules-based Journal Entry generation** (v0.3.0) — user-configured rules
  (`/admin/rules`) match on merchant / description / Plaid category / amount and
  auto-generate a Journal Entry, inserted as a Draft for review. Rules name only
  the categorized **offset** account; the bank side comes from the transaction's
  linked account (v0.3.1). **Off by default** — opt in once your rules are trusted.
- **Intercompany transfer detection** (v0.4.1) — money moved between two ERPNext
  Companies you own arrives as two Plaid transactions (equal amount, opposite
  sign). Bank Bridge matches them and books a **Due from / Due to** pair across
  the two entities instead of letting a generic rule put an expense on one P&L
  and income on the other. Review, approve or unpair at `/admin/intercompany`.
  Auto-activates once accounts under a second Company are linked.
- **One identity per counterparty** (v0.4.5) — ERPNext keeps Customer and
  Supplier in unrelated doctypes, so a party that both buys from you and sells
  to you is two records with no link between them. Bank Bridge adds a
  **Counterparty** overlay that pairs them, giving one net position, one
  combined ledger, and a **1099-eligible list that can't include your bank**.
  See [Counterparty overlay](#counterparty-overlay-v045).
- **Disconnect a bank when you're done with it** (v0.4.7) — one button on the
  Accounts page calls Plaid's `/item/remove`, so Plaid stops pulling from that
  institution. Your transactions and the Journal Entries generated from them
  **stay** — disconnecting ends the feed, it doesn't erase history — and you can
  re-link the same bank later. See [Disconnecting a bank](#disconnecting-a-bank-v047).
- **Guided rule authoring** (v0.4.6) — filter the Transactions tab down to what
  **no rule caught**, see it grouped by merchant, and open the Rules editor
  pre-filled from a whole group in one click. The Rules list shows a **Matches**
  count so dead rules stand out, and a Company scope mistake is caught **when you
  save the rule** rather than silently blocking every Journal Entry it generates.
  See [Rule authoring](#rule-authoring-v046).
- **Books opening balances** (v0.4.4) — every real bank account already holds
  something on the day you link it. Bank Bridge books that against an
  auto-created **Opening Balance Equity** account, so the balance sheet shows
  what an account *holds* rather than what has *moved through it since linking*.
  Direction follows the account's side of the chart and Plaid's per-type sign
  convention, so credit cards open as liabilities and an overdrawn account
  flips. Draft for review, never double-booked; a backfill script covers
  accounts linked before v0.4.4.
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

v0.4.16 — functional pilot. Runs the full Plaid Link → sync → ERPNext push loop
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

**v0.4.0** bundles four changes. **(1) Dependabot** is enabled (`pip` + `docker`,
weekly, grouped). **(2) Multi-entity (Level 1):** you can choose the **owning
ERPNext Company** for each linked bank at Plaid Link time — the choice is stamped
on the Item and inherited by its accounts, and it drives the `company` on the
Bank Account, its GL leaf, and generated Journal Entries. A correction-only
per-account override lives on the Accounts page, and a drift guard refuses to
post a transaction whose ERPNext Bank Account Company no longer matches (logged
as a `company_drift_detected` audit event). Installs that never pick a Company
resolve to the Default Company and behave exactly as before. **(3) Balance-only
investments:** Plaid `investment` accounts (401k, IRA, brokerage, crypto, …) are
now supported balance-only — a Bank Account + GL leaf under *Non-current Assets →
Investments* (subdivided into Retirement / Marketable Securities / Digital Assets
/ Other), with the balance mirrored on each refresh and `/transactions/sync`
skipped (Plaid returns no investment transactions without the `investments`
product). **(4)** New **PRIVACY.md** and **TERMS.md** for open-source disclosure.
Backward-compatible: existing installs upgrade with no manual steps.

**v0.4.0.1** — hotfix for the v0.4.0 upgrade path. The startup migration runner
adds the new `owning_company` / `balance_only` columns via inspected
`ALTER TABLE … ADD COLUMN`, but on an existing (v0.3.x) database the v0.3.1 rule
backfill — which issues `PlaidAccount.query`, a SELECT that lists *every* model
column — ran **before** those columns were added, raised `UndefinedColumn`, and
the fail-open handler swallowed the remaining ADDs. The columns never landed and
every later `PlaidItem` query 500'd (`column plaid_items.owning_company does not
exist`). Fixed by ordering all additive `ADD COLUMN` steps (now a single
`SCHEMA_MIGRATIONS` list) ahead of every ORM-based backfill, so the schema is
fully migrated before any model is queried. Fresh installs are unaffected (a
silent no-op). Existing v0.3.x → v0.4.0.1 upgrades now migrate cleanly on boot.

v0.4.0.1 also surfaces the owning **Company** across the admin UI so a
multi-entity operator never loses track of whose books they're looking at: a
**Current Company** switcher in the navbar (a dropdown when the bridge holds
accounts under more than one Company, the name alone when there's just one, and
nothing on a single-entity install) sets a session-wide scope that filters every
screen. The **Accounts** page shows each linked bank's owning Company with a
correction-only **[Change]** control (confirmation-gated, since it retroactively
re-assigns the bank's accounts). **Transactions** and **Rules** gain a Company
filter plus a persistent "Viewing … for {Company}" header. Rules can now be
**Company-scoped** — a new nullable `applies_to_company` column (NULL =
company-agnostic, applies everywhere) that the rules engine honours, firing a
scoped rule only for transactions whose bank account resolves to that Company.

**v0.4.0.2** — fixes a cross-Company posting bug in the Rules editor. The
**Offset Account** dropdown was scoped to the ERPNext *default* Company
regardless of the rule's or the operator's scope, so a rule authored while scoped
to Company X could silently pick Company Y's expense account and post a Journal
Entry into the wrong entity's books. The dropdown now resolves its Company in
priority order — the rule's **Applies to Company** first, then the active session
scope, and only when neither is set does it list **every** Company's accounts
(each shown with its `- <abbr>` Company suffix so the choice is deliberate). The
feed re-scopes live when the rule's Company select changes, and is cached per
Company for the session (invalidated when the navbar scope switches). As a
belt-and-suspenders backstop, a **push-time guard** now refuses to post any
Journal Entry whose referenced GL account belongs to a different Company than the
target — it marks the JE `blocked`, records a
`journal_entry_blocked_cross_company` audit event, and never touches ERPNext —
so even a mis-scoped rule that slips through can't corrupt another entity's
ledger.

**v0.4.0.3** — makes Company-agnostic rules actually work across entities with
**two-mode offset accounts**. A rule's offset is now interpreted by its scope:

- **Scoped rule** (**Applies to Company** set) — *Mode A*: the offset is a
  specific, fully-qualified GL account (`Meals & Entertainment - BBT`), used
  verbatim. The offset dropdown lists that Company's real accounts. (Unchanged
  from v0.4.0.2.)
- **Agnostic rule** (Applies to Company = *all Companies*) — *Mode B*: the offset
  is a **logical account name** (`Meals & Entertainment`). At posting time it's
  resolved to *the transaction's own Company's* chart, so **one** "Uber Eats →
  Meals" rule books to each Company's own Meals account. The offset dropdown
  offers deduplicated logical names across every Company, and the form labels the
  active mode as you toggle **Applies to Company**.

If a transaction's Company has no account with that logical name, the Journal
Entry is **skipped** (state `skipped_missing_account`) rather than posted or
auto-created — a `journal_entry_skipped_missing_account` audit event is recorded
so you can provision the missing account and re-run. An agnostic rule whose
offset is still fully-qualified (a legacy value, or a single-Company install) is
used verbatim, and the v0.4.0.2 push-time guard remains its cross-Company
backstop. On upgrade, existing agnostic rules with a pinned, fully-qualified
offset are **auto-migrated** to a logical name on boot (idempotent; scoped rules
and names that legitimately contain ` - ` are left untouched).

**v0.4.0.4** — the Rules editor now **auto-fills the Description Template** from
the Match Type + Offset Account so a new rule gets a sensible Journal Entry
remark with zero typing. Pick an offset and the template populates (e.g. a
`merchant_exact` rule offset to *Fuel Expense* → `{{merchant_name}} - Fuel
Expense`); each match type has its own default. It never overwrites text you've
typed — a manual edit takes ownership, and a **↺ reset to default** link hands it
back. A **live preview** below the field renders the template against your most
recent matching transaction (or sample data when none match yet). Templates are
plain `{{variable}}` strings — `{{merchant_name}}` (falls back to the raw
description), `{{description}}`, `{{amount}}` (signed, e.g. `42.50 USD`),
`{{plaid_category}}`, `{{date}}` — and a variable that can't be resolved renders
empty, with the leftover ` - ` separators compacted away so the remark always
reads cleanly. Existing rules are unchanged: a rule with a template keeps it, and
a blank template still yields the same default remark.

**v0.4.0.5** — **fixes the Generated Journal Entries approval workflow**, which
silently did nothing. **Approve** submitted a bare `{doctype, name}` stub to
ERPNext's `frappe.client.submit`, and because that method submits *the object it
is handed* (it does not reload the record), ERPNext was asked to submit an empty
Journal Entry — no accounts, nothing that balances — which it rejected. The JE
stayed **Draft**, the local row stayed `pending_review`, and the only feedback
was a generic "could not submit." The fix **fetches the stored Journal Entry
first and submits that**, so the accounts, company and totals ERPNext validates
are the ones already on the record (Draft → Submitted). Every failure now
**surfaces the actual ERPNext reason** instead of failing silently. The page
gains a proper **state machine** — `pending_review → approved` (submit),
`pending_review → rejected` (Draft abandoned), `approved → rejected` (cancels the
submitted JE), `approved → reversed` (books an offsetting **Reverse** entry to
undo an approval), and `skipped_missing_account → pending_review` (**Retry** once
its account exists) — with guards (you can't approve a rejected entry) and
idempotent re-clicks. **Approve/Reject/Reverse/Retry** each POST to their own
endpoint; a checkbox column plus **Approve selected / Reject selected** apply an
action to many rows at once (nothing checked = all pending) with **partial-
success reporting** so one failure never rolls back the rows that succeeded.
Actions refresh **just their row** (JSON response, no full-page reload) and fall
back to a plain form POST + flash without JavaScript. Every transition records an
audit event (`journal_entry_approved` / `_rejected` / `_reversed` /
`_submitted_to_erpnext`). Existing `pending_review` entries approve normally after
the upgrade — no migration.

**v0.4.0.6** — **fixes the Offset Account dropdown serving a stale Chart of
Accounts**. An account created in ERPNext (e.g. `Interest Income - BBT` under
Income → Indirect Income) did not appear in the Rules editor picker, and — unlike
what v0.4.0.2 intended — **switching Company did not help**. The offer list was
cached in *two* places: a per-Company server cache that only cleared on a scope
change, and a **browser `sessionStorage` copy that outlived the Company toggle
entirely**, so the toggle cleared the server cache and the browser then served the
same stale list straight back. The account query itself was fine — it has always
covered **all five root types** (Asset, Liability, Equity, Income, Expense) with
no `root_type` / `account_type` filter, so the missing account was purely a cache
artifact. Now **opening the Rules editor always re-reads the chart from ERPNext**:
the page drops the server cache and its account fetch carries `?fresh=1`, which
bypasses both layers. Caching is otherwise unchanged — repeat requests within a
visit still hit the cache, so paging the admin UI doesn't hammer ERPNext. A new
**↻ refresh accounts** link beside the Offset Account field covers the one case a
page-load hook can't: creating the account in another tab while the editor sits
open. It clears **every** cached feed (the new account belongs in the
Company-agnostic logical-name list too) and reports how many accounts came back.
Create an account in ERPNext, reload the Rules editor, and it's selectable — no
Company toggle, no restart. Regression-tested against a chart spanning all five
root types, with group and disabled accounts still correctly excluded.

**v0.4.16** — **the "postgres auth drift" was never a password problem.**
Five releases chased a bug that reported itself as `password authentication
failed for user "bankbridge"` and looked exactly like the app role's password
drifting from the value baked into the postgres volume. v0.3.5 added a rescue
superuser, v0.4.7 fixed the rotate script, v0.4.11 scoped another pass. It kept
coming back, and on v0.4.15 it started 500ing the Plaid Settings page.

The password was correct the whole time. The app was connecting to **another
app's database**.

On Umbrel every app shares one flat Docker network, and nearly every app names
its postgres service `db`. Docker registers a network alias per service name, so
on a box running several such apps the name `db` resolves to *all* of them:

```
db → 10.21.0.73  (ours)
     10.21.0.66  (another app's postgres — answers, rejects our credentials)
     10.21.0.70  (another app's postgres — refuses the connection)
     10.21.0.24  (another app's postgres — refuses the connection)
```

libpq walks those addresses, and the two failure modes differ in a way that
makes the bug intermittent: a `Connection refused` falls through to the next
address, but a `password authentication failed` is **fatal** and stops the
connect dead. Docker randomizes the answer order per lookup, so whether a
connection worked came down to where the one *neighbouring* Postgres that
accepts TCP landed in the list. That is why boot self-heal could report success
and the same process 500 two hours later, and why every fix aimed at the
password derivation missed.

Two layers of fix, because they fail independently:

- **The database service is now `bankbridge-db`, not `db`** — a unique alias, so
  the name is unambiguous in the first place. This is the app-name-prefix
  convention this project already mandates for Funnel paths, applied to the
  Docker network namespace.
- **Connections no longer trust the name.** Bank Bridge resolves the DB host
  itself, and on an ambiguous name probes the candidates and uses only the one
  that proves it is our database (it authenticates *and* reports our database
  name). The winning address is pinned for reuse and re-probed if the container
  moves. Cost on an unambiguous host: one `getaddrinfo`, no probing.

Boot now logs what the DB host resolves to, and warns loudly when the alias is
shared — the observability whose absence let this hide for five releases.

**Recovery can no longer damage a neighbouring app.** The self-heal reacts to
`password authentication failed` by connecting as a superuser and running
`ALTER USER ... PASSWORD`. It had no check that the cluster it reached was ours,
so a shared alias put it one credential coincidence away from rewriting another
app's role password. It now verifies the cluster hosts our database *and* our
role before altering anything, and skips it otherwise.

> **Migration — nothing to do.** Restart Bank Bridge and the new connection
> path applies immediately; it fixes the 500s even on the old compose, before
> the service rename lands. Redeploying the compose renames the db container,
> which is safe — the data is on the named volume, not the service. No password
> is changed, no volume is touched, no data moves.

> **If you run other Umbrel apps built from this template** (BucketLog,
> VolumeVision), they very likely carry the same hazard: a `db` service name on
> the shared network, plus a recovery path that will `ALTER` whatever Postgres
> answers. Give each one an app-prefixed alias.

**v0.4.15** — **re-linking a bank stops colliding with your own records.**
v0.4.11 promised an idempotent reconnect and did not deliver one. Disconnecting
a bank and linking it again still failed with `DuplicateEntryError` on account
after account, and the loan types v0.4.14 added failed the same way.

**Why.** A Bank Account was deduped on exactly one key — the `plaid_account_id`
custom field — and a re-link mints a brand-new Plaid `account_id` for the same
real account. That key misses by construction, and the create that follows
collides with the record already on your books, because ERPNext autonames a Bank
Account `<account_name> - <bank>` and none of the parts that feed it changed.

v0.4.11 tried to close this one layer up: fingerprint the new local rows against
the retired ones and move the mapping across, so the import short-circuits on
"already mapped". That works when it fires — but it is a best-effort matcher
with several honest ways to decline (Plaid returned no mask, the older Item
carries no `institution_id`, more than one candidate matched), and **every**
decline fell through to the unguarded create. It also only ever ran for
newly-seen accounts inside the reconnect path, so the loan and investment types
were never covered at all.

**The fix asks ERPNext instead of guessing.** When the Plaid id misses, the
import now looks up the Bank Account this real account is already on the books
as, in two tiers, stopping at the first hit:

1. **Bank + last-4 + Company** — the tier that fires on virtually every re-link.
2. **Bank + subtype + Company**, and only when exactly one record matches — for
   the rare case where the bank actually reissued the account and the last-4
   changed.

An adopted record is repointed at the new Plaid id and the adoption is written
to the audit trail with the tier that matched. Because this lives in the shared
find-or-create path it covers depository, credit, **loan** and **investment**
accounts in one place, which is the half v0.4.14 missed.

**Three things it deliberately will not do.** It never adopts across Companies —
a record under another Company is another entity's books, so the correct move is
to create a fresh one under the target. It never takes a record that a *live*
Plaid account still claims, because two accounts pointing at one Bank Account
double-post every transaction in the overlap (a record is fair game only when
the account claiming it is gone, superseded, or belongs to a bank you
disconnected). And on the subtype tier, two candidates means it declines and
tells you, rather than welding one real account's ledger onto another.
Under-adopting costs a manual mapping; over-adopting corrupts the books quietly.

**New: `/admin/accounts/cleanup`.** Lists every ERPNext Bank Account no live
Plaid account claims — the leftovers from dry-runs and from imports that
targeted a different Company — grouped by bank, with a delete-or-ignore choice.
Deletion is idempotent, re-checks that the record is still unlinked before
acting, and reports (rather than forces) ERPNext's refusal to delete anything
with linked documents.

> **Migration — if you hit duplicate errors on v0.4.11–v0.4.14.** Deploy
> v0.4.15, then open **Accounts → Clean up unlinked accounts** and delete the
> stale records from your earlier attempts (anything ERPNext refuses to delete
> has real documents attached — leave it). Then disconnect and re-link the bank
> as normal. Records that survive under the Company you are importing into are
> adopted rather than duplicated; nothing needs to be re-mapped by hand.
> Fresh installs are unaffected, and no existing mapping changes on upgrade.
> `ERPNEXT_ADOPT_EXISTING=false` restores the old single-key behaviour.

**v0.4.14** — **the mortgage joins the books.**
Until now `is_supported` refused every loan account, so a farm with a $200,000
orchard mortgage had a balance sheet overstating its net worth by exactly that,
and the payments leaving the chequing account had nowhere honest to go. The
obvious workaround is wrong twice over: categorize the whole $2,000 payment as
an expense and you overstate expenses by the principal portion **and** still
leave the debt off the balance sheet. Principal repayment isn't an expense — it
settles a liability.

Loans now import as liabilities (GL leaf under Liabilities → Loans, Bank Account
Type `Loan`, subtypes Mortgage / Student / Auto / Home Equity / Construction /
Commercial / …), and their opening balance books on the credit side.

**The payment is booked as two entries, not one.** The textbook entry is three
lines — `Dr Loan / Dr Interest / Cr Bank` — and nothing here can emit three
lines: every Journal Entry builder in this app produces exactly two, and a rule
has a single offset account. It would also be the wrong shape, because a
mortgage's principal/interest split **changes every month**, so any ratio stored
on a rule is wrong from month two. So it decomposes:

```
payment    Dr Loan Liability      Cr Bank              ← an ordinary rule
accrual    Dr Interest Expense    Cr Loan Liability    ← generated automatically
```

Net effect is identical, and it's more faithful to what actually happens:
interest accrues against the loan, payments settle the balance. Nothing stores a
split, so a changing ratio needs no special handling at all. The payment half
needs no new machinery — once the loan has a GL account it appears in the rules
editor's dropdown like any other account.

**Exact figures only, never an estimate.** Interest comes from the lender's own
year-to-date total, differenced against what was last booked. An amortization
estimate (balance × rate ÷ 12) was considered and rejected for the same reason
statements refuse to guess an unparseable balance: it diverges silently on an
extra payment, an escrow adjustment, a fee or a rate change, and nobody
reconciles a number they were never told was approximate. A lender that reports
no year-to-date interest gets no accrual, and the Accounts page says **manual**
rather than leaving a silent gap.

Two subtleties that would each be a real bug:

- **Loans are balance-only.** Plaid *does* return transactions for loan
  accounts, and posting them would double-count every payment — the money
  leaving chequing is already mirrored on the chequing side, which is where it's
  booked from. Balance-only accounts are now excluded from the push outright.
- **A misfiled mortgage is still a liability.** An institution can serve a
  mortgage from its deposit platform, so it arrives as `type='depository'`.
  Trusting the type would book six figures of debt as a chequing **asset** — a
  sign flip on the largest number on the balance sheet. Classification keys off
  the subtype too.

The `liabilities` product is requested at link time and degrades independently:
an application approved for `statements` but not `liabilities` still gets
statements, and a loan without liability data still imports and still tracks its
balance.

**v0.4.13** — **transactions that can never post stop costing something.**
A Plaid Item can carry accounts ERPNext has no home for. The realistic case is a
**mortgage or student loan sharing an Item with your checking account**: loans
aren't Bank Accounts in ERPNext's model, so `is_supported` refuses them — but
`/transactions/sync` keeps returning their payments, which mirror locally and can
never post.

Keeping those rows is correct; the transactions really happened, and dropping
them would lose history the account's own import would want. What was wrong is
that the push path **loaded every one of them on every run just to skip it**, so
the work grew without bound forever. Their ids were also fed into the
intercompany pair detector on each pass, and they inflated `skipped` — making
that number useless for the case it was meant to describe.

The scan is now restricted, in SQL, to accounts that can actually receive a
posting. Measured over six pushes with a growing loan backlog, rows scanned stays
**flat** while the backlog grows from 4 to 24. Nothing is flagged on the rows, so
the behaviour is **self-healing**: map the account (or re-enable sync) and the
entire backlog posts on the very next push, with no migration and no state to
unwind.

And it is no longer silent. The count appears in the push stats, in the sync log
(`unpostable=N`), and per-account on the Accounts page as *"N txns waiting"* —
because *"where did my mortgage payments go?"* deserves an answer on the page
rather than nothing at all.

**v0.4.12** — **investments stop lying on the balance sheet.**
v0.4.0 brought investment accounts in as *balance-only*: Bank Bridge creates the
Bank Account and GL leaf, books an opening balance, and mirrors each refreshed
balance onto an informational field on the Bank Account. That field is not a
posting — so the GL leaf kept the value it opened at, **forever**. A brokerage
that grew from $50,000 to $65,000 read $50,000 on the balance sheet, with the
real number visible only to someone who opened the Bank Account form and knew to
look.

Now the difference is posted as a Journal Entry. Value up debits the investment
and credits **Unrealized Gain/Loss on Investments**; value down reverses it. The
equity leaf is auto-created under the company's Equity branch, once, in the 3100
slot beside Opening Balance Equity.

**Why equity and not income.** An unrealized gain is a paper movement — the
market moved, the farm did nothing, and no cash exists. Routing it to income
would put market noise straight into the operating result, so a quarter where
the orchard did badly but the brokerage rallied would report a profit nobody can
spend. Equity keeps the balance sheet honest while leaving the income statement
to say what the *farm* did. It's also the reversible choice: point
`UNREALIZED_GAIN_ACCOUNT_NAME` at an income account and every future entry
follows.

**The delta is measured against the ledger, not against yesterday.** Each entry
posts the gap between the live balance and what the GL leaf currently reflects,
so entries compose: +5,000 then −2,000 then +1,000 leaves the leaf at
opening +4,000, which is what a running ledger has to mean. A movement under
`INVESTMENT_REVALUATION_MIN_DELTA` (default $1) is skipped — a brokerage moves
every day, and nobody wants to approve a Journal Entry for eleven cents — and
the skipped amount is not lost, because the next entry measures from the ledger
and absorbs it.

**The rule that makes this safe to upgrade into.** A NULL baseline means "we
don't know what the ledger reflects" — which is true of *every* investment
account the moment you upgrade. Posting in that state would book each account's
entire value as a fictional one-off gain. So the first pass over an account
**seeds** the baseline and posts nothing: from the booked opening balance when
there is one, otherwise from the current balance, so revaluation tracks change
from when the feature was switched on. It never invents a gain that didn't
happen. Entries land as `pending_review` Drafts, like opening balances, so a
human approves every one.

Also here, and overdue: **investment accounts get their own type and subtype.**
They were typed `Current` — describing a 401k as a chequing-class account — with
subtype `Other`, losing exactly the precision v0.3.9 added for depository
accounts on the class where *which kind of investment* is the whole question.
Now `Investment`, with Brokerage / Ira / Roth / 401K / Retirement / Hsa /
Mutual Fund / Stock / Bond / Crypto Exchange mapped 1:1. Plaid's underscore
form (`crypto_exchange`) resolves too.

**v0.4.11** — **reconnecting a bank without losing what you set up.**
Two problems, one expensive and one quietly destructive.

The expensive one: nothing detected Plaid's `ITEM_LOGIN_REQUIRED`. When a bank
decided it wanted you to sign in again, the poll loop kept calling it — a
failed, billable request every cycle, forever — and the first anyone knew was
that transactions had stopped arriving. Bank Bridge now recognises that state
from **two independent signals**: Plaid's ITEM webhook (instant, and free —
handling one makes no API calls at all) and the error text of a failed sync, so
an install with no public webhook URL still works, one poll later. A bank in
that state is **parked**: not polled, badged on the Accounts page, with a
Reconnect button. Parking it is a cost *saving* — the same reasoning v0.4.7
already applied to disconnected Items.

The destructive one: **a re-link is not a reconnect.** Plaid mints a new
`item_id` *and new `account_id`s* for a new connection, and everything this app
keys on `account_id` then misses. Your ERPNext mapping, Company assignment and
import status revert to blank — but worse, two invisible things break. The
ERPNext Bank Account dedup key (`plaid_account_id`) no longer matches, so the
next import tries to create a **duplicate Bank Account** whose name collides and
fails outright. And the opening-balance idempotency key
(`opening-balance:<account_id>`) no longer matches, so a **second opening
balance** becomes eligible for an account that already has one — silently
double-counting the starting position.

So there are now two paths, and the right one is usually the cheap one:

* **Reconnect** uses Plaid's *update mode*, which repairs the existing Item in
  place. `item_id` and every `account_id` survive, so nothing is re-mapped and
  no second billable Item is created at Plaid. This is what expired credentials
  need.
* **Re-link** (a genuinely new connection) now runs **fingerprint adoption**:
  each new account is matched against retired ones on
  (institution, last-4, type, subtype), and on an *unambiguous single match* the
  configuration is **moved** across — mapping, Company, import status, and the
  opening-balance entry, which is re-keyed so no second one can ever be booked.

Adoption moves rather than copies, deliberately: two rows naming one ERPNext
Bank Account would both push into it and duplicate every transaction in the
overlap. The donor is retired in the same operation and stamped with
`superseded_by_account_id`. Matching is exact and unambiguous-only — zero or two
candidates means you map that one by hand — for the reason the counterparty
pairing states: fuzzy matching would silently attach one bank account's ledger
history to a different bank account, and exact matching can only ever
under-adopt, which is the safe direction to be wrong in.

Also here: the disconnect modal used to end *"You can re-link this bank later"*
with no caveat, which was a promise the code did not keep. It now says what
actually happens, and points at Reconnect.

Cost control: the only webhook behaviour that spends Plaid calls beyond the
scheduled poll is the TRANSACTIONS sync kick, which has been on since the pilot.
It can now be turned off with `PLAID_WEBHOOK_TRIGGERS_SYNC=false` while keeping
the free ITEM re-auth handling.

**v0.4.10** — **the statements show up in ERPNext.** v0.4.9 fetched the bank's
own monthly PDFs and filed them under `/admin/statements`, which is a fine place
for them if you log into Bank Bridge. Your bookkeeper doesn't. Your CPA doesn't.
They live in ERPNext, and the question they ask in April is *"show me the March
statement for this account"* — which Bank Bridge could answer and ERPNext could
not.

So a **`Bank Statement` doctype** now provisions itself in ERPNext at startup,
the same idempotent REST bootstrap the Counterparty overlay uses, and every
statement Bank Bridge holds is uploaded into it **with the PDF attached to the
record**. Account, period, opening and closing balance, reconciliation status
and variance — all readable from the Awesome Bar, all filterable, all
exportable, without anyone learning a second tool.

The **reconciliation verdict flows one way**, Bank Bridge → ERPNext, and keeps
flowing: a statement synced in March was measured against the mirror *as it was
in March*, so when a later backfill closes a transaction gap the status is
re-pushed rather than left stale. Nothing read back from ERPNext ever changes
local state — two writers on one verdict is a conflict nobody wins.

Two reports come with it. **Discrepancies** lists statements whose variance
exceeds a threshold (default $10, and it compares the *absolute* variance — a
mirror $500 over is exactly as wrong as one $500 short). **Coverage** is the
more valuable one: it names the months an account has *no statement for*. An
unparseable statement is at least visible; one that was never fetched is visible
nowhere, and it is precisely the one that leaves a quarter unreconciled at tax
time. Both fall back to Bank Bridge's own rows when ERPNext is unreachable, and
say on the page which source they used.

Upgrade path: nothing to do. The two new columns backfill to NULL, which reads
as *"not in ERPNext yet"*, so every statement an install already holds is picked
up about twenty seconds after the container restarts. `python -m
scripts.backfill_erpnext_statements` does it on demand (with `--dry-run`), and
`python -m scripts.provision_bank_statement_doctype` provisions without a
restart. Both are idempotent, and both re-**adopt** rather than duplicate a
record that already exists — which is what makes them safe against a data volume
restored from a backup taken before the upload.

One guard worth naming: unlike `Counterparty`, `Bank Statement` is a plausible
enough name that another app could already own it. If this ERPNext has a
`Bank Statement` doctype without a `plaid_statement_id` field, Bank Bridge
declares the overlay unavailable and **refuses to write to it** rather than
scribbling on a stranger's records.

**v0.4.9** — **bank statements: reconciliation, and opening balances the bank
wrote down.** Every number Bank Bridge held until now was either a transaction
Plaid mirrored or something this app derived from those transactions. A statement
is neither — it is the institution's own monthly assertion — and that buys two
things.

First, **reconciliation**. `/admin/statements` lists each month's statement and
measures it: the statement's own opening balance, plus the movement Bank Bridge
mirrored in that period, against the bank's closing balance. When they agree the
month is marked reconciled; when they don't, the difference is shown as a dollar
delta. That is a *"do my books match my bank?"* check for a named month, without
opening ERPNext.

Second, **opening balances without arithmetic**. The v0.4.4 backfill works
backwards from today's balance by subtracting everything mirrored since, which is
exact only if the mirror is complete — which is exactly why those entries land in
`pending_review` for someone to check against a real statement. Running
`scripts/backfill_statements.py --rebook` replaces both the arithmetic and the
manual check with the bank's own figure.

**It only does that for a statement that reconciles.** Booking an opening balance
dated a statement's period start asserts the ledger from that date forward is
exactly the mirrored transactions, so Bank Bridge tests that claim rather than
assuming it: a statement whose closing balance the mirror cannot reproduce, or
one older than transactions already mirrored, is refused and the estimate stands.

Two honest limits worth knowing before you enable it:

- **Balances are parsed out of the PDF, because Plaid does not return them.**
  `/statements/list` carries a statement id, a month, a year and a posting date —
  no balances at all. So Bank Bridge reads the PDF text and looks for the labels
  US institutions actually print ("beginning balance", "previous balance",
  "ending balance", "new balance"). A layout it doesn't recognize — or a scanned
  statement with no text layer — yields **no balance**, never a guess. The PDF is
  still stored and viewable; that account just keeps its v0.4.4 behaviour.
- **The `statements` product must be approved on your Plaid application** and
  requested when the bank was linked. Bank Bridge now asks for it at Link time
  and **falls back to a transactions-only token if Plaid says no**, so linking
  banks keeps working unchanged on an application that hasn't been approved yet;
  statements start arriving on the next link after approval.

Existing installs are untouched on upgrade: the import path still books the
cached Plaid balance (at link time that *is* the bank's number — there is no
estimate there to improve on), so no balance sheet moves. Statements only ever
add information. Set `STATEMENTS_ENABLED=false` to opt out entirely.

**v0.4.8** — **every Plaid-facing path moved under `/bankbridge/`.** No
behavior change, one prefix change. A Tailscale Funnel hostname is per-*machine*,
not per-app: every app on the same Umbrel shares `https://<host>.<tailnet>.ts.net`
and is distinguished only by path. Bank Bridge owning the bare `/plaid/*` and
`/api/plaid/*` prefixes meant the next app to want a public callback would have
to negotiate for path space, and a line in an access log wouldn't say which app
it belonged to. So Bank Bridge now claims exactly one prefix, `/bankbridge/`, and
nothing outside it — see [Multi-app path prefix
convention](#multi-app-path-prefix-convention) and [Path migration
(v0.4.8)](#path-migration-v048) for the upgrade steps.

Old paths keep working: pre-v0.4.8 URLs answer with a permanent redirect to their
new home, logged at INFO so an operator can see whether anything still calls
them. A `plaid_settings.json` holding an old redirect / webhook URL is rewritten
onto the new prefix automatically on read — no manual edit, no re-linking banks.

**v0.4.7** — **disconnect a bank, and pre-submission hardening.** Five fixes
found in an audit ahead of requesting Plaid production access:

1. **Disconnect flow (`/item/remove`).** `PRIVACY.md` promised you could
   "disconnect a bank by removing the linked item" and no code implemented it.
   Each linked bank on `/admin/accounts` now has a **Disconnect this bank**
   button behind a confirmation modal. It calls Plaid's `/item/remove`, which
   invalidates the access token, then flags the Item locally — **in that order**,
   so a Plaid failure leaves the bank connected rather than silently marking it
   dead while Plaid keeps the token alive. Nothing is deleted: the Item, its
   accounts, every mirrored transaction and every generated Journal Entry are
   retained, and the entries already pushed to ERPNext are untouched. A
   disconnected Item shows a **🔌 Disconnected** badge, is skipped by the sync
   scheduler and by the (unauthenticated) webhook kick, and re-linking the bank
   later mints a brand-new Item without touching the old row. Audited as
   `item_disconnected` with the actor and reason. Additive schema: `disconnected`
   and `disconnected_at`, backfilling to "still linked".
2. **plaid-python 18.4.0 → 40.1.0.** Twenty-two major versions, and — verified
   against the installed SDK — **zero breaking changes to this codebase**: every
   request model and `PlaidApi` method the wrapper touches kept an identical
   constructor signature. The majors track Plaid's own API surface growing, not
   renames of what we call.
3. **gunicorn 21.2.0 → 22.0.0.** Patches two request-smuggling CVEs,
   [CVE-2024-1135](https://github.com/advisories/GHSA-w3h3-4rj7-4ph4) and
   [CVE-2024-6827](https://github.com/advisories/GHSA-hc5x-x2vx-497g), both fixed
   in 22.0.0. Runtime-only; no app code depends on gunicorn's API.
4. **`scripts/rotate_db_password.sh` works from inside the container.** It used
   to shell out to `docker`, which the app image does not contain — so the
   obvious invocation (`docker exec <server> bash scripts/rotate_db_password.sh`)
   died on `docker not found` before doing anything. It now **detects its
   execution context**: on the host it keeps the full trust-auth repair, and
   inside the container it talks to Postgres over the wire with psycopg2 (the
   image has no `psql`), resets the app role's password and provisions the
   `bridgeadmin` rescue superuser so future drift self-heals at boot. Both paths
   verify the resulting credentials before reporting success.
5. **Funnel scope documented and narrowed.** The old deployment recipes forwarded
   the whole `/bankbridge/plaid/*` and `/bankbridge/api/plaid/*` prefixes, which publishes four
   unauthenticated write endpoints to the Internet. All three options now
   forward only the exact OAuth callback. See [Restricting Tailscale Funnel to
   the OAuth callback
   only](#restricting-tailscale-funnel-to-the-oauth-callback-only).

**v0.4.6** — **guided rule authoring**. The first sync used to end in
guess-and-check: hundreds of raw transactions, no Journal Entries, and no way to
tell which merchants still needed a rule short of reading the list. The
Transactions tab now has a **Rule state** filter (Unmatched / Rule matched / JE
error / JE cancelled), the unmatched view **groups by merchant** so one rule
clears a whole group, and both a group and a single row can open the Rules editor
**pre-filled**. The Rules list gains a **Matches** column so a dead rule is
obvious, and a scope mismatch between the Offset Account's Company and the
rule's is now caught **when you save the rule** instead of silently blocking
every Journal Entry it later generates. See
[Rule authoring](#rule-authoring-v046). Additive: the only schema change is a
`match_count` column that backfills to 0, and existing rules and transactions
behave exactly as before.

v0.4.6 also **fixes the v0.4.5 Counterparty overlay never provisioning itself on
an upgrading install**. The doctype bootstrap was only ever reachable through the
ERPNext *account import* path, so an install whose accounts were already imported
under an earlier version never ran it: no `Counterparty` doctype was created, no
CREATE was ever attempted, and the only symptom was 404s from the read paths.
`ensure_counterparty_doctype` was written to be called at startup — nothing
called it. It now runs once per container ~15s after boot, on the elected
scheduler worker (not in `create_app`, so boot never waits on ERPNext and N
gunicorn workers don't each probe). The outcome is logged explicitly as
`created`, `already_present`, or a named failure with the reason ERPNext gave —
v0.4.5 returned a bare boolean, which is why a broken install and a healthy one
produced identical logs. `scripts/provision_counterparty_doctype.py` forces it
without a restart.

**v0.4.5** — **one identity per counterparty**. ERPNext models the buy side and
the sell side as unrelated doctypes. A party that trades with you in both
directions — a bank, a wholesaler who buys your fruit and sells you bins, a
neighbouring grower you swap labour with — becomes two records that share
nothing but a spelling:

```
Customer  "Wells Fargo"   pays you interest      →  AR ledger
Supplier  "Wells Fargo"   charges you fees       →  AP ledger
                                                    (no link between them)
```

Nothing in ERPNext can answer *what is my net position with Wells Fargo?*, and
the list you reach for at tax time — "every Supplier" — cheerfully includes your
bank, your card issuer and the IRS. Issuing a 1099-NEC to any of those is the
classic January own-goal.

v0.4.5 adds a `Counterparty` doctype that sits **above** Customer and Supplier
and links 0-or-1 of each. It owns no accounting: every debit and credit still
posts exactly where it did before, so the audit trail, ERPNext's own AR/AP
reports and every existing workflow are untouched. The overlay adds identity —
and the four things identity makes possible:

- `/admin/counterparties` — every party with its role badges, and a **combined
  AR + AP ledger** per party in one chronological view, with drill-through to
  the underlying ERPNext vouchers.
- **Aged balances** — 30/60/90/120+ buckets, netted across both roles, so a bank
  you owe fees to and that owes you interest reads as one honest number.
- **1099-eligible counterparties** — Suppliers whose type is neither Financial
  Institution nor Government. It shows what it excluded and why, because a
  report that silently drops rows is one you can't trust.
- **Top counterparties by activity** — ranked by money *moved*, not balance
  outstanding.

The doctype is created over the REST API at startup, the same idempotent
bootstrap that already provisions Bank Account Types and custom fields — no
second Frappe app to deploy. On upgrade, the Customer and Supplier records you
already have are paired automatically (exact name match only; fuzzy-merging tax
identities is a mistake that surfaces in January). If the API user can't create
a DocType, the overlay quietly stays off and nothing else changes.

**v0.4.4** — **books the balance an account already had when you linked it**.
Bank Bridge recorded transactions and nothing else, so the ERPNext balance sheet
answered a question nobody asked: *how much has moved through this account since
Bank Bridge started watching it?* On an account whose recent activity is
net-negative, that reads as a negative asset:

```
Wells Fargo Money Market   ERPNext said   -17,550.00     ← alarming, and wrong
                           actually held       50.00
```

Nothing was miscounted. The $17,600 the account already held on the day it was
linked had simply never been recorded, because it predates every transaction
Plaid hands back. Every real bank account has such a balance, so this hit
everyone linking a real bank — it was just loudest where recent outflows were
large.

Importing an account now **books that opening balance too**, as a Draft Journal
Entry against an **Opening Balance Equity** account (auto-created under the
owning Company's Equity root if the chart doesn't ship one):

| Account type | Plaid `current` | Debit | Credit |
|---|---|---|---|
| Checking / savings / money market / investment | `+17,600` (money you **have**) | the bank GL account | Opening Balance Equity |
| Checking, overdrawn | `-120` | Opening Balance Equity | the bank GL account |
| Credit card / line of credit | `+2,400` (money you **owe**) | Opening Balance Equity | the card's liability account |
| Credit card, overpaid | `-75` | the card's liability account | Opening Balance Equity |

The direction comes from two facts read together, not from a table of special
cases: **which side of the chart the account's GL leaf sits on** (assets open by
debit, liabilities by credit), and **Plaid's per-type sign convention** — a
positive `current` means money you *have* on a depository account but money you
*owe* on a credit one. So the rule is "book the account's natural opening side,
flipped when the balance is negative", and an overdrawn checking account and an
overpaid credit card fall out of it as the same situation seen from either side.

The entry lands in **`pending_review`** like any rules-engine JE, so it flows
through the existing approve/reject workflow — nothing posts to the ledger until
you approve it. No Party is ever set: an opening balance is an equity event, not
a purchase from anyone. Re-importing never books a second one (the entry claims
a `UNIQUE` key derived from the Plaid account id).

**Accounts linked before v0.4.4** are fixed by a one-shot backfill that works
backwards from what Bank Bridge has mirrored — `opening = current balance −
everything seen since` — with the same per-type sign handling (a purchase lowers
a checking balance but *raises* a card's):

```bash
docker exec <bankbridge_container> python -m scripts.backfill_opening_balances --dry-run
docker exec <bankbridge_container> python -m scripts.backfill_opening_balances
```

Its numbers are **estimates**, bounded by how far back Plaid's history reached
when the account was linked (typically 30–90 days) — which is exactly why they
land in `pending_review`. Check each against a statement; where one is off,
reject it and re-book with the true figure from the **Opening Balance** column
on `/admin/accounts`, which also takes a custom amount and a backdated posting
date. The script is idempotent and will not overturn a rejection.

Set `AUTO_BOOK_OPENING_BALANCE=false` to book them all by hand instead, or
`OPENING_BALANCE_DATE=2026-01-01` to backdate to a fiscal year start.

Since v0.4.9 there is a way to skip that manual check entirely:
`scripts/backfill_statements.py --rebook` replaces the estimate with the opening
balance printed on a statement the bank issued — for any statement that
reconciles against the mirrored transactions. See
[Bank statements](#bank-statements-v049).

**v0.4.1** — **detects transfers between the Companies you own**. Moving money
from the Farm's checking account to your Personal one is not revenue and it is
not an expense — it is the same money in a different pocket. But Plaid delivers
it as **two** transactions, one on each linked account, and until now nothing
connected them:

```
Farm      -$10,000  "Transfer to Personal"   → an expense on the Farm's P&L
Personal  +$10,000  "Transfer from Farm"     → income on Personal's P&L
```

Both sets of books then showed activity that never happened, and the error
compounded every time money moved.

Bank Bridge now **matches the two legs and books the movement properly**. A
detection pass pairs transactions on **equal magnitude and opposite sign**
(a hard requirement), **dates within ±3 days**
(`INTERCOMPANY_DATE_TOLERANCE_DAYS`), **description similarity ≥ 0.6**
(stdlib `difflib` — no new dependency), and — the part that makes it
*intercompany* — **linked accounts belonging to different ERPNext Companies**.
Each candidate gets a confidence score, and at **≥ 0.75** the pair is recorded
automatically. Instead of two P&L entries, one **balanced pair** is written:

| Company | Debit | Credit |
|---------|-------|--------|
| Farm (money out) | `Due from Personal LLC` | `Farm Checking` |
| Personal (money in) | `Personal Checking` | `Due to Farm LLC` |

Profit & loss is untouched. The transfer lands on **both balance sheets** as a
mutual receivable/payable that nets to zero when the entities are consolidated.
The two counterparty control accounts are **auto-created on first use** — the
receivable under *Loans and Advances (Assets)*, the payable under *Current
Liabilities*, numbered to match your chart's scheme — and deliberately carry no
`Receivable`/`Payable` `account_type`, since ERPNext would then demand a Party
and the counterparty here is another Company of yours, not a customer.

Three things make this safe to leave on:

- **Rules can't double-book it.** Every rule gains an **Ignore for
  intercompany-paired transactions** checkbox, **on by default** and backfilled
  **on** for existing rules — so a generic "Transfer" rule doesn't also book one
  leg to P&L. Clear it per rule for the rare case you want one to fire anyway.
- **The two entries are atomic.** Two Journal Entries in two Companies can't
  share an ERPNext transaction, so if the second create fails the first Draft is
  **deleted** before the failure is recorded. Approve is the same: a failed
  second submit **cancels** the first. Your books are never half-updated.
- **Every pairing is reversible.** `/admin/intercompany` lists each detected
  transfer with both sides, the confidence score and both JE docnames, filtered
  by Company, state or confidence, with bulk Approve/Unpair. **Unpair** cancels
  whatever was booked and returns both transactions to normal categorization —
  and the rejected pair is *kept* as the record that stops the detector
  re-pairing them on the very next sync.

Because the two legs usually arrive on **different syncs** (each Plaid Item
advances its own cursor), a pair can form after one leg already got a
rules-engine entry. A **Draft** is abandoned and replaced; a **submitted** entry
is left alone and the pair reports why, because silently cancelling posted
activity would be worse than the double-count.

Detection needs linked accounts under **more than one Company**, so a
single-Company install is completely unaffected — every pass finds nothing and
the Intercompany page says so.

**v0.4.0.9** — **party type now respects `account_type`, not just `root_type`**.
v0.4.0.8 derived the party side from the offset account's **root type** — Income
→ Customer, Expense → Supplier. ERPNext, however, validates a Journal Entry
line's Party against the finer **`account_type`**, and it does so at **submit**,
not at create:

```
ValidationError: Party Type and Party can only be set for Receivable / Payable
account Interest Income - BBT
```

An ordinary Income account has `root_type=Income` but `account_type=Income
Account` — not `Receivable` — so an interest-income rule generated Journal
Entries that **created fine and then could not be approved**. The entries piled
up as un-submittable drafts with no way forward.

`Auto` now reads **both** types and books a party only where ERPNext will accept
one: a **Receivable** offset books a **Customer**, a **Payable** offset books a
**Supplier**, and **everything else — including ordinary Income and Expense
accounts — books no party**. Both fields come from the same account fetch, so
the extra precision costs no additional ERPNext round-trips.

Two more layers back that up. The **Rules editor refuses to save** a literal
`Supplier`/`Customer` whose offset account can't carry it, naming the account,
what it actually is, and the two ways out; a Company-agnostic rule whose logical
offset resolves incompatibly under *some* Company warns and saves on
confirmation, since the operator may know it never fires there. And a **boot
migration repairs the rules already in the database**, clearing `party_type` on
every rule whose offset ERPNext would reject it on and logging each flip. Both
checks act only on a **positive** mismatch — an unreachable ERPNext or an
unresolvable account yields no verdict and changes nothing, because silently
stripping a party on a network blip is worse than the bug being fixed. Existing
valid rules are untouched, and `Auto` / `— none —` are always allowed.

**v0.4.0.8** — **adds the sell side**. Everything through v0.4.0.7 was Accounts
Payable: the only party Bank Bridge would ever auto-create was a **Supplier**. A
farm also takes money **in** — fruit-buyer deposits, USDA/FSA payments, grants,
lease revenue, direct-to-consumer sales — and every one of those booked its
counterparty as a Supplier. That put AR activity on the AP ledger, filled the
**1099-NEC vendor list with people who are actually customers**, and
miscategorized the party for every downstream report.

Rules now carry a **Party type** of `Auto` (the default for new rules),
`Supplier`, `Customer`, or `— none —`. **Auto reads the offset account each time
the rule fires**: an **Income** account books a **Customer**, an **Expense**
account books a **Supplier**, and anything else — Asset, Liability, Equity, i.e.
a transfer between your own accounts — books **no party at all**. Deriving the
side from the account rather than the Plaid amount sign is deliberate: the sign
convention is ambiguous across refunds, reversals and the
`always_debit`/`always_credit` overrides, whereas the offset account is your own
explicit statement of what the transaction *is*. Customers are auto-created
through the same find-or-create path Suppliers use — idempotent at three levels,
name used verbatim, and a party that genuinely can't be created **drops the party
rather than failing the JE**.

Some counterparties trade with you **both ways**: Wells Fargo pays you interest
and charges you fees; a packing house buys your fruit and sells you supplies. For
a recognised **bank, credit union or brokerage** — by name keyword, by a known
institution name, or because the party came from a linked Plaid Item's
institution — Bank Bridge creates **both a Customer and a Supplier** at first
encounter, each keeping its own AR/AP ledger, so the second transaction can't
fail on a party that doesn't exist yet. Ordinary vendors (Uber, Starbucks,
Tractor Supply) stay **single-role** until a reverse-direction transaction
actually shows up. `BANKBRIDGE_DUAL_ROLE_PARTIES` and
`BANKBRIDGE_SINGLE_ROLE_PARTIES` override the heuristic in either direction.

**Backward compatible.** `party_type` has existed since v0.3.0 and NULL has
always meant *no party* — that is unchanged, so an existing rule that never named
a party still doesn't, and existing `Supplier` rules behave exactly as before.
Only **new** rules default to `Auto`. `Skip Party field` (v0.4.0.7) still
outranks everything. For books already posted the wrong way,
`scripts/backfill_customer_records.py` reports every JE that booked a Supplier
against an Income account, creates the matching Customers, and repoints the
**draft** entries; a **submitted** JE is immutable in Frappe, so it is reported
and left for you to cancel and re-book rather than silently amended. `--dry-run`
reports without changing anything, and the script is idempotent.

**v0.4.0.7** — **fixes Journal Entries failing for transactions Plaid gives no
merchant name**. Interest payments, credit-card payments and payroll ACHs all
came back `417 LinkValidationError: Could not find Row #1: Party: Wells Fargo`
while merchant transactions (Uber, Starbucks) posted fine. The cause: the
auto-Supplier hung off Plaid's `merchant_name` field, so a merchant transaction
minted its Supplier and its JE posted — but a **description-only** transaction
whose rule named a Party put that party on the document with no Supplier behind
it, and ERPNext refused the whole JE. The ensure now hangs off the **party**, not
the merchant field: whatever the source — the rule's own Party name, the Plaid
merchant, a **payroll processor read out of the description** (`ACH Electronic
CreditGUSTO PAY 123456` → `Gusto`, plus ADP / Paychex / Rippling and a dozen
more), or a fallback to the **account's own institution** (`INTRST PYMNT` →
`Wells Fargo`) — its Supplier is created before the JE is built. Derived parties
get a sensible **Supplier Group**, auto-provisioned if missing: banks under
`Financial Institutions`, payroll processors under `Payroll Providers`. A name
the operator typed is used **verbatim** (a literal `GUSTO` is not normalized into
a duplicate `Gusto`), the ensure is idempotent at three levels, and a Supplier
that genuinely can't be created **drops the party rather than failing the JE** —
a JE with no party beats no JE.

Rules also gain **Skip Party field**, for a rule that books a transfer between
two accounts you own (a card payment, a deposit, an inter-account move). Such a
transfer has no counterparty, so naming one just mints a junk Supplier; checked,
the generated JE carries no Party at all (ERPNext treats it as optional). The
Rules editor **pre-checks it automatically** when the chosen Offset Account
resolves to another Bank Account of the same Company — answered from local data,
so it works with ERPNext unreachable — and the operator can always override; only
what they save is stored. Existing rules default to unchecked and keep naming
their party exactly as before.

To clear entries already stuck in `error`, either click **Rerun rules** on
/admin/transactions (they're eligible — a failed row has no Journal Entry yet) or
run `scripts/backfill_missing_suppliers.py`, which parses the party out of each
failure, creates the missing Suppliers and re-generates the JEs back to
`pending_review`. Both are idempotent. No migration beyond the additive
`skip_party` column, which backfills to false.

## How it works

1. **Link a bank once** through Plaid Link (`/admin/link_bank`). OAuth-only
   banks bounce through `/bankbridge/plaid/oauth_return` and back automatically.
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
6. **Pair intercompany transfers** (v0.4.1) — before the rules run, the bridge
   checks whether a transaction is one leg of a transfer between two Companies
   you own (matching amount, sign, date and description across Companies). If so
   it is held back from the normal rules and booked as a **Due from / Due to**
   pair across both entities instead, so neither Company's P&L moves. Review at
   `/admin/intercompany`. Inert unless accounts under two Companies are linked.
7. **Reconcile** in ERPNext's built-in **Bank Reconciliation Tool** (or review
   the generated Journal Entries at `/admin/generated_entries`).

Idempotency is enforced two ways: a unique local row per Plaid transaction id,
and an ERPNext find-or-create keyed on `reference_number` (the Plaid id). The
deposit/withdrawal split follows Plaid's convention (positive amount = money
out of the account → withdrawal; negative = money in → deposit).

## Rule authoring (v0.4.6)

Writing the first set of categorization rules is the one genuinely hard part of
setting Bank Bridge up, and until v0.4.6 the UI gave no help with it. You synced,
got several hundred Bank Transactions and no Journal Entries, and had to work out
from the raw list which merchants repeated often enough to be worth a rule —
then write each one from memory, hit **Rerun rules**, and scroll back through the
list to see what stuck.

v0.4.6 turns that into a loop you can actually follow.

**1 · Find what no rule caught.** `/admin/transactions` gains a **Rule state**
filter, separate from the existing **Status** filter (that one is about the
Plaid → ERPNext push; this one is about what the rules engine did afterwards):

| Filter | Means |
| --- | --- |
| **Unmatched** | No rule matched, so no Journal Entry was generated. The list to write rules from. |
| **Rule matched** | A rule fired and a live Journal Entry exists (`pending_review` or `approved`). |
| **JE error** | A rule matched but the JE couldn't be created — `error`, `blocked` (cross-Company), or `skipped_missing_account`. |
| **JE cancelled** | A rule matched and its JE was rejected or reversed. |

All four consider only transactions the engine has actually seen — posted to
ERPNext and not removed — which is the same set **Rerun rules** operates on. A
row still pending has never been offered to the engine, so calling it
"unmatched" would send you off writing rules that can't fire yet.

**2 · Group the work.** Under **Unmatched**, transactions are grouped by
merchant:

```
▸ 12 unmatched from Uber Eats   · 284.40 total     [Create rule from this group]
▸  4 unmatched from Chevron     · 246.80 total     [Create rule from this group]
▸  2 unmatched from SQ BLUE BOTTLE  (by description)  · 14.00 total
```

Transactions Plaid gives no merchant name for are grouped by a **description
signature** — the first few alphabetic words, with card and store numbers
stripped — so `SQ *BLUE BOTTLE 4471 SEATTLE WA` and `SQ *BLUE BOTTLE 8890
PORTLAND OR` land together. Anything seen only once falls to a **One-offs**
section rather than being buried in a group of one.

**3 · Open the editor pre-filled.** "Create rule from this group" opens
`/admin/rules` with a **`merchant_contains`** rule already filled in — broad
enough to clear the whole group you just expanded. The **+ Rule** button on an
individual unmatched row uses **`merchant_exact`** instead (or a description
regex where there's no merchant): the narrowest rule that certainly covers the
row in front of you. Both set **Applies to Company** from the transaction's own
bank account, leave **Party type** on `Auto` (v0.4.0.9) and leave the
**Description Template** empty so the v0.4.0.4 auto-fill takes over once you pick
an Offset Account. Nothing is saved — it's a starting point you still edit.

**4 · See which rules are actually working.** The Rules list gains a sortable
**Matches** column: how many transactions each rule has fired on. A `0` is the
signal — the rule is dead, or scoped too narrowly to reach anything (usually a
Company scope no bank account belongs to, or a higher-priority rule shadowing
it). The count is served from a cached column refreshed by a daily rollup, not
recomputed per page load; it also refreshes inline right after **Rerun rules**,
and **↻ refresh match counts** on the Rules page forces it. Because an edit
clones the rule by design (v0.3.0 non-destructive history), the rollup credits an
archived version's matches to the live rule that superseded it — otherwise every
edit would reset a working rule to 0 and make it look dead.

**5 · Catch a scope mistake while authoring it.** Saving a rule whose Offset
Account belongs to a different ERPNext Company than the rule is scoped to now
raises a confirmation *before* the save persists:

> ⚠ **This rule's Offset Account belongs to another Company**
> "Fuel Expense - BL" belongs to Beta LLC, but this rule is scoped to Alpha LLC.
> [Cancel] [Save anyway]

This is v0.4.0.2's push-time cross-Company guard moved forward to authoring
time. Left alone, such a rule saves happily, generates Journal Entries, and every
one is blocked days later in a different part of the UI. It warns rather than
blocks — you may be mid-reorganization — and the Offset Account field also names
its resolved Company inline, live-updating as you change the scope:

```
Offset account (in Alpha LLC):                      ← scoped rule, Mode A
Offset account (logical name — resolves per-Company at JE time):   ← Mode B
```

Best-effort throughout: an unconfigured or unreachable ERPNext, or an account
whose Company can't be read, saves exactly as it did before. A Company-agnostic
rule never warns — its offset is a logical name that resolves per Company at JE
time (v0.4.0.3), so there's no single Company for it to disagree with.

## Bank statements (v0.4.9)

Bank Bridge pulls the statements your bank issued from Plaid's `/statements`
API, stores the PDFs, and reconciles each period against what it mirrored.

### Reconciliation — "do my books match my bank?"

`/admin/statements` groups statements by account and, for each month, computes:

```
statement opening balance
  +  the movement Bank Bridge mirrored inside that period
  =  what our books say the account closed at
  vs the bank's own closing balance
```

| Period | Opening | Movement | Expected | Bank closing | Delta | Status |
|--------|--------:|---------:|---------:|-------------:|------:|--------|
| 2026-07 | 17,600.00 | -50.00 | 17,650.00 | 17,650.00 | +0.00 | reconciled |
| 2026-08 | 4,100.00 | -210.00 | 4,310.00 | 4,660.00 | -350.00 | ⚠ off by 350.00 |

A delta means the mirror is missing transactions for that month — a named month
and a dollar figure, rather than a vague sense that something is off. The
direction of "movement" follows the account type: for a checking account a
purchase lowers the balance, for a credit card it raises what you owe.

A statement whose balances couldn't be read shows **no balances** rather than a
discrepancy. An unreadable PDF says nothing about whether the books agree, and
flagging it as a mismatch would train you to ignore the icon that matters.

### Opening balances from a statement

For accounts linked before v0.4.4, `scripts/backfill_opening_balances.py`
estimates the opening balance arithmetically. To replace those estimates with the
bank's own figure:

```bash
docker exec <bankbridge_container> python -m scripts.backfill_statements --dry-run
docker exec <bankbridge_container> python -m scripts.backfill_statements --rebook
```

`--rebook` is opt-in because it changes the books: it replaces an entry you may
already have reviewed and moves the posting date to the statement's period start.
An **approved** opening balance is never touched.

**A statement is only used when it reconciles.** Booking an opening balance dated
a statement's period start asserts that the ledger from that date forward is
exactly the mirrored transactions — so Bank Bridge checks that rather than
assuming it. A statement is refused when the mirror can't reproduce its closing
balance, or when transactions predating it already exist (they'd be counted
twice). A refused statement leaves the v0.4.4 estimate in place.

### What Plaid actually provides

`/statements/list` returns a statement id, month, year and posting date — **no
balances**. Those exist only inside the PDF, so Bank Bridge extracts the text and
looks for the labels US institutions print:

| | Labels recognized |
|---|---|
| Opening | beginning balance, opening balance, previous balance, balance forward, previous statement balance, starting balance |
| Closing | ending balance, closing balance, new balance, ending daily balance, statement balance |

Negative amounts are read in both printed forms (`-1,234.56` and `(1,234.56)`).
An unrecognized layout, or a scanned statement with no text layer, yields **no
balance** — never a guess. The PDF is still stored and viewable.

### Reading real statements (v0.4.41)

Two things had to change once production Wells Fargo PDFs started arriving.

**Line structure decides which amount a label owns.** Up to v0.4.40 the parser
flattened the whole PDF into one string and took the first amount within 120
characters of a label. On a one-column consumer statement that works. On a
Wells Fargo Advisors brokerage statement it does not: the *Cash sweep activity*
table extracts as

```
TRANSFER TO BANK DEPOSIT SWEEP ENDING BALANCE06/16 100,000.00 06/30 39,751.95
```

and `ending balance` claimed `100,000.00` — a single transfer — as the month's
closing balance. All 26 real statements on the test install parsed a wrong
closing that way. Matching is now **per line**, and the run between a label and
its amount must contain **no digits** (after an `on <date>` clause is stripped,
so `Beginning balance on July 1  $17,600.00` still reads). A label followed by
`06/16` is a table row and is refused.

**Labels only mean something inside their section.** `Interest`, `Total` and
`Other additions` each appear many times in a 35-page brokerage statement, and
the one that matters sits under a particular heading. Every field is looked up
inside a named section, which is also what stops the twelve-page holdings
table — whose ~90 rows each begin `Total …` — from answering a question about
realized gains. A heading is distinguished from prose that merely mentions it
(page 2 opens a disclosure paragraph with the words "Income summary:") by
carrying its column headers or being short.

**Brokerage statements state two different balances.** The *Progress summary*
gives total account value — cash plus securities at market. The *Cash flow
summary* gives the cash side alone. The **cash** figure is what reconciliation
and opening-balance anchoring use, because it is the only one the mirror can
reproduce — Bank Bridge sums cash events and has no record of market
appreciation. Total account value is shown beside it, never reconciled against.

### Everything a statement asserts (v0.4.41)

The parser no longer stops at two balances. Everything it recovers lands in
`PlaidStatement.parsed_metadata` (JSONB), with the handful that get queried
promoted to real columns beside it and written from the same blob, so the two
can never disagree.

| Layout | Fields recovered |
|---|---|
| **WF Advisors** (brokerage) — 31 fields, **verified** against 26 production statements | cash opening/closing, total account value opening/closing, cash deposited/withdrawn, securities purchased/sold/deposited/withdrawn, income and distributions, other additions/subtractions, net additions/subtractions to cash, advisory + platform fees, interest, sweep income, ordinary + qualified dividends, taxable/tax-exempt/total income, unrealized + realized gains (period and YTD, short and long term), checks written, ATM and card activity, available funds |
| **WF deposit** (checking/savings/MMA) — *unverified* | opening/closing balance, deposits and credits, withdrawals and debits, checks paid, interest earned, service fees, average ledger and collected balance |
| **WF credit card** — *unverified* | previous and new balance, payments and credits, purchases, cash advances, fees charged, interest charged, minimum payment due, payment due date, credit limit, available credit |

The gain/loss summary is stored as **three dicts, not nine keys** —
`short_term_gainloss`, `long_term_gainloss`, `total_gainloss`, each
`{unrealized, realized_period, realized_ytd}` — because the statement prints a
3×3 table and one holding period's three columns are one fact.

Every blob also carries its own provenance: `parser_version`, `layout`,
`verified`, `fields_failed`, a `pages` inventory of what is in the document but
*not* mined (a production brokerage statement is 9+ pages; this reads the
summary), and the period the *document* states for itself — kept beside the
calendar month derived from Plaid, because a bank whose cycle isn't a calendar
month makes every reconciliation in that window suspect and this is the only
record of it.

### A parser bump heals itself (v0.4.42)

v0.4.41 shipped a parser that corrected a wrong closing balance on all 26
production statements — and then **nothing changed**. `_store_one` skips any
statement whose PDF it already holds, so the corrected recognizer never
re-read a byte; every row kept the figure the old one produced until an
operator pressed *Re-parse stored PDFs*, a button nobody knew to press. The
install ran new code over old numbers for as long as it took someone to notice
a balance that looked wrong. That is the exact shape of bug a reconciliation
tool exists to prevent, so it is now prevented structurally:

- every row records the `parser_version` that produced its figures;
- the scheduled statement job re-reads any row whose version isn't the running
  one, on the same cadence as the pull;
- `/admin/statements` shows a banner while any stale row remains.

The pass is idempotent and free on a settled install — every row already
carries the current version, so it costs one query and opens no PDF. The manual
button stays, for when you don't want to wait for the schedule.

**"Unverified" is a real caveat.** The deposit and card field tables were
written from the standard Wells Fargo layouts but have not been checked against
a real document, because the live install holds none — its deposit and card
"statements" are Plaid sandbox mocks (*First Platypus Bank*, `Balance on
XX/XX:`) with no labelled totals at all. Figures from them are safe in the
sense everything here is safe — an unrecognized layout yields nothing, never a
guess — but the UI marks them **unverified layout** until a real statement has
been eyeballed against one.

**Extraction is defensive per field.** Each field runs inside its own
try/except; a pattern that raises costs that one figure, is named in
`fields_failed`, and is logged with the statement it failed on. A bad regex can
never take down the metadata for a statement whose balances parsed perfectly.

## Account pairing (v0.4.44)

Wells Fargo Advisors splits **one economic account across two Plaid accounts**,
and the split is invisible until you try to reconcile:

| | statements | BankTransactions | SecurityTransactions |
|---|---|---|---|
| brokerage ••6030 | 13 | **0** | 377 |
| its Brokerage Cash Services companion | **0** | 299 | 0 |

Anchoring the brokerage account alone measures the bank's closing balance
against a transaction feed that is *structurally empty*, so every period comes
back "unexplained" for a reason that has nothing to do with the books being
wrong. `PlaidAccount.paired_account_id` fixes that: when set,
`anchor_transaction_sum` counts the companion's BankTransactions too (not its
SecurityTransactions — those are already summed for the brokerage side, and
double-counting replaces one wrong answer with another).

**Re-linked accounts are one account (v0.4.45).** When a bank is re-linked
Plaid mints a new `account_id` for the same physical account; Bank Bridge
records that with `superseded_by_account_id`, but the transactions do **not**
move — everything before the re-link stays on the old row. On the live install
••3158 has two rows, one holding 2025-06 → 2026-03 and the other 2026-04
onward, and which one hygiene marked "active" was arbitrary. So a sum filtering
on a single `account_id` saw about half the history and reported the rest as
missing money: pairing ••6030 to *either* ••3158 row produced a partner sum of
$0 for ten of thirteen periods.

`supersede_chain()` walks the relationship **both directions and transitively**
(bounded at 10 links, so bad data can't loop), and both sides of the sum use it
— the brokerage side can be re-linked just as easily as the cash side. That
makes the active/superseded designation **cosmetic for reconciliation**: pair
to either row and the same transactions are found, so hygiene can be re-run
later, or not, without changing a single anchor.

**A paired brokerage's securities are not counted at all (v0.4.47–48).** The
companion is a *Brokerage Cash Services* account — a cash-sweep ledger where
every brokerage event (buy, sell, advisory fee, dividend) is recorded as an
*Increase/Decrease from Brokerage activity* bank line. So the companion's
`BankTransaction`s already **are** the account's complete cash story, and the
brokerage's own `SecurityTransaction`s would double-count it.

v0.4.47 tried to keep income and drop only trades, on the theory the companion
didn't carry income. Verifying against **26 months of production data**
disproved it: keeping income left ••9401 at −$30,225.86 total variance, a
proposed sign-negation only flipped that to +$30,225.86, and **excluding all
security transactions drove it to exactly $0.00** — the fees and dividends
sweep to the companion too. On ••6030 all three policies were identical
(its security contribution was already ~0), leaving a genuine **$152.74**
residual to tag rather than a double-count to remove.

An **unpaired** brokerage is untouched: with no companion holding the sweep,
its `SecurityTransaction`s are the only cash record there is.

**…and the re-link overlap is counted once (v0.4.46).** Walking the chain fixed the
split history and exposed the opposite problem in the months either side of the
re-link: Plaid ingested the same purchases into *both* account_ids with
**different `plaid_transaction_id`s**, so id-based dedupe cannot see them. On
••3158's May 2026 window that is 28 rows for 14 real purchases — the anchor
summed **−$33,775.58** where the bank saw **−$16,887.79**.

Rows are therefore deduplicated on a `(date, amount, normalised name)`
fingerprint, and **only across account_ids, never within one**: two identical
charges on the same day on the *same* account are two real purchases. For each
fingerprint the rows of whichever single account holds the most copies are
kept, so a genuine intra-account repeat survives alongside its cross-account
mirror.

**Detection, strongest evidence first.** The statement prints
`Brokerage Cash Services number: 1234567890`, whose last four digits are the
companion's Plaid mask — the bank's own assertion of the relationship, captured
into `PlaidStatement.cash_services_account_number`. Failing that, a
`… BROKERAGE …` account beside exactly one `… BROKERAGE CASH …` under the same
Company is the same relationship spelled differently. A pairing is made only
when **exactly one** candidate matches — pairing the wrong cash account would
silently fold another account's transactions into this reconciliation, which is
worse than a visible gap.

**Candidates are scoped to the Plaid Item** (v0.4.45), not merely the Company:
two brokerage accounts under one entity have two different companions, and
offering all four made pairing to the wrong one easy. A companion always
arrives on the same Link connection as the account it serves, so the Item is
the right scope; Company is kept as a second constraint, type must be
compatible (only a depository can be a brokerage's cash side), and superseded
rows are excluded — pairing to one still *works* via the chain walk, but the UI
shouldn't invite it. The dropdown and the auto-linker share one function, since
disagreeing about what a valid pairing is would itself be a bug. Runs after each re-parse
(before the anchor rebuild — the key is recovered by the re-parse and the sums
depend on the pairing). **A manual pairing on `/admin/accounts` always wins.**

## Sandbox account hiding (v0.4.44)

An install set up against Plaid's Sandbox keeps those test accounts forever —
a dozen `Plaid Checking ••0000` rows beside the real ones in every list and
count. They are **hidden, never deleted**: they are the only accounts with a
transaction history varied enough to exercise the parser and the reconciliation
engine against.

The filter is by **ERPNext Company** (`Bank Bridge Test` by default,
configurable) because a sandbox account is otherwise indistinguishable from a
production one — that is the point of a sandbox — while the Company assignment
is a human statement of intent. Hidden by default, which is the one behaviour
change on upgrade: the failure mode of hiding is "where did my account go" (one
toggle, on the page whose contents it changes), while the failure mode of
showing is a sandbox row silently inside a reconciliation total. When shown,
each carries a **SANDBOX** tag so it can never be mistaken for real money.

Applied consistently to `/admin/accounts`, `/admin/statements`,
`/admin/transactions`, `/admin/reconciliation`, `/admin/holdings`,
`/admin/investment_transactions`, the dashboard account count, and the Plaid
cost estimate.

## Statement-derived transaction dates (v0.5.3)

The last of the ••6030 residual was **date-boundary attribution**: Plaid and the
bank routinely file the same transaction in different statement windows. A $712
wire the bank dates Dec 31 and Plaid dates Jan 2 lands in opposite reconciliation
periods, showing **+$712 in one month and −$712 in the next**.

The bank's own **Activity Detail** pages carry each transaction's posted date.
This release parses them (beyond the Snapshot totals the v0.4.42 parser reads),
into a new `StatementTransaction` table, then **matches** each line to the Plaid
`BankTransaction` on the paired companion by *(amount — sign-flipped between the
two conventions, ±5 days, description overlap)*. Exactly one candidate →
`matched`; several → closest date, `ambiguous`; none → `no_match` (surfaced: a
line Plaid never returned). The bank's date is stamped onto the transaction as
`statement_posted_date`, and the anchor engine sums by
`COALESCE(statement_posted_date, date)` — so a transaction is counted in the
month the *statement* assigns.

**v0.5.4 wiring fix.** In v0.5.3 the statement-transaction pipeline lived only
in `reparse_stale`'s epilogue, so the `/admin/statements` "Re-parse stored PDFs"
button (which calls `reparse_stored`) populated nothing. Worse, extracting each
PDF *twice* (balance parse + activity parse) at pypdf's ~4s/PDF turned a
340-statement re-parse into a **38-minute** apparent hang. Now `reparse_stored`
extracts each PDF **once** and reuses the pages for both, gates activity parsing
to the ~26 paired-brokerage statements that actually need it, wraps each
statement in its own try/except (one bad PDF can't stall the batch), and runs
the whole pipeline itself — store → pair → match → rebuild. Verified on live
data: 2.3 min, 471 statement-transaction rows, 173 dates stamped, and the
Dec/Jan ±$712 pair collapsed to $0.00.

Runs in the re-parse epilogue, after pairing and before the anchor rebuild.
Gated by `PlaidItem.statement_date_override_enabled` (default TRUE — a matched
bank date is strictly more authoritative than Plaid's; off leaves every date
NULL and reconciliation unchanged). Dedup still fingerprints on `(amount,
description)`, unaffected by the date.

On a synthetic Dec/Jan fixture the ±$712 pair collapses to **$0.00** in both
periods. A residual line the bank shows but Plaid never returned (e.g. a $65
statement fee) is real off-Plaid activity this can't fix — it surfaces as
`no_match` for the operator.

## Investment Advisory Agreement automation (v0.5.2, Phase E)

Codifies the fee, benchmark, performance and compliance mechanics of an
Investment Management Agreement (a Client — an ERPNext Company — and a Manager)
so the quarterly reporting the agreement requires is a **review of what Bank
Bridge computed, not a recomputation**. Every derived figure is stored, not just
displayed. Seven new tables: `AdvisoryAgreement`, `DailyAUM`, `HighWaterMark`,
`HurdleRateSample`, `PerformanceSnapshot`, `AdvisoryFeeAccrual`,
`RiskControlCheck`.

**Four engines, and where each stops:**

- **Daily AUM + base-fee accrual** — `AUM × total_base_fee_rate / 365`, split
  into the bank's cut (recorded, never posted — WF deducts it) and the
  Manager's (accrued to the payable). Always runs; idempotent per day.
- **Quarterly base-fee settlement** — aggregates the Manager slice and posts
  `DR advisory expense, CR fee account`. Gated by `fee_accrual_enabled`.
- **Performance fee** — time-weighted return vs the hurdle (10-year Treasury,
  from FRED or manual entry), against a **high-water mark that only ratchets
  up**. A fee accrues only when *both* the hurdle is cleared *and* closing AUM
  exceeds the prior peak (recapture prevention). Accrued quarterly, **paid
  annually on Client approval** — no quarterly JE. Gated by
  `performance_fee_enabled`.
- **Daily risk controls** — single-position, bitcoin and sizing limits;
  violations recorded always, **alerts** gated by
  `risk_control_alerts_enabled`.

**All three kill switches default FALSE.** The engines run regardless (the
dashboard shows the numbers); the switch gates only whether a Journal Entry or
an alert fires. Nothing hits the Client's P&L without the Manager opting in —
the same boundary as v0.5.0/v0.5.1. Every JE carries `company =
client_company`. `/admin/advisory/<id>` renders the stored figures with the
three toggles inline.

## Investment transactions as Journal Entries (v0.5.1, Phase D)

Every `SecurityTransaction` on a brokerage account can post as a Journal Entry
with security-level detail (*Bought 100 TEST-AAPL at $150.00 = $15,000.00*): a
buy moves cost into `Marketable Securities - <ticker>`, a sell realizes gain or
loss, an advisory fee hits an expense line, a dividend an income line. GL
accounts are created on first use, idempotently.

**The Cash Clearing bridge.** The reconciliation subsystem (v0.4.48) established
that a paired brokerage's trade cash is *already* on the companion depository as
an "Increase/Decrease from Brokerage activity" `BankTransaction` — which the
rules engine also posts. Booking the security JE against the sweep account too
would **double-book every trade**. So a paired brokerage's investment JEs settle
against a per-Company **`1099 - Cash Clearing - Brokerage`** account:

```
Buy $10k:   DR Marketable Securities 10,000   CR Cash Clearing 10,000
companion:  DR Cash Clearing         10,000   CR Bank         10,000   (rules)
            → Marketable +10k, Bank −10k, Clearing nets to ZERO
```

Clearing must always net to zero; a non-zero balance means a
`SecurityTransaction` without its matching companion `BankTransaction`, surfaced
on `/admin/statements`. An **unpaired** brokerage has no companion double-post,
so it settles against its own bank leaf directly.

**Kill switch, default OFF.** Nothing posts until the operator flips
`invest_je_posting_enabled` on the Item (`/admin/accounts`) — these are real P&L
entries, so an upgrade auto-posts nothing. **Idempotent** on
`plaid_investment_transaction_id`: a re-sync of the same trade generates no
second JE. **Cost basis** is Specific Identification via `TradedCycle`, FIFO
against `RetainedLot` otherwise (lots consumed only after the JE posts, so an
ERPNext failure never leaves phantom-sold inventory). Every line carries
`company = owning_company`, so Orchard Meadow's JEs move by export/import with
nothing to unwind. **Unrealized gains are never posted** — Marketable Securities
sits at cost until a sell.

## Reconciliation status in ERPNext (v0.5.0)

A bookkeeper opens the ERPNext **Bank Statement** record and sees *this period
is already reconciled*, with the variance and the reason — without leaving
ERPNext to check Bank Bridge. Three custom fields carry it:

| Field | Type | Value |
|---|---|---|
| `bank_bridge_reconciled` | Check | 1 when the `StatementAnchor`'s `|variance|` ≤ threshold (default $1.00) |
| `bank_bridge_variance` | Currency | the anchor variance, signed |
| `bank_bridge_reason` | Small Text | the period's internal-tag summary (v0.4.49), `untagged` when unreconciled with no tags |

**This is STATUS, not a correction.** It emits **no Journal Entry**, rewrites
**no opening balance**, and posts **no adjustment** — verified by a test that
asserts the JE count never moves when status is pushed. That is the line the
"no ERPNext writes" rule actually drew: a correction would need unwinding when
Orchard Meadow's own ERPNext splits off, but a status field re-populates
cleanly on that instance's Bank Statement records with nothing to undo. The
same values write idempotently on re-push (diffed, so a settled install writes
nothing), and travel with the verdict so a re-anchor or a re-tag reaches ERPNext
on the next refresh.

The fields are provisioned as **Custom Fields** (not baked into the doctype
spec), because the Bank Statement doctype already exists on every v0.4.10
install — a Custom Field is the only idempotent way to reach an
already-created doctype, and it works identically on a fresh one. An ERPNext
that lacks the Custom Field doctype degrades to the exact pre-v0.5.0 payload
rather than failing the sync. `/admin/statements` shows a per-account
reconciled / needs-attention count at a glance.

## Internal attribution tags (v0.4.49)

The reconciliation view's **Reason** column auto-populates from the
categorization rules that already fire on transaction descriptors. A
`CategorizationRule` can carry an optional `bb_internal_tag` (a slug like
`owner_distribution` or `advisory_fee`); when it matches, that tag is stamped on
`BankTransaction.bb_internal_tag`, and the reconciliation view aggregates the
tags carried by each period's transactions:

- one shared tag → that tag (`owner_distribution`)
- several → dominant first, with counts (`owner_distribution (3), advisory_fee (1)`)
- variance but nothing tagged → `untagged`; no transactions → em-dash

One rule thus serves both categorization *and* reconciliation attribution — no
second tagging engine over the same descriptors. The period window matches the
anchor sum exactly (the account's supersede chain plus the paired companion's,
deduped across the chain), so the reason describes the movement the variance is
measured over.

**The tag never leaves Bank Bridge.** It appears in no Journal Entry payload,
remark, or any ERPNext-bound field — verified by test. That boundary is the
point: an attribution like `member_distribution` records *who* a payment went
to, which belongs in the operator's private reconciliation ledger, not in the
accounting system this feature deliberately keeps it out of.

A **Backfill tags** button re-runs every active rule against all stored
transactions and updates only the tag column — never building or altering a
Journal Entry — so a rule added today can label a year of history without
retro-posting entries for it. It is a pure function of the current rules:
idempotent, and it *clears* a tag whose rule lost it.

## Statement-anchored reconciliation (v0.4.43)

Bank Bridge's own durable record of what each account **actually held** at each
statement boundary, sourced from the bank's PDF — at `/admin/reconciliation`.

**Why it's a table and not a journal entry.** The accounts this matters most
for belong to an entity that will get its *own* ERPNext instance later. Pushing
balance corrections into the current farm books today would be work to reverse
tomorrow. So this release makes Bank Bridge authoritative for statement-boundary
balances and **emits nothing to ERPNext** — a property of the release, not an
oversight. When the second instance comes online, the chain replays against it
and matches by construction. For the same reason anchoring is deliberately
**company-agnostic**: every account with statements gets a chain, mapped or not.

Each `StatementAnchor` row is one identity plus the two ways it can fail:

```
anchored_opening + transaction_sum = computed_closing
anchored_closing − computed_closing = variance
```

- **`variance`** — money the *bank* saw and *Plaid* never reported: an
  off-platform wire, a tax payment, a transfer between institutions. A finding,
  not an error. Tag it with `variance_reason` as the real categories emerge.
- **`chain_gap_from_prior`** — this period's opening doesn't meet the previous
  period's closing, so a **statement is missing** between them and every
  variance after it is measured from the wrong baseline. Kept distinct from
  variance because the fix differs: one is "find the transaction", the other is
  "fetch the PDF".

Balances are the **cash-and-sweep** side, never total account value — only cash
can be reconciled against a transaction feed, since the mirror has no record of
market movement. `parser_version` is stamped on every row, and
`rebuild_statement_anchors()` runs automatically after each stale re-parse, so
a chain can never assert figures a later recognizer already corrected.

**The picker lists reconciliations, not Plaid rows (v0.4.47).** A Wells Fargo
Advisors setup is four Plaid accounts but **two** reconciliations: the
brokerage accounts hold the statements and therefore the anchors; their
cash-services companions hold every transaction and no statement to measure it
against. The account picker is keyed on `StatementAnchor`, so the companions
(and any brokerage account not yet anchored) don't appear, and each option is
labelled with its pair — `BUSINESS BROKERAGE ••6030 ⇄ ••3158` — because that is
what the numbers aggregate. A link or bookmark pointing at a companion
redirects to the brokerage side with a note saying why, rather than rendering
an empty table. Both `/admin/reconciliation/<id>` and `?account_id=…` work.

`/admin/accounts` shows each account's variance inline — the one-line answer to
*"is this account's Plaid data telling the whole story?"* — and the chain
downloads as CSV for a CPA or for diffing against the second instance.

### Validation — statement vs Plaid vs the mirror

`/admin/statements/<id>` puts three independent accounts of the same month side
by side:

| | What it is | Why it can be wrong |
|---|---|---|
| **Statement** | what the institution wrote down and mailed | recovered by regex from a PDF, so it can be *misread* |
| **Plaid** | what the API reports for the account now | a snapshot of *today* — compared only on the newest statement |
| **Mirror** | what Bank Bridge computes from stored transactions | complete arithmetic over a feed that may have *gaps* |

Two agreeing is ordinary. Two *disagreeing* is the finding, and each cause has
a different fix. Rows are flagged when they differ by more than
`STATEMENTS_VARIANCE_DOLLARS` (default `$1.00`) **and**
`STATEMENTS_VARIANCE_PCT` (default `0.001`) — both, so neither a rounding cent
on a $1.3M balance nor a fixed percentage of a tiny one raises noise. Signs are
normalised once, to cash-in-positive, so subtracting one column from the other
means something; the stored figures keep the bank's own signs.

**Statements are checked against each other.** One month's closing balance is
the next month's opening, so any statement whose opening disagrees with the
prior month's closing is flagged `parse_suspect` and marked **⚠ chain** in the
UI. It is advisory — a posted opening balance is still gated by the full
reconciliation test — but it catches a misparse without ERPNext, without a
mirrored transaction, and without anyone reading a PDF.

**After upgrading, press *Re-parse stored PDFs*** on `/admin/statements`. A
pull skips statements it already holds, so an improved parser reaches nothing
already on disk until you ask. The re-parse re-reads the bytes, rewrites only
the parsed columns, and leaves the PDFs and every posted journal entry alone;
the corrected balances then flow to the ERPNext `Bank Statement` records on the
next sync.

### Enabling it

The `statements` product must be approved on your Plaid application and requested
when the bank is linked. Bank Bridge requests it automatically and **retries
without it if Plaid refuses**, so linking banks is unaffected on an application
that hasn't been approved. After approval, re-link the bank (or run
`backfill_statements`) to start receiving statements.

PDFs are stored under `{DATA_DIR}/statements/{item_id}/{account_id}/{yyyy-mm}.pdf`
— on the persistent volume, so they survive a redeploy. A scheduled job checks
for new statements every `STATEMENTS_PULL_INTERVAL_DAYS` (default 30); one
already stored is never re-downloaded, and one whose PDF has gone missing is
fetched again.

## Bank Statement doctype (v0.4.10)

Everything above puts statements in *Bank Bridge*. This puts them in **ERPNext**,
where the person who actually needs them works.

At startup Bank Bridge provisions a custom **`Bank Statement`** doctype (module
*Accounts*, so it sits beside Bank Account in the desk) and uploads one record
per statement, **with the PDF attached**:

| Field | Notes |
|---|---|
| `bank_account` | Link → Bank Account. **Required** — see the caveat below |
| `period_start` / `period_end` | the statement period, required |
| `opening_balance` / `closing_balance` | as printed by the bank; **omitted, not zeroed**, when the PDF could not be parsed |
| `statement_pdf` | Attach — the bank's own document, uploaded private |
| `plaid_statement_id` | Plaid's id, **unique** — this is the idempotency guard |
| `reconciliation_status` | `Reconciled` / `Discrepancy` / `Not Checked` |
| `variance_amount` | signed: expected closing minus the bank's closing. Read-only |
| `fetched_at` | when Bank Bridge pulled it. Read-only |

Find them in ERPNext with the Awesome Bar → **"Bank Statement List"**. The PDF is
in the record's sidebar attachments.

**Direction of truth.** Bank Bridge is the source; records flow one way. Nothing
read back from ERPNext changes local state, and the reconciliation verdict is
re-pushed as the mirror improves — a statement measured in March is re-measured
and updated when a later backfill closes a transaction gap. Editing
`reconciliation_status` by hand in ERPNext will be overwritten.

**The one real caveat.** `bank_account` is a required Link, so a statement whose
Plaid account has **not been imported into ERPNext** cannot be created. Those are
reported as skipped-with-a-reason (not failures), and they sync themselves once
you import the account on `/admin/accounts`.

### Reports

| Page | Answers |
|---|---|
| `/admin/statements/reports/discrepancies` | which statements disagree with the mirror by more than `ERPNEXT_STATEMENT_VARIANCE_THRESHOLD` (compares the **absolute** variance — $500 over is as wrong as $500 short) |
| `/admin/statements/reports/coverage` | which months an account has **no statement for** over the last 12 |

Coverage is the one to watch. An unparseable statement is at least visible on
`/admin/statements`; a statement that was never fetched at all is visible
nowhere. Months before an account's first statement are not counted as gaps (an
account linked in May cannot be missing January), and the month in progress is
excluded (its statement hasn't been issued). Both reports fall back to Bank
Bridge's own rows when ERPNext is unreachable, and say so on the page.

### Upgrading, and doing it by hand

Nothing is required: `erpnext_docname` and `erpnext_synced_at` backfill to NULL,
which reads as *"not in ERPNext yet"*, so every statement already held is
uploaded about 20 seconds after the container restarts, and again after each
monthly pull. To do it now, from the **Bank Bridge** container:

```bash
# upload everything; --dry-run to see what would happen first
docker exec <bankbridge_container> python -m scripts.backfill_erpnext_statements --dry-run
docker exec <bankbridge_container> python -m scripts.backfill_erpnext_statements

# just create the doctype, without uploading
docker exec <bankbridge_container> python -m scripts.provision_bank_statement_doctype
```

Both are idempotent and exit non-zero on failure, so they can gate a deploy step.
A statement whose ERPNext record already exists is **adopted** (the local row
learns its docname) rather than duplicated — which is what makes them safe to run
against a data volume restored from a backup taken before the upload.

There is also a **Sync to ERPNext** button on `/admin/statements`.

### If it doesn't provision

The boot log says which of these happened, with ERPNext's own reason attached:

| State | Meaning |
|---|---|
| `created` / `already_present` | working |
| `permission_denied` | the API user lacks DocType create rights — give it **System Manager** |
| `foreign_doctype` | this ERPNext **already has** a `Bank Statement` doctype that Bank Bridge did not create (no `plaid_statement_id` field). Bank Bridge refuses to write to it; rename or remove the existing doctype |
| `doctype_api_unavailable` | a locked-down or very old Frappe with no DocType API |
| `not_configured` | ERPNext isn't set up yet — configure it and restart |

Every failure is fail-open: statements stay local, nothing is lost, and the next
scheduler tick retries.

## Loans (v0.4.14)

A loan is not a Bank Account in ERPNext's sense, which is why loans were refused
outright until v0.4.14. The consequence wasn't that they were handled badly —
they were **absent**, and a farm carrying an orchard mortgage had a balance sheet
overstating its net worth by the whole outstanding principal.

### What gets created

| | Value |
|---|---|
| Bank Account Type | `Loan` |
| Bank Account Subtype | `Mortgage`, `Student`, `Auto`, `Home Equity`, `Construction`, `Consumer`, `Commercial`, `Business`, `Overdraft`, `Line Of Credit` |
| GL parent | Liabilities → (Long-term Liabilities) → Loans |
| Opening balance | credit side — it's what you **owe** |

Loans are **balance-only**: their own transactions are mirrored but never
posted. That's the double-count guard — the money leaving your chequing account
is already mirrored on the chequing side, and that is where the payment is
booked from. Posting the loan's copy of the same event would book every payment
twice.

### Booking a payment: two entries, not one

A $2,000 mortgage payment is really two things — interest (a cost) and principal
(settling debt):

```
payment    Dr Loan Liability      Cr Bank              ← your rule
accrual    Dr Interest Expense    Cr Loan Liability    ← generated automatically
```

Bank Bridge generates the **accrual**. You create the **payment** rule: on the
Rules page, match the payment leaving your chequing account and point
`offset_account` at the loan's GL account. It appears in the dropdown like any
other account once the loan is imported.

Why not one three-line entry? Two reasons. Nothing in this app can emit a
three-line Journal Entry — every builder here produces exactly two, and a rule
has a single offset account. And a mortgage's principal/interest split **changes
every month**, so a ratio stored on a rule would be wrong from month two. The
decomposition sidesteps both: nothing stores a split, and the accrual is
computed fresh from live lender figures each time.

### Interest: exact, or nothing

Interest comes from the lender's own `ytd_interest_paid`, differenced against
what was last booked. No amortization estimate — for the same reason statements
refuse to guess an unparseable balance. An estimate diverges silently on an
extra payment, an escrow adjustment, a fee or a rate change, and nobody
reconciles a figure they weren't told was approximate.

The Accounts page shows a **Loans** panel with each loan's balance, rate, next
payment, year-to-date interest, and one honest label:

- **automatic** — the lender reports year-to-date interest; accruals are
  generated
- **manual** — it doesn't; book interest yourself from the lender's statement
- **not imported** — create it in ERPNext first

Accruals land as `pending_review` Drafts labelled *Loan interest*. The
year-to-date counter resets each January; that rollover is detected, so it never
posts a negative accrual wiping out the year's interest.

On upgrade the first pass **seeds** and posts nothing, so a year of
already-accrued interest can't arrive as one entry.

### The misfiled-mortgage case

An institution can serve a mortgage from its deposit platform, so Plaid reports
`type='depository'`, `subtype='mortgage'`. Trusting the type would book six
figures of debt as a chequing **asset**. Classification keys off the subtype as
well, so it still lands as a liability.

### The `liabilities` product

Requested at link time and **billable on your Plaid plan** — worth checking your
dashboard before enabling. It degrades independently of `statements`: an
application approved for one but not the other still gets the one it holds, and
a loan without liability data still imports and still tracks its balance.

Turn the whole feature off with `LOANS_ENABLED=false`.

### What is not covered

No amortization schedule, no escrow accounting, no payoff projection. Interest
is booked from what the lender reports, not modelled.

## Investment accounts (v0.4.0, mark-to-market v0.4.12)

Plaid `investment` accounts — brokerage, IRA, 401k, HSA, crypto — are supported
**balance-only**: Bank Bridge creates the Bank Account and a GL leaf under
Assets → Non-current Assets → Investments, books an opening balance, and tracks
the value. It does not fetch holdings or investment transactions (that needs
Plaid's separate `investments` product, which this app does not request).

### Typing

| | Value |
|---|---|
| Bank Account Type | `Investment` |
| Bank Account Subtype | `Brokerage`, `Ira`, `Roth`, `401K`, `Retirement`, `Hsa`, `Mutual Fund`, `Stock`, `Bond`, `Crypto Exchange` |
| GL parent | Investments → Retirement / Marketable Securities / Digital Assets |

Before v0.4.12 every investment was typed `Current` with subtype `Other`.

### Mark-to-market

Each sync compares the account's current value against what the GL leaf
reflects and posts the difference:

```
value up      Dr  <investment leaf>                Cr  Unrealized Gain/Loss
value down    Dr  Unrealized Gain/Loss             Cr  <investment leaf>
```

The equity leaf is auto-created once per company under the Equity branch (slot
3100, beside Opening Balance Equity). Entries are `pending_review` Drafts —
nothing posts to your books without approval — and appear on
`/admin/generated_entries` labelled *Investment revaluation*.

**Equity, not income, by default.** An unrealized gain is a paper movement: the
market moved, the farm did nothing, no cash exists. Booking it to income would
let a market rally report a profit the farm never earned. Set
`UNREALIZED_GAIN_ACCOUNT_NAME` to an income account if your accountant prefers
that treatment.

**Deltas compose.** Each entry measures against the ledger, not against the
previous reading, so +5,000 then −2,000 then +1,000 leaves the leaf at
opening +4,000. Movements under `INVESTMENT_REVALUATION_MIN_DELTA` (default $1)
are skipped, and the skipped amount is picked up by the next entry rather than
lost.

**On upgrade, nothing is posted on the first pass.** Every existing investment
account has no recorded baseline, and posting in that state would book the whole
account value as a fictitious gain. The first pass **seeds** the baseline
instead — from the booked opening balance if there is one, otherwise from the
current balance — and revaluation starts from there. An opening balance still
sitting in `pending_review` does not count as booked, because the ledger has not
moved yet.

Turn the whole behaviour off with `INVESTMENT_REVALUATION_ENABLED=false`.

### What is still not covered

No holdings, cost basis, realized gain/loss, or dividend detail — those need
Plaid's `investments` product. Mark-to-market moves the *total value* of the
account, which is what the balance sheet needs; it is not a substitute for a
broker's own statement at tax time.

## Reconnecting a bank (v0.4.11)

Banks expire. Plaid signals it with `ITEM_LOGIN_REQUIRED` (or warns first with
`PENDING_EXPIRATION`), and until you sign in again every call for that bank
fails.

Bank Bridge detects this from **two independent signals**, so neither is
required:

| Signal | Needs | Speed | Plaid cost |
|---|---|---|---|
| ITEM webhook | a public webhook URL | instant | **none** — handling it makes zero API calls |
| Failed-sync error text | nothing | next poll | none beyond the poll that failed |

A bank in that state is **parked**: it is not polled (so it stops burning a
failed billable request every cycle), it is badged on `/admin/accounts`, and it
gets a **Reconnect** button. A successful sync clears the flag automatically, so
a warning the bank later resolves on its own can't strand the connection.

### Reconnect vs. re-link — they are not the same thing

**Reconnect** uses Plaid's *update mode*: Link is handed the access token you
already hold, and re-authenticates the **existing** Item. `item_id` and every
`account_id` survive, so every mapping keeps working and there is nothing to
exchange afterwards. It also doesn't create a second billable Item at Plaid.
**This is what you want for expired credentials.**

**Re-linking** — adding the bank again as a new connection — creates a genuinely
new Item, and Plaid issues **new account ids** for the same real-world accounts.
Everything keyed on the old ids misses.

### Fingerprint adoption

For the re-link case, each new account is matched against retired ones on
**(institution, last-4, type, subtype)**. On an *unambiguous single match*, the
configuration is **moved** across:

- the ERPNext Bank Account and GL account mapping
- the owning Company (unless you picked one for this link — your explicit choice
  wins)
- import status and sync toggle
- the opening-balance entry, **re-keyed** to the new account id

That last one is the load-bearing part. The thing that actually prevents a
double-booked opening balance is the unique synthetic key
`opening-balance:<account_id>` — not the denormalized pointer. Leaving it on the
dead id would make the re-linked account look like one that had never been
booked, and it would book a second opening balance, silently double-counting.

Adoption **moves** rather than copies: two rows naming one ERPNext Bank Account
would both push into it and duplicate every transaction in the overlap. The
donor is retired in the same operation and stamped with
`superseded_by_account_id`, so the history stays queryable.

Matching is exact and **unambiguous-only**. Zero matches or two, and you map
that account by hand — fuzzy matching would silently attach one bank account's
ledger to a different one, and exact matching can only ever under-adopt, which
is the safe direction to be wrong in. An account with no last-4 is never
adopted, because without it "depository/checking" matches every checking account
at the bank.

Bank Bridge also rewrites the `plaid_account_id` custom field on the mapped
ERPNext Bank Account. Without that, ERPNext's own dedup can't see the account and
the next import tries to create a duplicate whose name collides. If ERPNext is
down at the time, the local adoption still commits and the repoint converges on
a later sync.

Turn the whole behaviour off with `RECONNECT_ADOPT_ENABLED=false`.

## Disconnecting a bank (v0.4.7)

Each linked bank on `/admin/accounts` has a **Disconnect this bank** button in
its header. It asks for confirmation, then calls Plaid's `/item/remove`:

> **Disconnect Wells Fargo?**
> Plaid will stop sending new transactions. Your existing transactions and
> generated Journal Entries stay in ERPNext. You can re-link this bank later.

**What it does.** Plaid invalidates the Item's access token and stops pulling
from the institution. Locally the Item is flagged `disconnected` — it shows a
**🔌 Disconnected** badge, the sync scheduler skips it on every subsequent tick,
and an inbound Plaid webhook naming it is ignored (that endpoint is
unauthenticated, so this also stops a spoofed payload provoking doomed API
calls).

**What it does *not* do.** Nothing is deleted. The Item row, its Plaid accounts,
every mirrored transaction and every generated Journal Entry stay exactly where
they are, and the Journal Entries already submitted to ERPNext are untouched.
**Disconnecting stops the future feed; it does not erase history** — your books
keep every entry the bank ever produced. That is deliberate: a disconnect is an
operational decision about data collection, never a destructive one about
accounting records.

**Ordering.** Plaid is called *first*, and the local flag is written only if the
call succeeded. The reverse order would leave an Item marked disconnected here
while Plaid happily kept the token live — the app would stop syncing it, so
nobody would notice the bank was still connected upstream. If Plaid refuses, the
modal shows the error and the bank stays connected.

**Re-linking.** Link the same bank again whenever you like. Plaid issues a fresh
`item_id` and access token, so you get a **new** Item; the disconnected one stays
as the permanent record of the previous link. Both are visible on the Accounts
page.

**Access.** The endpoint (`POST /api/items/<item_id>/disconnect`) lives on the
admin blueprint, so it is covered by `ADMIN_BASIC_AUTH_*` when configured and is
never exposed by the deployment recipes above. Every disconnect writes an
`item_disconnected` audit event recording who did it, when, and why.

## Counterparty overlay (v0.4.5)

ERPNext's Customer and Supplier are unrelated doctypes. That is the right call
for accounting — each side keeps its own ledger, its own tax treatment and its
own audit trail — and the wrong one for *identity*. A party that trades with you
in both directions ends up as two records that share nothing but a spelling, and
no report can put them back together.

Bank Bridge adds a **`Counterparty`** doctype that links 0-or-1 Customer and
0-or-1 Supplier. It is a pure overlay: **nothing about how anything posts
changes.** Every debit and credit still lands on the underlying Customer or
Supplier, so ERPNext's own reports, the audit trail and any existing workflow
keep working exactly as before.

| Field | Meaning |
| --- | --- |
| `counterparty_name` | the party name; also the docname (autoname `field:counterparty_name`) |
| `counterparty_type` | Individual / Company / Financial Institution / Government / Other |
| `customer_link` | the ERPNext Customer for this party, if any |
| `supplier_link` | the ERPNext Supplier for this party, if any |
| `dual_role_flag` | read-only; true exactly when **both** links are set |
| `date_of_first_transaction` | read-only; refreshed by the nightly rollup |
| `total_activity_ar` / `total_activity_ap` | read-only cached volume, refreshed by the rollup |
| `notes` | free text |

### How records get created

Three paths, all idempotent and all additive:

1. **At startup** — about 15 seconds after boot, on the one elected worker, the
   doctype is provisioned over the REST API and the Customer and Supplier
   records you already have are paired into Counterparties. Turn the pairing off
   with `COUNTERPARTY_AUTO_PAIR=false`. The boot log always says which of
   `created` / `already_present` / a named failure occurred.

   > **v0.4.5 upgraders:** this startup path only became real in **v0.4.6**.
   > Under v0.4.5 provisioning ran *only* off the ERPNext account-import path,
   > so an install whose accounts were already imported under an earlier version
   > never created the doctype — the symptom is 404s in the log from the read
   > paths and no `Counterparty` records anywhere. Upgrading to v0.4.6 fixes it
   > on the next restart; `python -m scripts.provision_counterparty_doctype`
   > fixes it now, without one.
2. **At party creation** — whenever Bank Bridge auto-creates or resolves a
   Supplier or Customer, it finds-or-creates the matching Counterparty and fills
   in that side's link. A dual-role party (v0.4.0.8's bank heuristic) gets both
   ERPNext records and therefore both links, so `dual_role_flag` lights up on
   its own.
3. **By hand** — `python -m scripts.pair_existing_customer_supplier --dry-run`
   shows the plan; without the flag it applies it. To force the *doctype* itself
   (and get the full reason if ERPNext refuses):

   ```bash
   docker exec <bankbridge_container> python -m scripts.provision_counterparty_doctype
   ```

   Idempotent, additive, and it exits non-zero when the doctype is unavailable,
   so it can gate a deploy step. `--no-pair` provisions the doctype only.

Matching is on the **exact** party name. "Wells Fargo" and "Wells Fargo Bank NA"
stay two Counterparties, deliberately: silently fusing two tax identities is a
mistake that surfaces as a wrong 1099 in January, while under-pairing is visible
and takes thirty seconds to fix by hand. Auto-link only ever *fills a blank* — a
link you corrected by hand is never overwritten.

### The screens

`/admin/counterparties` lists every party with role badges (💵 Customer, 💳
Supplier, both = dual). Clicking one opens the **combined ledger**: both roles
in one chronological view, read live from `GL Entry`, with AR balance, AP
balance and net position, and drill-through links to the underlying ERPNext
vouchers.

Three reports hang off it, each also available as JSON at the same path under
`/api/`:

- **Aged balances** (`/admin/counterparties/reports/aged`) — 30/60/90/120+
  buckets, netted across both roles. Note this ages **GL entries by posting
  date**, not invoices by due date: Bank Bridge posts Journal Entries, which
  have no due date, so ERPNext's own Accounts Receivable Summary shows a farm
  that reconciles bank activity almost nothing. The two will disagree for any
  party you also invoice properly, and that is expected.
- **1099-eligible** (`/admin/counterparties/reports/1099`) — Counterparties with
  a Supplier link whose type is neither Financial Institution nor Government.
  This is the list "every Supplier" should have been: it cannot accidentally
  include your bank or the IRS, because the type is set when the party is
  created rather than guessed at in January. It also shows what it excluded and
  why. It is a **candidate** list — the $600 threshold, W-9 status and the
  corporation exemption remain human judgement calls.
- **Top by activity** (`/admin/counterparties/reports/top`) — ranked by combined
  AR + AP **volume** for the current fiscal year. Volume, not balance: a
  customer who always pays on time would rank last on any balance-based measure
  while being one of the most important parties you have.

### The rollup job

A background job (default daily, `COUNTERPARTY_ROLLUP_INTERVAL_HOURS`) refreshes
each Counterparty's cached activity totals and first-transaction date. It reads
the party-bearing slice of the ledger **once** and aggregates in Python, rather
than issuing two queries per party — so its cost is flat as the party list
grows — then writes only the records whose numbers actually moved. On a farm
between seasons the steady state is zero writes.

The screens never depend on it: every page reads the ledger live. The cached
fields exist for people looking at a Counterparty inside ERPNext itself.

### If it can't be provisioned

Creating a DocType needs a System Manager API user. If yours isn't one, the
probe fails, the overlay is marked unavailable for the life of the process, and
**everything else carries on unchanged** — party creation, Journal Entries and
the whole posting path never depend on it. `/admin/counterparties` then explains
what happened rather than erroring. Set `COUNTERPARTY_OVERLAY_ENABLED=false` to
skip it entirely.

## Data model

| Table | Purpose |
|-------|---------|
| `plaid_items` | one linked login/institution; encrypted access token + sync cursor |
| `plaid_accounts` | accounts within an item; ERPNext Bank Account mapping + sync toggle + import status + opening-balance JE link (v0.4.4) |
| `bank_transactions` | local mirror of Plaid transactions + ERPNext docname/state |
| `suppliers` | merchant → ERPNext Supplier cache (normalized name, tallies) — v0.3.0 |
| `categorization_rules` | user rules: match predicate → offset account + direction + party + template (bank side from the txn; v0.3.1) — v0.3.0 |
| `generated_journal_entries` | per-JE state record (state, rule, JE docname) — v0.3.0 |
| `intercompany_transfer_pairs` | detected transfer between two owned Companies: both legs, both Companies, confidence, both JE docnames, state — v0.4.1 |
| `plaid_statements` | one bank-issued statement: Plaid's statement id (UNIQUE — the idempotency guard), period, opening/closing balances parsed from the PDF (NULL when unreadable), stored PDF path — v0.4.9; plus the ERPNext `Bank Statement` docname and last sync time, NULL until uploaded — v0.4.10 |
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
- `/admin/intercompany` — transfers detected between two Companies you own:
  both sides, confidence score, both generated Journal Entries; approve (submits
  both atomically) / unpair (cancels both and returns the transactions to the
  normal rules), individually or in bulk, filtered by Company / state /
  confidence
- `/admin/statements` — bank statement PDFs per account and month, each
  reconciled against the mirrored transactions (opening + movement vs the bank's
  closing balance), with mismatches flagged as a dollar delta; view any PDF
  inline, or pull new statements on demand
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
callback paths — `/bankbridge/plaid/*` and `/bankbridge/api/plaid/*` — over HTTPS, while `/admin` and
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
and forward only the OAuth callback `/bankbridge/plaid/oauth_return` to port 5202**,
keeping the admin UI *and* the unauthenticated Plaid write endpoints on the LAN.
(Earlier revisions of this guide forwarded the whole `/bankbridge/plaid/*` and
`/bankbridge/api/plaid/*` prefixes — see [Restricting Tailscale Funnel to the OAuth
callback only](#restricting-tailscale-funnel-to-the-oauth-callback-only) for why
that is wider than it needs to be, and how to narrow it.) After setting one up, register the resulting HTTPS URL as the
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
# Expose ONLY the OAuth callback over HTTPS → local port 5202.
# Note the target has NO path: Tailscale forwards the FULL request path to the
# backend (it does not strip the mount prefix), so a request to
# https://<host>/bankbridge/plaid/oauth_return arrives at 127.0.0.1:5202/bankbridge/plaid/oauth_return.
tailscale funnel --bg --https=443 \
  --set-path=/bankbridge/plaid/oauth_return http://127.0.0.1:5202

tailscale funnel status   # shows the public https://<your-umbrel>.<your-tailnet>.ts.net URL
```
Your redirect URI is then
`https://<your-umbrel>.<your-tailnet>.ts.net/bankbridge/plaid/oauth_return`.

> Substitute your own port if your install doesn't publish Bank Bridge on
> `5202` (check `docker ps`). To tear the Funnel down again:
> `tailscale funnel --https=443 --set-path=/bankbridge/plaid/oauth_return off`.

**Verify — do not skip this.** The point of the config is what it *refuses*:
```bash
HOST=https://<your-umbrel>.<your-tailnet>.ts.net

curl -sI $HOST/bankbridge/plaid/oauth_return        # Expect: 200 — the callback works.
curl -sI $HOST/admin                     # Expect: 404 — admin UI stays LAN-only.
curl -sI $HOST/bankbridge/api/plaid/create_link_token   # Expect: 404 — see below.
curl -sI $HOST/bankbridge/api/plaid/webhook             # Expect: 404 — see below.
```
If any of the last three return something other than 404, the Funnel is wider
than it should be — re-check the `--set-path` value and re-run
`tailscale funnel status`.

#### Restricting Tailscale Funnel to the OAuth callback only

**This matters.** A Funnel URL is on the public Internet. It is unguessable, but
it is not secret — it appears in TLS certificate transparency logs, and it is
handed to every bank you link. Treat it as public knowledge.

Bank Bridge's `/bankbridge/plaid` and `/bankbridge/api/plaid` blueprints are **unauthenticated by
design** (the Plaid callback can't carry your admin credentials, and Umbrel's
LAN boundary is the assumed trust boundary). Funnelling those prefixes broadly
therefore publishes four write endpoints to the world:

| Endpoint | What an attacker gets |
| --- | --- |
| `POST /bankbridge/api/plaid/create_link_token` | Burns billable Plaid API calls on your account, at whatever rate they like |
| `POST /bankbridge/api/plaid/set_link_company` | Redirects which ERPNext Company a pending link will book to |
| `POST /bankbridge/api/plaid/exchange_token` | Attempts to attach an Item they control to your install |
| `POST /bankbridge/api/plaid/webhook` | Spoofs "new transactions" events — there is **no signature verification** — forcing unscheduled syncs |

Only **`/bankbridge/plaid/oauth_return`** has to be publicly reachable: it is the URL the
bank redirects the operator's browser back to after an OAuth login. Everything
else is called by *your own browser on the LAN*, so nothing breaks by keeping it
off the Funnel.

**Do NOT do this** — the older, broader config this README used to recommend:
```bash
# ✗ WRONG — publishes all four write endpoints above.
tailscale funnel --set-path /bankbridge/plaid       http://localhost:5202/bankbridge/plaid
tailscale funnel --set-path /bankbridge/api/plaid   http://localhost:5202/bankbridge/api/plaid
```
If you set that up previously, turn both off and re-run the single-path command:
```bash
tailscale funnel --https=443 --set-path=/bankbridge/plaid off
tailscale funnel --https=443 --set-path=/bankbridge/api/plaid off
```

**Security note.** With the single-path Funnel, `/admin` and every write
endpoint are unreachable from the Internet and stay on the LAN. Set
`ADMIN_BASIC_AUTH_*` as defense-in-depth regardless — and note it also covers
the v0.4.7 disconnect endpoint (`POST /api/items/<id>/disconnect`), which lives
on the admin blueprint precisely so it is never publicly reachable.

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
    path: ^/bankbridge/plaid/oauth_return$       # the OAuth callback — the ONLY public path
    service: http://localhost:5202
  - service: http_status:404          # /admin, the Plaid write endpoints, all else
```
The exact-match path is deliberate: broadening it to `^/bankbridge/plaid/.*` or
`^/bankbridge/api/plaid/.*` publishes four unauthenticated write endpoints — see
[Restricting Tailscale Funnel to the OAuth callback
only](#restricting-tailscale-funnel-to-the-oauth-callback-only), which explains
the risk in full (it applies to every option here, not just Funnel).
Then run it (or install as a service):
```bash
cloudflared tunnel run bank-bridge
```
Your redirect URI is `https://bank-bridge.<your-domain>/bankbridge/plaid/oauth_return`.

**Verify**
```bash
curl -sI https://bank-bridge.<your-domain>/bankbridge/plaid/oauth_return   # Expect 200
curl -sI https://bank-bridge.<your-domain>/admin                # Expect 404
```

**Security note.** The `ingress` rules publish only the exact OAuth callback
path; the catch-all `http_status:404` keeps `/admin` and the Plaid write
endpoints off the public hostname. Enable `ADMIN_BASIC_AUTH_*` as an extra
layer.

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

    # `location =` is an EXACT match — the OAuth callback and nothing else.
    # A prefix match (`location /bankbridge/plaid/`) would also publish the four
    # unauthenticated Plaid write endpoints; see the Funnel section above.
    location = /bankbridge/plaid/oauth_return {
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
Your redirect URI is `https://<your-domain>/bankbridge/plaid/oauth_return`.

**Verify**
```bash
curl -sI https://<your-domain>/bankbridge/plaid/oauth_return   # Expect 200
curl -sI https://<your-domain>/admin                # Expect connection closed (444)
```

**Security note.** Only the exact `/bankbridge/plaid/oauth_return` path is proxied;
`location /` returns `444` so `/admin` and the Plaid write endpoints are never
served publicly. Because a reverse proxy is the easiest place to accidentally
widen scope, set `ADMIN_BASIC_AUTH_*` too.

## Path migration (v0.4.8)

In v0.4.8 every Plaid-facing route moved under a `/bankbridge/` prefix:

| Pre-v0.4.8 | v0.4.8 |
| --- | --- |
| `GET /plaid/oauth_return` | `GET /bankbridge/plaid/oauth_return` |
| `POST /api/plaid/create_link_token` | `POST /bankbridge/api/plaid/create_link_token` |
| `POST /api/plaid/exchange_token` | `POST /bankbridge/api/plaid/exchange_token` |
| `POST /api/plaid/set_link_company` | `POST /bankbridge/api/plaid/set_link_company` |
| `POST /api/plaid/webhook` | `POST /bankbridge/api/plaid/webhook` |

`/admin/*` and `/api/sync/plaid_now` are unchanged — they are LAN-only admin
surfaces, never publicly exposed, and never handed to Plaid.

**Nothing breaks on upgrade.** Two shims carry an existing install across:

- **Old URLs still answer.** Every path in the left column responds with a
  permanent redirect to its right-column equivalent, query string intact (the
  OAuth return carries `oauth_state_id`, and dropping it would strand the
  handoff). The GET callback redirects with `301`; the POST endpoints use `308`,
  which is `301`'s method-preserving twin — a plain `301` would let a client
  downgrade `POST` to `GET` and silently drop the request body. Each redirect
  logs at INFO, so `docker logs` tells you whether anything is still on an old
  path.
- **Stored settings auto-migrate.** If `{DATA_DIR}/plaid_settings.json` holds a
  redirect or webhook URL on an old path, it is rewritten onto `/bankbridge/`
  when read — scheme, host, and port preserved. Idempotent, logged once per
  field per boot, and persisted the next time you save the settings page. You do
  not need to edit anything by hand or re-link any bank.

**What you should still do, once.** The redirects are a safety net, not the
destination — Plaid must be told the real URL:

1. Redeploy on v0.4.8 and open `/admin/plaid_settings`. The redirect URI should
   already show the `/bankbridge/` path.
2. Re-point your public HTTPS path at the new callback. For Tailscale Funnel:
   ```bash
   tailscale funnel --https=443 --set-path=/plaid/oauth_return off
   tailscale funnel --bg --https=443 \
     --set-path=/bankbridge/plaid/oauth_return http://127.0.0.1:5202
   ```
   (The target carries no path — Tailscale forwards the full request path to the
   backend rather than stripping the mount prefix, so adding one would produce a
   doubled `/bankbridge/bankbridge/…`. Substitute your own port if your install
   doesn't publish Bank Bridge on `5202`.) Cloudflare Tunnel and nginx users:
   update the `path:` / `location =` value the same way.
3. Register the new URL in the Plaid dashboard (Developers → API → Allowed
   redirect URIs) and remove the old one once you've confirmed the new one works.
4. Verify:
   ```bash
   HOST=https://<your-umbrel>.<your-tailnet>.ts.net
   curl -sI $HOST/bankbridge/plaid/oauth_return   # Expect: 200
   curl -sI $HOST/admin                           # Expect: 404
   ```

## Multi-app path prefix convention

> Any Bank-Bridge-adjacent app hosted on the same Umbrel that needs public
> Tailscale Funnel exposure **MUST** prefix its paths with `/<app-name>/` — e.g.
> `/bankbridge/`, `/bucketlog/`, `/volumevision/`. This prevents path collisions
> across apps sharing the same tailnet hostname and makes ownership obvious in
> logs and audit trails.

The constraint is Tailscale's: a Funnel hostname belongs to a *machine*, so every
app on the box is reached through the same `https://<host>.<tailnet>.ts.net` and
separated only by path. An app that claims a generic prefix (`/api/`, `/plaid/`,
`/webhook/`) makes the next app's callback either impossible or ambiguous, and
makes a line in an access log unattributable. One prefix per app, claimed up
front, costs nothing and removes the whole class of problem.

Within Bank Bridge the rule applies to Plaid-facing paths only. `/admin/*` and
the internal `/api/*` endpoints stay where they are: they are LAN-only, never
funnelled, and moving them would churn bookmarks for no gain.

## Configuration

All runtime settings can be entered in the admin UI (persisted to the data
volume) or seeded via environment variables.

### 1. Plaid (`/admin/plaid_settings`)

Create an app at [dashboard.plaid.com](https://dashboard.plaid.com):

- Copy your **Client ID** and per-environment **Secrets** (Developers → Keys).
- Register the redirect URI `http://umbrel.local:5202/bankbridge/plaid/oauth_return`
  (Developers → API → Allowed redirect URIs) — required for OAuth banks.
- Start in **sandbox** to rehearse the flow; switch to **production** once your
  Plaid app is approved for the institutions you need.

**Production OAuth needs HTTPS.** Plaid accepts a plain `http://…` redirect URI
in sandbox, but **production OAuth banks require an `https://` redirect URI**
reachable from the public Internet. You only need to expose the two callback
paths (`/bankbridge/plaid/*` and `/bankbridge/api/plaid/*`), not `/admin`. See
[Production Deployment](#production-deployment-https-for-plaid-oauth) for three
ready-to-use patterns (Tailscale Funnel, Cloudflare Tunnel, or nginx +
Let's Encrypt), then register the resulting HTTPS URL (e.g.
`https://<your-host>/bankbridge/plaid/oauth_return`) as the redirect URI in both the Plaid
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
`credit card`, `line of credit` → account type **Credit**. Investment accounts
(401k, IRA, brokerage, crypto, …) are supported **balance-only** (see below).
Loans (mortgage, student, auto) and the catch-all `other` are **not** Bank
Accounts in ERPNext's model and are skipped with a "not supported" note. Set
`ERPNEXT_DEFAULT_BANK_ACCOUNT_TYPE` to force a single account type for every
import if your Chart of Accounts names them differently.

**Investment accounts (balance-only, v0.4.0).** Plaid `investment` accounts
(401k, IRA, HSA, brokerage, crypto, …) don't return transactions without Plaid's
separate, pricier **investments** product, so Bank Bridge supports them
**balance-only**: it still creates a **Bank Account** + GL leaf for each, but
**does not sync transactions** — it only mirrors the current balance. On the
Accounts page these rows carry a *"balance-only, no transactions"* badge.

The GL leaves are placed under **Assets → Non-current Assets → Investments**,
auto-subdivided by Plaid subtype:

| Plaid subtype | Investments subgroup | Group # |
|---|---|---|
| `401k`, `ira`, `roth`, `retirement`, `hsa` | **Retirement** | 1310 |
| `brokerage`, `mutual fund`, `stock`, `bond` | **Marketable Securities** | 1320 |
| `crypto exchange` | **Digital Assets** | 1330 |
| anything else | **Other** | 1390 |

(The **Investments** parent group is 1300; a `crypto`-subtype account lands under
Digital Assets even if it comes from a non-investment institution. Reserved
numbers are only applied when your chart already numbers its accounts.) The
current balance is written to a `plaid_balance` **Custom Field** on the Bank
Account each time `/accounts/get` refreshes (daily by default) — it's
**informational**, never reconciled. This is the roadmap's **tier-2** investment
support (balances in the books); full **tier-3** support (holdings + investment
transactions, which needs Plaid's investments product) is not implemented.
Configure the group names with `ERPNEXT_INVESTMENTS_GROUP_NAME` /
`ERPNEXT_NONCURRENT_ASSETS_GROUP_NAME`.

**Multi-entity: which Company owns the accounts (v0.4.0).** If you keep books
for more than one entity in the same ERPNext, you can choose the **owning
Company** for each linked bank at Plaid Link time. The *Link a bank* page shows a
**Owning Company** dropdown (populated from your ERPNext Companies) above the
link button; whatever you pick is stamped on the new Item and inherited by every
account it fans out to. From then on, that Company is used as the `company` on
the Bank Account, its auto-created GL leaf, and any Journal Entry the rules
engine generates — so each entity's transactions land in its own books.

The guiding principle from the roadmap: **entities own the accounts; Bank Bridge
just provides the pipeline.** Bank Bridge never invents Companies or moves money
between them — it only routes each account's data to the Company you chose.

If an account ends up under the wrong entity, the Accounts page has a
per-account **Company** dropdown framed as *"Override owning Company (correction
only)"*. This is a correction escape-hatch, **not** a normal-flow feature: the
normal flow is to pick the Company once at link time. Clearing the override lets
the account fall back to inheriting its bank's Company.

**Drift protection.** Before posting a transaction, Bank Bridge checks that the
target Bank Account's `company` in ERPNext still matches the account's chosen
owning Company. If someone changed the Company on the ERPNext side, the mismatch
is logged as a `company_drift_detected` **AuditEvent** and the transaction is
**refused** (left pending, never posted into the wrong entity's books) until the
drift is corrected. Installs that never pick an owning Company skip this check
entirely and behave exactly as they did before v0.4.0 — the column simply
resolves to the ERPNext **Default Company**, so upgrading needs no manual step.

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
| `ADMIN_BASIC_AUTH_PASS` | "" | v0.3.6 · optional Basic Auth password for `/admin` (plaintext or a `werkzeug` hash). **Both** vars set → auth on; **either** blank → off (LAN mode). Never gates `/bankbridge/plaid/*` or `/bankbridge/api/plaid/*` |
| `PLAID_CLIENT_ID` / `PLAID_SECRET` | "" | seed; editable in the UI |
| `PLAID_ENV` | `sandbox` | `sandbox` \| `production` |
| `PLAID_REDIRECT_URI` | `http://umbrel.local:5202/bankbridge/plaid/oauth_return` | must match the Plaid dashboard |
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
| `INTERCOMPANY_DATE_TOLERANCE_DAYS` | `3` | v0.4.1 · ± window, in days, the two legs of one intercompany transfer may be dated apart |
| `INTERCOMPANY_DESCRIPTION_THRESHOLD` | `0.6` | v0.4.1 · minimum description similarity (0.0–1.0, stdlib `difflib`) for two transactions to be considered a candidate pair |
| `INTERCOMPANY_CONFIDENCE_THRESHOLD` | `0.75` | v0.4.1 · minimum overall confidence to pair **automatically**; below this the candidate is logged and left for a human |
| `INTERCOMPANY_LOOKBACK_DAYS` | `30` | v0.4.1 · how far back a detection pass looks for a counterparty (each Plaid Item advances its own cursor, so the two legs often arrive on different syncs) |
| `ERPNEXT_LOANS_ADVANCES_GROUP_NAME` | `Loans and Advances (Assets)` | v0.4.1 · Chart-of-Accounts group the auto-created intercompany `Due from …` receivables go under (the `Due to …` payables use `ERPNEXT_CURRENT_LIABILITIES_GROUP_NAME`) |
| `COUNTERPARTY_OVERLAY_ENABLED` | `true` | v0.4.5 · master switch for the Counterparty overlay. `false` skips the doctype bootstrap and the auto-link, and the `/admin/counterparties` screens render an empty state |
| `COUNTERPARTY_AUTO_PAIR` | `true` | v0.4.5 · pair the Customer / Supplier records that already exist into Counterparties at startup. Idempotent and additive. `false` leaves the overlay empty until you run `scripts/pair_existing_customer_supplier.py` |
| `COUNTERPARTY_ROLLUP_INTERVAL_HOURS` | `24` | v0.4.5 · how often the background job refreshes each Counterparty's cached activity totals. `0` or negative disables the job; the reports read the ledger live either way |
| `COUNTERPARTY_FISCAL_YEAR_START_MONTH` | `1` | v0.4.5 · first month (1-12) of your fiscal year for the "top counterparties" report. `1` is a calendar year; set `9` for a crop year starting in September |
| `RULE_MATCH_COUNT_ROLLUP_INTERVAL_HOURS` | `24` | v0.4.6 · how often the background job refreshes the **Matches** count on each rule. Local-only (it reads the generated-entry table, never ERPNext). `0` or negative disables the job; **Rerun rules** and the **↻ refresh match counts** button update the counts either way |
| `AUTO_BOOK_OPENING_BALANCE` | `true` | v0.4.4 · book each account's existing balance as a Draft Journal Entry when it is first imported. Set `false` to book them by hand from `/admin/accounts` instead |
| `OPENING_BALANCE_DATE` | `today` | v0.4.4 · posting date for auto-booked opening balances. An ISO date (`2026-01-01`) backdates them to e.g. a fiscal year start; an unparseable value falls back to today with a warning rather than failing the import |
| `OPENING_BALANCE_EQUITY_ACCOUNT_NAME` | `Opening Balance Equity` | v0.4.4 · the Equity leaf every opening balance is offset against, auto-created under the owning Company's Equity root when the chart doesn't ship one |
| `STATEMENTS_ENABLED` | `true` | v0.4.9 · pull bank-issued statement PDFs from Plaid. Off → nothing is listed, downloaded or stored, and opening balances behave exactly as in v0.4.4 |
| `STATEMENTS_PULL_INTERVAL_DAYS` | `30` | v0.4.9 · how often the background job checks each linked bank for statements not already held. Monthly, because that is how often a bank issues one. `0` or negative disables the job |
| `STATEMENTS_STORAGE_PATH` | `{DATA_DIR}/statements` | v0.4.9 · where statement PDFs are filed, as `{item_id}/{account_id}/{yyyy-mm}.pdf`. Defaults onto the persistent volume so PDFs survive a redeploy |
| `STATEMENTS_DOWNLOAD_ATTEMPTS` | `3` | v0.4.9 · attempts per statement download before giving up, with exponential backoff. The row is kept either way, so the next pull retries it |
| `STATEMENTS_RECONCILE_TOLERANCE` | `1.00` | v0.4.9 · how far a statement's closing balance may sit from the mirror's own arithmetic before the period is flagged. Also load-bearing: a statement outside this tolerance is never used to book an opening balance |
| `ERPNEXT_STATEMENTS_ENABLED` | `true` | v0.4.10 · provision the `Bank Statement` doctype and upload statements into ERPNext. Off → nothing is provisioned or uploaded, and v0.4.9's local storage behaves identically |
| `ERPNEXT_STATEMENTS_AUTO_SYNC` | `true` | v0.4.10 · upload automatically at startup and after each pull. Off leaves the doctype provisioned but makes uploading a manual step (`scripts.backfill_erpnext_statements`) |
| `STATEMENTS_VARIANCE_DOLLARS` | `1.00` | v0.4.41 · dollar half of the flag threshold on the statement validation view. A figure must differ from the mirror by more than this **and** more than `STATEMENTS_VARIANCE_PCT` to be flagged |
| `STATEMENTS_VARIANCE_PCT` | `0.001` | v0.4.41 · fractional half of the same threshold (0.1%). Requiring both is what keeps a rounding cent on a $1.3M balance, and a fixed percentage of a $4 one, out of the report |
| `ERPNEXT_STATEMENT_VARIANCE_THRESHOLD` | `10.00` | v0.4.10 · how large a reconciliation variance must be to earn a row in the discrepancy report. Distinct from `STATEMENTS_RECONCILE_TOLERANCE`, which decides whether a period reconciles *at all* — this decides what is worth a human's attention |
| `ERPNEXT_STATEMENT_STATUS_THRESHOLD` | `1.00` | v0.5.0 · how far a `StatementAnchor`'s variance may sit from zero and still set `bank_bridge_reconciled = 1` on the ERPNext Bank Statement record. A dollar spans rounding and sub-$1 statement fees. Separate from `STATEMENTS_RECONCILE_TOLERANCE` so the ERPNext-visible flag can be loosened without changing which statements are safe to anchor on |
| `ERPNEXT_STATEMENT_COVERAGE_MONTHS` | `12` | v0.4.10 · how many closed months the statement coverage report looks back over for gaps |
| `RECONNECT_ADOPT_ENABLED` | `true` | v0.4.11 · when a bank is re-linked, let each new account inherit the ERPNext mapping, Company and opening balance of the retired account it replaces, on an unambiguous (institution, last-4, type, subtype) match. Off → a re-link produces unconfigured accounts, as before v0.4.11 |
| `ERPNEXT_ADOPT_EXISTING` | `true` | v0.4.15 · when the `plaid_account_id` dedup key misses (which on a re-link it always does), reuse the Bank Account this real account is already on the books as, matched on Bank + last-4 + Company then Bank + subtype + Company. Never adopts across Companies or over a record a live Plaid account still claims. Off → `plaid_account_id` is the only dedup key, as before v0.4.15 |
| `PLAID_WEBHOOK_TRIGGERS_SYNC` | `true` | v0.4.11 · whether a Plaid TRANSACTIONS webhook kicks an immediate sync. This costs Plaid calls **beyond** the scheduled poll — the one webhook behaviour that shows up on a bill. Turning it off keeps the free ITEM re-auth webhooks working and lets the scheduled poll do the fetching |
| `INVESTMENT_REVALUATION_ENABLED` | `true` | v0.4.12 · mark balance-only investment accounts to market by posting the change in value as a Journal Entry. Off → the GL leaf keeps its opening value, as in v0.4.0–v0.4.11 |
| `UNREALIZED_GAIN_ACCOUNT_NAME` | `Unrealized Gain/Loss on Investments` | v0.4.12 · the account revaluations post against, auto-created under the company's **Equity** branch. Point it at an income account if your accountant wants unrealized movement in the P&L |
| `INVESTMENT_REVALUATION_MIN_DELTA` | `1.00` | v0.4.12 · the smallest movement worth a Journal Entry. Skipped amounts aren't lost — the next entry measures against the ledger and absorbs them |
| `LOANS_ENABLED` | `true` | v0.4.14 · import loan accounts as liabilities and request Plaid's `liabilities` product. Off restores the pre-v0.4.14 behaviour: loans are unsupported and nothing is created or stored for them |
| `LOAN_INTEREST_ACCRUAL_ENABLED` | `true` | v0.4.14 · generate interest accrual entries from the lender's year-to-date figures. Off still imports the loan and tracks its balance — right when you book interest by hand from the lender's statement |
| `LOAN_INTEREST_ACCOUNT_NAME` | `Interest Expense` | v0.4.14 · the expense leaf accruals debit. An existing account of this name in your chart is adopted, not duplicated |
| `LOAN_MIN_ACCRUAL` | `1.00` | v0.4.14 · the smallest interest movement worth a Journal Entry |
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

> **Read this first (v0.4.16).** If you are here because of `password
> authentication failed for user "bankbridge"`, the cause is *probably not* what
> this section describes. On Umbrel that error is far more often the connection
> landing on **another app's postgres** through the shared `db` network alias —
> see the v0.4.16 entry above. Check the boot log for a `DB host ... is
> AMBIGUOUS` warning before assuming password drift. Genuine drift, described
> below, is real but rare.

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

922 tests cover Fernet encryption round-trip + key persistence, Plaid response
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
scheduler, interval persistence, and the per-Item daily call brake), the
multi-entity owning-Company resolution + inheritance + drift refusal (v0.4.0),
balance-only investment placement + the `balance_only` flag + the
transactions-sync skip, and intercompany transfer detection (v0.4.1: the
amount/sign, date-tolerance, description-similarity and different-Companies
criteria and the confidence score they produce; paired transactions being hidden
from rules that ignore them; the Due from / Due to entries balancing; the
compensating rollback when the second Company's entry fails; auto-created
intercompany accounts; unpair reversing both entries and suppressing
re-detection; the review UI's filters, actions and bulk operations; and the
single-Company regression guarantee that none of it activates), and opening
balances (v0.4.4: the direction rule across asset/liability and the
negative-balance flip on each; Plaid's per-type sign convention surviving into
the debit/credit lines; the Opening Balance Equity leaf being auto-created once
per Company and reused; auto-booking at import and its opt-out; the configurable
posting date including the fall-back on a bad value; re-import never
double-booking; the backfill script's estimate, dry run, idempotency and refusal
to overturn a rejection; the manual amount/date override endpoint; and the
existing approve/reject workflow driving the entries unchanged), and the counterparty overlay (v0.4.5: the doctype bootstrap being idempotent and degrading to a no-op when the API user lacks DocType rights; auto-link on both the Supplier and Customer paths; dual-role detection setting the flag from both links; a human-set link never being stomped; concurrent creates recovering by re-fetching rather than failing; the pairing migration's dry run, idempotency and exact-name matching; ageing bucket boundaries and the netting across roles; the 1099 report excluding Financial Institution and Government types and declaring what it dropped; top-by-activity sorting by combined volume within the configured fiscal year; the rollup reading the ledger a fixed number of times regardless of party count, skipping unchanged records and never blanking a first-transaction date; and every screen rendering on an install that has no overlay at all), and guided rule authoring (v0.4.6: the four rule-state filters partitioning the eligible transactions with no overlap and no gaps, and ignoring rows the engine has never seen; merchant grouping counts, totals and ordering, with singletons falling through to one-offs and merchantless rows grouping by description signature; the group prefill producing a `merchant_contains` rule and the per-row prefill a `merchant_exact` one whose regex actually matches the description it came from; the prefill rejecting an unknown match type and never creating a rule on its own; the match-count rollup folding an archived version's matches into its live successor, terminating on a supersede cycle, skipping unchanged rules and leaving `updated_at` alone; the scope-mismatch warning firing on a Company mismatch, staying silent for a Company-agnostic rule or an unconfigured ERPNext, persisting on the confirmed re-submit, and preserving the edited rule's id so confirming supersedes rather than duplicates; and the `match_count` migration adding the column to an existing database, idempotently, with the first rollup filling in real history), and counterparty doctype provisioning (v0.4.6: the startup job being actually registered on the elected scheduler as a one-shot — the regression guard for the v0.4.5 gap where the code existed but nothing invoked it — and provisioning, pairing, honouring the master switch, surviving an unconfigured or exploding ERPNext, and auditing its outcome; the provision report distinguishing `created` from `already_present` and naming a permission denial, a failed create and an absent DocType API with the reason ERPNext gave; every failure state carrying operator-facing help; `ensure_counterparty_doctype` keeping its boolean contract for the fifteen existing call sites; and the management CLI creating, re-running idempotently, pairing or skipping pairing, reporting a refusal without raising, and exiting non-zero so it can gate a deploy step), and the disconnect flow (v0.4.7: `/item/remove` being called with the DECRYPTED access token; the Item being flagged with a timestamp and audited with actor and reason; the Item, its accounts and its transactions all surviving the disconnect; a Plaid failure — API error or misconfiguration — leaving the Item CONNECTED and unaudited, which is the ordering guarantee the whole flow rests on; an unknown Item 404ing and an already-disconnected one 409ing without re-calling Plaid; the endpoint being covered by the admin Basic Auth gate; only the named Item being affected; disconnected Items dropping out of `sync_all` and out of the unauthenticated webhook kick while connected ones still run; a re-link minting a new Item and leaving the old row and its timestamp alone; the Accounts page swapping the Disconnect button for a 🔌 badge and rendering the modal copy; the wrapper building real plaid-python 40.1.0 request models for all seven endpoints — including omitting a blank cursor rather than sending `''`; the dependency pins staying at or above the CVE-patched gunicorn and the `/item/remove`-capable SDK; and `rotate_db_password.sh` parsing as valid bash, announcing its detected context, running its container path without any docker client — the v0.4.7 regression guard — reaching for psycopg2 rather than the absent `psql`, explaining itself when `DATABASE_URL` or docker is missing, and deriving the same rescue password the app does), and bank statements (v0.4.9: `/statements/list` flattened across accounts with the owning account_id carried onto every row; the deliberate asymmetry where a listing failure returns `[]` — an Item without the product is the common case — while a download failure raises, because Plaid promised that document; the `statements` product being requested at Link time and the token FALLING BACK to transactions-only when Plaid refuses, which is what keeps an unapproved application linking banks; balance recovery from real PDF text in both the depository and credit-card vocabularies, negatives printed as `-` and as parentheses, and the NULL — never a guess — for an unrecognized layout, an image-only statement or a label whose amount sits outside its own window; the storage layout, path components that cannot escape the store even when spelled `..`, atomic writes and a re-issued statement not overwriting the first; idempotency on both levels, where a statement already held costs no download and a row whose PDF vanished is re-fetched into the same row; exponential backoff on a failed download and the row that survives total failure so the next pull retries it; reconciliation across account types with pending, removed and out-of-period rows excluded, and `no_data` held distinct from `mismatch`; ANCHOR SAFETY — a statement being refused when the mirror cannot reproduce its closing balance or when movement predates it, the oldest qualifying statement winning, and a quiet period reconciling trivially; the upgrade guarantee that import fetches statements but books exactly what v0.4.4 booked, with only the backfill path anchoring; the admin page flagging a discrepancy by dollar delta, the PDF served inline with no-store and nosniff, and a missing file 404ing rather than 500ing; and the monthly job's cadence, its disable switch and its refusal to die on a failed pull), and statements inside ERPNext (v0.4.10: the `Bank Statement` doctype provisioning idempotently and naming every refusal — a permission denial, an absent DocType API, a rejected create — with the reason ERPNext gave; the guard `Counterparty` never needed, where an existing `Bank Statement` doctype without a `plaid_statement_id` field is declared foreign and REFUSED rather than written to; the doctype spec keeping the unique key, the required Bank Account link and a label on every field; the startup job being actually registered on the elected scheduler — the same regression guard v0.4.6 added after v0.4.5 shipped a doctype nothing invoked — and provisioning, syncing, honouring both switches and surviving an unconfigured or refusing ERPNext with local state untouched; the upload carrying the mapped fields and attaching the PDF privately, omitting rather than zeroing balances it could not parse, and still leaving a usable record when the attachment fails; idempotency on both levels, where a second pass uploads nothing and a record whose local docname was lost is ADOPTED rather than duplicated — including when a concurrent worker wins the create race; a missing ERPNext Bank Account and a missing period being skips with reasons rather than failures; the reconciliation verdict mapping all three outcomes, keeping the sign of the variance, reaching ERPNext on create, being RE-pushed when a later backfill closes a gap, and costing no write when unchanged; both reports over ERPNext and over the local fallback on a 500 and on an expired key, the discrepancy report comparing the absolute variance and ignoring unreconcilable statements, and the coverage report finding interior and trailing gaps while refusing to blame an account for months before its first statement or for the month still in progress; the admin pages actually RENDERING — the guard for the one bug this release shipped and caught, a context key colliding with `render_template_string`'s own first parameter, which every module-level test passed straight through; and both CLI scripts creating, re-running idempotently, dry-running without a write, adopting prior records and reporting a refusal without raising). The Plaid SDK and ERPNext are mocked (`tests/fakes.py`),
so no network access or extra wheels are needed.

## Security notes

- Bank credentials never reach this app — Plaid Link handles authentication and
  returns a token.
- Plaid access tokens are encrypted at rest with Fernet; the key lives on the
  app's data volume (`fernet.key`, mode `0600`), never in the database or git.
- Least-privilege Plaid scope: `transactions`, plus `statements` when enabled
  (read-only access to statement documents the bank already issued you).
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
- Bulk rule creation: write one rule per merchant group in a single pass,
  instead of one round trip through the editor each.
- Multi-select of Plaid categories on a single rule (currently one category per
  rule; the picker is single-select).

**Done:** ~~Merchant → ERPNext Supplier auto-create + rules-based transaction
categorization~~ + ~~full append-only audit trail with non-destructive rule
history~~ (v0.3.0). ~~Rule-builder autocomplete (merchants + Plaid categories),
category-based Name suggestions, and shadow-conflict warnings~~ (v0.3.2).
~~Guided first-sync rule authoring: unmatched filter, merchant grouping,
create-rule-from-group, per-rule match counts, and scope mismatches caught at
authoring time~~ (v0.4.6). ~~Disconnect a linked bank from the admin UI via
Plaid `/item/remove`, retaining all history~~ (v0.4.7). ~~Bank statement PDFs
with per-month reconciliation against the mirrored transactions, and
statement-sourced opening balances gated on that reconciliation~~ (v0.4.9).

## Compliance and disclosure

This is self-hosted, open-source software with no hosted service behind it. Two
short documents spell out how it treats your data and the terms of use:

- **[PRIVACY.md](PRIVACY.md)** — what data is collected (Plaid transactions,
  account metadata, balances), where it lives (encrypted at rest on your own
  hardware, no telemetry), the only third party involved (Plaid), retention
  (permanent until you delete it), and how to remove everything.
- **[TERMS.md](TERMS.md)** — as-is, no-warranty MIT terms; that you are
  responsible for your own Plaid costs, backups, and the legal/tax correctness of
  your bookkeeping; and the disclaimer of liability for accounting errors.

For the security posture and vulnerability reporting, see
[SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
