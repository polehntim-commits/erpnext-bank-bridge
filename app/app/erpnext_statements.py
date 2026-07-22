# SPDX-License-Identifier: MIT
"""Bank statements, surfaced inside ERPNext (v0.4.10).

WHY. v0.4.9 pulls the bank's own monthly PDFs and files them under DATA_DIR,
where /admin/statements renders them beautifully — for anyone who logs into Bank
Bridge. The bookkeeper and the CPA do not. They live in ERPNext, and at tax time
the question they ask is "show me the statement behind this account for March",
which Bank Bridge could answer and ERPNext could not.

THE SHAPE. A `Bank Statement` doctype provisioned over REST, exactly the way
v0.4.5 provisions Counterparty, holding one record per statement with the PDF
attached to it. Bank Bridge remains the source of truth: records flow one way,
Bank-Bridge → ERPNext, and nothing read back from ERPNext ever changes local
state. That asymmetry is deliberate — two writers on one reconciliation verdict
is a conflict nobody would win, and the verdict is computed here (see
statements.reconcile_statement) from data only this side holds.

IDEMPOTENCY, twice over. `plaid_statement_id` is unique in ERPNext, and
`PlaidStatement.erpnext_docname` records the docname locally once a record
lands. The local column is the fast path (no round trip); the ERPNext uniqueness
probe is the correct one, and it is what recovers a data volume restored from a
backup taken before an upload — the row's docname is blank, so the sync tries
again, finds the record already there, and re-adopts it rather than creating a
duplicate.

WHY `bank_account` IS REQUIRED, AND WHAT THAT COSTS. A statement that does not
name an ERPNext Bank Account is not useful to the person this feature exists
for — they navigate from the account. So the Link is `reqd`, and the honest
consequence is that a statement for a Plaid account which was never imported
into ERPNext CANNOT be created. That is reported as a skip with a reason, not a
failure, and it resolves itself the moment the operator imports the account.

FAIL-OPEN, like every other ERPNext overlay here. Statements are an audit-trail
convenience. Nothing in this module may raise into the fetch path, the posting
path or the scheduler: an unreachable ERPNext logs, leaves local state exactly
as it was, and is retried on the next tick.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from flask import current_app

from . import db
from .erpnext_accounts import (_mark_doctype_available, _mark_doctype_unavailable,
                               _is_missing_doctype_error, is_doctype_unavailable)
from .erpnext_client import ERPNextAPIError, ERPNextClient, ERPNextError
from .models import PlaidAccount, PlaidStatement

log = logging.getLogger('bankbridge.erpnext_statements')

BANK_STATEMENT_DT = 'Bank Statement'
DOCTYPE_DT = 'DocType'
BANK_ACCOUNT_DT = 'Bank Account'

# Filed under Accounts, so the Bank Statement list sits beside Bank Account and
# the Bank Reconciliation Tool in the desk — where someone looking for a
# statement would actually go hunting.
BANK_STATEMENT_MODULE = 'Accounts'

# The `reconciliation_status` values. Mirrors statements.reconcile_statement's
# three outcomes, deliberately including the third: an unparseable PDF is NOT a
# discrepancy, and flagging it as one would train an operator to ignore the one
# value on this field that means something.
STATUS_RECONCILED = 'Reconciled'
STATUS_DISCREPANCY = 'Discrepancy'
STATUS_NOT_CHECKED = 'Not Checked'
RECONCILIATION_STATUSES = (STATUS_RECONCILED, STATUS_DISCREPANCY,
                           STATUS_NOT_CHECKED)

# statements.reconcile_statement's `status` → the ERPNext-facing value.
_STATUS_FROM_VERDICT = {
    'ok': STATUS_RECONCILED,
    'mismatch': STATUS_DISCREPANCY,
    'no_data': STATUS_NOT_CHECKED,
}

# The field the PDF attaches to.
PDF_FIELDNAME = 'statement_pdf'

# The field whose presence proves an existing `Bank Statement` doctype is OURS
# rather than something this ERPNext already had under the same name. See
# _is_our_doctype.
MARKER_FIELD = 'plaid_statement_id'


def _now() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── configuration ───────────────────────────────────────────────────────────

def is_enabled() -> bool:
    """Master switch (ERPNEXT_STATEMENTS_ENABLED, default on). Off → nothing in
    this module touches ERPNext, and v0.4.9's local statement storage behaves
    exactly as it did."""
    return bool(current_app.config.get('ERPNEXT_STATEMENTS_ENABLED', True))


def discrepancy_threshold() -> float:
    """How large a reconciliation variance has to be, in dollars, before the
    discrepancy report calls it out (ERPNEXT_STATEMENT_VARIANCE_THRESHOLD,
    default 10.00).

    Distinct from statements.reconcile_tolerance (default $1.00), which decides
    whether a period reconciles AT ALL. This one decides what is worth a human's
    attention: a $2 variance is a real mismatch worth recording on the record,
    and not worth a report row."""
    try:
        return abs(float(current_app.config.get(
            'ERPNEXT_STATEMENT_VARIANCE_THRESHOLD', 10.00)))
    except (TypeError, ValueError):
        return 10.00


def coverage_months() -> int:
    """How many months back the coverage report looks
    (ERPNEXT_STATEMENT_COVERAGE_MONTHS, default 12)."""
    try:
        months = int(current_app.config.get(
            'ERPNEXT_STATEMENT_COVERAGE_MONTHS', 12))
    except (TypeError, ValueError):
        months = 12
    return max(1, min(120, months))


# ── doctype bootstrap ───────────────────────────────────────────────────────

def _field(fieldname, fieldtype, label, **extra) -> dict:
    """One DocField spec. Frappe wants an explicit `label` on every field — it
    does not reliably title-case a fieldname for a doctype created over REST,
    and a label-less field renders blank in the desk. Same lesson, same helper
    shape, as counterparty._field."""
    spec = {'fieldname': fieldname, 'fieldtype': fieldtype, 'label': label}
    spec.update(extra)
    return spec


def bank_statement_doctype_spec() -> dict:
    """The DocType document we POST to create the statement overlay.

    `autoname: hash` — unlike Counterparty there is no human-meaningful name to
    key on. The natural key is `plaid_statement_id`, which is Plaid's opaque
    token: unique, but meaningless as a docname and long enough to be ugly in a
    breadcrumb. A hash name plus a unique field gets the constraint without the
    URL, and the list view is keyed on the fields a human actually reads
    (account, period, status).

    `variance_amount` and `fetched_at` are read_only: they are Bank Bridge's
    computed output, and a hand-edit would be silently overwritten on the next
    sync. Marking them so says as much in the UI."""
    return {
        'doctype': DOCTYPE_DT,
        'name': BANK_STATEMENT_DT,
        'module': BANK_STATEMENT_MODULE,
        'custom': 1,
        'naming_rule': 'Random',
        'autoname': 'hash',
        'track_changes': 1,
        'fields': [
            _field('bank_account', 'Link', 'Bank Account',
                   options=BANK_ACCOUNT_DT, reqd=1, in_list_view=1),
            _field('period_start', 'Date', 'Period Start', reqd=1,
                   in_list_view=1),
            _field('period_end', 'Date', 'Period End', reqd=1, in_list_view=1),
            _field('opening_balance', 'Currency', 'Opening Balance'),
            _field('closing_balance', 'Currency', 'Closing Balance'),
            _field(PDF_FIELDNAME, 'Attach', 'Statement PDF'),
            _field(MARKER_FIELD, 'Data', 'Plaid Statement ID', unique=1,
                   reqd=1),
            _field('reconciliation_status', 'Select', 'Reconciliation Status',
                   options='\n'.join(RECONCILIATION_STATUSES),
                   default=STATUS_NOT_CHECKED, in_list_view=1),
            _field('variance_amount', 'Currency', 'Variance Amount',
                   read_only=1),
            _field('fetched_at', 'Datetime', 'Fetched At', read_only=1),
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
    """True when Frappe refused for lack of rights rather than because the
    request was malformed — an API key minted for a user without DocType create
    permission is the realistic failure, and it means "unavailable", not
    "crash"."""
    if e.status_code in (401, 403):
        return True
    blob = ((e.response_body or '') + ' ' + str(e)).lower()
    return 'permissionerror' in blob or 'not permitted' in blob


def _is_our_doctype(doc: dict) -> bool:
    """Whether an existing `Bank Statement` doctype is the one THIS module
    creates.

    Counterparty could skip this check — nothing ships a doctype by that name.
    'Bank Statement' is a different matter: it is a plausible enough name that a
    Frappe app, an older ERPNext, or an operator's own customization could
    already occupy it. Writing our fields into a stranger's doctype would fail
    noisily at best and corrupt someone's records at worst.

    The test is the presence of `plaid_statement_id`, which nothing but this
    module has a reason to define. A doctype missing it is treated as foreign
    and left strictly alone."""
    fields = doc.get('fields')
    if not isinstance(fields, list):
        # A Frappe that answers the probe without expanding the child table
        # tells us nothing either way. Trust it rather than refuse the feature:
        # a genuine name collision surfaces on the first create, which fails
        # safely and is reported.
        return True
    return any((f or {}).get('fieldname') == MARKER_FIELD for f in fields)


# Outcomes provision_report can return in `state`. Stable strings — the startup
# job, the CLI and the tests all key off them.
PROVISION_CREATED = 'created'
PROVISION_PRESENT = 'already_present'
PROVISION_NO_CLIENT = 'not_configured'
PROVISION_PERMISSION = 'permission_denied'
PROVISION_NO_DOCTYPE_API = 'doctype_api_unavailable'
PROVISION_PROBE_FAILED = 'probe_failed'
PROVISION_CREATE_FAILED = 'create_failed'
PROVISION_FOREIGN = 'foreign_doctype'

PROVISION_HELP = {
    PROVISION_PERMISSION:
        'the ERPNext API user lacks create permission on DocType. Give it the '
        'System Manager role (or create the Bank Statement doctype by hand) '
        'and restart.',
    PROVISION_NO_DOCTYPE_API:
        'this ERPNext does not expose the DocType API, so the statement '
        'overlay cannot provision itself. Usually a locked-down or very old '
        'Frappe.',
    PROVISION_PROBE_FAILED:
        'the doctype could not be probed. Check the ERPNext URL and '
        'credentials on /admin/erpnext_settings, then restart.',
    PROVISION_CREATE_FAILED:
        'ERPNext rejected the doctype create. The reason it gave is in the '
        'message above.',
    PROVISION_NO_CLIENT:
        'ERPNext is not configured yet. Set it up on /admin/erpnext_settings; '
        'the overlay provisions itself on the next restart.',
    PROVISION_FOREIGN:
        "this ERPNext already has a 'Bank Statement' doctype that Bank Bridge "
        'did not create (it has no plaid_statement_id field). Bank Bridge will '
        'not write to it. Rename or remove the existing doctype to enable the '
        'statement overlay.',
}


def provision_report(client: ERPNextClient | None) -> dict:
    """Idempotently provision the Bank Statement doctype and REPORT what
    happened: `{'ok': bool, 'state': str, 'reason': str}`.

    Safe to call on every startup: the happy path on a provisioned instance is a
    single GET that 200s. Never raises. A refusal is recorded in the shared
    unavailable-doctype registry so the sync path short-circuits without
    re-probing for the rest of the process's life."""
    if client is None:
        return {'ok': False, 'state': PROVISION_NO_CLIENT, 'reason': ''}
    try:
        existing = client.get_doc(DOCTYPE_DT, BANK_STATEMENT_DT)
    except ERPNextAPIError as e:
        detail = str(e)[:300]
        if _is_permission_error(e):
            state = PROVISION_PERMISSION
        elif _is_missing_doctype_error(e):
            state = PROVISION_NO_DOCTYPE_API
        else:
            state = PROVISION_PROBE_FAILED
        log.warning('Bank Statement doctype NOT provisioned (%s) — %s [%s]',
                    state, PROVISION_HELP.get(state, ''), detail)
        _mark_doctype_unavailable(BANK_STATEMENT_DT)
        return {'ok': False, 'state': state, 'reason': detail}
    if existing is not None:
        if not _is_our_doctype(existing):
            log.warning('Bank Statement doctype NOT provisioned (%s) — %s',
                        PROVISION_FOREIGN, PROVISION_HELP[PROVISION_FOREIGN])
            _mark_doctype_unavailable(BANK_STATEMENT_DT)
            return {'ok': False, 'state': PROVISION_FOREIGN, 'reason': ''}
        log.info('Bank Statement doctype already present — nothing to provision')
        _mark_doctype_available(BANK_STATEMENT_DT)
        return {'ok': True, 'state': PROVISION_PRESENT, 'reason': ''}
    log.info('Bank Statement doctype absent — creating it')
    try:
        client.create_doc(DOCTYPE_DT, bank_statement_doctype_spec())
    except (ERPNextAPIError, ERPNextError) as e:
        # A concurrent worker winning the race is a success, not a failure:
        # re-probe rather than trusting the error text.
        try:
            racer = client.get_doc(DOCTYPE_DT, BANK_STATEMENT_DT)
            if racer is not None and _is_our_doctype(racer):
                log.info('Bank Statement doctype created concurrently by '
                         'another worker — treating as provisioned')
                _mark_doctype_available(BANK_STATEMENT_DT)
                return {'ok': True, 'state': PROVISION_PRESENT, 'reason': ''}
        except (ERPNextAPIError, ERPNextError):
            pass
        detail = str(e)[:300]
        state = (PROVISION_PERMISSION
                 if isinstance(e, ERPNextAPIError) and _is_permission_error(e)
                 else PROVISION_CREATE_FAILED)
        log.warning('Bank Statement doctype NOT provisioned (%s) — %s [%s]',
                    state, PROVISION_HELP.get(state, ''), detail)
        _mark_doctype_unavailable(BANK_STATEMENT_DT)
        return {'ok': False, 'state': state, 'reason': detail}
    log.info('provisioned the %s doctype', BANK_STATEMENT_DT)
    _mark_doctype_available(BANK_STATEMENT_DT)
    return {'ok': True, 'state': PROVISION_CREATED, 'reason': ''}


