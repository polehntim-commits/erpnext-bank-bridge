# SPDX-License-Identifier: MIT
"""APPLY the v0.9.0 Party repair to bank-transaction Journal Entries. Phase 2 of
2 — see scripts/plan_je_party_backfill.py for phase 1 and for what the gap was.

    DRY RUN BY DEFAULT. Without --commit this writes nothing and only reports.
    The flag is the whole safety design: an operator who typos the command gets
    a report, never a mutated ledger.

DRAFTS ONLY, AND THIS SCRIPT ENFORCES IT ITSELF. `party` and `party_type` on a
Journal Entry Account are not `allow_on_submit` — verified on the live site:

    frappe.get_meta("Journal Entry Account").get_field("party").allow_on_submit
    → 0

(`cost_center` IS, which is exactly why v0.8.5's cost-center repair could reach
submitted entries and this one must not.) The planner already routes submitted
entries to `review` rather than to `changes`, but this script RE-CHECKS
docstatus on every line before writing anyway — a plan is a file, files get
edited, and the guard that matters is the one next to the write.

WHY NOT WRITE THE SUBMITTED ONES ANYWAY. Two reasons, and the second is the
decisive one:

  1. Frappe refuses the field on a submitted document, so it would have to be a
     direct `frappe.db.set_value` that bypasses validation.
  2. **Every supplier-wise report reads `tabGL Entry`, not the Journal Entry.**
     Party on a submitted entry lives in BOTH tables. Writing only the JE child
     row would make the form look repaired while the Accounts Payable and
     supplier-ledger reports kept the old (empty) answer — a state strictly
     worse than the bug, because it hides itself. Writing both means
     re-deriving party GL entries under a submitted voucher, which is ledger
     surgery that belongs in an accountant's hands via cancel + amend, not in an
     unattended script.

     Contrast the cost-center repair, which DID write both tables: a cost center
     is a dimension on an existing GL row, so updating it changes how a row is
     grouped. A party is closer to an identity — it drives AP aging, party-wise
     balances and 1099 aggregation — and back-filling one under a submitted
     voucher without ERPNext's own party validation running is how a ledger
     acquires a party its subledger does not agree with.

  So the submitted entries are REPORTED, with the party the rules now imply, and
  the operator decides. `--list-submitted` prints exactly that list.

A DRAFT NEEDS NO GL WRITE. A draft has produced no GL Entries at all — they are
created at submit — so repairing a draft is one field on one child row, and the
entry then submits through ERPNext's normal validation with the party in place.
That is the whole reason drafts are the safe population: the ledger is written
once, correctly, by ERPNext.

FAIL SAFE. Every line is re-read before it is written and skipped unless it
still holds exactly the value the plan recorded. A plan built yesterday against
an entry someone edited this morning cannot clobber that edit; it reports
`stale` and moves on.

FAIL FORWARD. Nothing is skipped silently. `already_correct`, `stale`,
`row_not_found`, `now_submitted` and `ineligible_account` are each counted,
listed and written to the log file, so a partial run says exactly what it did
and did not touch.

Every write is logged as (JE docname, offset account, party type, party, rule id)
to stdout and, with --log, to a JSONL file — the audit trail for why the books
changed.

    # phase 1 produced /tmp/je_party_plan.json inside the Bank Bridge container
    docker cp <bankbridge_container>:/tmp/je_party_plan.json /tmp/
    docker cp /tmp/je_party_plan.json <erpnext_container>:/tmp/
    docker cp scripts/backfill_je_parties.py <erpnext_container>:/tmp/

    # DRY RUN — reports, writes nothing
    docker exec <erpnext_container> bash -lc 'cd /home/frappe/frappe-bench/sites \\
        && ../env/bin/python /tmp/backfill_je_parties.py <site> /tmp/je_party_plan.json'

    # then, once the report reads right
    docker exec <erpnext_container> bash -lc 'cd /home/frappe/frappe-bench/sites \\
        && ../env/bin/python /tmp/backfill_je_parties.py <site> /tmp/je_party_plan.json \\
           --commit --log /tmp/je_party_backfill.jsonl'
"""
from __future__ import annotations

import argparse
import json
import sys

JE_ACCOUNT_DT = 'Journal Entry Account'
JE_DT = 'Journal Entry'

# Outcomes, in the order the report lists them. `updated` is the only one that
# writes; the rest exist so a skip is never invisible.
OUTCOMES = ('updated', 'already_correct', 'stale', 'row_not_found',
            'now_submitted', 'ineligible_account', 'party_missing')

# ERPNext's own rule, restated here because this script runs INSIDE the ERPNext
# container with no Bank Bridge code importable. Kept in sync with
# app/categorization.PARTY_ELIGIBLE_ACCOUNT_TYPES by the test that asserts the
# two agree (tests/test_je_party_backfill.py).
PARTY_ELIGIBLE_ACCOUNT_TYPES = ('Receivable', 'Payable', 'Equity')


