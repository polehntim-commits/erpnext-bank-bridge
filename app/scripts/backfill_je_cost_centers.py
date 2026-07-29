# SPDX-License-Identifier: MIT
"""APPLY the v0.8.5 cost-center repair to already-posted bank-transaction
Journal Entries. Phase 2 of 2 — see scripts/plan_je_cost_center_backfill.py for
phase 1 and for what the bug was.

    DRY RUN BY DEFAULT. Without --commit this writes nothing and only reports.
    The flag is the whole safety design: an operator who typos the command gets
    a report, never a mutated ledger.

WHY A DIRECT DB WRITE. `cost_center` on a Journal Entry Account row is not
`allow_on_submit`, so ERPNext refuses to change it through the REST API — the
only supported route is cancel + amend + resubmit, which for hundreds of
historical entries means hundreds of new docnames, a broken audit chain, and
every downstream Bank Transaction reconciliation link severed. A targeted field
update on the two tables that hold the value is smaller, reversible from the
plan file, and leaves docnames and links exactly where the accountant left them.

BOTH TABLES, ALWAYS. `tabJournal Entry Account` is what the JE form shows;
`tabGL Entry` is what every report reads. Updating only the first would make
the form look fixed while the Cost-Center-wise P&L kept the old answer — a
worse state than the bug, because it hides itself.

FAIL SAFE. Every line is re-read before it is written and skipped unless it
still holds exactly the value the plan recorded. A plan built yesterday against
an entry someone edited this morning cannot clobber that edit; it reports
`stale` and moves on.

FAIL FORWARD. Nothing is skipped silently. `already_correct`, `stale`,
`row_not_found` and `gl_not_found` are each counted, listed and written to the
log file, so a partial run says exactly what it did and did not touch.

Every write is logged as (JE docname, old cost center, new cost center, rule id)
to stdout and, with --log, to a JSONL file — the audit trail for why the books
changed.

    # phase 1 produced /tmp/je_cc_plan.json inside the Bank Bridge container
    docker cp <bankbridge_container>:/tmp/je_cc_plan.json /tmp/
    docker cp /tmp/je_cc_plan.json <erpnext_container>:/tmp/
    docker cp scripts/backfill_je_cost_centers.py <erpnext_container>:/tmp/

    # DRY RUN — reports, writes nothing
    docker exec <erpnext_container> bash -lc 'cd /home/frappe/frappe-bench/sites \
        && ../env/bin/python /tmp/backfill_je_cost_centers.py <site> /tmp/je_cc_plan.json'

    # then, once the report reads right
    docker exec <erpnext_container> bash -lc 'cd /home/frappe/frappe-bench/sites \
        && ../env/bin/python /tmp/backfill_je_cost_centers.py <site> /tmp/je_cc_plan.json \
           --commit --log /tmp/je_cc_backfill.jsonl'
"""
from __future__ import annotations

import argparse
import json
import sys

JE_ACCOUNT_DT = 'Journal Entry Account'
GL_ENTRY_DT = 'GL Entry'

# Outcomes, in the order the report lists them. `updated` is the only one that
# writes; the rest exist so a skip is never invisible.
OUTCOMES = ('updated', 'already_correct', 'stale', 'row_not_found',
            'gl_not_found')


def _gl_entries(frappe, change: dict) -> list:
    """The GL Entry rows this JE line produced.

    `voucher_detail_no` IS the child row's docname — ERPNext stamps it when it
    posts the ledger, so it identifies the line exactly even when a Journal
    Entry books the same account twice. The account fallback is for ledgers old
    enough (or repaired by hand) to have lost it; it is deliberately also
    filtered on the old cost center, so a fallback match can only ever hit rows
    that still carry the value the plan expects."""
    rows = frappe.get_all(
        GL_ENTRY_DT,
        filters={'voucher_type': 'Journal Entry',
                 'voucher_no': change['journal_entry'],
                 'voucher_detail_no': change['row'],
                 'is_cancelled': 0},
        fields=['name', 'cost_center'])
    if rows:
        return rows
    return frappe.get_all(
        GL_ENTRY_DT,
        filters={'voucher_type': 'Journal Entry',
                 'voucher_no': change['journal_entry'],
                 'account': change['account'],
                 'cost_center': change['old_cost_center'],
                 'is_cancelled': 0},
        fields=['name', 'cost_center'])


