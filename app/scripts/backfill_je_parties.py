# SPDX-License-Identifier: MIT
"""APPLY the Party repair to bank-transaction Journal Entries — drafts (v0.9.0)
and submitted entries (v0.9.1). Phase 2 of 2 — see
scripts/plan_je_party_backfill.py for phase 1 and for what the gap was.

    DRY RUN BY DEFAULT. Without --commit this writes nothing and only reports.
    The flag is the whole safety design: an operator who typos the command gets
    a report, never a mutated ledger.

    SUBMITTED ENTRIES NEED A SECOND FLAG. --include-submitted. Repairing a draft
    touches one field on an unposted document; repairing a submitted entry
    rewrites the posted ledger. Those are different-sized decisions and they get
    different-sized gates.

WHY A DIRECT DB WRITE FOR SUBMITTED ENTRIES. `party` and `party_type` on a
Journal Entry Account are not `allow_on_submit` — verified on the live site:

    frappe.get_meta("Journal Entry Account").get_field("party").allow_on_submit
    → 0

so ERPNext refuses the change through the REST API. The only supported route is
cancel + amend + resubmit, which for ~173 historical entries means ~173 new
docnames, a broken audit chain, and every downstream Bank Transaction
reconciliation link severed. A targeted field update on the two tables that hold
the value is smaller, reversible from the plan file, and leaves docnames and
links exactly where the accountant left them. This is the same trade v0.8.5 made
for `cost_center`, for the same reasons.

WHAT v0.9.0 GOT WRONG, AND WHAT IT GOT RIGHT. v0.9.0 refused submitted entries,
reasoning that writing both tables meant "re-deriving party GL entries under a
submitted voucher." The mechanism is not that. `party_type` and `party` are
COLUMNS ON GL ENTRY ROWS THAT ALREADY EXIST — ERPNext stamps `voucher_detail_no`
on every GL row when it posts, so the rows a JE line produced are addressable by
name, and nothing is re-derived or re-posted. Setting a column on an existing row
is exactly what the cost-center repair did.

What v0.9.0 got right is the half-repair hazard, and it is why this script writes
BOTH TABLES OR NEITHER. `tabJournal Entry Account` is what the JE form shows;
`tabGL Entry` is what every supplier-wise report reads — Accounts Payable,
supplier ledger, party-wise balances, 1099 aggregation. Writing only the child
row would make the form look repaired while every report kept the empty answer:
strictly worse than the bug, because it hides itself. So a submitted line whose
GL rows cannot be identified is REFUSED (`gl_not_found`, `gl_ambiguous`) rather
than half-written, and cancel + amend remains the answer for those.

IT ALSO REPAIRS A HALF-REPAIR. The JE row and its GL rows are checked
INDEPENDENTLY, so a line whose form was fixed by hand while the ledger was left
empty is detected and the GL rows are brought up — it does not report
`already_correct` off the child row alone and walk past a wrong ledger. That is
the one behaviour here that deliberately differs from the cost-center script.

A DRAFT NEEDS NO GL WRITE. A draft has produced no GL Entries at all — they are
created at submit — so repairing a draft is one field on one child row, and the
entry then submits through ERPNext's normal validation with the party in place.

CANCELLED ENTRIES ARE REFUSED WHOLE. docstatus 2 is not touched even at the DB
level: a cancelled voucher's GL rows carry `is_cancelled=1` and exist as history.
Naming a party on history changes what a prior-period report says about a
transaction that was undone.

FAIL SAFE. Every line — and every GL row under it — is re-read before it is
written and skipped unless it still holds exactly the value the plan recorded. A
plan built yesterday against an entry someone edited this morning cannot clobber
that edit; it reports `stale` and moves on. If a write raises, the run ABORTS and
rolls back rather than leaving a JE row repaired and its ledger not.

FAIL FORWARD. Nothing is skipped silently. Every outcome in OUTCOMES is counted,
listed with a reason, and written to the log file, so a partial run says exactly
what it did and did not touch.

Every write is logged as (JE docname, offset account, party type, party, rule id)
to stdout and, with --log, to a JSONL file — the audit trail for why the books
changed.

KAIROS. A one-time operator-invoked repair, run when the rules are right. Nothing
schedules it and there is no runtime tool for it.

    # phase 1 produced /tmp/je_party_plan.json inside the Bank Bridge container
    docker cp <bankbridge_container>:/tmp/je_party_plan.json /tmp/
    docker cp /tmp/je_party_plan.json <erpnext_container>:/tmp/
    docker cp scripts/backfill_je_parties.py <erpnext_container>:/tmp/

    # DRY RUN — reports, writes nothing
    docker exec <erpnext_container> bash -lc 'cd /home/frappe/frappe-bench/sites \\
        && ../env/bin/python /tmp/backfill_je_parties.py <site> /tmp/je_party_plan.json'

    # drafts only
    docker exec <erpnext_container> bash -lc 'cd /home/frappe/frappe-bench/sites \\
        && ../env/bin/python /tmp/backfill_je_parties.py <site> /tmp/je_party_plan.json \\
           --commit --log /tmp/je_party_backfill.jsonl'

    # then, once THAT report reads right, the posted ledger
    docker exec <erpnext_container> bash -lc 'cd /home/frappe/frappe-bench/sites \\
        && ../env/bin/python /tmp/backfill_je_parties.py <site> /tmp/je_party_plan.json \\
           --include-submitted --commit --log /tmp/je_party_backfill.jsonl'
"""
from __future__ import annotations

