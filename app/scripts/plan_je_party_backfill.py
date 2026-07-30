# SPDX-License-Identifier: MIT
"""PLAN the v0.9.0 Party repair of already-generated bank-transaction Journal
Entries. Phase 1 of 2 — this script WRITES NOTHING ANYWHERE.

    THE GAP IT REPAIRS.  Tim drilled into ACC-JV-2026-02312 (Sorren, $2,030) on
    2026-07-29 and found Party Type and Party EMPTY on both lines. The ledger
    could not answer "who did we pay?" — no supplier-wise spend report, no 1099
    pre-fill, no vendor reconciliation, no supplier ledger drilldown.

    Two separate causes, and only one of them is repairable:

      1. **v0.4.0.9's eligibility rule was NARROWER THAN ERPNEXT'S.** It allowed
         a Party only on ('Income','Receivable') and ('Expense','Payable'), so
         it refused every EQUITY account (ERPNext allows those) and every
         account whose `account_type` is simply BLANK (ERPNext skips the check
         entirely — the guard is `if account_type and ...`). On this install
         that is 12 already-submitted JE lines on Equity accounts and 29 of ~60
         leaf accounts. v0.9.0 fixes the rule; those entries are REPAIRABLE and
         are what this plan is for.

      2. **Sorren's own offset is genuinely ineligible.** '6400 - Professional
         Services - OML' carries `account_type='Expense Account'`, set
         explicitly, and ERPNext throws on a Party there at submit
         (erpnext/accounts/party.py::validate_account_party_type). No backfill
         can fix that entry as booked — it lands in `review` under
         `offset_ineligible`, which names the three real ways out: point the
         rule at an untyped expense account, clear the account_type on 6400, or
         route the spend through '2110 - Creditors - OML' as AP.

DRAFTS AND SUBMITTED ENTRIES ARE PLANNED SEPARATELY (v0.9.1). `party` and
`party_type` on a Journal Entry Account are NOT `allow_on_submit` — verified on
the live site:

    frappe.get_meta("Journal Entry Account").get_field("party").allow_on_submit
    → 0

`cost_center` IS, which is why v0.8.5's cost-center repair could go through the
ordinary write path and this one cannot. So a submitted entry needs the same
targeted two-table DB write v0.8.5 used, and it is gated behind its own flag:
this plan puts drafts in `changes` and submitted entries in `submitted_changes`,
and the applier will not touch the second list without `--include-submitted`.

v0.9.0 REPORTED the submitted entries rather than planning them, reasoning that
writing both tables meant "re-deriving party GL entries under a submitted
voucher." That was wrong about the mechanism, and the correction is the whole of
v0.9.1: `party_type` and `party` are COLUMNS ON GL ENTRY ROWS THAT ALREADY
EXIST. ERPNext stamps `voucher_detail_no` on every GL row when it posts, so the
rows this JE line produced are addressable by name and nothing is re-derived —
it is one column on the child row and the same column on its GL rows, the
identical shape to the cost-center repair. What v0.9.0 got RIGHT is that both
tables must move together: every supplier-wise report reads `tabGL Entry`, so
writing the JE child row alone would make the form look repaired while Accounts
Payable and the supplier ledger kept the empty answer — strictly worse than the
bug, because it hides itself. The applier writes both or neither.

Cancel + amend is still the right answer for anything this cannot reach — an
ineligible offset, a GL row that cannot be identified — and those keep landing
in `review` with the party the rules imply, so that decision stays informed.

WHY TWO SCRIPTS. The two facts needed to repair one line live in two different
databases. The RULE that matched a transaction — and the party it now names — is
in Bank Bridge's Postgres; the Journal Entry is in ERPNext's MariaDB. So this
script reads both sides (Postgres directly, ERPNext over REST) and emits a PLAN;
scripts/backfill_je_parties.py runs inside the ERPNext container and applies it.

The plan file is also the audit artifact. Per line it records the JE docname, the
child row, the offset account and its account_type, the party type and party
proposed, and the rule id that justifies it — reviewable before a single write
happens, and keepable afterwards as the record of why the books changed.

FAIL SAFE. Nothing here mutates anything: worst case is a stale plan, and the
applier re-checks every line against the live value before writing.

FAIL FORWARD. Every Journal Entry that CANNOT be repaired lands in `review` with
a reason — see REVIEW_REASONS. None is silently dropped. A rule whose party no
longer exists in ERPNext, an offset ERPNext will not accept a party on, a rule
whose party_type an older migration cleared: each is a thing an operator can act
on, and each is invisible if we only print the successes.

KAIROS. This is a one-time repair an operator runs when the rules are right, not
a scheduled job. Nothing calls it; there is no runtime tool for it.

    # inside the Bank Bridge container
    python3 -m scripts.plan_je_party_backfill --out /tmp/je_party_plan.json

    # review the summary it prints, then hand the plan to phase 2:
    #   scripts/backfill_je_parties.py
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

JOURNAL_ENTRY_DT = 'Journal Entry'

# Why a Journal Entry cannot be repaired. Each names something an operator can
# DO — a reason that only describes our confusion is not worth a line.
REVIEW_REASONS = {
    'no_rule_recorded': 'the JE records no rule_id — generated before the rules '
                        'engine, or by a path that does not name one',
    'rule_missing': 'the recorded rule id is gone from the rules table',
    'rule_has_no_party': 'the rule that matched (or the live rule that '
                         'superseded it) names no party and derives none — set '
                         'a Party Type / Party on the rule, then re-plan. NOTE '
                         'an early v0.4.0.9 migration CLEARED party_type off '
                         'rules pointing at Equity or untyped accounts, which '
                         'ERPNext would have accepted; those rules are here',
    'rule_skips_party': 'the rule sets Skip Party — it books a transfer between '
                        'accounts you own, which has no counterparty',
    'offset_ineligible': 'ERPNext refuses a Party on this offset account\'s '
                         'account_type. Point the rule at an account with no '
                         'account_type set, clear the account_type in ERPNext, '
                         'or route the spend through Accounts Payable',
    'offset_unresolvable': 'the offset account could not be read from ERPNext, '
                           'so its eligibility is unknown — nothing is proposed '
                           'rather than a party that may be refused at submit',
    'party_not_in_erpnext': 'the party the rule names does not exist in '
                            'ERPNext. Create it (or let the rule auto-create '
                            'it on the next fire), then re-plan',
    'je_not_found': 'ERPNext has no Journal Entry by that docname',
    'je_cancelled': 'the Journal Entry is cancelled (docstatus 2)',
    # LEGACY, plan_version 1 only. v0.9.0 routed every submitted entry here;
    # v0.9.1 routes them to `submitted_changes` instead, because the applier can
    # now write them. Kept because the applier still reads v1 plan files, and
    # because a reason an operator may find in an old plan needs an explanation.
    'submitted_not_writable': 'a plan_version 1 plan recorded this SUBMITTED '
                              'entry as unrepairable. v0.9.1 can repair it: '
                              're-plan and run the applier with '
                              '--include-submitted, or apply it by cancel + '
                              'amend',
    'offset_line_unidentified': 'no JE line matches the rule\'s offset account, '
                                'so which line should carry the party cannot be '
                                'decided from the data',
    'already_correct': 'the line already carries exactly this party',
}


def live_rule(rule):
    """Walk `superseded_by` forward to the rule version that is live today.

    An edit CLONES a rule and archives the original (see admin_ui.save_rule), so
    the version that generated a 2026-03 Journal Entry is usually NOT the version
    an operator has since given a party to. The live successor is the one whose
    party the books should now carry. Returns the live rule, or None if the chain
    dead-ends.

    Same helper, same reasoning, as plan_je_cost_center_backfill."""
    from app import db
    from app.models import CategorizationRule
    seen = set()
    while rule is not None and rule.superseded_by:
        if rule.id in seen:                 # a cycle can only be corruption
            return None
        seen.add(rule.id)
        rule = db.session.get(CategorizationRule, rule.superseded_by)
    return rule


def _je_lines(doc: dict) -> list:
    """The Journal Entry's account rows, as dicts."""
    return [ln for ln in (doc.get('accounts') or []) if isinstance(ln, dict)]


