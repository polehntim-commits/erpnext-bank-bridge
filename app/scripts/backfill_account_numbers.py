# SPDX-License-Identifier: MIT
"""One-shot, idempotent: assign account_number to the Bank-Bridge-managed GL
groups and their leaf accounts, matching the chart's existing numbering scheme.

Bank Bridge started numbering auto-created GL accounts in v0.3.9; accounts
imported earlier have an empty account_number. This script fills those in so the
whole Chart of Accounts reads consistently:

  * each managed group ("Bank Accounts", "Credit Cards", "Loans") gets a number
    in its parent's range (e.g. Current Liabilities siblings 2100/2200/2300/2400
    → new Credit Cards group 2500) when it doesn't have one;
  * each numberless leaf under a managed group gets the next sequential number
    from its siblings (1200 group → 1201, 1202, …).

It ONLY sets the `account_number` field (never renames the account docname), so
existing Bank Account `account` links and Bank Bridge's stored GL pointers stay
valid. When a company's chart doesn't use numbering (no numbered siblings and no
numbered parent) the accounts are left untouched.

Idempotent: a second run finds every managed account already numbered and does
nothing.

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


def _leading_int(value):
    m = _LEADING_INT_RE.match(str(value or '').strip())
    return int(m.group(1)) if m else None


def _company_numbers(frappe, company: str) -> set[int]:
    rows = frappe.get_all('Account', filters={'company': company},
                          fields=['account_number'])
    out = set()
    for r in rows:
        n = _leading_int(r.get('account_number'))
        if n is not None:
            out.add(n)
    return out


def _next_number(frappe, company: str, parent: str, *, is_group: bool):
    """Next account_number under `parent`, mirroring the app's scheme: max
    same-kind sibling + step (1 for a leaf; the siblings' spacing, default 100,
    for a group), else the parent's own leading number + step. None when neither
    siblings nor the parent are numbered (chart doesn't use numbering). Padded to
    the siblings' width and bumped past any number already used in the company."""
    siblings = frappe.get_all(
        'Account',
        filters={'parent_account': parent, 'company': company,
                 'is_group': 1 if is_group else 0},
        fields=['account_number'])
    sib_strs = [str(s.get('account_number') or '').strip() for s in siblings]
    nums = sorted(int(s) for s in sib_strs if s.isdigit())
    width = max((len(s) for s in sib_strs if s.isdigit()), default=0)

    if nums:
        if is_group:
            gaps = [b - a for a, b in zip(nums, nums[1:]) if b - a > 0]
            step = min(gaps) if gaps else 100
        else:
            step = 1
        candidate = nums[-1] + step
    else:
        base = _leading_int(frappe.db.get_value('Account', parent, 'account_number'))
        if base is None:
            return None
        step = 100 if is_group else 1
        candidate = base + step
        width = width or len(str(base))

    used = _company_numbers(frappe, company)
    bump = step if step else 1
    while candidate in used:
        candidate += bump
    return str(candidate).zfill(width)


def run(frappe) -> list[tuple[str, str]]:
    """Number the managed groups and their numberless leaves across every
    company. Returns (account, number) tuples actually assigned — empty on a
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
            # 1) number the group itself if it has none.
            if not str(group.get('account_number') or '').strip():
                num = _next_number(frappe, company, group['parent_account'],
                                   is_group=True)
                if num:
                    frappe.db.set_value('Account', group['name'],
                                        'account_number', num)
                    assigned.append((group['name'], num))
                    print(f'  group {group["name"]} → {num}')
            # 2) number each numberless leaf child, in a stable order.
            leaves = frappe.get_all(
                'Account',
                filters={'parent_account': group['name'], 'company': company,
                         'is_group': 0},
                fields=['name', 'account_number'], order_by='name asc')
            for leaf in leaves:
                if str(leaf.get('account_number') or '').strip():
                    continue
                num = _next_number(frappe, company, group['name'], is_group=False)
                if not num:
                    continue
                frappe.db.set_value('Account', leaf['name'], 'account_number', num)
                assigned.append((leaf['name'], num))
                print(f'  leaf  {leaf["name"]} → {num}')
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
