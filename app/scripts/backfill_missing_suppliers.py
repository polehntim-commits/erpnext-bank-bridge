# SPDX-License-Identifier: MIT
"""One-shot, idempotent: unblock Journal Entries that failed on a missing Party
Supplier (v0.4.0.7).

Before v0.4.0.7 the auto-Supplier only fired for a transaction Plaid gave a
`merchant_name`. A rule that named a Party for a DESCRIPTION-only transaction
(an interest payment, a card payment, a payroll ACH) therefore put a party on
the Journal Entry that ERPNext had no Supplier for, and refused the document:

    LinkValidationError: Could not find Row #1: Party: Wells Fargo
    POST /api/resource/Journal Entry -> 417

The code fix means this can't recur. This script cleans up the rows already
stuck in `error`:

  1. find every `error` GeneratedJournalEntry whose message is a Party
     LinkValidationError, and pull the party name out of it;
  2. ensure each distinct party has an ERPNext Supplier (find-or-create, with
     the same Supplier Group logic the live path uses);
  3. re-run the rules engine for that transaction, which now builds the JE with
     a party ERPNext accepts and lands the row back in `pending_review`.

Step 3 is deliberately a real re-generation rather than a bare state flip: a
failed row has no `erpnext_journal_entry_name`, so setting it to
`pending_review` on its own would leave an "approvable" row with no Journal
Entry behind it — Approve would then fail. Re-running produces the actual Draft.

Idempotent: a second run finds no `error` rows left to fix and changes nothing.
Rows whose bank transaction is gone, or that fail again for an unrelated reason,
are left in `error` and reported.

Note this runs in the BANK BRIDGE container (it needs Bank Bridge's own
database), not in the ERPNext bench:

    docker exec <bankbridge_container> python -m scripts.backfill_missing_suppliers

Nothing here is required after upgrading — clicking "Rerun rules" on
/admin/transactions does the same work through the UI. This is the scripted
equivalent for an operator who would rather not click.
"""
from __future__ import annotations

import re
import sys

# "Could not find Row #1: Party: Wells Fargo" — Frappe's LinkValidationError for
# an unknown party link. The row prefix is optional (the message shape differs
# between a row-level and a document-level validation), and the name runs to the
# end of the line or the end of the quoted JSON string.
_PARTY_ERROR_RE = re.compile(
    r'could not find\s+(?:row\s*#\d+:\s*)?party:\s*(?P<name>[^"\\\n}]+)', re.I)


def party_name_from_error(message: str) -> str:
    """The party name a Party LinkValidationError names, or '' when `message`
    isn't one. Trailing punctuation Frappe wraps the body in is stripped."""
    m = _PARTY_ERROR_RE.search(message or '')
    if not m:
        return ''
    return m.group('name').strip().strip('\'"').rstrip('.,;: ').strip()


def stuck_entries():
    """Every `error` GeneratedJournalEntry whose failure was a missing Party
    Supplier, as (entry, party_name) pairs. Rows that failed for any other
    reason are not this script's business and are left alone."""
    from app.models import GeneratedJournalEntry
    out = []
    for g in GeneratedJournalEntry.query.filter_by(state='error').all():
        party = party_name_from_error(g.error_message or '')
        if party:
            out.append((g, party))
    return out


def run(client) -> dict:
    """Create the missing Suppliers and re-generate their Journal Entries.
    Returns a summary dict. `client` is an ERPNextClient (or a test double)."""
    from app import categorization, db, erpnext_bank
    from app.models import BankTransaction

    pairs = stuck_entries()
    suppliers_created, regenerated, still_failing = [], [], []
    ensured: dict[str, str | None] = {}      # party name -> docname (dedup)

    for g, party in pairs:
        if party not in ensured:
            # source='' keeps the configured default Supplier Group unless the
            # name is recognisably an institution/payroll party — the live path
            # derives that from the transaction, which we do below.
            row = BankTransaction.query.filter_by(
                plaid_transaction_id=g.plaid_transaction_id).first()
            source = ''
            if row is not None:
                derived, derived_source = \
                    erpnext_bank.derive_party_from_transaction(row)
                if derived == party:
                    source = derived_source
            docname = erpnext_bank.ensure_supplier(client, party, source=source)
            ensured[party] = docname
            if docname:
                suppliers_created.append(party)
                print(f'  Supplier ensured: {party!r} → {docname!r}')
            else:
                print(f'  ! could not create Supplier {party!r}')
        if not ensured[party]:
            still_failing.append((g.plaid_transaction_id, 'no Supplier'))
            continue
        row = BankTransaction.query.filter_by(
            plaid_transaction_id=g.plaid_transaction_id).first()
        if row is None:
            still_failing.append((g.plaid_transaction_id, 'transaction gone'))
            print(f'  ! {g.plaid_transaction_id}: bank transaction no longer '
                  'available; left in error')
            continue
        categorization.generate_journal_entry(client, row)
        db.session.refresh(g)
        if g.state == 'error':
            still_failing.append((g.plaid_transaction_id,
                                  (g.error_message or '')[:120]))
            print(f'  ! {g.plaid_transaction_id}: still failing — '
                  f'{(g.error_message or "")[:120]}')
        else:
            regenerated.append(g.plaid_transaction_id)
            print(f'  {g.plaid_transaction_id}: → {g.state} '
                  f'({g.erpnext_journal_entry_name})')
    return {'considered': len(pairs),
            'suppliers_ensured': suppliers_created,
            'regenerated': regenerated,
            'still_failing': still_failing}


def main() -> int:
    from app import create_app
    from app.sync_engine import get_erp_client_or_none

    app = create_app({'SCHEDULER_ENABLED': False})
    with app.app_context():
        client = get_erp_client_or_none()
        if client is None:
            print('backfill_missing_suppliers: ERPNext is not configured — '
                  'check the connection on /admin/settings and re-run.')
            return 1
        result = run(client)
    print(f'backfill_missing_suppliers: {result["considered"]} stuck entr(ies), '
          f'{len(result["suppliers_ensured"])} Supplier(s) ensured, '
          f'{len(result["regenerated"])} Journal Entr(ies) regenerated, '
          f'{len(result["still_failing"])} still failing.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
