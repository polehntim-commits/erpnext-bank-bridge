# SPDX-License-Identifier: MIT
"""Investment Advisory Agreement automation (v0.5.2, Phase E).

The fee, benchmark, performance and compliance mechanics of an Investment
Management Agreement, computed and stored so the quarterly reporting the
agreement requires is a REVIEW of what Bank Bridge worked out — not a
recomputation a bookkeeper does from scratch. That is the whole design test
here: every derived figure is persisted, line by line.

FOUR ENGINES, and where each stops:

  1. Daily AUM + base-fee accrual (sample_daily_aum). Always runs; it is data.
  2. Quarterly base-fee settlement JE (settle_quarter). Gated by the agreement's
     `fee_accrual_enabled` kill switch — the accrual is recorded regardless, but
     the ERPNext Journal Entry that moves it onto the books needs the switch on.
  3. Quarterly performance fee (compute_performance). The math always runs and
     is stored; whether a resulting fee is booked is gated by
     `performance_fee_enabled`.
  4. Daily risk-control check (run_risk_check). Always runs and records
     violations; whether an ALERT fires is gated by
     `risk_control_alerts_enabled`.

THE BOUNDARY, unchanged from v0.5.0/v0.5.1: computation and status are free;
a write to the Client's P&L is a deliberate opt-in. Every JE carries
`company = agreement.client_company`, so the Client's fee entries stay
separable and move by export/import with nothing to unwind. Nothing here
rewrites an opening balance or posts a correction.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from flask import current_app

from . import audit
from . import db
from . import erpnext_accounts
from .models import (AdvisoryAgreement, AdvisoryFeeAccrual, DailyAUM,
                     GeneratedJournalEntry, HighWaterMark, HurdleRateSample,
                     PerformanceSnapshot, PlaidAccount, RiskControlCheck,
                     Security, SecurityHolding)

log = logging.getLogger('bankbridge.advisory')

JOURNAL_ENTRY_DT = 'Journal Entry'


class AdvisoryError(Exception):
    """A registration or amendment was refused for an expected, explainable
    reason (unknown vocabulary value, a second active agreement on one account,
    an amendment of a superseded version). Callers translate it into whatever
    their surface's clean failure is — an MCP tool error, a form message — so
    nothing here needs to know about HTTP or JSON-RPC."""


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


def quarter_label(d: date) -> str:
    return f'{d.year}-Q{(d.month - 1) // 3 + 1}'


def quarter_start(d: date) -> date:
    return date(d.year, ((d.month - 1) // 3) * 3 + 1, 1)


# ── registration: the agreement as a signed document (v0.7.4) ────────────────
#
# Everything above this line assumes an agreement already EXISTS and computes
# what it owes. This section is the other half: recording the agreement itself —
# the parties, the mandate, the fee basis, the term — so advisory activity is a
# first-class entry in the reconciliation ledger and the eventual K-1 / audit
# trail can name the document that authorized every fee.
#
# The vocabularies are closed on purpose. A free-text `objective` or
# `billing_frequency` reads fine and aggregates to nothing; a fixed set means a
# report can group by it and an operator's typo is caught at registration rather
# than discovered in a quarterly.

OBJECTIVES = ('Aggressive Growth', 'Growth', 'Moderate Growth', 'Income',
              'Capital Preservation', 'Custom')
FEE_TYPES = ('Percent of AUM', 'Flat Annual', 'Performance', 'Hybrid')
BILLING_FREQUENCIES = ('Monthly', 'Quarterly', 'Semi-Annual', 'Annual')
STATUSES = ('active', 'terminated', 'superseded')

# fee_type → the amount(s) the document must actually state for that basis to
# mean anything. 'Performance' names neither: its rate is `performance_fee_rate`,
# which registration does not set (see register_agreement's notes).
_FEE_TYPE_REQUIRES = {
    'Percent of AUM': ('fee_percent_of_aum',),
    'Flat Annual': ('fee_flat_annual',),
    'Hybrid': ('fee_percent_of_aum', 'fee_flat_annual'),
    'Performance': (),
}


def _as_date(value, field: str) -> date | None:
    """An ISO 'YYYY-MM-DD' string or a date → date; None/'' → None."""
    if value in (None, ''):
        return None
    if isinstance(value, date):
        return value
    from datetime import datetime
    try:
        return datetime.strptime(str(value).strip()[:10], '%Y-%m-%d').date()
    except ValueError:
        raise AdvisoryError(
            f'{field} must be an ISO date (YYYY-MM-DD), got {value!r}')


def _one_of(value, allowed: tuple, field: str) -> str:
    """`value` trimmed after checking it against a closed vocabulary. '' passes
    through — an unstated term is recorded as unstated, never guessed."""
    v = (value or '').strip()
    if not v:
        return ''
    for a in allowed:
        if v.lower() == a.lower():
            return a               # canonical casing, so reports group cleanly
    raise AdvisoryError(f'{field} must be one of {", ".join(allowed)} — '
                        f'got {value!r}')


def _as_float(value, field: str) -> float | None:
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise AdvisoryError(f'{field} must be a number, got {value!r}')


def _as_int(value, field: str) -> int | None:
    if value in (None, ''):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise AdvisoryError(f'{field} must be an integer, got {value!r}')


def active_agreement_for_account(account_id: str,
                                 exclude_id: int | None = None):
    """The agreement currently governing one Plaid account, or None.

    Walks the table in Python rather than querying `managed_account_ids`,
    deliberately: the column is JSON with a JSONB variant, the table holds a
    handful of rows, and a containment query that works on both Postgres and the
    SQLite the tests run on would cost more than it saves."""
    for ag in AdvisoryAgreement.query.all():
        if exclude_id is not None and ag.id == exclude_id:
            continue
        if account_id in ag.account_ids() and ag.is_active():
            return ag
    return None


def _apply_fee_basis(agreement: AdvisoryAgreement, fee_type: str,
                     pct_of_aum, flat_annual) -> None:
    """Write the fee basis onto the agreement, including the ENGINE rate.

    `fee_percent_of_aum` is stated the way a document states it (1.0 for 1%);
    `total_base_fee_rate` is the fraction the daily accrual multiplies by
    (0.01). Storing both is the point — the first is what a human checks against
    the PDF, the second is what sample_daily_aum uses, and a conversion done
    once here cannot drift the way one done at every read would."""
    agreement.fee_type = fee_type
    agreement.fee_flat_annual = flat_annual
    if pct_of_aum is not None:
        agreement.total_base_fee_rate = round(float(pct_of_aum) / 100.0, 8)


def register_agreement(account, *, name: str, client_entity: str,
                       advisor_entity: str, effective_date,
                       objective: str = '', investment_horizon_years=None,
                       fee_type: str = '', fee_percent_of_aum=None,
                       fee_flat_annual=None, billing_frequency: str = '',
                       termination_date=None, applies_to_company: str = '',
                       document_reference: str = '',
                       actor: str = 'system') -> tuple:
    """Register one advisory agreement against one managed Plaid account.

    `account` is an already-resolved PlaidAccount (the caller owns mask →
    account resolution, since that is where the brokerage/cash-companion
    preference lives). Returns `(agreement, notes)`, where `notes` lists what
    registration deliberately did NOT configure — see below.

    REFUSES, as an AdvisoryError:
      * a missing name / client_entity / advisor_entity / effective_date;
      * an objective, fee_type or billing_frequency outside its vocabulary;
      * a fee_type whose stated amount is missing (a 'Percent of AUM' agreement
        with no percent is not a fee basis, it is a blank);
      * a termination_date on or before the effective_date;
      * a SECOND active agreement on the same account. One account, one
        governing agreement: two would make "which terms billed this quarter?"
        unanswerable, which is the question this record exists to answer. To
        replace an agreement, terminate the incumbent first (or amend it).

    WHAT REGISTRATION DOES NOT SET, and why each is left alone rather than
    defaulted to something plausible:

      * The three kill switches (fee_accrual_enabled, performance_fee_enabled,
        risk_control_alerts_enabled) stay OFF. Registering the terms is not
        authorizing a posting to the Client's P&L — that opt-in is the boundary
        this whole module is built around.
      * `bank_fee_rate` is set to 0.0, so the full stated percent accrues as the
        Manager's settleable fee. The split between a bank's directly-deducted
        cut and the Manager's is a term registration has no argument for;
        leaving the v0.5.2 class default (0.75%) would silently divert three
        quarters of a 1% fee into a bucket that is never posted.
      * `performance_fee_rate` is set to 0.0 — an agreement registered without a
        performance-fee rate earns none, rather than inheriting the 20% class
        default that no one signed.
      * The settlement JE accounts (advisory_expense_account, fee_account_id)
        stay blank, so settle_quarter records the accrual and posts nothing
        until an operator names the two accounts on /admin/advisory.

    Each of those is returned in `notes` rather than only documented here,
    because the caller is usually an AI relaying to an operator who needs to
    know the registration is not yet a billing configuration."""
    name = (name or '').strip()
    client_entity = (client_entity or '').strip()
    advisor_entity = (advisor_entity or '').strip()
    if not name:
        raise AdvisoryError('agreement_name is required')
    if not client_entity:
        raise AdvisoryError('client_entity is required')
    if not advisor_entity:
        raise AdvisoryError('advisor_entity is required')

    eff = _as_date(effective_date, 'effective_date')
    if eff is None:
        raise AdvisoryError('effective_date is required (YYYY-MM-DD)')
    term = _as_date(termination_date, 'termination_date')
    if term is not None and term <= eff:
        raise AdvisoryError(
            f'termination_date {term.isoformat()} is on or before '
            f'effective_date {eff.isoformat()} — the agreement would never '
            f'govern anything')

    objective = _one_of(objective, OBJECTIVES, 'objective')
    fee_type = _one_of(fee_type, FEE_TYPES, 'fee_type')
    billing_frequency = _one_of(billing_frequency, BILLING_FREQUENCIES,
                                'billing_frequency')
    horizon = _as_int(investment_horizon_years, 'investment_horizon_years')
    pct = _as_float(fee_percent_of_aum, 'fee_percent_of_aum')
    flat = _as_float(fee_flat_annual, 'fee_flat_annual')
    _check_fee_basis(fee_type, pct, flat)

    conflict = active_agreement_for_account(account.account_id)
    if conflict is not None:
        raise AdvisoryError(
            f'account {account.mask} is already governed by active advisory '
            f'agreement #{conflict.id} ({conflict.name!r}). One account has one '
            f'governing agreement — terminate or amend #{conflict.id} first '
            f'(update_advisory_agreement with a termination_date)')

    agreement = AdvisoryAgreement(
        name=name, effective_date=eff, termination_date=term,
        client_entity=client_entity, advisor_entity=advisor_entity,
        # manager_name is the v0.5.2 display field the dashboard renders; the
        # advisor IS the manager for a registered agreement, so it mirrors
        # advisor_entity rather than sitting blank next to it.
        manager_name=advisor_entity,
        client_company=(applies_to_company or '').strip(),
        managed_account_ids=[account.account_id],
        objective=objective, investment_horizon_years=horizon,
        billing_frequency=billing_frequency,
        document_reference=(document_reference or '').strip(),
        bank_fee_rate=0.0, performance_fee_rate=0.0,
        total_base_fee_rate=0.0, status='active')
    _apply_fee_basis(agreement, fee_type, pct, flat)
    db.session.add(agreement)
    db.session.commit()

    notes = _registration_notes(agreement)
    audit.record('advisory_agreement_registered',
                 subject_type='AdvisoryAgreement', subject_id=agreement.id,
                 after=agreement.to_dict(),
                 notes=f'registered {name!r} on account {account.mask} '
                       f'({client_entity} / {advisor_entity})',
                 actor=actor)
    return agreement, notes


def _check_fee_basis(fee_type: str, pct, flat) -> None:
    """A stated fee_type must carry the amount that makes it a basis."""
    if not fee_type:
        return
    have = {'fee_percent_of_aum': pct, 'fee_flat_annual': flat}
    missing = [f for f in _FEE_TYPE_REQUIRES.get(fee_type, ())
               if have.get(f) in (None, 0, 0.0)]
    if missing:
        raise AdvisoryError(
            f"fee_type {fee_type!r} requires {' and '.join(missing)} — "
            f'a fee basis with no amount is not a fee basis')


def _registration_notes(agreement: AdvisoryAgreement) -> list:
    """What this agreement still needs before it can bill anything. Returned to
    the caller so an operator sees the gap without reading the source."""
    notes = []
    if not agreement.fee_accrual_enabled:
        notes.append(
            'fee_accrual_enabled is OFF — daily AUM and the base-fee accrual '
            'are recorded, but no settlement Journal Entry is posted until an '
            'operator opts in on /admin/advisory')
    if not (agreement.advisory_expense_account or '').strip() \
            or not (agreement.fee_account_id or '').strip():
        notes.append(
            'no settlement accounts configured (advisory_expense_account / '
            'fee_account_id) — set them on /admin/advisory before enabling '
            'fee posting')
    if not float(agreement.bank_fee_rate or 0.0):
        notes.append(
            'bank_fee_rate is 0.0 — the full stated rate accrues as the '
            "Manager's settleable fee. Set a bank cut on /admin/advisory if "
            'the advisor deducts part of the fee directly')
    if not float(agreement.performance_fee_rate or 0.0):
        notes.append(
            'performance_fee_rate is 0.0 — no performance fee is computed for '
            'this agreement')
    if not (agreement.document_reference or '').strip():
        notes.append(
            'no document_reference — this record is not linked to a signed '
            'document in ERPNext')
    # The accrual engine is percent-of-AUM only (sample_daily_aum multiplies AUM
    # by total_base_fee_rate). A flat or performance-only basis is REGISTERED
    # correctly and accrues nothing daily, which an operator must be told
    # outright rather than discover from a quarter of zeroes.
    if (agreement.fee_type or '') in ('Flat Annual', 'Performance'):
        notes.append(
            f'fee_type is {agreement.fee_type!r} — the daily accrual engine '
            f'computes percent-of-AUM fees only, so this agreement accrues '
            f'nothing automatically; its fee is recorded by hand')
    return notes


# MCP/UI argument name → model attribute, for an amendment's patch. Deliberately
# NOT a superset of the model: `managed_account_ids` is absent because moving an
# agreement onto a different account is a new agreement, not an amendment, and
# the one-active-per-account rule could not be honestly enforced across a
# clone-and-supersede that also changed the account.
AMENDABLE = {
    'agreement_name': 'name',
    'client_entity': 'client_entity',
    'advisor_entity': 'advisor_entity',
    'objective': 'objective',
    'investment_horizon_years': 'investment_horizon_years',
    'fee_type': 'fee_type',
    'fee_percent_of_aum': 'total_base_fee_rate',   # converted, see _apply_fee_basis
    'fee_flat_annual': 'fee_flat_annual',
    'billing_frequency': 'billing_frequency',
    'effective_date': 'effective_date',
    'termination_date': 'termination_date',
    'applies_to_company': 'client_company',
    'document_reference': 'document_reference',
    'status': 'status',
}

# Columns a new VERSION does NOT inherit — the row's identity and its place in
# the history chain. Everything else is read off the table rather than spelled
# out, so a column a future release adds is carried across an amendment instead
# of being silently reset to its default (the same reasoning as _RULE_CLONE_SKIP
# in the MCP blueprint).
_AGREEMENT_CLONE_SKIP = frozenset({'id', 'created_at', 'updated_at',
                                   'superseded_by', 'status'})

# The child tables whose rows follow the LIVE agreement across an amendment.
_AGREEMENT_CHILDREN = (DailyAUM, AdvisoryFeeAccrual, PerformanceSnapshot,
                       HighWaterMark, RiskControlCheck)


def amend_agreement(agreement: AdvisoryAgreement, patch: dict,
                    actor: str = 'system') -> tuple:
    """Amend one agreement by CLONE-AND-SUPERSEDE. Returns `(new, old, applied)`.

    An amendment writes a NEW row carrying the amended terms and marks the old
    one `status='superseded'` with `superseded_by` pointing forward. Nothing is
    mutated in place, so the terms that governed a past fee posting stay exactly
    readable — which is the whole reason a fee record cites an agreement id.

    THE HISTORY MOVES, THE TERMS DO NOT. Every child row (daily AUM, accruals,
    performance snapshots, high-water marks, risk checks) is re-pointed to the
    new id, so the live agreement carries its full history and its dashboard
    keeps rendering. The superseded row is left holding only its terms and its
    dates — which is precisely what it is for. To ask "under what terms was the
    2026-Q2 fee accrued?", follow the accrual's date back through the version
    chain, not the accrual's foreign key.

    REFUSES an amendment of a SUPERSEDED version (it is history and governs
    nothing — amend its successor), an empty patch, and every vocabulary/date
    violation register_agreement refuses."""
    if (agreement.status or '') == 'superseded':
        raise AdvisoryError(
            f'agreement #{agreement.id} is superseded — it is a historical '
            f'version and governs nothing. Amend the one that superseded it'
            + (f' (#{agreement.superseded_by})' if agreement.superseded_by
               else ''))
    clean = {k: v for k, v in (patch or {}).items() if k in AMENDABLE}
    if not clean:
        raise AdvisoryError(
            'nothing to amend — pass at least one field '
            f"({', '.join(sorted(AMENDABLE))})")

    for field in ('effective_date', 'termination_date'):
        if field in clean:
            clean[field] = _as_date(clean[field], field)
    if 'objective' in clean:
        clean['objective'] = _one_of(clean['objective'], OBJECTIVES,
                                     'objective')
    if 'fee_type' in clean:
        clean['fee_type'] = _one_of(clean['fee_type'], FEE_TYPES, 'fee_type')
    if 'billing_frequency' in clean:
        clean['billing_frequency'] = _one_of(
            clean['billing_frequency'], BILLING_FREQUENCIES,
            'billing_frequency')
    if 'status' in clean:
        clean['status'] = _one_of(clean['status'], STATUSES, 'status')
        if clean['status'] == 'superseded':
            raise AdvisoryError(
                "status 'superseded' is set by an amendment, not by one — pass "
                "'terminated' to end the agreement, or amend the terms")
    if 'investment_horizon_years' in clean:
        clean['investment_horizon_years'] = _as_int(
            clean['investment_horizon_years'], 'investment_horizon_years')
    if 'fee_percent_of_aum' in clean:
        clean['fee_percent_of_aum'] = _as_float(clean['fee_percent_of_aum'],
                                                'fee_percent_of_aum')
    if 'fee_flat_annual' in clean:
        clean['fee_flat_annual'] = _as_float(clean['fee_flat_annual'],
                                             'fee_flat_annual')

    # The resulting terms, not the incoming ones: an amendment that changes
    # fee_type and its amount in one call must be checked against the pair it
    # LANDS on, and one that changes only the amount against the type already
    # stored.
    eff = clean.get('effective_date', agreement.effective_date)
    term = clean.get('termination_date', agreement.termination_date)
    if eff and term and term <= eff:
        raise AdvisoryError(
            f'termination_date {term.isoformat()} is on or before '
            f'effective_date {eff.isoformat()}')
    fee_type = clean.get('fee_type', agreement.fee_type or '')
    pct = clean.get('fee_percent_of_aum',
                    round(float(agreement.total_base_fee_rate or 0.0) * 100, 6))
    flat = clean.get('fee_flat_annual', agreement.fee_flat_annual)
    _check_fee_basis(fee_type, pct, flat)

    before = agreement.to_dict()
    vals = {c.name: getattr(agreement, c.name)
            for c in AdvisoryAgreement.__table__.columns
            if c.name not in _AGREEMENT_CLONE_SKIP}
    for key, attr in AMENDABLE.items():
        if key in clean and key != 'fee_percent_of_aum':
            vals[attr] = clean[key]
    vals['status'] = clean.get('status', 'active')
    # A JSON/JSONB column must be COPIED, not shared: handing the same list
    # object to two rows makes a later mutation of one silently rewrite the
    # other's history.
    vals['managed_account_ids'] = list(agreement.account_ids())
    vals['risk_control_config'] = dict(agreement.risk_control_config or {})

    new = AdvisoryAgreement(**vals)
    if 'fee_percent_of_aum' in clean or 'fee_type' in clean \
            or 'fee_flat_annual' in clean:
        _apply_fee_basis(new, fee_type, clean.get('fee_percent_of_aum'), flat)
    db.session.add(new)
    db.session.flush()                     # assign new.id
    for model in _AGREEMENT_CHILDREN:
        (model.query.filter_by(agreement_id=agreement.id)
         .update({'agreement_id': new.id}, synchronize_session=False))
    agreement.status = 'superseded'
    agreement.superseded_by = new.id
    db.session.commit()

    applied = sorted(clean)
    audit.record('advisory_agreement_amended',
                 subject_type='AdvisoryAgreement', subject_id=new.id,
                 before=before, after=new.to_dict(),
                 notes=f'agreement #{agreement.id} superseded by #{new.id} '
                       f"({', '.join(applied)})",
                 actor=actor)
    return new, agreement, applied


# ── AUM sampling + base-fee accrual ──────────────────────────────────────────

def account_market_value(account_id: str) -> float:
    """The market value of one managed account, PLUS the cash on its paired
    companion. A brokerage account's cash lives on a separate companion
    account, so that companion's balance is added when the account names one.

    The account's OWN value is holdings-or-balance, never both (v0.7.5). Plaid
    defines `balances.current` on an investment account as the account's total
    market value — the priced holdings plus settled cash — so summing the
    SecurityHolding rows AND balance_current counts every position twice. That
    is what v0.5.2 did, and it roughly doubled the AUM every base fee accrues
    off. Holdings win when we have them: they are per-position and dated, where
    balance_current is one cached scalar from the last accounts pull. With no
    holdings (a custodian Plaid returns none for, or an install without the
    `investments` product) balance_current is all there is, and reporting it
    beats collapsing the account to zero.

    Non-investment accounts are unaffected: a depository or credit account has
    no holdings to double, and its balance_current stays authoritative even if
    a stray SecurityHolding row is somehow filed against it."""
    account = PlaidAccount.query.filter_by(account_id=account_id).first()
    if account is None:
        return 0.0

    holdings = SecurityHolding.query.filter_by(account_id=account_id).all()
    if holdings and erpnext_accounts.is_investment(account):
        total = 0.0
        for h in holdings:
            if h.institution_value is not None:
                total += float(h.institution_value)
            elif h.quantity and h.institution_price:
                total += float(h.quantity) * float(h.institution_price)
    else:
        total = float(account.balance_current or 0.0)

    partner_id = (account.paired_account_id or '').strip()
    if partner_id:
        partner = PlaidAccount.query.filter_by(account_id=partner_id).first()
        if partner is not None:
            total += float(partner.balance_current or 0.0)
    return round(total, 2)


def agreement_aum(agreement: AdvisoryAgreement) -> float:
    """Total AUM across every account the agreement manages."""
    return round(sum(account_market_value(aid)
                     for aid in agreement.account_ids()), 2)


def sample_daily_aum(agreement: AdvisoryAgreement,
                     on: date | None = None) -> DailyAUM:
    """Record one day's AUM and base-fee accrual for the agreement.

    Idempotent on (agreement, date): re-sampling the same day overwrites its
    row rather than double-accruing. The quarter-to-date cumulative is the sum
    of every accrual from the quarter's first day through this one, so a
    re-sample stays consistent no matter the order days arrive in.

    ALWAYS runs regardless of the kill switches — an accrual is data the
    dashboard shows; only the settlement JE is gated."""
    on = on or date.today()
    aum = agreement_aum(agreement)
    daily = round(aum * float(agreement.total_base_fee_rate or 0.0) / 365.0, 2)
    row = (DailyAUM.query
           .filter_by(agreement_id=agreement.id, date=on).first())
    if row is None:
        row = DailyAUM(agreement_id=agreement.id, date=on)
        db.session.add(row)
    row.total_market_value = aum
    row.fee_accrual_daily = daily
    db.session.flush()
    qstart = quarter_start(on)
    qtd = (db.session.query(db.func.coalesce(
        db.func.sum(DailyAUM.fee_accrual_daily), 0.0))
        .filter(DailyAUM.agreement_id == agreement.id,
                DailyAUM.date >= qstart, DailyAUM.date <= on)
        .scalar())
    row.cumulative_fee_accrual_qtd = round(float(qtd), 2)
    db.session.commit()
    return row


def base_fee_split(agreement: AdvisoryAgreement, daily_accrual: float) -> dict:
    """Split one day's base accrual into the bank's cut (recorded, never posted
    — WF deducts it directly) and the Manager's cut (accrued to the payable and
    settled quarterly)."""
    total_rate = float(agreement.total_base_fee_rate or 0.0) or 1.0
    bank = round(daily_accrual * float(agreement.bank_fee_rate or 0.0)
                 / total_rate, 4)
    return {'bank': bank, 'manager': round(daily_accrual - bank, 4)}


# ── quarterly base-fee settlement ────────────────────────────────────────────

def settle_quarter(client, agreement: AdvisoryAgreement,
                   quarter_end: date) -> AdvisoryFeeAccrual | None:
    """Aggregate the quarter's Manager base-fee accrual and, when
    `fee_accrual_enabled` is on, post the settlement Journal Entry.

    IDEMPOTENT on (agreement, 'base', quarter): the accrual row is the guard, so
    a re-run recognizes the settled quarter and posts nothing new. The accrual
    is recorded whether or not the switch is on — so a bookkeeper can SEE the
    pending amount — and gains its `erpnext_je_id` only once actually posted.

    Returns the AdvisoryFeeAccrual, or None when there is nothing to settle."""
    period = quarter_label(quarter_end)
    qstart = quarter_start(quarter_end)
    rows = (DailyAUM.query
            .filter(DailyAUM.agreement_id == agreement.id,
                    DailyAUM.date >= qstart, DailyAUM.date <= quarter_end)
            .all())
    if not rows:
        return None
    manager_total = round(sum(
        base_fee_split(agreement, r.fee_accrual_daily)['manager']
        for r in rows), 2)
    if manager_total <= 0:
        return None

    accrual = (AdvisoryFeeAccrual.query
               .filter_by(agreement_id=agreement.id, fee_type='base',
                          period_label=period).first())
    if accrual is None:
        accrual = AdvisoryFeeAccrual(
            agreement_id=agreement.id, fee_type='base', period_label=period,
            accrual_date=quarter_end)
        db.session.add(accrual)
    accrual.amount = manager_total
    accrual.accrual_date = quarter_end
    accrual.updated_at = _now()

    if accrual.posted_to_erpnext:
        db.session.commit()
        return accrual
    if not agreement.fee_accrual_enabled:
        accrual.notes = 'accrued — fee posting disabled (opt-in required)'
        db.session.commit()
        return accrual
    _post_fee_je(client, agreement, accrual,
                 f'Manager advisory base fee, {period}')
    db.session.commit()
    return accrual


def _post_fee_je(client, agreement: AdvisoryAgreement,
                 accrual: AdvisoryFeeAccrual, remark: str) -> None:
    """Post the settlement JE for one accrual and mark it. DR the advisory
    expense, CR the fee account. Company-scoped; never raises — a failure
    leaves the accrual recorded-but-unposted for the next run to retry."""
    dr = (agreement.advisory_expense_account or '').strip()
    cr = (agreement.fee_account_id or '').strip()
    if not dr or not cr:
        accrual.notes = 'cannot post — fee accounts not configured'
        return
    doc = {'doctype': JOURNAL_ENTRY_DT, 'voucher_type': 'Journal Entry',
           'company': agreement.client_company, 'user_remark': remark,
           'posting_date': accrual.accrual_date.isoformat(),
           'accounts': [
               {'account': dr, 'debit_in_account_currency': accrual.amount},
               {'account': cr, 'credit_in_account_currency': accrual.amount}]}
    try:
        from .erpnext_client import ERPNextAPIError, ERPNextError
        created = client.create_doc(JOURNAL_ENTRY_DT, doc)
        name = created.get('name')
        if not name:
            raise ERPNextAPIError('no JE name', status_code=None)
    except Exception as e:  # noqa: BLE001 - record and retry next run
        db.session.rollback()
        accrual = (AdvisoryFeeAccrual.query
                   .filter_by(id=accrual.id).first())
        if accrual is not None:
            accrual.notes = f'post failed: {str(e)[:200]}'
        log.warning('advisory fee JE failed for agreement %s: %s',
                    agreement.id, e)
        return
    accrual.posted_to_erpnext = True
    accrual.erpnext_je_id = name
    accrual.notes = f'posted {remark}'
    # Mirror into the GeneratedJournalEntry ledger the dashboard reads.
    gid = f'advfee:{agreement.id}:{accrual.period_label}:{accrual.fee_type}'
    gje = GeneratedJournalEntry.query.filter_by(
        plaid_transaction_id=gid).first()
    if gje is None:
        gje = GeneratedJournalEntry(plaid_transaction_id=gid)
        db.session.add(gje)
    gje.erpnext_journal_entry_name = name
    gje.amount = accrual.amount
    gje.rule_name = 'advisory_fee'
    gje.description = remark
    gje.state = 'approved'
    audit.record('advisory_fee_posted', subject_type='AdvisoryFeeAccrual',
                 subject_id=accrual.id,
                 after={'journal_entry': name, 'amount': accrual.amount,
                        'company': agreement.client_company, 'doc': doc},
                 notes=remark)


# ── hurdle rate ──────────────────────────────────────────────────────────────

def record_hurdle_sample(on: date, rate_pct: float,
                         source: str = 'manual') -> HurdleRateSample:
    """Store one day's hurdle rate, overwriting a same-date sample."""
    row = HurdleRateSample.query.filter_by(date=on).first()
    if row is None:
        row = HurdleRateSample(date=on)
        db.session.add(row)
    row.rate_pct = round(float(rate_pct), 4)
    row.source = source
    db.session.commit()
    return row


