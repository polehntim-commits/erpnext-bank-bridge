# SPDX-License-Identifier: MIT
"""One-shot, idempotent: number the Bank-Bridge-managed GL groups and their leaf
accounts, ordered by **liquidity** (most-liquid lowest number), matching the
chart's existing numbering scheme.

Bank Bridge started numbering auto-created GL accounts in v0.3.9; accounts
imported earlier have an empty (or non-liquidity) account_number. This script
fills / re-orders them so the whole Chart of Accounts reads on standard
balance-sheet convention:

  * each managed group ("Bank Accounts", "Credit Cards", "Loans") gets a number
    in its parent's range (e.g. Current Liabilities siblings 2100/2200/2300/2400
    → new Credit Cards group 2500) when it doesn't have one;
  * each recognized leaf under a managed group is placed in its liquidity band
    (most-liquid first). Under group 1200: Cash Management → 1201, Checking →
    1211, Savings → 1221, Money Market → 1222, CD → 1231. On the Liabilities
    side: Credit Card → 2501…, Line of Credit → 2511…

Liquidity rank is derived from the account NAME (which carries the Title-cased
Plaid subtype), since ERPNext doesn't store the Plaid subtype. Leaves whose name
matches no known subtype (e.g. a hand-made "Plaid Test") are left untouched.

It ONLY sets the `account_number` field (never renames the account docname), so
existing Bank Account `account` links and Bank Bridge's stored GL pointers stay
valid. Reassignments use a park-then-set two-pass so ERPNext's per-company
account_number uniqueness constraint is never transiently violated. When a
company's chart doesn't use numbering the accounts are left untouched.

Idempotent: a second run finds every managed account already correctly numbered
and does nothing.

Run inside the ERPNext container (bench):

    docker cp scripts/backfill_account_numbers.py <erpnext_container>:/tmp/
    docker exec <erpnext_container> bash -lc 'cd /home/frappe/frappe-bench/sites \
        && ../env/bin/python /tmp/backfill_account_numbers.py <site>'
"""
from __future__ import annotations

import re
import sys

MANAGED_GROUPS = ('Bank Accounts', 'Credit Cards', 'Loans')
_LEADING_INT_RE = re.compile(r'^(\d+)')
_LIQUIDITY_BAND_SIZE = 10

# (regex, liquidity_rank, within-rank order). Ordered most-specific first, and
# mirrors app/erpnext_accounts.py's LIQUIDITY_RANK. Depository and credit
# keywords never appear in the same group, so sharing rank values is safe. The
# within-rank order lets Money Market sit after Savings (same rank 3).
_NAME_RANK = [
    (re.compile(r'cash management', re.I), 1, 0),
    (re.compile(r'money market', re.I), 3, 1),
    (re.compile(r'line of credit', re.I), 2, 0),
    (re.compile(r'credit card', re.I), 1, 0),
    (re.compile(r'checking', re.I), 2, 0),
    (re.compile(r'savings', re.I), 3, 0),
    (re.compile(r'paypal', re.I), 1, 0),
    (re.compile(r'\bcd\b', re.I), 4, 0),
]


def _leading_int(value):
    m = _LEADING_INT_RE.match(str(value or '').strip())
    return int(m.group(1)) if m else None


def _rank_and_order(name: str):
    """(liquidity_rank, within-rank order) for an account NAME, or None when the
    name matches no known subtype (leave such accounts untouched)."""
    for rx, rank, order in _NAME_RANK:
        if rx.search(name or ''):
            return rank, order
    return None


def _band_slot(rank: int) -> int:
    return 9 if rank >= 90 else min(max(rank, 1) - 1, 8)


def _band_start(base: int, rank: int) -> int:
    return base + 1 + _band_slot(rank) * _LIQUIDITY_BAND_SIZE


def _company_numbers(frappe, company: str) -> set:
    rows = frappe.get_all('Account', filters={'company': company},
                          fields=['account_number'])
    out = set()
    for r in rows:
        n = _leading_int(r.get('account_number'))
        if n is not None:
            out.add(n)
    return out


def _next_group_number(frappe, company: str, parent: str):
    """Number for a new/unnumbered GROUP under `parent`: max sibling group + the
    siblings' spacing (default 100), else the parent's own number + 100. None
    when the chart isn't numbered. Bumped past any company collision."""
    siblings = frappe.get_all(
        'Account', filters={'parent_account': parent, 'company': company,
                            'is_group': 1}, fields=['account_number'])
    nums = sorted(n for n in (_leading_int(s.get('account_number'))
                              for s in siblings) if n is not None)
    if nums:
        gaps = [b - a for a, b in zip(nums, nums[1:]) if b - a > 0]
        step = min(gaps) if gaps else 100
        candidate = nums[-1] + step
    else:
        base = _leading_int(frappe.db.get_value('Account', parent, 'account_number'))
        if base is None:
            return None
        step = 100
        candidate = base + step
    used = _company_numbers(frappe, company)
    while candidate in used:
        candidate += step
    return str(candidate)


