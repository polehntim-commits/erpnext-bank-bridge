# SPDX-License-Identifier: MIT
"""One-shot, idempotent: repair Journal Entries that booked a SUPPLIER against an
INCOME account (v0.4.0.8).

Everything through v0.4.0.7 was Accounts Payable: the only party Bank Bridge
would ever auto-create was a Supplier. A rule that categorized money coming IN —
a fruit-buyer deposit, a USDA/FSA payment, a grant, lease revenue — therefore
booked its counterparty as a Supplier even though the offset was an Income
account. That is wrong three ways:

  * the party sits on the AP ledger when the activity is AR;
  * the 1099-NEC vendor list fills up with people who are customers;
  * the party is semantically miscategorized for every downstream report.

v0.4.0.8 fixes the code path (party_type='Auto' derives Customer from an Income
offset — see app/categorization.py). This script cleans up what already landed:

  1. find every generated Journal Entry whose rule has an INCOME offset account
     but whose posted JE carries a `party_type` of Supplier;
  2. report each mismatch;
  3. ensure a matching ERPNext Customer exists for that party name;
  4. repoint the JE's party to the Customer — but ONLY while the JE is still a
     Draft. A submitted Journal Entry is immutable in Frappe, so an already-
     approved one is reported and left alone for the operator to cancel and
     re-book by hand; silently amending posted books is not this script's call.

Idempotent on every step: ensure_customer is find-or-create, and a repointed
Draft no longer reads as a Supplier mismatch on the next run. Re-running only
ever re-reports the submitted JEs it cannot touch.

Runs in the BANK BRIDGE container (it needs Bank Bridge's own database), not in
the ERPNext bench:

    docker exec <bankbridge_container> python -m scripts.backfill_customer_records

Flags:
    --dry-run      report the mismatches and change nothing at all.
    --no-repoint   create the Customers but leave every JE untouched.
"""
from __future__ import annotations

import sys

JOURNAL_ENTRY_DT = 'Journal Entry'


def _income_offset_rules(client, company_by_rule: dict) -> dict:
    """{rule_id: offset_account} for every rule whose offset account is an
    ERPNext INCOME account. Rules with no offset (the deprecated pre-v0.3.1
    debit/credit pair) and rules whose root_type can't be read are skipped —
    an undeterminable root_type means "don't guess", same as at JE time.

    Archived rules are included on purpose: a JE generated months ago was booked
    by whatever version of the rule was live then, and that row is usually
    archived by now."""
    from app import erpnext_bank
    from app.models import CategorizationRule

    out = {}
    root_cache: dict[tuple, str] = {}
    for rule in CategorizationRule.query.all():
        offset = (rule.offset_account or '').strip()
        if not offset:
            continue
        company = company_by_rule.get(rule.id, '')
        key = (offset, company)
        if key not in root_cache:
            root_cache[key] = erpnext_bank.root_type_for_account(
                client, offset, company)
        if root_cache[key] == 'Income':
            out[rule.id] = offset
    return out


def _supplier_parties(doc) -> list:
    """The (row_index, party) pairs on a Journal Entry doc that are booked to a
    Supplier. Empty when the doc carries no Supplier party at all."""
    if not isinstance(doc, dict):
        return []
    out = []
    for i, line in enumerate(doc.get('accounts') or []):
        if not isinstance(line, dict):
            continue
        if (line.get('party_type') or '').strip() == 'Supplier' and line.get('party'):
            out.append((i, (line.get('party') or '').strip()))
    return out


def mismatches(client) -> list:
    """Every generated Journal Entry that booked a Supplier against an Income
    offset, as a list of dicts:

        {'gje', 'je_name', 'rule_id', 'offset_account', 'party',
         'row_index', 'submitted'}

    Reads the party from the POSTED ERPNext document rather than re-deriving it
    locally, so what we report is what is actually on the books."""
    from app import erpnext_accounts
    from app.models import GeneratedJournalEntry

    posted = [g for g in GeneratedJournalEntry.query.all()
              if g.erpnext_journal_entry_name and g.rule_id]
    if not posted:
        return []
    # The Company each rule's offset must be read under — a Mode B (agnostic)
    # rule names a LOGICAL account, which only resolves within a Company.
    company_by_rule: dict = {}
    for g in posted:
        from app.models import BankTransaction
        row = BankTransaction.query.filter_by(
            plaid_transaction_id=g.plaid_transaction_id).first()
        company = erpnext_accounts.owning_company_for_account_id(
            getattr(row, 'account_id', None)) if row is not None else ''
        company_by_rule.setdefault(g.rule_id, company or '')

    income_rules = _income_offset_rules(client, company_by_rule)
    if not income_rules:
        return []

    found = []
    for g in posted:
        if g.rule_id not in income_rules:
            continue
        try:
            doc = client.get_doc(JOURNAL_ENTRY_DT, g.erpnext_journal_entry_name)
        except Exception as e:                      # noqa: BLE001 - report, skip
            print(f'  ! {g.erpnext_journal_entry_name}: could not read — {e}')
            continue
        for idx, party in _supplier_parties(doc):
            found.append({
                'gje': g,
                'je_name': g.erpnext_journal_entry_name,
                'rule_id': g.rule_id,
                'offset_account': income_rules[g.rule_id],
                'party': party,
                'row_index': idx,
                # docstatus 1 = submitted, and therefore immutable in Frappe.
                'submitted': int(doc.get('docstatus') or 0) == 1,
                'doc': doc,
            })
    return found


