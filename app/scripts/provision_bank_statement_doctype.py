# SPDX-License-Identifier: MIT
"""One-shot, idempotent: force-provision the Bank Statement doctype and say
exactly what happened (v0.4.10).

WHY THIS EXISTS. The same reason its Counterparty sibling does. Provisioning
runs once per container about 20 seconds after boot, so on a healthy install you
should never need this script. Run it when you want to provision NOW without a
restart, or when the boot log says the doctype is unavailable and you want the
full reason with the ERPNext response attached.

WHAT IT DOES. Probes for the Bank Statement doctype and creates it if absent.
That is all — uploading statements into it is
`scripts/backfill_erpnext_statements.py`, which also provisions, so this script
is for the case where you want to separate "can ERPNext give me the doctype?"
from "upload everything".

SAFETY. Additive only. It creates one doctype and nothing else. It never touches
a record, a posting or an existing doctype — including a `Bank Statement`
doctype it did not create, which it refuses to write to and reports instead (see
erpnext_statements._is_our_doctype).

EXIT CODE. 0 when the doctype is available, 1 when it is not — so it can gate a
deploy step.

Runs in the BANK BRIDGE container (it needs Bank Bridge's ERPNext credentials),
not in the ERPNext bench:

    docker exec <bankbridge_container> python -m scripts.provision_bank_statement_doctype
"""
from __future__ import annotations

import argparse
import sys


def run(client) -> dict:
    """Provision the doctype. Returns erpnext_statements.provision_report's
    dict. `client` is an ERPNextClient (or a test double)."""
    from app import erpnext_statements as es

    if not es.is_enabled():
        print('  ! ERPNEXT_STATEMENTS_ENABLED is false — the ERPNext statement')
        print('    overlay is switched off for this install. Nothing to')
        print('    provision.')
        return {'ok': False, 'state': 'disabled', 'reason': ''}

    report = es.provision_report(client)
    state = report['state']
    if report['ok']:
        if state == es.PROVISION_CREATED:
            print(f'  ✓ created the {es.BANK_STATEMENT_DT} doctype.')
        else:
            print(f'  ✓ the {es.BANK_STATEMENT_DT} doctype is already present '
                  '— nothing to do.')
        print('    Upload statements into it with: python -m '
              'scripts.backfill_erpnext_statements')
    else:
        print(f'  ! the {es.BANK_STATEMENT_DT} doctype is NOT available '
              f'({state}).')
        help_text = es.PROVISION_HELP.get(state)
        if help_text:
            print(f'    {help_text}')
        if report['reason']:
            print(f'    ERPNext said: {report["reason"]}')
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Force-provision the ERPNext Bank Statement doctype '
                    '(idempotent).')
    parser.parse_args(argv)

    from app import create_app
    from app.sync_engine import get_erp_client_or_none

    # SCHEDULER_ENABLED off: this is a one-shot CLI, and letting it elect the
    # container's scheduler would leave a background thread running behind it.
    app = create_app({'SCHEDULER_ENABLED': False})
    with app.app_context():
        client = get_erp_client_or_none()
        if client is None:
            print('provision_bank_statement_doctype: ERPNext is not configured '
                  '— check the connection on /admin/erpnext_settings and '
                  're-run.')
            return 1
        print('provision_bank_statement_doctype: provisioning…')
        report = run(client)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
