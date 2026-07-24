# SPDX-License-Identifier: MIT
"""Which Plaid accounts the UI shows (v0.4.44).

An install that was set up against Plaid's Sandbox before Production was
enabled keeps those test accounts forever: 'Plaid Checking ••0000', 'Plaid
Saving ••1111', a dozen of them, sitting beside the real ones in every list,
every dropdown and every count. They are not junk — they are the only accounts
with a transaction history varied enough to exercise the parser and the
reconciliation engine against, so DELETING THEM WOULD COST SOMETHING REAL.

So they are hidden, not removed. Nothing in this module writes, and nothing it
does is irreversible: flip one setting and every row is back.

WHY THE FILTER IS BY COMPANY. A sandbox account is not distinguishable by
type, subtype, mask or institution — Plaid's sandbox mints accounts that look
exactly like production ones, which is the point of a sandbox. What DOES
distinguish them is the ERPNext Company the operator assigned them to, because
that assignment is a human statement of intent: 'these are the test ones'. The
sentinel Company name is therefore the rule, and it is configurable rather than
hardcoded so an install that named its scratch entity something else can say so.

DEFAULT IS HIDDEN, which is the one decision here that changes behaviour on
upgrade. It is the right default because the accounts are noise for every
operator who isn't testing the parser, and because the failure mode of hiding
is 'where did my account go' (one toggle, discoverable on the settings page and
named in the accounts-page footer) while the failure mode of showing is a
sandbox row silently included in a reconciliation total.
"""
import json
import logging
import os

from flask import current_app

from . import db
from .models import BankTransaction, PlaidAccount, PlaidItem

log = logging.getLogger('bankbridge.account_visibility')

_FILENAME = 'ui_settings.json'

# The ERPNext Company name that marks an account as a test fixture rather than
# a real one, and whether such accounts are shown at all.
_DEFAULTS = {
    'include_sandbox_accounts': False,
    'sandbox_company': 'Bank Bridge Test',
}

_FIELDS = tuple(_DEFAULTS.keys())


def _path() -> str:
    return os.path.join(current_app.config['DATA_DIR'], _FILENAME)


def load() -> dict:
    """Current settings — defaults overlaid with the persisted JSON.

    Migrates on read and never raises: a missing, unreadable or partial file
    yields the defaults, so a boot with a stale or read-only data volume still
    produces correct values (the convention every settings module here
    follows)."""
    out = dict(_DEFAULTS)
    try:
        with open(_path()) as fh:
            persisted = json.load(fh) or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return out
    for key in _FIELDS:
        if key not in persisted:
            continue
        try:
            out[key] = (bool(persisted[key])
                        if isinstance(_DEFAULTS[key], bool)
                        else str(persisted[key]))
        except (TypeError, ValueError):
            log.warning('bad value for %s in %s, using default', key,
                        _FILENAME)
    return out


def save(updates: dict) -> dict:
    """Merge `updates` into the persisted settings and write back atomically.
    Unknown keys are dropped, so a stale form submission cannot pollute the
    file."""
    current = load()
    for key in _FIELDS:
        if key in updates:
            current[key] = (bool(updates[key])
                            if isinstance(_DEFAULTS[key], bool)
                            else str(updates[key]).strip())
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    tmp = _path() + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(current, fh, indent=2, sort_keys=True)
    os.replace(tmp, _path())
    return current


def sandbox_company() -> str:
    return (load().get('sandbox_company') or '').strip()


def include_sandbox() -> bool:
    return bool(load().get('include_sandbox_accounts'))


def _company_map() -> dict:
    """{account_id: effective owning Company} using the v0.4.0 resolution order
    (per-account override → Item Company). The ERPNext default is deliberately
    NOT consulted: an account falling through to the default is by definition
    not assigned to the sandbox Company, and reading settings here would make a
    UI filter depend on an ERPNext round trip."""
    items = {it.item_id: (it.owning_company or '').strip()
             for it in PlaidItem.query.all()}
    return {a.account_id: ((a.owning_company or '').strip()
                           or items.get(a.item_id, ''))
            for a in PlaidAccount.query.all()}


