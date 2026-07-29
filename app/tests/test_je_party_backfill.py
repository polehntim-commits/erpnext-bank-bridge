# SPDX-License-Identifier: MIT
"""The v0.9.0 Journal Entry Party backfill — planner and applier.

WHAT IS BEING REPAIRED. Tim drilled into ACC-JV-2026-02312 (Sorren, $2,030) and
found Party Type and Party empty on both lines. Two different causes, and the
tests below keep them apart because only one is repairable:

  * v0.4.0.9's eligibility rule was NARROWER than ERPNext's — it refused Equity
    accounts and accounts with a blank `account_type`, both of which ERPNext
    accepts. Those entries are repairable, and are what the plan is for.
  * Sorren's own offset, '6400 - Professional Services', is `account_type =
    'Expense Account'`, which ERPNext genuinely refuses. No backfill fixes that;
    it must land in `review` naming the ways out.

The properties under test, not just the arithmetic:

  * FAIL SAFE — the planner writes NOTHING (not even an auto-created Supplier,
    which is why it does not call categorization.resolve_party). The applier is
    DRY RUN unless --commit.
  * DRAFTS ONLY — `party` is not allow_on_submit, so a submitted entry is
    reported with its proposed party, never written. The applier re-checks
    docstatus itself, because a plan is a file and files get edited.
  * FAIL FORWARD — every unrepairable entry lands in `review` with a documented
    reason. None is silently skipped.
  * IDEMPOTENT — a second run reports `already_correct` and writes nothing,
    which is also what makes an interrupted first run safe to re-run.

Synthetic amounts only.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, erpnext_settings  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        GeneratedJournalEntry, PlaidAccount, PlaidItem)

from scripts import backfill_je_parties as applier  # noqa: E402
from scripts import plan_je_party_backfill as planner  # noqa: E402
from tests.fakes import FakeERPClient  # noqa: E402

COMPANY = 'Orchard Example, LLC'
BANK_GL = '1261 - Wells Fargo Checking - OML'
# The real shape of the reported bug: an EXPLICITLY typed expense account.
EXPENSE_TYPED = '6400 - Professional Services - OML'
# The shape v0.4.0.9 wrongly refused: no account_type at all.
EXPENSE_UNTYPED = '5201 - Administrative Expenses - OML'
PAYABLE = '2110 - Creditors - OML'
EQUITY = '3201 - Member Distribution - OML'

CHART = [
    {'name': BANK_GL, 'account_name': 'Wells Fargo Checking',
     'company': COMPANY, 'root_type': 'Asset', 'account_type': 'Bank'},
    {'name': EXPENSE_TYPED, 'account_name': 'Professional Services',
     'company': COMPANY, 'root_type': 'Expense',
     'account_type': 'Expense Account'},
    {'name': EXPENSE_UNTYPED, 'account_name': 'Administrative Expenses',
     'company': COMPANY, 'root_type': 'Expense'},
    {'name': PAYABLE, 'account_name': 'Creditors', 'company': COMPANY,
     'root_type': 'Liability', 'account_type': 'Payable'},
    {'name': EQUITY, 'account_name': 'Member Distribution',
     'company': COMPANY, 'root_type': 'Equity', 'account_type': 'Equity'},
]


class BenchFrappe:
    """Stand-in for the `frappe` module, covering the surface the applier uses:
    db.get_value on a child row and on the parent's docstatus, db.set_value in
    its DICT form, db.exists for the party, and get_cached_value for the
    account_type re-check.

    Rows live in `tables` as {doctype: {docname: {...}}}, so an assertion reads
    the same table the script wrote."""

    def __init__(self, tables=None, accounts=None, parties=None):
        self.tables = {dt: {n: dict(r) for n, r in rows.items()}
                       for dt, rows in (tables or {}).items()}
        self.accounts = dict(accounts or {})
        self.parties = {k: set(v) for k, v in (parties or {}).items()}
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

    def set_value(self, doctype, name, field, value=None):
        target = self.tables.setdefault(doctype, {}).setdefault(name, {})
        updates = field if isinstance(field, dict) else {field: value}
        target.update(updates)
        self.writes.append((doctype, name, dict(updates)))

    def exists(self, doctype, name):
        return name in self.parties.get(doctype, set())

    def commit(self):
        self.committed = True

    # ── frappe module ───────────────────────────────────────────────────
    def get_cached_value(self, doctype, name, field):
        if doctype == 'Account':
            return (self.accounts.get(name) or {}).get(field)
        return None


class Base(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir, 'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', COMPANY)
        db.session.add(PlaidItem(item_id='item-1', institution_name='WF',
                                 access_token_encrypted='x'))
        db.session.add(PlaidAccount(
            account_id='acct-checking', item_id='item-1', name='WF Checking',
            mask='3158', type='depository', subtype='checking',
            erpnext_gl_account_name=BANK_GL, sync_enabled=True))
        db.session.commit()
        self.erp = FakeERPClient(chart_accounts=CHART, companies=[COMPANY],
                                 company_abbr='OML',
                                 existing_suppliers=['Sorren'])

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        os.close(self._dbfd)
        os.unlink(self._dbpath)

    # ── fixtures ────────────────────────────────────────────────────────────

    def _rule(self, **kw):
        kw.setdefault('name', 'Sorren (accounting)')
        kw.setdefault('match_type', 'merchant_contains')
        kw.setdefault('match_value', 'Sorren')
        kw.setdefault('offset_account', EXPENSE_UNTYPED)
        kw.setdefault('offset_direction', 'auto')
        kw.setdefault('party_type', 'Supplier')
        kw.setdefault('party_name', 'Sorren')
        kw.setdefault('applies_to_company', COMPANY)
        rule = CategorizationRule(**kw)
        db.session.add(rule)
        db.session.commit()
        return rule

    def _posted(self, rule, *, je='ACC-JV-2026-02312', tid='t-sorren',
                docstatus=0, offset_account=None, party_type=None, party=None,
                rule_id=...):
        """One JE in ERPNext plus the local rows that produced it. Returns the
        ERPNext doc dict, whose `accounts` rows carry child docnames like a real
        Frappe fetch."""
        offset = offset_account or (rule.offset_account if rule else
                                    EXPENSE_UNTYPED)
        db.session.add(BankTransaction(
            plaid_transaction_id=tid, account_id='acct-checking',
            amount=2030.0, merchant_name='Sorren', name='SORREN LLC',
            date=date(2026, 3, 6),
            erpnext_bank_transaction_id=f'ACC-BTN-{tid}'))
        db.session.add(GeneratedJournalEntry(
            plaid_transaction_id=tid,
            rule_id=(rule.id if rule_id is ... and rule else rule_id),
            rule_name=(rule.name if rule else ''),
            erpnext_journal_entry_name=je, amount=2030.0,
            state='approved' if docstatus == 1 else 'pending_review'))
        db.session.commit()
        doc = {
            'name': je, 'docstatus': docstatus, 'company': COMPANY,
            'accounts': [
                {'name': f'{je}-r1', 'account': offset,
                 'debit_in_account_currency': 2030.0,
                 'party_type': party_type, 'party': party},
                {'name': f'{je}-r2', 'account': BANK_GL,
                 'credit_in_account_currency': 2030.0,
                 'party_type': None, 'party': None},
            ],
        }
        self.erp.docs[je] = doc
        return doc

    def _bench(self, plan, *, docstatus=0, party_types=('Supplier',)):
        """A BenchFrappe seeded from a plan, so the applier tests start from
        exactly the state the planner emitted."""
        je_rows = {}
        je_parents = {}
        for change in plan['changes']:
            je_rows[change['row']] = {
                'party_type': change['old_party_type'] or None,
                'party': change['old_party'] or None,
                'parent': change['journal_entry'],
                'account': change['account'],
            }
            je_parents[change['journal_entry']] = {'docstatus': docstatus}
        return BenchFrappe(
            tables={'Journal Entry Account': je_rows,
                    'Journal Entry': je_parents},
            # dict(a), not a — a test that reclassifies an account must not
            # mutate the module-level CHART and pollute every later test in the
            # process. (It did, and the suite caught it.)
            accounts={a['name']: dict(a) for a in CHART},
            parties={pt: ['Sorren'] for pt in party_types})


# ── the planner ─────────────────────────────────────────────────────────────

class PlannerWritesNothing(Base):
    def test_the_planner_creates_no_erpnext_documents(self):
        """FAIL SAFE · a planner that mints a Supplier as a side effect of
        PLANNING is not a planner. This is why it does not call
        categorization.resolve_party, which ensures the party exists."""
        rule = self._rule(party_name='Never Heard Of Them')
        self._posted(rule)

        planner.build_plan(self.erp)

        self.assertEqual(self.erp.created['Supplier'], {})
        self.assertEqual(self.erp.created['Customer'], {})
        self.assertEqual(self.erp.created['Journal Entry'], {})
        self.assertEqual(self.erp.submitted, set())


class PlannerFindsRepairableEntries(Base):
    def test_an_untyped_expense_offset_is_repairable(self):
        """The clause v0.4.0.9 missed: a blank account_type skips ERPNext's
        check, so a Supplier is legal there. 29 of the live chart's leaf
        accounts are in this state."""
        rule = self._rule(offset_account=EXPENSE_UNTYPED)
        self._posted(rule)

        plan = planner.build_plan(self.erp)

        self.assertEqual(len(plan['changes']), 1)
        change = plan['changes'][0]
        self.assertEqual(change['journal_entry'], 'ACC-JV-2026-02312')
        self.assertEqual(change['row'], 'ACC-JV-2026-02312-r1')
        self.assertEqual(change['account'], EXPENSE_UNTYPED)
        self.assertEqual(change['new_party_type'], 'Supplier')
        self.assertEqual(change['new_party'], 'Sorren')
        self.assertEqual(change['old_party'], '')
        self.assertEqual(change['docstatus'], 0)

    def test_an_equity_offset_is_repairable(self):
        """The other missed clause. ERPNext allows a Party on Equity, which is
        what makes an owner draw attributable."""
        rule = self._rule(offset_account=EQUITY, party_type='Supplier')
        self._posted(rule, offset_account=EQUITY)

        plan = planner.build_plan(self.erp)

        self.assertEqual(len(plan['changes']), 1)
        self.assertEqual(plan['changes'][0]['account'], EQUITY)

    def test_a_payable_offset_is_repairable(self):
        rule = self._rule(offset_account=PAYABLE)
        self._posted(rule, offset_account=PAYABLE)

        plan = planner.build_plan(self.erp)

        self.assertEqual(len(plan['changes']), 1)
        self.assertEqual(plan['changes'][0]['account_type'], 'Payable')

    def test_the_party_rides_the_offset_line_not_the_bank_line(self):
        """A bank line is account_type 'Bank', which ERPNext never accepts a
        Party on — so only one of the two rows may be named."""
        rule = self._rule()
        self._posted(rule)

        plan = planner.build_plan(self.erp)

        self.assertEqual(plan['changes'][0]['row'], 'ACC-JV-2026-02312-r1')
        self.assertNotEqual(plan['changes'][0]['account'], BANK_GL)

    def test_the_live_successor_rules_party_is_used_not_the_archived_one(self):
        """An edit CLONES a rule, so the version that generated a March entry is
        not the version an operator has since given a party to."""
        old = self._rule(name='Sorren (old)', party_type=None, party_name=None)
        new = self._rule(name='Sorren (live)', party_type='Supplier',
                         party_name='Sorren')
        old.superseded_by = new.id
        old.archived = True
        db.session.commit()
        self._posted(old)

        plan = planner.build_plan(self.erp)

        self.assertEqual(len(plan['changes']), 1)
        self.assertEqual(plan['changes'][0]['live_rule_id'], new.id)
        self.assertEqual(plan['changes'][0]['new_party'], 'Sorren')


class PlannerFailsForward(Base):
    def _reasons(self, plan):
        return {r['reason'] for r in plan['review']}

    def test_the_sorren_case_is_reported_as_ineligible_not_dropped(self):
        """THE REPORTED BUG, and the honest answer to it. '6400 - Professional
        Services' is explicitly `account_type='Expense Account'`; ERPNext throws
        on a Party there at submit. No backfill fixes it — but the operator has
        to be TOLD, with the party that was wanted and the account that refused
        it."""
        rule = self._rule(offset_account=EXPENSE_TYPED)
        self._posted(rule, offset_account=EXPENSE_TYPED)

        plan = planner.build_plan(self.erp)

        self.assertEqual(plan['changes'], [])
        self.assertEqual(len(plan['review']), 1)
        row = plan['review'][0]
        self.assertEqual(row['reason'], 'offset_ineligible')
        self.assertEqual(row['offset_account'], EXPENSE_TYPED)
        self.assertEqual(row['offset_account_type'], 'Expense Account')
        self.assertEqual(row['party_type'], 'Supplier')
        self.assertEqual(row['party'], 'Sorren')
        # The reason must name the ways out, since it is all the operator sees.
        detail = planner.REVIEW_REASONS['offset_ineligible']
        self.assertIn('no account_type set', detail)
        self.assertIn('Accounts Payable', detail)

    def test_a_submitted_entry_is_reported_never_planned(self):
        """`party` is not allow_on_submit. The proposal is recorded so the
        operator's cancel+amend decision is informed rather than blind."""
        rule = self._rule()
        self._posted(rule, docstatus=1)

        plan = planner.build_plan(self.erp)

        self.assertEqual(plan['changes'], [])
        row = plan['review'][0]
        self.assertEqual(row['reason'], 'submitted_not_writable')
        self.assertEqual(row['new_party_type'], 'Supplier')
        self.assertEqual(row['new_party'], 'Sorren')

    def test_a_party_missing_from_erpnext_is_reported(self):
        rule = self._rule(party_name='Nobody Ltd')
        self._posted(rule)

        plan = planner.build_plan(self.erp)

        self.assertEqual(plan['changes'], [])
        self.assertEqual(self._reasons(plan), {'party_not_in_erpnext'})

    def test_a_rule_with_no_party_is_reported(self):
        rule = self._rule(party_type=None, party_name=None)
        self._posted(rule)

        plan = planner.build_plan(self.erp)

        self.assertEqual(self._reasons(plan), {'rule_has_no_party'})

    def test_a_skip_party_rule_is_reported_not_forced(self):
        rule = self._rule(skip_party=True)
        self._posted(rule)

        plan = planner.build_plan(self.erp)

        self.assertEqual(plan['changes'], [])
        self.assertEqual(self._reasons(plan), {'rule_skips_party'})

    def test_a_je_with_no_recorded_rule_is_reported(self):
        self._posted(None, rule_id=None)

        plan = planner.build_plan(self.erp)

        self.assertEqual(self._reasons(plan), {'no_rule_recorded'})

    def test_a_cancelled_je_is_reported(self):
        rule = self._rule()
        self._posted(rule, docstatus=2)

        plan = planner.build_plan(self.erp)

        self.assertEqual(self._reasons(plan), {'je_cancelled'})

    def test_a_line_already_correct_is_counted_not_re_proposed(self):
        rule = self._rule()
        self._posted(rule, party_type='Supplier', party='Sorren')

        plan = planner.build_plan(self.erp)

        self.assertEqual(plan['changes'], [])
        self.assertEqual(plan['lines_already_correct'], 1)

    def test_every_review_reason_is_documented(self):
        """FAIL FORWARD · a reason with no explanation is one an operator cannot
        act on."""
        for reason, text in planner.REVIEW_REASONS.items():
            self.assertTrue(text.strip(), reason)

    def test_the_summary_names_the_ineligible_account_type(self):
        rule = self._rule(offset_account=EXPENSE_TYPED)
        self._posted(rule, offset_account=EXPENSE_TYPED)

        text = planner.summarize(planner.build_plan(self.erp))

        self.assertIn('offset_ineligible', text)
        self.assertIn('Expense Account', text)


