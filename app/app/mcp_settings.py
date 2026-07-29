# SPDX-License-Identifier: MIT
"""MCP server configuration (v0.6.0).

Two independent gates protect the AI-operable surface:

  1. THE ENDPOINT ITSELF is off unless `BB_MCP_AUTH_TOKEN` is set in the
     environment (an Umbrel env override). No token → /mcp returns 404, i.e.
     the feature does not exist. The token is never persisted to disk or the
     database; it lives only in the process environment, so a leaked DATA_DIR
     or a DB dump cannot expose it.

  2. EACH MUTATING TOOL has its own kill switch, persisted under DATA_DIR the
     same way strategy_settings/plaid_settings are, and every one DEFAULTS OFF.
     A fresh install exposes the read-only tools and nothing else; an operator
     must deliberately flip a switch on /admin/mcp before the AI can change
     anything. Read tools are never gated (they cannot alter the books).

The split is deliberate: the token decides *whether an AI can talk to Bank
Bridge at all* (an ops/network decision, hence env), while the kill switches
decide *what it may change* (a per-capability trust decision, hence a settings
file the operator edits in the UI).
"""
import json
import logging
import os

from flask import current_app

log = logging.getLogger('bankbridge.mcp_settings')

_FILENAME = 'mcp_settings.json'

# One kill switch per MUTATING tool. ALL default False — the read-only tools
# carry no entry here because they are never gated.
_DEFAULTS = {
    'create_rule': False,
    # v0.7.3 — editing an existing rule is a SEPARATE switch from creating one,
    # deliberately: a new rule only affects transactions it newly matches, while
    # an update re-points a rule that is already categorizing traffic (and can
    # switch it off entirely). An operator who trusts an AI to propose new rules
    # has not thereby trusted it to rewrite the working ones.
    'update_rule': False,
    # v0.7.4 — registering an advisory agreement records TERMS, not a posting:
    # the agreement's own three switches still gate every JE. It is nonetheless
    # a separate switch from create_rule and defaults OFF like the rest, because
    # the record it writes is what a K-1 and an audit trail cite — an AI that
    # may propose categorization rules has not thereby been trusted to state
    # what two parties agreed to. Amending is separate again, on the same
    # reasoning create_rule/update_rule are split: a new agreement governs an
    # account nothing else governed, while an amendment restates the terms an
    # existing fee history already accrued under.
    'create_advisory_agreement': False,
    'update_advisory_agreement': False,
    'set_variance_tag': False,
    'trigger_reparse': False,
    'rebuild_anchors': False,
    'pair_accounts': False,
    'enable_je_posting': False,
    'disable_je_posting': False,
    # v0.7.1 — these two change what is reachable from the public Internet, so
    # they are the most consequential switches on this list and default OFF like
    # the rest. enable_public_url publishes the OAuth callback; disable_public_url
    # withdraws it, which breaks OAuth re-links until it is turned back on.
    'enable_public_url': False,
    'disable_public_url': False,
    # v0.8.4 — the admin actions that were button-only. Each is a separate
    # switch on the same reasoning as create_rule/update_rule: they differ in
    # what they can cost.
    #
    #   rerun_rules writes Journal Entries in BULK across the whole mirror. It
    #     is idempotent and it only ever posts what the operator's own rules
    #     already say — but "only what the rules say" is a lot of documents.
    #   reset_investment_drafts DELETES documents from ERPNext. It refuses to
    #     touch a submitted entry and it is the designed repair path, and it is
    #     still the only tool here that destroys anything.
    #   post_clearing_cleanup_je writes a six-figure correction to the general
    #     ledger. Draft-only and dry-run by default, and the most consequential
    #     accounting act on this list.
    #   enable_je_gate / disable_je_gate decide whether ANY of the above can
    #     post. Split in two because enabling and pausing are not the same
    #     trust: an operator may well want an AI able to stop the posting
    #     engine without being able to start it.
    #   set_erpnext_config re-points the install at a different ERPNext. It
    #     changes where every future document goes, which makes it closer to
    #     enable_public_url than to a settings edit.
    'rerun_rules': False,
    'reset_investment_drafts': False,
    'post_clearing_cleanup_je': False,
    'enable_je_gate': False,
    'disable_je_gate': False,
    'set_erpnext_config': False,
}

_FIELDS = tuple(_DEFAULTS.keys())

_ENV_TOKEN = 'BB_MCP_AUTH_TOKEN'


def auth_token() -> str:
    """The bearer token the /mcp endpoint requires, from the environment.
    Empty string when unset — which disables the endpoint entirely."""
    return (os.environ.get(_ENV_TOKEN) or '').strip()


def is_enabled() -> bool:
    """Whether the MCP endpoint exists at all (a token is configured)."""
    return bool(auth_token())


def masked_token() -> str:
    """The token with all but its last four characters hidden, for display on
    /admin/mcp. Never renders the token itself."""
    tok = auth_token()
    if not tok:
        return ''
    if len(tok) <= 4:
        return '•' * len(tok)
    return '•' * (len(tok) - 4) + tok[-4:]


def _path() -> str:
    return os.path.join(current_app.config['DATA_DIR'], _FILENAME)


def load() -> dict:
    """Current kill-switch state — defaults (all OFF) overlaid with persisted
    JSON. Always returns every key in _FIELDS as a bool."""
    out = dict(_DEFAULTS)
    try:
        with open(_path()) as fh:
            persisted = json.load(fh) or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return out
    for k in _FIELDS:
        if k in persisted:
            out[k] = bool(persisted[k])
    return out


def is_tool_enabled(tool_name: str) -> bool:
    """Whether a MUTATING tool is permitted. A read-only tool (no entry in
    _FIELDS) is always permitted; a mutating tool defaults OFF."""
    if tool_name not in _FIELDS:
        return True
    return bool(load().get(tool_name, False))


def save(updates: dict) -> dict:
    """Merge kill-switch `updates` into the persisted file and write back.
    Only whitelisted _FIELDS are persisted, so a stale form cannot pollute it."""
    current = load()
    for k in _FIELDS:
        if k in updates:
            current[k] = bool(updates[k])
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    tmp = _path() + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(current, fh, indent=2, sort_keys=True)
    os.replace(tmp, _path())
    return current