def poll_fred_hurdle(on: date | None = None) -> HurdleRateSample | None:
    """Fetch the latest 10-year Treasury (FRED DGS10) and store it.

    Degrades cleanly: with no FRED_API_KEY configured, or on any HTTP failure,
    it returns None and the operator enters the rate by hand (record_hurdle_
    sample). The hurdle math never depends on the poll succeeding — a missing
    day is interpolated from the samples that exist (see hurdle_return)."""
    key = (current_app.config.get('FRED_API_KEY') or '').strip()
    if not key:
        return None
    try:
        import requests
        resp = requests.get(
            'https://api.stlouisfed.org/fred/series/observations',
            params={'series_id': 'DGS10', 'api_key': key, 'file_type': 'json',
                    'sort_order': 'desc', 'limit': 1},
            timeout=10)
        resp.raise_for_status()
        obs = (resp.json().get('observations') or [])
        if not obs:
            return None
        value = obs[0].get('value')
        obs_date = obs[0].get('date')
        if value in (None, '.', ''):
            return None
        from datetime import datetime
        d = datetime.strptime(obs_date, '%Y-%m-%d').date()
        return record_hurdle_sample(d, float(value), source='fred')
    except Exception as e:  # noqa: BLE001 - the poll is best-effort
        log.info('FRED hurdle poll failed (%s); manual entry still available', e)
        return None


