# SPDX-License-Identifier: MIT
"""Categorization rules, read from ERPNext (v1.0.0).

WHAT MOVED AND WHAT DID NOT. The RULES move to ERPNext; the ENGINE does not.
ERPNext's native Bank Categorization Rule categorizes a Bank Transaction — it
sets a category and an account — and stops there. Bank Bridge's rules generate
draft Journal Entries, which is a different and heavier act, and moving it would
mean moving the party resolution, the cost-center mirroring, the dedup guard and
the description templating with it. So the source of truth for WHICH rule
applies is ERPNext, and the machinery that turns a matched rule into an entry
stays in app/categorization.py, unchanged.

HOW THE MIRROR WORKS. `refresh` fetches the active rule set over the API and
UPSERTS it into the local `categorization_rules` table, keyed on
`erpnext_rule_name`. That local table is no longer authoritative — it is a
read-through cache — but keeping the rules as real rows rather than as transient
dicts is deliberate and load-bearing:

  * every downstream consumer (JE generation, the rule-stats rollup, the audit
    trail, /admin/rules, the `bb_internal_tag` attribution) already takes a
    CategorizationRule, and a parallel dict-shaped path would be a second
    implementation of the matcher waiting to drift from the first;
  * a generated Journal Entry records the rule id that produced it. If the rule
    only existed in memory, that provenance would be a dangling number the day
    after ERPNext edited it;
  * and when ERPNext is unreachable, the cache IS the rollback. RULES_SOURCE=
    local is then a settings flip rather than a restore.

WHAT HAPPENS WHEN ERPNEXT HAS RULES. Once a fetch succeeds and returns at least
one active rule, the engine considers ONLY ERPNext-sourced rules — that is what
"single source of truth" means, and a mixed set where a stale local rule could
outrank an ERPNext one at the same priority would be the worst of both. Rules
authored locally are not deleted; they are simply not consulted while
RULES_SOURCE=erpnext and ERPNext has an answer. When the fetch fails or ERPNext
holds no rules at all, the engine falls back to the full local set and logs
which it used — a categorization run that silently matched nothing because a
method 404'd would post no Journal Entries and look exactly like a quiet day.
"""
from __future__ import annotations

import json
import logging
import time

from flask import current_app

from . import appcache
from . import db
from .erpnext_client import ERPNextAPIError, ERPNextError
from .erpnext_push import RULES_METHOD, _client_or_none
from .models import CategorizationRule

log = logging.getLogger('bankbridge.erpnext_rules')


# The last successful fetch, as {'at': monotonic_seconds, 'stats': …}. It bounds
# how often a busy sync re-asks ERPNext for a rule set that changes a few times
# a month; a stale entry costs at most ERPNEXT_CACHE_TTL_SECONDS of drift, and
# startup and `rerun_rules` both force past it, so the two moments where
# freshness actually matters never wait.
#
# Per APP INSTANCE (see app/appcache.py): "when did we last fetch" is a fact
# about one app talking to one ERPNext, and a second app in the same process
# has its own answer.
_CACHE = 'erpnext_rules_refresh'


def _last_refresh() -> dict:
    cache = appcache.bucket(_CACHE)
    if not cache:
        cache.update({'at': None, 'stats': None})
    return cache


def _ttl() -> int:
    try:
        return int(current_app.config.get('ERPNEXT_CACHE_TTL_SECONDS', 300))
    except (RuntimeError, TypeError, ValueError):
        return 300


def reset_cache() -> None:
    """Forget when we last fetched. For tests, and for a settings change that
    should take effect now rather than at the end of the TTL."""
    _last_refresh().update({'at': None, 'stats': None})


# ── the field mapping ───────────────────────────────────────────────────────
#
# ERPNext's Bank Categorization Rule and Bank Bridge's CategorizationRule name
# the same concepts differently, and the ERPNext side is a sibling codebase
# still settling. Each local column therefore lists EVERY remote spelling it
# accepts, first match wins. Tolerating `pattern` and `match_value` costs one
# tuple; discovering after a deploy that 64 rules silently mapped to an empty
# match_value costs a day of miscategorized transactions.
_FIELD_ALIASES = {
    'name': ('rule_name', 'title', 'name'),
    'match_type': ('match_type', 'matchtype'),
    'match_value': ('match_value', 'pattern', 'value'),
    'offset_account': ('offset_account', 'account', 'expense_account'),
    'applies_to_company': ('applies_to_company', 'company'),
    'priority': ('priority',),
    'active': ('active', 'enabled', 'is_active'),
    'cost_center': ('cost_center',),
    'bank_cost_center': ('bank_cost_center',),
    'party_type': ('party_type',),
    'party_name': ('party_name', 'party'),
    'bb_internal_tag': ('bb_internal_tag', 'internal_tag', 'tag'),
    'offset_direction': ('offset_direction',),
    'description_template': ('description_template',),
}

