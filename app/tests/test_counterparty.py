# SPDX-License-Identifier: MIT
"""v0.4.5 · Counterparty overlay — one identity over Customer + Supplier.

The gap these cover: ERPNext keeps the buy side and the sell side in unrelated
doctypes, so a party that trades both ways (a bank, a wholesaler, a neighbouring
grower) is two records with no link. Nothing can answer "what is my net position
with Wells Fargo?", and the 1099 list is "every Supplier" — which includes your
bank and the IRS.

v0.4.5 adds a `Counterparty` doctype, provisioned over REST at startup, that
links 0-or-1 Customer and 0-or-1 Supplier and owns no accounting whatsoever.

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import counterparty, erpnext_accounts, erpnext_bank  # noqa: E402
from app.models import Customer, Supplier  # noqa: E402
from app.services import scheduler  # noqa: E402
from scripts import pair_existing_customer_supplier as pair_script  # noqa: E402
from tests.fakes import FakeERPClient  # noqa: E402


def gl(party_type, party, posting_date, debit=0.0, credit=0.0,
       account='Debtors - T', voucher_no='ACC-JV-0001', company='Testing'):
    """One GL Entry row as Frappe hands it back (dates as ISO strings)."""
    return {'party_type': party_type, 'party': party,
            'posting_date': posting_date, 'debit': debit, 'credit': credit,
            'account': account, 'voucher_type': 'Journal Entry',
            'voucher_no': voucher_no, 'company': company, 'remarks': '',
            'is_cancelled': 0}


class Base(unittest.TestCase):
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

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _ready(self, **kw):
        """A fake ERPNext that already has the Counterparty doctype, with the
        overlay marked available — the steady state after one bootstrap."""
        kw.setdefault('counterparty_doctype', True)
        client = FakeERPClient(**kw)
        counterparty.ensure_counterparty_doctype(client)
        return client


# ── doctype bootstrap ────────────────────────────────────────────

class DoctypeBootstrapTests(Base):
    def test_creates_the_doctype_when_absent(self):
        client = FakeERPClient(counterparty_doctype=False)
        self.assertTrue(counterparty.ensure_counterparty_doctype(client))
        spec = client.created['DocType']['Counterparty']
        self.assertEqual(spec['autoname'], 'field:counterparty_name')
        self.assertEqual(spec['custom'], 1)
        self.assertEqual(spec['module'], 'Accounts')
        names = [f['fieldname'] for f in spec['fields']]
        self.assertEqual(names, [
            'counterparty_name', 'counterparty_type', 'customer_link',
            'supplier_link', 'dual_role_flag', 'date_of_first_transaction',
            'total_activity_ar', 'total_activity_ap', 'notes'])
        # Every field carries an explicit label — Frappe renders a label-less
        # field blank in the desk.
        self.assertTrue(all(f.get('label') for f in spec['fields']))

    def test_is_idempotent_across_repeated_bootstraps(self):
        """The whole point: it is safe to run on EVERY startup."""
        client = FakeERPClient(counterparty_doctype=False)
        for _ in range(4):
            self.assertTrue(counterparty.ensure_counterparty_doctype(client))
        creates = [c for c in client.calls
                   if c[0] == 'create_doc' and c[1] == 'DocType']
        self.assertEqual(len(creates), 1)

    def test_existing_doctype_is_never_recreated(self):
        client = FakeERPClient(counterparty_doctype=True)
        self.assertTrue(counterparty.ensure_counterparty_doctype(client))
        self.assertEqual(client.created['DocType'], {})

    def test_permission_error_degrades_to_unavailable(self):
        """An API user without DocType rights must disable the overlay, not
        crash the bootstrap that called it."""
        client = FakeERPClient(doctype_permission_error=True)
        self.assertFalse(counterparty.ensure_counterparty_doctype(client))
        self.assertTrue(
            erpnext_accounts.is_doctype_unavailable('Counterparty'))
        self.assertFalse(counterparty.available(client))

    def test_failed_create_degrades_to_unavailable(self):
        client = FakeERPClient(counterparty_doctype=False,
                               fail_doctype_create=True)
        self.assertFalse(counterparty.ensure_counterparty_doctype(client))
        self.assertFalse(counterparty.available(client))

    def test_master_switch_off_skips_everything(self):
        self.app.config['COUNTERPARTY_OVERLAY_ENABLED'] = False
        client = FakeERPClient(counterparty_doctype=False)
        self.assertFalse(counterparty.available(client))
        status = counterparty.bootstrap(client)
        self.assertFalse(status['available'])
        self.assertEqual(client.created['DocType'], {})

    def test_erpnext_bootstrap_reports_the_overlay_without_failing_on_it(self):
        """A refused overlay is an install without the feature, NOT a partial
        account-import bootstrap — it must stay out of `partial`."""
        client = FakeERPClient(doctype_permission_error=True)
        status = erpnext_accounts.bootstrap(client)
        self.assertFalse(status['Counterparty'])
        self.assertFalse(status['partial'])


# ── find-or-create + linking ─────────────────────────────────────

class LinkingTests(Base):
    def test_creates_a_counterparty_with_a_supplier_link(self):
        client = self._ready()
        name = counterparty.find_or_create_counterparty(
            client, 'Uber', supplier='Uber')
        self.assertEqual(name, 'Uber')
        doc = client.created['Counterparty']['Uber']
        self.assertEqual(doc['supplier_link'], 'Uber')
        self.assertEqual(doc['dual_role_flag'], 0)
        self.assertEqual(doc['counterparty_type'], 'Company')

    def test_creates_a_counterparty_with_a_customer_link(self):
        client = self._ready()
        counterparty.find_or_create_counterparty(client, 'FUN', customer='FUN')
        doc = client.created['Counterparty']['FUN']
        self.assertEqual(doc['customer_link'], 'FUN')
        self.assertEqual(doc['dual_role_flag'], 0)

    def test_second_role_sets_the_dual_role_flag(self):
        """Wells Fargo charges you fees AND pays you interest — one identity,
        both roles, flagged."""
        client = self._ready()
        counterparty.find_or_create_counterparty(
            client, 'Wells Fargo', supplier='Wells Fargo')
        counterparty.find_or_create_counterparty(
            client, 'Wells Fargo', customer='Wells Fargo')
        doc = client.created['Counterparty']['Wells Fargo']
        self.assertEqual(doc['supplier_link'], 'Wells Fargo')
        self.assertEqual(doc['customer_link'], 'Wells Fargo')
        self.assertEqual(doc['dual_role_flag'], 1)

    def test_repeat_link_writes_nothing(self):
        client = self._ready()
        counterparty.find_or_create_counterparty(
            client, 'Uber', supplier='Uber')
        before = len(client.calls)
        for _ in range(3):
            counterparty.find_or_create_counterparty(
                client, 'Uber', supplier='Uber')
        updates = [c for c in client.calls[before:] if c[0] == 'update_doc']
        self.assertEqual(updates, [])

    def test_a_human_set_link_is_never_stomped(self):
        """Auto-link fills blanks. It does not overwrite an operator's
        correction."""
        client = self._ready()
        client.created['Counterparty']['Acme'] = {
            'counterparty_name': 'Acme', 'counterparty_type': 'Company',
            'supplier_link': 'Acme Farms LLC', 'dual_role_flag': 0}
        counterparty.find_or_create_counterparty(
            client, 'Acme', supplier='Acme')
        self.assertEqual(
            client.created['Counterparty']['Acme']['supplier_link'],
            'Acme Farms LLC')

    def test_concurrent_create_recovers_by_refetching(self):
        """Two syncs racing on the same new party: the loser must link onto the
        winner's document, not report a failure."""
        client = self._ready(counterparty_create_race=True)
        name = counterparty.find_or_create_counterparty(
            client, 'Gusto', supplier='Gusto')
        self.assertEqual(name, 'Gusto')
        self.assertIn('Gusto', client.created['Counterparty'])

    def test_bank_names_are_typed_as_financial_institutions(self):
        client = self._ready()
        counterparty.find_or_create_counterparty(
            client, 'Wells Fargo', supplier='Wells Fargo')
        self.assertEqual(
            client.created['Counterparty']['Wells Fargo']['counterparty_type'],
            'Financial Institution')

    def test_ordinary_names_default_to_company(self):
        client = self._ready()
        for name in ('Uber', 'Starbucks', "McDonald's", 'FUN', 'GUSTO'):
            counterparty.find_or_create_counterparty(
                client, name, supplier=name)
            self.assertEqual(
                client.created['Counterparty'][name]['counterparty_type'],
                'Company', f'{name} should not be typed an institution')

    def test_unavailable_overlay_is_a_silent_no_op(self):
        client = FakeERPClient(doctype_permission_error=True)
        counterparty.ensure_counterparty_doctype(client)
        self.assertIsNone(counterparty.find_or_create_counterparty(
            client, 'Uber', supplier='Uber'))
        self.assertIsNone(counterparty.link_party(
            client, 'Uber', 'Supplier', 'Uber'))


