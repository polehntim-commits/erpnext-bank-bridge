# SPDX-License-Identifier: MIT
"""Idempotent re-import after a disconnect + re-link (v0.4.15).

THE BUG THESE COVER. v0.4.11 made a re-link idempotent by fingerprinting the new
PlaidAccount rows against the retired ones and moving the mapping across
(app/reconnect.py). That works, but it is a best-effort matcher with several
legitimate ways to decline — a blank mask, a blank institution_id on the older
Item, more than one candidate — and EVERY decline fell through to an unguarded
create, which collided with the record already in ERPNext and raised
DuplicateEntryError. It also never covered the loan types v0.4.14 added.

v0.4.15 adds a second, independent tier that asks ERPNext directly: is this real
account already on the books under the Plaid id it used to carry? Because it
lives in the shared find-or-create path it covers depository, credit, loan and
investment accounts at once.

Covered here:

  * tier 1 — Bank + last-4 + Company adopts the existing record
  * tier 1 fires for every supported kind: depository (checking, savings, CD,
    money market, cash management), credit, loan (mortgage, student, auto) and
    investment (401k, IRA, HSA, brokerage)
  * tier 2 — Bank + subtype + Company, and ONLY when unambiguous
  * NEVER cross-Company: a record under another Company is left alone and a new
    one is created under the target
  * NEVER steal a record a live Plaid account still claims
  * adoption repoints plaid_account_id and writes an audit event
  * several matches resolve to the most recently modified record
  * the cleanup page lists unlinked records, groups them, and deletes
    idempotently
  * regressions: a fresh install still creates everything, and the v0.4.11
    reconnect path and v0.4.14 loan classification are untouched

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
import unittest.mock

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import account_cleanup, create_app, crypto, db  # noqa: E402
from app import erpnext_accounts, erpnext_settings  # noqa: E402
from app.models import AuditEvent, PlaidAccount, PlaidItem  # noqa: E402

from tests.fakes import FakeERPClient  # noqa: E402

COMPANY = 'Example Company LLC'
OTHER_COMPANY = 'Bank Bridge Test'


class RelinkBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        erpnext_settings.save('http://erp.test', 'K', 'SECRET', COMPANY)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    # ── fixtures ────────────────────────────────────────────────────────────

    def _item(self, item_id='item-new', institution='Wells Fargo',
              institution_id='ins_1', disconnected=False, company=COMPANY):
        it = PlaidItem(item_id=item_id,
                       access_token_encrypted=crypto.encrypt('access-x'),
                       institution_id=institution_id,
                       institution_name=institution, status='active',
                       disconnected=disconnected, owning_company=company)
        db.session.add(it)
        db.session.commit()
        return it

    def _account(self, account_id, item_id='item-new', mask='4444',
                 type_='depository', subtype='checking', company=COMPANY,
                 bank_account=None, superseded=None):
        a = PlaidAccount(account_id=account_id, item_id=item_id,
                         name=f'{subtype} {mask}', mask=mask, type=type_,
                         subtype=subtype, owning_company=company,
                         erpnext_bank_account_name=bank_account,
                         superseded_by_account_id=superseded,
                         import_status='pending')
        db.session.add(a)
        db.session.commit()
        return a

    def _existing(self, client, docname, *, bank='Wells Fargo',
                  account_name=None, company=COMPANY, last_4='4444',
                  subtype='Checking', plaid_account_id='', modified='2026-01-01'):
        """Stage a Bank Account already on the operator's books — the record an
        earlier install created and a re-link now collides with."""
        client.existing_bank_accounts[docname] = {
            'account_name': account_name or docname.rsplit(' - ', 1)[0],
            'bank': bank, 'company': company, 'last_4': last_4,
            'account_subtype': subtype, 'plaid_account_id': plaid_account_id,
            'modified': modified,
        }
        return docname

    def _import(self, account, client):
        return erpnext_accounts.import_plaid_account_to_erpnext(
            account.account_id, client=client, ensure_fields=False)


# ── tier 1: bank + last-4 + Company ─────────────────────────────────────────

class MaskTierTests(RelinkBase):
    def test_adopts_the_existing_record_instead_of_colliding(self):
        """The headline bug: a re-linked account reuses the record already in
        ERPNext rather than creating a duplicate docname."""
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                             plaid_account_id='dead-old-id')
        acct = self._account('fresh-plaid-id')

        res = self._import(acct, erp)

        self.assertEqual(res['status'], 'imported')
        self.assertEqual(res['bank_account'], doc)
        self.assertFalse(res['created_account'])
        self.assertEqual(erp.creates_of('Bank Account'), [],
                         'adoption must not create a second Bank Account')

    def test_adoption_repoints_the_dedup_key_at_the_new_plaid_id(self):
        """Without the repoint the NEXT import misses again and re-creates the
        duplicate this whole path exists to prevent."""
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                             plaid_account_id='dead-old-id')
        acct = self._account('fresh-plaid-id')

        self._import(acct, erp)

        updates = [c for c in erp.calls
                   if c[0] == 'update_doc' and c[1] == 'Bank Account']
        self.assertTrue(updates, 'expected a repoint write')
        self.assertEqual(updates[-1][3]['plaid_account_id'], 'fresh-plaid-id')
        self.assertEqual(updates[-1][2], doc)

    def test_adoption_writes_an_audit_event_naming_the_tier(self):
        erp = FakeERPClient()
        self._item()
        self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                       plaid_account_id='dead-old-id')
        acct = self._account('fresh-plaid-id')

        self._import(acct, erp)

        ev = AuditEvent.query.filter_by(
            event_type='erpnext_bank_account_adopted').first()
        self.assertIsNotNone(ev, 'adoption must be auditable')
        self.assertEqual(ev.subject_id, 'fresh-plaid-id')
        self.assertIn('mask', ev.payload_after)

    def test_the_mask_is_read_from_the_account_name_when_the_field_is_missing(self):
        """An ERPNext whose Custom Field doctype was unavailable at bootstrap
        has the last-4 in the account_name and nowhere else."""
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                             last_4='')
        acct = self._account('fresh-plaid-id')

        res = self._import(acct, erp)

        self.assertEqual(res['bank_account'], doc)

    def test_a_different_last_four_is_not_adopted_on_the_mask_tier(self):
        erp = FakeERPClient()
        self._item()
        self._existing(erp, 'Wells Fargo Checking - 9999 - Wells Fargo',
                       last_4='9999', subtype='Checking')
        # Two candidates of one subtype would make the subtype tier ambiguous;
        # add a second so this test isolates the MASK tier.
        self._existing(erp, 'Wells Fargo Checking - 8888 - Wells Fargo',
                       last_4='8888', subtype='Checking')
        acct = self._account('fresh-plaid-id', mask='4444')

        res = self._import(acct, erp)

        self.assertTrue(res['created_account'],
                        'no mask match and an ambiguous subtype → create new')

    def test_an_account_with_no_mask_falls_through_to_the_subtype_tier(self):
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo')
        acct = self._account('fresh-plaid-id', mask='')

        res = self._import(acct, erp)

        self.assertEqual(res['bank_account'], doc)


# ── every supported account kind ────────────────────────────────────────────

class EveryAccountKindTests(RelinkBase):
    """v0.4.14's loan types and v0.4.12's investment types were never wired into
    the reconnect-layer adoption. Because v0.4.15 sits in the shared
    find-or-create path, all four families are covered by construction — these
    pin that down so a future kind cannot quietly regress."""

    KINDS = [
        ('depository', 'checking', 'Checking'),
        ('depository', 'savings', 'Savings'),
        ('depository', 'cd', 'Cd'),
        ('depository', 'money market', 'Money Market'),
        ('depository', 'cash management', 'Cash Management'),
        ('credit', 'credit card', 'Credit Card'),
        ('loan', 'mortgage', 'Mortgage'),
        ('loan', 'student', 'Student'),
        ('loan', 'auto', 'Auto'),
        ('investment', '401k', '401K'),
        ('investment', 'ira', 'Ira'),
        ('investment', 'hsa', 'Hsa'),
        ('investment', 'brokerage', 'Brokerage'),
    ]

    def test_every_supported_kind_adopts_rather_than_duplicating(self):
        self._item()
        for type_, subtype, title in self.KINDS:
            with self.subTest(type=type_, subtype=subtype):
                erp = FakeERPClient()
                mask = '4444'
                docname = f'Wells Fargo {title} - {mask} - Wells Fargo'
                # The stored subtype is whatever the importer would have written
                # for this kind, so the subtype tier is exercised truthfully.
                acct_id = f'fresh-{subtype.replace(" ", "-")}'
                acct = self._account(acct_id, mask=mask, type_=type_,
                                     subtype=subtype)
                self.assertTrue(erpnext_accounts.is_supported(acct),
                                f'{type_}/{subtype} should be supported')
                self._existing(
                    erp, docname, last_4=mask,
                    subtype=erpnext_accounts.erpnext_account_subtype(acct),
                    plaid_account_id='dead-old-id')

                res = self._import(acct, erp)

                self.assertEqual(res['bank_account'], docname)
                self.assertFalse(res['created_account'])
                self.assertEqual(erp.creates_of('Bank Account'), [])
                db.session.query(PlaidAccount).delete()
                db.session.commit()

    def test_loan_classification_survives_adoption(self):
        """v0.4.14 regression: an adopted mortgage is still a loan, so it stays
        balance-only and lands on the liability side."""
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Mortgage - 8888 - Wells Fargo',
                             last_4='8888', subtype='Mortgage')
        acct = self._account('fresh-mortgage', mask='8888', type_='loan',
                             subtype='mortgage')

        res = self._import(acct, erp)

        self.assertEqual(res['bank_account'], doc)
        self.assertTrue(erpnext_accounts.is_loan(acct))
        self.assertEqual(erpnext_accounts._gl_side(acct), 'loan')


# ── tier 2: bank + subtype + Company ────────────────────────────────────────

class SubtypeTierTests(RelinkBase):
    def test_a_reissued_account_with_a_new_last_four_adopts_on_subtype(self):
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Savings - 1111 - Wells Fargo',
                             last_4='1111', subtype='Savings')
        acct = self._account('fresh-plaid-id', mask='2222', subtype='savings')

        res = self._import(acct, erp)

        self.assertEqual(res['bank_account'], doc)
        self.assertFalse(res['created_account'])

    def test_two_accounts_of_one_subtype_are_ambiguous_and_not_adopted(self):
        """Guessing here would weld one real account's ledger onto another."""
        erp = FakeERPClient()
        self._item()
        self._existing(erp, 'Wells Fargo Savings - 1111 - Wells Fargo',
                       last_4='1111', subtype='Savings')
        self._existing(erp, 'Wells Fargo Savings - 3333 - Wells Fargo',
                       last_4='3333', subtype='Savings')
        acct = self._account('fresh-plaid-id', mask='2222', subtype='savings')

        res = self._import(acct, erp)

        self.assertTrue(res['created_account'],
                        'ambiguity must under-adopt, not guess')

    def test_a_different_subtype_is_not_adopted(self):
        erp = FakeERPClient()
        self._item()
        self._existing(erp, 'Wells Fargo Savings - 1111 - Wells Fargo',
                       last_4='1111', subtype='Savings')
        acct = self._account('fresh-plaid-id', mask='2222', subtype='checking')

        res = self._import(acct, erp)

        self.assertTrue(res['created_account'])


