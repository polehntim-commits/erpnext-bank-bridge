# Changelog

Notable changes, newest first. Releases before v1.0.0 are documented as
per-version sections in [README.md](README.md), which remains the reference for
how each feature works and why — this file records what changed and when.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses semantic versioning, where the major number tracks the
architecture rather than the API surface.

---

## [1.0.10] — 2026-10-14

**Plaid Statements, end to end: link, consent, pull, reconcile, and the PDF on
its ERPNext Statement Anchor.** See `docs/plaid-statements.md`.

### Changed

- **Link requests Statements as `required_if_supported_products`**, not as a
  `products` entry. A bank without Statements now links anyway (its Statements
  endpoints answer PRODUCTS_NOT_SUPPORTED, shown as "manual upload
  required"). The window runs from up to 24 months back to today. An
  application without the product still falls back to a link without it.
- **Plaid Statements are off by default** behind a persisted switch
  (`/admin/statements`; `STATEMENTS_ENABLED` only seeds it). An install that
  already holds Plaid statements, like OML, is migrated on once and persisted.
  The monthly job is always scheduled and checks the switch on each run, so
  turning it on needs no restart.

### Added

- **"Enable statements for this connection"** on `/admin/accounts`. This is
  Link update mode with `products=["statements"]` and the window, so the user
  sees only the Statements consent pane, and item and account ids are kept.
  `POST /bankbridge/api/plaid/statements_consented` records the consent
  (`plaid_items.statements_consented_at`) and starts a pull.
- **PDFs on the ERPNext Statement Anchor.** Each statement PDF is uploaded
  attached to the anchor for its Bank Account and period, and the anchor's
  `statement_pdf` field is set and checked against ERPNext's echo. This covers
  checking and savings as well as brokerage. It's idempotent: it reads what's
  attached first and links an already-attached file without re-uploading it.
  It runs after every statement pull, on every sync, and in
  `push_statement_pdfs` (new `target`: `both` / `anchors` / `bank_account`).
- **`get_statement_coverage` MCP tool** (read-only). Per account: supported /
  manual_upload_required / not_consented / unknown / off, statements held,
  first and last, missing months, consent date, and the last pull's outcome.
- **`pull_statements` MCP tool.** It runs the monthly job now, in the
  background, behind the kill switch `pull_statements` (off by default).
- **`get_erpnext_push_status` → `statement_pdfs`**: anchors with and without
  their PDF.
- A test asserting that access tokens and PDF bytes never reach the logs.

---

## [1.0.9] — 2026-10-11

**Every categorization rule belongs to one Company, before Constancy Farms,
LLC is linked.**

### Changed

- **Rules are company-scoped, and matching refuses to cross Companies.** A
  blank `applies_to_company` used to mean "every Company". On OML 65 of 67 rules
  were blank, so linking Constancy would have sent its cash withdrawals to OML's
  Member Distributions (182 "CASH"), a payment to Tim to OML's management fees
  (171/172), and its Oregon DOR payroll tax to a licence fee (168).
  - A rule now fires only on transactions whose account EXPLICITLY belongs to
    the rule's Company: the account's owning Company, or its Item's.
  - A blank-scope rule matches nothing.
  - An account nobody assigned matches nothing once any Company has been
    assigned. It doesn't borrow the default Company's rules, so a Constancy
    account linked without picking its Company is not categorized as OML.
  - `generate_journal_entry` refuses a rule handed in across Companies (audit
    `rule_refused_cross_company`), and the JE books into the rule's Company.
- **A logical offset resolves under the rule's Company** for every rule. Before,
  only an unscoped rule resolved "Member Distributions" to its account; a
  scoped one sent the bare name. "Mode B" (one agnostic rule resolving per
  Company) is gone: each Company has its own rules.
- **One rule source of truth: Bank Bridge's rule table.**
  - `RULES_SOURCE` now defaults to `local`.
  - A stored `erpnext` with no ERPNext-mirrored rules (OML: `rules_source=
    erpnext`, `rule_source_in_force=local`) is migrated to `local` on first
    read, once, so the flag and the engine agree. Opting into `erpnext`
    afterwards is explicit and sticks.
  - `list_rules`, `create_rule` and `update_rule` are no longer marked
    deprecated.
- **Authoring requires a Company.** `create_rule` requires `applies_to_company`,
  checked against ERPNext's Companies. `update_rule` refuses to leave a rule
  unscoped. The rule form has no "all Companies" option and refuses a blank
  one. The rules list flags an unscoped rule "⚠ none — never fires".
  `list_rules` filters by `company`.

### Added

- **Boot migration** (`rule_scope.apply_scope`). It assigns every unscoped live
  rule to the install's Company: `RULE_SCOPE_MIGRATION_COMPANY` if set, else the
  one Company every already-scoped rule uses (OML: rules 110 and 184, "Orchard
  Meadow, LLC"), else the only Company in play. When that's ambiguous it assigns
  nothing and says why. Once only, recorded by an `items_company_pinned`
  AuditEvent, it also stamps Items with no Company with that Company, so OML's
  accounts keep matching; a later unassigned Item is never pinned.
- **`get_rule_scope_report` MCP tool** (read-only). Lists unscoped rules
  (should be 0), Items with no Company, and the migration target and its
  reason.

---

## [1.0.8] — 2026-10-04

**One statement per period, and a statement pull now reconciles what it pulls.**

### Fixed

- **Duplicate statement rows after a refresh.** `/statements/refresh` makes
  Plaid re-issue every statement in its window under a new `statement_id`,
  and `/statements/list` keeps listing the old ids beside the new ones.
  Statements were keyed on `statement_id` alone, and v1.0.6's window
  deliberately re-covered the newest month held, so OML got second copies of
  May and June; dev got up to four of each May–September statement. The copies
  re-anchored their periods out of order, which set a false
  `chain_gap_from_prior`, and their anchors and Bank Statement records were
  pushed to ERPNext.
  - A statement whose (account, period_start, period_end) is already held is
    now skipped without being downloaded, and the original row is kept.
  - The refresh window starts the day after the newest statement held,
    following the account furthest behind, with no overlap. Nothing is
    requested when everything up to today is already held.
- **Two pulls at once.** v1.0.6's lock covered one process only. Gunicorn's
  two workers and the scheduler thread could pull at the same moment and store
  the same re-issued statements. A file lock in `DATA_DIR` now serializes the
  scheduled pull, the *Pull statements* button and the webhook fetch across
  the container.
- **A pull didn't reconcile what it pulled.** New statements only had their
  anchors rebuilt. Their Activity lines were never matched, so a boundary
  purchase (bank-dated in the new month, Plaid-dated in the old) could not
  move; OML's June +76.37 stayed open after July arrived. A pull that stores
  anything now runs the same pipeline as *Re-parse*: pair companions → match
  lines (stamp the bank's posted dates) → synthesize → rebuild anchors.
- Matching, synthesis and the anchor rebuild skip later copies, so duplicates
  still on disk can't double-count a line or fake a chain gap.

### Added

- **`dedupe_statements` MCP tool** (kill switch `dedupe_statements`, off by
  default; `dry_run` defaults to true).
  - It removes re-issued copies from Bank Bridge's own mirror, keeping the
    original (lowest id) per account and period.
  - A copy goes only when its PDF is byte-identical. Its manual adjustments
    and any synthesized transactions move to the original first, and its
    derived lines, anchor and queued ERPNext push are dropped.
  - Copies that differ are kept and reported. PDF files stay on disk.
  - It never writes ERPNext. Bank Statement records it holds for removed copies
    are listed in `erpnext_bank_statements`.

### Dev, read-only

- 28 duplicate statement rows across 6030 and 9401 (May–September 2026). All
  are byte-identical, with no lines, synthesized rows or adjustments; each has
  an anchor.

---

## [1.0.7] — 2026-10-03

**Stale duplicate Bank Transactions are flagged and kept, never deleted.**

### Changed

- **`cleanup_duplicate_bank_transactions` now flags and keeps.**
  - **Input:** an explicit list of `{bank_transaction (stale), duplicate_of
    (kept)}`. A bare stale name is also accepted; its kept twin is resolved
    from `find_duplicate_bank_transactions` and never guessed. Each pair in
    the finder's output now carries the `cleanup_entry` to pass.
  - **Checks on each stale BT:** same Bank Account and amount as the kept
    one; the kept one is live and not itself flagged; no live Journal Entry
    on the stale one (its JEs must already be cancelled); not reconciled.
  - **Flag:** `is_duplicate = 1` and `duplicate_of = <kept>`.
  - **Description:** on a draft it is prefixed `DUPLICATE of ACC-BTN-… — `.
    ERPNext locks a submitted BT's description, so there the flag fields and
    the comment carry the marker.
  - **Cancel:** a draft is submitted first, because Frappe cancels only
    submitted documents. It ends at docstatus 2, status *Cancelled*, out of
    Pending and out of the Bank Reconciliation Tool.
  - **Audit:** a Comment on the Bank Transaction records who, when and why,
    and an AuditEvent is written. A local row still pointing at the stale BT
    is repointed at the kept one.
  - **Safety:** it never deletes anything and never touches a Journal Entry.
    `dry_run` defaults to true, `reason` is required for a real run, and the
    kill switch is off by default.
- **Plaid removals of draft Bank Transactions** are submitted then cancelled,
  instead of failing to cancel and being flagged for review.
- **Two explicit item options**, each for a case a human has already ruled on
  and never inferred:
  - `no_twin: true` (requires a `reason`) for a stale pending row with no
    posted twin to keep, such as a reversed authorization. It is flagged with
    `duplicate_of` empty and the prefix `DUPLICATE (stale pending, no posted
    twin) — `.
  - `allow_amount_difference: true` for a kept twin whose amount
    legitimately differs, such as a tip. The direction (in or out) must still
    match.

### Fixed

- **A posted charge inheriting a cancelled JE.** When a posted transaction
  took over its pending twin's Bank Transaction and Journal Entry record, and
  that JE had since been cancelled by hand (OML's 10/3 cleanup did this),
  the posted charge ended up with no live JE. The push now checks the
  inherited JE in ERPNext and books a fresh one if it is cancelled or gone.

### Verified on dev

- `/statements/refresh` against the live Wells Fargo Item: before the call
  Plaid listed 26 statements ending June 2026; 20 s after, 36 statements
  through September 2026.
- The real flag-and-keep code run end-to-end against dev ERPNext on fresh
  test Bank Transactions: paired, tip difference and no-twin cases were all
  flagged, prefixed, submitted, cancelled and commented. The dry run changed
  nothing, and a re-run reported them as already flagged.

### Added

- **`Bank Transaction.is_duplicate` (Check) and `duplicate_of` (Link → Bank
  Transaction).** These are Custom Fields with `allow_on_submit`, provisioned
  by Bank Bridge over REST on first use, the same way it provisions the Bank
  Statement status fields. They live in Bank Bridge, not erpnext_mcp, because
  erpnext_mcp promises to add no fields to doctypes it didn't create.
- `erpnext_bank.submit_bank_transaction` submits a draft Bank Transaction
  with its full document.

### Known, not changed

- **Every Bank Transaction the sync creates stays a draft (Pending).**
  `erpnext_bank._submit` sends a `{doctype, name}` stub, and Frappe rejects it
  with `TimestampMismatchError`. The code logs "left as draft" and carries on.
  This is the same bug v0.4.0.4 fixed for Journal Entries. Switching the sync
  to `submit_bank_transaction` would move every new Bank Transaction into the
  Bank Reconciliation Tool, so that is left for the operator to decide.

---

## [1.0.6] — 2026-10-03

**New bank statements arrive again.**

### Fixed

- **Statements stopped at the Link date.** Plaid extracts an Item's statements
  once, at Link, for the window the link token names. Statements the bank
  issues later only become listable after `/statements/refresh`, which Bank
  Bridge never called. Every monthly pull re-listed the Link-time set, stored
  nothing, and reported success. On OML the ••6030 brokerage sat at June 2026,
  and its June reconciliation waited on a July statement that could never
  arrive.
- **A failing statement listing no longer reads as "nothing new".**
  `PlaidClient.statements_list` used to return `[]` for every error, so an Item
  that couldn't serve statements, an Item needing re-auth, and a Plaid outage
  all looked like a quiet, successful pull. It now raises, and the pull writes
  the error to its pull record.

### Changed

- **The scheduled statement pull refreshes first.** It asks Plaid for
  `/statements/refresh` on every Item. The window runs from a month before the
  newest statement held, or 24 months back if none, up to today. The pull then
  waits for each Item's `STATEMENTS_REFRESH_COMPLETE` webhook, up to
  `STATEMENTS_REFRESH_WAIT_SECONDS` (default 300) shared across Items, and
  lists. An Item whose webhook doesn't arrive in time is listed anyway (the
  poll fallback), so an install with no public webhook URL still works.
- **`STATEMENTS_REFRESH_COMPLETE` webhook handled.** If a pull is waiting, in
  either gunicorn worker, it proceeds. If nobody is waiting (the refresh timed
  out), the webhook fetches that Item's statements itself on a background
  thread and rebuilds anchors if anything new arrived.
- **The *Pull statements* button on `/admin/statements`** runs the same
  refresh → wait → list → rebuild-anchors pass on a background thread and
  returns immediately. Only one runs per process at a time.
- **Pull records.** Each statements step writes a `plaid_sync_log` row:
  - direction `statements_refresh` (`success` / `failed` / `unsupported`,
    with the requested window or Plaid's error), and
  - direction `statements_pull` (listed/stored/skipped/failed counts and the
    newest period held).

  Product-not-available codes (`PRODUCTS_NOT_SUPPORTED`, `PRODUCT_NOT_ENABLED`,
  …) are recorded as `unsupported` rather than `failed`. The `statements_pulled`
  audit note now names refresh outcomes and errors.

### Added

- `plaid_items.statements_refresh_status`, `statements_refresh_requested_at`
  and `statements_refresh_completed_at`. These are additive, migrate at boot,
  and are NULL on upgrade.

---

## [1.0.5] — 2026-10-03

**A Plaid warning no longer stops a bank syncing forever.**

### Fixed

- **PENDING_EXPIRATION / NEW_ACCOUNTS_AVAILABLE parked the Item permanently.**
  Both are Plaid *warnings* — the Item still works — but the ITEM webhook
  handler set `needs_reauth` for them, and the poll loop skips every
  `needs_reauth` Item. Only a successful pull (or a re-link) clears the flag,
  and a skipped Item is never pulled, so the bank silently stopped syncing
  until someone pressed Reconnect. Now only hard codes park
  (`ITEM_LOGIN_REQUIRED`, `ITEM_LOCKED`, `USER_PERMISSION_REVOKED`); the
  sync-error classifier no longer treats `PENDING_EXPIRATION` text as one
  either.

### Changed

- Warnings (`PENDING_EXPIRATION`, `PENDING_DISCONNECT`,
  `NEW_ACCOUNTS_AVAILABLE`) are recorded in `reauth_reason` with
  `needs_reauth` left false. The Accounts page shows a "⏳ Reconnect soon"
  pill, an advance-notice banner and the Reconnect button; syncing continues.
  No new column or table — hard vs. warning is derived from the code.
- A successful pull clears a park immediately but retires a warning only once
  it is older than 14 days (`reconnect.WARNING_TTL`). PENDING_EXPIRATION is
  sent about a week before the Item stops working and pulls keep succeeding
  until then, so clearing on the next good pull would hide the only advance
  notice Plaid gives. A Reconnect clears a warning at once.
- A warning never overwrites a hard reason on an already-parked Item.

### Upgrade

- On boot, `_unpark_warning_only_items` releases any Item the old behaviour
  parked with a warning reason (`needs_reauth` → false, reason kept, so the
  banner still shows). Idempotent; hard parks are untouched. Those Items
  resume on the next poll.

---

## [1.0.4] — 2026-10-03

**A pending card charge is booked once — when it posts.**

### Fixed

- **Pending charges were expensed twice.** Plaid reports a card charge first as
  a PENDING authorization, then as a POSTED transaction under a new
  `transaction_id` (its `pending_transaction_id` points back) while listing the
  pending id under `removed`. Bank Bridge booked the pending row immediately (a
  Bank Transaction plus a submitted rule Journal Entry), then booked the posted
  row as a second Bank Transaction and a second JE. The removal was meant to
  cancel the first Bank Transaction, but ERPNext refuses to cancel one that a
  submitted JE still references (`LinkExistsError`, HTTP 417), and
  `cancel_bank_transaction` read every 417 as "already cancelled". Nothing was
  cancelled and nothing said so.
- **`cancel_bank_transaction` no longer reads a refusal as success.** On a
  409/417 it re-reads the document and only a real docstatus 2 counts. It now
  returns True/False.

### Changed

- **Pending transactions are held until they post** (new default). Turn on
  *Push pending transactions as unmatched* on `/admin/erpnext_settings` (or
  `ERPNEXT_PUSH_PENDING_TRANSACTIONS=true`) to push them as Bank Transactions
  with **no** Journal Entry. A pending row never gets a Supplier or a JE either
  way, and `rerun_rules` skips it.
- **A posted transaction takes over its pending twin.** `pending_transaction_id`
  is stored now. When it names a row already pushed, the posted row inherits
  that Bank Transaction and its Journal Entry record: the BT's reference
  number moves to the posted id, a draft BT is rewritten in full, and the
  existing JE is reused — never a second one. ERPNext allows only the reference
  number to change on a submitted Bank Transaction, so it keeps the pending
  date and description. If the posted amount differs, the row is **flagged for
  review** (`review_reason`) and neither document is edited.
- **A removed transaction is cancelled properly.** Bank Bridge's own rule JE is
  cancelled first (ERPNext then lets the Bank Transaction go), then the BT. If
  the JE is reconciled elsewhere, or isn't an active Bank Bridge rule entry,
  nothing is cancelled and the row is flagged for review instead. A draft JE is
  left alone and flagged, because drafts can't be cancelled.
- **A rule JE is allocated to its Bank Transaction.** When a rule JE is
  submitted (auto-submit or Approve), it is reconciled against its BT through
  ERPNext's own `reconcile_vouchers`. The BT then reads Reconciled, and no
  other job (ERPNext's Bank Reconciliation Tool, an AI "Bank Entry" run) books
  it again. Fail-soft: a BT something else already reconciled is left as is.
- Plaid `modified` rows use the same update path. A submitted BT that nothing
  links to is still cancelled and replaced as before. One that something links
  to keeps its document and gets a review flag if the amount changed.

### Added

- **`find_duplicate_bank_transactions` MCP tool** (read-only). Lists
  pending/posted pairs that are both still live in ERPNext, with every live
  Journal Entry on either one. That covers both rule JEs and "Bank Entry"
  vouchers whose `cheque_no` is the BT name. Each pair gets a keep/remove
  recommendation: keep the posted BT and one JE (preferring the Bank Bridge
  rule JE); remove the pending BT, its JEs, and any extra JE on the posted BT.
  A pair whose amounts differ is marked `review`.
- **`cleanup_duplicate_bank_transactions` MCP tool.** Cancels an explicit
  approved list, and refuses anything a fresh report doesn't recommend
  removing. It cancels JEs before BTs, never deletes anything, and defaults to
  `dry_run=true`. It has its own kill switch, **off by default**.
- `bank_transactions.pending_transaction_id` and `.review_reason` columns.
  These are additive and migrate at boot. The push summary has a new `review`
  count.

### Upgrade notes

Existing duplicate pairs are **not** touched by the upgrade. Run
`find_duplicate_bank_transactions`, check the pairs, then pass the approved
`remove` entries to `cleanup_duplicate_bank_transactions` (dry run first).

---

## [1.0.3] — 2026-09-26

**Brokerage statement PDFs land on their ERPNext Bank Account.**

### Added

- **`push_statement_pdfs` MCP tool.** Attaches every stored brokerage statement
  PDF that its ERPNext Bank Account does not already carry, as a private file
  named `{institution}-Brokerage-{mask}-{period_end}-Statement.pdf` (e.g.
  `WF-Brokerage-9401-2026-06-30-Statement.pdf`). Optional `account_mask` and
  `period` (`YYYY-MM` or an exact `YYYY-MM-DD` period end) narrow the run; omit
  both to push everything missing. `dry_run` lists what would be pushed under
  `would_push` and uploads nothing. Returns `{mode, pushed, skipped, failed}`
  per statement, each skip with a reason (`already_attached`, `no_pdf`,
  `unmapped`). Gated by its own kill switch, **off by default**.
- **Two upload modes**, chosen with `mode`. Both upload through Bank Bridge's
  own ERPNext client (the same API-key session as the anchor push) to Frappe's
  standard `POST /api/method/upload_file`, private and attached to the Bank
  Account. Nothing needs deploying on the ERPNext side.
  - **`staged`** (the default, and what every sync uses): the stored PDF is
    re-read in chunks and reassembled, and it is sent only if the result
    matches its size on disk and the sha256 of the first read. A file still
    being written or a truncated volume is caught here, not attached.
  - **`direct`**: one read, one upload.

  Both modes use the same name and the same already-attached check, and the
  result says which mode ran.
- **Every sync now runs the same push** as part of its ERPNext leg (beside the
  Plaid metadata push and the queue drain), so a newly pulled statement reaches
  its Bank Account on the next sync. The result appears as
  `erpnext_push.statement_pdfs` in the sync result, as counts.

### How it stays idempotent

- The file name is the key. ERPNext's existing attachments on the target Bank
  Accounts are read first — one File query per run — and a statement whose
  name is already there is skipped. If that list cannot be read, **nothing is
  uploaded**: Frappe does not reliably refuse a duplicate name, so uploading
  blind is how an account ends up with every statement twice.
- No queue and no local "pushed" marker. A failed upload is simply still
  missing on the next run, and a file someone deletes in ERPNext comes back.
- Following v1.0.2, an upload counts as pushed only when ERPNext returns a File
  record with a `file_url`, attached to the right Bank Account, and — when
  Frappe reports one — a `content_hash` matching the bytes sent.

### Scope

- Live (not re-link-retired), mapped **investment** accounts only. Statements
  recorded under a pre-relink Plaid id are attached to the live account's Bank
  Account. Depository statements keep reaching ERPNext through their
  `Bank Statement` records, unchanged.

---

## [1.0.2] — 2026-08-15

**A 200 is not a confirmation, and a Plaid account id is not an identity.**

Two bugs with one shape: the pipe believed something it had not been told.

### Fixed

- **The metadata push marked "unchanged" over four empty Bank Accounts.**
  `sync_now` reported `erpnext_push.metadata.unchanged: 4` while all four
  ERPNext Bank Accounts held null for `plaid_account_mask`,
  `plaid_account_type`, `plaid_account_subtype` and `sync_enabled: false`.

  The cause was not `rebuild_anchors` stamping a fingerprint as a side effect —
  nothing outside `erpnext_push` ever writes one. It was that
  `PushSession.call` discarded ERPNext's reply and treated HTTP 200 as proof,
  then stamped the fingerprint on it — and the fingerprint is exactly what
  stops a fact being pushed again, so the fields were never sent a second time.

  Several things produce a 200 that writes nothing, and this build could not
  tell any of them from a success: Frappe hands a whitelisted method only the
  kwargs its signature **declares** and drops the rest silently (so a push to
  an `erpnext_mcp` older than a field writes nothing); the custom field may be
  absent from the Bank Account doctype; a validation may rewrite the value
  after the write. Which of these the live install actually hit is not
  recoverable after the fact — that opacity is itself the defect. (The
  `tolerance` → `variance_tolerance` fix in v1.0.1 is the same class of bug,
  caught by hand, and is what made this one worth generalizing.)

  Every push now reads ERPNext's own reply. `push_account_pairing` echoes the
  Bank Account **as it now reads**, so the check is a field-by-field comparison
  against ERPNext's state rather than against its claim of success:

  - **confirmed** — the echo shows the fields. Stamp, and stop pushing.
  - **contradicted** — the reply is one this build understands and it says the
    fields are not there. Treated as a failed push: queued, retried on the
    backoff, and `last_error` names the missing fields *and* the likely cause.
    A silent null becomes a queue depth, and it drains itself the moment the
    ERPNext side gains the fields.
  - **unconfirmed** — a reply shape this build cannot read. Neither stamped nor
    queued: retrying would meet the same silence and the queue would never
    drain, so the fact is simply re-sent next sync against an idempotent
    upsert. Counted separately in every push result.

  Every fingerprint already on a live database was written under the old
  meaning of "stamped", so `PUSH_FINGERPRINT_CONTRACT` is folded into the hash
  input. That invalidates them exactly once, with no clock, no migration and no
  state, and each row self-resolves on its first verified push.

- **Anchors built before a Plaid re-link never reached ERPNext.**
  `reconnect.adopt` nulls the retired account's Bank Account on purpose — two
  rows naming one Bank Account would double-post every transaction in the
  overlap — but anchors, statements and transactions recorded before the
  re-link keep the dead `account_id`. `push_anchor` resolved the mapping from
  that one row, found nothing, and reported the entire pre-relink half of the
  history as an unmapped account. It now resolves through the chain to the row
  that inherited the mapping; ERPNext is idempotent on the period, so both
  halves land under one Bank Account in one chain.

- **`get_erpnext_push_status` counted retired ids as unmapped accounts** — nine
  of them on the live install — inviting an operator to go and map nine
  accounts whose correct action was nothing.

### Added

- **`plaid_account_links`** — the durable Plaid id chain, one row per hop.
  `superseded_by_account_id` remains the live pointer; this is the history, and
  it exists because that column cannot be. It is only as durable as the retired
  row it sits on — nothing prunes a `PlaidAccount` today, which is what makes
  that easy to miss: the history has no independent existence, so the day
  anything does prune, the loss is silent and total. It also cannot record when
  or why, and it loses the two identifiers that actually survive a re-link: the
  **mask**, which is what a human calls the account, and the **ERPNext Bank
  Account docname**, which is what the books call it. Both are snapshotted at
  the hop — the one moment both are readable, since `adopt` is about to strip
  the donor's. The link row deliberately carries **no foreign key** to either
  account, so it cannot be taken along with them.
- **`reconnect.chain_for` / `current_account_id` / `mapped_account_for`** — the
  chain walk, reading the link table and `superseded_by_account_id` together.
  Neither source is complete alone: an install upgrading into v1.0.2 has hops
  only the column knows about, an install that has since pruned has hops only
  the table knows about, and their union is the chain. Walks both directions
  (an id can be handed a chain from the middle), depth-bounded and cycle-safe.
- **`relinked_accounts`** in `get_erpnext_push_status` — every real account
  Plaid has re-issued ids for, with its full chain oldest first and the Bank
  Account holding its books. This is the answerable form of "ERPNext has
  `ZE4Z…`, Bank Bridge has `jN7x…`, is that one account or two".
- `unconfirmed` counters throughout: `PushSession.stats`, `drain`,
  `push_metadata_for`, `push_anchors`, and the `erpnext_push` block in
  `run_sync`.

### Changed

- The metadata push is now also the **repoint**. `plaid_account_id` rides in
  the payload and ERPNext writes it keyed by the Bank Account *docname*, so a
  sync after a re-link corrects a record still holding the dead Plaid id. That
  only ever worked in theory before: the unverified push stamped a fingerprint,
  after which the stale id read as up to date forever.
- `FakeERPClient` models both push endpoints properly — it keeps what it was
  asked to write and echoes it back — with a `drops_metadata_fields` flag that
  reproduces the older `erpnext_mcp` build. A fake that answered `{}` could not
  tell a real write from a silent drop either.

### Migration

Additive and automatic. `create_all` builds `plaid_account_links`, and a boot
backfill reconstructs every hop this install already recorded in
`superseded_by_account_id` — idempotent, and one query with no writes on every
boot after the first. Run it before pruning any retired account.

No ERPNext-side change is required: `erpnext_mcp.bank.push_account_pairing`
already declares all four metadata kwargs and `upsert_pairing` already writes
them, so on a current ERPNext this release simply lands the data that v1.0.1
believed it had sent. On an older one, the queue now says so by name.

### State of the live install when this shipped

Checked 2026-08-15 with `get_account_pairing` / `get_statement_anchor_chain`,
against `erpnext_mcp` v0.73.0: all four Bank Accounts already carried correct
metadata *including* the current `plaid_account_id`, so the reported metadata
symptom had cleared on the box before this fix landed. The defect is real and
is reproduced in tests regardless. What is still zero there is Statement
Anchors — `anchored_periods: 0` on every account against ~27 periods held
locally — which is what the chain resolution above is aimed at, and the first
thing to re-check after deploying.

---

## [1.0.1] — 2026-08-15

Recorded retrospectively (commit `d79f246`, released without a version bump).

### Fixed

- **Anchor pushes sent `tolerance`; ERPNext's `push_statement_anchor` declares
  `variance_tolerance`.** Frappe drops kwargs a whitelisted method does not
  declare, so the payload arrived carrying *no* tolerance at all and the
  Statement Anchor doctype recomputed `reconciled` against its own 0.01
  default. The push looked clean and the answer was a different one from Bank
  Bridge's. The renamed key changes the payload, so every anchor's fingerprint
  changed with it and the whole chain re-pushed on the next rebuild — no
  migration was needed. v1.0.2 generalizes the class of bug this belongs to.

---

## [1.0.0] — 2026-08-14

**Bank Bridge becomes a stateless data pipe. ERPNext becomes the source of
truth.**

Through v0.9.1 two systems held authoritative financial data. ERPNext owned Bank
Transactions; Bank Bridge owned the statement anchor chain — the period-by-period
reconciliation truth — plus the categorization rules, the account pairings and
the advisory terms, in its own Postgres. ERPNext could not answer "does ••6030
reconcile in October?" without asking a sidecar Flask app.

This release moves the authority and keeps the pipe. Bank Bridge still talks to
Plaid, parses statement PDFs, pairs trade legs and generates Journal Entries;
everything authoritative is now pushed to ERPNext. **No internal table is
dropped** — they become the write-ahead cache in front of ERPNext and the
rollback behind it.

Implements the Bank Bridge side of *Bank Bridge → ERPNext Consolidation Plan*
(ADR-2026-08-14), sprints 1 and 2 combined.

### Added

- **`app/erpnext_push.py`** — one choke point for every push to ERPNext.
  Fail-soft by construction (no entry point raises), fingerprinted (an unchanged
  fact is not re-sent), queued on failure, and batched behind a circuit breaker
  so an unreachable ERPNext costs one timeout per batch rather than one per row.
- **`erpnext_push_queue` table** — one pending write per target, holding the
  exact payload rather than a pointer to its source, with exponential backoff
  (60s → 1h, capped) in `next_attempt_at`. Drained by every sync.
- **Statement Anchor push** after `rebuild_anchors`, after a statement parses,
  and (forced) after `set_variance_tag` — the variance reason is the one part of
  an anchor a human authored.
- **Account pairing push** on `pair_accounts` and from `/admin/accounts`, both
  sides, so ERPNext's Bank Account records render the relationship from either
  end.
- **Plaid metadata sync** on every sync: `plaid_account_id`,
  `plaid_account_mask`, `plaid_account_type`, `plaid_account_subtype` and
  `sync_enabled` onto the mapped ERPNext Bank Account, gated by a fingerprint so
  the steady state writes nothing.
- **`app/erpnext_rules.py`** — fetches the active Bank Categorization Rule set
  from ERPNext on boot, on every `rerun_rules`, and otherwise at most once per
  `ERPNEXT_CACHE_TTL_SECONDS`, and mirrors it into the local table keyed on the
  new `categorization_rules.erpnext_rule_name`. Tolerates ERPNext's own field
  spellings (`pattern`, `account`, `enabled`, `company`) and three response
  envelopes. A rule ERPNext withdraws is deactivated, never deleted.
- **`combined` match type** — `match_value` is a JSON object of `all` / `any`
  clauses over the other match types, replacing the practice of smuggling an
  amount into a description regex. Malformed specs match nothing; nesting is
  depth-bounded.
- **`advisory.fee_terms()`** — the single seam where a fee rate is read. Derives
  the engine rate from ERPNext's `fee_percent_of_aum` exactly as the local path
  does, and never writes the fetched terms back onto the local row.
- **`get_erpnext_push_status`** (MCP, read-only) — queue depth and contents,
  anchors that have never reached ERPNext, unmapped accounts, and which source
  each read is configured to use *and actually using*.
- **`flush_erpnext_push_queue`** (MCP, kill switch, default OFF) — retry the
  queue now, ignoring the backoff.
- **`ANCHOR_SOURCE`, `RULES_SOURCE`, `ADVISORY_SOURCE`** (`erpnext` | `local`),
  persisted alongside the other ERPNext settings and seeded from the
  environment. These are the plan's §9 rollback in full.
- **`ERPNEXT_CACHE_TTL_SECONDS`** (default `300`).
- `ERPNextClient` accepts a per-client `retry_backoffs`, so a call with a
  fallback does not spend the posting path's 1s/3s/9s ladder proving an outage.
- **`app/appcache.py`** — per-app-instance cache buckets. The three new read
  caches are keyed by database-derived values (a rule-table fingerprint, an
  agreement id) which are unique only within one database; hanging them off
  `current_app.extensions` makes a second app in the same process impossible to
  contaminate.
- A newly-arrived statement now gets its anchor built by the statement-pull job.
  It did not before — `reparse_stale` only re-reads statements parsed by an
  *older* recognizer — so a fresh month sat parsed and unanchored until an
  operator pressed Rebuild. With the consolidation the anchor is what gets
  pushed, so an unbuilt anchor is a month ERPNext never hears about.

### Changed

- **The fourteen migrated MCP tools now answer inside a handover envelope** —
  `{"deprecated": true, "use_instead": "erpnext.…", "data": {…}}` — and carry a
  `DEPRECATED (v1.0.0)` prefix on their `tools/list` description. Every one still
  works, unchanged, for the duration of the migration window. **This is a
  breaking change for any caller that read the payload directly.**
- **The rule matcher is single-pass.** `evaluate_rules` matched against a full
  `CategorizationRule.query` per transaction; it now matches against an
  immutable snapshot fetched once, sorted by priority, hydrating only the
  winner. The snapshot is keyed on a table fingerprint (`count`, `max(id)`,
  `max(updated_at)`), so every authoring surface is seen immediately without
  coupling to an invalidator.
- **With `RULES_SOURCE=erpnext` and at least one mirrored rule present, only
  mirrored rules fire.** Local rules are not deleted and are not consulted.
  With none mirrored, or ERPNext unreachable, the full local set is the
  fallback.
- `rerun_rules` forces a rule refresh before running and reports `rule_source`
  and `rules_refreshed`, so "generated nothing" is distinguishable from "ERPNext
  was unreachable".
- `get_reconciliation_status` and `list_unreconciled_statements` report
  `anchor_source` (`erpnext` | `local` | `mixed`); `list_rules` reports
  `rule_source` and each rule's `erpnext_rule_name`; `get_account_topology`
  reports each account's mapped `erpnext_bank_account` and last metadata push.
- `run_sync` returns an `erpnext_push` block — what the metadata mirror did and
  what the queue drain did.
- `rebuild_statement_anchors` returns an `erpnext` block alongside its counts.

### Migration

Additive and automatic. The boot migration adds
`categorization_rules.erpnext_rule_name`,
`statement_anchors.erpnext_push_fingerprint` / `erpnext_pushed_at`,
`plaid_accounts.erpnext_metadata_fingerprint` / `erpnext_metadata_pushed_at`,
and creates `erpnext_push_queue`. All five columns backfill to NULL, which
correctly means "never pushed to (or fetched from) ERPNext".

Before the `erpnext_mcp` app is deployed on the ERPNext side, every push 404s
and queues and every read falls back locally — the pre-v1.0.0 behaviour plus a
growing queue. When it lands, `flush_erpnext_push_queue` (or the next sync)
replays the whole history.

### Not in this release

Removal of the deprecated tools, and deletion of the internal tables. Both wait
on 30 days of parallel operation and an operator's sign-off, per the plan's §9.
