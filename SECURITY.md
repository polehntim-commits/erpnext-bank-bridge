# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for a
suspected vulnerability.

- Use GitHub's private vulnerability reporting on this repository:
  **Security → Report a vulnerability** (Security Advisories).
- We aim to acknowledge a report within 5 business days and to provide a fix or
  mitigation timeline after triage.

Responsible disclosure is appreciated: please give us a reasonable window to
release a fix before any public disclosure.

## Supported versions

This project is at an early (v0.1.0) stage. Security fixes are applied to the
latest release on `main`.

## Security posture

This app is designed to run **self-hosted on a trusted LAN** (behind Umbrel),
not on the public Internet.

**Credential handling**
- Bank credentials **never reach this app.** Plaid Link performs the bank
  authentication in the user's browser and returns a token; the app only ever
  holds Plaid tokens, never bank usernames/passwords.
- Plaid **access tokens are encrypted at rest** with
  [Fernet](https://cryptography.io/en/latest/fernet/) (AES-128-CBC + HMAC-SHA256).
  The symmetric key lives in `${DATA_DIR}/fernet.key` (file mode `0600`), on the
  app's private data volume — never in the database and never in source control.
- The least-privilege Plaid product scope is requested: **`transactions` only**
  (no `auth`, `identity`, `assets`, or balances-write scopes).

**Network**
- Calls to Plaid go to `api.plaid.com` over **HTTPS/TLS** (handled by the
  official `plaid-python` SDK).
- ERPNext is reached over the **local network** at an operator-configured URL
  (on Umbrel, the app-proxy address such as `http://umbrel.local:5300`); this
  traffic stays on the LAN and is never exposed to the Internet.
- The browser loads Plaid's Link SDK from `cdn.plaid.com` (required by Plaid).
- **No telemetry or analytics.** The only outbound connections are to Plaid and
  to your own ERPNext instance.

**Admin interface**
- The admin UI is **unauthenticated by design** and intended to be reachable
  only on a trusted LAN behind Umbrel's app proxy. **Do not expose it to the
  Internet.**

**Secrets in source control**
- `.env`, `fernet.key`, `plaid_settings.json`, and `erpnext_settings.json` are
  git-ignored and must never be committed. Only `.env.example` (placeholders)
  is tracked. The Fernet key has never been committed to this repository's
  history.

## Backups

`${DATA_DIR}/fernet.key` decrypts every stored Plaid access token. Back it up
securely alongside the database; losing it requires re-linking every bank.