# ERPNext spellings for the match types, folded onto Bank Bridge's vocabulary.
# An unknown value maps to merchant_contains — the safest predicate, because it
# is the narrowest of the substring family and a rule that matches too little
# leaves a transaction for a human, while one that matches too much books it
# somewhere wrong.
_MATCH_TYPE_ALIASES = {
    'merchant_exact': 'merchant_exact',
    'exact': 'merchant_exact',
    'merchant': 'merchant_contains',
    'merchant_contains': 'merchant_contains',
    'contains': 'merchant_contains',
    'description_regex': 'description_regex',
    'regex': 'description_regex',
    'plaid_category_matches': 'plaid_category_matches',
    'category': 'plaid_category_matches',
    'amount_range': 'amount_range',
    'amount': 'amount_range',
    'combined': 'combined',
}

_TRUEY = ('1', 'true', 'yes', 'on', 'y', 't')


def _pick(row: dict, field: str):
    for key in _FIELD_ALIASES.get(field, (field,)):
        if key in row and row[key] not in (None, ''):
            return row[key]
    return None


def _as_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in _TRUEY
    return bool(value)


def normalize_rule(row: dict) -> dict | None:
    """One ERPNext rule dict → the local column values, or None when the row
    carries no docname (without one there is nothing to upsert ON, and a rule
    that re-inserted itself on every fetch would multiply)."""
    docname = (row.get('name') or row.get('docname') or '').strip() \
        if isinstance(row, dict) else ''
    if not docname:
        return None
    mt = (_pick(row, 'match_type') or 'merchant_contains')
    match_type = _MATCH_TYPE_ALIASES.get(str(mt).strip().lower(),
                                         'merchant_contains')
    try:
        priority = int(_pick(row, 'priority') or 100)
    except (TypeError, ValueError):
        priority = 100
    return {
        'erpnext_rule_name': docname,
        'name': str(_pick(row, 'name') or docname)[:255],
        'match_type': match_type,
        'match_value': str(_pick(row, 'match_value') or ''),
        'offset_account': str(_pick(row, 'offset_account') or '')[:255],
        'applies_to_company': (str(_pick(row, 'applies_to_company') or '')
                               .strip() or None),
        'priority': priority,
        'active': _as_bool(_pick(row, 'active'), True),
        'cost_center': (str(_pick(row, 'cost_center') or '').strip() or None),
        'bank_cost_center': (str(_pick(row, 'bank_cost_center') or '').strip()
                             or None),
        'party_type': (str(_pick(row, 'party_type') or '').strip() or None),
        'party_name': (str(_pick(row, 'party_name') or '').strip() or None),
        'bb_internal_tag': str(_pick(row, 'bb_internal_tag') or '')[:120],
        'offset_direction': (str(_pick(row, 'offset_direction') or 'auto')
                             .strip() or 'auto'),
        'description_template': str(_pick(row, 'description_template') or ''),
    }


# ── fetch ───────────────────────────────────────────────────────────────────

def fetch(client=None):
    """Every rule ERPNext holds, as a list of dicts — or None when it could not
    be read. None and [] are DIFFERENT answers and the caller acts on the
    difference: [] is "ERPNext has no rules, use the local set", None is
    "ERPNext did not answer, use the local set and say why"."""
    if client is None:
        client = _client_or_none()
    if client is None:
        return None
    try:
        out = client.call_method(RULES_METHOD,
                                 params={'active_only': 0, 'limit': 0})
    except (ERPNextAPIError, ERPNextError) as e:
        log.info('rule fetch from ERPNext failed (%s) — using the local rule '
                 'table', e)
        return None
    except Exception:  # noqa: BLE001
        log.warning('rule fetch from ERPNext raised', exc_info=True)
        return None
    return _rule_rows(out)


def _rule_rows(out):
    """The rule list out of whatever envelope ERPNext returned — same tolerance,
    and same reason, as erpnext_push._anchor_rows."""
    if isinstance(out, list):
        return out
    if isinstance(out, dict):
        for key in ('rules', 'data', 'rows', 'message'):
            value = out.get(key)
            if isinstance(value, list):
                return value
    return None


