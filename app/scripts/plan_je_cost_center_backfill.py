# SPDX-License-Identifier: MIT
"""PLAN the v0.8.5 cost-center repair of already-posted bank-transaction Journal
Entries. Phase 1 of 2 — this script WRITES NOTHING ANYWHERE.

    THE BUG IT REPAIRS.  Through v0.8.4 a rule's Cost Center rode the OFFSET
    line of the generated Journal Entry and the bank line was left blank, at
    which point ERPNext's own fallback filled it with the COMPANY DEFAULT. So
    ACC-JV-2026-02312 (Sorren, $2,030) posted line 1 — 6400 Professional
    Services — to "310 - G and A Administration - OML" and line 2 — 1261 Wells
    Fargo Checking — to "Main - OML". The cost sat in one segment and the cash
    that paid it in another, across the whole historical population.

    v0.8.5 fixes it going forward (categorization.apply_rule_dimensions). This
    pair of scripts fixes what is already on the books.

WHY TWO SCRIPTS. The two facts needed to repair one line live in two different
databases. The RULE that matched a transaction — and the cost center it now
names — is in Bank Bridge's Postgres; the posted Journal Entry is in ERPNext's
MariaDB, and its cost_center is not writable through the REST API once the
entry is submitted. So this script reads both sides (Postgres directly, ERPNext
over REST) and emits a PLAN; scripts/backfill_je_cost_centers.py runs inside the
ERPNext container and applies it.

The plan file is also the audit artifact. It records, per line, the JE docname,
the child row, the account, the cost center now, the cost center proposed, and
the rule ids that justify it — reviewable before a single write happens, and
keepable afterwards as the record of why the books changed.

FAIL SAFE. Nothing here mutates anything: worst case is a stale plan, and the
applier re-checks every line against the live value before writing.

FAIL FORWARD. Every Journal Entry that CANNOT be repaired lands in `review`
with a reason. None is silently dropped — a JE whose rule was deleted, or whose
rule still names no cost center, is exactly the thing an operator needs to see.

KAIROS. This is a one-time repair an operator runs when the rules are right,
not a scheduled job. Nothing calls it; there is no runtime tool for it.

    # inside the Bank Bridge container
    python3 -m scripts.plan_je_cost_center_backfill --out /tmp/je_cc_plan.json

    # review the summary it prints, then hand the plan to phase 2:
    #   scripts/backfill_je_cost_centers.py
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

# The three states a line can be in, reported separately because they mean
# different things to an operator: `changes` is work to do, `already_correct`
# is work v0.8.5 already did (or a prior run of this repair), and `review` is
# work only a human can decide.
REVIEW_REASONS = {
    'no_rule_recorded': 'the JE records no rule_id — generated before the '
                        'rules engine, or by a path that does not name one',
    'rule_missing': 'the recorded rule id is gone from the rules table',
    'rule_has_no_cost_center': 'the rule that matched (or the live rule that '
                               'superseded it) still names no cost center — '
                               'set one on the rule, then re-plan',
    'je_not_found': 'ERPNext has no Journal Entry by that docname',
    'je_cancelled': 'the Journal Entry is cancelled (docstatus 2)',
    'bank_leg_unidentified': "the rule overrides the bank leg's cost center, "
                             'but no JE line matches this transaction’s bank '
                             'GL account — which line is the bank leg cannot '
                             'be decided from the data',
}


def live_rule(rule):
    """Walk `superseded_by` forward to the rule version that is live today.

    An edit CLONES a rule and archives the original (see save_rule), so the
    version that generated a 2024 Journal Entry is usually NOT the version an
    operator has since given a cost center to. The live successor is the one
    whose cost center the books should now carry — that is what "a matching
    rule NOW has a proper cost center" means. Returns the live rule, or None if
    the chain dead-ends."""
    from app.models import CategorizationRule
    from app import db
    seen = set()
    while rule is not None and rule.superseded_by:
        if rule.id in seen:                 # a cycle can only be corruption
            return None
        seen.add(rule.id)
        rule = db.session.get(CategorizationRule, rule.superseded_by)
    return rule


def _je_lines(doc: dict) -> list:
    """The Journal Entry's account rows, as dicts. Frappe returns them under
    `accounts`; a doc fetched without children yields []."""
    return [ln for ln in (doc.get('accounts') or []) if isinstance(ln, dict)]


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


def _desired_for_lines(rule, lines: list, bank_account: str):
    """[(line, desired_cost_center)] for every line, or None when the bank leg
    matters and cannot be identified.

    THE COMMON CASE NEEDS NO CLASSIFICATION. When the rule mirrors (the v0.8.5
    default) every line wants the same value, so which line is the bank leg is
    a question that never has to be answered — and answering questions the data
    doesn't force is how a backfill guesses wrong at scale.

    Only a rule that OVERRIDES its bank leg makes the distinction load-bearing,
    and there the bank GL account from the transaction's Plaid account is the
    authority. No match, no guess: the caller sends the JE to review."""
    from app import categorization
    offset_cc = (getattr(rule, 'cost_center', None) or '').strip()
    bank_cc = categorization.bank_leg_cost_center(rule)
    if bank_cc == offset_cc:
        return [(ln, offset_cc) for ln in lines]
    if not bank_account or not any(
            (ln.get('account') or '') == bank_account for ln in lines):
        return None
    return [(ln, bank_cc if (ln.get('account') or '') == bank_account
             else offset_cc) for ln in lines]


def build_plan(client, *, limit: int | None = None) -> dict:
    """Read every rules-generated bank-transaction Journal Entry and return the
    plan dict. Pure read: `client` is only ever asked for documents."""
    from app.models import CategorizationRule, GeneratedJournalEntry
    from app import db

    q = (GeneratedJournalEntry.query
         .filter(GeneratedJournalEntry.erpnext_journal_entry_name.isnot(None),
                 GeneratedJournalEntry.erpnext_journal_entry_name != '',
                 # Investment JEs are posted by invest_je.py from per-Item
                 # dimensions, not from a categorization rule — v0.8.3 already
                 # fixed their cost centers and a rule lookup here would be
                 # meaningless. This column is the discriminator (see the model).
                 GeneratedJournalEntry.plaid_investment_transaction_id.is_(None))
         .order_by(GeneratedJournalEntry.id))
    if limit:
        q = q.limit(limit)

    changes, review, already_correct = [], [], 0
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
        if not (rule.cost_center or '').strip():
            review.append({**ctx, 'reason': 'rule_has_no_cost_center',
                           'live_rule_id': rule.id,
                           'live_rule_name': rule.name or ''})
            continue

        doc = client.get_doc('Journal Entry', je_name)
        if not doc:
            review.append({**ctx, 'reason': 'je_not_found',
                           'live_rule_id': rule.id})
            continue
        if int(doc.get('docstatus') or 0) == 2:
            review.append({**ctx, 'reason': 'je_cancelled',
                           'live_rule_id': rule.id})
            continue

        lines = _je_lines(doc)
        desired = _desired_for_lines(rule, lines, _bank_gl_account(gje))
        if desired is None:
            review.append({**ctx, 'reason': 'bank_leg_unidentified',
                           'live_rule_id': rule.id})
            continue

        for line, want in desired:
            current = (line.get('cost_center') or '').strip()
            # NEVER CLEAR. A `want` of '' means the rule deliberately hands
            # this leg to ERPNext's own default — which is precisely the value
            # already sitting there. Writing '' would strip a cost center off a
            # posted, audited entry to replace it with the same thing.
            if not want or current == want:
                already_correct += 1
                continue
            changes.append({
                'journal_entry': je_name,
                'row': line.get('name') or '',
                'account': line.get('account') or '',
                'old_cost_center': current,
                'new_cost_center': want,
                'rule_id': gje.rule_id,
                'live_rule_id': rule.id,
                'rule_name': rule.name or gje.rule_name or '',
                'plaid_transaction_id': gje.plaid_transaction_id,
            })

    return {
        'plan_version': 1,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'journal_entries_examined': examined,
        'lines_already_correct': already_correct,
        'counts_by_rule': counts_by_rule(changes),
        'changes': changes,
        'review': review,
    }


def counts_by_rule(changes: list) -> list:
    """Proposed changes rolled up per LIVE rule — the summary an operator reads
    before authorizing anything ("47 entries under Sorren → 310 - G and A")."""
    by: dict = {}
    for c in changes:
        key = c['live_rule_id']
        agg = by.setdefault(key, {
            'live_rule_id': key, 'rule_name': c['rule_name'],
            'new_cost_center': c['new_cost_center'],
            'journal_entries': set(), 'lines': 0, 'from_cost_centers': {}})
        agg['journal_entries'].add(c['journal_entry'])
        agg['lines'] += 1
        old = c['old_cost_center'] or '(blank)'
        agg['from_cost_centers'][old] = agg['from_cost_centers'].get(old, 0) + 1
    out = []
    for agg in by.values():
        out.append({**agg, 'journal_entries': len(agg['journal_entries'])})
    return sorted(out, key=lambda a: -a['lines'])


def summarize(plan: dict) -> str:
    """The human-readable digest printed to stdout (and worth pasting into a
    commit message or a note to the accountant)."""
    lines = [
        f"Journal Entries examined : {plan['journal_entries_examined']}",
        f"Lines already correct    : {plan['lines_already_correct']}",
        f"Lines to change          : {len(plan['changes'])}",
        f"Needing review           : {len(plan['review'])}",
        '',
        'BY RULE',
    ]
    if not plan['counts_by_rule']:
        lines.append('  (nothing to change)')
    for agg in plan['counts_by_rule']:
        froms = ', '.join(f'{k} ×{v}' for k, v in
                          sorted(agg['from_cost_centers'].items(),
                                 key=lambda kv: -kv[1]))
        lines.append(
            f"  rule #{agg['live_rule_id']} “{agg['rule_name']}” → "
            f"{agg['new_cost_center']}: {agg['lines']} line(s) across "
            f"{agg['journal_entries']} entry(ies)   [from: {froms}]")
    if plan['review']:
        lines += ['', 'NEEDING REVIEW (nothing will be written for these)']
        by_reason: dict = {}
        for r in plan['review']:
            by_reason.setdefault(r['reason'], []).append(r)
        for reason, rows in sorted(by_reason.items()):
            lines.append(f"  {reason} ×{len(rows)} — "
                         f"{REVIEW_REASONS.get(reason, '')}")
            for r in rows[:10]:
                lines.append(f"      {r['journal_entry']} "
                             f"(rule {r.get('rule_id')} "
                             f"“{r.get('rule_name', '')}”)")
            if len(rows) > 10:
                lines.append(f'      … and {len(rows) - 10} more '
                             f'(all listed in the plan file)')
    return '\n'.join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Plan the v0.8.5 bank-tx Journal Entry cost-center repair. '
                    'Writes nothing but the plan file.')
    ap.add_argument('--out', default='',
                    help='write the plan JSON here (default: stdout only)')
    ap.add_argument('--limit', type=int, default=0,
                    help='examine at most N Journal Entries (a smoke test)')
    args = ap.parse_args(argv)

    from app import create_app
    from app import erpnext_bank
    app = create_app()
    with app.app_context():
        client = erpnext_bank.get_client()
        plan = build_plan(client, limit=args.limit or None)
    print(summarize(plan))
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            json.dump(plan, fh, indent=2, sort_keys=True)
        print(f'\nplan written to {args.out} '
              f'({len(plan["changes"])} change(s) to apply)')
        print('Next: scripts/backfill_je_cost_centers.py — DRY RUN first, '
              'then --commit.')
    else:
        print('\n(no --out given, so no plan file was written)')
    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
