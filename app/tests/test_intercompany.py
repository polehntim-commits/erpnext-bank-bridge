# SPDX-License-Identifier: MIT
"""Intercompany transfer detection + paired Journal Entries (v0.4.1).

A transfer between two ERPNext Companies the operator owns arrives as two Plaid
transactions — equal magnitude, opposite sign, one per Company. Booking them
separately puts an expense on one entity's P&L and income on the other's for
money that never left the operator's hands. These tests cover the three things
that have to hold for that not to happen:

  * DETECTION — amount/sign, date tolerance, description similarity, and the
    different-Companies requirement, plus the confidence score they produce
  * DIVERSION — a paired transaction is invisible to rules carrying
    `ignore_for_paired` (the default), so the generic Transfer rule can't fire
  * BOOKING — the Due from / Due to entry pair balances, is created atomically
    across two Companies, and is fully reversible via Unpair

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date, timedelta

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import categorization, erpnext_accounts, erpnext_settings  # noqa: E402
from app import intercompany, sync_engine  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        GeneratedJournalEntry, IntercompanyTransferPair,
                        PlaidAccount, PlaidItem)

from tests.fakes import FakeERPClient  # noqa: E402

FARM = 'Farm LLC'
PERSONAL = 'Personal LLC'
FARM_ACC = 'acct-farm-checking'
PERSONAL_ACC = 'acct-personal-checking'
FARM_GL = 'Farm Checking - FL'
PERSONAL_GL = 'Personal Checking - PL'

# A Chart of Accounts with both Companies' anchors, so the intercompany
# provisioning has somewhere to create the Due from / Due to leaves.
CHART = [
    {'account_name': 'Current Assets', 'company': FARM, 'is_group': 1,
     'root_type': 'Asset', 'name': 'Current Assets - FL'},
    {'account_name': 'Current Liabilities', 'company': FARM, 'is_group': 1,
     'root_type': 'Liability', 'name': 'Current Liabilities - FL'},
    {'account_name': 'Farm Checking', 'company': FARM, 'is_group': 0,
     'root_type': 'Asset', 'name': FARM_GL},
    {'account_name': 'Current Assets', 'company': PERSONAL, 'is_group': 1,
     'root_type': 'Asset', 'name': 'Current Assets - PL'},
    {'account_name': 'Current Liabilities', 'company': PERSONAL, 'is_group': 1,
     'root_type': 'Liability', 'name': 'Current Liabilities - PL'},
    {'account_name': 'Personal Checking', 'company': PERSONAL, 'is_group': 0,
     'root_type': 'Asset', 'name': PERSONAL_GL},
]


class IntercompanyBase(unittest.TestCase):
    EXTRA_CONFIG: dict = {}

    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        cfg = {'TESTING': True,
               'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
               'DATA_DIR': self._datadir, 'FERNET_KEY': '',
               'SCHEDULER_ENABLED': False}
        cfg.update(self.EXTRA_CONFIG)
        self.app = create_app(cfg)
        self.ctx = self.app.app_context()
        self.ctx.push()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', FARM)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    # ── fixtures ────────────────────────────────────────────────────────

    def _two_companies(self):
        """One linked, mapped, GL-backed account under each Company — the
        minimum shape in which an intercompany transfer can exist at all."""
        for item_id, company in (('item-farm', FARM), ('item-personal', PERSONAL)):
            db.session.add(PlaidItem(
                item_id=item_id, access_token_encrypted=crypto.encrypt('t'),
                institution_id='ins_1', institution_name='Wells Fargo',
                status='active', owning_company=company))
        db.session.add(PlaidAccount(
            account_id=FARM_ACC, item_id='item-farm', name='Farm Checking',
            type='depository', subtype='checking',
            erpnext_bank_account_name='Farm Checking - WF',
            erpnext_gl_account_name=FARM_GL, sync_enabled=True))
        db.session.add(PlaidAccount(
            account_id=PERSONAL_ACC, item_id='item-personal',
            name='Personal Checking', type='depository', subtype='checking',
            erpnext_bank_account_name='Personal Checking - WF',
            erpnext_gl_account_name=PERSONAL_GL, sync_enabled=True))
        db.session.commit()

    def _one_company(self):
        """Both accounts under a SINGLE Company — the regression shape, where
        detection must stay completely inert."""
        db.session.add(PlaidItem(
            item_id='item-farm', access_token_encrypted=crypto.encrypt('t'),
            institution_id='ins_1', status='active', owning_company=FARM))
        for acc, gl in ((FARM_ACC, FARM_GL), (PERSONAL_ACC, PERSONAL_GL)):
            db.session.add(PlaidAccount(
                account_id=acc, item_id='item-farm', type='depository',
                subtype='checking', erpnext_bank_account_name=f'{acc} - WF',
                erpnext_gl_account_name=gl, sync_enabled=True))
        db.session.commit()

    def _txn(self, tid, account_id, amount, name='Transfer',
             when=date(2026, 7, 10), bank_txn='ACC-BTN-0001'):
        row = BankTransaction(
            plaid_transaction_id=tid, account_id=account_id, amount=amount,
            date=when, name=name, merchant_name='', removed=False,
            erpnext_bank_transaction_id=bank_txn)
        db.session.add(row)
        db.session.commit()
        return row

    def _transfer(self, amount=10000.0, out_when=date(2026, 7, 10),
                  in_when=date(2026, 7, 10),
                  out_name='Transfer to Personal',
                  in_name='Transfer from Farm'):
        """The canonical pair: Farm pays out, Personal receives. Plaid's
        convention is positive = money OUT of the account."""
        out = self._txn('t-out', FARM_ACC, amount, out_name, out_when)
        inn = self._txn('t-in', PERSONAL_ACC, -amount, in_name, in_when)
        return out, inn

    def _client(self, **kwargs):
        kwargs.setdefault('chart_accounts', CHART)
        kwargs.setdefault('companies', [FARM, PERSONAL])
        return FakeERPClient(**kwargs)


# ── description similarity + scoring ────────────────────────────────────

class TestSimilarity(IntercompanyBase):
    def test_normalization_folds_case_and_punctuation(self):
        self.assertEqual(
            intercompany.normalize_description('ONLINE TRANSFER #1234!'),
            'online transfer 1234')

    def test_identical_descriptions_score_one(self):
        self.assertEqual(
            intercompany.description_similarity('Wire transfer',
                                                'wire  TRANSFER'), 1.0)

    def test_canonical_transfer_pair_clears_the_threshold(self):
        # The shape the whole feature is designed around must pass the 0.6 gate.
        sim = intercompany.description_similarity('Transfer to Personal',
                                                  'Transfer from Farm')
        self.assertGreaterEqual(sim, intercompany.description_threshold())

    def test_unrelated_merchants_score_low(self):
        self.assertLess(
            intercompany.description_similarity('CHEVRON 0123',
                                                'WHOLE FOODS MKT'), 0.4)

    def test_blank_descriptions_score_zero_not_one(self):
        # Absence of evidence must not read as perfect evidence, or amount alone
        # would pair unrelated transactions.
        self.assertEqual(intercompany.description_similarity('', ''), 0.0)
        self.assertEqual(intercompany.description_similarity('Transfer', ''), 0.0)


class TestScoring(IntercompanyBase):
    D = date(2026, 7, 10)

    def _score(self, a=100.0, b=-100.0, da=None, db_=None,
               sa='Transfer to Personal', sb='Transfer from Farm'):
        return intercompany.score_pair(a, b, da or self.D, db_ or self.D, sa, sb)

    def test_same_day_identical_description_scores_one(self):
        self.assertEqual(self._score(sa='Wire transfer', sb='Wire transfer'), 1.0)

    def test_canonical_pair_clears_the_auto_threshold(self):
        self.assertGreaterEqual(self._score(),
                                intercompany.confidence_threshold())

    def test_confidence_decays_with_date_distance(self):
        same = self._score()
        one = self._score(db_=self.D + timedelta(days=1))
        three = self._score(db_=self.D + timedelta(days=3))
        self.assertGreater(same, one)
        self.assertGreater(one, three)

    def test_weak_evidence_falls_below_the_auto_threshold(self):
        # Three days apart AND a middling description: detected as a candidate,
        # but deliberately not confident enough to book without a human.
        score = self._score(db_=self.D + timedelta(days=3))
        self.assertGreater(score, 0.0)
        self.assertLess(score, intercompany.confidence_threshold())

    def test_same_sign_scores_zero(self):
        # Two outflows are not two legs of one movement.
        self.assertEqual(self._score(a=100.0, b=100.0), 0.0)

    def test_mismatched_amount_scores_zero(self):
        self.assertEqual(self._score(a=100.0, b=-100.01), 0.0)

    def test_zero_amount_scores_zero(self):
        self.assertEqual(self._score(a=0.0, b=0.0), 0.0)

    def test_outside_the_date_window_scores_zero(self):
        self.assertEqual(self._score(db_=self.D + timedelta(days=4)), 0.0)

    def test_low_description_similarity_scores_zero(self):
        self.assertEqual(
            self._score(sa='CHEVRON 0123', sb='WHOLE FOODS MKT'), 0.0)

    def test_missing_date_scores_zero(self):
        self.assertEqual(intercompany.score_pair(
            100.0, -100.0, None, self.D, 'Transfer', 'Transfer'), 0.0)

    def test_score_is_bounded(self):
        self.assertLessEqual(self._score(sa='X transfer', sb='X transfer'), 1.0)


# ── detection ───────────────────────────────────────────────────────────

class TestDetection(IntercompanyBase):
    def test_detects_the_canonical_transfer(self):
        self._two_companies()
        self._transfer()
        pairs = intercompany.detect_pairs()
        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        # Direction follows the MONEY, not discovery order.
        self.assertEqual(pair.from_transaction_id, 't-out')
        self.assertEqual(pair.from_company, FARM)
        self.assertEqual(pair.to_transaction_id, 't-in')
        self.assertEqual(pair.to_company, PERSONAL)
        self.assertEqual(pair.amount, 10000.0)
        self.assertEqual(pair.state, 'pending')

    def test_both_transactions_are_stamped_with_the_pair(self):
        self._two_companies()
        self._transfer()
        pair = intercompany.detect_pairs()[0]
        for tid in ('t-out', 't-in'):
            row = BankTransaction.query.filter_by(
                plaid_transaction_id=tid).first()
            self.assertEqual(row.intercompany_pair_id, pair.id)

    def test_direction_is_normalized_regardless_of_insert_order(self):
        self._two_companies()
        # Insert the INFLOW first — orientation must still come out source-first.
        self._txn('t-in', PERSONAL_ACC, -500.0, 'Transfer from Farm')
        self._txn('t-out', FARM_ACC, 500.0, 'Transfer to Personal')
        pair = intercompany.detect_pairs()[0]
        self.assertEqual(pair.from_transaction_id, 't-out')
        self.assertEqual(pair.to_transaction_id, 't-in')

    def test_pairs_within_the_date_tolerance(self):
        self._two_companies()
        self._transfer(in_when=date(2026, 7, 11),
                       out_name='Wire transfer', in_name='Wire transfer')
        self.assertEqual(len(intercompany.detect_pairs()), 1)

    def test_does_not_pair_outside_the_date_tolerance(self):
        self._two_companies()
        self._transfer(in_when=date(2026, 7, 20))
        self.assertEqual(intercompany.detect_pairs(), [])

    def test_does_not_pair_mismatched_amounts(self):
        self._two_companies()
        self._txn('t-out', FARM_ACC, 10000.0, 'Transfer to Personal')
        self._txn('t-in', PERSONAL_ACC, -9999.0, 'Transfer from Farm')
        self.assertEqual(intercompany.detect_pairs(), [])

    def test_does_not_pair_dissimilar_descriptions(self):
        self._two_companies()
        self._transfer(out_name='CHEVRON 0123', in_name='WHOLE FOODS MKT')
        self.assertEqual(intercompany.detect_pairs(), [])

    def test_does_not_pair_within_a_single_company(self):
        # Two accounts of the SAME Company moving money is an ordinary internal
        # transfer — booking Due-from/Due-to against yourself is meaningless.
        self._one_company()
        self._transfer()
        self.assertEqual(intercompany.detect_pairs(), [])

    def test_does_not_pair_two_outflows(self):
        self._two_companies()
        self._txn('t-out', FARM_ACC, 100.0, 'Transfer')
        self._txn('t-out2', PERSONAL_ACC, 100.0, 'Transfer')
        self.assertEqual(intercompany.detect_pairs(), [])

    def test_ignores_removed_transactions(self):
        self._two_companies()
        out, inn = self._transfer()
        inn.removed = True
        db.session.commit()
        self.assertEqual(intercompany.detect_pairs(), [])

    def test_a_transaction_joins_at_most_one_pair(self):
        self._two_companies()
        self._transfer()
        # A second, identical inflow that could equally have matched.
        self._txn('t-in2', PERSONAL_ACC, -10000.0, 'Transfer from Farm')
        pairs = intercompany.detect_pairs()
        self.assertEqual(len(pairs), 1)
        claimed = {pairs[0].from_transaction_id, pairs[0].to_transaction_id}
        self.assertEqual(len(claimed), 2)

    def test_detection_is_idempotent(self):
        self._two_companies()
        self._transfer()
        self.assertEqual(len(intercompany.detect_pairs()), 1)
        self.assertEqual(intercompany.detect_pairs(), [])
        self.assertEqual(IntercompanyTransferPair.query.count(), 1)

    def test_find_transfer_pair_returns_counterparty_and_confidence(self):
        self._two_companies()
        out, inn = self._transfer()
        found = intercompany.find_transfer_pair(out)
        self.assertIsNotNone(found)
        counterparty, confidence = found
        self.assertEqual(counterparty.plaid_transaction_id, 't-in')
        self.assertGreaterEqual(confidence,
                                intercompany.confidence_threshold())

    def test_find_transfer_pair_returns_none_with_no_counterparty(self):
        self._two_companies()
        out = self._txn('t-out', FARM_ACC, 10000.0, 'Transfer to Personal')
        self.assertIsNone(intercompany.find_transfer_pair(out))

    def test_below_threshold_candidate_is_not_paired(self):
        self._two_companies()
        # Three days apart with a middling description → detected, not booked.
        self._transfer(in_when=date(2026, 7, 13))
        self.assertEqual(intercompany.detect_pairs(), [])
        out = BankTransaction.query.filter_by(plaid_transaction_id='t-out').first()
        found = intercompany.find_transfer_pair(out)
        self.assertIsNotNone(found)          # the CANDIDATE is still visible…
        self.assertLess(found[1], intercompany.confidence_threshold())  # …just weak

    def test_multi_company_accounts_gates_detection(self):
        self._one_company()
        self.assertFalse(intercompany.multi_company_accounts())
        db.session.query(PlaidAccount).filter_by(
            account_id=PERSONAL_ACC).update({'owning_company': PERSONAL})
        db.session.commit()
        self.assertTrue(intercompany.multi_company_accounts())


# ── rule diversion ──────────────────────────────────────────────────────

class TestRuleDiversion(IntercompanyBase):
    def _transfer_rule(self, ignore_for_paired=True):
        rule = CategorizationRule(
            name='Transfers', priority=10, active=True, archived=False,
            match_type='merchant_contains', match_value='',
            offset_account='Owner Draws', offset_direction='auto',
            ignore_for_paired=ignore_for_paired)
        # match_contains on '' matches nothing; use a description regex instead
        # so the rule genuinely fires on the transfer text.
        rule.match_type = 'description_regex'
        rule.match_value = 'Transfer'
        db.session.add(rule)
        db.session.commit()
        return rule

    def test_rule_fires_on_an_unpaired_transaction(self):
        self._two_companies()
        self._transfer_rule()
        out = self._txn('t-solo', FARM_ACC, 500.0, 'Transfer to somewhere')
        self.assertIsNotNone(categorization.find_matching_rule(out))

    def test_rule_does_not_fire_on_a_paired_transaction(self):
        self._two_companies()
        self._transfer_rule()
        self._transfer()
        intercompany.detect_pairs()
        out = BankTransaction.query.filter_by(plaid_transaction_id='t-out').first()
        self.assertIsNone(categorization.find_matching_rule(out))

    def test_a_rule_that_opts_out_still_fires_on_a_paired_transaction(self):
        self._two_companies()
        self._transfer_rule(ignore_for_paired=False)
        self._transfer()
        intercompany.detect_pairs()
        out = BankTransaction.query.filter_by(plaid_transaction_id='t-out').first()
        self.assertIsNotNone(categorization.find_matching_rule(out))

    def test_the_trace_records_the_skipped_rule(self):
        # The audit log must show WHY an otherwise-matching rule didn't win.
        self._two_companies()
        rule = self._transfer_rule()
        self._transfer()
        intercompany.detect_pairs()
        out = BankTransaction.query.filter_by(plaid_transaction_id='t-out').first()
        winner, trace = categorization.evaluate_rules(out)
        self.assertIsNone(winner)
        self.assertEqual([t['rule_id'] for t in trace], [rule.id])
        self.assertFalse(trace[0]['matched'])


# ── Journal Entry generation ────────────────────────────────────────────

class TestJournalEntries(IntercompanyBase):
    def _detected(self):
        self._two_companies()
        self._transfer()
        return intercompany.detect_pairs()[0]

    def test_books_a_balanced_entry_on_each_company(self):
        pair = self._detected()
        client = self._client()
        result = intercompany.generate_pair_journal_entries(client, pair)
        self.assertIsNotNone(result)
        docs = [c[2] for c in client.calls
                if c[0] == 'create_doc' and c[1] == 'Journal Entry']
        self.assertEqual(len(docs), 2)
        for doc in docs:
            debits = sum(ln.get('debit_in_account_currency', 0) for ln in doc['accounts'])
            credits = sum(ln.get('credit_in_account_currency', 0) for ln in doc['accounts'])
            self.assertEqual(debits, credits)
            self.assertEqual(debits, 10000.0)

    def test_source_debits_due_from_and_credits_the_bank(self):
        pair = self._detected()
        client = self._client()
        intercompany.generate_pair_journal_entries(client, pair)
        source = next(c[2] for c in client.calls
                      if c[0] == 'create_doc' and c[1] == 'Journal Entry'
                      and c[2]['company'] == FARM)
        debit = next(ln for ln in source['accounts']
                     if 'debit_in_account_currency' in ln)
        credit = next(ln for ln in source['accounts']
                      if 'credit_in_account_currency' in ln)
        self.assertIn('Due from', debit['account'])
        self.assertIn(PERSONAL, debit['account'])
        self.assertEqual(credit['account'], FARM_GL)

    def test_target_debits_the_bank_and_credits_due_to(self):
        pair = self._detected()
        client = self._client()
        intercompany.generate_pair_journal_entries(client, pair)
        target = next(c[2] for c in client.calls
                      if c[0] == 'create_doc' and c[1] == 'Journal Entry'
                      and c[2]['company'] == PERSONAL)
        debit = next(ln for ln in target['accounts']
                     if 'debit_in_account_currency' in ln)
        credit = next(ln for ln in target['accounts']
                      if 'credit_in_account_currency' in ln)
        self.assertEqual(debit['account'], PERSONAL_GL)
        self.assertIn('Due to', credit['account'])
        self.assertIn(FARM, credit['account'])

    def test_neither_entry_touches_profit_and_loss(self):
        """The entire point: only the two bank accounts and the two
        counterparty control accounts are referenced — no Income or Expense
        account on either side, so neither Company's P&L moves."""
        pair = self._detected()
        client = self._client()
        intercompany.generate_pair_journal_entries(client, pair)
        referenced = set()
        for c in client.calls:
            if c[0] == 'create_doc' and c[1] == 'Journal Entry':
                referenced.update(ln['account'] for ln in c[2]['accounts'])
        # Four distinct accounts, and every one is a bank leaf or a Due
        # from/Due to control account we just provisioned.
        self.assertEqual(len(referenced), 4)
        self.assertIn(FARM_GL, referenced)
        self.assertIn(PERSONAL_GL, referenced)
        control = {a for a in referenced if a not in (FARM_GL, PERSONAL_GL)}
        self.assertEqual(len(control), 2)
        for name in control:
            doc = client.created['Account'][name]
            self.assertTrue(doc['account_name'].startswith(('Due from', 'Due to')),
                            f'{name} is neither a bank nor a control account')
            # Anchored on the balance sheet — never under an Income/Expense root.
            parent = client.created['Account'].get(doc['parent_account']) \
                or client.chart_accounts[doc['parent_account']]
            root = parent.get('root_type') or client.chart_accounts[
                parent['parent_account']]['root_type']
            self.assertIn(root, ('Asset', 'Liability'))

    def test_the_pair_records_both_journal_entry_names(self):
        pair = self._detected()
        source, target = intercompany.generate_pair_journal_entries(
            self._client(), pair)
        self.assertEqual(pair.from_journal_entry, source)
        self.assertEqual(pair.to_journal_entry, target)
        self.assertNotEqual(source, target)

    def test_each_leg_gets_a_generated_journal_entry_row(self):
        pair = self._detected()
        intercompany.generate_pair_journal_entries(self._client(), pair)
        for tid in ('t-out', 't-in'):
            gje = GeneratedJournalEntry.query.filter_by(
                plaid_transaction_id=tid).first()
            self.assertIsNotNone(gje)
            self.assertEqual(gje.intercompany_pair_id, pair.id)
            self.assertEqual(gje.state, 'pending_review')

    def test_generation_is_idempotent(self):
        pair = self._detected()
        client = self._client()
        first = intercompany.generate_pair_journal_entries(client, pair)
        second = intercompany.generate_pair_journal_entries(client, pair)
        self.assertEqual(first, second)
        self.assertEqual(len([c for c in client.calls
                              if c[0] == 'create_doc'
                              and c[1] == 'Journal Entry']), 2)

    def test_a_partial_failure_rolls_back_the_first_entry(self):
        # The atomicity guarantee: if the second Company's entry fails, the
        # first Company's Draft is deleted, so neither book is half-updated.
        pair = self._detected()
        client = self._client(fail_je_create_after=1)
        self.assertIsNone(
            intercompany.generate_pair_journal_entries(client, pair))
        self.assertEqual(len(client.deleted), 1)
        self.assertEqual(client.created['Journal Entry'], {})
        self.assertIsNone(pair.from_journal_entry)
        self.assertIsNone(pair.to_journal_entry)
        self.assertEqual(pair.state, 'pending')
        self.assertIn(PERSONAL, pair.note)

    def test_a_first_leg_failure_books_nothing(self):
        pair = self._detected()
        client = self._client(fail_je_create=True)
        self.assertIsNone(
            intercompany.generate_pair_journal_entries(client, pair))
        self.assertEqual(client.created['Journal Entry'], {})
        self.assertIsNone(pair.from_journal_entry)

    def test_an_unmapped_bank_account_refuses_rather_than_half_books(self):
        pair = self._detected()
        PlaidAccount.query.filter_by(account_id=PERSONAL_ACC).update(
            {'erpnext_gl_account_name': None})
        db.session.commit()
        client = self._client()
        self.assertIsNone(
            intercompany.generate_pair_journal_entries(client, pair))
        self.assertEqual(client.created['Journal Entry'], {})
        self.assertIn('bank accounts', pair.note)