def ensure_bank_statement_doctype(client: ERPNextClient) -> bool:
    """Boolean face of provision_report, for callers that only need the gate."""
    return provision_report(client)['ok']


def available(client: ERPNextClient | None) -> bool:
    """Cheap gate for the hot paths: is the overlay usable right now? Consults
    the master switch and the process-local unavailable registry WITHOUT an
    ERPNext round-trip."""
    if client is None or not is_enabled():
        return False
    return not is_doctype_unavailable(BANK_STATEMENT_DT)


# ── local ↔ ERPNext field mapping ───────────────────────────────────────────

def _fmt_date(value) -> str | None:
    return value.isoformat() if isinstance(value, date) else None


def _fmt_datetime(value) -> str | None:
    """Frappe's Datetime wire format."""
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return None


def bank_account_for(statement: PlaidStatement) -> str:
    """The ERPNext Bank Account docname this statement belongs under, or ''.

    '' is the ordinary "this Plaid account was never imported into ERPNext"
    case, and it is a SKIP rather than a failure — `bank_account` is a required
    Link, so there is nothing valid to create, and importing the account fixes
    it without any action here."""
    account = PlaidAccount.query.filter_by(
        account_id=statement.plaid_account_id).first()
    if account is None:
        return ''
    return (account.erpnext_bank_account_name or '').strip()