def _offset_line(doc: dict, offset_account: str, bank_account: str):
    """The line the party belongs on, or None when it cannot be identified.

    The offset (categorized) line, found by account docname. When the rule's
    offset is a Mode B LOGICAL name it will not match a docname, so the fallback
    is elimination: with exactly two lines, the one that is NOT the bank leg is
    the offset. No third guess — an unidentifiable line goes to review, because
    hanging a party off the wrong leg of a posted entry is worse than leaving it
    blank."""
    lines = _je_lines(doc)
    offset = (offset_account or '').strip()
    if offset:
        for ln in lines:
            if (ln.get('account') or '').strip() == offset:
                return ln
    bank = (bank_account or '').strip()
    if bank and len(lines) == 2:
        others = [ln for ln in lines
                  if (ln.get('account') or '').strip() != bank]
        if len(others) == 1:
            return others[0]
    return None


def _bank_gl_account(gje) -> str:
    """The bank-side GL account docname for this JE's transaction, '' when it
    can't be resolved. Read from the transaction's own Plaid account, which is
    where build_journal_entry took it from in the first place."""
    from app import categorization
    from app.models import BankTransaction
    row = BankTransaction.query.filter_by(
        plaid_transaction_id=gje.plaid_transaction_id).first()
    if row is None:
        return ''
    return categorization.bank_gl_account_for(row)


