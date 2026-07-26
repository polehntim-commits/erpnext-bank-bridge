# SPDX-License-Identifier: MIT
"""v0.7.0 — public-URL detection for the Plaid OAuth callback.

Plaid OAuth banks redirect the operator's browser to an `https://` URL that must
be reachable from the public Internet. Umbrel is LAN-only, so something has to
put Bank Bridge on a public name; Tailscale Funnel is the recommended way (see
docs/tailscale-funnel.md). Everything this module does exists to get the
operator from "I have no public URL" to "this exact string is registered in my
Plaid dashboard" without an SSH session.

WHY DETECTION IS INDIRECT. Bank Bridge runs in a container with no access to
the host's `tailscaled` socket, so it cannot ask Tailscale anything. Bind-mounting
`/var/run/tailscale/tailscaled.sock` was considered and REJECTED for v0.7.0: it
adds container privileges, and the socket path/permissions vary across Umbrel
installs, so it would fail differently on every box. Instead we take the
hostname from one of two places, in this order:

  1. `TAILSCALE_FUNNEL_HOSTNAME` in the environment (an Umbrel app override) —
     the ops-layer answer, set once by whoever configured the Funnel.
  2. `funnel_hostname` persisted in plaid_settings.json — what the operator
     pasted into the Manual Entry field on /admin/plaid_settings.

Env wins because it tracks the machine's actual Funnel config; a stale saved
value must not shadow it. But the saved value is NEVER silently discarded — when
the two disagree, detect() reports both and the admin page shows the conflict, so
a renamed tailnet reads as a visible mismatch rather than a redirect URI that
mysteriously stopped matching.

WHAT WE NEVER DO IS TRUST A HOSTNAME AS TYPED. Everything is normalized through
normalize_hostname() and re-validated on read, so the only URL this module can
ever construct — or probe — is `https://<validated-fqdn>[:port]/bankbridge/...`.
The operator cannot inject a scheme, a path, or a query, which keeps the
reachability probe from being a general-purpose fetcher on an unauthenticated
LAN page."""
import logging
import os
import re
import urllib.error
import urllib.request

from flask import current_app

from . import legacy_paths
from . import plaid_settings

log = logging.getLogger('bankbridge.funnel')

#: The container port Umbrel's app_proxy forwards to, and therefore the port a
#: Funnel has to be pointed at. Kept here (rather than inlined in the docs
#: strings) so the wizard's copy-paste commands can never drift from reality.
APP_PROXY_PORT = 5202

#: The one path that has to be publicly reachable. Derived from legacy_paths so
#: a future prefix change moves both at once.
OAUTH_RETURN_PATH = legacy_paths.PREFIX + '/plaid/oauth_return'

#: Optional env var naming the machine's Funnel hostname, e.g.
#: `umbrel.tail1234.ts.net`. Blank/unset → fall back to the persisted value.
ENV_VAR = 'TAILSCALE_FUNNEL_HOSTNAME'

#: Seconds to wait on the reachability probe. Short on purpose: the probe is a
#: nice-to-have diagnostic on a page the operator is staring at, and a Funnel
#: that is genuinely unreachable should say so in a few seconds, not thirty.
PROBE_TIMEOUT = 5.0

_LABEL_RE = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$')


# ── hostname normalization ───────────────────────────────────────────────────

def normalize_hostname(raw: str | None) -> str:
    """A bare `host[:port]` from whatever the operator pasted, or '' if it isn't
    a usable public hostname.

    Accepts every shape `tailscale funnel status` or a browser address bar can
    produce, because all of them get pasted in practice:

        umbrel.tail1234.ts.net
        https://umbrel.tail1234.ts.net
        https://umbrel.tail1234.ts.net/
        https://umbrel.tail1234.ts.net/bankbridge/plaid/oauth_return
        HTTPS://Umbrel.Tail1234.TS.NET:443/

    …all normalize to `umbrel.tail1234.ts.net`. The `:443` is dropped because we
    only ever emit `https://`, and a redirect URI has to match the Plaid
    dashboard byte-for-byte — `host` and `host:443` are the same endpoint but two
    different strings, and only one of them will be registered.

    A single-label name (`umbrel`, `localhost`) is rejected: it cannot resolve
    on the public Internet, so accepting it would only produce a redirect URI
    that fails at the bank."""
    s = (raw or '').strip()
    if not s:
        return ''
    # Fragment and query first — a pasted URL may carry either, and neither is
    # part of the host.
    s = s.split('#', 1)[0].split('?', 1)[0]
    if '://' in s:
        s = s.split('://', 1)[1]
    # userinfo is never part of a Funnel URL; strip it rather than carry
    # credentials into a string we render on a page.
    if '@' in s:
        s = s.rsplit('@', 1)[1]
    s = s.split('/', 1)[0].strip().rstrip('.').lower()
    if s.endswith(':443'):
        s = s[:-len(':443')]
    return s if is_valid_hostname(s) else ''


def is_valid_hostname(host: str) -> bool:
    """Whether `host` is a plausible public `host[:port]`. Deliberately not
    Tailscale-specific — an operator fronting Bank Bridge with Cloudflare Tunnel
    or their own domain gets the same wizard."""
    if not host or len(host) > 253 + 6:
        return False
    if ':' in host:
        host, _, port = host.partition(':')
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            return False
    if not host or len(host) > 253:
        return False
    labels = host.split('.')
    if len(labels) < 2:
        return False
    return all(_LABEL_RE.match(label) for label in labels)


