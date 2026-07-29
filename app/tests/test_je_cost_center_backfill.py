# SPDX-License-Identifier: MIT
"""The two-phase repair of already-posted bank-tx Journal Entries (v0.8.5).

v0.8.5 stamps both JE legs going forward. This covers what fixes the entries
already on the books — ACC-JV-2026-02312 and the rest of the historical
population, whose cash leg fell through to "Main - OML".

  * scripts/plan_je_cost_center_backfill.py — reads Bank Bridge's rules and
    ERPNext's posted entries, WRITES NOTHING, emits a plan + a review list
  * scripts/backfill_je_cost_centers.py — runs inside the ERPNext container,
    DRY RUN unless --commit, writes both `Journal Entry Account` and `GL Entry`

Covered here:
  * the plan proposes the LIVE rule's cost center (an edit clones a rule, so
    the version that generated a 2024 entry is not the one carrying today's
    cost center) and counts correctly by rule
  * FAIL FORWARD — a JE with no rule, a deleted rule, a still-uncosted rule, a
    missing or cancelled entry, and an unidentifiable bank leg each land in
    `review` with a reason rather than being skipped in silence
  * lines already correct are not proposed again; a bank leg the rule sends to
    '(none)' is never CLEARED
  * FAIL SAFE — the applier's dry run writes nothing, `--commit` writes, a
    stale line is refused, and a second run is a no-op
  * every write is logged as (JE docname, old cc, new cc, rule id)

    cd app
    python3 -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import categorization, db  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        GeneratedJournalEntry, PlaidAccount)

from scripts import backfill_je_cost_centers as applier  # noqa: E402
from scripts import plan_je_cost_center_backfill as planner  # noqa: E402
from tests.test_categorization import Base  # noqa: E402

NONE = categorization.BANK_LEG_NO_COST_CENTER
EXPENSE = '6400 Professional Services - OML'
BANK_GL = '1261 Wells Fargo Checking - 3158 - OML'
GA = '310 - G and A Administration - OML'
MAIN = 'Main - OML'


class FakeJournalEntryClient:
    """The one call the planner makes of ERPNext: get_doc('Journal Entry', …).
    `docs` maps docname → doc; anything absent reads back as the real client's
    404 (None)."""

    def __init__(self, docs=None):
        self.docs = dict(docs or {})
        self.calls = []

    def get_doc(self, doctype, name):
        self.calls.append((doctype, name))
        return self.docs.get(name)


class FakeBenchFrappe:
    """A stand-in for the `frappe` module covering the surface the applier
    touches: db.get_value on a child row, get_all over GL Entry, db.set_value.

    Rows live in `tables` as {doctype: {docname: {...}}}, so an assertion reads
    the same table the script wrote."""

    def __init__(self, tables=None):
        self.tables = {dt: {n: dict(r) for n, r in rows.items()}
                       for dt, rows in (tables or {}).items()}
        self.writes = []
        self.committed = False
        self.db = self

    # ── frappe.db ───────────────────────────────────────────────────────
    def get_value(self, doctype, name, field, as_dict=False):
        row = self.tables.get(doctype, {}).get(name)
        if row is None:
            return None
        if isinstance(field, (list, tuple)):
            return ({f: row.get(f) for f in field} if as_dict
                    else [row.get(f) for f in field])
        return row.get(field)

    def set_value(self, doctype, name, field, value):
        self.tables.setdefault(doctype, {}).setdefault(name, {})[field] = value
        self.writes.append((doctype, name, field, value))

    def commit(self):
        self.committed = True

    # ── frappe module ───────────────────────────────────────────────────
    def get_all(self, doctype, filters=None, fields=None, order_by=None):
        out = []
        for name, row in self.tables.get(doctype, {}).items():
            if all(str(row.get(k, '')) == str(v)
                   for k, v in (filters or {}).items()):
                out.append({f: (name if f == 'name' else row.get(f))
                            for f in (fields or ['name'])})
        return out


class PlannerBase(Base):
    """Seeds one Plaid account whose GL account is the checking account the
    reported bug names, so the planner can identify a bank leg from data rather
    than from a hard-coded account number."""

    def setUp(self):
        super().setUp()
        db.session.add(PlaidAccount(
            account_id='acct-wf-checking', item_id='item-abc',
            name='WF Checking', mask='3158', type='depository',
            subtype='checking', erpnext_gl_account_name=BANK_GL,
            sync_enabled=True))
        db.session.commit()

    def _posted(self, rule, *, je='ACC-JV-2026-02312', tid='t-sorren',
               offset_cc=GA, bank_cc=MAIN, docstatus=1, amount=2030.0,
               rule_id=..., offset_account=EXPENSE, bank_account=BANK_GL):
        """One posted JE + the GeneratedJournalEntry and BankTransaction that
        produced it. Returns the ERPNext doc dict."""
        db.session.add(BankTransaction(
            plaid_transaction_id=tid, account_id='acct-wf-checking',
            amount=amount, merchant_name='Sorren', name='SORREN LLC',
            date=date(2026, 2, 14),
            erpnext_bank_transaction_id=f'ACC-BTN-{tid}'))
        db.session.add(GeneratedJournalEntry(
            plaid_transaction_id=tid,
            rule_id=(rule.id if rule_id is ... else rule_id),
            rule_name=(rule.name if rule is not None else ''),
            erpnext_journal_entry_name=je, state='approved', amount=amount))
        db.session.commit()
        doc = {
            'name': je, 'docstatus': docstatus,
            'accounts': [
                {'name': f'{je}-r1', 'account': offset_account,
                 'debit_in_account_currency': amount,
                 **({'cost_center': offset_cc} if offset_cc else {})},
                {'name': f'{je}-r2', 'account': bank_account,
                 'credit_in_account_currency': amount,
                 **({'cost_center': bank_cc} if bank_cc else {})},
            ]}
        return doc

    def _rule(self, **kw):
        defaults = dict(name='Sorren', priority=100, active=True,
                        match_type='merchant_contains', match_value='SORREN',
                        offset_account=EXPENSE, cost_center=GA)
        defaults.update(kw)
        rule = CategorizationRule(**defaults)
        db.session.add(rule)
        db.session.commit()
        return rule

    def _plan(self, docs):
        return planner.build_plan(FakeJournalEntryClient(docs))


# ── phase 1: the plan ───────────────────────────────────────────────────────
class PlanTest(PlannerBase):
    def test_the_reported_bug_the_bank_leg_is_proposed_for_repair(self):
        rule = self._rule()
        doc = self._posted(rule)
        plan = self._plan({doc['name']: doc})

        self.assertEqual(len(plan['changes']), 1)
        change = plan['changes'][0]
        self.assertEqual(change['journal_entry'], 'ACC-JV-2026-02312')
        self.assertEqual(change['account'], BANK_GL)
        self.assertEqual(change['old_cost_center'], MAIN)
        self.assertEqual(change['new_cost_center'], GA)
        self.assertEqual(change['live_rule_id'], rule.id)
        # The debit leg was already right and is not proposed again.
        self.assertEqual(plan['lines_already_correct'], 1)
        self.assertEqual(plan['review'], [])

    def test_it_writes_nothing(self):
        """The planner's whole safety story: it is a reader."""
        rule = self._rule()
        doc = self._posted(rule)
        before = json.dumps(doc, sort_keys=True)
        self._plan({doc['name']: doc})
        self.assertEqual(json.dumps(doc, sort_keys=True), before)

    def test_counts_by_rule_roll_up_entries_and_lines(self):
        rule = self._rule()
        docs = {}
        for i in range(3):
            d = self._posted(rule, je=f'ACC-JV-{i}', tid=f't{i}')
            docs[d['name']] = d
        plan = self._plan(docs)
        self.assertEqual(len(plan['changes']), 3)
        agg = plan['counts_by_rule'][0]
        self.assertEqual(agg['live_rule_id'], rule.id)
        self.assertEqual(agg['journal_entries'], 3)
        self.assertEqual(agg['lines'], 3)
        self.assertEqual(agg['new_cost_center'], GA)
        self.assertEqual(agg['from_cost_centers'], {MAIN: 3})

    def test_it_proposes_the_LIVE_rules_cost_center_not_the_one_that_fired(self):
        """An edit clones the rule. The 2026 entry names the archived version,
        which had no cost center; the cost center an operator has since set
        lives on its successor, and that is the value the books should get."""
        old = self._rule(cost_center=None)
        new = self._rule(cost_center=GA)
        old.superseded_by, old.archived, old.active = new.id, True, False
        db.session.commit()
        doc = self._posted(old)
        plan = self._plan({doc['name']: doc})

        self.assertEqual(len(plan['changes']), 1)
        self.assertEqual(plan['changes'][0]['rule_id'], old.id)
        self.assertEqual(plan['changes'][0]['live_rule_id'], new.id)
        self.assertEqual(plan['changes'][0]['new_cost_center'], GA)

    def test_a_line_already_correct_is_not_proposed(self):
        rule = self._rule()
        doc = self._posted(rule, bank_cc=GA)
        plan = self._plan({doc['name']: doc})
        self.assertEqual(plan['changes'], [])
        self.assertEqual(plan['lines_already_correct'], 2)

    def test_an_investment_je_is_not_examined_at_all(self):
        """Investment JEs come from per-Item dimensions (v0.8.3), not from a
        rule — a rule lookup for one would be meaningless."""
        rule = self._rule()
        doc = self._posted(rule, je='ACC-JV-INV', tid='inv-1')
        gje = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='inv-1').one()
        gje.plaid_investment_transaction_id = 'plaid-inv-1'
        db.session.commit()
        plan = self._plan({doc['name']: doc})
        self.assertEqual(plan['journal_entries_examined'], 0)
        self.assertEqual(plan['changes'], [])


