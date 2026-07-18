# SPDX-License-Identifier: MIT
"""Counterparty overlay — one semantic identity over ERPNext's Customer +
Supplier split (v0.4.5).

THE PROBLEM. ERPNext keeps the buy side and the sell side in two unrelated
doctypes. A party that trades with you in both directions — a bank, a
wholesaler who buys your fruit and sells you bins, a neighbouring grower you
swap labour with — becomes two records with no link between them. Nothing in
ERPNext can answer "what is my net position with Wells Fargo?", and the 1099
vendor list is just "every Supplier", which happily includes your bank and the
IRS. Odoo and Dolibarr solve this with a unified Partner; migrating off ERPNext
to get it is not a trade worth making.

THE SHAPE. A `Counterparty` doctype sits ABOVE Customer and Supplier and links
0-or-1 of each. It owns no accounting: every debit and credit still posts to the
underlying Customer / Supplier exactly as before, so the audit trail, the AR/AP
ageing ERPNext already does, and every existing report are untouched. The
overlay only adds identity — and the queries that identity makes possible.

WHY REST-CREATED, NOT A FRAPPE APP. The doctype is provisioned through the same
idempotent REST bootstrap this bridge already uses for Bank Account Types,
Account Subtypes and custom fields (see erpnext_accounts.bootstrap). A separate
Frappe app would mean a second deployable, a second upgrade path and a bench
install on Tim's box for one doctype. `custom: 1` keeps it a user doctype —
stored in the database, invisible to `bench migrate`, removable by deleting it.

FAIL-SAFE BY CONSTRUCTION. Every entry point degrades to a no-op when the
doctype can't be provisioned (an API user without DocType create rights is the
likely case). The overlay is a reporting convenience; nothing in the posting
path may ever fail because of it. Auto-link swallows its own errors, and the
unavailable-doctype registry short-circuits repeat attempts for the rest of the
process's life.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from flask import current_app

from .erpnext_accounts import (_mark_doctype_available, _mark_doctype_unavailable,
                               is_doctype_unavailable, _is_missing_doctype_error)
from .erpnext_client import ERPNextAPIError, ERPNextClient, ERPNextError

log = logging.getLogger('bankbridge.counterparty')

COUNTERPARTY_DT = 'Counterparty'
DOCTYPE_DT = 'DocType'
CUSTOMER_DT = 'Customer'
SUPPLIER_DT = 'Supplier'
GL_ENTRY_DT = 'GL Entry'

# The module the custom doctype is filed under. 'Accounts' is stock ERPNext, so
# the Counterparty list shows up beside Customer and Supplier in the desk.
COUNTERPARTY_MODULE = 'Accounts'

COUNTERPARTY_TYPES = ('Individual', 'Company', 'Financial Institution',
                      'Government', 'Other')
DEFAULT_COUNTERPARTY_TYPE = 'Company'

# Counterparty types that must NEVER appear on a 1099-NEC. Paying a bank
# interest or the IRS a tax bill is not non-employee compensation, and issuing
# either a 1099 is the classic bookkeeping own-goal this overlay exists to
# prevent. See nec_1099_candidates.
NON_1099_TYPES = frozenset({'Financial Institution', 'Government'})

# Ageing buckets, in days, as (label, lower_bound_inclusive, upper_bound_inclusive).
# An upper bound of None is the open-ended tail.
AGING_BUCKETS = (
    ('0-30', 0, 30),
    ('31-60', 31, 60),
    ('61-90', 61, 90),
    ('91-120', 91, 120),
    ('120+', 121, None),
)


# ── doctype bootstrap ───────────────────────────────────────────────────────

def _field(fieldname, fieldtype, label, **extra) -> dict:
    """One DocField spec. Frappe wants an explicit `label` on every field —
    it does not reliably title-case the fieldname for a doctype created over
    REST, and a label-less field renders blank in the desk."""
    spec = {'fieldname': fieldname, 'fieldtype': fieldtype, 'label': label}
    spec.update(extra)
    return spec


def counterparty_doctype_spec() -> dict:
    """The full DocType document we POST to create the overlay.

    Deliberately flat: no child tables, no naming series, no workflow. The
    autoname is `field:counterparty_name`, so the docname IS the party name —
    which makes the Customer/Supplier pairing a name equality check rather than
    a lookup table, and makes the record trivially findable by anyone poking at
    ERPNext directly.

    Note there is no `unique: 1` on counterparty_name even though the design
    calls for uniqueness: with `autoname: field:…` the value becomes the primary
    key, so Frappe already enforces uniqueness at the database level, and adding
    the redundant unique index makes some Frappe versions reject the create."""
    return {
        'doctype': DOCTYPE_DT,
        'name': COUNTERPARTY_DT,
        'module': COUNTERPARTY_MODULE,
        'custom': 1,
        'naming_rule': 'By fieldname',
        'autoname': 'field:counterparty_name',
        'track_changes': 1,
        'fields': [
            _field('counterparty_name', 'Data', 'Counterparty Name',
                   reqd=1, in_list_view=1),
            _field('counterparty_type', 'Select', 'Counterparty Type',
                   options='\n'.join(COUNTERPARTY_TYPES), reqd=1,
                   in_list_view=1, default=DEFAULT_COUNTERPARTY_TYPE),
            _field('customer_link', 'Link', 'Customer', options=CUSTOMER_DT,
                   in_list_view=1),
            _field('supplier_link', 'Link', 'Supplier', options=SUPPLIER_DT,
                   in_list_view=1),
            _field('dual_role_flag', 'Check', 'Dual Role', read_only=1),
            _field('date_of_first_transaction', 'Date',
                   'Date of First Transaction', read_only=1),
            _field('total_activity_ar', 'Currency', 'Total Activity (AR)',
                   read_only=1),
            _field('total_activity_ap', 'Currency', 'Total Activity (AP)',
                   read_only=1),
            _field('notes', 'Small Text', 'Notes'),
        ],
        'permissions': [
            {'role': 'System Manager', 'read': 1, 'write': 1, 'create': 1,
             'delete': 1, 'report': 1, 'export': 1},
            {'role': 'Accounts Manager', 'read': 1, 'write': 1, 'create': 1,
             'report': 1, 'export': 1},
            {'role': 'Accounts User', 'read': 1, 'report': 1},
        ],
    }


def _is_permission_error(e: ERPNextAPIError) -> bool:
    """True when Frappe refused the write for lack of rights rather than
    because the request was malformed. An API key minted for a user without
    DocType create permission is the realistic failure here, and it must be
    handled as 'overlay unavailable', not as a crash."""
    if e.status_code in (401, 403):
        return True
    blob = ((e.response_body or '') + ' ' + str(e)).lower()
    return 'permissionerror' in blob or 'not permitted' in blob


def is_enabled() -> bool:
    """Master switch. Off → every entry point in this module is a no-op, and
    an install that wants nothing to do with the overlay pays no ERPNext calls
    for it."""
    return bool(current_app.config.get('COUNTERPARTY_OVERLAY_ENABLED', True))


# The outcomes provision_report can return in its 'state' field. Stable strings
# — the startup job, the management CLI and the tests all key off them.
PROVISION_CREATED = 'created'
PROVISION_PRESENT = 'already_present'
PROVISION_NO_CLIENT = 'not_configured'
PROVISION_PERMISSION = 'permission_denied'
PROVISION_NO_DOCTYPE_API = 'doctype_api_unavailable'
PROVISION_PROBE_FAILED = 'probe_failed'
PROVISION_CREATE_FAILED = 'create_failed'

# Operator-facing explanation per failure state. The whole point of v0.4.6's
# change here is that a refusal says WHY in the boot log, rather than leaving an
# install quietly without the feature (which is what happened on the v0.4.5
# upgrade path — see provision_report).
PROVISION_HELP = {
    PROVISION_PERMISSION:
        'the ERPNext API user lacks create permission on DocType. Give it the '
        'System Manager role (or create the Counterparty doctype by hand) and '
        'restart.',
    PROVISION_NO_DOCTYPE_API:
        "this ERPNext does not expose the DocType API, so the overlay cannot "
        'provision itself. Usually a locked-down or very old Frappe.',
    PROVISION_PROBE_FAILED:
        'the doctype could not be probed. Check the ERPNext URL and '
        'credentials on /admin/erpnext_settings, then restart.',
    PROVISION_CREATE_FAILED:
        'ERPNext rejected the doctype create. The reason it gave is in the '
        'message above.',
    PROVISION_NO_CLIENT:
        'ERPNext is not configured yet. Set it up on /admin/erpnext_settings; '
        'the overlay provisions itself on the next restart.',
}


def provision_report(client: ERPNextClient | None) -> dict:
    """Idempotently provision the Counterparty doctype and REPORT what happened:
    `{'ok': bool, 'state': str, 'reason': str}`.

    Same work `ensure_counterparty_doctype` has always done — this is the
    structured form of it, so the startup job and the management CLI can tell an
    operator *which* of "created it", "it was already there" and "ERPNext said
    no, because X" actually occurred. v0.4.5 only returned a bool, which is why
    an install that never provisioned looked identical to one that did.

    Safe to call on every startup: the happy path on an already-provisioned
    instance is a single GET that 200s. A 404 on that GET is the one signal
    that means "not created yet" — a genuinely absent DOCUMENT of doctype
    DocType, which is different from the absent-MODULE 500 that
    `_is_missing_doctype_error` detects. Both are handled.

    Never raises. A refusal is recorded in the shared unavailable-doctype
    registry so the auto-link path short-circuits without re-probing."""
    if client is None:
        return {'ok': False, 'state': PROVISION_NO_CLIENT, 'reason': ''}
    try:
        existing = client.get_doc(DOCTYPE_DT, COUNTERPARTY_DT)
    except ERPNextAPIError as e:
        detail = str(e)[:300]
        if _is_permission_error(e):
            state = PROVISION_PERMISSION
        elif _is_missing_doctype_error(e):
            state = PROVISION_NO_DOCTYPE_API
        else:
            state = PROVISION_PROBE_FAILED
        log.warning('Counterparty doctype NOT provisioned (%s) — %s [%s]',
                    state, PROVISION_HELP.get(state, ''), detail)
        _mark_doctype_unavailable(COUNTERPARTY_DT)
        return {'ok': False, 'state': state, 'reason': detail}
    if existing is not None:
        # v0.4.6 · say so explicitly. Silence here is what made a never-created
        # doctype indistinguishable from a healthy one in the boot log.
        log.info('Counterparty doctype already present — nothing to provision')
        _mark_doctype_available(COUNTERPARTY_DT)
        return {'ok': True, 'state': PROVISION_PRESENT, 'reason': ''}
    log.info('Counterparty doctype absent — creating it')
    try:
        client.create_doc(DOCTYPE_DT, counterparty_doctype_spec())
    except (ERPNextAPIError, ERPNextError) as e:
        # A concurrent worker winning the race is a success, not a failure:
        # re-probe rather than trusting the error text.
        try:
            if client.get_doc(DOCTYPE_DT, COUNTERPARTY_DT) is not None:
                log.info('Counterparty doctype created concurrently by another '
                         'worker — treating as provisioned')
                _mark_doctype_available(COUNTERPARTY_DT)
                return {'ok': True, 'state': PROVISION_PRESENT, 'reason': ''}
        except (ERPNextAPIError, ERPNextError):
            pass
        detail = str(e)[:300]
        state = (PROVISION_PERMISSION
                 if isinstance(e, ERPNextAPIError) and _is_permission_error(e)
                 else PROVISION_CREATE_FAILED)
        log.warning('Counterparty doctype NOT provisioned (%s) — %s [%s]',
                    state, PROVISION_HELP.get(state, ''), detail)
        _mark_doctype_unavailable(COUNTERPARTY_DT)
        return {'ok': False, 'state': state, 'reason': detail}
    log.info('provisioned the %s doctype', COUNTERPARTY_DT)
    _mark_doctype_available(COUNTERPARTY_DT)
    return {'ok': True, 'state': PROVISION_CREATED, 'reason': ''}


def ensure_counterparty_doctype(client: ERPNextClient) -> bool:
    """Idempotently provision the Counterparty doctype. Returns True when it is
    available (already present or just created), False when this ERPNext won't
    give it to us. The boolean face of `provision_report`, kept for the callers
    that only need the gate."""
    return provision_report(client)['ok']


def available(client: ERPNextClient | None) -> bool:
    """Cheap gate for the hot paths: is the overlay usable right now? Consults
    the master switch and the process-local unavailable registry WITHOUT an
    ERPNext round-trip — the doctype bootstrap is what populates the registry."""
    if client is None or not is_enabled():
        return False
    return not is_doctype_unavailable(COUNTERPARTY_DT)


# ── type inference ──────────────────────────────────────────────────────────

def counterparty_type_for(party_name: str, *, source: str = '') -> str:
    """Best-guess `counterparty_type` for a newly-minted Counterparty.

    Reuses the v0.4.0.8 dual-role heuristic verbatim rather than inventing a
    second name-matching rule: the exact signals that say "this party both buys
    and sells" (a bank keyword, a known institution name, a party derived from a
    linked Plaid Item) are the signals that say "this is a Financial
    Institution". Keeping one heuristic means a config override tuned for
    dual-role behaviour also fixes the 1099 report, which is what an operator
    would expect.

    Everything else defaults to Company. Individual / Government / Other are
    offered in the dropdown for a human to set — guessing a sole proprietor from
    a bank memo line is not something a heuristic can do honestly, and guessing
    wrong here would silently drop a real contractor off the 1099 list."""
    from . import erpnext_bank
    if erpnext_bank.is_dual_role_party(party_name, source=source):
        return 'Financial Institution'
    return DEFAULT_COUNTERPARTY_TYPE


# ── find-or-create + linking ────────────────────────────────────────────────

def get_counterparty(client: ERPNextClient, name: str) -> dict | None:
    """The Counterparty document for `name`, or None. Tolerates every ERPNext
    error by answering None — a reporting overlay must not raise into a caller
    that is trying to post a Journal Entry."""
    try:
        return client.get_doc(COUNTERPARTY_DT, name)
    except (ERPNextAPIError, ERPNextError):
        return None


def _link_updates(doc: dict, customer: str, supplier: str) -> dict:
    """The subset of {customer_link, supplier_link, dual_role_flag} that would
    actually CHANGE `doc`. Empty when the document is already correct, which is
    what makes the auto-link idempotent and keeps a re-sync from issuing a PUT
    per transaction.

    A link is only ever ADDED, never cleared: if an operator has hand-corrected
    a Counterparty to point at a differently-named Customer, a later auto-link
    for the same party must not stomp that. It fills blanks, nothing else."""
    updates = {}
    if customer and not (doc.get('customer_link') or '').strip():
        updates['customer_link'] = customer
    if supplier and not (doc.get('supplier_link') or '').strip():
        updates['supplier_link'] = supplier
    eff_customer = updates.get('customer_link') or (doc.get('customer_link') or '')
    eff_supplier = updates.get('supplier_link') or (doc.get('supplier_link') or '')
    dual = 1 if (eff_customer.strip() and eff_supplier.strip()) else 0
    if int(doc.get('dual_role_flag') or 0) != dual:
        updates['dual_role_flag'] = dual
    return updates


def find_or_create_counterparty(client: ERPNextClient | None, party_name: str, *,
                                customer: str = '', supplier: str = '',
                                source: str = '',
                                counterparty_type: str = '') -> str | None:
    """Find-or-create the Counterparty for `party_name` and make sure it links
    whichever of `customer` / `supplier` were supplied. Returns its docname, or
    None when the overlay is unavailable or the write failed.

    Idempotent three ways over: the fetch short-circuits an existing record, the
    update is diffed so an already-correct document costs no PUT, and a create
    that loses a race to a concurrent sync falls back to fetching the winner's
    document rather than failing.

    `dual_role_flag` is derived here — it is exactly "both links are populated" —
    so it can never drift from the links it summarises."""
    name = (party_name or '').strip()
    if not name or not available(client):
        return None
    doc = get_counterparty(client, name)
    if doc is None:
        payload = {
            'counterparty_name': name,
            'counterparty_type': (counterparty_type
                                  or counterparty_type_for(name, source=source)),
        }
        if customer:
            payload['customer_link'] = customer
        if supplier:
            payload['supplier_link'] = supplier
        payload['dual_role_flag'] = 1 if (customer and supplier) else 0
        try:
            created = client.create_doc(COUNTERPARTY_DT, payload)
            return (created or {}).get('name') or name
        except (ERPNextAPIError, ERPNextError) as e:
            # Lost a race, or the create was rejected. Re-fetch: if a concurrent
            # worker created it, fall through to the update path and link onto
            # THEIR document instead of reporting a failure.
            doc = get_counterparty(client, name)
            if doc is None:
                log.warning('could not create Counterparty %r: %s', name,
                            str(e)[:300])
                return None
    updates = _link_updates(doc, customer, supplier)
    if not updates:
        return doc.get('name') or name
    try:
        client.update_doc(COUNTERPARTY_DT, doc.get('name') or name, updates)
    except (ERPNextAPIError, ERPNextError) as e:
        log.warning('could not update Counterparty %r links: %s', name,
                    str(e)[:300])
        return doc.get('name') or name
    return doc.get('name') or name


def link_party(client: ERPNextClient | None, party_name: str, party_type: str,
               erpnext_name: str, *, source: str = '') -> str | None:
    """Auto-link entry point for the party-creation paths: record that
    `erpnext_name` (a Customer or a Supplier docname) belongs to the
    Counterparty for `party_name`.

    Called from erpnext_bank.ensure_supplier / ensure_customer immediately after
    a party resolves. NEVER raises and never returns a value the caller must
    check — the party record is what the Journal Entry needs, and an overlay
    that can't keep up must not cost us one."""
    if not available(client) or not erpnext_name:
        return None
    kwargs = ({'customer': erpnext_name} if party_type == CUSTOMER_DT
              else {'supplier': erpnext_name})
    try:
        return find_or_create_counterparty(client, party_name, source=source,
                                           **kwargs)
    except Exception:  # pragma: no cover - the overlay may never break a push
        log.warning('counterparty auto-link failed for %r (%s)', party_name,
                    party_type, exc_info=True)
        return None