# ── intercompany account auto-creation ──────────────────────────────────

class TestAccountProvisioning(IntercompanyBase):
    def test_creates_both_sides_on_first_use(self):
        client = self._client()
        due_from, due_to = erpnext_accounts.ensure_intercompany_accounts(
            client, FARM, PERSONAL)
        self.assertIn(f'Due from {PERSONAL}', due_from)
        self.assertIn(f'Due to {FARM}', due_to)

    def test_due_from_lands_on_the_assets_side(self):
        client = self._client()
        erpnext_accounts.ensure_intercompany_account(
            client, FARM, PERSONAL, 'due_from')
        created = [d for d in client.created['Account'].values()
                   if d['account_name'].startswith('Due from')]
        self.assertEqual(len(created), 1)
        # Anchored under the Loans and Advances group, itself under Current Assets.
        parent = client.created['Account'][created[0]['parent_account']]
        self.assertEqual(parent['parent_account'], 'Current Assets - FL')

    def test_due_to_lands_under_current_liabilities(self):
        client = self._client()
        erpnext_accounts.ensure_intercompany_account(
            client, PERSONAL, FARM, 'due_to')
        created = [d for d in client.created['Account'].values()
                   if d['account_name'].startswith('Due to')]
        self.assertEqual(created[0]['parent_account'], 'Current Liabilities - PL')

    def test_no_party_account_type_is_set(self):
        # 'Receivable'/'Payable' would make ERPNext demand a Party on every line,
        # and the counterparty here is another Company you own, not a customer.
        client = self._client()
        erpnext_accounts.ensure_intercompany_accounts(client, FARM, PERSONAL)
        for doc in client.created['Account'].values():
            self.assertNotIn(doc.get('account_type'), ('Receivable', 'Payable'))

    def test_reuses_an_existing_account_instead_of_duplicating(self):
        client = self._client()
        first = erpnext_accounts.ensure_intercompany_account(
            client, FARM, PERSONAL, 'due_from')
        before = len(client.created['Account'])
        second = erpnext_accounts.ensure_intercompany_account(
            client, FARM, PERSONAL, 'due_from')
        self.assertEqual(first, second)
        self.assertEqual(len(client.created['Account']), before)

    def test_returns_none_without_a_chart_to_anchor_to(self):
        client = FakeERPClient(chart_accounts=[], companies=[FARM])
        self.assertIsNone(erpnext_accounts.ensure_intercompany_account(
            client, FARM, PERSONAL, 'due_from'))

    def test_rejects_an_unknown_side(self):
        self.assertIsNone(erpnext_accounts.ensure_intercompany_account(
            self._client(), FARM, PERSONAL, 'sideways'))