# ── the guard rails ─────────────────────────────────────────────────────────

class NeverCrossCompanyTests(RelinkBase):
    def test_a_record_under_another_company_is_not_adopted(self):
        """Tim's case: the dry-run records sit under a different Company. Those
        are another entity's books — create a fresh record under the target."""
        erp = FakeERPClient()
        self._item()
        self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                       company=OTHER_COMPANY)
        acct = self._account('fresh-plaid-id', company=COMPANY)

        res = self._import(acct, erp)

        self.assertTrue(res['created_account'])
        created = erp.creates_of('Bank Account')
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][2]['company'], COMPANY)

    def test_the_right_company_is_adopted_when_both_exist(self):
        erp = FakeERPClient()
        self._item()
        self._existing(erp, 'Wells Fargo Checking - 4444 - BBT',
                       company=OTHER_COMPANY)
        mine = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                              company=COMPANY)
        acct = self._account('fresh-plaid-id', company=COMPANY)

        res = self._import(acct, erp)

        self.assertEqual(res['bank_account'], mine)

    def test_a_blank_company_does_not_match_a_named_one(self):
        erp = FakeERPClient()
        self._item()
        self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                       company='')
        acct = self._account('fresh-plaid-id', company=COMPANY)

        res = self._import(acct, erp)

        self.assertTrue(res['created_account'])