def hurdle_return(start: date, end: date) -> float:
    """The hurdle benchmark's return over [start, end] as a fraction, from the
    stored daily rates. A 10-year Treasury rate is an ANNUAL yield, so the
    period return is the average rate over the window pro-rated by the number
    of days it spans. 0.0 when no samples cover the period — a missing feed
    should not manufacture an excess return."""
    samples = (HurdleRateSample.query
               .filter(HurdleRateSample.date >= start,
                       HurdleRateSample.date <= end)
               .all())
    if not samples:
        return 0.0
    avg_rate = sum(float(s.rate_pct) for s in samples) / len(samples)
    days = max(1, (end - start).days)
    return round((avg_rate / 100.0) * (days / 365.0), 6)


# ── performance fee ──────────────────────────────────────────────────────────

def current_high_water_mark(agreement: AdvisoryAgreement) -> float:
    """The highest mark recorded so far, or 0.0 when none is."""
    row = (HighWaterMark.query
           .filter_by(agreement_id=agreement.id)
           .order_by(HighWaterMark.mark_value.desc()).first())
    return float(row.mark_value) if row else 0.0


def ratchet_high_water_mark(agreement: AdvisoryAgreement, on: date,
                            value: float, period: str) -> bool:
    """Record a new high-water mark IFF `value` exceeds the current one. The
    ratchet: a mark only ever moves up, so no performance fee can be charged on
    merely recovering ground already billed (recapture prevention). Returns
    True when a new mark was set."""
    if value <= current_high_water_mark(agreement):
        return False
    db.session.add(HighWaterMark(
        agreement_id=agreement.id, mark_date=on, mark_value=round(value, 2),
        established_by_period=period))
    db.session.commit()
    return True