# ── approve / unpair ────────────────────────────────────────────────────

class TestReviewActions(IntercompanyBase):
    def _booked(self, client=None):
        self._two_companies()
        self._transfer()
        pair = intercompany.detect_pairs()[0]
        intercompany.generate_pair_journal_entries(client or self._client(), pair)
        return pair

    def test_approve_submits_both_entries(self):
        client = self._client()
        pair = self._booked(client)
        ok, _msg = intercompany.approve_pair(client, pair)
        db.session.commit()
        self.assertTrue(ok)
        self.assertEqual(pair.state, 'approved')
        self.assertEqual(client.submitted,
                         {pair.from_journal_entry, pair.to_journal_entry})

    def test_approve_mirrors_the_state_onto_both_legs(self):
        client = self._client()
        pair = self._booked(client)
        intercompany.approve_pair(client, pair)
        db.session.commit()
        states = {g.state for g in GeneratedJournalEntry.query.filter_by(
            intercompany_pair_id=pair.id).all()}
        self.assertEqual(states, {'approved'})

    def test_approve_is_idempotent(self):
        client = self._client()
        pair = self._booked(client)
        intercompany.approve_pair(client, pair)
        ok, msg = intercompany.approve_pair(client, pair)
        self.assertTrue(ok)
        self.assertIn('Already', msg)

    def test_approve_refuses_an_unbooked_pair(self):
        self._two_companies()
        self._transfer()
        pair = intercompany.detect_pairs()[0]
        ok, msg = intercompany.approve_pair(self._client(), pair)
        self.assertFalse(ok)
        self.assertIn('generated', msg)

    def test_unpair_returns_both_transactions_to_normal_rules(self):
        client = self._client()
        pair = self._booked(client)
        ok, _msg = intercompany.reject_pair(client, pair)
        db.session.commit()
        self.assertTrue(ok)
        self.assertEqual(pair.state, 'rejected')
        for tid in ('t-out', 't-in'):
            row = BankTransaction.query.filter_by(
                plaid_transaction_id=tid).first()
            self.assertIsNone(row.intercompany_pair_id)

    def test_unpair_cancels_submitted_entries(self):
        client = self._client()
        pair = self._booked(client)
        intercompany.approve_pair(client, pair)
        db.session.commit()
        intercompany.reject_pair(client, pair)
        db.session.commit()
        self.assertEqual(client.cancelled,
                         {pair.from_journal_entry, pair.to_journal_entry})

    def test_unpair_suppresses_re_detection(self):
        # Without the suppression record the detector would re-pair the same two
        # transactions on the very next sync, undoing the operator's decision.
        client = self._client()
        pair = self._booked(client)
        intercompany.reject_pair(client, pair)
        db.session.commit()
        self.assertEqual(intercompany.detect_pairs(), [])

    def test_unpair_is_idempotent(self):
        client = self._client()
        pair = self._booked(client)
        intercompany.reject_pair(client, pair)
        db.session.commit()
        ok, msg = intercompany.reject_pair(client, pair)
        self.assertTrue(ok)
        self.assertIn('Already', msg)

    def test_retry_books_a_pair_that_previously_failed(self):
        self._two_companies()
        self._transfer()
        pair = intercompany.detect_pairs()[0]
        # First attempt fails outright…
        intercompany.generate_pair_journal_entries(
            self._client(fail_je_create=True), pair)
        self.assertIsNone(pair.from_journal_entry)
        # …and a Retry against a healthy ERPNext succeeds.
        ok, _msg = intercompany.retry_pair(self._client(), pair)
        self.assertTrue(ok)
        self.assertIsNotNone(pair.from_journal_entry)
        self.assertIsNone(pair.note)