def list_counterparties(client: ERPNextClient | None, *, search: str = '',
                        counterparty_type: str = '') -> list:
    """Every Counterparty, optionally filtered by a name substring and/or type.
    Returns [] (never raises) when the overlay is unavailable, so the admin page
    renders an empty state instead of a 500 on an un-provisioned install."""
    if not available(client):
        return []
    filters = []
    if search.strip():
        filters.append(['counterparty_name', 'like', f'%{search.strip()}%'])
    if counterparty_type.strip():
        filters.append(['counterparty_type', '=', counterparty_type.strip()])
    try:
        return client.list_docs(
            COUNTERPARTY_DT, filters=filters or None,
            fields=['name', 'counterparty_name', 'counterparty_type',
                    'customer_link', 'supplier_link', 'dual_role_flag',
                    'date_of_first_transaction', 'total_activity_ar',
                    'total_activity_ap'],
            order_by='counterparty_name asc')
    except (ERPNextAPIError, ERPNextError) as e:
        log.warning('could not list Counterparties: %s', str(e)[:300])
        return []


# ── GL activity ─────────────────────────────────────────────────────────────
#
# Everything below reads GL Entry, not Sales/Purchase Invoice. GL Entry is where
# EVERY posting lands whatever produced it, and Bank Bridge's own output is
# Journal Entries, which never touch an invoice. Reading invoices would show a
# farm that posts JEs an empty ledger.