import argparse
import json
import sys

JE_ACCOUNT_DT = 'Journal Entry Account'
JE_DT = 'Journal Entry'
GL_ENTRY_DT = 'GL Entry'

# Outcomes, in the order the report lists them. `updated` is the only one that
# writes; the rest exist so a skip is never invisible.
OUTCOMES = ('updated', 'already_correct', 'stale', 'row_not_found',
            'now_submitted', 'submitted_not_included', 'cancelled',
            'wrong_company', 'ineligible_account', 'party_missing',
            'gl_not_found', 'gl_ambiguous', 'write_failed')

# Outcomes that mean "a human has to look at this one".
SKIPPED_OUTCOMES = tuple(o for o in OUTCOMES
                         if o not in ('updated', 'already_correct'))

# ERPNext's own rule, restated here because this script runs INSIDE the ERPNext
# container with no Bank Bridge code importable. Kept in sync with
# app/categorization.PARTY_ELIGIBLE_ACCOUNT_TYPES by the test that asserts the
# two agree (tests/test_je_party_backfill.py).
PARTY_ELIGIBLE_ACCOUNT_TYPES = ('Receivable', 'Payable', 'Equity')

# An ineligible offset is usually a typo-level mistake in the rule. A FIXED ASSET
# offset is not — it means the rule is booking an expense to the balance sheet,
# and the fix is to re-point the rule, never to clear the account_type (ERPNext's
# depreciation workflow reads it). See the runbook section in the README.
FIXED_ASSET_ADVICE = (
    'this is a Fixed Asset account, which means the RULE is mis-categorized, '
    'not the account. Point the rule\'s offset_account at the expense account '
    'the spend belongs to (a locksmith bill is Repairs & Maintenance; rent is '
    'Occupancy & Utilities), then re-plan. Do NOT clear account_type on a Fixed '
    'Asset account — depreciation needs it.')


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


def _ineligible_detail(frappe, account: str) -> str:
    """Why the account refused, and what to do about it. A refusal that only
    says "no" is a refusal an operator cannot act on."""
    account_type = frappe.get_cached_value('Account', account,
                                           'account_type') or ''
    # The account name is not repeated here — every caller prints it alongside.
    if account_type == 'Fixed Asset':
        return FIXED_ASSET_ADVICE
    return (f'account_type {account_type!r} is one ERPNext will not accept a '
            'Party on. Point the rule at an account with no account_type set, '
            'clear the account_type in ERPNext, or route the spend through '
            'Accounts Payable')


def _party_exists(frappe, party_type: str, party: str) -> bool:
    """Whether the party docname is really there. A Journal Entry naming a party
    that does not exist fails at submit with a LinkValidationError, which would
    turn a repairable draft into an unsubmittable one — the opposite of the
    point. On a SUBMITTED entry a dangling party is worse still: every report
    that joins the party would show a broken link with no document to open."""
    try:
        return bool(frappe.db.exists(party_type, party))
    except Exception:                     # noqa: BLE001 - treat as absent
        return False


