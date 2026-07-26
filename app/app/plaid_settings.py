# SPDX-License-Identifier: MIT
"""Plaid connection settings, persisted as a small JSON blob under DATA_DIR.

Same contract as app/erpnext_settings.py: the Config env vars (PLAID_*) seed
the defaults; the /admin/plaid_settings page writes {DATA_DIR}/plaid_settings.json
which — once written — WINS. This lets an operator paste their Plaid Client ID
+ Sandbox Secret + Production Secret and flip sandbox→production without a
redeploy.

Plaid uses ONE secret per environment, so we store both and hand the active
one to the client based on `environment`. The secrets are credentials; the
admin page renders only masked previews and never echoes the full values."""
import json
import logging
import os

from flask import current_app

from . import legacy_paths
from . import sync_config

log = logging.getLogger('bankbridge.plaid_settings')

#: Fields whose v0.4.8 path migration has already been announced this process.
_LOGGED_URL_MIGRATIONS: set[str] = set()

_FILENAME = 'plaid_settings.json'
# `sync_interval_hours` rides along in this same JSON blob (the sync-frequency
# picker lives on the Plaid settings page). It's the background poll cadence in
# hours; 0 = manual only. See app/sync_config.py.
#
# v0.7.0 — `funnel_hostname` too: the public hostname an operator pasted into the
# Manual Entry field of the public-URL wizard (see app/funnel.py). It lives here
# rather than in a file of its own because it is a Plaid-connection setting —
# the redirect URI is derived from it — and one settings blob beats two.
_FIELDS = ('client_id', 'sandbox_secret', 'production_secret',
           'environment', 'redirect_uri', 'webhook_url', 'sync_interval_hours',
           'funnel_hostname')


def _path() -> str:
    return os.path.join(current_app.config['DATA_DIR'], _FILENAME)


def _defaults() -> dict:
    c = current_app.config
    # The single PLAID_SECRET env seeds whichever environment is active, so a
    # headless deploy needs only PLAID_SECRET + PLAID_ENV.
    env = (c.get('PLAID_ENV') or 'sandbox').strip().lower()
    seed_secret = (c.get('PLAID_SECRET') or '').strip()
    return {
        'client_id': (c.get('PLAID_CLIENT_ID') or '').strip(),
        'sandbox_secret': seed_secret if env == 'sandbox' else '',
        'production_secret': seed_secret if env == 'production' else '',
        'environment': env if env in ('sandbox', 'production') else 'sandbox',
        'redirect_uri': (c.get('PLAID_REDIRECT_URI') or '').strip(),
        'webhook_url': (c.get('PLAID_WEBHOOK_URL') or '').strip(),
        'sync_interval_hours': sync_config.normalize_interval(
            c.get('SYNC_INTERVAL_HOURS', 24)),
        # NOT seeded from TAILSCALE_FUNNEL_HOSTNAME on purpose. Every other
        # field here follows "env seeds, persisted wins" — but for the Funnel
        # hostname that ordering is backwards: the env var describes the
        # machine's CURRENT Funnel config, so a persisted value from an older
        # tailnet name would silently shadow it and produce a redirect URI that
        # no longer resolves. funnel.detect() reads the env separately and
        # prefers it, keeping this field purely "what the operator pasted".
        'funnel_hostname': '',
    }


def load() -> dict:
    """Current settings — env defaults overlaid with persisted JSON. Always
    returns every key in _FIELDS."""
    d = _defaults()
    try:
        with open(_path(), encoding='utf-8') as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k in _FIELDS:
                if k in saved and saved[k] is not None:
                    d[k] = saved[k]
    except (FileNotFoundError, ValueError, OSError):
        pass
    if d.get('environment') not in ('sandbox', 'production'):
        d['environment'] = 'sandbox'
    d['sync_interval_hours'] = sync_config.normalize_interval(
        d.get('sync_interval_hours'))
    _migrate_plaid_urls(d)
    return d