# ── auto-link from the party creation paths ──────────────────────

class AutoLinkTests(Base):
    def test_ensure_supplier_links_a_counterparty(self):
        client = self._ready()
        erpnext_bank.ensure_supplier(client, 'Uber')
        self.assertEqual(
            client.created['Counterparty']['Uber']['supplier_link'], 'Uber')

    def test_ensure_customer_links_a_counterparty(self):
        client = self._ready()
        erpnext_bank.ensure_customer(client, 'FUN')
        self.assertEqual(
            client.created['Counterparty']['FUN']['customer_link'], 'FUN')

    def test_dual_role_party_creation_produces_one_dual_counterparty(self):
        """ensure_party provisions BOTH ERPNext records for a bank; the overlay
        must end up with ONE Counterparty carrying both links."""
        client = self._ready()
        erpnext_bank.ensure_party(client, 'Wells Fargo', 'Customer',
                                  source='institution')
        doc = client.created['Counterparty']['Wells Fargo']
        self.assertEqual(doc['customer_link'], 'Wells Fargo')
        self.assertEqual(doc['supplier_link'], 'Wells Fargo')
        self.assertEqual(doc['dual_role_flag'], 1)
        self.assertEqual(doc['counterparty_type'], 'Financial Institution')

    def test_merchant_path_links_a_counterparty(self):
        client = self._ready()
        erpnext_bank.get_or_create_supplier(client, 'STARBUCKS #1234',
                                            amount=-5.75)
        self.assertIn('Starbucks', client.created['Counterparty'])

    def test_a_broken_overlay_never_costs_us_the_party(self):
        """The load-bearing guarantee: the Journal Entry still needs its
        Supplier, so an overlay failure must be invisible to this caller."""
        client = FakeERPClient(counterparty_doctype=True,
                               fail_counterparty_create=True)
        counterparty.ensure_counterparty_doctype(client)
        self.assertEqual(erpnext_bank.ensure_supplier(client, 'Uber'), 'Uber')
        self.assertIsNotNone(Supplier.query.filter_by(
            normalized_name='Uber').first())

    def test_overlay_disabled_leaves_party_creation_untouched(self):
        self.app.config['COUNTERPARTY_OVERLAY_ENABLED'] = False
        client = FakeERPClient()
        self.assertEqual(erpnext_bank.ensure_customer(client, 'FUN'), 'FUN')
        self.assertIsNotNone(Customer.query.filter_by(
            normalized_name='FUN').first())
        self.assertEqual(client.created['Counterparty'], {})