def _gl_entries(frappe, change: dict):
    """(rows, problem) — the GL Entry rows this JE line produced.

    `voucher_detail_no` IS the child row's docname: ERPNext stamps it when it
    posts the ledger, so it identifies the line exactly even when a Journal Entry
    books the same account twice. That is the whole reason a submitted party
    repair is addressable at all.

    The fallback is for ledgers old enough (or repaired by hand) to have lost it,
    and it is DELIBERATELY STRICTER than the cost-center script's. A cost centre
    is a grouping; a party is an identity, and hanging one on the wrong leg of a
    posted entry is worse than leaving it blank. So the fallback matches on the
    voucher and account, then keeps only rows still holding the party the plan
    expects, and refuses outright if that leaves more than one candidate. The
    party match is done in Python rather than in the filter because an empty
    party is NULL in some rows and '' in others, and a SQL `party = ''` silently
    misses the NULLs — which are most of them, that being the bug."""
    rows = frappe.get_all(
        GL_ENTRY_DT,
        filters={'voucher_type': JE_DT,
                 'voucher_no': change['journal_entry'],
                 'voucher_detail_no': change['row'],
                 'is_cancelled': 0},
        fields=['name', 'party_type', 'party', 'account'])
    if rows:
        return rows, ''

    candidates = frappe.get_all(
        GL_ENTRY_DT,
        filters={'voucher_type': JE_DT,
                 'voucher_no': change['journal_entry'],
                 'account': change['account'],
                 'is_cancelled': 0},
        fields=['name', 'party_type', 'party', 'account'])
    old_type = (change.get('old_party_type') or '')
    old_party = (change.get('old_party') or '')
    matching = [r for r in candidates
                if (r.get('party_type') or '') == old_type
                and (r.get('party') or '') == old_party]
    if len(matching) > 1:
        return [], 'ambiguous'
    return matching, ''


def _needs_write(row: dict, change: dict) -> bool:
    return ((row.get('party_type') or '') != change['new_party_type']
            or (row.get('party') or '') != change['new_party'])


def _is_stale(row: dict, change: dict) -> bool:
    """A row holds neither the value the plan recorded nor the one it proposes,
    so something outside this plan has touched it."""
    return ((row.get('party_type') or '') != (change.get('old_party_type') or '')
            or (row.get('party') or '') != (change.get('old_party') or ''))


def submitted_changes(plan: dict) -> list:
    """The submitted lines a plan proposes.

    plan_version 2 has them in `submitted_changes`. A version 1 plan (v0.9.0)
    recorded them as `review` rows under `submitted_not_writable` with the same
    fields merged in, so an old plan file still applies rather than silently
    proposing nothing."""
    rows = plan.get('submitted_changes')
    if rows is not None:
        return list(rows)
    return [r for r in (plan.get('review') or [])
            if r.get('reason') == 'submitted_not_writable' and r.get('row')]