class PlanBankLegOverrideTest(PlannerBase):
    def test_an_override_costs_the_two_legs_differently(self):
        rule = self._rule(bank_cost_center='Treasury - OML')
        doc = self._posted(rule)
        plan = self._plan({doc['name']: doc})
        by_account = {c['account']: c for c in plan['changes']}
        self.assertEqual(by_account[BANK_GL]['new_cost_center'],
                         'Treasury - OML')
        self.assertNotIn(EXPENSE, by_account)     # already GA

    def test_the_sentinel_never_CLEARS_a_posted_cost_center(self):
        """'(none)' means "let ERPNext default it" — which is the value already
        on the line. Writing '' would strip a cost center off an audited entry
        to replace it with the same thing."""
        rule = self._rule(bank_cost_center=NONE)
        doc = self._posted(rule)
        plan = self._plan({doc['name']: doc})
        self.assertEqual(plan['changes'], [])
        self.assertEqual(plan['review'], [])

    def test_an_unidentifiable_bank_leg_goes_to_review_rather_than_a_guess(self):
        """The override makes "which line is the bank leg" load-bearing. When
        the transaction's own GL account matches no line — a v0.5.15 Cash
        Clearing routing, a re-mapped account — the planner refuses to guess."""
        rule = self._rule(bank_cost_center='Treasury - OML')
        doc = self._posted(rule, bank_account='1099 Cash Clearing - OML')
        plan = self._plan({doc['name']: doc})
        self.assertEqual(plan['changes'], [])
        self.assertEqual([r['reason'] for r in plan['review']],
                         ['bank_leg_unidentified'])

    def test_a_mirroring_rule_needs_no_bank_leg_identification(self):
        """The common case must survive the same unmatched account: every line
        wants the same value, so the question never has to be answered."""
        rule = self._rule()
        doc = self._posted(rule, bank_account='1099 Cash Clearing - OML')
        plan = self._plan({doc['name']: doc})
        self.assertEqual(len(plan['changes']), 1)
        self.assertEqual(plan['changes'][0]['new_cost_center'], GA)


