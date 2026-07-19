<!-- SPDX-License-Identifier: MIT -->
# Privacy

ERPNext Bank Bridge is self-hosted software. You run it on your own hardware —
typically an [Umbrel](https://umbrel.com) home server — and it talks to your own
ERPNext and to Plaid on your behalf. There is no hosted service, no company
account, and no one else in the loop. This document explains, plainly, what data
the app touches and where that data lives.

## What the app collects

When you link a bank through Plaid, the app pulls and stores:

- **Transactions** — date, amount, description, merchant name, and the Plaid
  category for each transaction on the accounts you connect.
- **Account metadata** — account name, type/subtype, the last four digits of the
  account number (the "mask"), and the institution name.
- **Balances** — the current and available balance for each account, refreshed
  periodically. For investment accounts, only the balance is stored (no
  holdings, no investment transactions).
- **Bank statement PDFs** (v0.4.9, only if enabled) — the monthly statements
  your bank issued, fetched through Plaid and stored as files on your data
  volume. These are complete statement documents, so they may contain more than
  the app itself reads from them (it extracts only the opening and closing
  balances). They are stored **unencrypted**, the same as the rest of the local
  database, and are readable by anyone with access to the app's data volume or
  its admin interface. Set `STATEMENTS_ENABLED=false` to never fetch them.
- **A Plaid access token** — the credential Plaid issues so the app can fetch
  your data. It is stored **encrypted at rest** (Fernet symmetric encryption) and
  is never written to logs or shown in the UI.

That's the whole list. The app does not ask for, store, or transmit your online
banking password — Plaid handles the bank login, and the app never sees it.

## Where the data lives

Everything stays on **your** hardware. The app keeps its data in a Postgres
database and a small data directory on the machine you run it on. Nothing is sent
to the developer, to an analytics service, or to any third party other than
Plaid (which you are already using to connect your bank). There is **no
telemetry** — the app does not phone home, count installs, or report usage.

The one place your financial data is deliberately copied *to* is your own
**ERPNext** instance, because that is the entire point of the app: it creates
Bank Transaction records (and, if you enable it, Suppliers and Journal Entries)
in the ERPNext you configured. That ERPNext is also yours.

## Who has access

Only you. The admin interface is intended to run inside your home network (the
Umbrel trust boundary) and is unauthenticated by default on the LAN; an optional
HTTP Basic Auth layer is available if you expose it more widely. Access to the
data therefore comes down to access to your server and your ERPNext — both of
which you control. The developer has no access to your instance and no ability to
see your data.

## Third parties

The only third party involved is **Plaid**, and only because it is the bridge to
your bank. When you link an institution, Plaid authenticates you with your bank
and returns transaction and balance data to your self-hosted app. Plaid's own
handling of that data is governed by
[Plaid's privacy policy](https://plaid.com/legal/) and your agreement with your
bank — not by this project. The app sends Plaid only what a normal Plaid
integration requires (your access token and the sync cursor); it does not share
your data with Plaid beyond that.

## Retention

The app keeps your data for as long as you keep it. There is no automatic
expiry, no retention window, and no background deletion. Transactions and the
local mirror persist until **you** remove them.

## Your rights and controls

Because you host it, you are in full control:

- **Delete everything** by removing the Umbrel app (which removes its data
  volume) or by wiping/dropping the Postgres database.
- **Disconnect a bank** with the *Disconnect this bank* button on
  `/admin/accounts`. This calls Plaid's `/item/remove`, which invalidates the
  access token so Plaid stops pulling from that institution. Your existing
  transactions and the Journal Entries generated from them are **kept** — the
  disconnect stops the future feed, it does not erase history. You can also
  revoke the connection from your bank or from Plaid directly.
- **Delete stored statement PDFs** by removing the `statements/` directory in
  the app's data volume, or prevent them being fetched at all with
  `STATEMENTS_ENABLED=false`. Deleting the files does not affect any Journal
  Entry already booked from them.
- **Export or inspect** anything at any time — it is your database.

## Contact

This is a solo-maintained, open-source project. There is **no dedicated support
team** and no privacy office. Questions, concerns, or bug reports go through
GitHub issues:

- <https://github.com/polehntim-commits/erpnext-bank-bridge/issues>

Please do not include real account numbers, access tokens, or transaction data
in an issue.