def _apply_one(frappe, change: dict, *, population: str, commit: bool,
               company: str) -> dict:
    """Decide and (when `commit`) perform the repair of one planned line.

    Returns the result record. Raises only if a write itself fails, which
    apply_plan turns into an aborted run — never a half-written line."""
    rec = {k: change.get(k) for k in (
        'journal_entry', 'row', 'account', 'account_type',
        'old_party_type', 'old_party', 'new_party_type', 'new_party',
        'rule_id', 'live_rule_id', 'rule_name', 'company')}
    rec['population'] = population
    rec['gl_entries'] = 0

    row = frappe.db.get_value(
        JE_ACCOUNT_DT, change['row'],
        ['party_type', 'party', 'parent', 'account', 'docstatus'],
        as_dict=True)
    if not row:
        return {**rec, 'outcome': 'row_not_found'}
    if row.get('parent') != change['journal_entry']:
        # A stale IDENTITY, not a stale value — nothing here is safe.
        return {**rec, 'outcome': 'stale',
                'detail': f"row belongs to {row.get('parent')!r}"}

    # THE GUARD THAT MATTERS. Re-read docstatus from the parent rather than
    # trusting the plan: a plan is a file, files get edited, and someone may
    # simply have submitted this draft since it was built.
    docstatus = int(frappe.db.get_value(
        JE_DT, change['journal_entry'], 'docstatus') or 0)
    if docstatus == 2:
        return {**rec, 'outcome': 'cancelled',
                'detail': 'the Journal Entry is cancelled (docstatus 2). A '
                          'cancelled voucher is history; naming a party on it '
                          'changes what a prior-period report says about a '
                          'transaction that was undone'}
    if population == 'draft' and docstatus != 0:
        # The plan says draft and the ledger says posted. Writing the posted
        # ledger off a row the plan file labels a draft would make the audit
        # artifact lie about what was done, so re-plan instead — it is cheap.
        return {**rec, 'outcome': 'now_submitted',
                'detail': 'this was a draft when the plan was built and is now '
                          'submitted. Re-plan and re-run with '
                          '--include-submitted'}
    if population == 'submitted' and docstatus == 0:
        return {**rec, 'outcome': 'stale',
                'detail': 'the plan recorded this as submitted and it is now a '
                          'draft (docstatus 0) — an amend replaces the docname, '
                          'so this is not the document the plan measured. '
                          'Re-plan'}

    if company:
        # Company scoping is read from the LIVE document, not the plan, for the
        # same reason as docstatus: the plan is an assertion, the DB is the fact.
        je_company = (frappe.db.get_value(
            JE_DT, change['journal_entry'], 'company') or '').strip()
        if je_company != company:
            return {**rec, 'outcome': 'wrong_company',
                    'company': je_company,
                    'detail': f'entry belongs to {je_company!r}, not '
                              f'{company!r}'}

    account = (row.get('account') or change.get('account') or '').strip()
    if not _eligible(frappe, account):
        return {**rec, 'outcome': 'ineligible_account',
                'detail': _ineligible_detail(frappe, account)}
    if not _party_exists(frappe, change['new_party_type'],
                         change['new_party']):
        return {**rec, 'outcome': 'party_missing',
                'detail': f"{change['new_party_type']} "
                          f"{change['new_party']!r} does not exist in ERPNext "
                          '— writing it would leave a broken link every report '
                          'that joins the party would show'}

    expected = (f"{change.get('old_party_type') or ''!r}/"
                f"{change.get('old_party') or ''!r}")
    je_needs = _needs_write(row, change)
    if je_needs and _is_stale(row, change):
        return {**rec, 'outcome': 'stale',
                'detail': f"line now reads "
                          f"{(row.get('party_type') or '')!r}/"
                          f"{(row.get('party') or '')!r}, plan expected "
                          f"{expected}"}

    # A DRAFT has no GL Entries — they are created at submit — so the child row
    # is the whole repair, and ERPNext validates the party when the operator
    # submits. That is what makes drafts the cheap population.
    if population == 'draft':
        if not je_needs:
            return {**rec, 'outcome': 'already_correct'}
        if commit:
            frappe.db.set_value(JE_ACCOUNT_DT, change['row'], {
                'party_type': change['new_party_type'],
                'party': change['new_party']})
        return {**rec, 'outcome': 'updated', 'written': bool(commit)}

    gl_rows, problem = _gl_entries(frappe, change)
    if problem == 'ambiguous':
        return {**rec, 'outcome': 'gl_ambiguous',
                'detail': 'more than one GL Entry on this account under this '
                          'voucher still holds the old party and none carries '
                          'voucher_detail_no, so which row belongs to this line '
                          'cannot be decided from the data. Repair by cancel + '
                          'amend'}
    if not gl_rows:
        # The form value could be fixed here and the ledger left wrong. That is
        # the one half-repair this script refuses to make, because every
        # supplier-wise report reads the ledger.
        return {**rec, 'outcome': 'gl_not_found',
                'detail': 'no live GL Entry found for this line, so the ledger '
                          'cannot be moved with the form. Repair by cancel + '
                          'amend'}

    rec['gl_entries'] = len(gl_rows)
    stale_gl = [g for g in gl_rows
                if _needs_write(g, change) and _is_stale(g, change)]
    if stale_gl:
        g = stale_gl[0]
        return {**rec, 'outcome': 'stale',
                'detail': f"GL Entry {g['name']} reads "
                          f"{(g.get('party_type') or '')!r}/"
                          f"{(g.get('party') or '')!r}, plan expected "
                          f"{expected}"}

    gl_needs = [g for g in gl_rows if _needs_write(g, change)]
    if not je_needs and not gl_needs:
        return {**rec, 'outcome': 'already_correct'}

    # THE HALF-REPAIR CASE, checked independently of the child row. A JE row
    # someone fixed by hand leaves the ledger — and therefore every report —
    # still empty. Reporting `already_correct` off the child row alone would
    # walk straight past it.
    rec['je_row_written'] = bool(je_needs)
    rec['gl_entries_written'] = len(gl_needs)
    if commit:
        payload = {'party_type': change['new_party_type'],
                   'party': change['new_party']}
        # One transaction, committed by main() once the whole run succeeds:
        # both tables move together or the run rolls back.
        if je_needs:
            frappe.db.set_value(JE_ACCOUNT_DT, change['row'], dict(payload))
        for gl in gl_needs:
            frappe.db.set_value(GL_ENTRY_DT, gl['name'], dict(payload))
    return {**rec, 'outcome': 'updated', 'written': bool(commit)}