# ── pairing migration ────────────────────────────────────────────

TIMS_SUPPLIERS = ['Uber', 'Starbucks', "McDonald's", 'United', 'FUN',
                  'Wells Fargo', 'GUSTO']
TIMS_CUSTOMERS = ['Wells Fargo']


class PairingTests(Base):
    def _tims_install(self, **kw):
        return self._ready(existing_suppliers=TIMS_SUPPLIERS,
                           existing_customers=TIMS_CUSTOMERS, **kw)

    def test_pairs_tims_install_into_seven_counterparties(self):
        """Tim's actual install: 7 Suppliers, 1 Customer that duplicates one of
        them → 7 Counterparties, exactly one of them dual-role."""
        client = self._tims_install()
        result = counterparty.pair_existing_parties(client)
        self.assertEqual(result['created'], 7)
        self.assertEqual(result['failed'], 0)
        made = client.created['Counterparty']
        self.assertEqual(sorted(made), sorted(TIMS_SUPPLIERS))
        duals = [n for n, d in made.items() if d.get('dual_role_flag')]
        self.assertEqual(duals, ['Wells Fargo'])
        self.assertEqual(made['Wells Fargo']['customer_link'], 'Wells Fargo')
        self.assertEqual(made['Wells Fargo']['supplier_link'], 'Wells Fargo')

    def test_pairing_is_idempotent(self):
        client = self._tims_install()
        counterparty.pair_existing_parties(client)
        second = counterparty.pair_existing_parties(client)
        self.assertEqual(second['created'], 0)
        self.assertEqual(second['linked'], 0)
        self.assertEqual(second['unchanged'], 7)
        self.assertEqual(second['failed'], 0)

    def test_dry_run_writes_nothing(self):
        client = self._tims_install()
        result = counterparty.pair_existing_parties(client, dry_run=True)
        self.assertEqual(result['created'], 7)
        self.assertEqual(client.created['Counterparty'], {})
        self.assertEqual(
            [c for c in client.calls if c[0] == 'create_doc'
             and c[1] == 'Counterparty'], [])

    def test_pairing_links_a_side_added_later(self):
        """A Counterparty that already exists with one role picks up the other
        when ERPNext gains the matching party."""
        client = self._ready(existing_suppliers=['Wells Fargo'])
        counterparty.pair_existing_parties(client)
        self.assertEqual(
            client.created['Counterparty']['Wells Fargo'].get('customer_link'),
            None)
        client.existing_customers.add('Wells Fargo')
        result = counterparty.pair_existing_parties(client)
        self.assertEqual(result['linked'], 1)
        doc = client.created['Counterparty']['Wells Fargo']
        self.assertEqual(doc['customer_link'], 'Wells Fargo')
        self.assertEqual(doc['dual_role_flag'], 1)

    def test_pairing_is_a_no_op_when_the_overlay_is_unavailable(self):
        client = FakeERPClient(doctype_permission_error=True)
        counterparty.ensure_counterparty_doctype(client)
        result = counterparty.pair_existing_parties(client)
        self.assertEqual(result['created'], 0)
        self.assertEqual(client.created['Counterparty'], {})

    def test_script_run_pairs_and_reports(self):
        client = self._tims_install()
        result = pair_script.run(client)
        self.assertTrue(result['available'])
        self.assertEqual(result['created'], 7)

    def test_script_dry_run_writes_nothing(self):
        client = self._tims_install()
        result = pair_script.run(client, dry_run=True)
        self.assertEqual(result['created'], 7)
        self.assertEqual(client.created['Counterparty'], {})

    def test_bootstrap_pairs_on_first_startup(self):
        client = FakeERPClient(counterparty_doctype=False,
                               existing_suppliers=TIMS_SUPPLIERS,
                               existing_customers=TIMS_CUSTOMERS)
        status = counterparty.bootstrap(client)
        self.assertTrue(status['available'])
        self.assertEqual(status['paired']['created'], 7)

    def test_bootstrap_respects_the_auto_pair_switch(self):
        self.app.config['COUNTERPARTY_AUTO_PAIR'] = False
        client = FakeERPClient(counterparty_doctype=False,
                               existing_suppliers=TIMS_SUPPLIERS)
        status = counterparty.bootstrap(client)
        self.assertTrue(status['available'])
        self.assertIsNone(status['paired'])
        self.assertEqual(client.created['Counterparty'], {})