# ── the applier ─────────────────────────────────────────────────────────────

class ApplierBase(Base):
    def _plan(self, **kw):
        rule = self._rule(**kw)
        self._posted(rule)
        return planner.build_plan(self.erp)


class ApplierIsDryRunByDefault(ApplierBase):
    def test_dry_run_writes_nothing(self):
        plan = self._plan()
        frappe = self._bench(plan)

        result = applier.apply_plan(frappe, plan)

        self.assertEqual(result['counts']['updated'], 1)
        self.assertFalse(result['committed'])
        self.assertEqual(frappe.writes, [])
        row = frappe.tables['Journal Entry Account']['ACC-JV-2026-02312-r1']
        self.assertIsNone(row['party'])

    def test_commit_writes_the_party_onto_the_draft(self):
        plan = self._plan()
        frappe = self._bench(plan)

        result = applier.apply_plan(frappe, plan, commit=True)

        self.assertEqual(result['counts']['updated'], 1)
        row = frappe.tables['Journal Entry Account']['ACC-JV-2026-02312-r1']
        self.assertEqual(row['party_type'], 'Supplier')
        self.assertEqual(row['party'], 'Sorren')

    def test_a_draft_needs_no_gl_write(self):
        """A draft has produced no GL Entries — they are created at submit — so
        one child-row field is the whole repair, and ERPNext then validates the
        party on submit. That is what makes drafts the safe population."""
        plan = self._plan()
        frappe = self._bench(plan)

        applier.apply_plan(frappe, plan, commit=True)

        self.assertEqual([w for w in frappe.writes if w[0] == 'GL Entry'], [])
        self.assertEqual({w[0] for w in frappe.writes},
                         {'Journal Entry Account'})

    def test_it_is_idempotent(self):
        plan = self._plan()
        frappe = self._bench(plan)
        applier.apply_plan(frappe, plan, commit=True)

        second = applier.apply_plan(frappe, plan, commit=True)

        self.assertEqual(second['counts']['updated'], 0)
        self.assertEqual(second['counts']['already_correct'], 1)