def apply_plan(frappe, plan: dict, *, commit: bool = False,
               include_submitted: bool = False, company: str = '') -> dict:
    """Walk the plan and (when `commit`) write it. Returns a result dict:
    per-outcome counts, the per-rule rollup, and one record per line with what
    happened to it.

    Drafts always; submitted entries only with `include_submitted`, and then
    both `tabJournal Entry Account` and `tabGL Entry` move together.

    Idempotent. A second run finds every line already carrying its new party and
    reports `already_correct` — which is also what makes an interrupted first run
    safe to simply re-run.

    ABORTS on a failed write. A run that raised half-way through a line would
    have left a JE row repaired and its ledger not, so the exception is recorded
    against the line, the walk stops, and main() rolls back."""
    results = []
    aborted = False
    work = [(c, 'draft') for c in (plan.get('changes') or [])]
    for change in submitted_changes(plan):
        work.append((change, 'submitted'))

    for change, population in work:
        if population == 'submitted' and not include_submitted:
            results.append({
                **{k: change.get(k) for k in (
                    'journal_entry', 'row', 'account', 'new_party_type',
                    'new_party', 'rule_id', 'live_rule_id', 'rule_name')},
                'population': population, 'gl_entries': 0,
                'outcome': 'submitted_not_included',
                'detail': 'a submitted entry — repairing it rewrites the posted '
                          'ledger. Re-run with --include-submitted to apply it'})
            continue
        try:
            results.append(_apply_one(frappe, change, population=population,
                                      commit=commit, company=company))
        except Exception as exc:           # noqa: BLE001 - recorded, then abort
            results.append({
                **{k: change.get(k) for k in (
                    'journal_entry', 'row', 'account', 'new_party_type',
                    'new_party', 'rule_id', 'live_rule_id', 'rule_name')},
                'population': population, 'gl_entries': 0,
                'outcome': 'write_failed', 'detail': f'{type(exc).__name__}: '
                                                     f'{exc}'})
            aborted = True
            break

    counts = {o: 0 for o in OUTCOMES}
    for r in results:
        counts[r['outcome']] = counts.get(r['outcome'], 0) + 1
    return {
        'committed': bool(commit) and not aborted,
        'aborted': aborted,
        'include_submitted': bool(include_submitted),
        'company': company,
        'counts': counts,
        'by_rule': _by_rule(results),
        'results': results,
        # Carried through so the log file stands alone as an audit record.
        'plan_generated_at': plan.get('generated_at'),
        'plan_version': plan.get('plan_version'),
        'review': plan.get('review') or [],
    }


def _by_rule(results: list) -> list:
    by: dict = {}
    for r in results:
        key = (r['live_rule_id'], r['new_party_type'], r['new_party'],
               r.get('population'))
        agg = by.setdefault(key, {
            'live_rule_id': r['live_rule_id'], 'rule_name': r['rule_name'],
            'new_party_type': r['new_party_type'],
            'new_party': r['new_party'],
            'population': r.get('population'), **{o: 0 for o in OUTCOMES}})
        agg[r['outcome']] = agg.get(r['outcome'], 0) + 1
    return sorted(by.values(), key=lambda a: (a['population'] or '',
                                              -a['updated']))