def compute_performance(agreement: AdvisoryAgreement, quarter_end: date, *,
                        opening_aum: float, closing_aum: float,
                        contributions: float = 0.0,
                        withdrawals: float = 0.0) -> PerformanceSnapshot:
    """Compute and STORE one quarter's performance figure.

    The gate for a performance fee is two-sided, and BOTH must hold:

      1. HURDLE cleared — the portfolio's return beats the benchmark's.
      2. HIGH-WATER MARK cleared — closing AUM exceeds the prior peak, so the
         fee is charged only on genuinely new gains.

    When either fails, `performance_fee_accrued` is 0.0 and the snapshot says
    why. The fee, when earned, is `excess_return × performance_fee_rate ×
    average_aum` — recorded here; whether it is BOOKED is a separate, gated act
    (see accrue_performance_fee). Idempotent on (agreement, quarter)."""
    qstart = quarter_start(quarter_end)
    twr = ((closing_aum - opening_aum - contributions + withdrawals)
           / opening_aum) if opening_aum else 0.0
    hurdle = (hurdle_return(qstart, quarter_end)
              if agreement.hurdle_benchmark else 0.0)
    excess = round(twr - hurdle, 6)
    average_aum = round((opening_aum + closing_aum) / 2.0, 2)
    hwm_start = current_high_water_mark(agreement)
    above_hwm = (closing_aum > hwm_start) if agreement.high_water_mark_enabled \
        else True
    hurdle_cleared = excess > 0

    fee = 0.0
    if hurdle_cleared and above_hwm:
        fee = round(excess * float(agreement.performance_fee_rate or 0.0)
                    * average_aum, 2)
        fee = max(0.0, fee)

    period = quarter_label(quarter_end)
    if above_hwm and closing_aum > hwm_start:
        ratchet_high_water_mark(agreement, quarter_end, closing_aum, period)
    hwm_end = current_high_water_mark(agreement)

    snap = (PerformanceSnapshot.query
            .filter_by(agreement_id=agreement.id,
                       quarter_end=quarter_end).first())
    if snap is None:
        snap = PerformanceSnapshot(agreement_id=agreement.id,
                                   quarter_end=quarter_end)
        db.session.add(snap)
    snap.opening_aum = round(opening_aum, 2)
    snap.closing_aum = round(closing_aum, 2)
    snap.contributions = round(contributions, 2)
    snap.withdrawals = round(withdrawals, 2)
    snap.average_aum = average_aum
    snap.gross_return_pct = round(twr * 100, 4)
    snap.net_return_pct = round(twr * 100, 4)
    snap.hurdle_return_pct = round(hurdle * 100, 4)
    snap.excess_return_pct = round(excess * 100, 4)
    snap.performance_fee_accrued = fee
    snap.high_water_mark_at_start = round(hwm_start, 2)
    snap.high_water_mark_at_end = round(hwm_end, 2)
    snap.hurdle_cleared = hurdle_cleared
    snap.above_high_water_mark = bool(above_hwm)
    if fee > 0:
        snap.notes = 'performance fee earned'
    elif not hurdle_cleared:
        snap.notes = 'no performance fee — hurdle not cleared'
    else:
        snap.notes = 'no performance fee — below high-water mark'
    snap.updated_at = _now()
    db.session.commit()
    return snap