class ApplierRefusesUnsafeWrites(ApplierBase):
    def test_a_draft_submitted_since_the_plan_was_built_is_refused(self):
        """THE GUARD THAT MATTERS. The planner excluded submitted entries, but a
        plan is a file and files get edited — and someone may simply have
        submitted this draft in the meantime. `party` is not allow_on_submit."""
        plan = self._plan()
        frappe = self._bench(plan, docstatus=1)

        result = applier.apply_plan(frappe, plan, commit=True)

        self.assertEqual(result['counts']['updated'], 0)
        self.assertEqual(result['counts']['now_submitted'], 1)
        self.assertEqual(frappe.writes, [])
        self.assertIn('cancel + amend', result['results'][0]['detail'])

    def test_an_account_reclassified_since_the_plan_is_refused(self):
        """Eligibility is re-checked against the LIVE account_type. Writing a
        party ERPNext now refuses would make a repairable draft unsubmittable."""
        plan = self._plan()
        frappe = self._bench(plan)
        frappe.accounts[EXPENSE_UNTYPED]['account_type'] = 'Expense Account'

        result = applier.apply_plan(frappe, plan, commit=True)

        self.assertEqual(result['counts']['ineligible_account'], 1)
        self.assertEqual(frappe.writes, [])

    def test_a_blank_account_type_is_still_eligible_at_apply_time(self):
        """The applier restates ERPNext's `if account_type and ...` guard, so it
        must not treat "no type" as "not allowed"."""
        plan = self._plan()
        frappe = self._bench(plan)

        result = applier.apply_plan(frappe, plan, commit=True)

        self.assertEqual(result['counts']['updated'], 1)

    def test_a_party_that_vanished_is_refused(self):
        plan = self._plan()
        frappe = self._bench(plan)
        frappe.parties['Supplier'] = set()

        result = applier.apply_plan(frappe, plan, commit=True)

        self.assertEqual(result['counts']['party_missing'], 1)
        self.assertEqual(frappe.writes, [])

    def test_a_line_edited_since_the_plan_is_reported_stale_not_clobbered(self):
        plan = self._plan()
        frappe = self._bench(plan)
        frappe.tables['Journal Entry Account']['ACC-JV-2026-02312-r1'].update(
            {'party_type': 'Customer', 'party': 'Someone Else'})

        result = applier.apply_plan(frappe, plan, commit=True)

        self.assertEqual(result['counts']['stale'], 1)
        self.assertEqual(frappe.writes, [])
        row = frappe.tables['Journal Entry Account']['ACC-JV-2026-02312-r1']
        self.assertEqual(row['party'], 'Someone Else')

    def test_a_row_belonging_to_another_entry_is_stale_identity(self):
        plan = self._plan()
        frappe = self._bench(plan)
        frappe.tables['Journal Entry Account'][
            'ACC-JV-2026-02312-r1']['parent'] = 'ACC-JV-SOMETHING-ELSE'

        result = applier.apply_plan(frappe, plan, commit=True)

        self.assertEqual(result['counts']['stale'], 1)
        self.assertEqual(frappe.writes, [])

    def test_a_missing_row_is_reported(self):
        plan = self._plan()
        frappe = self._bench(plan)
        del frappe.tables['Journal Entry Account']['ACC-JV-2026-02312-r1']

        result = applier.apply_plan(frappe, plan, commit=True)

        self.assertEqual(result['counts']['row_not_found'], 1)