def payload_for(statement: PlaidStatement, *, bank_account: str) -> dict:
    """The Bank Statement document body for one local statement row.

    Balances are omitted (not zeroed) when the PDF could not be parsed: 0.00 is
    a real balance and asserting it for a statement we could not read would be a
    lie the CPA has no way to spot. An absent field reads as "unknown", which is
    the truth."""
    doc = {
        'bank_account': bank_account,
        'period_start': _fmt_date(statement.period_start),
        'period_end': _fmt_date(statement.period_end),
        MARKER_FIELD: statement.statement_id,
        'fetched_at': _fmt_datetime(statement.fetched_at) or _fmt_datetime(_now()),
    }
    if statement.opening_balance is not None:
        doc['opening_balance'] = round(float(statement.opening_balance), 2)
    if statement.closing_balance is not None:
        doc['closing_balance'] = round(float(statement.closing_balance), 2)
    return doc


def find_by_plaid_id(client: ERPNextClient, statement_id: str) -> dict | None:
    """The existing Bank Statement record for a Plaid statement id, or None.

    This is the authoritative idempotency probe — `PlaidStatement.
    erpnext_docname` is only a local cache of its answer, and a data volume
    restored from an older backup has the column blank for records that DO
    exist. Tolerates every ERPNext error by answering None; the caller then
    attempts a create, which the unique constraint rejects safely."""
    if not statement_id:
        return None
    try:
        rows = client.list_docs(
            BANK_STATEMENT_DT,
            filters=[[MARKER_FIELD, '=', statement_id]],
            # opening/closing_balance are read (v0.4.41) because
            # push_reconciliation now diffs them too — a probe that omitted
            # them would read every record as having 0.00 and rewrite it on
            # every pass, which is exactly the churn the diff exists to avoid.
            fields=['name', MARKER_FIELD, 'reconciliation_status',
                    'variance_amount', 'opening_balance', 'closing_balance',
                    PDF_FIELDNAME],
            limit_page_length=1)
    except (ERPNextAPIError, ERPNextError) as e:
        log.warning('could not probe %s for %s: %s', BANK_STATEMENT_DT,
                    statement_id, str(e)[:200])
        return None
    return rows[0] if rows else None


