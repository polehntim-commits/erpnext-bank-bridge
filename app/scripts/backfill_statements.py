# SPDX-License-Identifier: MIT
"""One-shot, idempotent: pull bank statements for accounts already linked.

From v0.4.9 on, importing a Plaid account fetches its oldest bank statement, and
a scheduled job picks up new ones monthly (see app/statements.py). Accounts
linked before that — or linked before `statements` was approved on the Plaid
application — have none, so /admin/statements is empty for them and their
opening balances are still the v0.4.4 arithmetic estimate.

This script closes both gaps in one pass:

  1. PULL. Lists every statement Plaid holds for each connected Item and
     downloads the ones not already stored, parsing opening and closing balances
     out of each PDF.
  2. RE-BOOK (optional, --rebook). For any account whose opening balance is
     still an estimate, re-books it from a statement the bank issued — but only
     when that statement RECONCILES against the mirrored transactions, which is
     the check statements.choose_anchor_statement exists to make. A statement
     the mirror cannot reproduce is left alone and the estimate stands.

Why --rebook is opt-in rather than automatic: re-booking replaces an entry an
operator may already have reviewed, and it moves the posting date to the
statement's period start. That is a real change to the books, so it is a
decision, not a side effect of fetching PDFs.

An already-APPROVED opening balance is never touched. Approving one is a
decision, and the whole point of the pending_review workflow is that nothing
overturns it silently.

Idempotent on both halves: a statement whose row exists and whose PDF is on disk
is skipped without a download (statement_id is UNIQUE), and an account whose
opening balance already came from a statement is left alone.

Runs in the BANK BRIDGE container (it needs Bank Bridge's own database), not in
the ERPNext bench. Dry-run first — it prints everything it would do:

    docker exec <bankbridge_container> python -m scripts.backfill_statements --dry-run
    docker exec <bankbridge_container> python -m scripts.backfill_statements
    docker exec <bankbridge_container> python -m scripts.backfill_statements --rebook

Then approve any resulting Drafts on /admin/generated_entries.
"""
from __future__ import annotations

import sys


def pull(*, dry_run: bool = False) -> dict:
    """Download every statement Plaid has that we don't already hold.

    A dry run LISTS but does not download: listing is a read-only Plaid call, so
    it can honestly report what a real run would fetch without writing a byte."""
    from app import statements
    from app.models import PlaidItem

    if not statements.is_enabled():
        print('  statements are disabled (STATEMENTS_ENABLED=false) — nothing '
              'to do.')
        return statements._blank_stats()

    if dry_run:
        stats = statements._blank_stats()
        client = statements._plaid_client_or_none()
        if client is None:
            print('  Plaid is not configured — check /admin/plaid_settings.')
            return stats
        from app import crypto
        items = PlaidItem.query.filter(
            PlaidItem.disconnected.is_(False)).order_by(PlaidItem.id).all()
        from app.models import PlaidStatement
        for item in items:
            try:
                token = crypto.decrypt(item.access_token_encrypted)
            except Exception as e:
                print(f'  {item.institution_name or item.item_id}: token '
                      f'unreadable ({e})')
                continue
            listed = client.statements_list(token)
            stats['items'] += 1
            stats['listed'] += len(listed)
            held = 0
            for row in listed:
                existing = PlaidStatement.query.filter_by(
                    statement_id=row['statement_id']).first()
                if existing is not None and statements.pdf_exists(existing):
                    held += 1
            stats['skipped_existing'] += held
            stats['stored'] += len(listed) - held
            print(f'  {item.institution_name or item.item_id}: '
                  f'{len(listed)} statement(s) available, {held} already '
                  f'stored, would download {len(listed) - held}')
        return stats

    stats = statements.fetch_all()
    print(f"  listed {stats['listed']}, stored {stats['stored']}, "
          f"already had {stats['skipped_existing']}, "
          f"failed {stats['failed']}")
    for err in stats['errors']:
        print(f'  ! {err}')
    return stats


def rebook(client, *, dry_run: bool = False) -> dict:
    """Re-book opening balances from a reconciling bank statement.

    Only touches an account whose existing opening balance is absent or still
    `pending_review` — an approved entry is a decision already made, and a
    rejected one is too (the operator said no; re-booking would overturn it)."""
    from app import db, opening_balance as obal, statements
    from app.models import PlaidAccount

    stats = {'considered': 0, 'rebooked': [], 'skipped': [], 'failed': []}
    for account in PlaidAccount.query.order_by(PlaidAccount.name).all():
        label = (account.name or account.official_name or account.mask
                 or account.account_id)
        if not (account.erpnext_gl_account_name or '').strip():
            stats['skipped'].append((account.account_id, 'no GL Account linked'))
            continue
        existing = obal.existing_entry(account)
        if existing is not None and existing.state != 'pending_review':
            stats['skipped'].append(
                (account.account_id,
                 f'opening balance is {existing.state} — leaving it alone'))
            continue
        anchor = statements.anchor_for(account)
        if anchor is None:
            stats['skipped'].append(
                (account.account_id,
                 'no statement reconciles against the mirrored transactions'))
            print(f'  {label}: no usable statement — the estimate stands')
            continue
        stats['considered'] += 1
        amount, when, statement = anchor
        print(f'  {label}: statement {statement.period_label()} opens at '
              f'{amount:,.2f} (posting {when})')
        if dry_run:
            continue
        if existing is not None:
            # Clear the estimate so book_opening_balance writes a fresh entry
            # rather than short-circuiting on "already booked". The row itself is
            # re-pointed, not duplicated — the synthetic key is UNIQUE.
            existing.state = 'rejected'
            db.session.commit()
        result = obal.book_opening_balance(client, account, force=True,
                                          prefer_statement=True)
        if result['status'] == 'booked':
            stats['rebooked'].append((account.account_id, amount,
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

    args = sys.argv[1:]
    dry_run = '--dry-run' in args
    do_rebook = '--rebook' in args

    app = create_app({'SCHEDULER_ENABLED': False})
    with app.app_context():
        if dry_run:
            print('backfill_statements: DRY RUN — nothing will be downloaded '
                  'or created.\n')
        print('Pulling statements from Plaid:')
        pulled = pull(dry_run=dry_run)

        rebooked = None
        if do_rebook:
            client = None
            if not dry_run:
                client = get_erp_client_or_none()
                if client is None:
                    print('\nbackfill_statements: ERPNext is not configured — '
                          'statements were pulled, but opening balances cannot '
                          'be re-booked. Check /admin/erpnext_settings.')
                    return 1
            print('\nRe-booking opening balances from statements:')
            rebooked = rebook(client, dry_run=dry_run)

    print()
    if dry_run:
        print(f"backfill_statements: would download {pulled['stored']} "
              f"statement(s).")
        if rebooked is not None:
            print(f"Would re-book {rebooked['considered']} opening balance(s) "
                  f"from a reconciling statement.")
        print('Re-run without --dry-run to do it.')
        return 0
    print(f"backfill_statements: {pulled['stored']} statement(s) stored, "
          f"{pulled['skipped_existing']} already held, "
          f"{pulled['failed']} failed.")
    if rebooked is not None:
        print(f"{len(rebooked['rebooked'])} opening balance(s) re-booked from "
              f"a bank statement, {len(rebooked['skipped'])} skipped, "
              f"{len(rebooked['failed'])} failed.")
        if rebooked['rebooked']:
            print('Approve them on /admin/generated_entries.')
    if not do_rebook:
        print('Review them on /admin/statements. Add --rebook to also re-book '
              'opening balances from a statement where one reconciles.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