class ApplierReporting(ApplierBase):
    def test_the_dry_run_report_says_it_wrote_nothing(self):
        plan = self._plan()
        frappe = self._bench(plan)

        text = applier.report(applier.apply_plan(frappe, plan))

        self.assertIn('DRY RUN', text)
        self.assertIn('--commit', text)

    def test_the_committed_report_says_so(self):
        plan = self._plan()
        frappe = self._bench(plan)

        text = applier.report(
            applier.apply_plan(frappe, plan, commit=True))

        self.assertIn('COMMITTED', text)

    def test_every_skip_appears_in_the_report(self):
        """FAIL FORWARD · a partial run must say exactly what it did not do."""
        plan = self._plan()
        frappe = self._bench(plan, docstatus=1)

        text = applier.report(applier.apply_plan(frappe, plan))

        self.assertIn('now_submitted', text)
        self.assertIn('ACC-JV-2026-02312', text)

    def test_submitted_entries_are_surfaced_with_their_proposed_party(self):
        rule = self._rule()
        self._posted(rule, docstatus=1)
        plan = planner.build_plan(self.erp)
        frappe = self._bench(plan)

        text = applier.report(applier.apply_plan(frappe, plan))

        self.assertIn('SUBMITTED ENTRIES', text)
        self.assertIn('allow_on_submit', text)
        self.assertIn('Supplier: Sorren', text)

    def test_a_plan_of_the_wrong_kind_is_refused(self):
        """The cost-center plan and the party plan have different shapes; running
        one through the other's applier would be a silent no-op at best."""
        import json
        import tempfile as tf
        with tf.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
            json.dump({'plan_kind': 'je_cost_center', 'changes': []}, fh)
            path = fh.name
        try:
            rc = applier.main([COMPANY, path])
        finally:
            os.unlink(path)
        self.assertEqual(rc, 2)


class EligibilityStaysInSync(unittest.TestCase):
    def test_the_applier_restates_the_same_eligible_account_types(self):
        """The applier runs INSIDE the ERPNext container and cannot import Bank
        Bridge, so it restates the rule. This test is the seam that keeps the two
        copies honest."""
        from app import categorization
        self.assertEqual(
            set(applier.PARTY_ELIGIBLE_ACCOUNT_TYPES),
            set(categorization.PARTY_ELIGIBLE_ACCOUNT_TYPES))


if __name__ == '__main__':
    unittest.main()