def _resolve_party(client, rule, company: str):
    """(party_type, party_name) the live rule wants, WITHOUT creating anything,
    or ('', '') when it wants none.

    Deliberately NOT `categorization.resolve_party`: that one ensures the party
    exists, which means creating Suppliers and Customers as a side effect of
    PLANNING. A planner that mutates ERPNext is not a planner."""
    from app import categorization
    if getattr(rule, 'skip_party', False):
        return '', ''
    party_type = categorization.effective_party_type(client, rule, company)
    if not party_type:
        return '', ''
    return party_type, (rule.party_name or '').strip()


def build_plan(client, *, limit: int | None = None) -> dict:
    """Read every rules-generated bank-transaction Journal Entry and return the
    plan dict. Pure read: `client` is only ever asked for documents."""
    from app import categorization, db, erpnext_accounts, erpnext_bank
    from app.models import BankTransaction, CategorizationRule
    from app.models import GeneratedJournalEntry

    q = (GeneratedJournalEntry.query
         .filter(GeneratedJournalEntry.erpnext_journal_entry_name.isnot(None),
                 GeneratedJournalEntry.erpnext_journal_entry_name != '',
                 # Investment JEs are posted by invest_je.py from per-Item
                 # dimensions, not from a categorization rule, so a rule lookup
                 # here would be meaningless. This column is the discriminator.
                 GeneratedJournalEntry.plaid_investment_transaction_id.is_(None))
         .order_by(GeneratedJournalEntry.id))
    if limit:
        q = q.limit(limit)

    changes, submitted_changes, review = [], [], []
    already_correct = 0
    examined = 0
    for gje in q.all():
        examined += 1
        je_name = gje.erpnext_journal_entry_name
        ctx = {'journal_entry': je_name,
               'plaid_transaction_id': gje.plaid_transaction_id,
               'rule_id': gje.rule_id, 'rule_name': gje.rule_name or ''}

        if not gje.rule_id:
            review.append({**ctx, 'reason': 'no_rule_recorded'})
            continue
        recorded = db.session.get(CategorizationRule, gje.rule_id)
        rule = live_rule(recorded)
        if rule is None:
            review.append({**ctx, 'reason': 'rule_missing'})
            continue
        ctx = {**ctx, 'live_rule_id': rule.id,
               'live_rule_name': rule.name or ''}
        if getattr(rule, 'skip_party', False):
            review.append({**ctx, 'reason': 'rule_skips_party'})
            continue

        row = BankTransaction.query.filter_by(
            plaid_transaction_id=gje.plaid_transaction_id).first()
        company = erpnext_accounts.owning_company_for_account_id(
            getattr(row, 'account_id', None)) if row is not None else ''

        party_type, party_name = _resolve_party(client, rule, company)
        if not party_type:
            review.append({**ctx, 'reason': 'rule_has_no_party'})
            continue

        # The offset account the JE actually names, resolved the same way the JE
        # path resolves it, so eligibility is tested against the real account.
        offset = (rule.offset_account or '').strip()
        is_agnostic = not (getattr(rule, 'applies_to_company', None) or '').strip()
        logical = (offset
                   if categorization.logical_account_name(offset) == offset
                   else '')
        resolved_offset = offset
        if is_agnostic and logical and company:
            resolved_offset = (erpnext_accounts.resolve_logical_account(
                client, logical, company) or offset)

        verdict, acct_type = categorization.party_eligibility(
            client, resolved_offset, company, party_type)
        if verdict == categorization.ELIGIBILITY_BLOCKED:
            review.append({**ctx, 'reason': 'offset_ineligible',
                           'offset_account': resolved_offset,
                           'offset_account_type': acct_type,
                           'party_type': party_type,
                           'party': party_name})
            continue
        if verdict == categorization.ELIGIBILITY_UNKNOWN:
            review.append({**ctx, 'reason': 'offset_unresolvable',
                           'offset_account': resolved_offset,
                           'party_type': party_type, 'party': party_name})
            continue

        # The party must ALREADY EXIST. This is a planner; it does not mint.
        docname = party_name
        if party_name:
            found = erpnext_bank.find_party_by_title(client, party_type,
                                                     party_name)
            docname = found or ''
        if not docname:
            review.append({**ctx, 'reason': 'party_not_in_erpnext',
                           'offset_account': resolved_offset,
                           'party_type': party_type,
                           'party': party_name})
            continue

        doc = client.get_doc(JOURNAL_ENTRY_DT, je_name)
        if not doc:
            review.append({**ctx, 'reason': 'je_not_found'})
            continue
        docstatus = int(doc.get('docstatus') or 0)
        if docstatus == 2:
            review.append({**ctx, 'reason': 'je_cancelled'})
            continue

        line = _offset_line(doc, resolved_offset, _bank_gl_account(gje))
        if line is None:
            review.append({**ctx, 'reason': 'offset_line_unidentified',
                           'offset_account': resolved_offset})
            continue

        current_type = (line.get('party_type') or '').strip()
        current_party = (line.get('party') or '').strip()
        if current_type == party_type and current_party == docname:
            already_correct += 1
            continue

        entry = {
            'journal_entry': je_name,
            'row': line.get('name') or '',
            'account': line.get('account') or '',
            'account_type': acct_type,
            'old_party_type': current_type,
            'old_party': current_party,
            'new_party_type': party_type,
            'new_party': docname,
            'rule_id': gje.rule_id,
            'live_rule_id': rule.id,
            'rule_name': rule.name or gje.rule_name or '',
            'plaid_transaction_id': gje.plaid_transaction_id,
            'docstatus': docstatus,
            # Carried so the applier can honour --company without a second read,
            # and so a plan file says which books it belongs to.
            'company': (doc.get('company') or '').strip(),
        }
        if docstatus == 1:
            # A separate list, not a separate shape. The applier needs
            # --include-submitted to touch it, because these writes reach the
            # posted ledger and a draft repair does not.
            submitted_changes.append(entry)
            continue
        changes.append(entry)

    return {
        # 2 (v0.9.1) added `submitted_changes`. A v1 plan has none, and the
        # applier falls back to reading v1's `submitted_not_writable` review
        # rows, so an old plan file still applies.
        'plan_version': 2,
        'plan_kind': 'je_party',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'journal_entries_examined': examined,
        'lines_already_correct': already_correct,
        'counts_by_rule': counts_by_rule(changes),
        'counts_by_rule_submitted': counts_by_rule(submitted_changes),
        'changes': changes,
        'submitted_changes': submitted_changes,
        'review': review,
    }