class NeverStealALiveRecordTests(RelinkBase):
    def test_a_record_a_live_account_claims_is_left_alone(self):
        """Two Plaid accounts pointing at one Bank Account would double-post
        every transaction in the overlap."""
        erp = FakeERPClient()
        self._item()
        self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                       plaid_account_id='live-sibling')
        self._account('live-sibling',
                      bank_account='Wells Fargo Checking - 4444 - Wells Fargo')
        acct = self._account('fresh-plaid-id')

        res = self._import(acct, erp)

        self.assertTrue(res['created_account'])

    def test_a_record_claimed_by_a_disconnected_account_is_adoptable(self):
        """The disconnect + re-link case. The old row still exists (disconnect
        keeps it) but its bank is gone, so the record is free."""
        erp = FakeERPClient()
        self._item('item-old', disconnected=True)
        self._item('item-new')
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                             plaid_account_id='old-id')
        self._account('old-id', item_id='item-old', bank_account=None)
        acct = self._account('fresh-plaid-id', item_id='item-new')

        res = self._import(acct, erp)

        self.assertEqual(res['bank_account'], doc)

    def test_a_record_claimed_by_a_superseded_account_is_adoptable(self):
        """v0.4.11 retired the donor; its id is stale by definition."""
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                             plaid_account_id='retired-id')
        self._account('retired-id', superseded='someone-else')
        acct = self._account('fresh-plaid-id')

        res = self._import(acct, erp)

        self.assertEqual(res['bank_account'], doc)

    def test_a_dangling_plaid_id_is_adoptable(self):
        """The local row is gone entirely — nothing can be double-posting."""
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                             plaid_account_id='never-heard-of-it')
        acct = self._account('fresh-plaid-id')

        res = self._import(acct, erp)

        self.assertEqual(res['bank_account'], doc)


