<!-- SPDX-License-Identifier: MIT -->
# Plaid Statements — operator guide (v1.0.10)

Bank Bridge pulls each linked bank's monthly statement PDFs from Plaid. It
parses and reconciles them against the transaction feed, then attaches each
PDF to the matching ERPNext **Statement Anchor** (its `statement_pdf` field).
Plaid Statements replaces uploading statements by hand wherever the bank
supports it.

## Switching it on

1. **Plaid Dashboard.** Make sure the *Statements* product is enabled for the
   application (Dashboard → Products, or ask Plaid). Statements is billed per
   Item when the Item is created or consents, and per statement extracted when
   refreshed.
2. **Deploy** v1.0.10 or later.
3. **Turn it on** at `/admin/statements` → *Turn Plaid Statements on*.
   - It's off by default on a new install.
   - An install that already holds statements pulled from Plaid, like OML's
     Wells Fargo connection, is switched on automatically at upgrade.
4. **Existing connections:** on `/admin/accounts`, press *Enable statements
   for this connection* on each bank linked before. This opens Plaid Link in
   update mode for that same connection.
   - It shows only the Statements consent pane, and the credentials pane too
     if the link is more than two years old.
   - Item and account ids don't change, so no mapping is lost.
   - A statement pull starts as soon as consent completes.
5. **New connections** request Statements automatically, as "required if
   supported": a bank that doesn't offer statements still links.

## What runs on its own

- **The monthly job** asks Plaid to refresh each connection's statements and
  waits for the webhook (or polls). It downloads new PDFs (one per account and
  period, never twice), parses them, matches them to transactions, rebuilds
  anchors, and attaches the PDFs to their ERPNext Statement Anchors.
- **On demand:** the *Pull statements* button on `/admin/statements`, or the
  `pull_statements` MCP tool (kill switch, off by default).

## Checking coverage

`get_statement_coverage` (MCP, read-only) reports, per account:

| status | meaning |
|---|---|
| `supported` | Plaid delivers this account's statements |
| `manual_upload_required` | the bank lists none for this account; keep uploading by hand |
| `not_consented` | the connection lacks Statements consent; press *Enable statements* |
| `unknown` | never pulled yet |
| `off` | Plaid Statements are switched off |

It also reports the first and last statement held and every missing month.
`get_erpnext_push_status` → `statement_pdfs` counts the ERPNext anchors that
carry their PDF and lists the ones that don't.

Plaid documents Statements as depository-only. In practice Wells Fargo has
also delivered its Advisors brokerage statements (••6030, ••9401), so trust the
coverage report over the documentation.

## What is never logged

Access tokens and PDF bytes never reach a log line. Errors log Plaid's error
text only.