def accrue_performance_fee(snap: PerformanceSnapshot) -> AdvisoryFeeAccrual | None:
    """Record the performance fee from a snapshot as an AdvisoryFeeAccrual,
    keyed idempotently per quarter.

    Recorded regardless of the kill switch (it is data), but marked NOT posted:
    the performance fee is accrued quarterly and PAID annually subject to Client
    approval, so no quarterly JE is emitted here. The `performance_fee_enabled`
    switch and the annual approval flow gate the eventual posting."""
    if snap.performance_fee_accrued <= 0:
        return None
    agreement = db.session.get(AdvisoryAgreement, snap.agreement_id)
    period = snap.period_label()
    accrual = (AdvisoryFeeAccrual.query
               .filter_by(agreement_id=snap.agreement_id,
                          fee_type='performance', period_label=period).first())
    if accrual is None:
        accrual = AdvisoryFeeAccrual(
            agreement_id=snap.agreement_id, fee_type='performance',
            period_label=period, accrual_date=snap.quarter_end)
        db.session.add(accrual)
    accrual.amount = snap.performance_fee_accrued
    accrual.accrual_date = snap.quarter_end
    accrual.posted_to_erpnext = False
    enabled = bool(agreement and agreement.performance_fee_enabled)
    accrual.notes = ('accrued — pays annually on Client approval'
                     if enabled
                     else 'accrued — performance fee posting disabled')
    accrual.updated_at = _now()
    db.session.commit()
    return accrual


