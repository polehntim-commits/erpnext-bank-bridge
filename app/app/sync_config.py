# SPDX-License-Identifier: MIT
"""Sync-frequency presets + cost math for the admin sync-cadence picker.

The background poll cadence is stored as a plain integer number of hours — the
same ``SYNC_INTERVAL_HOURS`` knob the scheduler has always read — but the admin
UI exposes it as a small set of cost-aware presets instead of a raw number.
``0`` (or any non-positive value) means **MANUAL ONLY**: no auto-poll job is
scheduled and syncs run only from the dashboard "Sync now" button.

These helpers are pure (no DB / Flask state) so they're trivially testable and
shared by the scheduler (app/services/scheduler.py), the admin view
(app/blueprints/admin_ui.py), and the persistence layer (app/plaid_settings.py).
"""

MANUAL = 0  # canonical "manual only" sentinel — any interval <= 0 folds to this

# Ordered presets for the dropdown; `value` is the interval in hours, `value=0`
# is the manual-only option. The label carries the per-day call count so the
# cost trade-off is visible without doing the math.
PRESETS = (
    {'value': 1,   'label': 'Hourly (24 syncs/day)'},
    {'value': 6,   'label': 'Every 6 hours (4 syncs/day)'},
    {'value': 24,  'label': 'Daily (1 sync/day)'},
    {'value': 72,  'label': 'Every 3 days'},
    {'value': 168, 'label': 'Weekly'},
    {'value': 720, 'label': 'Monthly'},
    {'value': 0,   'label': 'Manual only (no auto-sync)'},
)

# Indicative default price per Plaid /transactions/sync call for the estimate.
DEFAULT_PRICE_PER_CALL = 0.30

# ~average days per month, for the monthly cost projection.
_DAYS_PER_MONTH = 30.0


def normalize_interval(raw) -> int:
    """Coerce a raw interval to a clean int of hours.

    Junk (None / non-numeric) → daily (24). Any non-positive value → MANUAL
    (0). Positive values pass through unchanged."""
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        return 24
    return hours if hours > 0 else MANUAL


def is_auto_sync_enabled(hours) -> bool:
    """True when the interval schedules an automatic poll (hours > 0)."""
    return normalize_interval(hours) > 0


def syncs_per_day(hours) -> float:
    """Approximate automatic syncs per day for an interval; 0.0 for manual."""
    h = normalize_interval(hours)
    return 0.0 if h <= 0 else 24.0 / h


def monthly_calls(hours, accounts: int = 1) -> float:
    """Approx billable Plaid pull calls per month: ~one /transactions/sync call
    per poll per linked account across ~30 days. Manual → 0."""
    return syncs_per_day(hours) * _DAYS_PER_MONTH * max(0, int(accounts))


def monthly_cost_estimate(hours, accounts: int = 1,
                          price_per_call: float = DEFAULT_PRICE_PER_CALL) -> float:
    """Approx monthly Plaid cost for a preset at a given per-call price. A
    planning aid, not a bill — rounded to cents."""
    return round(monthly_calls(hours, accounts) * float(price_per_call), 2)


def preset_label(hours) -> str:
    """Human label for an interval — a matching preset, or a plain description
    for a custom (non-preset) env value."""
    h = normalize_interval(hours)
    for p in PRESETS:
        if p['value'] == h:
            return p['label']
    return f'Every {h}h' if h > 0 else 'Manual only (no auto-sync)'
