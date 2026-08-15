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
_FIELDS = ('url', 'api_key', 'api_secret', 'default_company',
           # v0.8.4 — the Journal-Entry generation gate. Was env-only
           # (ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES), which meant flipping it
           # cost a compose edit and an Umbrel app recreate: an operator
           # pausing posting to investigate a bad rule had to restart the app
           # to do it, and again to resume. Persisted here it is a toggle on
           # the settings page and an MCP call. The env var still seeds the
           # boot default (see _defaults), so an install that has never touched
           # the toggle behaves exactly as it did before.
           'auto_generate_journal_entries',
           # v1.0.0 — the consolidation rollback switches. Each names where a
           # READ comes from: 'erpnext' (the default, and the point of the
           # consolidation) or 'local' (this app's own tables, which are never
           # deleted and stay current as a cache). Writes are unaffected —
           # anchors and pairings are pushed to ERPNext either way, because a
           # rollback that also stopped the outward flow would leave ERPNext
           # silently stale rather than merely unused.
           'anchor_source', 'rules_source', 'advisory_source')

# Fields whose persisted value is a bool rather than a string. Kept explicit so
# `load` coerces them through `_as_bool` rather than `bool()` — this file is
# operator-editable, and `bool("false")` is True.
_BOOL_FIELDS = ('auto_generate_journal_entries',)

# The three consolidation flags, and the only two values each may take. A
# hand-edited file holding anything else falls back to ERPNEXT rather than to
# an empty string, because '' is not a source and a silently source-less read
# would answer from nothing.
SOURCE_FIELDS = ('anchor_source', 'rules_source', 'advisory_source')
SOURCE_ERPNEXT = 'erpnext'
SOURCE_LOCAL = 'local'
SOURCES = (SOURCE_ERPNEXT, SOURCE_LOCAL)

# The strings a human writes into a JSON settings file meaning "off". Anything
# else non-empty is True, which keeps `true`/`1`/`yes` all working.
_FALSEY = ('false', 'no', 'off', '0', '')


def _as_bool(value) -> bool:
    """A persisted value as a bool, tolerating the string forms a hand-edit
    produces. `save` always writes a real JSON boolean; this is for the file
    somebody opened in an editor."""
    if isinstance(value, str):
        return value.strip().lower() not in _FALSEY
    return bool(value)


def _path() -> str:
    return os.path.join(current_app.config['DATA_DIR'], _FILENAME)


def _defaults() -> dict:
    c = current_app.config
    return {
        'url': (c.get('ERPNEXT_URL') or '').strip(),
        'api_key': (c.get('ERPNEXT_API_KEY') or '').strip(),
        'api_secret': (c.get('ERPNEXT_API_SECRET') or '').strip(),
        'default_company': (c.get('ERPNEXT_DEFAULT_COMPANY') or '').strip(),
        'auto_generate_journal_entries': bool(
            c.get('ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES', False)),
        'anchor_source': _as_source(c.get('ANCHOR_SOURCE')),
        'rules_source': _as_source(c.get('RULES_SOURCE')),
        'advisory_source': _as_source(c.get('ADVISORY_SOURCE')),
    }


def _as_source(value) -> str:
    """A persisted/env source value, normalised. Anything that isn't one of
    SOURCES becomes 'erpnext' — see SOURCE_FIELDS for why the unknown case
    resolves forward rather than to nothing."""
    v = (value or '').strip().lower() if isinstance(value, str) else ''
    return v if v in SOURCES else SOURCE_ERPNEXT


def load() -> dict:
    """Current ERPNext settings — env-var defaults overlaid with the
    persisted JSON. Always returns every key in _FIELDS.

    Migrates on read, per the DATA_DIR convention: a file written before
    v0.8.4 has no `auto_generate_journal_entries` key at all, and the absence
    correctly falls through to the env default rather than to False."""
    d = _defaults()
    try:
        with open(_path(), encoding='utf-8') as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k in _FIELDS:
                if k in saved and saved[k] is not None:
                    if k in _BOOL_FIELDS:
                        d[k] = _as_bool(saved[k])
                    elif k in SOURCE_FIELDS:
                        d[k] = _as_source(saved[k])
                    else:
                        d[k] = saved[k]
    except (FileNotFoundError, ValueError, OSError):
        pass
    return d


def je_generation_enabled() -> bool:
    """Whether the rules engine may write Journal Entries — the persisted
    toggle when one has been set, else the env var it was seeded from.

    THE ONE PLACE that decides. `categorization.categorize_after_push` and the
    admin/MCP surfaces all read this rather than the raw config key, so the
    toggle and the env var can never disagree about what is in force."""
    return bool(load().get('auto_generate_journal_entries', False))


def set_je_generation(enabled: bool) -> bool:
    """Persist the JE gate and return the value now in force."""
    d = load()
    d['auto_generate_journal_entries'] = bool(enabled)
    _write(d)
    return d['auto_generate_journal_entries']


# ── v1.0.0 · the consolidation source flags ─────────────────────────────────
#
# THE ONE PLACE each is read. Every call site asks these three functions rather
# than the raw config key, for the same reason je_generation_enabled exists:
# the env var only SEEDS the default, and a reader that consulted the env
# directly would report a source the operator had already switched away from.

def anchor_source() -> str:
    """Where the statement-anchor chain is read from — 'erpnext' or 'local'."""
    return _as_source(load().get('anchor_source'))


def rules_source() -> str:
    """Where the categorization rule set is read from."""
    return _as_source(load().get('rules_source'))


def advisory_source() -> str:
    """Where advisory-agreement fee terms are read from."""
    return _as_source(load().get('advisory_source'))


def set_source(field: str, value: str) -> str:
    """Persist one source flag and return the value now in force. Raises
    ValueError on an unknown field or value — this is the rollback switch, and
    a typo that silently did nothing is the worst possible behaviour for it."""
    if field not in SOURCE_FIELDS:
        raise ValueError(f'unknown source flag {field!r} — one of '
                         + ', '.join(SOURCE_FIELDS))
    v = (value or '').strip().lower()
    if v not in SOURCES:
        raise ValueError(f'{field} must be one of ' + ', '.join(SOURCES)
                         + f', got {value!r}')
    d = load()
    d[field] = v
    _write(d)
    return v


def _write(d: dict) -> None:
    os.makedirs(current_app.config['DATA_DIR'], exist_ok=True)
    with open(_path(), 'w', encoding='utf-8') as f:
        json.dump({k: d[k] for k in _FIELDS}, f)


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
    _write(d)
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