# ── risk controls ────────────────────────────────────────────────────────────

_DEFAULT_RISK = {'single_position_limit_pct': 10.0,
                 'sector_concentration_limit_pct': 25.0,
                 'bitcoin_allocation_pct': 5.0,
                 'new_entry_limit_pct': 2.5}


def run_risk_check(agreement: AdvisoryAgreement,
                   on: date | None = None) -> RiskControlCheck:
    """Compute the day's position concentrations and flag any that breach the
    agreement's limits. ALWAYS runs and records — the alert is what the kill
    switch gates, not the check. Idempotent on (agreement, date)."""
    on = on or date.today()
    cfg = {**_DEFAULT_RISK, **(agreement.risk_control_config or {})}
    holdings = []
    for aid in agreement.account_ids():
        holdings += SecurityHolding.query.filter_by(account_id=aid).all()
    concentrations = {}
    total = 0.0
    for h in holdings:
        value = float(h.institution_value or 0.0)
        if value <= 0:
            continue
        sec = Security.query.filter_by(security_id=h.security_id).first()
        ticker = (sec.ticker_symbol if sec else '') or h.security_id
        concentrations[ticker] = round(
            concentrations.get(ticker, 0.0) + value, 2)
        total += value

    violations = []
    single_limit = float(cfg['single_position_limit_pct'])
    btc_limit = float(cfg['bitcoin_allocation_pct'])
    pct = {}
    for ticker, value in concentrations.items():
        p = round(value / total * 100, 2) if total else 0.0
        pct[ticker] = p
        if p > single_limit:
            violations.append({
                'rule': 'single_position_limit', 'ticker': ticker,
                'pct': p, 'limit': single_limit,
                'action': f'trim {ticker} below {single_limit}% of the portfolio'})
        if ('BTC' in ticker.upper() or 'BITCOIN' in ticker.upper()) \
                and p > btc_limit:
            violations.append({
                'rule': 'bitcoin_allocation', 'ticker': ticker, 'pct': p,
                'limit': btc_limit,
                'action': f'reduce bitcoin exposure below {btc_limit}%'})

    row = (RiskControlCheck.query
           .filter_by(agreement_id=agreement.id, check_date=on).first())
    if row is None:
        row = RiskControlCheck(agreement_id=agreement.id, check_date=on)
        db.session.add(row)
    row.position_concentrations = pct
    row.single_position_limit_pct = single_limit
    row.sector_concentration_limit_pct = float(
        cfg['sector_concentration_limit_pct'])
    row.bitcoin_allocation_pct = btc_limit
    row.violations = violations
    db.session.commit()
    if violations and agreement.risk_control_alerts_enabled:
        audit.record('risk_control_violation',
                     subject_type='AdvisoryAgreement', subject_id=agreement.id,
                     after={'violations': violations, 'date': on.isoformat()},
                     notes=f'{len(violations)} risk-control violation(s)')
    return row