def _number_group(frappe, company: str, group: dict) -> tuple | None:
    """Assign a number to a managed group that lacks one. Returns (name, number)
    when it did, else None."""
    if str(group.get('account_number') or '').strip():
        return None
    num = _next_group_number(frappe, company, group['parent_account'])
    if not num:
        return None
    frappe.db.set_value('Account', group['name'], 'account_number', num)
    return (group['name'], num)


def _plan_leaf_targets(frappe, company: str, group_name: str, base: int) -> dict:
    """Map {leaf_name: target_number} for the recognized leaves under a managed
    group, placing each in its liquidity band (most-liquid lowest, within-rank by
    (order, name)). Unrecognized leaves are omitted (left untouched). Targets
    avoid numbers held by any *other* account in the company."""
    leaves = frappe.get_all(
        'Account', filters={'parent_account': group_name, 'company': company,
                            'is_group': 0},
        fields=['name', 'account_name', 'account_number'])
    recognized = []
    for leaf in leaves:
        ro = _rank_and_order(leaf.get('account_name') or leaf['name'])
        if ro is None:
            continue
        recognized.append((leaf, ro[0], ro[1]))

    # Numbers we must not step on: everything in the company EXCEPT the current
    # numbers of the very leaves we're about to renumber (those are freed).
    reassigning = {_leading_int(leaf.get('account_number'))
                   for leaf, _, _ in recognized}
    reassigning.discard(None)
    blocked = _company_numbers(frappe, company) - reassigning

    targets: dict = {}
    by_rank: dict = {}
    for leaf, rank, order in recognized:
        by_rank.setdefault(rank, []).append((order, leaf['account_name'] or leaf['name'], leaf))
    for rank in sorted(by_rank):
        n = _band_start(base, rank)
        for _order, _name, leaf in sorted(by_rank[rank], key=lambda t: (t[0], t[1])):
            while n in blocked or n in targets.values():
                n += 1
            targets[leaf['name']] = n
            n += 1
    return targets


def run(frappe) -> list[tuple[str, str]]:
    """Number managed groups and liquidity-order their leaves across every
    company. Returns (account, number) tuples actually changed — empty on a
    re-run."""
    assigned: list[tuple[str, str]] = []
    for comp in frappe.get_all('Company', fields=['name']):
        company = comp['name']
        for group_name in MANAGED_GROUPS:
            group = frappe.db.get_value(
                'Account',
                {'company': company, 'account_name': group_name, 'is_group': 1},
                ['name', 'parent_account', 'account_number'], as_dict=True)
            if not group:
                continue
            changed = _number_group(frappe, company, group)
            if changed:
                assigned.append(changed)
                group['account_number'] = changed[1]
                print(f'  group {changed[0]} → {changed[1]}')
            base = _leading_int(group.get('account_number'))
            if base is None:
                continue   # chart doesn't number this branch
            width = len(str(base))
            targets = _plan_leaf_targets(frappe, company, group['name'], base)
            pending = []
            for leaf_name, target in targets.items():
                cur = _leading_int(
                    frappe.db.get_value('Account', leaf_name, 'account_number'))
                if cur != target:
                    pending.append((leaf_name, str(target).zfill(width)))
            # Park-then-set so the per-company uniqueness constraint never trips
            # mid-reorder (e.g. two leaves swapping bands).
            for i, (leaf_name, _target) in enumerate(pending):
                frappe.db.set_value('Account', leaf_name, 'account_number',
                                    f'BKFILL-PARK-{i}')
            for leaf_name, target in pending:
                frappe.db.set_value('Account', leaf_name, 'account_number', target)
                assigned.append((leaf_name, target))
                print(f'  leaf  {leaf_name} → {target}')
    return assigned


def main() -> None:
    import frappe
    site = sys.argv[1] if len(sys.argv) > 1 else None
    frappe.init(site=site) if site else frappe.init()
    frappe.connect()
    try:
        assigned = run(frappe)
        frappe.db.commit()
        print(f'backfill_account_numbers: assigned {len(assigned)} number(s).')
    finally:
        frappe.destroy()


if __name__ == '__main__':
    main()