def counts_by_rule(changes: list) -> list:
    """Proposed changes rolled up per LIVE rule — the summary an operator reads
    before authorizing anything ("47 drafts under Sorren → Supplier: Sorren")."""
    by: dict = {}
    for c in changes:
        key = (c['live_rule_id'], c['new_party_type'], c['new_party'])
        agg = by.setdefault(key, {
            'live_rule_id': c['live_rule_id'], 'rule_name': c['rule_name'],
            'new_party_type': c['new_party_type'],
            'new_party': c['new_party'],
            'journal_entries': set(), 'lines': 0})
        agg['journal_entries'].add(c['journal_entry'])
        agg['lines'] += 1
    out = [{**agg, 'journal_entries': len(agg['journal_entries'])}
           for agg in by.values()]
    return sorted(out, key=lambda a: -a['lines'])


def summarize(plan: dict) -> str:
    """The human-readable digest printed to stdout (and worth pasting into a
    note to the accountant)."""
    submitted = plan.get('submitted_changes') or []
    lines = [
        f"Journal Entries examined : {plan['journal_entries_examined']}",
        f"Lines already correct    : {plan['lines_already_correct']}",
        f"Draft lines to change    : {len(plan['changes'])}",
        f"Submitted lines to change: {len(submitted)}",
        f"Needing review           : {len(plan['review'])}",
        '',
        'BY RULE — DRAFTS (one field on the child row; no GL exists yet)',
    ]
    if not plan['counts_by_rule']:
        lines.append('  (nothing to change)')
    for agg in plan['counts_by_rule']:
        lines.append(
            f"  rule #{agg['live_rule_id']} “{agg['rule_name']}” → "
            f"{agg['new_party_type']}: {agg['new_party']} — "
            f"{agg['lines']} line(s) across {agg['journal_entries']} entry(ies)")
    if submitted:
        lines += ['',
                  'BY RULE — SUBMITTED (writes the posted ledger too: the JE '
                  'child row AND',
                  '  its GL Entry rows. The applier needs --include-submitted '
                  'to touch these.)']
        for agg in plan.get('counts_by_rule_submitted') or []:
            lines.append(
                f"  rule #{agg['live_rule_id']} “{agg['rule_name']}” → "
                f"{agg['new_party_type']}: {agg['new_party']} — "
                f"{agg['lines']} line(s) across "
                f"{agg['journal_entries']} entry(ies)")
    if plan['review']:
        lines += ['', 'NEEDING REVIEW (nothing will be written for these)']
        by_reason: dict = {}
        for r in plan['review']:
            by_reason.setdefault(r['reason'], []).append(r)
        for reason, rows in sorted(by_reason.items()):
            lines.append(f"  {reason} ×{len(rows)} — "
                         f"{REVIEW_REASONS.get(reason, '')}")
            for r in rows[:10]:
                extra = ''
                if r.get('new_party'):
                    extra = (f" → would be {r['new_party_type']}: "
                             f"{r['new_party']}")
                if r.get('offset_account_type'):
                    extra += f" [offset is {r['offset_account_type']}]"
                lines.append(f"      {r['journal_entry']} "
                             f"(rule {r.get('rule_id')} "
                             f"“{r.get('rule_name', '')}”){extra}")
            if len(rows) > 10:
                lines.append(f'      … and {len(rows) - 10} more '
                             f'(all listed in the plan file)')
    return '\n'.join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Plan the v0.9.0 bank-tx Journal Entry Party repair. '
                    'Writes nothing but the plan file.')
    ap.add_argument('--out', default='',
                    help='write the plan JSON here (default: stdout only)')
    ap.add_argument('--limit', type=int, default=0,
                    help='examine at most N Journal Entries (a smoke test)')
    args = ap.parse_args(argv)

    from app import create_app, erpnext_bank
    app = create_app()
    with app.app_context():
        client = erpnext_bank.get_client()
        plan = build_plan(client, limit=args.limit or None)
    print(summarize(plan))
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            json.dump(plan, fh, indent=2, default=str)
        print(f'\nplan written to {args.out}')
    else:
        print('\n(no --out given; nothing was saved)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
