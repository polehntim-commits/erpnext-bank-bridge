# Public URL setup — Tailscale Funnel and the alternatives

**Applies to:** Bank Bridge v0.7.0 and later
**Wizard:** `/admin/plaid_settings` → *Plaid Redirect URI — Public URL Setup*

This is the deep-dive. If you just want to get it working: on **v0.7.1+** supply
one Tailscale auth key and click one button — see
[Sidecar mode](#0-sidecar-mode-v071-recommended). Everything after that section
is the manual path, the alternatives, and the reference material.

---

## 0. Sidecar mode (v0.7.1, recommended)

### Why this exists

v0.7.0 asked you to run `tailscale funnel` on the Umbrel host. On a box using
Umbrel's Tailscale community app, that cannot work:

```
error: failed apply web serve: only localhost or 127.0.0.1 proxies are
currently supported
```

Funnel will only proxy to **its own** localhost, and Umbrel's Tailscale app runs
Tailscale in its own container — whose localhost is not Bank Bridge. No flag
fixes this. The daemon has to be in Bank Bridge's network namespace.

So v0.7.1 ships Tailscale **inside Bank Bridge's own compose**, with
`network_mode: "service:server"`. The sidecar's `127.0.0.1:5202` is then our own
gunicorn, and Funnel proxies to it happily.

This is additive. If you already have a working public URL via
`TAILSCALE_FUNNEL_HOSTNAME` or a pasted hostname, or you front Bank Bridge with
Cloudflare Tunnel, **nothing changes for you** — those paths are untouched.

### Setup

**One value is required: a Tailscale auth key.**

1. Go to <https://login.tailscale.com/admin/settings/keys> → **Generate auth
   key**. Make it **reusable** — the sidecar re-authenticates whenever it
   restarts, and a single-use key would leave it stranded after the first
   restart.
2. Set it in your Umbrel app override for `fafo-bank-bridge`:

   ```yaml
   services:
     tailscale:
       environment:
         TS_AUTHKEY: "tskey-auth-…"
   ```
3. Restart the app.
4. Open `/admin/plaid_settings` → *Plaid Redirect URI — Public URL Setup* and
   click **Enable Public URL**.

That's it. The button writes the serve config, Tailscale provisions the HTTPS
certificate, and the resulting redirect URI is saved as your
`PLAID_REDIRECT_URI`.

5. Click **Copy Plaid dashboard URL** and paste that exact string into the Plaid
   dashboard under **Developers → API → Allowed redirect URIs**. Bank Bridge
   cannot do this step for you — it is on Plaid's side.

> **Alternative to the auth key.** If you'd rather not mint one, the wizard also
> surfaces a **one-time browser login link** when the sidecar is unauthenticated
> (the daemon reports it as `AuthURL`). Click it, approve the machine in
> Tailscale, come back and hit **Refresh status**. Convenient, but the link is
> single-use and changes on every sidecar restart — for something that survives
> restarts unattended, use the auth key.

### What the wizard shows you, state by state

| State | What you see | What to do |
| --- | --- | --- |
| **No sidecar** (`mode: none` / `manual`) | The v0.7.0 guided flow: numbered `tailscale funnel` commands and a Manual Entry field | Add the sidecar, or keep using the manual path |
| **Sidecar unauthenticated** (`sidecar_unauth`) | "Tailscale sidecar is running but needs authentication", the one-time login link if offered, and the `TS_AUTHKEY` steps | Supply a key or click the link, then Refresh status |
| **Sidecar ready** (`sidecar_ready`) | "authenticated", the tailnet hostname, and a single **Enable Public URL** button | Click it |
| **Funnel active** (`sidecar_funnel`) | The public URL, the exact redirect URI, whether it's already saved, and **Use this / Copy / Test URL / Refresh / Disable Funnel** | Copy the URI into your Plaid dashboard |

An unauthenticated sidecar is a **normal, harmless state** — it idles, `/healthz`
answers 503, and nothing else in Bank Bridge is affected. That is what makes
upgrading to v0.7.1 safe before you have supplied a key.

### How it works underneath

Three channels, each chosen for a different reason:

| Purpose | Mechanism | Why this one |
| --- | --- | --- |
| Enable / disable Funnel | Write the **serve config file** (`TS_SERVE_CONFIG`), which containerboot watches and re-applies | Declarative, so it survives a sidecar restart. A LocalAPI POST is runtime state and would be silently reverted on the next restart. |
| Is there a sidecar, is it logged in | **`/healthz`** on `TS_LOCAL_ADDR_PORT` (shared localhost) | Documented and precise: 200 = has a tailnet IP, 503 = running but not logged in, refused = absent. No socket, no token. |
| The tailnet FQDN | **LocalAPI** over a shared unix socket | Nothing else knows it — `${TS_CERT_DOMAIN}` is substituted inside tailscaled. Tailscale state this API is undocumented, so every read is best-effort. |

The serve config Bank Bridge writes:

```json
{
  "TCP": { "443": { "HTTPS": true } },
  "Web": {
    "${TS_CERT_DOMAIN}:443": {
      "Handlers": {
        "/bankbridge/plaid/oauth_return": { "Proxy": "http://127.0.0.1:5202" }
      }
    }
  },
  "AllowFunnel": { "${TS_CERT_DOMAIN}:443": true }
}
```

Note what it does **not** contain: a `/` handler. Only the OAuth callback is
published; `/admin` and the four unauthenticated Plaid write endpoints stay on
your LAN. `${TS_CERT_DOMAIN}` keeps the file portable across tailnets, so moving
the data volume doesn't require editing it.

**Disable Funnel** rewrites the same file with `AllowFunnel: false`. That leaves
a valid tailnet-only Serve, and deliberately leaves your saved
`PLAID_REDIRECT_URI` alone — it is still what your Plaid dashboard has
registered, so re-enabling needs no dashboard edit.

### AI / MCP control

Four MCP tools cover the same ground (see the README's MCP section):

- `get_public_url_status` — read-only; the full tri-state including `auth_url`
- `test_public_url` — read-only; HEAD the callback
- `enable_public_url` — **mutating**, kill switch defaults **OFF**
- `disable_public_url` — **mutating**, kill switch defaults **OFF**

The two mutating tools change what the *public Internet* can reach, so they are
the most consequential switches on `/admin/mcp`. Both are off until you turn them
on.

### Sidecar troubleshooting

**Wizard says "needs authentication" after I set `TS_AUTHKEY`.**
The key is read at container start — restart the app. If it persists, the key was
probably single-use and already consumed, or expired; generate a reusable one.

**The node appears in Tailscale as a duplicate on every restart.**
The `${APP_DATA_DIR}/tailscale/state` volume isn't persisting. Node identity
lives there; without it the sidecar re-registers as a new machine each boot.

**"Sidecar ready" but the hostname is blank.**
The LocalAPI read didn't land — usually the
`${APP_DATA_DIR}/tailscale/run:/var/run/tailscale` mount. Harmless: set
`TAILSCALE_FUNNEL_HOSTNAME` or paste the hostname in Manual Entry and everything
else works. (Note this mount cannot work on Docker Desktop for Mac, which does
not carry unix sockets across a bind mount. Umbrel is Linux, where it does.)

**Enable Public URL succeeded but reported no URL.**
Funnel really is on; the FQDN just hadn't surfaced yet. Click **Refresh status**.
Don't click Enable again — it's idempotent, but there's nothing to fix.

**Recreating the app breaks the sidecar.**
`network_mode: "service:server"` means the sidecar lives in the server's network
namespace, so recreating `server` destroys it. `restart: on-failure` brings the
sidecar back; if it stays down, restart the app.

**Funnel still refuses.** Funnel is a tailnet-wide ACL capability — see
[Troubleshooting](#6-troubleshooting) below. The sidecar cannot work around it.

---

## 1. Why a public URL is needed at all

Most banks Plaid supports use **OAuth**: instead of typing credentials into
Plaid Link, you are sent to your bank's own login page, and the bank sends your
browser back afterwards. "Sends your browser back" means an HTTP redirect to a
URL you registered in advance — the **redirect URI**.

Two constraints follow, and neither is negotiable:

- **It must be `https://`.** Plaid rejects `http://` redirect URIs for
  production OAuth.
- **It must be reachable from the public Internet.** The redirect is performed by
  the *bank's* servers and your browser, not by Bank Bridge. `http://umbrel.local:5202/…`
  is meaningless outside your LAN, and `192.168.x.x` is meaningless outside your
  house.

Umbrel is LAN-only by default, so something has to put one path of Bank Bridge on
a public name. That is the entire problem this page solves.

For Bank Bridge the redirect URI is always:

```
https://<your-public-host>/bankbridge/plaid/oauth_return
```

The `/bankbridge/` prefix is deliberate — see [Multi-app path prefix
convention](../README.md#multi-app-path-prefix-convention). A Funnel hostname
belongs to a *machine*, so every app on your Umbrel shares one
`https://<host>.<tailnet>.ts.net` and is separated only by path.

### Only one path needs to be public

This is the part worth being careful about. Bank Bridge's Plaid blueprint is
**unauthenticated by design** — the bank's redirect cannot carry your admin
credentials, and Umbrel's LAN is the assumed trust boundary. Exposing the whole
app therefore publishes four write endpoints to the Internet:

| Endpoint | What a stranger gets |
| --- | --- |
| `POST /bankbridge/api/plaid/create_link_token` | Burns billable Plaid API calls on your account |
| `POST /bankbridge/api/plaid/set_link_company` | Redirects which ERPNext Company a pending link books to |
| `POST /bankbridge/api/plaid/exchange_token` | Attempts to attach an Item they control to your install |
| `POST /bankbridge/api/plaid/webhook` | Spoofs "new transactions" events (no signature verification) forcing unscheduled syncs |

…plus `/admin/*`, which is your entire bookkeeping UI.

Only **`/bankbridge/plaid/oauth_return`** has to be public. Everything else is
called by your own browser on the LAN, so restricting the tunnel to that single
path costs you nothing. Every option below shows the path-restricted form.

> A Funnel URL is unguessable but **not secret** — it appears in TLS certificate
> transparency logs, and you hand it to every bank you link. Treat it as public
> knowledge and let the path restriction do the work.

---

## 2. Option comparison

| Option | Cost | You need | HTTPS cert | Path restriction | Survives reboot |
| --- | --- | --- | --- | --- | --- |
| **Sidecar mode** *(v0.7.1, recommended)* | Free | Tailscale account + an auth key | Automatic | Yes (serve config) | Yes |
| Tailscale Funnel on the host | Free | Tailscale account, and a host install Funnel can reach port 5202 from | Automatic | Yes (`--set-path`) | Yes (`--bg`) |
| Cloudflare Tunnel | Free | Cloudflare account + a domain on CF DNS | Automatic | Yes (`ingress` rules) | Yes (as a service) |
| ngrok | Free tier / paid | ngrok account | Automatic | Partial | Free tier: **no** — URL changes |
| Port-forward + DDNS | Free | Router access, DDNS provider, certbot | You manage it | Yes (nginx `location =`) | Yes |

**Recommendation: sidecar mode** ([§0](#0-sidecar-mode-v071-recommended)). No
port forwarding, no DNS, no certificate management, no inbound firewall hole, a
stable hostname, first-class path restriction — and it is the only Tailscale
option that works on an Umbrel whose Tailscale runs as a separate community app,
because Funnel only proxies to its own localhost.

**Host-level Tailscale Funnel** (§3) is the v0.7.0 path and still correct if
Tailscale is installed directly on the host *and* can reach port 5202 as its own
localhost. If you get `only localhost or 127.0.0.1 proxies are currently
supported`, you need sidecar mode instead.

**ngrok is a poor fit** for anything but a one-off test: on the free tier the
URL changes every restart, and a redirect URI that changes means re-registering
in the Plaid dashboard every time. If you do use it, expect to repeat step 5
below on each restart.

**Port-forward + DDNS** is the most work and the most exposure (a real open port
on your router) but is the right answer if you already run a public web server on
the box and want everything under your own domain. See
[Option C in the README](../README.md#option-c--nginx-reverse-proxy--lets-encrypt)
for the nginx config.

---

## 3. Host-level Tailscale Funnel, step by step

> This is the **manual** path (v0.7.0). It only works if Tailscale is installed
> on the host itself and Funnel can reach port 5202 as its own localhost. If
> Tailscale runs as a separate Umbrel app, use
> [sidecar mode](#0-sidecar-mode-v071-recommended) — this will fail with
> `only localhost or 127.0.0.1 proxies are currently supported`.

### Prerequisites

1. A [Tailscale](https://tailscale.com) account.
2. **Funnel enabled for your tailnet.** This is an ACL setting, not a per-device
   one: Tailscale admin console → **Access controls**, and the
   `funnel` node attribute must be granted. A fresh tailnet usually needs this
   added explicitly — see [Troubleshooting](#6-troubleshooting) if
   `tailscale funnel` reports it is not available.
3. The Umbrel host on your tailnet.

### Step 1 — install Tailscale on the Umbrel host

Either the **Umbrel community app store** (recommended — it survives Umbrel
updates and is managed alongside your other apps), or directly on the host:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

### Step 2 — join your tailnet

```bash
sudo tailscale up
```

This prints a URL; open it in a browser to authenticate the machine.

### Step 3 — serve only the OAuth callback over HTTPS

```bash
sudo tailscale funnel --bg --https=443 \
  --set-path=/bankbridge/plaid/oauth_return http://127.0.0.1:5202
```

- `--bg` runs it in the background **and persists it across reboots** — this is
  what makes the Funnel survive an Umbrel restart. Without it the Funnel dies
  with your SSH session.
- `--set-path=…` is the path restriction. Everything else 404s publicly.
- The target carries **no path**. Tailscale forwards the full request path to the
  backend rather than stripping the mount prefix, so adding `/bankbridge` to the
  target would produce a doubled `/bankbridge/bankbridge/…`.
- `5202` is Umbrel's app_proxy port for Bank Bridge. Confirm yours with
  `docker ps` if you changed it.

> **The quick-start alternative, and why the wizard doesn't lead with it.**
> `sudo tailscale funnel --bg 5202` is one short command and it does work — but
> it publishes the *whole* app, including `/admin` and the four unauthenticated
> write endpoints in §1. Use it to get unstuck if the `--set-path` form is
> giving you trouble, then narrow it. If you leave it broad, set
> `ADMIN_BASIC_AUTH_USER` and `ADMIN_BASIC_AUTH_PASS` at minimum.

To tear it down:

```bash
sudo tailscale funnel --https=443 --set-path=/bankbridge/plaid/oauth_return off
```

### Step 4 — read off your public hostname

```bash
sudo tailscale funnel status
```

You want the host part of the `https://…` line, e.g. `umbrel.tail1234.ts.net`.

### Step 5 — verify what it publishes *and what it refuses*

Do not skip this. The point of the configuration is what it says no to:

```bash
HOST=https://umbrel.tail1234.ts.net      # your own hostname

curl -sI $HOST/bankbridge/plaid/oauth_return           # Expect: 200
curl -sI $HOST/admin                                   # Expect: 404
curl -sI $HOST/bankbridge/api/plaid/create_link_token  # Expect: 404
curl -sI $HOST/bankbridge/api/plaid/webhook            # Expect: 404
```

If any of the last three answers anything but 404, the Funnel is wider than it
should be — re-check `--set-path` and re-run `tailscale funnel status`.

Run these from **off your tailnet** (phone on cellular is easiest). From a device
already on the tailnet the name can resolve to the private address instead, which
tells you nothing about public reachability.

### Step 6 — tell Bank Bridge and tell Plaid

In the Bank Bridge admin UI, open **`/admin/plaid_settings`** →
*Plaid Redirect URI — Public URL Setup*:

- If the wizard already shows your URL (see §4), click **Use this as Plaid
  Redirect URI**.
- Otherwise paste the hostname from step 4 into **Manual entry** — a bare
  hostname, an `https://` URL, or even the whole redirect URI all work — click
  **Test URL** if you want a reachability check, then **Save as Plaid Redirect
  URI**.

Then click **Copy Plaid dashboard URL** and paste that exact string into the
Plaid dashboard under **Developers → API → Allowed redirect URIs**.

**Plaid compares the redirect URI byte-for-byte.** `https://host` and
`https://host:443` are the same endpoint and two different strings; a trailing
slash matters too. Copy, don't retype — that is what the copy button is for.

---

## 4. Making the wizard auto-detect: `TAILSCALE_FUNNEL_HOSTNAME`

Bank Bridge runs in a Docker container with **no access to the host's
`tailscaled` socket**, so it cannot ask Tailscale what your Funnel hostname is.
Bind-mounting `/var/run/tailscale/tailscaled.sock` into the container was
considered and rejected for v0.7.0: it adds container privileges and the socket
path and permissions vary across Umbrel installs, so it would fail differently on
every box.

Instead, tell it once. Set `TAILSCALE_FUNNEL_HOSTNAME` to the bare hostname from
step 4 in your Umbrel app override:

```yaml
services:
  server:
    environment:
      TAILSCALE_FUNNEL_HOSTNAME: "umbrel.tail1234.ts.net"
```

On Umbrel this goes in the app's override file
(`~/umbrel/app-data/fafo-bank-bridge/docker-compose.override.yml` on most
installs — check your Umbrel version's path), then restart the app. The wizard
now shows **State A** on load: the detected URL, the exact redirect URI, and a
one-click save.

Value shapes are all accepted and normalized (`umbrel.tail1234.ts.net`,
`https://umbrel.tail1234.ts.net`, with or without a trailing slash or the full
callback path). A `:443` is dropped, because the redirect URI has to match the
Plaid dashboard string exactly.

### Precedence: env wins, and disagreement is shown

Two places can supply the hostname:

1. `TAILSCALE_FUNNEL_HOSTNAME` in the environment
2. the value you saved via **Manual entry**, persisted in
   `{DATA_DIR}/plaid_settings.json`

**The environment variable wins.** This inverts Bank Bridge's usual "env seeds
the default, the saved value wins" rule, on purpose: the env var describes the
machine's *current* Funnel, so a saved value from an older tailnet name must not
shadow it and quietly produce a redirect URI that no longer resolves.

The saved value is never silently discarded, though — when the two disagree the
wizard shows both and tells you which is in effect, so a renamed tailnet reads as
a visible mismatch rather than OAuth mysteriously breaking.

Clicking **Use this as Plaid Redirect URI** on an env-detected hostname does
*not* copy it into the settings file, so clearing the env var later doesn't leave
a phantom behind that looks locally configured.

---

## 5. What "Test URL" does, and what it can't tell you

**Test URL** makes a `HEAD` request to
`https://<hostname>/bankbridge/plaid/oauth_return` from inside the Bank Bridge
container and reports:

| Result | Meaning |
| --- | --- |
| `HTTP 200` | Reachable and answering. What you want. |
| `HTTP 3xx redirect` | Reachable, but Plaid needs the callback to answer *directly* at the registered URI, not after a hop. |
| `HTTP 404` | The host answered but nothing serves that path — check the Funnel target port and `--set-path`. |
| Not reachable | See below. |

**"Not reachable from this container" is not conclusive.** A Funnel is reached
from the public Internet, and this request originates inside a container that may
already be on the tailnet, where the same name can resolve to the private address
or not resolve at all. The probe is advisory and never blocks saving. The
authoritative test is step 5 above, run from a device off your tailnet.

The probe can only ever request `https://<validated-hostname>/bankbridge/plaid/oauth_return`
— you supply a hostname, not a URL, so the scheme, path and port are always Bank
Bridge's own.

---

## 6. Troubleshooting

**`tailscale funnel` says Funnel is not available / not enabled.**
Funnel is a tailnet-wide ACL capability, not a device setting. Tailscale admin
console → **Access controls** → grant the `funnel` node attribute to the node (or
to `tag:…`/`autogroup:member` as appropriate). The CLI usually prints a direct
link to the right page. Nothing on the Bank Bridge side can work around this.

**HTTPS certificate errors on the `ts.net` name.**
Tailscale provisions the certificate on first request, which can take a few
seconds. Also confirm MagicDNS and HTTPS certificates are enabled for the tailnet
(admin console → **DNS**).

**The Funnel disappears after a reboot.**
You omitted `--bg`. Re-run the step 3 command with it.

**Plaid rejects the redirect URI even though `curl` returns 200.**
Almost always a byte-level mismatch: `:443` present in one place and not the
other, a trailing slash, `http` vs `https`, or a stale entry still registered.
Use the **Copy Plaid dashboard URL** button and paste — don't retype.

**OAuth returns to the browser but the link never completes.**
The callback page runs JavaScript that POSTs to
`/bankbridge/api/plaid/exchange_token` **on your LAN**. That endpoint must *not*
be public, but your browser does have to reach it — so finish the Link flow on a
LAN device, not on cellular.

**The wizard still shows State B after setting the env var.**
The container reads it at startup: restart the app. If it still doesn't appear,
the value probably failed validation — a single-label name like `umbrel` or
`localhost` is refused because it cannot resolve publicly. Check `docker logs`
and use the Manual entry field as a cross-check.

**Upgrading from a pre-v0.4.8 install.**
Stored redirect URIs on the old `/plaid/oauth_return` path migrate to
`/bankbridge/plaid/oauth_return` automatically on read, and the old paths still
answer with a permanent redirect. You still need to update the Funnel
`--set-path` and the Plaid dashboard. See
[Path migration](../README.md#path-migration-v048).

---

## 7. See also

- [Production Deployment (HTTPS for Plaid OAuth)](../README.md#production-deployment-https-for-plaid-oauth)
  — full configs for Cloudflare Tunnel and nginx + Let's Encrypt
- [Multi-app path prefix convention](../README.md#multi-app-path-prefix-convention)
  — why `/bankbridge/`
- `app/funnel.py` — detection, normalization and the probe