def _eligible(frappe, account: str) -> bool:
    """Whether ERPNext would accept a Party on this account.

    A BLANK account_type is eligible — that is ERPNext's own guard
    (`if account_type and account_type not in [...]`), not a leniency of ours.
    Re-checked here rather than trusted from the plan because the plan may have
    been built before someone reclassified the account."""
    account_type = frappe.get_cached_value('Account', account, 'account_type')
    if not account_type:
        return True
    return account_type in PARTY_ELIGIBLE_ACCOUNT_TYPES


def _party_exists(frappe, party_type: str, party: str) -> bool:
    """Whether the party docname is really there. A Journal Entry naming a party
    that does not exist fails at submit with a LinkValidationError, which would
    turn a repairable draft into an unsubmittable one — the opposite of the
    point."""
    try:
        return bool(frappe.db.exists(party_type, party))
    except Exception:                     # noqa: BLE001 - treat as absent
        return False


def apply_plan(frappe, plan: dict, *, commit: bool = False) -> dict:
    """Walk the plan's `changes` and (when `commit`) write them. Returns a result
    dict: per-outcome counts, the per-rule rollup, and one record per line with
    what happened to it.

    Idempotent. A second run finds every line already carrying its new party and
    reports `already_correct` — which is also what makes an interrupted first run
    safe to simply re-run."""
    results = []
    for change in plan.get('changes') or []:
        rec = {k: change.get(k) for k in (
            'journal_entry', 'row', 'account', 'account_type',
            'old_party_type', 'old_party', 'new_party_type', 'new_party',
            'rule_id', 'live_rule_id', 'rule_name')}

        row = frappe.db.get_value(
            JE_ACCOUNT_DT, change['row'],
            ['party_type', 'party', 'parent', 'account', 'docstatus'],
            as_dict=True)
        if not row:
            results.append({**rec, 'outcome': 'row_not_found'})
            continue
        if row.get('parent') != change['journal_entry']:
            # A stale IDENTITY, not a stale value — nothing here is safe.
            results.append({**rec, 'outcome': 'stale',
                            'detail': f"row belongs to {row.get('parent')!r}"})
            continue

        # THE GUARD THAT MATTERS. Re-read docstatus from the parent: the planner
        # excluded submitted entries, but a plan is a file and files get edited,
        # and someone may simply have submitted this draft since it was built.
        docstatus = int(frappe.db.get_value(
            JE_DT, change['journal_entry'], 'docstatus') or 0)
        if docstatus != 0:
            results.append({
                **rec, 'outcome': 'now_submitted',
                'detail': f'Journal Entry docstatus is {docstatus}; `party` is '
                          'not allow_on_submit, so this needs cancel + amend'})
            continue

        current_type = (row.get('party_type') or '').strip()
        current_party = (row.get('party') or '').strip()
        if (current_type == change['new_party_type']
                and current_party == change['new_party']):
            results.append({**rec, 'outcome': 'already_correct'})
            continue
        if (current_type != (change['old_party_type'] or '')
                or current_party != (change['old_party'] or '')):
            results.append({
                **rec, 'outcome': 'stale',
                'detail': f'line now reads {current_type!r}/{current_party!r}, '
                          f"plan expected {change['old_party_type']!r}/"
                          f"{change['old_party']!r}"})
            continue

        account = (row.get('account') or change.get('account') or '').strip()
        if not _eligible(frappe, account):
            results.append({
                **rec, 'outcome': 'ineligible_account',
                'detail': f'{account} has an account_type ERPNext will not '
                          'accept a Party on — the entry would fail at submit'})
            continue
        if not _party_exists(frappe, change['new_party_type'],
                             change['new_party']):
            results.append({
                **rec, 'outcome': 'party_missing',
                'detail': f"{change['new_party_type']} "
                          f"{change['new_party']!r} does not exist — writing it "
                          'would make this draft unsubmittable'})
            continue

        if commit:
            # A DRAFT has no GL Entries (they are created at submit), so this is
            # the only table that holds the value. ERPNext validates the party
            # when the operator submits, which is what makes this safe.
            frappe.db.set_value(JE_ACCOUNT_DT, change['row'], {
                'party_type': change['new_party_type'],
                'party': change['new_party']})
        results.append({**rec, 'outcome': 'updated', 'written': bool(commit)})

    counts = {o: 0 for o in OUTCOMES}
    for r in results:
        counts[r['outcome']] = counts.get(r['outcome'], 0) + 1
    submitted_review = [r for r in (plan.get('review') or [])
                        if r.get('reason') == 'submitted_not_writable']
    return {
        'committed': bool(commit),
        'counts': counts,
        'by_rule': _by_rule(results),
        'results': results,
        # Carried through so the log file stands alone as an audit record.
        'plan_generated_at': plan.get('generated_at'),
        'review': plan.get('review') or [],
        'submitted_needing_amend': submitted_review,
    }