class PlanFailForwardTest(PlannerBase):
    """Every JE the repair cannot touch is REPORTED. A silently skipped entry
    is the failure mode this whole review step exists to prevent."""

    def _reasons(self, plan):
        return sorted(r['reason'] for r in plan['review'])

    def test_a_je_with_no_recorded_rule(self):
        doc = self._posted(None, rule_id=None)
        plan = self._plan({doc['name']: doc})
        self.assertEqual(self._reasons(plan), ['no_rule_recorded'])
        self.assertEqual(plan['changes'], [])

    def test_a_je_whose_rule_is_gone(self):
        rule = self._rule()
        doc = self._posted(rule)
        db.session.delete(rule)
        db.session.commit()
        plan = self._plan({doc['name']: doc})
        self.assertEqual(self._reasons(plan), ['rule_missing'])

    def test_a_rule_that_still_names_no_cost_center(self):
        """The actionable one: give the rule a cost center and re-plan."""
        rule = self._rule(cost_center=None)
        doc = self._posted(rule)
        plan = self._plan({doc['name']: doc})
        self.assertEqual(self._reasons(plan), ['rule_has_no_cost_center'])
        self.assertEqual(plan['review'][0]['live_rule_id'], rule.id)

    def test_a_je_erpnext_no_longer_has(self):
        rule = self._rule()
        self._posted(rule)
        plan = self._plan({})                 # ERPNext returns its 404
        self.assertEqual(self._reasons(plan), ['je_not_found'])

    def test_a_cancelled_je(self):
        rule = self._rule()
        doc = self._posted(rule, docstatus=2)
        plan = self._plan({doc['name']: doc})
        self.assertEqual(self._reasons(plan), ['je_cancelled'])

    def test_every_reason_is_documented(self):
        """A reason code with no explanation is a dead end for the operator
        holding the plan file."""
        rule = self._rule(cost_center=None)
        self._posted(rule)
        plan = self._plan({})
        for r in plan['review']:
            self.assertIn(r['reason'], planner.REVIEW_REASONS)
        self.assertIn('bank_leg_unidentified', planner.REVIEW_REASONS)

    def test_the_summary_names_the_entries_needing_review(self):
        rule = self._rule(cost_center=None)
        doc = self._posted(rule)
        text = planner.summarize(self._plan({doc['name']: doc}))
        self.assertIn('NEEDING REVIEW', text)
        self.assertIn('ACC-JV-2026-02312', text)
        self.assertIn('rule_has_no_cost_center', text)

    def test_one_bad_je_does_not_stop_the_good_ones(self):
        good = self._rule()
        bad = self._rule(name='Uncosted', match_value='NAPA', cost_center=None)
        d1 = self._posted(good, je='ACC-JV-1', tid='t1')
        d2 = self._posted(bad, je='ACC-JV-2', tid='t2')
        plan = self._plan({d1['name']: d1, d2['name']: d2})
        self.assertEqual(len(plan['changes']), 1)
        self.assertEqual(plan['changes'][0]['journal_entry'], 'ACC-JV-1')
        self.assertEqual(len(plan['review']), 1)