# ── PDF attach ──────────────────────────────────────────────────────────────

def attach_pdf(client: ERPNextClient, statement: PlaidStatement,
               docname: str) -> str:
    """Upload this statement's PDF and attach it to `docname`. Returns the
    file_url, or '' when there was nothing to upload or the upload failed.

    A failed attach is NOT a failed sync: the record — account, period,
    balances, reconciliation verdict — is the part the reports run on, and it is
    already correct. The PDF is re-attempted on the next tick because
    `statement_pdf` is still blank."""
    path = None
    try:
        from . import statements as stmts
        path = stmts.resolve_pdf_path(statement)
    except Exception:  # pragma: no cover - defensive, resolve is total
        path = None
    if not path:
        return ''
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError as e:
        log.warning('could not read statement PDF %s: %s', path, e)
        return ''
    label = statement.period_label() or statement.statement_id
    filename = f'statement-{label}.pdf'
    try:
        created = client.upload_file(filename, data, doctype=BANK_STATEMENT_DT,
                                     docname=docname, is_private=1,
                                     fieldname=PDF_FIELDNAME)
    except (ERPNextAPIError, ERPNextError) as e:
        log.warning('could not attach PDF for statement %s: %s',
                    statement.statement_id, str(e)[:200])
        return ''
    file_url = ((created or {}).get('file_url') or '').strip()
    if not file_url:
        return ''
    # Frappe sets the target field itself when `fieldname` is supplied, but only
    # on versions that honour it. Writing it explicitly makes the Attach field
    # populated on every version, and is a no-op PUT when Frappe already did it.
    try:
        client.update_doc(BANK_STATEMENT_DT, docname, {PDF_FIELDNAME: file_url})
    except (ERPNextAPIError, ERPNextError) as e:
        log.info('PDF uploaded but %s field not set on %s: %s', PDF_FIELDNAME,
                 docname, str(e)[:200])
    return file_url