class AmbiguityResolutionTests(RelinkBase):
    def test_several_mask_matches_resolve_to_the_most_recent(self):
        """An operator who has been re-running a dry-run wants the newest of
        their attempts, not an arbitrary one."""
        erp = FakeERPClient()
        self._item()
        self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                       modified='2026-01-01')
        newest = self._existing(erp, 'WF Checking - 4444 - Wells Fargo',
                                modified='2026-06-01')
        acct = self._account('fresh-plaid-id')

        res = self._import(acct, erp)

        self.assertEqual(res['bank_account'], newest)

    def test_a_different_bank_is_never_matched(self):
        """A Wells Fargo ...4444 must not adopt a Chase ...4444."""
        erp = FakeERPClient()
        self._item(institution='Wells Fargo')
        self._existing(erp, 'Chase Checking - 4444 - Chase', bank='Chase')
        acct = self._account('fresh-plaid-id')

        res = self._import(acct, erp)

        self.assertTrue(res['created_account'])


# ── switch + regressions ────────────────────────────────────────────────────

class SwitchAndRegressionTests(RelinkBase):
    def test_the_switch_restores_pre_v0415_behaviour(self):
        erp = FakeERPClient()
        self.app.config['ERPNEXT_ADOPT_EXISTING'] = False
        self._item()
        self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo')
        acct = self._account('fresh-plaid-id')

        res = self._import(acct, erp)

        self.assertTrue(res['created_account'])

    def test_a_fresh_install_creates_everything_as_before(self):
        """No pre-existing records → the create path is untouched."""
        erp = FakeERPClient()
        self._item()
        acct = self._account('brand-new')

        res = self._import(acct, erp)

        self.assertEqual(res['status'], 'imported')
        self.assertTrue(res['created_account'])
        self.assertEqual(len(erp.creates_of('Bank Account')), 1)

    def test_importing_the_same_account_twice_still_dedups_on_the_plaid_id(self):
        erp = FakeERPClient()
        self._item()
        acct = self._account('brand-new')

        first = self._import(acct, erp)
        acct.erpnext_bank_account_name = None      # force the lookup to re-run
        db.session.commit()
        second = self._import(acct, erp)

        self.assertEqual(first['bank_account'], second['bank_account'])
        self.assertFalse(second['created_account'])
        self.assertEqual(len(erp.creates_of('Bank Account')), 1)

    def test_an_unreadable_erpnext_declines_rather_than_raising(self):
        """A failed fingerprint lookup must never sink an import — it falls
        through to the create, which is the pre-v0.4.15 behaviour."""
        erp = FakeERPClient()
        erp.fail_list['Bank Account'] = (500, '{"exc":"boom"}')
        self._item()
        acct = self._account('fresh-plaid-id')

        docname, tier = erpnext_accounts.fingerprint_existing_bank_account(
            erp, acct, 'Wells Fargo', COMPANY)

        self.assertIsNone(docname)
        self.assertEqual(tier, '')


