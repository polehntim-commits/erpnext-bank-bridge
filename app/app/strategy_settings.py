# SPDX-License-Identifier: MIT
"""Strategy tracker configuration (v0.4.31 · v0.5.0 Phase B).

The 5:4 buy-and-hold + covered-options overlay + 400% diversification
trigger are the OPERATOR'S strategy, not a fixed rule set. Every parameter
is configurable so the same code supports variations — 4:3, 6:5, tighter
profit targets, wider matching windows, etc. — and so the strategy can
evolve without a code change.

Persisted the same way plaid_settings.py and erpnext_settings.py are: a
JSON blob under DATA_DIR. Env vars seed the defaults; the /admin/strategy
page writes the file which then wins.

The frozen config snapshot on each StrategyTracker run captures the exact
values used at that time — so a re-analysis six months later, under a
different settings value, doesn't retroactively rewrite what the strategy
was doing back then.
"""
import json
import logging
import os

from flask import current_app

log = logging.getLogger('bankbridge.strategy_settings')

_FILENAME = 'strategy_settings.json'

# Strategy field defaults. See v0.5.0 spec Phase B for the semantic of each.
_DEFAULTS = {
    # 5:4 buy pattern
    'retain_ratio': 0.20,                # keep 20% (1 of 5) after selling 4/5
    'profit_target_pct': 25.0,           # sell at ~1.25× the buy price
    'matching_tolerance_qty_pct': 5.0,   # 76-84% sell qualifies as "4 of 5"
    'matching_tolerance_price_pct': 3.0,  # 22-28% profit qualifies
    'matching_window_days': 180,         # buy-to-sell must be within 180 days
    # 400% diversification trigger
    'diversification_trigger_pct': 400.0,  # ≥5× cost basis = flag for sell
    'diversification_sell_pct': 10.0,    # sell 10% of retained shares
    # Options overlay
    'options_writing_enabled': True,     # detect covered calls / cash-secured
    'options_covered_call_min_gain_pct': 25.0,  # only write calls at ≥25% gain
    'options_cash_secured_put_dry_powder_pct': 50.0,  # max % cash for puts
    'flag_naked_positions': True,        # alert on any short options w/o cover
}

_FIELDS = tuple(_DEFAULTS.keys())


def _path() -> str:
    return os.path.join(current_app.config['DATA_DIR'], _FILENAME)


def load() -> dict:
    """Current strategy settings — defaults overlaid with persisted JSON.
    Always returns every key in _FIELDS with a value of the right type."""
    out = dict(_DEFAULTS)
    try:
        with open(_path()) as fh:
            persisted = json.load(fh) or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return out
    for k in _FIELDS:
        if k in persisted:
            try:
                if isinstance(_DEFAULTS[k], bool):
                    out[k] = bool(persisted[k])
                elif isinstance(_DEFAULTS[k], float):
                    out[k] = float(persisted[k])
                elif isinstance(_DEFAULTS[k], int):
                    out[k] = int(persisted[k])
                else:
                    out[k] = persisted[k]
            except (TypeError, ValueError):
                log.warning('bad value for %s in strategy_settings, using '
                            'default', k)
    return out


def save(updates: dict) -> dict:
    """Merge `updates` into the persisted settings and write back. Returns
    the resulting settings dict. Unknown keys in `updates` are silently
    dropped — only the whitelisted _FIELDS are persisted, so a stale
    submission cannot pollute the file."""
    current = load()
    for k in _FIELDS:
        if k in updates:
            try:
                if isinstance(_DEFAULTS[k], bool):
                    current[k] = bool(updates[k])
                elif isinstance(_DEFAULTS[k], float):
                    current[k] = float(updates[k])
                elif isinstance(_DEFAULTS[k], int):
                    current[k] = int(updates[k])
                else:
                    current[k] = updates[k]
            except (TypeError, ValueError):
                log.warning('rejected invalid update for %s', k)
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    tmp = _path() + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(current, fh, indent=2, sort_keys=True)
    os.replace(tmp, _path())
    return current


def as_json_snapshot() -> str:
    """Serialize current settings for freezing on a StrategyTracker row.
    Uses sort_keys so the same values always serialize the same way (a
    stable serialization means a stable audit hash if we ever want one)."""
    return json.dumps(load(), sort_keys=True)
