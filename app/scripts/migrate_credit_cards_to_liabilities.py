# SPDX-License-Identifier: MIT
"""One-shot, idempotent: move credit-card GL accounts off the Assets side and
onto Liabilities → Current Liabilities → Credit Cards.

Before v0.3.9 every imported Plaid account — credit cards included — was created
as a Bank-typed leaf under the Assets-side "Bank Accounts" group, which
overstates assets and understates liabilities. This script realigns any existing
credit-card GL account (the `account` linked from a Bank Account whose
account_type is "Credit") under a "Credit Cards" group on the Liabilities side,
auto-creating that group the first time. The leaf's own account_type is forced
back to "Bank" so ERPNext's Bank Reconciliation Tool keeps operating on it.

Idempotent: a second run finds every card already under Credit Cards with
account_type Bank and changes nothing. Safe to re-run.

Account NUMBERS are intentionally NOT assigned here — run
backfill_account_numbers.py afterwards to number the new group and its leaves.

Run inside the ERPNext container (bench):

    docker cp scripts/migrate_credit_cards_to_liabilities.py <erpnext_container>:/tmp/
    docker exec <erpnext_container> bash -lc 'cd /home/frappe/frappe-bench/sites \
        && ../env/bin/python /tmp/migrate_credit_cards_to_liabilities.py <site>'

(<site> defaults to the bench's currentsite, e.g. "frontend".)
"""
from __future__ import annotations

import sys

CREDIT_CARD_GROUP = 'Credit Cards'
CURRENT_LIABILITIES = 'Current Liabilities'


def _find_group(frappe, company: str, account_name: str):
    """Docname of the is_group Account named `account_name` in `company`, or
    None."""
    return frappe.db.get_value(
        'Account',
        {'company': company, 'account_name': account_name, 'is_group': 1},
        'name')


def _liability_root(frappe, company: str):
    """The company's root Liability group (no parent), or None."""
    groups = frappe.get_all(
        'Account',
        filters={'company': company, 'is_group': 1, 'root_type': 'Liability'},
        fields=['name', 'parent_account'])
    for g in groups:
        if not (g.get('parent_account') or ''):
            return g['name']
    return groups[0]['name'] if groups else None


def _ensure_current_liabilities(frappe, company: str):
    """Find (or create under the Liability root) the Current Liabilities group."""
    existing = _find_group(frappe, company, CURRENT_LIABILITIES)
    if existing:
        return existing
    root = _liability_root(frappe, company)
    if not root:
        return None
    doc = frappe.get_doc({'doctype': 'Account', 'account_name': CURRENT_LIABILITIES,
                          'parent_account': root, 'company': company, 'is_group': 1})
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc.name


def ensure_credit_card_group(frappe, company: str):
    """Find (or create) the Credit Cards group under Current Liabilities; return
    its docname, or None when the company has no Liability branch to anchor to."""
    existing = _find_group(frappe, company, CREDIT_CARD_GROUP)
    if existing:
        return existing
    parent = _ensure_current_liabilities(frappe, company)
    if not parent:
        return None
    doc = frappe.get_doc({'doctype': 'Account', 'account_name': CREDIT_CARD_GROUP,
                          'parent_account': parent, 'company': company,
                          'is_group': 1})
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc.name


def run(frappe) -> list[str]:
    """Move every credit-card GL account under its company's Credit Cards group
    (fixing account_type back to Bank). Returns the list of GL accounts that were
    actually changed this run — empty on a re-run once everything is in place."""
    moved: list[str] = []
    cards = frappe.get_all('Bank Account', filters={'account_type': 'Credit'},
                           fields=['name', 'account'])
    group_cache: dict[str, str | None] = {}
    for ba in cards:
        gl = ba.get('account')
        if not gl:
            continue
        company = frappe.db.get_value('Account', gl, 'company')
        if not company:
            continue
        if company not in group_cache:
            group_cache[company] = ensure_credit_card_group(frappe, company)
        group = group_cache[company]
        if not group:
            print(f'  ! no Credit Cards anchor for company {company!r}; '
                  f'skipping {gl}')
            continue
        cur = frappe.db.get_value('Account', gl,
                                  ['parent_account', 'account_type'], as_dict=True)
        needs_parent = (cur.get('parent_account') if cur else None) != group
        needs_type = (cur.get('account_type') if cur else None) != 'Bank'
        if not (needs_parent or needs_type):
            continue
        doc = frappe.get_doc('Account', gl)
        if needs_parent:
            doc.parent_account = group
        if needs_type:
            doc.account_type = 'Bank'
        doc.flags.ignore_permissions = True
        doc.save()
        moved.append(gl)
        print(f'  moved {gl} → {group} (account_type=Bank)')
    return moved


def main() -> None:
    import frappe
    site = sys.argv[1] if len(sys.argv) > 1 else None
    frappe.init(site=site) if site else frappe.init()
    frappe.connect()
    try:
        moved = run(frappe)
        frappe.db.commit()
        print(f'migrate_credit_cards_to_liabilities: moved {len(moved)} '
              f'credit-card account(s).')
    finally:
        frappe.destroy()


if __name__ == '__main__':
    main()