# ── mirror ──────────────────────────────────────────────────────────────────

def sync_into_local(rows) -> dict:
    """Upsert fetched rules into the local cache. Returns
    {'fetched', 'created', 'updated', 'deactivated', 'skipped'}.

    DEACTIVATES rather than deletes a mirrored rule ERPNext no longer returns.
    Deleting would orphan every GeneratedJournalEntry that names it, and the
    question "which rule booked this?" is one an auditor asks about entries made
    years ago. Only rows carrying an `erpnext_rule_name` are ever touched — a
    locally-authored rule is never modified by a fetch, because it is not a
    cache of anything."""
    stats = {'fetched': 0, 'created': 0, 'updated': 0, 'deactivated': 0,
             'skipped': 0}
    seen = set()
    for raw in (rows or []):
        values = normalize_rule(raw if isinstance(raw, dict) else {})
        if values is None:
            stats['skipped'] += 1
            continue
        stats['fetched'] += 1
        docname = values['erpnext_rule_name']
        seen.add(docname)
        rule = (CategorizationRule.query
                .filter(CategorizationRule.erpnext_rule_name == docname,
                        CategorizationRule.archived.is_(False))
                .order_by(CategorizationRule.id.desc()).first())
        if rule is None:
            rule = CategorizationRule(**values)
            db.session.add(rule)
            stats['created'] += 1
            continue
        changed = False
        for key, value in values.items():
            if getattr(rule, key, None) != value:
                setattr(rule, key, value)
                changed = True
        if changed:
            stats['updated'] += 1
    # Mirrored rules ERPNext stopped returning. Scoped to rows that carry a
    # docname AND are still active, so this is a no-op on the steady state.
    #
    # AN EMPTY `seen` IS A REAL ANSWER and retires the whole mirror: `fetch`
    # returns None (not []) when ERPNext could not be read, so reaching here
    # with nothing means ERPNext genuinely holds no rules — at which point
    # rule_snapshots falls back to the local table, which is the documented
    # behaviour. Leaving the mirror active instead would keep firing rules
    # ERPNext no longer has, which is the one thing "source of truth" rules out.
    #
    # But NOT when a row was skipped: ERPNext sent rules and this build could
    # not map them (no docname). That is a mapping failure, not a withdrawal,
    # and deactivating 64 working rules over it would be the worst possible
    # reading of an ambiguous response.
    if not stats['skipped']:
        for rule in (CategorizationRule.query
                     .filter(CategorizationRule.erpnext_rule_name.isnot(None),
                             CategorizationRule.active.is_(True),
                             CategorizationRule.archived.is_(False)).all()):
            if rule.erpnext_rule_name not in seen:
                rule.active = False
                stats['deactivated'] += 1
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.warning('could not write the fetched rule set to the local cache',
                    exc_info=True)
        return {**stats, 'error': 'local cache write failed'}
    return stats


def refresh(client=None, *, force: bool = False) -> dict:
    """Fetch + mirror, honouring the TTL unless forced.

    Returns {'refreshed': bool, 'reason': str, **stats}. Never raises: this runs
    at startup and inside every rerun, and an ERPNext that is merely down must
    not be able to stop either."""
    from . import categorization
    from . import erpnext_settings
    if erpnext_settings.rules_source() != erpnext_settings.SOURCE_ERPNEXT:
        return {'refreshed': False, 'reason': 'RULES_SOURCE=local'}
    last = _last_refresh()
    if not force and last['at'] is not None \
            and (time.monotonic() - last['at']) < _ttl():
        return {'refreshed': False, 'reason': 'cached',
                **(last['stats'] or {})}
    rows = fetch(client)
    if rows is None:
        return {'refreshed': False, 'reason': 'erpnext unreachable'}
    stats = sync_into_local(rows)
    last.update({'at': time.monotonic(), 'stats': stats})
    # The rule set just changed under the matcher's feet; drop its snapshot so
    # the very next evaluation sees what was fetched rather than what was.
    categorization.invalidate_rule_cache()
    log.info('rules refreshed from ERPNext: %s', json.dumps(stats))
    return {'refreshed': True, 'reason': 'fetched', **stats}


def mirrored_rule_count() -> int:
    """How many ACTIVE rules in the local table came from ERPNext. The test the
    engine applies before letting ERPNext's set displace the local one."""
    try:
        return int(CategorizationRule.query
                   .filter(CategorizationRule.erpnext_rule_name.isnot(None),
                           CategorizationRule.active.is_(True),
                           CategorizationRule.archived.is_(False)).count())
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return 0
