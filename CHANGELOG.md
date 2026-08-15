# Changelog

Notable changes, newest first. Releases before v1.0.0 are documented as
per-version sections in [README.md](README.md), which remains the reference for
how each feature works and why — this file records what changed and when.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses semantic versioning, where the major number tracks the
architecture rather than the API surface.

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