def _by_rule(results: list) -> list:
    by: dict = {}
    for r in results:
        key = (r['live_rule_id'], r['new_party_type'], r['new_party'])
        agg = by.setdefault(key, {
            'live_rule_id': r['live_rule_id'], 'rule_name': r['rule_name'],
            'new_party_type': r['new_party_type'],
            'new_party': r['new_party'], **{o: 0 for o in OUTCOMES}})
        agg[r['outcome']] = agg.get(r['outcome'], 0) + 1
    return sorted(by.values(), key=lambda a: -a['updated'])


def report(result: dict, *, list_submitted: bool = False) -> str:
    verb = 'UPDATED' if result['committed'] else 'WOULD UPDATE'
    counts = result['counts']
    out = [
        ('COMMITTED — the drafts were written.' if result['committed']
         else 'DRY RUN — nothing was written. Re-run with --commit to apply.'),
        '',
        f"{verb:<18}: {counts['updated']}",
        f"already correct   : {counts['already_correct']}",
        f"stale (skipped)   : {counts['stale']}",
        f"row not found     : {counts['row_not_found']}",
        f"now submitted     : {counts['now_submitted']}",
        f"ineligible account: {counts['ineligible_account']}",
        f"party missing     : {counts['party_missing']}",
        '',
        'BY RULE',
    ]
    if not result['by_rule']:
        out.append('  (the plan proposed no draft changes)')
    for agg in result['by_rule']:
        out.append(f"  rule #{agg['live_rule_id']} “{agg['rule_name']}” → "
                   f"{agg['new_party_type']}: {agg['new_party']} — "
                   f"{agg['updated']} {verb.lower()}, "
                   f"{agg['already_correct']} already correct, "
                   f"{agg['stale']} stale")
    skipped = [r for r in result['results']
               if r['outcome'] not in ('updated', 'already_correct')]
    if skipped:
        out += ['', 'SKIPPED — each one needs a human look']
        for r in skipped:
            out.append(f"  {r['outcome']}: {r['journal_entry']} "
                       f"{r['account']} — {r.get('detail', '')}")
    pending = result.get('submitted_needing_amend') or []
    if pending:
        out += ['', f'SUBMITTED ENTRIES ({len(pending)}) — not writable here.',
                '  `party` is not allow_on_submit, and repairing one means '
                'cancel + amend',
                '  (or an accountant\'s judgement that it is not worth it). '
                'The party the',
                '  rules now imply is recorded for each:']
        shown = pending if list_submitted else pending[:10]
        for r in shown:
            out.append(f"    {r['journal_entry']} {r.get('account', '')} → "
                       f"{r.get('new_party_type')}: {r.get('new_party')} "
                       f"(rule {r.get('live_rule_id')} "
                       f"“{r.get('rule_name', '')}”)")
        if not list_submitted and len(pending) > len(shown):
            out.append(f'    … and {len(pending) - len(shown)} more '
                       '(--list-submitted for all, or read the plan file)')
    other_review = [r for r in (result.get('review') or [])
                    if r.get('reason') != 'submitted_not_writable']
    if other_review:
        by_reason: dict = {}
        for r in other_review:
            by_reason.setdefault(r['reason'], []).append(r)
        out += ['', 'FROM THE PLAN — needing review, never written']
        for reason, rows in sorted(by_reason.items()):
            out.append(f'  {reason} ×{len(rows)}')
    return '\n'.join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Apply the v0.9.0 Journal Entry Party repair to DRAFT '
                    'entries. Dry run unless --commit.')
    ap.add_argument('site', help='the Frappe site name (e.g. frontend)')
    ap.add_argument('plan', help='the plan JSON from phase 1')
    ap.add_argument('--commit', action='store_true',
                    help='actually write (default: dry run)')
    ap.add_argument('--log', default='',
                    help='append one JSON object per line to this file')
    ap.add_argument('--list-submitted', action='store_true',
                    help='print every submitted entry needing cancel + amend, '
                         'not just the first ten')
    args = ap.parse_args(argv)

    with open(args.plan, encoding='utf-8') as fh:
        plan = json.load(fh)
    kind = plan.get('plan_kind')
    if kind and kind != 'je_party':
        print(f'refusing: {args.plan} is a {kind!r} plan, not a je_party plan',
              file=sys.stderr)
        return 2

    import frappe
    frappe.init(site=args.site)
    frappe.connect()
    try:
        result = apply_plan(frappe, plan, commit=args.commit)
        if args.commit:
            frappe.db.commit()
    finally:
        frappe.destroy()

    print(report(result, list_submitted=args.list_submitted))
    if args.log:
        with open(args.log, 'a', encoding='utf-8') as fh:
            for rec in result['results']:
                fh.write(json.dumps({**rec,
                                     'committed': result['committed']},
                                    default=str) + '\n')
        print(f"\n{len(result['results'])} line(s) logged to {args.log}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
