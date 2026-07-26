# SPDX-License-Identifier: MIT
"""v0.7.1 — the Tailscale sidecar that makes the public URL one click.

WHY A SIDECAR AT ALL. v0.7.0 told the operator to run `tailscale funnel` on the
Umbrel host. On a box using Umbrel's Tailscale community app that cannot work:

    error: failed apply web serve: only localhost or 127.0.0.1 proxies are
    currently supported

Funnel will only proxy to its OWN localhost, and Umbrel's Tailscale app runs
Tailscale in its own container — whose localhost is not Bank Bridge. There is no
flag that fixes this; the daemon has to be in Bank Bridge's network namespace.
So v0.7.1 ships Tailscale in Bank Bridge's own compose with
`network_mode: "service:server"`. The sidecar's 127.0.0.1 IS Bank Bridge, and
`Proxy: http://127.0.0.1:5202` resolves to our own gunicorn.

THREE CHANNELS, DELIBERATELY CHOSEN. Talking to the sidecar looks like one
problem and is really three, with different stability guarantees:

  1. WRITES — enabling and disabling Funnel — go through the **serve config
     file** named by `TS_SERVE_CONFIG`. containerboot watches that file and
     re-applies it on change, so writing JSON is a complete, supported API. The
     alternative (POST the LocalAPI `serve-config` endpoint) is worse in a
     specific way: it is runtime state, so the next sidecar restart re-applies
     the FILE and silently reverts us. Declarative wins.

  2. LIVENESS + AUTH go through the **`/healthz` endpoint** on
     `TS_LOCAL_ADDR_PORT`, which shares our localhost. It is documented and
     precise: 200 when the node holds at least one tailnet IP, 503 when it does
     not, connection-refused when no sidecar is there at all. That is exactly
     the present / authenticated / absent split the wizard needs, and it needs
     no socket and no token.

  3. THE TAILNET FQDN comes from the **LocalAPI** over a shared unix socket,
     because nothing else knows it — `${TS_CERT_DOMAIN}` is substituted inside
     tailscaled, which does not tell us. Tailscale state this API is not yet
     documented, so every read here is BEST-EFFORT: a failure degrades to the
     v0.7.0 env-var / paste-it-in path and never breaks a page or a save.

Note this reverses v0.7.0's "no socket bind-mount" decision, and only because
what changed is whose socket it is. v0.7.0 refused to mount the *host's*
tailscaled socket: that needs container privileges and the path and permissions
vary per Umbrel install, so it would fail differently on every box. This socket
belongs to a sidecar declared in our own compose, on a volume we define. Same
mechanism, entirely different blast radius.
"""
import http.client
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request

from flask import current_app

log = logging.getLogger('bankbridge.tailscale')

#: How long a status read is reused. The wizard re-reads on every page load and
#: on every button press; without this, one operator clicking around would
#: hammer /healthz and the LocalAPI. Short enough that "Enable" → "it's on"
#: still feels immediate, because those paths bypass the cache explicitly.
CACHE_TTL_SECONDS = 30.0

#: Bounded hard. Both endpoints are on our own localhost or a unix socket, so a
#: healthy answer is sub-millisecond; anything slow means "not there".
TIMEOUT_SECONDS = 2.0

#: The LocalAPI's canonical Host value for unix-socket requests.
_LOCALAPI_HOST = 'local-tailscaled.sock'

_cache: dict = {'at': 0.0, 'value': None}


def reset_cache() -> None:
    """Drop the cached status. Called by the enable/disable paths so the very
    next read reflects what we just wrote, and by tests between cases."""
    _cache['at'] = 0.0
    _cache['value'] = None


# ── configuration ────────────────────────────────────────────────────────────

def _cfg(key: str, default: str) -> str:
    try:
        return (current_app.config.get(key) or default)
    except RuntimeError:  # outside an app context
        return os.environ.get(key) or default


def is_enabled() -> bool:
    """Whether to look for a sidecar at all. False makes every function here
    report 'absent' without touching the network — the escape hatch for an
    install that fronts Bank Bridge some other way and doesn't want the probe."""
    try:
        return bool(current_app.config.get('TAILSCALE_SIDECAR_ENABLED', True))
    except RuntimeError:
        return True


