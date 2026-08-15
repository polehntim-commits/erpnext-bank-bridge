# Changelog

Notable changes, newest first. Releases before v1.0.0 are documented as
per-version sections in [README.md](README.md), which remains the reference for
how each feature works and why — this file records what changed and when.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses semantic versioning, where the major number tracks the
architecture rather than the API surface.

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
