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
from .models import (AdvisoryAgreement, AdvisoryFeeAccrual, DailyAUM,
                     GeneratedJournalEntry, HighWaterMark, HurdleRateSample,
                     PerformanceSnapshot, PlaidAccount, RiskControlCheck,
                     Security, SecurityHolding)

log = logging.getLogger('bankbridge.advisory')

JOURNAL_ENTRY_DT = 'Journal Entry'


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


def quarter_label(d: date) -> str:
    return f'{d.year}-Q{(d.month - 1) // 3 + 1}'


def quarter_start(d: date) -> date:
    return date(d.year, ((d.month - 1) // 3) * 3 + 1, 1)


# ── AUM sampling + base-fee accrual ──────────────────────────────────────────

def account_market_value(account_id: str) -> float:
    """The market value of one managed account: its security holdings at the
    last institution price, PLUS its cash sweep balance. A brokerage account's
    cash lives on a paired companion, so that companion's balance is included
    when the account names one."""
    total = 0.0
    for h in SecurityHolding.query.filter_by(account_id=account_id).all():
        if h.institution_value is not None:
            total += float(h.institution_value)
        elif h.quantity and h.institution_price:
            total += float(h.quantity) * float(h.institution_price)
    account = PlaidAccount.query.filter_by(account_id=account_id).first()
    if account is not None:
        total += float(account.balance_current or 0.0)
        partner_id = (account.paired_account_id or '').strip()
        if partner_id:
            partner = PlaidAccount.query.filter_by(
                account_id=partner_id).first()
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
