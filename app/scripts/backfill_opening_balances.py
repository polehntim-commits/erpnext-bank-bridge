# SPDX-License-Identifier: MIT
"""One-shot, idempotent: book opening balances for accounts linked BEFORE v0.4.2.

From v0.4.2 on, importing a Plaid account books what it already held at that
moment (see app/opening_balance.py). Accounts linked before that have no such
entry, so ERPNext reports their movement-since-link instead of their position —
which shows up as a negative asset on any account whose recent activity is
net-negative:

    Wells Fargo Money Market   ERPNext says  -17,550.00
                               actually holds     50.00

This script closes that gap for the accounts already in the database. It cannot
know the true opening balance — Plaid only hands back the CURRENT one — so it
works backwards from what Bank Bridge has mirrored:

    opening = current Plaid balance − everything we have seen move since

with the per-type sign handling that "everything we have seen move" requires
(a purchase lowers a checking balance but RAISES a credit card's), implemented
once in opening_balance.estimate_opening_balance and shared with the tests.

That makes every number here an ESTIMATE, and its accuracy is bounded by how far
back Plaid's transaction history reached when the account was linked — typically
30–90 days. If the account had activity before that window, the estimate absorbs
it. This is exactly why the entries land in `pending_review`: the operator checks
each one against a real statement and rejects the ones that are off, then re-books
those with the true figure from /admin/accounts.

Idempotent by construction, not by a flag: an opening balance claims the UNIQUE
key `opening-balance:<account_id>` on generated_journal_entries, so an account
that already has one — booked automatically, by hand, or by an earlier run of
this script — is skipped. A rejected entry is skipped too, since rejecting one
is a decision, and this script re-running must not overturn it.

Runs in the BANK BRIDGE container (it needs Bank Bridge's own database), not in
the ERPNext bench. Dry-run first — it prints every entry it would create:

    docker exec <bankbridge_container> python -m scripts.backfill_opening_balances --dry-run
    docker exec <bankbridge_container> python -m scripts.backfill_opening_balances

Then approve the resulting Drafts on /admin/generated_entries.
"""
from __future__ import annotations

import sys


def _plan(account, transactions) -> dict:
    """What this account's backfill would do, as a printable row. Pure — the
    dry-run and the real run compute this identically, so what --dry-run shows
    is exactly what the real run books."""
    from app import opening_balance as obal

    estimate = obal.estimate_opening_balance(account, transactions)
    counted = [t for t in transactions if not t.pending and not t.removed]
    return {
        'account_id': account.account_id,
        'label': account.name or account.official_name or account.mask
        or account.account_id,
        'type': f"{account.type or '?'}/{account.subtype or '?'}",
        'current_balance': float(account.balance_current or 0.0),
        'transaction_total': round(sum(float(t.amount or 0.0)
                                       for t in counted), 2),
        'transaction_count': len(counted),
        'estimate': estimate,
        'debit_own_account': obal.opening_balance_direction(account, estimate),
    }


def _formula(plan: dict) -> str:
    """The arithmetic behind one estimate, spelled out so the operator can check
    it against a statement without reading this file."""
    sign = '+' if plan['estimate'] >= plan['current_balance'] else '-'
    return (f"{plan['current_balance']:,.2f} (current) {sign} "
            f"{abs(round(plan['estimate'] - plan['current_balance'], 2)):,.2f} "
            f"({plan['transaction_count']} mirrored transaction(s)) = "
            f"{plan['estimate']:,.2f}")


def run(client, *, dry_run: bool = False) -> dict:
    """Estimate and book an opening balance for every linked account that has
    none. Returns aggregate stats plus the per-account plans, so a caller (and
    the test suite) can assert on what it decided without parsing stdout."""
    from app import db, opening_balance as obal
    from app.models import BankTransaction, PlaidAccount

    stats = {'considered': 0, 'booked': [], 'skipped': [], 'failed': [],
             'plans': []}
    accounts = PlaidAccount.query.order_by(PlaidAccount.name).all()
    for account in accounts:
        if not (account.erpnext_gl_account_name or '').strip():
            stats['skipped'].append(
                (account.account_id, 'no GL Account linked — import it first'))
            continue
        existing = obal.existing_entry(account)
        if existing is not None:
            stats['skipped'].append(
                (account.account_id,
                 f'already has an opening balance ({existing.state})'))
            continue
        stats['considered'] += 1
        transactions = (BankTransaction.query
                        .filter_by(account_id=account.account_id).all())
        plan = _plan(account, transactions)
        stats['plans'].append(plan)
        side = 'Dr own account' if plan['debit_own_account'] else 'Cr own account'
        print(f"  {plan['label']} ({plan['type']}): {_formula(plan)}  [{side}]")
        if dry_run:
            continue
        if round(abs(plan['estimate']), 2) < 0.005:
            stats['skipped'].append(
                (account.account_id, 'estimated opening balance is zero'))
            print('    → skipped (estimate is zero — nothing to book)')
            continue
        result = obal.book_opening_balance(client, account,
                                           amount=plan['estimate'])
        if result['status'] == 'booked':
            stats['booked'].append((account.account_id, plan['estimate'],
                                    result['journal_entry']))
            print(f"    → {result['message']}")
        elif result['status'] == 'error':
            stats['failed'].append((account.account_id, result['message']))
            print(f"    ! {result['message']}")
        else:
            stats['skipped'].append((account.account_id, result['message']))
            print(f"    → skipped ({result['message']})")
    if not dry_run:
        db.session.commit()
    return stats


def main() -> int:
    from app import create_app
    from app.sync_engine import get_erp_client_or_none

    dry_run = '--dry-run' in sys.argv[1:]
    app = create_app({'SCHEDULER_ENABLED': False})
    with app.app_context():
        client = None
        if not dry_run:
            client = get_erp_client_or_none()
            if client is None:
                print('backfill_opening_balances: ERPNext is not configured — '
                      'check the connection on /admin/erpnext_settings and '
                      're-run. (--dry-run needs no connection.)')
                return 1
        if dry_run:
            print('backfill_opening_balances: DRY RUN — nothing will be '
                  'created in ERPNext.\n')
        result = run(client, dry_run=dry_run)
    print()
    if dry_run:
        print(f'backfill_opening_balances: would book '
              f'{result["considered"]} opening balance(s); '
              f'{len(result["skipped"])} account(s) skipped. '
              f'Re-run without --dry-run to create them.')
        return 0
    print(f'backfill_opening_balances: {len(result["booked"])} opening '
          f'balance(s) booked pending review, '
          f'{len(result["skipped"])} skipped, '
          f'{len(result["failed"])} failed.')
    if result['booked']:
        print('Approve them on /admin/generated_entries once you have checked '
              'each estimate against a statement.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