# ── reconciliation verdict ──────────────────────────────────────────────────

def verdict_fields(statement: PlaidStatement) -> dict:
    """`{'reconciliation_status', 'variance_amount'}` for one statement, from
    v0.4.9's own reconciliation.

    `variance_amount` is the SIGNED delta (expected closing minus the bank's
    closing), not its magnitude. The sign is real information — it says whether
    the mirror is over or short — and discarding it to make one report's filter
    simpler would be the wrong trade. The discrepancy report takes the absolute
    value at read time instead."""
    from . import statements as stmts
    try:
        verdict = stmts.reconcile_statement(statement)
    except Exception:  # pragma: no cover - reconcile is already total
        log.warning('reconciliation failed for statement %s',
                    statement.statement_id, exc_info=True)
        return {'reconciliation_status': STATUS_NOT_CHECKED,
                'variance_amount': 0.0}
    status = _STATUS_FROM_VERDICT.get(verdict['status'], STATUS_NOT_CHECKED)
    delta = verdict.get('delta')
    return {
        'reconciliation_status': status,
        'variance_amount': round(float(delta), 2) if delta is not None else 0.0,
    }


def _num(value) -> float:
    try:
        return round(float(value or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def push_reconciliation(client: ERPNextClient | None,
                        statement: PlaidStatement, *,
                        existing: dict | None = None) -> str:
    """Refresh one statement's reconciliation verdict in ERPNext.

    Returns 'updated', 'unchanged', 'missing' (no ERPNext record yet) or
    'failed'. Diffed before the PUT: on a settled install every statement's
    verdict is already correct, and a re-sync that rewrote them all would churn
    ERPNext's version history for nothing.

    `existing` lets a caller that has already read the record (sync_all reads
    them all in one list call) supply it, so a bulk refresh costs one round trip
    rather than one per statement."""
    if not available(client):
        return 'failed'
    docname = (statement.erpnext_docname or '').strip()
    if existing is None:
        existing = find_by_plaid_id(client, statement.statement_id)
    if not docname:
        if existing is None:
            return 'missing'
        docname = existing.get('name') or ''
        if not docname:
            return 'missing'
    wanted = verdict_fields(statement)
    # v0.4.41 — the balances travel with the verdict on a refresh, not only on
    # the initial create. A re-parse (statements.reparse_stored) can correct a
    # figure on a statement ERPNext already holds, and until this was here the
    # correction stayed local: the Bank Statement record kept the number a
    # weaker parser recovered, which is precisely the record a bookkeeper opens
    # as support for an opening-balance entry. Only stated figures are sent —
    # an unparseable balance leaves ERPNext's field alone rather than zeroing a
    # value someone may have keyed in by hand.
    if statement.opening_balance is not None:
        wanted['opening_balance'] = round(float(statement.opening_balance), 2)
    if statement.closing_balance is not None:
        wanted['closing_balance'] = round(float(statement.closing_balance), 2)
    if existing is not None:
        current = {
            'reconciliation_status': (existing.get('reconciliation_status')
                                      or STATUS_NOT_CHECKED),
            'variance_amount': _num(existing.get('variance_amount')),
        }
        for field in ('opening_balance', 'closing_balance'):
            if field in wanted:
                current[field] = _num(existing.get(field))
        if current == wanted:
            return 'unchanged'
    try:
        client.update_doc(BANK_STATEMENT_DT, docname, wanted)
    except (ERPNextAPIError, ERPNextError) as e:
        log.warning('could not update reconciliation on %s: %s', docname,
                    str(e)[:200])
        return 'failed'
    log.info('statement %s reconciliation → %s (variance %.2f)',
             statement.statement_id, wanted['reconciliation_status'],
             wanted['variance_amount'])
    statement.erpnext_synced_at = _now()
    db.session.commit()
    return 'updated'


# ── sync one ────────────────────────────────────────────────────────────────

def sync_statement(client: ERPNextClient | None, statement: PlaidStatement, *,
                   dry_run: bool = False) -> dict:
    """Make ERPNext hold a Bank Statement record for one local statement.

    Returns `{'action', 'docname', 'reason'}` where `action` is one of:

      * 'created'   — a new record (and, when a PDF exists, its attachment)
      * 'adopted'   — ERPNext already had it; the local row now records its name
      * 'skipped'   — already synced, nothing to do
      * 'no_account'— the Plaid account has no ERPNext Bank Account yet
      * 'no_period' — the statement has no period, so a required field is absent
      * 'failed'    — ERPNext refused the create

    Never raises."""
    out = {'action': 'failed', 'docname': '', 'reason': ''}
    if not available(client):
        out['reason'] = 'ERPNext statement overlay unavailable'
        return out

    # Fast path: we already know its docname and there is nothing to re-check.
    if (statement.erpnext_docname or '').strip():
        out.update(action='skipped', docname=statement.erpnext_docname.strip())
        return out

    # `period_start` / `period_end` are reqd on the doctype. A statement whose
    # month/year Plaid did not give us cannot satisfy that, and inventing bounds
    # would put a fabricated period in front of the CPA.
    if statement.period_start is None or statement.period_end is None:
        out.update(action='no_period', reason='statement has no period bounds')
        return out

    bank_account = bank_account_for(statement)
    if not bank_account:
        out.update(action='no_account',
                   reason='the Plaid account has no ERPNext Bank Account — '
                          'import it on /admin/accounts and this statement '
                          'syncs on the next tick')
        return out

    existing = find_by_plaid_id(client, statement.statement_id)
    if existing is not None:
        docname = existing.get('name') or ''
        out.update(action='adopted', docname=docname)
        if dry_run:
            return out
        statement.erpnext_docname = docname
        statement.erpnext_synced_at = _now()
        db.session.commit()
        # An adopted record may predate its attachment (a create whose upload
        # failed, retried on a later tick). Fill the gap.
        if docname and not (existing.get(PDF_FIELDNAME) or '').strip():
            attach_pdf(client, statement, docname)
        push_reconciliation(client, statement)
        return out

    if dry_run:
        out.update(action='created', docname='(dry-run)')
        return out

    doc = {**payload_for(statement, bank_account=bank_account),
           **verdict_fields(statement)}
    try:
        created = client.create_doc(BANK_STATEMENT_DT, doc)
    except (ERPNextAPIError, ERPNextError) as e:
        # Losing a race to a concurrent worker is a success: the unique
        # constraint on plaid_statement_id is what rejected us, and the winner's
        # record is the one we wanted. Re-probe rather than trust the error text.
        racer = find_by_plaid_id(client, statement.statement_id)
        if racer is not None and racer.get('name'):
            statement.erpnext_docname = racer['name']
            statement.erpnext_synced_at = _now()
            db.session.commit()
            out.update(action='adopted', docname=racer['name'])
            return out
        detail = str(e)[:300]
        log.warning('could not create %s for statement %s: %s',
                    BANK_STATEMENT_DT, statement.statement_id, detail)
        out['reason'] = detail
        return out

    docname = (created or {}).get('name') or ''
    if not docname:
        out['reason'] = 'ERPNext returned no docname'
        return out
    statement.erpnext_docname = docname
    statement.erpnext_synced_at = _now()
    db.session.commit()
    attach_pdf(client, statement, docname)
    log.info('created %s %s for statement %s (%s)', BANK_STATEMENT_DT, docname,
             statement.statement_id, statement.period_label())
    out.update(action='created', docname=docname)
    return out


# ── sync all ────────────────────────────────────────────────────────────────

def blank_stats() -> dict:
    """The sync tally.

    `skipped` counts statements a pass found already synced. Note that the bulk
    entry points select on `pending_statements()`, which excludes those — so in
    a bulk run it stays 0 and `already_synced` is the number that matters. The
    key is kept because `sync_statement` reports 'skipped' for a single
    statement, and the two must agree on vocabulary."""
    return {'scanned': 0, 'created': 0, 'adopted': 0, 'skipped': 0,
            'already_synced': 0, 'no_account': 0, 'no_period': 0, 'failed': 0,
            'reconciled': 0, 'errors': []}


def pending_statements() -> list:
    """Local statements with no ERPNext docname yet, oldest period first."""
    return (PlaidStatement.query
            .filter((PlaidStatement.erpnext_docname.is_(None))
                    | (PlaidStatement.erpnext_docname == ''))
            .order_by(PlaidStatement.period_start.asc().nullslast(),
                      PlaidStatement.id.asc())
            .all())


def synced_statements() -> list:
    """Local statements that DO have an ERPNext record — the ones whose
    reconciliation verdict is worth re-pushing."""
    return (PlaidStatement.query
            .filter(PlaidStatement.erpnext_docname.isnot(None),
                    PlaidStatement.erpnext_docname != '')
            .order_by(PlaidStatement.period_start.asc().nullslast(),
                      PlaidStatement.id.asc())
            .all())


def sync_all(client: ERPNextClient | None = None, *, dry_run: bool = False,
             refresh_reconciliation: bool = True) -> dict:
    """Push every local statement that ERPNext doesn't hold yet, then refresh
    the reconciliation verdict on the ones it does.

    The second pass matters more than it looks: a statement synced in March
    reconciles against whatever the mirror held in March, and a later backfill
    that closes a transaction gap changes the answer. Re-pushing the verdict is
    what keeps ERPNext's status honest as the mirror improves — and it is
    diffed, so on a settled install it costs one list call and no writes.

    Never raises. Returns the stats dict."""
    stats = blank_stats()
    if not is_enabled():
        stats['errors'].append(
            'the ERPNext statement overlay is disabled '
            '(ERPNEXT_STATEMENTS_ENABLED)')
        return stats
    if client is None:
        from .sync_engine import get_erp_client_or_none
        client = get_erp_client_or_none()
    if not available(client):
        stats['errors'].append(
            'ERPNext is unavailable or the Bank Statement doctype is not '
            'provisioned — statements stay local and sync on the next tick')
        return stats

    stats['already_synced'] = len(synced_statements())
    for statement in pending_statements():
        stats['scanned'] += 1
        try:
            result = sync_statement(client, statement, dry_run=dry_run)
        except Exception as e:  # never let one statement sink the pass
            db.session.rollback()
            log.warning('statement %s sync crashed: %s',
                        statement.statement_id, e, exc_info=True)
            stats['failed'] += 1
            stats['errors'].append(f'{statement.statement_id}: {e}')
            continue
        action = result['action']
        if action in stats:
            stats[action] += 1
        if action == 'failed' and result['reason']:
            stats['errors'].append(
                f"{statement.statement_id}: {result['reason']}")

    if refresh_reconciliation and not dry_run:
        # One list call for every record, then a diffed PUT only where the
        # verdict actually moved — rather than a probe per statement.
        by_plaid_id = {}
        for r in (list_records(client) or []):
            key = (r.get(MARKER_FIELD) or '').strip()
            if key:
                by_plaid_id[key] = r
        for statement in synced_statements():
            try:
                result = push_reconciliation(
                    client, statement,
                    existing=by_plaid_id.get(statement.statement_id))
                if result == 'updated':
                    stats['reconciled'] += 1
            except Exception as e:  # pragma: no cover - push is already total
                db.session.rollback()
                log.warning('reconciliation push crashed for %s: %s',
                            statement.statement_id, e)
    return stats


# ── reports ─────────────────────────────────────────────────────────────────
#
# Both reports read ERPNext first and fall back to local data when it cannot be
# reached. The fallback is not a lesser answer: Bank Bridge is the source of
# truth for every number in both reports, so a locally-computed row says exactly
# what the ERPNext-backed one would. The `source` key tells the page which it
# got, because an operator staring at a report during an outage deserves to know
# whether they are looking at ERPNext.

def list_records(client: ERPNextClient | None) -> list | None:
    """Every Bank Statement record, or None when ERPNext can't answer."""
    if not available(client):
        return None
    try:
        return client.list_docs(
            BANK_STATEMENT_DT,
            fields=['name', 'bank_account', 'period_start', 'period_end',
                    'opening_balance', 'closing_balance', MARKER_FIELD,
                    'reconciliation_status', 'variance_amount'],
            order_by='period_start asc')
    except (ERPNextAPIError, ERPNextError) as e:
        log.warning('could not list %s records: %s', BANK_STATEMENT_DT,
                    str(e)[:200])
        return None


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or '').strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, '%Y-%m-%d').date()
    except ValueError:
        return None