# ── cleanup ─────────────────────────────────────────────────────────────────

class CleanupListingTests(RelinkBase):
    def test_lists_records_no_live_account_claims(self):
        erp = FakeERPClient()
        self._item()
        self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo')
        self._existing(erp, 'Wells Fargo Savings - 5555 - Wells Fargo',
                       last_4='5555', subtype='Savings')

        rows = account_cleanup.unlinked_bank_accounts(client=erp)

        self.assertEqual({r['name'] for r in rows},
                         {'Wells Fargo Checking - 4444 - Wells Fargo',
                          'Wells Fargo Savings - 5555 - Wells Fargo'})

    def test_a_mapped_record_is_not_listed(self):
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo')
        self._account('live-id', bank_account=doc)

        rows = account_cleanup.unlinked_bank_accounts(client=erp)

        self.assertEqual(rows, [])

    def test_a_record_claimed_by_a_live_plaid_id_is_not_listed(self):
        """The pointer may have drifted, but the record is still in use — and
        the fingerprint would refuse to adopt it, so cleanup must refuse to
        offer it. The two definitions have to agree."""
        erp = FakeERPClient()
        self._item()
        self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                       plaid_account_id='live-id')
        self._account('live-id')

        rows = account_cleanup.unlinked_bank_accounts(client=erp)

        self.assertEqual(rows, [])

    def test_a_record_from_a_disconnected_bank_is_listed(self):
        erp = FakeERPClient()
        self._item('item-old', disconnected=True)
        self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo',
                       plaid_account_id='old-id')
        self._account('old-id', item_id='item-old')

        rows = account_cleanup.unlinked_bank_accounts(client=erp)

        self.assertEqual(len(rows), 1)

    def test_grouping_is_by_bank_and_alphabetical(self):
        rows = [{'name': 'a', 'bank': 'Wells Fargo'},
                {'name': 'b', 'bank': 'Chase'},
                {'name': 'c', 'bank': 'Wells Fargo'},
                {'name': 'd', 'bank': ''}]

        groups = account_cleanup.group_by_bank(rows)

        self.assertEqual([g['bank'] for g in groups],
                         ['Chase', 'No bank', 'Wells Fargo'])
        self.assertEqual([g['count'] for g in groups], [1, 1, 2])