def sandbox_account_ids() -> set:
    """Every account assigned to the sandbox Company, whether or not they are
    currently hidden. Callers that need to TAG rather than filter (the accounts
    page, when the toggle is on) use this."""
    company = sandbox_company()
    if not company:
        return set()
    return {aid for aid, owner in _company_map().items() if owner == company}


def hidden_account_ids() -> set:
    """The accounts to filter out right now — empty when the toggle is on."""
    return set() if include_sandbox() else sandbox_account_ids()


def visible_accounts_query(query=None):
    """A PlaidAccount query with hidden accounts excluded.

    Takes an optional base query so a caller can layer this onto its own
    filters and ordering rather than losing them. Returns the query unchanged
    when nothing is hidden, which keeps the generated SQL identical to the
    pre-v0.4.44 one on an install that has no sandbox accounts at all."""
    query = PlaidAccount.query if query is None else query
    hidden = hidden_account_ids()
    if not hidden:
        return query
    return query.filter(PlaidAccount.account_id.notin_(tuple(hidden)))


def visible_accounts(query=None) -> list:
    """The visible accounts as a list, in whatever order the query specifies."""
    return visible_accounts_query(query).all()


def is_visible(account) -> bool:
    """Whether one account survives the filter. For a caller holding an object
    rather than a query."""
    account_id = getattr(account, 'account_id', account)
    return account_id not in hidden_account_ids()


def filter_accounts(accounts) -> list:
    """Filter an already-materialised list. One settings read and one company
    map for the whole list, rather than per row."""
    hidden = hidden_account_ids()
    if not hidden:
        return list(accounts)
    return [a for a in accounts
            if getattr(a, 'account_id', a) not in hidden]


def summary() -> dict:
    """{'hidden', 'total_sandbox', 'showing', 'company'} — what the settings
    page and the accounts-page footer report, so the toggle's effect is
    legible before it is flipped."""
    sandbox = sandbox_account_ids()
    showing = include_sandbox()
    return {'hidden': 0 if showing else len(sandbox),
            'total_sandbox': len(sandbox),
            'showing': showing,
            'company': sandbox_company()}


# ── re-link twin visibility (v0.5.8) ────────────────────────────────────────
# When a bank is re-linked Plaid mints a NEW account_id for the same physical
# account and marks the old row `superseded_by_account_id -> new`. Both rows
# then carry the overlap-period transactions with DIFFERENT
# plaid_transaction_ids — genuine twins. The anchor engine already collapses
# them (statements.dedupe_across_accounts); these helpers give the SAME
# collapse to user-facing aggregations (merchant counts, dashboard totals),
# which otherwise show "<merchant>: 2 transactions" for one real purchase.
#
# The chosen collapse is "hide the retired account entirely": a row on an
# account with `superseded_by_account_id` set is pre-relink bookkeeping, and
# the active row is the one a merchant total should reflect. Cleaner than a
# per-row fingerprint pass, and it is what an operator means by a merchant
# count. NOT for reconciliation (which spans both rows via supersede_chain) or
# for raw data views where both twins are legitimately distinct rows.

def superseded_account_ids():
    """A subquery of the account_ids of RETIRED PlaidAccounts — those pointing
    at a re-linked replacement via `superseded_by_account_id`. Returned as a
    query (not a materialized list) so it composes as an IN/NOT IN subselect."""
    return (db.session.query(PlaidAccount.account_id)
            .filter(PlaidAccount.superseded_by_account_id.isnot(None)))


def visible_bank_transactions_query():
    """`BankTransaction.query` with rows on retired (superseded) accounts
    excluded — the base every USER-FACING aggregation should start from so a
    re-link twin is counted once, not twice."""
    return BankTransaction.query.filter(
        BankTransaction.account_id.notin_(superseded_account_ids()))