GL_FIELDS = ['posting_date', 'account', 'debit', 'credit', 'voucher_type',
             'voucher_no', 'party_type', 'party', 'company', 'remarks']


def _num(v) -> float:
    try:
        return round(float(v or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def party_gl_entries(client: ERPNextClient | None, party_type: str,
                     party: str, *, company: str = '') -> list:
    """Every non-cancelled GL Entry booked against one party, oldest first."""
    if client is None or not party or party_type not in (CUSTOMER_DT, SUPPLIER_DT):
        return []
    filters = [['party_type', '=', party_type], ['party', '=', party],
               ['is_cancelled', '=', 0]]
    if company.strip():
        filters.append(['company', '=', company.strip()])
    try:
        return client.list_docs(GL_ENTRY_DT, filters=filters, fields=GL_FIELDS,
                                order_by='posting_date asc')
    except (ERPNextAPIError, ERPNextError) as e:
        log.warning('could not read GL entries for %s %r: %s', party_type,
                    party, str(e)[:300])
        return []


def all_party_gl_rows(client: ERPNextClient | None, *, company: str = '') -> list:
    """Every non-cancelled party-bearing GL Entry in ONE call.

    This is the scaling decision that makes the daily rollup viable. The obvious
    implementation — loop the Counterparties, two GL queries each — is 2N
    ERPNext round-trips and gets slower every time a party is added. Pulling the
    party-bearing slice of the ledger once and aggregating it in Python is a
    fixed two-ish requests regardless of how many Counterparties exist, and the
    slice is small: only AR/AP lines carry a party at all.

    The tradeoff is memory proportional to ledger size rather than to party
    count. For a farm's ledger that is thousands of rows, not millions; if it
    ever stops being true, the fix is a date floor on this query, not a return
    to per-party fanout."""
    if client is None:
        return []
    rows = []
    for ptype in (CUSTOMER_DT, SUPPLIER_DT):
        filters = [['party_type', '=', ptype], ['is_cancelled', '=', 0]]
        if company.strip():
            filters.append(['company', '=', company.strip()])
        try:
            rows.extend(client.list_docs(GL_ENTRY_DT, filters=filters,
                                         fields=GL_FIELDS,
                                         order_by='posting_date asc'))
        except (ERPNextAPIError, ERPNextError) as e:
            log.warning('could not read %s GL entries: %s', ptype, str(e)[:300])
    return rows


def _as_date(value):
    """Frappe hands dates back as 'YYYY-MM-DD' strings; tests and callers may
    pass real dates. Returns a `date` or None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = (str(value or '')).strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, '%Y-%m-%d').date()
    except ValueError:
        return None


def entry_amount(row: dict, party_type: str) -> float:
    """The signed amount of one GL row from the party's perspective.

    AR (Customer): a debit increases what they owe you, so debit - credit.
    AP (Supplier): a credit increases what you owe them, so credit - debit.

    Both therefore read positive when the party's balance grows in its natural
    direction, which is what lets AR and AP be compared and netted."""
    if party_type == CUSTOMER_DT:
        return round(_num(row.get('debit')) - _num(row.get('credit')), 2)
    return round(_num(row.get('credit')) - _num(row.get('debit')), 2)


def aggregate_by_party(rows: list) -> dict:
    """Fold GL rows into {(party_type, party): {balance, volume, first_date}}.

    `balance` is the signed net position (what the rollup and the ageing report
    want); `volume` is the sum of ABSOLUTE movement (what "top counterparties by
    activity" wants — a party you invoiced and were paid by in full has a zero
    balance but plenty of activity)."""
    out = {}
    for row in rows:
        ptype = (row.get('party_type') or '').strip()
        party = (row.get('party') or '').strip()
        if not party or ptype not in (CUSTOMER_DT, SUPPLIER_DT):
            continue
        key = (ptype, party)
        bucket = out.setdefault(key, {'balance': 0.0, 'volume': 0.0,
                                      'first_date': None, 'count': 0})
        amount = entry_amount(row, ptype)
        bucket['balance'] = round(bucket['balance'] + amount, 2)
        bucket['volume'] = round(
            bucket['volume'] + abs(_num(row.get('debit')))
            + abs(_num(row.get('credit'))), 2)
        bucket['count'] += 1
        d = _as_date(row.get('posting_date'))
        if d and (bucket['first_date'] is None or d < bucket['first_date']):
            bucket['first_date'] = d
    return out


# ── unified ledger (the detail page) ────────────────────────────────────────

def combined_ledger(client: ERPNextClient | None, counterparty: dict, *,
                    company: str = '') -> dict:
    """The chronological AR + AP ledger for one Counterparty, plus its net
    position — the thing ERPNext structurally cannot show you.

    Each returned entry carries `role` (Customer / Supplier) and `direction`
    (IN when the money moves toward you, OUT when it moves away), so a reader
    can see at a glance that the same name is on both sides. `voucher_type` /
    `voucher_no` are kept so the UI can drill through to the real ERPNext
    document."""
    customer = (counterparty.get('customer_link') or '').strip()
    supplier = (counterparty.get('supplier_link') or '').strip()
    entries = []
    for ptype, party in ((CUSTOMER_DT, customer), (SUPPLIER_DT, supplier)):
        if not party:
            continue
        for row in party_gl_entries(client, ptype, party, company=company):
            amount = entry_amount(row, ptype)
            entries.append({
                'posting_date': _as_date(row.get('posting_date')),
                'role': ptype,
                'party': party,
                'account': row.get('account') or '',
                'debit': _num(row.get('debit')),
                'credit': _num(row.get('credit')),
                'amount': amount,
                # Direction is ROLE-derived, deliberately. A single GL line
                # cannot tell an accrual from a settlement — a debit to a
                # Payable is "you paid the bill", a credit is "you incurred
                # it", and both are money leaving the business, just at
                # different moments. Guessing cash timing from the sign would
                # label a supplier payment "IN" half the time. So we report the
                # direction the ROLE always implies (a Customer is a source of
                # money, a Supplier is a use of it) and let the amount carry
                # the magnitude.
                'direction': 'IN' if ptype == CUSTOMER_DT else 'OUT',
                'voucher_type': row.get('voucher_type') or '',
                'voucher_no': row.get('voucher_no') or '',
                'company': row.get('company') or '',
                'remarks': row.get('remarks') or '',
            })
    entries.sort(key=lambda e: (e['posting_date'] or date.min,
                                e['voucher_no'] or ''))
    ar = round(sum(e['amount'] for e in entries if e['role'] == CUSTOMER_DT), 2)
    ap = round(sum(e['amount'] for e in entries if e['role'] == SUPPLIER_DT), 2)
    return {
        'entries': entries,
        'ar_balance': ar,
        'ap_balance': ap,
        # What they owe you minus what you owe them. Positive = they are net
        # in your debt.
        'net_position': round(ar - ap, 2),
        'first_date': min((e['posting_date'] for e in entries
                           if e['posting_date']), default=None),
    }


# ── rollup ──────────────────────────────────────────────────────────────────

def _fmt_date(d) -> str | None:
    return d.isoformat() if isinstance(d, date) else None


def rollup_counterparties(client: ERPNextClient | None, *,
                          company: str = '') -> dict:
    """Refresh `total_activity_ar`, `total_activity_ap` and
    `date_of_first_transaction` on every Counterparty from the live ledger.

    One ledger read (see all_party_gl_rows), then one PUT per Counterparty whose
    numbers actually MOVED. On a farm where most parties are dormant between
    seasons the steady state is zero writes, which is what keeps a daily job on
    a Raspberry-Pi-class box uneventful.

    Returns {'scanned', 'updated', 'skipped', 'failed'}. Never raises."""
    result = {'scanned': 0, 'updated': 0, 'skipped': 0, 'failed': 0}
    if not available(client):
        return result
    rows = all_party_gl_rows(client, company=company)
    agg = aggregate_by_party(rows)
    for cp in list_counterparties(client):
        result['scanned'] += 1
        customer = (cp.get('customer_link') or '').strip()
        supplier = (cp.get('supplier_link') or '').strip()
        ar = agg.get((CUSTOMER_DT, customer)) if customer else None
        ap = agg.get((SUPPLIER_DT, supplier)) if supplier else None
        first_dates = [b['first_date'] for b in (ar, ap)
                       if b and b['first_date']]
        wanted = {
            'total_activity_ar': (ar or {}).get('volume', 0.0),
            'total_activity_ap': (ap or {}).get('volume', 0.0),
            'date_of_first_transaction': _fmt_date(min(first_dates))
            if first_dates else None,
        }
        current = {
            'total_activity_ar': _num(cp.get('total_activity_ar')),
            'total_activity_ap': _num(cp.get('total_activity_ap')),
            'date_of_first_transaction': (
                _fmt_date(_as_date(cp.get('date_of_first_transaction')))),
        }
        changed = {k: v for k, v in wanted.items() if current.get(k) != v}
        # Never blank a first-transaction date we already hold: a rollup scoped
        # to one Company, or run while ERPNext is mid-restart, would otherwise
        # erase real history on a transient empty read.
        if changed.get('date_of_first_transaction') is None:
            changed.pop('date_of_first_transaction', None)
        if not changed:
            result['skipped'] += 1
            continue
        try:
            client.update_doc(COUNTERPARTY_DT, cp.get('name'), changed)
            result['updated'] += 1
        except (ERPNextAPIError, ERPNextError) as e:
            result['failed'] += 1
            log.warning('rollup failed for Counterparty %r: %s',
                        cp.get('name'), str(e)[:200])
    log.info('[counterparty] rollup: %s', result)
    return result


# ── reports ─────────────────────────────────────────────────────────────────

def bucket_for(age_days: int) -> str:
    """The ageing bucket label an entry of `age_days` falls in."""
    for label, low, high in AGING_BUCKETS:
        if age_days >= low and (high is None or age_days <= high):
            return label
    return AGING_BUCKETS[0][0]


def aged_balances(client: ERPNextClient | None, *, company: str = '',
                  as_of: date | None = None) -> list:
    """Combined AR + AP ageing per Counterparty.

    IMPORTANT — what is being aged. ERPNext's own ageing reports age an
    INVOICE by its due date. This ages a GL ENTRY by its posting date, because
    Bank Bridge posts Journal Entries, which have no due date and no invoice
    behind them. For a farm reconciling bank activity that is the meaningful
    question ("how old is this money?"); it is NOT a drop-in replacement for
    ERPNext's Accounts Receivable Summary, and the two will disagree on any
    party you also invoice properly. That is by design, and the admin page says
    so.

    `net` is AR minus AP, so a bank you owe fees to and that owes you interest
    nets out to a single honest number instead of two half-truths."""
    today = as_of or date.today()
    rows = all_party_gl_rows(client, company=company)
    by_party = {}
    for row in rows:
        ptype = (row.get('party_type') or '').strip()
        party = (row.get('party') or '').strip()
        if not party or ptype not in (CUSTOMER_DT, SUPPLIER_DT):
            continue
        by_party.setdefault((ptype, party), []).append(row)
    out = []
    for cp in list_counterparties(client):
        customer = (cp.get('customer_link') or '').strip()
        supplier = (cp.get('supplier_link') or '').strip()
        buckets = {label: 0.0 for label, _, _ in AGING_BUCKETS}
        ar = ap = 0.0
        for ptype, party in ((CUSTOMER_DT, customer), (SUPPLIER_DT, supplier)):
            if not party:
                continue
            for row in by_party.get((ptype, party), []):
                amount = entry_amount(row, ptype)
                if ptype == CUSTOMER_DT:
                    ar = round(ar + amount, 2)
                else:
                    ap = round(ap + amount, 2)
                d = _as_date(row.get('posting_date'))
                age = (today - d).days if d else 0
                # AP is subtracted so each bucket is a NET figure, matching the
                # `net` total. A bucket holding an equal payable and receivable
                # correctly reads zero.
                signed = amount if ptype == CUSTOMER_DT else -amount
                label = bucket_for(max(0, age))
                buckets[label] = round(buckets[label] + signed, 2)
        if not customer and not supplier:
            continue
        out.append({
            'counterparty': cp.get('name'),
            'counterparty_type': cp.get('counterparty_type') or '',
            'customer_link': customer,
            'supplier_link': supplier,
            'dual_role': bool(int(cp.get('dual_role_flag') or 0)),
            'ar_balance': ar,
            'ap_balance': ap,
            'net': round(ar - ap, 2),
            'buckets': buckets,
        })
    out.sort(key=lambda r: abs(r['net']), reverse=True)
    return out


def nec_1099_candidates(client: ERPNextClient | None) -> list:
    """Counterparties that could legitimately receive a 1099-NEC: they have a
    Supplier link (you paid them) and their type is neither Financial
    Institution nor Government.

    This is the report that pays for the whole overlay. "Every Supplier" is the
    list ERPNext can give you, and it contains your bank, your card issuer and
    the tax authority — all of which are wrong to 1099 and at least one of which
    someone does every January. Typing the party once, at the moment it is
    created, is what makes the January list correct.

    It is a CANDIDATE list, not a filing: the $600 threshold, W-9 status and
    corporate-vs-individual exemptions are judgement calls a human makes. The
    payment total is included so that judgement has a number to work from."""
    out = []
    for cp in list_counterparties(client):
        supplier = (cp.get('supplier_link') or '').strip()
        if not supplier:
            continue
        ctype = (cp.get('counterparty_type') or '').strip()
        if ctype in NON_1099_TYPES:
            continue
        out.append({
            'counterparty': cp.get('name'),
            'counterparty_type': ctype,
            'supplier_link': supplier,
            'customer_link': (cp.get('customer_link') or '').strip(),
            'dual_role': bool(int(cp.get('dual_role_flag') or 0)),
            'total_activity_ap': _num(cp.get('total_activity_ap')),
        })
    out.sort(key=lambda r: r['total_activity_ap'], reverse=True)
    return out


def excluded_1099_counterparties(client: ERPNextClient | None) -> list:
    """The Suppliers the 1099 report deliberately left out, with the type that
    excluded them. Shown alongside the candidate list because a report that
    silently drops rows is one an operator can't trust — seeing 'Wells Fargo —
    excluded, Financial Institution' is the proof the filter did its job."""
    out = []
    for cp in list_counterparties(client):
        supplier = (cp.get('supplier_link') or '').strip()
        ctype = (cp.get('counterparty_type') or '').strip()
        if supplier and ctype in NON_1099_TYPES:
            out.append({'counterparty': cp.get('name'),
                        'counterparty_type': ctype,
                        'supplier_link': supplier})
    out.sort(key=lambda r: r['counterparty'] or '')
    return out


def fiscal_year_start(as_of: date | None = None) -> date:
    """Start of the fiscal year the report covers. Calendar-year by default;
    override with COUNTERPARTY_FISCAL_YEAR_START_MONTH for a farm whose books
    run on a crop year rather than January."""
    today = as_of or date.today()
    try:
        month = int(current_app.config.get(
            'COUNTERPARTY_FISCAL_YEAR_START_MONTH', 1) or 1)
    except (TypeError, ValueError):
        month = 1
    month = min(12, max(1, month))
    year = today.year if today.month >= month else today.year - 1
    return date(year, month, 1)


def top_by_activity(client: ERPNextClient | None, *, company: str = '',
                    limit: int = 50, as_of: date | None = None) -> list:
    """Counterparties ranked by combined AR + AP volume for the current fiscal
    year.

    Ranked on VOLUME (absolute movement), not balance: the party you do the most
    business with is the one worth knowing about, and a customer who always pays
    on time would rank last on any balance-based measure."""
    start = fiscal_year_start(as_of)
    end = as_of or date.today()
    rows = [r for r in all_party_gl_rows(client, company=company)
            if (lambda d: d is not None and start <= d <= end)(
                _as_date(r.get('posting_date')))]
    agg = aggregate_by_party(rows)
    out = []
    for cp in list_counterparties(client):
        customer = (cp.get('customer_link') or '').strip()
        supplier = (cp.get('supplier_link') or '').strip()
        ar = agg.get((CUSTOMER_DT, customer), {}) if customer else {}
        ap = agg.get((SUPPLIER_DT, supplier), {}) if supplier else {}
        ar_vol = _num(ar.get('volume'))
        ap_vol = _num(ap.get('volume'))
        total = round(ar_vol + ap_vol, 2)
        if not total:
            continue
        out.append({
            'counterparty': cp.get('name'),
            'counterparty_type': cp.get('counterparty_type') or '',
            'dual_role': bool(int(cp.get('dual_role_flag') or 0)),
            'ar_volume': ar_vol,
            'ap_volume': ap_vol,
            'total_volume': total,
            'entry_count': int(ar.get('count', 0)) + int(ap.get('count', 0)),
        })
    out.sort(key=lambda r: r['total_volume'], reverse=True)
    return out[:max(1, limit)]


# ── pairing migration (shared by the script and the startup pass) ───────────

def _enabled_party_names(client: ERPNextClient, doctype: str,
                         name_field: str) -> list:
    """Every non-disabled party of `doctype`, as (docname, party_name) pairs.
    Disabled records are skipped: pairing them would resurrect a party the
    operator has deliberately retired."""
    try:
        rows = client.list_docs(doctype, filters=[['disabled', '=', 0]],
                                fields=['name', name_field],
                                order_by='name asc')
    except (ERPNextAPIError, ERPNextError) as e:
        log.warning('could not list %ss for pairing: %s', doctype, str(e)[:300])
        return []
    return [(r.get('name'), (r.get(name_field) or r.get('name') or '').strip())
            for r in rows if r.get('name')]


def pair_existing_parties(client: ERPNextClient | None, *,
                          dry_run: bool = False) -> dict:
    """Create the Counterparty overlay over the Customer / Supplier records that
    already exist, pairing the two sides wherever a name appears on both.

    Matching is on the party NAME, exactly. Fuzzy matching was considered and
    rejected: silently fusing "Wells Fargo" with "Wells Fargo Bank NA" into one
    tax identity is a mistake that surfaces in January, and an operator merging
    two Counterparties by hand is a thirty-second job. Exact matching can only
    ever under-pair, which is the safe direction to be wrong in.

    Idempotent — re-running links nothing new and writes nothing. `dry_run`
    plans the whole pass without a single write.

    Returns {'created', 'linked', 'unchanged', 'failed', 'actions'} where
    `actions` is the human-readable log of what did (or would) happen."""
    result = {'created': 0, 'linked': 0, 'unchanged': 0, 'failed': 0,
              'actions': []}
    if not available(client):
        result['actions'].append(
            'Counterparty doctype unavailable — nothing to do')
        return result
    customers = dict(_enabled_party_names(client, CUSTOMER_DT, 'customer_name'))
    suppliers = dict(_enabled_party_names(client, SUPPLIER_DT, 'supplier_name'))
    # Index by party name so the same name on both sides pairs up. ERPNext
    # autonames both doctypes from the name field, so docname and party name are
    # normally identical — but we key on the NAME field, which is what a human
    # would call the party.
    by_name: dict[str, dict] = {}
    for docname, party_name in customers.items():
        by_name.setdefault(party_name or docname, {})['customer'] = docname
    for docname, party_name in suppliers.items():
        by_name.setdefault(party_name or docname, {})['supplier'] = docname
    for party_name in sorted(by_name):
        sides = by_name[party_name]
        customer = sides.get('customer', '')
        supplier = sides.get('supplier', '')
        existing = get_counterparty(client, party_name)
        if existing is None:
            roles = '+'.join(k for k in ('customer', 'supplier') if k in sides)
            if dry_run:
                result['created'] += 1
                result['actions'].append(
                    f'CREATE {party_name} ({roles}, type='
                    f'{counterparty_type_for(party_name)})')
                continue
            made = find_or_create_counterparty(
                client, party_name, customer=customer, supplier=supplier)
            if made:
                result['created'] += 1
                result['actions'].append(f'created {party_name} ({roles})')
            else:
                result['failed'] += 1
                result['actions'].append(f'FAILED to create {party_name}')
            continue
        updates = _link_updates(existing, customer, supplier)
        if not updates:
            result['unchanged'] += 1
            continue
        if dry_run:
            result['linked'] += 1
            result['actions'].append(f'LINK {party_name}: {updates}')
            continue
        try:
            client.update_doc(COUNTERPARTY_DT,
                              existing.get('name') or party_name, updates)
            result['linked'] += 1
            result['actions'].append(f'linked {party_name}: {updates}')
        except (ERPNextAPIError, ERPNextError) as e:
            result['failed'] += 1
            result['actions'].append(f'FAILED to link {party_name}: {e}')
    return result


def bootstrap(client: ERPNextClient | None) -> dict:
    """Provision the doctype and, on an install that has never had the overlay,
    pair the Customer / Supplier records that already exist.

    Called from the ERPNext bootstrap so an upgrading install gets its overlay
    with no manual step. The pairing pass is gated on
    COUNTERPARTY_AUTO_PAIR (default on) and is itself idempotent, so the
    steady-state cost after the first run is one list call per side that changes
    nothing."""
    status = {'available': False, 'paired': None}
    if client is None or not is_enabled():
        return status
    status['available'] = ensure_counterparty_doctype(client)
    if not status['available']:
        return status
    if current_app.config.get('COUNTERPARTY_AUTO_PAIR', True):
        try:
            status['paired'] = pair_existing_parties(client)
        except Exception:  # pragma: no cover - never block bootstrap
            log.warning('counterparty pairing pass failed', exc_info=True)
    return status
