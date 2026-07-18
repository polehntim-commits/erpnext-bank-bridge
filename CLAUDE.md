# ERPNext Bank Bridge — working notes

Pulls bank transactions from Plaid into ERPNext as Bank Transaction records.
Flask app under `app/`, Postgres-only, deployed as an Umbrel community app.

## Design principles

**Multi-app path prefix convention.** Any Bank-Bridge-adjacent app hosted on the
same Umbrel that needs public Tailscale Funnel exposure MUST prefix its paths
with `/<app-name>/` (e.g. `/bankbridge/`, `/bucketlog/`, `/volumevision/`). This
prevents path collisions across apps sharing the same tailnet hostname and makes
ownership obvious in logs and audit trails.

A Funnel hostname belongs to a *machine*, not an app: every app on the box is
reached through the same `https://<host>.<tailnet>.ts.net` and separated only by
path. Claiming a generic prefix (`/api/`, `/plaid/`, `/webhook/`) makes the next
app's callback ambiguous and its log lines unattributable.

Scope: Plaid-facing (publicly reachable) paths. `/admin/*` and the internal
`/api/*` endpoints are LAN-only, never funnelled, and stay where they are.

**Database.** Maximal data science; avoid table sprawl.

**Forms.** Usability first — keep complex tasks simple for the end user.

## Conventions

- Every source file carries an `# SPDX-License-Identifier: MIT` header.
- Route changes that touch a public path need a backward-compat shim
  (`app/app/legacy_paths.py`) plus a migration section in the README — an
  operator's Plaid dashboard is configuration we cannot reach from here.
- Settings persisted under `DATA_DIR` migrate **on read**, idempotently, so a
  boot with a read-only or stale data volume still produces correct values.

## Tests

```bash
cd app
python3 -m unittest discover -s tests -v   # or: python3 -m pytest tests -q
```

Needs `DATABASE_URL` set (tests seed a dummy value) and Python 3.11 — the
`psycopg2` wheel has no build for newer interpreters yet.
