# SPDX-License-Identifier: MIT
"""v0.4.8 — the `/bankbridge/` path prefix and its backward-compat shims.

Every Plaid-facing route moved under `/bankbridge/` so Bank Bridge can share one
Tailscale Funnel hostname with other Umbrel apps without path collisions (see
"Multi-app path prefix convention" in the README). Two things have to survive
that move:

  * URLs already registered in an operator's Plaid dashboard, and
  * URLs already persisted in {DATA_DIR}/plaid_settings.json.

The first is handled by `install_legacy_redirects` (permanent redirects, logged
at INFO so we can see who is still on the old path). The second by `migrate_url`,
which app/plaid_settings.py applies on every read.

Redirect status codes: GET callbacks get 301; the POST endpoints get **308**,
which is 301's method-preserving twin. A 301 on a POST lets the client downgrade
to GET and drop the body, which would silently break `exchange_token` rather
than redirect it."""
import logging
from urllib.parse import urlsplit, urlunsplit

from flask import redirect, request

log = logging.getLogger('bankbridge.legacy_paths')

#: Public path prefix owned by this app.
PREFIX = '/bankbridge'

#: Old path → new path, for every Plaid-facing route. `/plaid/webhook` never
#: existed as a route (the receiver has always lived at `/api/plaid/webhook`),
#: but it is a plausible thing to have typed into a Plaid dashboard, so it maps
#: to the canonical webhook path too.
LEGACY_PATH_MAP = {
    '/plaid/oauth_return': '/bankbridge/plaid/oauth_return',
    '/plaid/webhook': '/bankbridge/api/plaid/webhook',
    '/api/plaid/webhook': '/bankbridge/api/plaid/webhook',
    '/api/plaid/create_link_token': '/bankbridge/api/plaid/create_link_token',
    '/api/plaid/exchange_token': '/bankbridge/api/plaid/exchange_token',
    '/api/plaid/set_link_company': '/bankbridge/api/plaid/set_link_company',
}

#: Paths reached with GET — a body-less request, so a classic 301 is safe.
_GET_PATHS = frozenset({'/plaid/oauth_return'})


def redirect_status(old_path: str) -> int:
    """301 for GET callbacks, 308 (method-preserving) for POST endpoints."""
    return 301 if old_path in _GET_PATHS else 308


def new_path_for(path: str) -> str | None:
    """The `/bankbridge/`-prefixed path for a legacy path, or None if `path` is
    not a legacy Plaid path. Trailing slashes are tolerated."""
    return LEGACY_PATH_MAP.get(path.rstrip('/') or path)


def migrate_url(url: str) -> str:
    """Rewrite a stored Plaid URL onto the `/bankbridge/` prefix.

    Operates on the path component only, so scheme/host/port/query survive:
    `https://umbrel.tail2b0bb0.ts.net/plaid/oauth_return` becomes
    `https://umbrel.tail2b0bb0.ts.net/bankbridge/plaid/oauth_return`. A URL that
    is already migrated (or is not a Plaid path at all) is returned unchanged,
    so this is idempotent and safe to run on every read."""
    if not url:
        return url
    parts = urlsplit(url)
    new_path = new_path_for(parts.path)
    if new_path is None:
        return url
    return urlunsplit(parts._replace(path=new_path))


def install_legacy_redirects(app) -> None:
    """Wire the pre-v0.4.8 Plaid paths to their `/bankbridge/` equivalents.

    A before_request hook rather than routes on the blueprint: it keeps the
    legacy surface in one auditable place and cannot be confused for a real
    endpoint. Query strings ride along untouched — Plaid appends `oauth_state_id`
    to the OAuth return and dropping it would strand the handoff."""

    @app.before_request
    def _redirect_legacy_plaid_path():  # pragma: no cover - covered via routes
        new_path = new_path_for(request.path)
        if new_path is None:
            return None
        target = new_path
        if request.query_string:
            target += '?' + request.query_string.decode('utf-8', 'replace')
        status = redirect_status(request.path.rstrip('/') or request.path)
        log.info('legacy Plaid path %s %s → %s (v0.4.8 /bankbridge/ migration; '
                 'update your Plaid dashboard and Tailscale funnel)',
                 request.method, request.path, new_path)
        return redirect(target, code=status)