class CleanupDeletionTests(RelinkBase):
    def test_deletes_an_unlinked_record(self):
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo')

        res = account_cleanup.delete_bank_account(doc, client=erp)

        self.assertEqual(res['status'], 'deleted')
        self.assertIn(doc, erp.deleted)

    def test_deletion_is_idempotent(self):
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo')

        account_cleanup.delete_bank_account(doc, client=erp)
        again = account_cleanup.delete_bank_account(doc, client=erp)

        self.assertEqual(again['status'], 'gone')

    def test_deletion_is_audited(self):
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo')

        account_cleanup.delete_bank_account(doc, client=erp)

        ev = AuditEvent.query.filter_by(
            event_type='erpnext_bank_account_deleted').first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.subject_id, doc)

    def test_a_record_that_became_mapped_is_left_alone(self):
        """The page the operator submitted from may be minutes old."""
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo')
        self._account('live-id', bank_account=doc)

        res = account_cleanup.delete_bank_account(doc, client=erp)

        self.assertEqual(res['status'], 'linked')
        self.assertNotIn(doc, erp.deleted)

    def test_erpnexts_refusal_is_reported_not_raised(self):
        """ERPNext refuses to delete a record with linked documents. That is the
        guard rail working, so it is an outcome, not an error."""
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo')
        erp.method_error = ('frappe.client.delete',
                            (417, '{"exc":"LinkExistsError"}'))

        res = account_cleanup.delete_bank_account(doc, client=erp)

        self.assertEqual(res['status'], 'in_use')
        self.assertIn('linked documents', res['message'])

    def test_delete_many_summarises_outcomes(self):
        erp = FakeERPClient()
        self._item()
        a = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo')
        b = self._existing(erp, 'Wells Fargo Savings - 5555 - Wells Fargo',
                           last_4='5555', subtype='Savings')
        self._account('live-id', bank_account=b)

        out = account_cleanup.delete_many([a, b], client=erp)

        self.assertEqual(out['deleted'], 1)
        self.assertEqual(out['skipped'], 1)
        self.assertIn(a, erp.deleted)
        self.assertNotIn(b, erp.deleted)

    def test_delete_many_with_nothing_selected_is_a_no_op(self):
        erp = FakeERPClient()

        out = account_cleanup.delete_many([], client=erp)

        self.assertEqual(out, {'results': [], 'deleted': 0, 'skipped': 0})


class CleanupPageTests(RelinkBase):
    def test_the_page_renders_and_lists_unlinked_records(self):
        erp = FakeERPClient()
        self._item()
        self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo')
        with unittest.mock.patch.object(account_cleanup, 'get_client',
                                        return_value=erp):
            r = self.app.test_client().get('/admin/accounts/cleanup')

        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('Wells Fargo Checking - 4444 - Wells Fargo', body)
        self.assertIn('Unlinked ERPNext Bank Accounts', body)

    def test_the_page_says_so_when_there_is_nothing_to_clean(self):
        erp = FakeERPClient()
        with unittest.mock.patch.object(account_cleanup, 'get_client',
                                        return_value=erp):
            r = self.app.test_client().get('/admin/accounts/cleanup')

        self.assertIn('Nothing to clean up', r.get_data(as_text=True))

    def test_posting_deletes_the_ticked_records(self):
        erp = FakeERPClient()
        self._item()
        doc = self._existing(erp, 'Wells Fargo Checking - 4444 - Wells Fargo')
        with unittest.mock.patch.object(account_cleanup, 'get_client',
                                        return_value=erp):
            r = self.app.test_client().post('/admin/accounts/cleanup',
                                            data={'docname': doc})

        self.assertEqual(r.status_code, 200)
        self.assertIn(doc, erp.deleted)
        self.assertIn('1 deleted', r.get_data(as_text=True))

    def test_posting_nothing_is_a_no_op(self):
        erp = FakeERPClient()
        with unittest.mock.patch.object(account_cleanup, 'get_client',
                                        return_value=erp):
            r = self.app.test_client().post('/admin/accounts/cleanup', data={})

        self.assertEqual(r.status_code, 200)
        self.assertIn('Nothing was selected', r.get_data(as_text=True))
        self.assertEqual(erp.deleted, set())

    def test_the_accounts_page_links_to_the_cleanup_page(self):
        r = self.app.test_client().get('/admin/accounts')

        self.assertEqual(r.status_code, 200)
        self.assertIn('/admin/accounts/cleanup', r.get_data(as_text=True))


if __name__ == '__main__':      # pragma: no cover
    unittest.main()
