# SPDX-License-Identifier: MIT
"""One-shot, idempotent: force-provision the Counterparty doctype and say
exactly what happened (v0.4.6).

WHY THIS EXISTS. v0.4.5 shipped the Counterparty overlay but only ever reached
its provisioning code through the ERPNext *account import* path. An install that
had already imported its accounts under an earlier version never ran it: the
doctype was never created, no CREATE was ever attempted, and the only symptom
was a stream of 404s in the log from the read paths. v0.4.6 fixes the wiring —
provisioning now runs once per container about 15 seconds after boot — so on a
healthy install you should never need this script.

Run it when you want to provision NOW without a restart, or when the boot log
says the overlay is unavailable and you want the full reason with the ERPNext
response attached.

WHAT IT DOES. Probes for the Counterparty doctype; creates it if absent; then
(unless --no-pair) runs the same idempotent pairing pass the startup path does,
building Counterparties over the Customer and Supplier records you already have.

SAFETY. Additive only. It creates the doctype and Counterparty records and fills
BLANK links on existing ones. It never edits a Customer, a Supplier, an Account
or a posting, never clears a link a human set, and never deletes anything.
Re-running after a successful run reports "already present" and issues no
writes.

EXIT CODE. 0 when the doctype is available, 1 when it is not — so it can gate a
deploy step.

Runs in the BANK BRIDGE container (it needs Bank Bridge's ERPNext credentials),
not in the ERPNext bench:

    docker exec <bankbridge_container> python -m scripts.provision_counterparty_doctype
    docker exec <bankbridge_container> python -m scripts.provision_counterparty_doctype --no-pair
"""
from __future__ import annotations

import argparse
import sys


def run(client, *, pair: bool = True) -> dict:
    """Provision the doctype and optionally pair existing parties. Returns
    counterparty.provision_report's dict with a 'paired' key added (None when
    pairing was skipped). `client` is an ERPNextClient (or a test double)."""
    from app import counterparty

    if not counterparty.is_enabled():
        print('  ! COUNTERPARTY_OVERLAY_ENABLED is false — the overlay is')
        print('    switched off for this install. Nothing to provision.')
        return {'ok': False, 'state': 'disabled', 'reason': '', 'paired': None}

    report = dict(counterparty.provision_report(client), paired=None)
    state = report['state']
    if report['ok']:
        if state == counterparty.PROVISION_CREATED:
            print(f'  ✓ created the {counterparty.COUNTERPARTY_DT} doctype.')
        else:
            print(f'  ✓ the {counterparty.COUNTERPARTY_DT} doctype is already '
                  'present — nothing to do.')
    else:
        print(f'  ! the {counterparty.COUNTERPARTY_DT} doctype is NOT '
              f'available ({state}).')
        help_text = counterparty.PROVISION_HELP.get(state)
        if help_text:
            print(f'    {help_text}')
        if report['reason']:
            print(f'    ERPNext said: {report["reason"]}')
        return report

    if not pair:
        print('  · skipping the pairing pass (--no-pair).')
        return report
    result = counterparty.pair_existing_parties(client)
    report['paired'] = result
    for line in result.get('actions', []):
        print(f'  {line}')
    if not result.get('actions'):
        print('  nothing to pair — every party already has its Counterparty.')
    else:
        print(f'  created {result.get("created", 0)}, '
              f'linked {result.get("linked", 0)}, '
              f'unchanged {result.get("unchanged", 0)}, '
              f'failed {result.get("failed", 0)}.')
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Force-provision the ERPNext Counterparty doctype '
                    '(idempotent).')
    parser.add_argument('--no-pair', action='store_true',
                        help='provision the doctype only; skip building '
                             'Counterparties over existing Customers/Suppliers')
    args = parser.parse_args(argv)

    from app import create_app
    from app.sync_engine import get_erp_client_or_none

    # SCHEDULER_ENABLED off: this is a one-shot CLI, and letting it elect the
    # container's scheduler would leave a background thread running behind it.
    app = create_app({'SCHEDULER_ENABLED': False})
    with app.app_context():
        client = get_erp_client_or_none()
        if client is None:
            print('provision_counterparty_doctype: ERPNext is not configured '
                  '— check the connection on /admin/erpnext_settings and '
                  're-run.')
            return 1
        print('provision_counterparty_doctype: provisioning…')
        report = run(client, pair=not args.no_pair)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