def health_addr() -> str:
    return _cfg('TAILSCALE_LOCAL_ADDR_PORT', '127.0.0.1:41414')


def socket_path() -> str:
    return _cfg('TAILSCALE_SOCKET', '/var/run/tailscale/tailscaled.sock')


def serve_config_path() -> str:
    return _cfg('TAILSCALE_SERVE_CONFIG', '/config/serve.json')


# ── channel 2: liveness + authentication via /healthz ────────────────────────

def _health() -> tuple:
    """(present, authenticated).

    200 → the node holds a tailnet IP, so it is up AND logged in.
    503 → containerboot is running but has no tailnet IP: almost always a
          missing or rejected TS_AUTHKEY, which is the state a fresh install
          sits in and the one the wizard has to explain.
    refused/timeout → no sidecar in this namespace."""
    url = f'http://{health_addr()}/healthz'
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as resp:
            code = getattr(resp, 'status', None) or resp.getcode()
        return True, code == 200
    except urllib.error.HTTPError as exc:
        # It answered, so the sidecar exists — it just isn't authenticated.
        return True, exc.code == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False, False


# ── channel 3: the FQDN via LocalAPI (best-effort) ───────────────────────────

class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over an AF_UNIX socket. Stdlib only — urllib has no unix
    transport and the LocalAPI has no TCP listener on Linux."""

    def __init__(self, path: str, timeout: float):
        super().__init__(_LOCALAPI_HOST, timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def _localapi_get(path: str):
    """GET one LocalAPI path, decoded from JSON. None on any failure.

    Never raises and never logs above debug: this is enrichment. The wizard is
    fully usable when it returns None — it just asks for the hostname instead of
    knowing it."""
    conn = None
    try:
        conn = _UnixHTTPConnection(socket_path(), TIMEOUT_SECONDS)
        # `Sec-Tailscale` is required by some LocalAPI builds as an anti-CSRF
        # measure and ignored by the rest, so send it unconditionally.
        conn.request('GET', path, headers={
            'Host': _LOCALAPI_HOST, 'Sec-Tailscale': 'localapi'})
        resp = conn.getresponse()
        if resp.status != 200:
            log.debug('localapi %s → HTTP %s', path, resp.status)
            return None
        return json.loads(resp.read().decode('utf-8', 'replace'))
    except (OSError, ValueError, http.client.HTTPException) as exc:
        log.debug('localapi %s unavailable: %s', path, exc)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:  # pragma: no cover - defensive
                pass


def _fqdn_from_status(status: dict) -> str:
    """This node's certificate FQDN, from a LocalAPI status payload.

    `CertDomains` is the right field — it is documented as the names the control
    plane will provision TLS for, without a trailing dot, which is precisely
    what `${TS_CERT_DOMAIN}` resolves to and therefore what the Funnel serves.
    `Self.DNSName` is the fallback and needs its trailing dot stripped."""
    domains = status.get('CertDomains') or []
    if isinstance(domains, list):
        for d in domains:
            if isinstance(d, str) and d.strip():
                return d.strip().rstrip('.').lower()
    self_node = status.get('Self') or {}
    if isinstance(self_node, dict):
        dns = (self_node.get('DNSName') or '').strip()
        if dns:
            return dns.rstrip('.').lower()
    return ''


# ── channel 1: the serve config file ─────────────────────────────────────────

def build_serve_config(port: int, path: str, funnel: bool) -> dict:
    """The `ipn.ServeConfig` that publishes exactly `path` and nothing else.

    `${TS_CERT_DOMAIN}` is substituted by containerboot with this node's FQDN,
    which keeps the file portable — the same JSON is correct on any tailnet, so
    an operator can move the data volume without editing it.

    Path-scoped on purpose, matching the v0.7.0 guidance: a `/` handler would
    publish /admin and Bank Bridge's four unauthenticated Plaid write endpoints
    to the Internet. `AllowFunnel` is what makes it public at all; with it false
    the same config is a tailnet-only Serve."""
    host_key = '${TS_CERT_DOMAIN}:443'
    return {
        'TCP': {'443': {'HTTPS': True}},
        'Web': {host_key: {'Handlers': {
            path: {'Proxy': f'http://127.0.0.1:{port}'}}}},
        'AllowFunnel': {host_key: bool(funnel)},
    }


def read_serve_config():
    """The serve config we last wrote, or None if there is none/it is corrupt."""
    try:
        with open(serve_config_path(), encoding='utf-8') as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def _funnel_requested(cfg) -> bool:
    """Whether a serve config asks for Funnel on any host key."""
    if not isinstance(cfg, dict):
        return False
    allow = cfg.get('AllowFunnel')
    if not isinstance(allow, dict):
        return False
    return any(bool(v) for v in allow.values())


def write_serve_config(port: int, path: str, funnel: bool) -> dict:
    """Write the serve config atomically and return it.

    Atomic because containerboot WATCHES this file: a reader that catches a
    half-written file would apply a broken config or none. os.replace is atomic
    on the same filesystem, so the watcher only ever sees a complete document."""
    cfg = build_serve_config(port, path, funnel)
    target = serve_config_path()
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = target + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
    os.replace(tmp, target)
    reset_cache()
    log.info('tailscale serve config written: funnel=%s path=%s → 127.0.0.1:%s',
             funnel, path, port)
    return cfg


# ── the composed status ──────────────────────────────────────────────────────

def _read_status() -> dict:
    present, authenticated = _health()
    cfg = read_serve_config()
    out = {
        'enabled': True,
        'present': present,
        'authenticated': authenticated,
        'hostname': '',
        'backend_state': '',
        'auth_url': '',
        'localapi_ok': False,
        'funnel_active': _funnel_requested(cfg),
        'serve_config_present': cfg is not None,
    }
    if not present:
        return out
    # Enrichment, attempted whenever a sidecar answered at all — including the
    # UNAUTHENTICATED case, which is not obvious and is the whole reason this is
    # not gated on `authenticated`. A tailscaled with no key reports
    # BackendState=NeedsLogin and an `AuthURL`: a one-click browser login the
    # operator can use instead of minting a TS_AUTHKEY. Verified against
    # tailscale/tailscale:v1.90.8 with an empty key. It has no FQDN yet
    # (CertDomains is null, Self.DNSName is ""), which _fqdn_from_status already
    # reads as ''.
    status = _localapi_get('/localapi/v0/status')
    if isinstance(status, dict):
        out['localapi_ok'] = True
        out['hostname'] = _fqdn_from_status(status)
        out['backend_state'] = (status.get('BackendState') or '').strip()
        out['auth_url'] = (status.get('AuthURL') or '').strip()
    return out


def status(force: bool = False) -> dict:
    """Current sidecar state, cached for CACHE_TTL_SECONDS.

    Keys: present, authenticated, hostname, funnel_active, localapi_ok,
    backend_state, serve_config_present, enabled."""
    if not is_enabled():
        return {'enabled': False, 'present': False, 'authenticated': False,
                'hostname': '', 'backend_state': '', 'auth_url': '',
                'localapi_ok': False, 'funnel_active': False,
                'serve_config_present': False}
    now = time.monotonic()
    if not force and _cache['value'] is not None and \
            (now - _cache['at']) < CACHE_TTL_SECONDS:
        return dict(_cache['value'])
    value = _read_status()
    _cache['at'] = now
    _cache['value'] = value
    return dict(value)


# ── the two actions the wizard and MCP both drive ────────────────────────────

def enable_funnel(port: int, path: str) -> dict:
    """Publish `path` over Funnel. Returns the post-write status (uncached).

    Idempotent — writing the same config twice is the same bytes, and
    containerboot re-applying an unchanged config is a no-op."""
    write_serve_config(port, path, funnel=True)
    return status(force=True)


def disable_funnel(port: int, path: str) -> dict:
    """Stop publishing publicly. Writes the SAME handler with AllowFunnel off
    rather than deleting the file: that leaves a valid tailnet-only Serve, so
    the app stays reachable over the tailnet and re-enabling is one flag."""
    write_serve_config(port, path, funnel=False)
    return status(force=True)