def discrepancy_report(client: ERPNextClient | None = None, *,
                       threshold: float | None = None) -> dict:
    """Statements whose reconciliation variance exceeds the threshold.

    `|variance|` is compared, not `variance`: a mirror that is $500 SHORT is
    every bit as wrong as one that is $500 over, and a signed filter would show
    only half of them. (The stored value stays signed — see verdict_fields.)

    Returns `{'source', 'threshold', 'rows'}`; rows are worst-first, because a
    report that makes you scroll to find the big number is one nobody reads."""
    limit = discrepancy_threshold() if threshold is None else abs(float(threshold))
    rows = list_records(client)
    out = []
    if rows is not None:
        source = 'erpnext'
        for r in rows:
            variance = _num(r.get('variance_amount'))
            if abs(variance) <= limit:
                continue
            out.append({
                'docname': r.get('name') or '',
                'bank_account': r.get('bank_account') or '',
                'period_start': _as_date(r.get('period_start')),
                'period_end': _as_date(r.get('period_end')),
                'opening_balance': _num(r.get('opening_balance')),
                'closing_balance': _num(r.get('closing_balance')),
                'statement_id': r.get(MARKER_FIELD) or '',
                'status': r.get('reconciliation_status') or STATUS_NOT_CHECKED,
                'variance': variance,
            })
    else:
        source = 'local'
        for st in PlaidStatement.query.order_by(
                PlaidStatement.period_start.asc().nullslast()).all():
            fields = verdict_fields(st)
            variance = _num(fields['variance_amount'])
            if abs(variance) <= limit:
                continue
            out.append({
                'docname': (st.erpnext_docname or ''),
                'bank_account': bank_account_for(st),
                'period_start': st.period_start,
                'period_end': st.period_end,
                'opening_balance': _num(st.opening_balance),
                'closing_balance': _num(st.closing_balance),
                'statement_id': st.statement_id,
                'status': fields['reconciliation_status'],
                'variance': variance,
            })
    out.sort(key=lambda r: abs(r['variance']), reverse=True)
    return {'source': source, 'threshold': limit, 'rows': out}