# ── supersede: a rules-engine entry already booked one leg ──────────────

class TestSupersede(IntercompanyBase):
    def _leg_with_rule_entry(self, state='pending_review'):
        self._two_companies()
        self._transfer()
        pair = intercompany.detect_pairs()[0]
        db.session.add(GeneratedJournalEntry(
            plaid_transaction_id='t-out', rule_id=1, rule_name='Transfers',
            erpnext_journal_entry_name='ACC-JV-9999', state=state,
            amount=10000.0))
        db.session.commit()
        return pair

    def test_a_stale_draft_is_abandoned_and_replaced(self):
        pair = self._leg_with_rule_entry('pending_review')
        client = self._client()
        self.assertIsNotNone(
            intercompany.generate_pair_journal_entries(client, pair))
        self.assertIn('ACC-JV-9999', client.deleted)
        gje = GeneratedJournalEntry.query.filter_by(
            plaid_transaction_id='t-out').first()
        self.assertEqual(gje.intercompany_pair_id, pair.id)
        self.assertNotEqual(gje.erpnext_journal_entry_name, 'ACC-JV-9999')

    def test_a_submitted_entry_blocks_rather_than_being_cancelled(self):
        # Posted activity is not something to undo behind the operator's back.
        pair = self._leg_with_rule_entry('approved')
        client = self._client()
        self.assertIsNone(
            intercompany.generate_pair_journal_entries(client, pair))
        self.assertEqual(client.deleted, set())
        self.assertEqual(client.created['Journal Entry'], {})
        self.assertIn('ACC-JV-9999', pair.note)
        self.assertEqual(pair.state, 'pending')


