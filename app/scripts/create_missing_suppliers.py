# SPDX-License-Identifier: MIT
"""One-off, idempotent: create ERPNext Supplier records for every party name
Bank Bridge rules point at that ERPNext doesn't already have.

Context 2026-07-30: after the party-wiring rollout (Sprint 5 Waves 2 & 3),
the plan_je_party_backfill.py surfaced 25 JEs whose rule now names a Supplier
that ERPNext has no record of. auto_create_party on the rules handles this
going forward when the next matching bank tx arrives — but the historical 25
JEs are already submitted, so no rule-fire will trigger the auto-create for
them. This script pre-creates the suppliers so the party-backfill can attach.

Idempotent: existing Suppliers are skipped. Safe to re-run.

Runs INSIDE the ERPNext container:

    docker cp create_missing_suppliers.py fafo-erpnext_server_1:/tmp/
    docker exec -u frappe -w /home/frappe/frappe-bench fafo-erpnext_server_1 \\
        env/bin/python /tmp/create_missing_suppliers.py

The list below is derived from the 64 categorization rules updated during the
2026-07-29 party-wiring session. Skip names that ERPNext already has (they
won't be recreated).
"""
from __future__ import annotations

import sys

# Every Supplier name a rule points at, as of 2026-07-30. Duplicates are fine
# — the exists check dedupes at the ERPNext side.
SUPPLIER_NAMES = [
    "Sawyer's Hardware LLC",
    "Anthropic",
    "USPTO",
    "76 Gas Stations",
    "AutoZone",
    "Spectrum",
    "Coastal",
    "Microsoft",
    "Northern Wasco County PUD",
    "Safeway",
    "Walgreens",
    "Wasco County",
    "Amazon",
    "Astro",
    "T-Mobile",
    "The Home Depot",
    "Starbucks",
    "Bryant Pipe & Supply",
    "Ozzies Deli & Gyro",
    "NAPA Auto Parts",
    "The Dalles Fuel Station",
    "Coastal Farm and Ranch",
    "Olive Garden",
    "Arby's",
    "Dairy Queen",
    "Fred Meyer",
    "Verizon",
    "Popeyes",
    "Ernies Locks & Keys",
    "Devins Alignment",
    "LensCrafters",
    "Trailer Station",
    "Westgate Dino Mart",
    "ARCO",
    "Advanced Auto Parts",
    "Friend & Reagan PC",
    "Gorge Garden Center",
    "Jones Truck & Implement",
    "Les Schwab",
    "Cooper Family Orchards",
    "Grocery Outlet",
    "Zoho",
    "Shell",
    "Sheppards Fuel",
    "Smoke Wring BBQ",
    "Valvoline",
    "Precision Automotive",
    "Oregon Equipment",
    "Rain Flo Irrigation",
    "Peterson Dalles Real Estate",
    "TradingView",
    "Sorren",
    "Oregon Department of Revenue",
    "Mitchell Huru",
    "Bob's Texas T-Bone & Frosty's Lounge",
]


def default_supplier_group() -> str:
    """Pick the first non-group Supplier Group ERPNext has, or fall back to
    'All Supplier Groups' if none. Every fresh ERPNext ships with something
    workable in the tree; this avoids hard-coding a value that may not exist.
    """
    import frappe
    rows = frappe.get_all(
        'Supplier Group',
        filters={'is_group': 0},
        fields=['name'],
        order_by='lft',
        limit=1,
    )
    return rows[0]['name'] if rows else 'All Supplier Groups'


def main() -> int:
    import frappe
    frappe.init(site='frontend', sites_path='/home/frappe/frappe-bench/sites')
    frappe.connect()

    group = default_supplier_group()
    created: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    for name in SUPPLIER_NAMES:
        if frappe.db.exists('Supplier', name):
            skipped.append(name)
            continue
        try:
            doc = frappe.get_doc({
                'doctype': 'Supplier',
                'supplier_name': name,
                'supplier_group': group,
                'supplier_type': 'Company',
            })
            doc.insert(ignore_permissions=True)
            created.append(name)
        except Exception as e:  # noqa: BLE001
            failed.append((name, str(e)[:200]))

    frappe.db.commit()

    print(f'\ncreate_missing_suppliers: {len(created)} created, '
          f'{len(skipped)} already existed, {len(failed)} failed.')
    print(f'supplier_group used: {group!r}')
    if created:
        print('\nCREATED:')
        for n in created:
            print(f'  + {n}')
    if failed:
        print('\nFAILED (left for manual review):')
        for n, err in failed:
            print(f'  ! {n} — {err}')
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