# ── ledger + reports ─────────────────────────────────────────────

# Wells Fargo on both sides: it paid us $50 of interest (AR) and charged $30 of
# fees (AP). Uber is AP only. FUN is AR only.
LEDGER = [
    gl('Customer', 'Wells Fargo', '2026-07-01', debit=50.0),
    gl('Supplier', 'Wells Fargo', '2026-07-02', credit=30.0),
    gl('Supplier', 'Uber', '2026-06-01', credit=120.0),
    gl('Customer', 'FUN', '2026-01-15', debit=900.0),
    # A cancelled entry must never be counted.
    {**gl('Supplier', 'Uber', '2026-06-05', credit=999.0), 'is_cancelled': 1},
]


class LedgerTests(Base):
    def _seeded(self):
        client = self._ready(gl_entries=LEDGER)
        counterparty.find_or_create_counterparty(
            client, 'Wells Fargo', customer='Wells Fargo',
            supplier='Wells Fargo')
        counterparty.find_or_create_counterparty(client, 'Uber',
                                                 supplier='Uber')
        counterparty.find_or_create_counterparty(client, 'FUN', customer='FUN')
        return client

    def test_combined_ledger_merges_both_roles_chronologically(self):
        client = self._seeded()
        cp = counterparty.get_counterparty(client, 'Wells Fargo')
        led = counterparty.combined_ledger(client, cp)
        self.assertEqual([e['posting_date'] for e in led['entries']],
                         [date(2026, 7, 1), date(2026, 7, 2)])
        self.assertEqual([e['role'] for e in led['entries']],
                         ['Customer', 'Supplier'])
        self.assertEqual([e['direction'] for e in led['entries']],
                         ['IN', 'OUT'])

    def test_net_position_nets_the_two_roles(self):
        client = self._seeded()
        cp = counterparty.get_counterparty(client, 'Wells Fargo')
        led = counterparty.combined_ledger(client, cp)
        self.assertEqual(led['ar_balance'], 50.0)
        self.assertEqual(led['ap_balance'], 30.0)
        self.assertEqual(led['net_position'], 20.0)
        self.assertEqual(led['first_date'], date(2026, 7, 1))

    def test_cancelled_entries_are_excluded(self):
        client = self._seeded()
        cp = counterparty.get_counterparty(client, 'Uber')
        led = counterparty.combined_ledger(client, cp)
        self.assertEqual(len(led['entries']), 1)
        self.assertEqual(led['ap_balance'], 120.0)

    def test_aging_buckets_are_assigned_by_posting_date(self):
        client = self._seeded()
        rows = {r['counterparty']: r for r in counterparty.aged_balances(
            client, as_of=date(2026, 7, 20))}
        wf = rows['Wells Fargo']
        # 19 and 18 days old → both in 0-30, netted (AR 50 − AP 30).
        self.assertEqual(wf['buckets']['0-30'], 20.0)
        self.assertEqual(wf['net'], 20.0)
        # Uber is 49 days old → 31-60, and it is a payable so it nets negative.
        self.assertEqual(rows['Uber']['buckets']['31-60'], -120.0)
        # FUN is 186 days old → the open-ended tail.
        self.assertEqual(rows['FUN']['buckets']['120+'], 900.0)

    def test_bucket_boundaries(self):
        self.assertEqual(counterparty.bucket_for(0), '0-30')
        self.assertEqual(counterparty.bucket_for(30), '0-30')
        self.assertEqual(counterparty.bucket_for(31), '31-60')
        self.assertEqual(counterparty.bucket_for(120), '91-120')
        self.assertEqual(counterparty.bucket_for(121), '120+')
        self.assertEqual(counterparty.bucket_for(9999), '120+')

    def test_1099_report_excludes_banks_and_government(self):
        """The report that pays for the whole overlay."""
        client = self._seeded()
        counterparty.find_or_create_counterparty(
            client, 'IRS', supplier='IRS', counterparty_type='Government')
        names = [r['counterparty']
                 for r in counterparty.nec_1099_candidates(client)]
        self.assertIn('Uber', names)
        self.assertNotIn('Wells Fargo', names)   # Financial Institution
        self.assertNotIn('IRS', names)           # Government
        self.assertNotIn('FUN', names)           # customer-only, never a 1099

    def test_1099_report_shows_what_it_excluded(self):
        client = self._seeded()
        excluded = {r['counterparty']: r['counterparty_type']
                    for r in counterparty.excluded_1099_counterparties(client)}
        self.assertEqual(excluded, {'Wells Fargo': 'Financial Institution'})

    def test_top_by_activity_sorts_by_combined_volume(self):
        client = self._seeded()
        rows = counterparty.top_by_activity(client, as_of=date(2026, 7, 20))
        self.assertEqual([r['counterparty'] for r in rows],
                         ['FUN', 'Uber', 'Wells Fargo'])
        wf = next(r for r in rows if r['counterparty'] == 'Wells Fargo')
        self.assertEqual(wf['ar_volume'], 50.0)
        self.assertEqual(wf['ap_volume'], 30.0)
        self.assertEqual(wf['total_volume'], 80.0)

    def test_top_by_activity_is_scoped_to_the_fiscal_year(self):
        """A calendar-year book must not count last year's activity."""
        client = self._seeded()
        rows = counterparty.top_by_activity(client, as_of=date(2026, 7, 20))
        self.assertIn('FUN', [r['counterparty'] for r in rows])
        self.app.config['COUNTERPARTY_FISCAL_YEAR_START_MONTH'] = 6
        rows = counterparty.top_by_activity(client, as_of=date(2026, 7, 20))
        # FUN's only entry is 2026-01-15, before a June fiscal-year start.
        self.assertNotIn('FUN', [r['counterparty'] for r in rows])

    def test_fiscal_year_start_honours_the_configured_month(self):
        self.assertEqual(counterparty.fiscal_year_start(date(2026, 7, 20)),
                         date(2026, 1, 1))
        self.app.config['COUNTERPARTY_FISCAL_YEAR_START_MONTH'] = 9
        self.assertEqual(counterparty.fiscal_year_start(date(2026, 7, 20)),
                         date(2025, 9, 1))
        self.assertEqual(counterparty.fiscal_year_start(date(2026, 10, 2)),
                         date(2026, 9, 1))


