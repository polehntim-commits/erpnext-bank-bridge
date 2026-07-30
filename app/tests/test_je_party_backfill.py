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
# The shape behind Wave 3's Feature B: two live rules offset a Fixed Asset
# account, which ERPNext refuses a Party on and which must NOT be untyped to make
# the backfill pass — depreciation reads account_type.
FIXED_ASSET = '1810 - Office Equipment - OML'
OTHER_COMPANY = 'Second Books, LLC'

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
    {'name': FIXED_ASSET, 'account_name': 'Office Equipment',
     'company': COMPANY, 'root_type': 'Asset',
     'account_type': 'Fixed Asset'},
]

# Every account_type the backfill must refuse, and why it is in the list.
REFUSED_ACCOUNT_TYPES = (
    'Expense Account',      # the reported Sorren case, explicitly typed
    'Indirect Expense',     # the other typed-expense shape in the live chart
    'Fixed Asset',          # Feature B — a mis-categorized rule, not a bad type
)


class BenchFrappe:
    """Stand-in for the `frappe` module, covering the surface the applier uses:
    db.get_value on a child row and on the parent's docstatus/company,
    db.set_value in its DICT form, db.exists for the party, get_all for the GL
    Entry lookup, db.commit/db.rollback, and get_cached_value for the
    account_type re-check.

    Rows live in `tables` as {doctype: {docname: {...}}}, so an assertion reads
    the same table the script wrote — which is the point for a repair whose whole
    correctness claim is "BOTH tables moved"."""

    def __init__(self, tables=None, accounts=None, parties=None):
        self.tables = {dt: {n: dict(r) for n, r in rows.items()}
                       for dt, rows in (tables or {}).items()}
        self.accounts = dict(accounts or {})
        self.parties = {k: set(v) for k, v in (parties or {}).items()}
        self.writes = []
        self.committed = False
        self.rolled_back = False
        self.raise_on = set()
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
        if (doctype, name) in self.raise_on:
            raise RuntimeError(f'simulated write failure on {doctype} {name}')
        target = self.tables.setdefault(doctype, {}).setdefault(name, {})
        updates = field if isinstance(field, dict) else {field: value}
        target.update(updates)
        self.writes.append((doctype, name, dict(updates)))

    def exists(self, doctype, name):
        return name in self.parties.get(doctype, set())

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    # ── frappe module ───────────────────────────────────────────────────
    def get_all(self, doctype, filters=None, fields=None):
        """Equality-only filtering, which is all the applier's GL lookup uses.
        `name` is always present in the result whether or not it was asked for,
        as it is from a real `frappe.get_all`."""
        out = []
        for name, row in self.tables.get(doctype, {}).items():
            if any(row.get(k) != v for k, v in (filters or {}).items()):
                continue
            rec = {f: row.get(f) for f in (fields or [])}
            rec['name'] = name
            out.append(rec)
        return out

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

    def _bench(self, plan, *, docstatus=0, party_types=('Supplier',),
               submitted_docstatus=1, gl=True, voucher_detail_no=True,
               company=COMPANY):
        """A BenchFrappe seeded from a plan, so the applier tests start from
        exactly the state the planner emitted.

        The submitted population also gets the GL Entry rows ERPNext would have
        posted, because "both tables moved" is the property under test. `gl=False`
        is the ledger that cannot be found; `voucher_detail_no=False` is the old
        ledger that lost the stamp and has to be matched by account."""
        je_rows, je_parents, gl_rows = {}, {}, {}

        def _seed(change, parent_docstatus, with_gl):
            je_rows[change['row']] = {
                'party_type': change['old_party_type'] or None,
                'party': change['old_party'] or None,
                'parent': change['journal_entry'],
                'account': change['account'],
            }
            je_parents[change['journal_entry']] = {
                'docstatus': parent_docstatus, 'company': company}
            if not with_gl:
                return
            gl_rows[f"gl-{change['row']}"] = {
                'voucher_type': 'Journal Entry',
                'voucher_no': change['journal_entry'],
                'voucher_detail_no': (change['row'] if voucher_detail_no
                                      else None),
                'is_cancelled': 0,
                'party_type': change['old_party_type'] or None,
                'party': change['old_party'] or None,
                'account': change['account'],
                'company': company,
            }

        for change in plan['changes']:
            _seed(change, docstatus, with_gl=False)
        for change in plan.get('submitted_changes') or []:
            _seed(change, submitted_docstatus, with_gl=gl)

        return BenchFrappe(
            tables={'Journal Entry Account': je_rows,
                    'Journal Entry': je_parents,
                    'GL Entry': gl_rows},
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

    def test_a_submitted_entry_is_planned_separately_not_as_review(self):
        """v0.9.1 · a submitted entry IS repairable — `party` is a column on GL
        Entry rows that already exist — so it belongs in its own list, not in
        `review`. Separate rather than merged because writing it rewrites the
        posted ledger, which is a bigger decision than repairing a draft and
        gets its own flag on the applier."""
        rule = self._rule()
        self._posted(rule, docstatus=1)

        plan = planner.build_plan(self.erp)

        self.assertEqual(plan['changes'], [])
        self.assertEqual(plan['review'], [])
        self.assertEqual(len(plan['submitted_changes']), 1)
        row = plan['submitted_changes'][0]
        self.assertEqual(row['journal_entry'], 'ACC-JV-2026-02312')
        self.assertEqual(row['row'], 'ACC-JV-2026-02312-r1')
        self.assertEqual(row['new_party_type'], 'Supplier')
        self.assertEqual(row['new_party'], 'Sorren')
        self.assertEqual(row['docstatus'], 1)
        self.assertEqual(row['company'], COMPANY)

    def test_the_plan_version_is_bumped_so_the_applier_can_tell(self):
        """The applier reads v1 plans too, and needs to know which shape it
        has."""
        self._posted(self._rule())

        self.assertEqual(planner.build_plan(self.erp)['plan_version'], 2)

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
        """THE GUARD THAT MATTERS, and it survives v0.9.1. The plan file says
        this line is a draft; the ledger says it is posted. Writing the posted
        ledger off a row the audit artifact labels a draft would make that
        artifact lie about what was done, so it re-plans instead — which is
        cheap. Refused even with --include-submitted, for the same reason."""
        plan = self._plan()
        frappe = self._bench(plan, docstatus=1)

        result = applier.apply_plan(frappe, plan, commit=True,
                                    include_submitted=True)

        self.assertEqual(result['counts']['updated'], 0)
        self.assertEqual(result['counts']['now_submitted'], 1)
        self.assertEqual(frappe.writes, [])
        self.assertIn('Re-plan', result['results'][0]['detail'])

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
        """Without the flag they are still REPORTED, with the party the rules
        imply and the flag that would apply them — a skip an operator can act
        on."""
        rule = self._rule()
        self._posted(rule, docstatus=1)
        plan = planner.build_plan(self.erp)
        frappe = self._bench(plan)

        text = applier.report(applier.apply_plan(frappe, plan))

        self.assertIn('submitted, not included', text)
        self.assertIn('--include-submitted', text)
        self.assertIn('Supplier: Sorren', text)

    def test_the_report_names_every_write_with_its_rule(self):
        """The audit line Tim asked for: (JE docname, offset account, party,
        rule)."""
        rule = self._rule()
        self._posted(rule, docstatus=1)
        plan = planner.build_plan(self.erp)
        frappe = self._bench(plan)

        text = applier.report(applier.apply_plan(
            frappe, plan, commit=True, include_submitted=True))

        self.assertIn('ACC-JV-2026-02312', text)
        self.assertIn(EXPENSE_UNTYPED, text)
        self.assertIn('Supplier: Sorren', text)
        self.assertIn(f'rule {rule.id}', text)
        self.assertIn('GL row', text)

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


class SubmittedBase(Base):
    """The v0.9.1 population: entries that are already posted, so the repair has
    to move the GL too."""

    JE = 'ACC-JV-2026-02312'
    ROW = 'ACC-JV-2026-02312-r1'
    GL = 'gl-ACC-JV-2026-02312-r1'

    def _plan(self, **kw):
        rule = self._rule(**kw)
        self._posted(rule, docstatus=1)
        plan = planner.build_plan(self.erp)
        self.assertEqual(len(plan['submitted_changes']), 1,
                         'fixture did not produce a submitted change')
        return plan

    def _apply(self, plan, frappe, **kw):
        kw.setdefault('commit', True)
        kw.setdefault('include_submitted', True)
        return applier.apply_plan(frappe, plan, **kw)

    def _je_row(self, frappe):
        return frappe.tables['Journal Entry Account'][self.ROW]

    def _gl_row(self, frappe):
        return frappe.tables['GL Entry'][self.GL]


class SubmittedEntriesAreGated(SubmittedBase):
    def test_the_flag_is_off_by_default(self):
        """FAIL SAFE · repairing a draft touches an unposted document; repairing
        a submitted entry rewrites the posted ledger. Different-sized decisions
        get different-sized gates, so --commit alone is not enough."""
        plan = self._plan()
        frappe = self._bench(plan)

        result = applier.apply_plan(frappe, plan, commit=True)

        self.assertEqual(result['counts']['updated'], 0)
        self.assertEqual(result['counts']['submitted_not_included'], 1)
        self.assertEqual(frappe.writes, [])
        self.assertIn('--include-submitted', result['results'][0]['detail'])

    def test_the_flag_without_commit_still_writes_nothing(self):
        """Both gates are independent — the second does not imply the first."""
        plan = self._plan()
        frappe = self._bench(plan)

        result = self._apply(plan, frappe, commit=False)

        self.assertEqual(result['counts']['updated'], 1)
        self.assertFalse(result['committed'])
        self.assertEqual(frappe.writes, [])
        self.assertIsNone(self._je_row(frappe)['party'])
        self.assertIsNone(self._gl_row(frappe)['party'])


class SubmittedEntriesWriteBothTables(SubmittedBase):
    def test_both_tables_are_written(self):
        """THE POINT OF WAVE 3. `tabJournal Entry Account` is what the form
        shows; `tabGL Entry` is what every supplier-wise report reads. A repair
        that moved only the first would make the form look fixed while Accounts
        Payable kept the empty answer."""
        plan = self._plan()
        frappe = self._bench(plan)

        result = self._apply(plan, frappe)

        self.assertEqual(result['counts']['updated'], 1)
        je = self._je_row(frappe)
        self.assertEqual(je['party_type'], 'Supplier')
        self.assertEqual(je['party'], 'Sorren')
        gl = self._gl_row(frappe)
        self.assertEqual(gl['party_type'], 'Supplier')
        self.assertEqual(gl['party'], 'Sorren')
        self.assertEqual({w[0] for w in frappe.writes},
                         {'Journal Entry Account', 'GL Entry'})

    def test_the_gl_row_is_found_by_voucher_detail_no(self):
        """`voucher_detail_no` IS the child row's docname, which is what makes a
        submitted repair addressable at all — and it identifies the line exactly
        even when one entry books the same account twice."""
        plan = self._plan()
        frappe = self._bench(plan)
        # A second line on the SAME account, whose GL row must not be touched.
        frappe.tables['GL Entry']['gl-other'] = {
            'voucher_type': 'Journal Entry', 'voucher_no': self.JE,
            'voucher_detail_no': f'{self.JE}-r3', 'is_cancelled': 0,
            'party_type': None, 'party': None, 'account': EXPENSE_UNTYPED,
            'company': COMPANY}

        self._apply(plan, frappe)

        self.assertEqual(self._gl_row(frappe)['party'], 'Sorren')
        self.assertIsNone(frappe.tables['GL Entry']['gl-other']['party'])

    def test_a_cancelled_gl_row_is_left_alone(self):
        """`is_cancelled=1` rows are history under a re-posted voucher."""
        plan = self._plan()
        frappe = self._bench(plan)
        frappe.tables['GL Entry'][self.GL]['is_cancelled'] = 1

        result = self._apply(plan, frappe)

        self.assertEqual(result['counts']['gl_not_found'], 1)
        self.assertEqual(frappe.writes, [])

    def test_it_is_idempotent(self):
        """Which is also what makes an interrupted first run safe to re-run."""
        plan = self._plan()
        frappe = self._bench(plan)
        self._apply(plan, frappe)

        second = self._apply(plan, frappe)

        self.assertEqual(second['counts']['updated'], 0)
        self.assertEqual(second['counts']['already_correct'], 1)

    def test_a_half_repair_is_completed_not_called_already_correct(self):
        """The behaviour that deliberately differs from the cost-center script.
        A JE row someone fixed by hand leaves the ledger — and therefore every
        report — still empty. Reading `already_correct` off the child row alone
        would walk straight past the exact state this backfill exists to end."""
        plan = self._plan()
        frappe = self._bench(plan)
        self._je_row(frappe).update({'party_type': 'Supplier',
                                     'party': 'Sorren'})

        result = self._apply(plan, frappe)

        self.assertEqual(result['counts']['updated'], 1)
        self.assertEqual(self._gl_row(frappe)['party'], 'Sorren')
        # Only the ledger needed moving, so only the ledger was written.
        self.assertEqual({w[0] for w in frappe.writes}, {'GL Entry'})

    def test_the_fallback_matches_by_account_when_the_stamp_is_missing(self):
        """Ledgers old enough (or repaired by hand) can have lost
        `voucher_detail_no`."""
        plan = self._plan()
        frappe = self._bench(plan, voucher_detail_no=False)

        result = self._apply(plan, frappe)

        self.assertEqual(result['counts']['updated'], 1)
        self.assertEqual(self._gl_row(frappe)['party'], 'Sorren')

    def test_the_fallback_survives_a_null_party_column(self):
        """The party match is done in Python, not in the SQL filter, because an
        empty party is NULL in some rows and '' in others and `party = ''`
        silently misses the NULLs — which are most of them, that being the
        bug."""
        plan = self._plan()
        frappe = self._bench(plan, voucher_detail_no=False)
        self._gl_row(frappe).update({'party_type': None, 'party': None})

        self.assertEqual(self._apply(plan, frappe)['counts']['updated'], 1)


class SubmittedEntriesRefuseUnsafeWrites(SubmittedBase):
    def test_every_ineligible_account_type_is_refused(self):
        """ERPNext throws on a Party on these at submit
        (validate_account_party_type), and on a POSTED entry the write would put
        the ledger in a state ERPNext itself would not have produced."""
        # One plan, one bench per account_type — which is also the real sequence:
        # the plan is built once and the account is reclassified afterwards.
        plan = self._plan()
        for account_type in REFUSED_ACCOUNT_TYPES:
            with self.subTest(account_type=account_type):
                frappe = self._bench(plan)
                frappe.accounts[EXPENSE_UNTYPED]['account_type'] = account_type

                result = self._apply(plan, frappe)

                self.assertEqual(result['counts']['ineligible_account'], 1)
                self.assertEqual(frappe.writes, [])
                self.assertIsNone(self._gl_row(frappe)['party'])

    def test_the_fixed_asset_refusal_names_the_reroute(self):
        """FEATURE B · a Fixed Asset offset is not a bad account_type, it is a
        MIS-CATEGORIZED RULE — the rule is booking an expense to the balance
        sheet. The fix is to re-point the rule; clearing account_type would break
        ERPNext's depreciation workflow, which reads it. The script refuses and
        says so; it does not auto-fix, because which expense account is right is
        an operator's call."""
        plan = self._plan()
        frappe = self._bench(plan)
        frappe.accounts[EXPENSE_UNTYPED]['account_type'] = 'Fixed Asset'

        result = self._apply(plan, frappe)

        detail = result['results'][0]['detail']
        self.assertIn('mis-categorized', detail)
        self.assertIn('offset_account', detail)
        self.assertIn('Repairs & Maintenance', detail)
        self.assertIn('Occupancy & Utilities', detail)
        self.assertIn('Do NOT clear account_type', detail)
        self.assertIn('depreciation', detail)

    def test_a_party_missing_from_erpnext_is_refused(self):
        """On a posted entry a dangling party is worse than none: every report
        that joins it shows a broken link with no document to open."""
        plan = self._plan()
        frappe = self._bench(plan)
        frappe.parties['Supplier'] = set()

        result = self._apply(plan, frappe)

        self.assertEqual(result['counts']['party_missing'], 1)
        self.assertEqual(frappe.writes, [])

    def test_a_cancelled_entry_is_refused_whole(self):
        """docstatus 2 is not touched even at the DB level. A cancelled voucher
        is history; naming a party on it changes what a prior-period report says
        about a transaction that was undone."""
        plan = self._plan()
        frappe = self._bench(plan, submitted_docstatus=2)

        result = self._apply(plan, frappe)

        self.assertEqual(result['counts']['cancelled'], 1)
        self.assertEqual(result['counts']['updated'], 0)
        self.assertEqual(frappe.writes, [])
        self.assertIn('history', result['results'][0]['detail'])

    def test_a_je_row_edited_since_the_plan_is_refused_not_clobbered(self):
        plan = self._plan()
        frappe = self._bench(plan)
        self._je_row(frappe).update({'party_type': 'Customer',
                                     'party': 'Someone Else'})

        result = self._apply(plan, frappe)

        self.assertEqual(result['counts']['stale'], 1)
        self.assertEqual(frappe.writes, [])
        self.assertEqual(self._je_row(frappe)['party'], 'Someone Else')

    def test_a_gl_row_edited_since_the_plan_is_refused(self):
        """DRIFT DETECTION reaches the ledger too, not just the form. A GL row
        an accountant attributed by hand is a decision, and a plan built before
        it does not get to overwrite it."""
        plan = self._plan()
        frappe = self._bench(plan)
        self._gl_row(frappe).update({'party_type': 'Customer',
                                     'party': 'Someone Else'})

        result = self._apply(plan, frappe)

        self.assertEqual(result['counts']['stale'], 1)
        self.assertEqual(frappe.writes, [])
        self.assertEqual(self._gl_row(frappe)['party'], 'Someone Else')
        self.assertIn('GL Entry', result['results'][0]['detail'])

    def test_a_line_with_no_gl_row_is_refused_not_half_written(self):
        """THE ONE HALF-REPAIR THIS SCRIPT REFUSES TO MAKE. Fixing the form and
        leaving the ledger empty is strictly worse than the bug, because it hides
        itself behind a form that looks right."""
        plan = self._plan()
        frappe = self._bench(plan, gl=False)

        result = self._apply(plan, frappe)

        self.assertEqual(result['counts']['gl_not_found'], 1)
        self.assertEqual(frappe.writes, [])
        self.assertIsNone(self._je_row(frappe)['party'])
        self.assertIn('cancel + amend', result['results'][0]['detail'])

    def test_an_ambiguous_fallback_is_refused(self):
        """A party is an identity, not a grouping — hanging one on the wrong leg
        of a posted entry is worse than leaving it blank. So where the
        cost-center script would take a fallback match, this one refuses unless
        the match is unique."""
        plan = self._plan()
        frappe = self._bench(plan, voucher_detail_no=False)
        frappe.tables['GL Entry']['gl-twin'] = {
            **self._gl_row(frappe), 'voucher_detail_no': None}

        result = self._apply(plan, frappe)

        self.assertEqual(result['counts']['gl_ambiguous'], 1)
        self.assertEqual(frappe.writes, [])
        self.assertIn('cancel + amend', result['results'][0]['detail'])

    def test_a_failed_write_aborts_and_rolls_back(self):
        """Neither table may move without the other, so a write that raises
        part-way stops the walk instead of leaving a repaired form over an empty
        ledger. main() rolls back on `aborted`."""
        plan = self._plan()
        frappe = self._bench(plan)
        frappe.raise_on = {('GL Entry', self.GL)}

        result = self._apply(plan, frappe)

        self.assertTrue(result['aborted'])
        self.assertFalse(result['committed'])
        self.assertEqual(result['counts']['write_failed'], 1)
        self.assertIn('ABORTED', applier.report(result))

    def test_a_commit_that_wrote_nothing_does_not_read_as_success(self):
        """FAIL FORWARD · "COMMITTED" over a report of nothing but refusals reads
        as a repair that happened. It didn't."""
        plan = self._plan()
        frappe = self._bench(plan, gl=False)

        text = applier.report(self._apply(plan, frappe))

        self.assertIn('NOTHING WAS WRITTEN', text)
        self.assertIn('gl_not_found', text)

    def test_an_entry_that_became_a_draft_is_stale_not_written(self):
        """An amend replaces the docname, so a plan row that said "submitted"
        pointing at a draft is not the document the plan measured."""
        plan = self._plan()
        frappe = self._bench(plan, submitted_docstatus=0)

        result = self._apply(plan, frappe)

        self.assertEqual(result['counts']['stale'], 1)
        self.assertEqual(frappe.writes, [])


class SubmittedEntriesHonourCompanyScoping(SubmittedBase):
    def test_another_companys_entry_is_skipped(self):
        plan = self._plan()
        frappe = self._bench(plan, company=OTHER_COMPANY)

        result = self._apply(plan, frappe, company=COMPANY)

        self.assertEqual(result['counts']['wrong_company'], 1)
        self.assertEqual(frappe.writes, [])

    def test_the_scoped_company_is_written(self):
        plan = self._plan()
        frappe = self._bench(plan, company=COMPANY)

        result = self._apply(plan, frappe, company=COMPANY)

        self.assertEqual(result['counts']['updated'], 1)

    def test_the_company_is_read_from_the_ledger_not_the_plan(self):
        """Same reason as docstatus: the plan is an assertion, the DB is the
        fact. A plan built before an entry was moved between companies must not
        authorize a write into the wrong books."""
        plan = self._plan()
        self.assertEqual(plan['submitted_changes'][0]['company'], COMPANY)
        frappe = self._bench(plan, company=OTHER_COMPANY)

        result = self._apply(plan, frappe, company=COMPANY)

        self.assertEqual(result['counts']['wrong_company'], 1)
        self.assertEqual(result['results'][0]['company'], OTHER_COMPANY)


class LegacyPlansStillApply(SubmittedBase):
    def test_a_plan_version_1_plan_still_applies(self):
        """v0.9.0 recorded submitted entries as `review` rows under
        `submitted_not_writable` with the same fields merged in. An operator with
        yesterday's plan file gets the repair, not a silent no-op."""
        plan = self._plan()
        frappe = self._bench(plan)
        legacy = {
            'plan_version': 1, 'plan_kind': 'je_party',
            'changes': [],
            'review': [{**c, 'reason': 'submitted_not_writable'}
                       for c in plan['submitted_changes']],
        }

        result = self._apply(legacy, frappe)

        self.assertEqual(result['counts']['updated'], 1)
        self.assertEqual(self._gl_row(frappe)['party'], 'Sorren')

    def test_the_legacy_review_reason_is_still_documented(self):
        """It can still turn up in an old plan file, so it still needs an
        explanation — and the explanation now names the way forward."""
        text = planner.REVIEW_REASONS['submitted_not_writable']
        self.assertIn('--include-submitted', text)

    def test_a_legacy_review_row_is_not_double_counted_as_review(self):
        """The applier consumes those rows as work; the report must not also list
        them as unrepairable."""
        plan = self._plan()
        frappe = self._bench(plan)
        legacy = {
            'plan_version': 1, 'plan_kind': 'je_party', 'changes': [],
            'review': [{**c, 'reason': 'submitted_not_writable'}
                       for c in plan['submitted_changes']],
        }

        text = applier.report(self._apply(legacy, frappe))

        self.assertNotIn('needing review, never written', text)


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