# ── phase 2: the apply ──────────────────────────────────────────────────────
class ApplyBase(unittest.TestCase):
    JE = 'ACC-JV-2026-02312'
    ROW = 'ACC-JV-2026-02312-r2'
    GL = 'GL-0002'

    def _frappe(self, *, je_cc=MAIN, gl_cc=MAIN, parent=None,
                voucher_detail_no=...):
        return FakeBenchFrappe({
            'Journal Entry Account': {
                self.ROW: {'cost_center': je_cc,
                           'parent': parent or self.JE,
                           'account': BANK_GL},
            },
            'GL Entry': {
                self.GL: {'voucher_type': 'Journal Entry', 'voucher_no': self.JE,
                          'voucher_detail_no': (self.ROW
                                                if voucher_detail_no is ...
                                                else voucher_detail_no),
                          'account': BANK_GL, 'cost_center': gl_cc,
                          'is_cancelled': 0},
            },
        })

    def _plan(self, **kw):
        change = {'journal_entry': self.JE, 'row': self.ROW,
                  'account': BANK_GL, 'old_cost_center': MAIN,
                  'new_cost_center': GA, 'rule_id': 12, 'live_rule_id': 31,
                  'rule_name': 'Sorren',
                  'plaid_transaction_id': 't-sorren'}
        change.update(kw)
        return {'plan_version': 1, 'generated_at': '2026-07-29T00:00:00+00:00',
                'changes': [change], 'review': []}