def _repoint(client, hit, customer_docname: str) -> bool:
    """Repoint one Draft Journal Entry row from its Supplier to `customer_docname`.
    Returns True on success. A submitted JE is never touched."""
    doc = hit['doc']
    accounts = [dict(line) for line in (doc.get('accounts') or [])]
    accounts[hit['row_index']]['party_type'] = 'Customer'
    accounts[hit['row_index']]['party'] = customer_docname
    client.update_doc(JOURNAL_ENTRY_DT, hit['je_name'], {'accounts': accounts})
    return True


def run(client, *, dry_run: bool = False, repoint: bool = True) -> dict:
    """Report the Supplier-on-Income mismatches, create the matching Customers,
    and repoint the Draft Journal Entries. Returns a summary dict.
    `client` is an ERPNextClient (or a test double)."""
    from app import erpnext_bank

    hits = mismatches(client)
    if not hits:
        print('backfill_customer_records: no Supplier-on-Income mismatches found.')
        return {'considered': 0, 'customers_ensured': [], 'repointed': [],
                'submitted_skipped': [], 'failed': []}

    print(f'backfill_customer_records: {len(hits)} mismatch(es) found:')
    for h in hits:
        state = 'submitted' if h['submitted'] else 'draft'
        print(f'  {h["je_name"]} ({state}): party {h["party"]!r} booked as '
              f'Supplier against Income account {h["offset_account"]!r}')
    if dry_run:
        print('backfill_customer_records: --dry-run, nothing changed.')
        return {'considered': len(hits), 'customers_ensured': [],
                'repointed': [], 'submitted_skipped': [], 'failed': [],
                'dry_run': True}

    ensured: dict = {}          # party name -> Customer docname (dedup)
    customers, repointed, submitted_skipped, failed = [], [], [], []
    for h in hits:
        party = h['party']
        if party not in ensured:
            docname = erpnext_bank.ensure_customer(client, party,
                                                   source='backfill')
            ensured[party] = docname
            if docname:
                customers.append(party)
                print(f'  Customer ensured: {party!r} → {docname!r}')
            else:
                print(f'  ! could not create Customer {party!r}')
        docname = ensured[party]
        if not docname:
            failed.append((h['je_name'], 'no Customer'))
            continue
        if h['submitted']:
            # Immutable in Frappe. Reported, never silently amended.
            submitted_skipped.append((h['je_name'], party))
            print(f'  · {h["je_name"]} is submitted — Customer {docname!r} '
                  'created, but the posted entry still names the Supplier. '
                  'Cancel + re-book it in ERPNext to move it to AR.')
            continue
        if not repoint:
            continue
        try:
            _repoint(client, h, docname)
        except Exception as e:                      # noqa: BLE001 - report, skip
            failed.append((h['je_name'], str(e)[:120]))
            print(f'  ! {h["je_name"]}: repoint failed — {str(e)[:120]}')
            continue
        repointed.append(h['je_name'])
        print(f'  {h["je_name"]}: party → Customer {docname!r}')

    return {'considered': len(hits), 'customers_ensured': customers,
            'repointed': repointed, 'submitted_skipped': submitted_skipped,
            'failed': failed}


def main(argv=None) -> int:
    from app import create_app
    from app.sync_engine import get_erp_client_or_none

    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = '--dry-run' in argv
    repoint = '--no-repoint' not in argv

    app = create_app({'SCHEDULER_ENABLED': False})
    with app.app_context():
        client = get_erp_client_or_none()
        if client is None:
            print('backfill_customer_records: ERPNext is not configured — '
                  'check the connection on /admin/settings and re-run.')
            return 1
        result = run(client, dry_run=dry_run, repoint=repoint)
    print(f'backfill_customer_records: {result["considered"]} mismatch(es), '
          f'{len(result["customers_ensured"])} Customer(s) ensured, '
          f'{len(result["repointed"])} Journal Entr(ies) repointed, '
          f'{len(result["submitted_skipped"])} submitted (left for review), '
          f'{len(result["failed"])} failed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
