# SPDX-License-Identifier: MIT
"""One-shot, idempotent: upload every locally-held bank statement into ERPNext
as a Bank Statement record with its PDF attached (v0.4.10).

WHY THIS EXISTS. v0.4.9 fetched statements into Bank Bridge's own storage. An
install upgrading to v0.4.10 therefore has a pile of statements ERPNext has
never seen. The scheduler picks them up on its own about 20 seconds after boot,
so on a healthy install you should never need this script.

Run it when you want the upload to happen NOW without a restart, when
ERPNEXT_STATEMENTS_AUTO_SYNC is off, or when you want to see per-statement
reasons for anything that did not sync.

WHAT IT DOES. Provisions the Bank Statement doctype if it is absent, then for
every local statement without an ERPNext record: creates the record, attaches
the PDF, and writes the reconciliation verdict. Finally it refreshes the verdict
on records that already exist, because a statement synced months ago reconciles
against the mirror as it was THEN, and a since-closed transaction gap changes
the answer.

SAFETY. Additive only. It creates Bank Statement records and attaches files. It
never edits a Bank Account, an Account, a Journal Entry or any posting, and it
never deletes anything. Re-running after a successful run reports everything as
already synced and issues no creates.

IDEMPOTENCY. Two independent guards: the local `erpnext_docname` column, and the
unique `plaid_statement_id` on the ERPNext side. The second is what makes this
safe to run against a data volume restored from a backup — a statement whose
local column is blank but whose ERPNext record exists is ADOPTED (the local row
learns its docname) rather than duplicated.

EXIT CODE. 0 when the doctype is available and nothing failed, 1 otherwise — so
it can gate a deploy step.

Runs in the BANK BRIDGE container (it needs Bank Bridge's ERPNext credentials),
not in the ERPNext bench:

    docker exec <bankbridge_container> python -m scripts.backfill_erpnext_statements --dry-run
    docker exec <bankbridge_container> python -m scripts.backfill_erpnext_statements
"""
from __future__ import annotations

import argparse
import sys

# Per-statement outcomes, in the order the summary prints them, with the
# operator-facing gloss for each.
_ACTION_LABELS = {
    'created': 'created in ERPNext',
    'adopted': 'already in ERPNext — local row re-pointed at it',
    'skipped': 'already synced',
    'no_account': 'SKIPPED — the Plaid account has no ERPNext Bank Account',
    'no_period': 'SKIPPED — the statement has no period bounds',
    'failed': 'FAILED',
}


def run(client, *, dry_run: bool = False, verbose: bool = True) -> dict:
    """Provision, then sync every pending statement. Returns the stats dict with
    an added 'provision' key. `client` is an ERPNextClient (or a test double)."""
    from app import erpnext_statements as es

    if not es.is_enabled():
        print('  ! ERPNEXT_STATEMENTS_ENABLED is false — the ERPNext statement')
        print('    overlay is switched off for this install. Nothing to do.')
        return {'provision': {'ok': False, 'state': 'disabled', 'reason': ''},
                'scanned': 0, 'created': 0, 'adopted': 0, 'skipped': 0,
                'no_account': 0, 'no_period': 0, 'failed': 0, 'reconciled': 0,
                'errors': []}

    report = es.provision_report(client)
    if report['ok']:
        if report['state'] == es.PROVISION_CREATED:
            print(f'  ✓ created the {es.BANK_STATEMENT_DT} doctype.')
        else:
            print(f'  ✓ the {es.BANK_STATEMENT_DT} doctype is already present.')
    else:
        print(f'  ! the {es.BANK_STATEMENT_DT} doctype is NOT available '
              f"({report['state']}).")
        help_text = es.PROVISION_HELP.get(report['state'])
        if help_text:
            print(f'    {help_text}')
        if report['reason']:
            print(f'    ERPNext said: {report["reason"]}')
        return {**es.blank_stats(), 'provision': report}

    pending = es.pending_statements()
    if not pending:
        print('  · every local statement is already in ERPNext.')
    elif verbose:
        print(f'  · {len(pending)} statement(s) not yet in ERPNext:')

    stats = es.blank_stats()
    # Counted BEFORE the loop: these are the ones a previous run already landed.
    stats['already_synced'] = len(es.synced_statements())
    for statement in pending:
        stats['scanned'] += 1
        result = es.sync_statement(client, statement, dry_run=dry_run)
        action = result['action']
        if action in stats:
            stats[action] += 1
        if action == 'failed' and result['reason']:
            stats['errors'].append(f"{statement.statement_id}: {result['reason']}")
        if verbose:
            label = statement.period_label() or statement.statement_id
            prefix = 'WOULD ' if dry_run and action == 'created' else ''
            print(f'    {label}: {prefix}{_ACTION_LABELS.get(action, action)}')
            if result['reason'] and action != 'failed':
                print(f'      {result["reason"]}')
            elif result['reason']:
                print(f'      ERPNext said: {result["reason"]}')

    # Refresh verdicts on already-synced records. Skipped on a dry run: it is
    # the one part of this script that writes without creating anything.
    if not dry_run:
        rows = {(r.get(es.MARKER_FIELD) or '').strip(): r
                for r in (es.list_records(client) or [])}
        for statement in es.synced_statements():
            if es.push_reconciliation(
                    client, statement,
                    existing=rows.get(statement.statement_id)) == 'updated':
                stats['reconciled'] += 1
        if stats['reconciled']:
            print(f"  · refreshed the reconciliation verdict on "
                  f"{stats['reconciled']} existing record(s).")

    return {**stats, 'provision': report}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Upload locally-held bank statements into ERPNext as Bank '
                    'Statement records (idempotent).')
    parser.add_argument('--dry-run', action='store_true',
                        help='report what would be uploaded; write nothing')
    parser.add_argument('--quiet', action='store_true',
                        help='summary only, no per-statement lines')
    args = parser.parse_args(argv)

    from app import create_app
    from app.sync_engine import get_erp_client_or_none

    # SCHEDULER_ENABLED off: this is a one-shot CLI, and letting it elect the
    # container's scheduler would leave a background thread running behind it.
    app = create_app({'SCHEDULER_ENABLED': False})
    with app.app_context():
        client = get_erp_client_or_none()
        if client is None:
            print('backfill_erpnext_statements: ERPNext is not configured — '
                  'check the connection on /admin/erpnext_settings and re-run.')
            return 1
        if args.dry_run:
            print('backfill_erpnext_statements: DRY RUN — nothing will be '
                  'created or uploaded.\n')
        print('backfill_erpnext_statements: syncing…')
        stats = run(client, dry_run=args.dry_run, verbose=not args.quiet)

    print()
    if not stats['provision']['ok']:
        print('backfill_erpnext_statements: the doctype is unavailable, so no '
              'statements were uploaded.')
        return 1
    verb = 'would create' if args.dry_run else 'created'
    print(f"backfill_erpnext_statements: {verb} {stats['created']}, "
          f"adopted {stats['adopted']}, already synced "
          f"{stats['already_synced']}, failed {stats['failed']}.")
    if stats['no_account']:
        print(f"{stats['no_account']} statement(s) skipped because their Plaid "
              f"account has no ERPNext Bank Account — import the account on "
              f"/admin/accounts and re-run.")
    if stats['no_period']:
        print(f"{stats['no_period']} statement(s) skipped for having no period "
              f"bounds (Plaid gave no month/year).")
    for line in stats['errors']:
        print(f'  ! {line}')
    if args.dry_run:
        print('Re-run without --dry-run to do it.')
    else:
        print('Verify in ERPNext: Awesome Bar → "Bank Statement List".')
    return 1 if stats['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
