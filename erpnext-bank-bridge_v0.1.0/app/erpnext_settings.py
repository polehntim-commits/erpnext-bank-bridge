# SPDX-License-Identifier: MIT
"""ERPNext bridge connection settings.

Persisted as a small JSON blob under DATA_DIR so the /admin/erpnext_settings
page can edit the connection (URL, API key/secret, default Company) without a
redeploy. The Config env vars (ERPNEXT_*) seed the defaults; the JSON file —
once written — WINS. Same shape/contract as app/plaid_settings.py so callers
that already know that module have nothing new to learn.

The API secret is a credential. It lives inside the DATA_DIR volume (the trust
boundary for this single-tenant LAN deployment); the /admin/erpnext_settings
page never renders it back in full (only a masked preview). A flat global for
now — becomes a per-tenant row if multi-tenancy is ever added."""
import json
import os

from flask import current_app

_FILENAME = 'erpnext_settings.json'
_FIELDS = ('url', 'api_key', 'api_secret', 'default_company')


def _path() -> str:
    return os.path.join(current_app.config['DATA_DIR'], _FILENAME)


def _defaults() -> dict:
    c = current_app.config
    return {
        'url': (c.get('ERPNEXT_URL') or '').strip(),
        'api_key': (c.get('ERPNEXT_API_KEY') or '').strip(),
        'api_secret': (c.get('ERPNEXT_API_SECRET') or '').strip(),
        'default_company': (c.get('ERPNEXT_DEFAULT_COMPANY') or '').strip(),
    }


def load() -> dict:
    """Current ERPNext settings — env-var defaults overlaid with the
    persisted JSON. Always returns every key in _FIELDS."""
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
    return d


def save(url: str, api_key: str, api_secret=None, default_company: str = '') -> dict:
    """Persist the connection fields. `api_secret` is only overwritten when a
    non-None value is passed — so an admin can re-save the URL / company
    without re-typing the secret (the form submits None to keep the existing
    one). Returns the merged settings."""
    d = load()
    d['url'] = (url or '').strip()
    d['default_company'] = (default_company or '').strip()
    d['api_key'] = (api_key or '').strip()
    if api_secret is not None:
        d['api_secret'] = (api_secret or '').strip()
    os.makedirs(current_app.config['DATA_DIR'], exist_ok=True)
    with open(_path(), 'w', encoding='utf-8') as f:
        json.dump({k: d[k] for k in _FIELDS}, f)
    return d


def is_configured() -> bool:
    """True when we have enough to attempt a call (URL + key + secret)."""
    d = load()
    return bool(d['url'] and d['api_key'] and d['api_secret'])


def masked_secret() -> str:
    """A never-the-full-value preview of the stored secret for the UI:
    last 4 chars only, or '(none)' if unset."""
    s = load()['api_secret']
    if not s:
        return '(none)'
    return '••••' + s[-4:] if len(s) > 4 else '••••'
