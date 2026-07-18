# SPDX-License-Identifier: MIT
"""One-shot, idempotent: build the Counterparty overlay over the Customer and
Supplier records an install already has (v0.4.5).

WHAT IT DOES. Reads every non-disabled Customer and Supplier out of ERPNext,
groups them by party name, and creates one Counterparty per distinct name —
linking both sides where a name appears on both, one side where it appears
once. A name on both sides gets `dual_role_flag = 1`, which is the whole point:
"Wells Fargo" stops being two unrelated records and becomes one party you both
pay and get paid by.

WHY YOU PROBABLY DON'T NEED TO RUN IT. Bank Bridge runs the same pass at
startup (COUNTERPARTY_AUTO_PAIR, default on), so an upgrading install is
already paired by the time you read this. The script exists for three cases:
you turned the startup pass off, you want to SEE the plan before anything is
written (`--dry-run`), or you added parties directly in ERPNext and want them
picked up without a restart.

MATCHING IS EXACT, ON PURPOSE. "Wells Fargo" and "Wells Fargo Bank NA" stay two
Counterparties. Fuzzy-merging tax identities is the kind of clever that shows up
as a wrong 1099 in January; under-pairing is visible and takes thirty seconds to
fix by hand, over-pairing is invisible and doesn't.

SAFETY. Additive only. It creates Counterparty records and fills BLANK links on
existing ones. It never edits a Customer, a Supplier, an Account or a posting,
never clears a link a human set, and never deletes anything. Re-running after a
successful run reports everything as unchanged and issues no writes.

Runs in the BANK BRIDGE container (it needs Bank Bridge's ERPNext credentials),
not in the ERPNext bench:

    docker exec <bankbridge_container> python -m scripts.pair_existing_customer_supplier --dry-run
    docker exec <bankbridge_container> python -m scripts.pair_existing_customer_supplier
"""
from __future__ import annotations

import argparse
import sys


def run(client, *, dry_run: bool = False) -> dict:
    """Pair the existing parties. Returns counterparty.pair_existing_parties'
    summary dict. `client` is an ERPNextClient (or a test double)."""
    from app import counterparty

    if not counterparty.ensure_counterparty_doctype(client):
        print('  ! the Counterparty doctype is not available in this ERPNext.')
        print('    The API user needs create permission on DocType (a System')
        print('    Manager role), or COUNTERPARTY_OVERLAY_ENABLED is off.')
        return {'created': 0, 'linked': 0, 'unchanged': 0, 'failed': 0,
                'actions': [], 'available': False}
    result = counterparty.pair_existing_parties(client, dry_run=dry_run)
    result['available'] = True
    for line in result['actions']:
        print(f'  {line}')
    if not result['actions']:
        print('  nothing to do — every party already has its Counterparty.')
    return result


def main(argv=None) -> int:
    from app import create_app
    from app.sync_engine import get_erp_client_or_none

    parser = argparse.ArgumentParser(
        description='Build the Counterparty overlay over existing ERPNext '
                    'Customer + Supplier records.')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='report what would be created/linked without writing anything')
    args = parser.parse_args(argv)

    app = create_app({'SCHEDULER_ENABLED': False})
    with app.app_context():
        client = get_erp_client_or_none()
        if client is None:
            print('pair_existing_customer_supplier: ERPNext is not configured '
                  '— check the connection on /admin/erpnext_settings and '
                  're-run.')
            return 1
        if args.dry_run:
            print('pair_existing_customer_supplier: DRY RUN — no writes.')
        result = run(client, dry_run=args.dry_run)
    if not result.get('available'):
        return 1
    verb = 'would create' if args.dry_run else 'created'
    linked = 'would link' if args.dry_run else 'linked'
    print(f'pair_existing_customer_supplier: {verb} {result["created"]} '
          f'Counterparty(ies), {linked} {result["linked"]}, '
          f'{result["unchanged"]} already correct, {result["failed"]} failed.')
    return 1 if result['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