# ── sync integration + single-Company regression ────────────────────────

class TestSyncIntegration(IntercompanyBase):
    EXTRA_CONFIG = {'ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES': True,
                    'ERPNEXT_AUTO_CREATE_SUPPLIERS': False}

    def test_push_detects_and_books_in_one_run(self):
        self._two_companies()
        out, inn = self._transfer()
        for row in (out, inn):
            row.posted_at = None
            row.erpnext_bank_transaction_id = None
        db.session.commit()
        client = self._client()
        stats = sync_engine.push_pending(client, 'item-farm')
        self.assertEqual(stats['paired'], 1)
        self.assertEqual(stats['pairs_booked'], 1)
        pair = IntercompanyTransferPair.query.one()
        self.assertIsNotNone(pair.from_journal_entry)

    def test_the_generic_rule_does_not_also_fire(self):
        self._two_companies()
        db.session.add(CategorizationRule(
            name='Transfers', priority=10, active=True, archived=False,
            match_type='description_regex', match_value='Transfer',
            offset_account='Owner Draws', offset_direction='auto',
            ignore_for_paired=True))
        out, inn = self._transfer()
        for row in (out, inn):
            row.posted_at = None
            row.erpnext_bank_transaction_id = None
        db.session.commit()
        sync_engine.push_pending(self._client(), 'item-farm')
        # Exactly two JEs — the intercompany pair, not four.
        entries = GeneratedJournalEntry.query.all()
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e.intercompany_pair_id for e in entries))

    def test_single_company_sync_is_completely_unaffected(self):
        # The backward-compatibility guarantee: nothing about a one-Company
        # install changes, including the rules engine's ordinary behaviour.
        self._one_company()
        db.session.add(CategorizationRule(
            name='Transfers', priority=10, active=True, archived=False,
            match_type='description_regex', match_value='Transfer',
            offset_account='Owner Draws', offset_direction='auto'))
        out, inn = self._transfer()
        for row in (out, inn):
            row.posted_at = None
            row.erpnext_bank_transaction_id = None
        db.session.commit()
        stats = sync_engine.push_pending(self._client(), 'item-farm')
        self.assertEqual(stats['paired'], 0)
        self.assertEqual(IntercompanyTransferPair.query.count(), 0)
        for row in BankTransaction.query.all():
            self.assertIsNone(row.intercompany_pair_id)
        # …and the ordinary rule still generated its entries.
        self.assertEqual(GeneratedJournalEntry.query.count(), 2)


if __name__ == '__main__':
    unittest.main()