# ── rollup ───────────────────────────────────────────────────────

class RollupTests(Base):
    def _seeded(self):
        client = self._ready(gl_entries=LEDGER)
        counterparty.find_or_create_counterparty(
            client, 'Wells Fargo', customer='Wells Fargo',
            supplier='Wells Fargo')
        counterparty.find_or_create_counterparty(client, 'Uber',
                                                 supplier='Uber')
        return client

    def test_rollup_writes_activity_totals_and_first_date(self):
        client = self._seeded()
        result = counterparty.rollup_counterparties(client)
        self.assertEqual(result['updated'], 2)
        self.assertEqual(result['failed'], 0)
        wf = client.created['Counterparty']['Wells Fargo']
        self.assertEqual(wf['total_activity_ar'], 50.0)
        self.assertEqual(wf['total_activity_ap'], 30.0)
        self.assertEqual(wf['date_of_first_transaction'], '2026-07-01')

    def test_rollup_skips_counterparties_whose_numbers_did_not_move(self):
        """The steady state on a farm between seasons is zero writes."""
        client = self._seeded()
        counterparty.rollup_counterparties(client)
        second = counterparty.rollup_counterparties(client)
        self.assertEqual(second['updated'], 0)
        self.assertEqual(second['skipped'], 2)

    def test_rollup_never_blanks_a_first_transaction_date(self):
        """A transient empty read (a Company-scoped run, ERPNext restarting)
        must not erase real history."""
        client = self._seeded()
        counterparty.rollup_counterparties(client)
        client.gl_entries = []
        counterparty.rollup_counterparties(client)
        self.assertEqual(
            client.created['Counterparty']['Wells Fargo'][
                'date_of_first_transaction'], '2026-07-01')

    def test_rollup_reads_the_ledger_once_regardless_of_party_count(self):
        """The scaling decision: GL reads must not grow with counterparties."""
        client = self._seeded()
        for i in range(10):
            counterparty.find_or_create_counterparty(
                client, f'Party {i}', supplier=f'Party {i}')
        before = len(client.calls)
        counterparty.rollup_counterparties(client)
        gl_reads = [c for c in client.calls[before:]
                    if c[0] == 'list_docs' and c[1] == 'GL Entry']
        self.assertEqual(len(gl_reads), 2)   # one per party_type, not per party

    def test_rollup_is_a_no_op_when_the_overlay_is_unavailable(self):
        client = FakeERPClient(doctype_permission_error=True)
        counterparty.ensure_counterparty_doctype(client)
        self.assertEqual(counterparty.rollup_counterparties(client)['scanned'], 0)

    def test_rollup_schedule_is_configurable_and_disablable(self):
        self.assertEqual(scheduler.rollup_interval_or_none(self.app), 24)
        self.app.config['COUNTERPARTY_ROLLUP_INTERVAL_HOURS'] = 6
        self.assertEqual(scheduler.rollup_interval_or_none(self.app), 6)
        self.app.config['COUNTERPARTY_ROLLUP_INTERVAL_HOURS'] = 0
        self.assertIsNone(scheduler.rollup_interval_or_none(self.app))
        self.app.config['COUNTERPARTY_ROLLUP_INTERVAL_HOURS'] = 24
        self.app.config['COUNTERPARTY_OVERLAY_ENABLED'] = False
        self.assertIsNone(scheduler.rollup_interval_or_none(self.app))