# ── dashboard assembly ───────────────────────────────────────────────────────

def dashboard(agreement: AdvisoryAgreement) -> dict:
    """Everything the /admin/advisory/<id> page shows, as one dict — so the
    page is a render of stored figures, never a recomputation."""
    aum = agreement_aum(agreement)
    year = date.today().year
    ytd_base = round(sum(
        a.amount for a in AdvisoryFeeAccrual.query.filter_by(
            agreement_id=agreement.id, fee_type='base').all()
        if a.accrual_date and a.accrual_date.year == year), 2)
    ytd_perf = round(sum(
        a.amount for a in AdvisoryFeeAccrual.query.filter_by(
            agreement_id=agreement.id, fee_type='performance').all()
        if a.accrual_date and a.accrual_date.year == year), 2)
    latest_snap = (PerformanceSnapshot.query
                   .filter_by(agreement_id=agreement.id)
                   .order_by(PerformanceSnapshot.quarter_end.desc()).first())
    latest_risk = (RiskControlCheck.query
                   .filter_by(agreement_id=agreement.id)
                   .order_by(RiskControlCheck.check_date.desc()).first())
    marks = (HighWaterMark.query
             .filter_by(agreement_id=agreement.id)
             .order_by(HighWaterMark.mark_date.asc()).all())
    accruals = (AdvisoryFeeAccrual.query
                .filter_by(agreement_id=agreement.id)
                .order_by(AdvisoryFeeAccrual.accrual_date.desc()).all())
    return {
        'agreement': agreement,
        'aum': aum,
        'ytd_base_fee': ytd_base,
        'ytd_performance_fee': ytd_perf,
        'high_water_mark': current_high_water_mark(agreement),
        'high_water_marks': [m.to_dict() for m in marks],
        'latest_snapshot': latest_snap.to_dict() if latest_snap else None,
        'risk_violations': list(latest_risk.violations or []) if latest_risk
        else [],
        'risk_check_date': (latest_risk.check_date.isoformat()
                            if latest_risk and latest_risk.check_date else ''),
        'accruals': [a.to_dict() for a in accruals],
    }
