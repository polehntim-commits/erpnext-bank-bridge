# SPDX-License-Identifier: MIT
"""Per-app-instance in-memory caches (v1.0.0).

THE PROBLEM. The consolidation introduced three read caches — the rule
matcher's snapshot, the fetched-rules TTL, and advisory fee terms — and each is
keyed by something derived from the database: a rule-table fingerprint, an
agreement id. Held in a module-level dict, those keys are only unique WITHIN one
database. A process that builds a second Flask app against a second database
(which is exactly what the test suite does, once per test) then shares one cache
across both, and a key that collides hands the second app the first app's
answer.

That is not a hypothetical: `agreement.id == 1` exists in every one of a hundred
throwaway test databases, and a rule table with one row inserted by raw SQL
fingerprints identically in all of them.

THE FIX. Hang the cache off `current_app.extensions`, which Flask already scopes
per application object. One app per process in production, so this changes
nothing there; in the suite each app gets its own bucket and cross-test bleed
becomes impossible rather than merely unlikely.

Falls back to a process-global bucket outside an application context, so a
helper that happens to run at import time still works instead of raising.
"""
from __future__ import annotations

from flask import current_app

_EXT_KEY = 'bankbridge'

# The no-app-context fallback. Deliberately last-resort: nothing in the app
# should be caching before an app exists, and if something does, sharing one
# bucket is a better failure than a RuntimeError from a cache lookup.
_detached: dict = {}


def bucket(name: str) -> dict:
    """The named cache dict for the current app. Created on first use."""
    try:
        store = current_app.extensions.setdefault(_EXT_KEY, {})
    except RuntimeError:               # outside an application context
        store = _detached
    return store.setdefault(f'cache:{name}', {})


def clear(name: str) -> None:
    """Empty one named cache for the current app."""
    bucket(name).clear()