class ApplyDryRunTest(ApplyBase):
    def test_dry_run_is_the_default_and_writes_nothing(self):
        frappe = self._frappe()
        result = applier.apply_plan(frappe, self._plan())
        self.assertFalse(result['committed'])
        self.assertEqual(result['counts']['updated'], 1)
        self.assertEqual(frappe.writes, [])
        self.assertEqual(frappe.tables['Journal Entry Account'][self.ROW]
                         ['cost_center'], MAIN)
        self.assertEqual(frappe.tables['GL Entry'][self.GL]['cost_center'],
                         MAIN)

    def test_it_says_so_out_loud(self):
        text = applier.report(applier.apply_plan(self._frappe(), self._plan()))
        self.assertIn('DRY RUN', text)
        self.assertIn('--commit', text)
        self.assertIn('WOULD UPDATE', text)

    def test_it_reports_counts_by_rule(self):
        result = applier.apply_plan(self._frappe(), self._plan())
        agg = result['by_rule'][0]
        self.assertEqual(agg['live_rule_id'], 31)
        self.assertEqual(agg['rule_name'], 'Sorren')
        self.assertEqual(agg['updated'], 1)
        self.assertIn('Sorren', applier.report(result))


class ApplyCommitTest(ApplyBase):
    def test_commit_writes_both_the_form_row_and_the_ledger(self):
        """Only the second is what a Cost-Center-wise report reads — updating
        the JE alone would make the form look fixed and leave the books wrong."""
        frappe = self._frappe()
        result = applier.apply_plan(frappe, self._plan(), commit=True)
        self.assertTrue(result['committed'])
        self.assertEqual(result['counts']['updated'], 1)
        self.assertEqual(frappe.tables['Journal Entry Account'][self.ROW]
                         ['cost_center'], GA)
        self.assertEqual(frappe.tables['GL Entry'][self.GL]['cost_center'], GA)
        self.assertEqual(applier.report(result).splitlines()[0][:9], 'COMMITTED')

    def test_the_ledger_is_found_by_account_when_the_detail_link_is_missing(self):
        frappe = self._frappe(voucher_detail_no='')
        applier.apply_plan(frappe, self._plan(), commit=True)
        self.assertEqual(frappe.tables['GL Entry'][self.GL]['cost_center'], GA)

    def test_a_second_run_is_a_no_op(self):
        frappe = self._frappe()
        applier.apply_plan(frappe, self._plan(), commit=True)
        again = applier.apply_plan(frappe, self._plan(), commit=True)
        self.assertEqual(again['counts']['updated'], 0)
        self.assertEqual(again['counts']['already_correct'], 1)

    def test_every_write_is_logged_with_je_old_new_and_rule(self):
        frappe = self._frappe()
        result = applier.apply_plan(frappe, self._plan(), commit=True)
        fd, path = tempfile.mkstemp(suffix='.jsonl')
        os.close(fd)
        self.addCleanup(os.remove, path)
        self.assertEqual(applier.write_log(path, result), 1)
        with open(path, encoding='utf-8') as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec['journal_entry'], self.JE)
        self.assertEqual(rec['old_cost_center'], MAIN)
        self.assertEqual(rec['new_cost_center'], GA)
        self.assertEqual(rec['rule_id'], 12)
        self.assertEqual(rec['live_rule_id'], 31)
        self.assertEqual(rec['outcome'], 'updated')
        self.assertTrue(rec['committed'])

    def test_the_log_carries_the_unrepairable_entries_too(self):
        """The audit file has to stand alone: what changed AND what was left."""
        frappe = self._frappe()
        plan = self._plan()
        plan['review'] = [{'journal_entry': 'ACC-JV-9', 'rule_id': None,
                           'reason': 'no_rule_recorded'}]
        result = applier.apply_plan(frappe, plan, commit=True)
        fd, path = tempfile.mkstemp(suffix='.jsonl')
        os.close(fd)
        self.addCleanup(os.remove, path)
        applier.write_log(path, result)
        with open(path, encoding='utf-8') as fh:
            kinds = [json.loads(ln)['kind'] for ln in fh if ln.strip()]
        self.assertEqual(kinds, ['line', 'review'])