def report(result: dict) -> str:
    verb = 'UPDATED' if result['committed'] else 'WOULD UPDATE'
    counts = result['counts']
    if result.get('aborted'):
        head = ('ABORTED — a write failed. Nothing was committed; the run was '
                'rolled back so no line is half-repaired.')
    elif result['committed'] and counts['updated']:
        head = 'COMMITTED — the books were written.'
    elif result['committed']:
        # "COMMITTED" over a report of nothing but refusals reads as success.
        head = ('COMMITTED, BUT NOTHING WAS WRITTEN — no line needed a change '
                'that could be made safely. The skips below say why.')
    else:
        head = 'DRY RUN — nothing was written. Re-run with --commit to apply.'
    out = [head]
    if not result.get('include_submitted'):
        out.append('Drafts only. --include-submitted also repairs the posted '
                   'ledger of submitted entries.')
    if result.get('company'):
        out.append(f"Scoped to company {result['company']!r}.")
    out += [
        '',
        f"{verb:<23}: {counts['updated']}",
        f"{'already correct':<23}: {counts['already_correct']}",
        f"{'stale (skipped)':<23}: {counts['stale']}",
        f"{'row not found':<23}: {counts['row_not_found']}",
        f"{'now submitted':<23}: {counts['now_submitted']}",
        f"{'submitted, not included':<23}: "
        f"{counts['submitted_not_included']}",
        f"{'cancelled':<23}: {counts['cancelled']}",
        f"{'wrong company':<23}: {counts['wrong_company']}",
        f"{'ineligible account':<23}: {counts['ineligible_account']}",
        f"{'party missing':<23}: {counts['party_missing']}",
        f"{'GL not found':<23}: {counts['gl_not_found']}",
        f"{'GL ambiguous':<23}: {counts['gl_ambiguous']}",
        f"{'write failed':<23}: {counts['write_failed']}",
        '',
        'BY RULE',
    ]
    if not result['by_rule']:
        out.append('  (the plan proposed nothing)')
    for agg in result['by_rule']:
        out.append(f"  [{agg['population']}] rule #{agg['live_rule_id']} "
                   f"“{agg['rule_name']}” → "
                   f"{agg['new_party_type']}: {agg['new_party']} — "
                   f"{agg['updated']} {verb.lower()}, "
                   f"{agg['already_correct']} already correct, "
                   f"{agg['stale']} stale")
    written = [r for r in result['results'] if r['outcome'] == 'updated']
    if written:
        out += ['', f'{verb} — (entry, offset account, party, rule)']
        for r in written:
            gl = (f", {r['gl_entries_written']} GL row(s)"
                  if r.get('gl_entries_written') else '')
            out.append(f"  {r['journal_entry']} {r['account']} → "
                       f"{r['new_party_type']}: {r['new_party']} "
                       f"(rule {r['live_rule_id']}{gl})")
    skipped = [r for r in result['results']
               if r['outcome'] in SKIPPED_OUTCOMES]
    if skipped:
        out += ['', 'SKIPPED — each one needs a human look']
        for r in skipped:
            out.append(f"  {r['outcome']}: {r['journal_entry']} "
                       f"{r.get('account') or ''} — {r.get('detail', '')}")
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


def write_log(path: str, result: dict) -> int:
    """One JSONL record per line touched — (JE docname, offset account, party
    type, party, rule id, outcome) — plus one per unrepairable JE carried from
    the plan. Returns the number of records written."""
    n = 0
    with open(path, 'a', encoding='utf-8') as fh:
        for r in result['results']:
            fh.write(json.dumps({'kind': 'line',
                                 'committed': result['committed'],
                                 'plan_generated_at':
                                     result.get('plan_generated_at'), **r},
                                sort_keys=True, default=str) + '\n')
            n += 1
        for r in result['review']:
            fh.write(json.dumps({'kind': 'review', **r},
                                sort_keys=True, default=str) + '\n')
            n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Apply the Journal Entry Party repair. DRY RUN unless '
                    '--commit; drafts only unless --include-submitted.')
    ap.add_argument('site', help='the Frappe site name (e.g. frontend)')
    ap.add_argument('plan', help='the plan JSON from phase 1')
    ap.add_argument('--commit', action='store_true',
                    help='actually write (default: dry run)')
    ap.add_argument('--include-submitted', action='store_true',
                    help='also repair SUBMITTED entries, writing both the JE '
                         'child row and its GL Entry rows. Without this only '
                         'drafts are touched.')
    ap.add_argument('--company', default='',
                    help='only touch entries belonging to this company')
    ap.add_argument('--log', default='',
                    help='append one JSON object per line to this file')
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
        result = apply_plan(frappe, plan, commit=args.commit,
                            include_submitted=args.include_submitted,
                            company=args.company)
        if args.commit and not result['aborted']:
            frappe.db.commit()
        elif args.commit:
            # Both tables move together or neither does.
            frappe.db.rollback()
    finally:
        frappe.destroy()

    print(report(result))
    if args.log:
        n = write_log(args.log, result)
        print(f'\n{n} audit record(s) appended to {args.log}')
    return 1 if result['aborted'] else 0


if __name__ == '__main__':
    sys.exit(main())