def base_url(hostname: str) -> str:
    """`https://<hostname>` — the public origin, no trailing slash. '' for a
    hostname that doesn't validate, so a caller can't build a half-URL."""
    h = normalize_hostname(hostname)
    return f'https://{h}' if h else ''


def redirect_uri_for(hostname: str) -> str:
    """The full string that must be registered in the Plaid dashboard AND saved
    as PLAID_REDIRECT_URI. '' when the hostname doesn't validate."""
    base = base_url(hostname)
    return base + OAUTH_RETURN_PATH if base else ''


# ── detection ────────────────────────────────────────────────────────────────

def env_hostname() -> str:
    """The hostname from `TAILSCALE_FUNNEL_HOSTNAME`, normalized.

    Reads app config first so a test (or a future admin override) can set it
    without touching the process environment, then falls back to os.environ for
    the ordinary container case."""
    val = ''
    try:
        val = current_app.config.get(ENV_VAR) or ''
    except RuntimeError:  # no app context — fall through to the environment
        pass
    if not val:
        val = os.environ.get(ENV_VAR) or ''
    return normalize_hostname(val)


def saved_hostname() -> str:
    """The hostname the operator pasted into Manual Entry, normalized on read so
    a hand-edited settings file can't feed junk into a URL."""
    return normalize_hostname(plaid_settings.load().get('funnel_hostname'))


def detect(probe_url: bool = False) -> dict:
    """Everything /admin/plaid_settings needs to render the public-URL section.

    `state` is 'configured' when we have a hostname from either source (the
    spec's State A) and 'unconfigured' otherwise (State B). Set `probe_url` to
    also HEAD the derived callback — off by default so an ordinary page load
    never waits on the network, on when the operator clicks Refresh status."""
    env = env_hostname()
    saved = saved_hostname()
    hostname = env or saved
    source = 'env' if env else ('saved' if saved else '')
    current = (plaid_settings.load().get('redirect_uri') or '').strip()
    derived = redirect_uri_for(hostname)
    out = {
        'state': 'configured' if hostname else 'unconfigured',
        'hostname': hostname,
        'source': source,
        'env_hostname': env,
        'saved_hostname': saved,
        # Both sources set and disagreeing: env wins, but the operator is told,
        # because the usual cause is a renamed tailnet and a stale saved value.
        'conflict': bool(env and saved and env != saved),
        'base_url': base_url(hostname),
        'redirect_uri': derived,
        'current_redirect_uri': current,
        'redirect_uri_matches': bool(derived) and current == derived,
        'oauth_return_path': OAUTH_RETURN_PATH,
        'app_proxy_port': APP_PROXY_PORT,
        'env_var': ENV_VAR,
        'probe': None,
    }
    if probe_url and derived:
        out['probe'] = probe(hostname)
    return out


# ── reachability probe ───────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface a 3xx instead of following it.

    A followed redirect would report 200 for a URL that Plaid will reject:
    Plaid compares the registered redirect URI against the one Link was given,
    so a callback that only works after a hop is a misconfiguration the operator
    needs to see, not something to paper over."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(hostname: str, timeout: float | None = None) -> dict:
    """HEAD the derived callback URL and describe what came back.

    Advisory only — never a gate on saving. A Funnel is reachable from the
    public Internet, and this request originates inside a container that may
    already be on the tailnet, where the same name can resolve to the private
    address (or not resolve at all). "Unreachable from here" therefore does NOT
    mean "unreachable from Plaid", and the UI says so."""
    url = redirect_uri_for(hostname)
    if not url:
        return {'ok': False, 'reachable': False, 'status': None, 'url': '',
                'location': '',
                'detail': 'No valid hostname to test.'}
    timeout = PROBE_TIMEOUT if timeout is None else timeout
    req = urllib.request.Request(url, method='HEAD')
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, 'status', None) or resp.getcode()
        return {
            'ok': status == 200, 'reachable': True, 'status': status,
            'url': url, 'location': '',
            'detail': f'HTTP {status} — the callback is reachable and answers.'
            if status == 200 else
            f'HTTP {status} — reachable, but the callback did not answer 200.'}
    except urllib.error.HTTPError as exc:
        status = exc.code
        location = (exc.headers.get('Location') or '') if exc.headers else ''
        if 300 <= status < 400:
            detail = (f'HTTP {status} redirect → {location or "(no Location)"} — '
                      'reachable, but Plaid needs the callback to answer '
                      'directly at the registered URI, not after a hop.')
        elif status == 404:
            detail = (f'HTTP 404 — the host answered but nothing is served at '
                      f'{OAUTH_RETURN_PATH}. Check the Funnel target port and '
                      f'--set-path.')
        else:
            detail = f'HTTP {status} — reachable, but returned an error.'
        return {'ok': False, 'reachable': True, 'status': status, 'url': url,
                'location': location, 'detail': detail}
    except urllib.error.URLError as exc:
        return {'ok': False, 'reachable': False, 'status': None, 'url': url,
                'location': '',
                'detail': f'Not reachable from this container: {exc.reason}. '
                          'That alone is not a failure — a Funnel is reached '
                          'from the public Internet, and a container already on '
                          'the tailnet often cannot resolve its own Funnel '
                          'name. Test from your phone on cellular to be sure.'}
    except (OSError, ValueError) as exc:  # pragma: no cover - defensive
        log.warning('funnel probe of %s failed', url, exc_info=True)
        return {'ok': False, 'reachable': False, 'status': None, 'url': url,
                'location': '', 'detail': f'Probe failed: {exc}'}