class ApplyRefusalTest(ApplyBase):
    """FAIL SAFE. A plan is a snapshot; the ledger may have moved under it."""

    def test_a_line_someone_else_changed_is_refused(self):
        frappe = self._frappe(je_cc='Irrigation - OML')
        result = applier.apply_plan(frappe, self._plan(), commit=True)
        self.assertEqual(result['counts']['stale'], 1)
        self.assertEqual(frappe.writes, [])
        self.assertIn('Irrigation - OML', applier.report(result))

    def test_a_row_that_moved_to_another_entry_is_refused(self):
        frappe = self._frappe(parent='ACC-JV-SOMETHING-ELSE')
        result = applier.apply_plan(frappe, self._plan(), commit=True)
        self.assertEqual(result['counts']['stale'], 1)
        self.assertEqual(frappe.writes, [])

    def test_a_vanished_row_is_refused(self):
        frappe = self._frappe()
        del frappe.tables['Journal Entry Account'][self.ROW]
        result = applier.apply_plan(frappe, self._plan(), commit=True)
        self.assertEqual(result['counts']['row_not_found'], 1)
        self.assertEqual(frappe.writes, [])

    def test_a_line_with_no_ledger_behind_it_is_refused_whole(self):
        """Refusing BOTH halves rather than fixing the form and leaving the
        ledger: a half-repair hides itself, which is worse than the bug."""
        frappe = self._frappe()
        frappe.tables['GL Entry'] = {}
        result = applier.apply_plan(frappe, self._plan(), commit=True)
        self.assertEqual(result['counts']['gl_not_found'], 1)
        self.assertEqual(frappe.writes, [])

    def test_every_refusal_is_listed_not_just_counted(self):
        frappe = self._frappe(je_cc='Irrigation - OML')
        text = applier.report(applier.apply_plan(frappe, self._plan()))
        self.assertIn('SKIPPED', text)
        self.assertIn(self.JE, text)

    def test_an_empty_plan_is_handled(self):
        result = applier.apply_plan(self._frappe(),
                                    {'changes': [], 'review': []})
        self.assertEqual(result['counts']['updated'], 0)
        self.assertIn('no changes', applier.report(result))


# ── the two phases agree on the wire format ─────────────────────────────────
class EndToEndTest(PlannerBase):
    def test_a_plan_from_phase_one_applies_in_phase_two(self):
        """The only contract between the two scripts is the JSON, so it is the
        thing worth asserting end to end."""
        rule = self._rule()
        doc = self._posted(rule)
        plan = json.loads(json.dumps(self._plan({doc['name']: doc})))

        row = plan['changes'][0]['row']
        frappe = FakeBenchFrappe({
            'Journal Entry Account': {
                row: {'cost_center': MAIN, 'parent': doc['name'],
                      'account': BANK_GL}},
            'GL Entry': {
                'GL-1': {'voucher_type': 'Journal Entry',
                         'voucher_no': doc['name'], 'voucher_detail_no': row,
                         'account': BANK_GL, 'cost_center': MAIN,
                         'is_cancelled': 0}},
        })
        dry = applier.apply_plan(frappe, plan)
        self.assertEqual(dry['counts']['updated'], 1)
        self.assertEqual(frappe.writes, [])

        wet = applier.apply_plan(frappe, plan, commit=True)
        self.assertEqual(wet['counts']['updated'], 1)
        self.assertEqual(frappe.tables['Journal Entry Account'][row]
                         ['cost_center'], GA)
        self.assertEqual(frappe.tables['GL Entry']['GL-1']['cost_center'], GA)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