def apply_plan(frappe, plan: dict, *, commit: bool = False) -> dict:
    """Walk the plan's `changes` and (when `commit`) write them. Returns a
    result dict: per-outcome counts, the per-rule rollup, and one record per
    line with what happened to it.

    Idempotent. A second run finds every line already carrying its new value
    and reports `already_correct` — which is also what makes an interrupted
    first run safe to simply re-run."""
    results = []
    for change in plan.get('changes') or []:
        rec = {**{k: change.get(k) for k in (
            'journal_entry', 'row', 'account', 'old_cost_center',
            'new_cost_center', 'rule_id', 'live_rule_id', 'rule_name')},
            'gl_entries': 0}

        row = frappe.db.get_value(
            JE_ACCOUNT_DT, change['row'], ['cost_center', 'parent'],
            as_dict=True)
        if not row:
            results.append({**rec, 'outcome': 'row_not_found'})
            continue
        current = (row.get('cost_center') or '').strip()
        if row.get('parent') != change['journal_entry']:
            # The child row belongs to a different entry than the plan thinks.
            # Not a stale value — a stale IDENTITY, and nothing here is safe.
            results.append({**rec, 'outcome': 'stale',
                            'detail': f"row belongs to {row.get('parent')!r}"})
            continue
        if current == change['new_cost_center']:
            results.append({**rec, 'outcome': 'already_correct'})
            continue
        if current != (change['old_cost_center'] or ''):
            results.append({**rec, 'outcome': 'stale',
                            'detail': f'line now reads {current!r}, plan '
                                      f"expected {change['old_cost_center']!r}"})
            continue

        gl_rows = _gl_entries(frappe, change)
        if not gl_rows:
            # The form value could be fixed here and the ledger left wrong.
            # That is the one half-repair this script refuses to make.
            results.append({**rec, 'outcome': 'gl_not_found'})
            continue

        if commit:
            frappe.db.set_value(JE_ACCOUNT_DT, change['row'], 'cost_center',
                                change['new_cost_center'])
            for gl in gl_rows:
                frappe.db.set_value(GL_ENTRY_DT, gl['name'], 'cost_center',
                                    change['new_cost_center'])
        results.append({**rec, 'outcome': 'updated',
                        'gl_entries': len(gl_rows), 'written': bool(commit)})

    counts = {o: 0 for o in OUTCOMES}
    for r in results:
        counts[r['outcome']] = counts.get(r['outcome'], 0) + 1
    return {
        'committed': bool(commit),
        'counts': counts,
        'by_rule': _by_rule(results),
        'results': results,
        # Carried through so the log file stands alone as an audit record.
        'plan_generated_at': plan.get('generated_at'),
        'review': plan.get('review') or [],
    }


def _by_rule(results: list) -> list:
    by: dict = {}
    for r in results:
        agg = by.setdefault(r['live_rule_id'], {
            'live_rule_id': r['live_rule_id'], 'rule_name': r['rule_name'],
            'new_cost_center': r['new_cost_center'],
            **{o: 0 for o in OUTCOMES}})
        agg[r['outcome']] = agg.get(r['outcome'], 0) + 1
    return sorted(by.values(), key=lambda a: -a['updated'])


def report(result: dict) -> str:
    verb = 'UPDATED' if result['committed'] else 'WOULD UPDATE'
    counts = result['counts']
    out = [
        ('COMMITTED — the ledger was written.' if result['committed']
         else 'DRY RUN — nothing was written. Re-run with --commit to apply.'),
        '',
        f"{verb:<14}: {counts['updated']}",
        f"already correct: {counts['already_correct']}",
        f"stale (skipped): {counts['stale']}",
        f"row not found  : {counts['row_not_found']}",
        f"GL not found   : {counts['gl_not_found']}",
        '',
        'BY RULE',
    ]
    if not result['by_rule']:
        out.append('  (the plan proposed no changes)')
    for agg in result['by_rule']:
        out.append(f"  rule #{agg['live_rule_id']} “{agg['rule_name']}” → "
                   f"{agg['new_cost_center']}: {agg['updated']} {verb.lower()}, "
                   f"{agg['already_correct']} already correct, "
                   f"{agg['stale']} stale")
    skipped = [r for r in result['results'] if r['outcome'] in
               ('stale', 'row_not_found', 'gl_not_found')]
    if skipped:
        out += ['', 'SKIPPED — each one needs a human look']
        for r in skipped:
            out.append(f"  {r['outcome']}: {r['journal_entry']} "
                       f"{r['account']} — {r.get('detail', '')}")
    if result['review']:
        out += ['',
                f"CARRIED FROM THE PLAN — {len(result['review'])} Journal "
                f'Entry(ies) no rule can repair. They are listed in the plan '
                f'file and in the log; give a rule a cost center and re-plan.']
    return '\n'.join(out)


def write_log(path: str, result: dict) -> int:
    """One JSONL record per line touched — (JE docname, old cc, new cc, rule
    id, outcome) — plus one record per unrepairable JE carried from the plan.
    Returns the number of records written."""
    n = 0
    with open(path, 'a', encoding='utf-8') as fh:
        for r in result['results']:
            fh.write(json.dumps({'kind': 'line',
                                 'committed': result['committed'], **r},
                                sort_keys=True) + '\n')
            n += 1
        for r in result['review']:
            fh.write(json.dumps({'kind': 'review', **r}, sort_keys=True) + '\n')
            n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Apply a cost-center backfill plan to posted Journal '
                    'Entries. DRY RUN unless --commit is given.')
    ap.add_argument('site', help='the Frappe site (e.g. frontend)')
    ap.add_argument('plan', help='the plan JSON from '
                                 'plan_je_cost_center_backfill.py')
    ap.add_argument('--commit', action='store_true',
                    help='actually write. Without this nothing is changed.')
    ap.add_argument('--log', default='',
                    help='append a JSONL audit record per line to this file')
    args = ap.parse_args(argv)

    with open(args.plan, encoding='utf-8') as fh:
        plan = json.load(fh)

    import frappe
    frappe.init(site=args.site)
    frappe.connect()
    try:
        result = apply_plan(frappe, plan, commit=args.commit)
        if args.commit:
            frappe.db.commit()
    finally:
        frappe.destroy()

    print(report(result))
    if args.log:
        n = write_log(args.log, result)
        print(f'\n{n} audit record(s) appended to {args.log}')
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