# ── admin UI ─────────────────────────────────────────────────────

class AdminUITests(Base):
    def setUp(self):
        super().setUp()
        self.client = self.app.test_client()

    def test_pages_render_without_erpnext(self):
        """Backward compat: a v0.4.4 install with no overlay must not 500."""
        for url in ('/admin/counterparties',
                    '/admin/counterparties/reports/aged',
                    '/admin/counterparties/reports/1099',
                    '/admin/counterparties/reports/top'):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
        body = self.client.get('/admin/counterparties').get_data(as_text=True)
        self.assertIn("overlay isn't provisioned", body)

    def test_json_endpoints_render_without_erpnext(self):
        for url in ('/api/counterparties/reports/aged',
                    '/api/counterparties/reports/1099',
                    '/api/counterparties/reports/top'):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertEqual(r.get_json()['rows'], [])

    def test_1099_json_declares_the_excluded_types(self):
        payload = self.client.get(
            '/api/counterparties/reports/1099').get_json()
        self.assertEqual(payload['excluded_types'],
                         ['Financial Institution', 'Government'])

    def test_unknown_counterparty_redirects_instead_of_500ing(self):
        r = self.client.get('/admin/counterparties/Nobody')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/admin/counterparties', r.headers['Location'])

    def test_navbar_links_the_new_page(self):
        body = self.client.get('/admin').get_data(as_text=True)
        self.assertIn('/admin/counterparties', body)

    def test_report_routes_beat_the_detail_catch_all(self):
        """`/admin/counterparties/reports/aged` must not be read as a
        counterparty named 'reports/aged'."""
        r = self.client.get('/admin/counterparties/reports/aged')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Aged counterparty balances', r.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