def _migrate_plaid_urls(d: dict) -> None:
    """v0.4.8 — rewrite pre-prefix Plaid URLs onto `/bankbridge/`, in place.

    Applied on read rather than as a one-shot file rewrite: it fixes the value
    every caller sees (including the settings form) without needing a writable
    data volume at boot, and it is idempotent, so a re-read costs nothing. The
    next save() persists the migrated value. Logged once per field per process
    so the operator sees it in `docker logs` without a line on every request."""
    for field in ('redirect_uri', 'webhook_url'):
        old = d.get(field) or ''
        new = legacy_paths.migrate_url(old)
        if new != old:
            d[field] = new
            if field not in _LOGGED_URL_MIGRATIONS:
                _LOGGED_URL_MIGRATIONS.add(field)
                log.info('v0.4.8 path migration: plaid %s %s → %s '
                         '(update this URL in your Plaid dashboard)',
                         field, old, new)


def save(client_id: str, environment: str, redirect_uri: str = '',
         webhook_url: str = '', sandbox_secret=None, production_secret=None,
         sync_interval_hours=None) -> dict:
    """Persist settings. Each secret is only overwritten when a non-None value
    is passed, so an admin can re-save the client id / environment without
    re-typing a secret (the form submits None to keep the existing one). The
    sync interval is likewise only touched when a value is passed."""
    d = load()
    d['client_id'] = (client_id or '').strip()
    env = (environment or 'sandbox').strip().lower()
    d['environment'] = env if env in ('sandbox', 'production') else 'sandbox'
    d['redirect_uri'] = (redirect_uri or '').strip()
    d['webhook_url'] = (webhook_url or '').strip()
    # A form submitted with a pre-v0.4.8 path is normalized on the way in, so
    # the migration can't be undone by re-saving the settings page.
    _migrate_plaid_urls(d)
    if sandbox_secret is not None:
        d['sandbox_secret'] = (sandbox_secret or '').strip()
    if production_secret is not None:
        d['production_secret'] = (production_secret or '').strip()
    if sync_interval_hours is not None:
        d['sync_interval_hours'] = sync_config.normalize_interval(
            sync_interval_hours)
    return _write(d)


def _write(d: dict) -> dict:
    """Persist exactly the whitelisted _FIELDS from `d` and return it. Shared by
    save() and save_public_url() so there is one writer, and a field added to
    _FIELDS is persisted by both without a second edit."""
    os.makedirs(current_app.config['DATA_DIR'], exist_ok=True)
    with open(_path(), 'w', encoding='utf-8') as f:
        json.dump({k: d.get(k) for k in _FIELDS}, f)
    return d


def save_public_url(funnel_hostname=None, redirect_uri=None) -> dict:
    """v0.7.0 — persist just the public-URL fields, leaving every other Plaid
    setting exactly as it was.

    The public-URL wizard on /admin/plaid_settings is a separate form from the
    credentials form, so it must not carry the client id or the secrets through a
    round-trip to change one URL. Each argument is only written when it is not
    None, which makes "save the hostname" and "save the redirect URI" independent
    and makes re-saving the same value a no-op (idempotent, as the Use-this
    button needs to be)."""
    d = load()
    if funnel_hostname is not None:
        d['funnel_hostname'] = (funnel_hostname or '').strip()
    if redirect_uri is not None:
        d['redirect_uri'] = (redirect_uri or '').strip()
        # A URL pasted on a pre-v0.4.8 path is normalized on the way in, exactly
        # as the credentials form does it.
        _migrate_plaid_urls(d)
    return _write(d)


def sync_interval_hours() -> int:
    """Effective background poll cadence in hours (0 = manual only). Persisted
    value wins over the SYNC_INTERVAL_HOURS env seed."""
    return sync_config.normalize_interval(load().get('sync_interval_hours'))


def active_secret() -> str:
    """The secret for the currently-selected environment."""
    d = load()
    return d['production_secret'] if d['environment'] == 'production' else d['sandbox_secret']


def is_configured() -> bool:
    """True when we have a client id + the active environment's secret."""
    return bool(load()['client_id'] and active_secret())


def _mask(s: str) -> str:
    if not s:
        return '(none)'
    return '••••' + s[-4:] if len(s) > 4 else '••••'


def masked() -> dict:
    """Never-the-full-value previews of both secrets for the settings UI."""
    d = load()
    return {
        'sandbox_secret': _mask(d['sandbox_secret']),
        'production_secret': _mask(d['production_secret']),
    }