def month_keys(months: int, *, as_of: date | None = None) -> list:
    """The last `months` calendar months as 'YYYY-MM', oldest first, ending with
    the month BEFORE `as_of`.

    The current month is excluded deliberately: a bank issues a statement after
    a period closes, so the month in progress has no statement yet and flagging
    it as a gap would put a permanent false positive at the top of the report."""
    today = as_of or date.today()
    cursor = date(today.year, today.month, 1) - timedelta(days=1)
    keys = []
    for _ in range(months):
        keys.append(f'{cursor.year:04d}-{cursor.month:02d}')
        cursor = date(cursor.year, cursor.month, 1) - timedelta(days=1)
    keys.reverse()
    return keys


def coverage_report(client: ERPNextClient | None = None, *,
                    months: int | None = None,
                    as_of: date | None = None) -> dict:
    """Which accounts are missing which months over the last `months`.

    This is the report that catches the failure statements exist to catch: a
    silent gap. A statement that fails to parse is visible on /admin/statements;
    a statement that was never fetched at all is visible NOWHERE, and it is
    exactly the one that leaves a quarter unreconciled at tax time.

    Coverage is measured per ERPNext Bank Account when ERPNext can be reached
    and per Plaid account otherwise. Accounts linked mid-window legitimately
    have no statements before they existed, so months earlier than an account's
    first known statement are not counted as gaps — only holes BETWEEN
    statements and trailing months after the last one.

    Returns `{'source', 'months', 'rows'}`."""
    window = coverage_months() if months is None else max(1, int(months))
    keys = month_keys(window, as_of=as_of)
    wanted = set(keys)
    rows = list_records(client)
    held: dict[str, set] = {}
    labels: dict[str, str] = {}

    if rows is not None:
        source = 'erpnext'
        for r in rows:
            account = (r.get('bank_account') or '').strip()
            start = _as_date(r.get('period_start'))
            if not account or start is None:
                continue
            labels[account] = account
            held.setdefault(account, set()).add(
                f'{start.year:04d}-{start.month:02d}')
    else:
        source = 'local'
        for st in PlaidStatement.query.all():
            account_id = (st.plaid_account_id or '').strip()
            if not account_id or st.period_start is None:
                continue
            held.setdefault(account_id, set()).add(
                f'{st.period_start.year:04d}-{st.period_start.month:02d}')
        for account in PlaidAccount.query.all():
            if account.account_id in held:
                labels[account.account_id] = (
                    account.name or account.official_name
                    or account.account_id)

    out = []
    for key in sorted(held):
        have = held[key]
        # Only look from this account's first statement forward: an account
        # linked in May cannot be missing January.
        in_window = sorted(have & wanted)
        first = in_window[0] if in_window else None
        if first is None:
            # Statements exist, but all of them predate the window. Nothing
            # inside the window is attributable as a gap.
            missing = []
        else:
            missing = [k for k in keys if k >= first and k not in have]
        out.append({
            'account': key,
            'label': labels.get(key, key),
            'months_held': len(in_window),
            'months_expected': len([k for k in keys if first and k >= first]),
            'missing': missing,
            'gap_count': len(missing),
        })
    out.sort(key=lambda r: (-r['gap_count'], r['label']))
    return {'source': source, 'months': keys, 'rows': out}


# ── bootstrap entry point ───────────────────────────────────────────────────

def bootstrap(client: ERPNextClient | None) -> dict:
    """Provision the doctype and, when it is available, push whatever local
    statements ERPNext doesn't hold. Called from the scheduler's boot job.

    Returns `{'available': bool, 'report': dict, 'synced': dict|None}`."""
    status = {'available': False, 'report': None, 'synced': None}
    if client is None or not is_enabled():
        return status
    report = provision_report(client)
    status['report'] = report
    status['available'] = report['ok']
    if not report['ok']:
        return status
    if current_app.config.get('ERPNEXT_STATEMENTS_AUTO_SYNC', True):
        try:
            status['synced'] = sync_all(client)
        except Exception:  # pragma: no cover - never block bootstrap
            log.warning('statement sync pass failed', exc_info=True)
    return status
