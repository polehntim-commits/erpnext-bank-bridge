# SPDX-License-Identifier: MIT
"""One-shot, idempotent: refine existing Bank Account `account_subtype` values to
the precise v0.3.9 buckets.

The v0.3.8 remediation backfilled subtypes coarsely (CD / Money Market / Cash
Management → "Current", credit cards → "Other"). v0.3.9 maps Plaid subtypes 1:1
onto the 10 provisioned "Bank Account Subtype" masters. This script brings the
already-imported Bank Accounts in line by re-deriving the precise subtype from
the account's name (which carries the Title-cased subtype, e.g. "… Money Market
- 4444") and its account_type, then updating only when it changes and the target
master exists.

Idempotent: a second run finds every account already precise and changes nothing.

Run inside the ERPNext container (bench):

    docker cp scripts/backfill_account_subtypes.py <erpnext_container>:/tmp/
    docker exec <erpnext_container> bash -lc 'cd /home/frappe/frappe-bench/sites \
        && ../env/bin/python /tmp/backfill_account_subtypes.py <site>'
"""
from __future__ import annotations

import re
import sys

# (regex, precise subtype). Ordered most-specific first so "cash management"
# wins over a stray "cash", and "credit card" / "line of credit" resolve before
# the bare "cd" word-boundary match.
_PATTERNS = [
    (re.compile(r'cash management', re.I), 'Cash Management'),
    (re.compile(r'money market', re.I), 'Money Market'),
    (re.compile(r'line of credit', re.I), 'Line Of Credit'),
    (re.compile(r'credit card', re.I), 'Credit Card'),
    (re.compile(r'checking', re.I), 'Checking'),
    (re.compile(r'savings', re.I), 'Savings'),
    (re.compile(r'paypal', re.I), 'Paypal'),
    (re.compile(r'\bcd\b', re.I), 'Cd'),
]


def precise_subtype(name: str, account_type: str | None):
    """The precise Bank Account Subtype for an account, from its name (which
    carries the Title-cased Plaid subtype) with an account_type fallback of
    Credit → Credit Card. None when nothing recognizable matches (leave as-is)."""
    for rx, subtype in _PATTERNS:
        if rx.search(name or ''):
            return subtype
    if (account_type or '').strip().lower() == 'credit':
        return 'Credit Card'
    return None


def run(frappe) -> list[tuple[str, str, str]]:
    """Update each Bank Account's account_subtype to its precise value where that
    differs and the target master exists. Returns (name, old, new) tuples for the
    accounts actually changed — empty on a re-run."""
    changed: list[tuple[str, str, str]] = []
    accounts = frappe.get_all('Bank Account',
                              fields=['name', 'account_subtype', 'account_type'])
    for a in accounts:
        target = precise_subtype(a.get('name'), a.get('account_type'))
        old = a.get('account_subtype') or ''
        if not target or target == old:
            continue
        if not frappe.db.exists('Bank Account Subtype', target):
            print(f'  ! Bank Account Subtype {target!r} missing; skipping '
                  f'{a.get("name")}')
            continue
        frappe.db.set_value('Bank Account', a['name'], 'account_subtype', target)
        changed.append((a['name'], old, target))
        print(f'  {a["name"]}: {old!r} → {target!r}')
    return changed


def main() -> None:
    import frappe
    site = sys.argv[1] if len(sys.argv) > 1 else None
    frappe.init(site=site) if site else frappe.init()
    frappe.connect()
    try:
        changed = run(frappe)
        frappe.db.commit()
        print(f'backfill_account_subtypes: updated {len(changed)} account(s).')
    finally:
        frappe.destroy()


if __name__ == '__main__':
    main()
