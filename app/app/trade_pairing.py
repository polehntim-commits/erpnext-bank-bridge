# SPDX-License-Identifier: MIT
"""Trade leg pairing — itemising the Cash Clearing imbalance (v0.8.1).

WHAT THIS IS FOR. `invest_je.clearing_imbalance` answers "how far is this
brokerage's Cash Clearing account from zero" with one number over all history.
That number is real and it is large — and it is also unactionable. Nobody can
chase a scalar. This module decomposes it into the individual movements
responsible, so `list_unpaired_trades` can hand back the dates, securities and
amounts that someone can actually go and look up.

THE TWO LEGS, AND THE TWO PLACES A CUSTODIAN PUTS THEM. Every trade moves money
twice — once as securities, once as cash — and which Plaid feed carries the cash
half is a property of the CUSTODIAN, not of the trade:

  * SAME-ACCOUNT (`wf_same_account`) — Wells Fargo Advisors, and what OML's live
    data actually shows. BOTH legs arrive on the brokerage account from
    /investments/transactions/get: a `type=buy` row for the security movement
    and a second `type=cash` row, same `security_id`, same date, same magnitude,
    for the cash settlement. The cash-services companion carries none of it —
    ••3194 holds debit-card purchases and transfers, not trade settlements.

  * CROSS-ACCOUNT (`cross_account`) — the shape v0.8.0 assumed for everyone: the
    security leg on the brokerage, the cash leg as an 'Increase/Decrease from
    Brokerage activity' BankTransaction on the companion, T+1 or T+2 later.

v0.8.0 looked for the cross-account shape ONLY, so against Wells Fargo every
trade read as an orphan security leg (1,208 of them on ••9401) and every
ordinary companion transaction read as an orphan cash leg. The imbalance that
fell out was the account's aggregate signed movement — a real number computed
from real rows, and meaningless as a diagnostic. v0.8.1 tries same-account
first, because that is the bank we actually integrate with, and keeps
cross-account as the fallback so a custodian that does split the legs still
pairs.

WHAT COUNTS AS A CASH LEG ON THE COMPANION. Only its brokerage-activity lines
(see `_is_brokerage_activity`). A debit-card purchase on the cash-services
account is an ordinary depository transaction that never had a security leg and
never will; v0.8.0 counted every one of them as an orphaned trade leg, which is
where most of ••9401's -$985k came from. It is not a trade, so it is neither a
pairing candidate nor a finding.

THE IDENTITY THIS PRESERVES. Every row's `delta` is `expected - actual` in
CASH IN POSITIVE terms, and the sum over an account reproduces
`clearing_imbalance` exactly:

    Σ delta  ==  clearing_imbalance(account)

That is deliberate and it is tested. A decomposition that did not add back up to
the number on the account page would be a second opinion, not an explanation,
and an operator would have no way to know which to believe. v0.8.1 keeps the
identity by construction rather than by agreement: `clearing_imbalance` no
longer re-derives the total from its own sums, it calls
`projected_clearing_imbalance` here and sums the same rows this module writes.
The two cannot drift because there is only one calculation left.

That change was forced, not opportunistic. Under the same-account shape the old
formula counted BOTH halves of every Wells Fargo trade on the security side —
the `buy` and its `cash` settlement — so it double-counted every trade and then
subtracted the companion's grocery bill. Pairing the halves without fixing the
scalar would have left the itemisation right and the headline wrong.

WHICH ACCOUNTS GET ROWS. Paired brokerages only. An UNPAIRED investment account
carries both legs in one /investments/transactions/get row — Plaid's `amount` IS
the cash impact — so its trades are self-pairing by construction, `invest_je`
settles them straight against the account's own GL leaf, and
`clearing_imbalance` returns 0.0. Writing rows for one would assert a failure
mode it cannot have.

DERIVED, ALWAYS. Nothing here is a source of truth: every row is rebuildable
from SecurityTransaction plus BankTransaction, `rebuild` is idempotent, and the
table can be dropped and recomputed without losing information. It is a cache of
an analysis, not a record of an event.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from . import db
from .models import (BankTransaction, PlaidAccount, SecurityTransaction,
                     TradeLegPairing)

log = logging.getLogger('bankbridge.trade_pairing')

# The Plaid investment-transaction types that carry the SECURITY half of a
# trade, and the one that carries the CASH half. Wells Fargo emits one of each
# per trade on the same account; a custodian that settles cross-account emits
# only the first kind and puts the cash on the companion.
SECURITY_LEG_TYPES = ('buy', 'sell', 'fee')
CASH_LEG_TYPES = ('cash',)

# Every type whose cash leg `invest_je` routes through Cash Clearing. Kept in
# lockstep with invest_je's `posted_types` — the Σ-delta identity in this
# module's docstring holds only while the two agree, so they are asserted equal
# in the test suite rather than left to drift.
CLEARING_TYPES = SECURITY_LEG_TYPES + CASH_LEG_TYPES

# How far apart a trade and its settlement may sit and still be one movement.
# Equities settle T+1 (T+2 before May 2024) and a Friday trade settles the
# following week, so three BUSINESS days each way covers a normal settlement
# plus a holiday, without reaching so far that two same-sized trades in one week
# can claim each other's cash. Wells Fargo's two halves usually share a date
# outright, so the window is doing its real work on the cross-account fallback.
MATCH_WINDOW_BUSINESS_DAYS = 3

# Cents, not dollars. The two legs describe one movement and the custodian
# reports both to the cent; anything looser starts pairing a $1,000.00 buy with
# a $1,000.40 one.
AMOUNT_TOLERANCE = 0.005

# The one concession, and it is a penny. Same-account matching runs an EXACT
# pass over every leg before this looser pass runs over what is left, so a
# rounding-apart pair still matches while a penny of slack can never take a
# partner away from a leg that matched it to the cent.
AMOUNT_TOLERANCE_ROUNDING = 0.0100001

# What makes a companion transaction a trade settlement rather than the
# cardholder buying groceries. Wells Fargo names the sweep 'Increase from
# Brokerage activity' / 'Decrease from Brokerage activity'; matching the one
# distinctive word covers both directions and any custodian that words it
# differently around the same noun. A list, and a module constant, because the
# next custodian's wording is the thing most likely to need adding.
COMPANION_CASH_LEG_MARKERS = ('brokerage',)

# Plaid's investment `subtype` values that state a cash DIRECTION outright.
# Wells Fargo reports both halves of a buy as POSITIVE amounts — the security
# leg positive because cash left, the settlement leg positive because cash left
# — so the sign alone cannot say which way a `type=cash` row moved money. The
# subtype can, and does.
CASH_LEG_OUTFLOW_SUBTYPES = ('withdrawal',)
CASH_LEG_INFLOW_SUBTYPES = ('deposit',)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _business_day_window(anchor: date, days: int) -> tuple[date, date]:
    """(earliest, latest) calendar dates within `days` BUSINESS days of
    `anchor`, weekends excluded when counting but included in the span.

    Counting in business days and returning calendar bounds is what lets the
    caller filter with a plain date comparison while a Thursday trade still
    reaches the following Tuesday. Bank holidays are not modelled — a US
    holiday calendar is a dependency and a maintenance burden for at most one
    extra day of reach, and the amount match is doing the real work anyway."""
    def walk(start: date, step: int) -> date:
        cur, left = start, days
        while left > 0:
            cur += timedelta(days=step)
            if cur.weekday() < 5:          # Mon-Fri
                left -= 1
        return cur
    return walk(anchor, -1), walk(anchor, 1)


def _cash_in(amount) -> float:
    """Plaid's amount (positive = money OUT) as CASH IN POSITIVE — the one
    convention every figure in this module is expressed in, flipped here once
    rather than at each call site."""
    return round(-float(amount or 0.0), 2)


def _settlement_cash_in(leg: SecurityTransaction) -> float:
    """A `type=cash` investment row as CASH IN POSITIVE, reading the DIRECTION
    off `subtype` when it states one.

    Wells Fargo's two halves of a $35.47 buy are both `amount=+35.47`: the
    security leg positive because cash left the account, and the settlement leg
    positive for the same reason. Taken at face value under `_cash_in` that
    reads as $70.94 leaving, which is one trade counted twice. `subtype` is the
    field that disambiguates — 'withdrawal' is money out whatever the sign says,
    'deposit' is money in — so it wins where it is present.

    Anything else (a `dividend`, a bare `cash`) falls back to Plaid's sign
    convention, and the pairing test below then requires that fallback to AGREE
    with the security leg's own direction before the two can pair. An unknown
    subtype cannot smuggle a sign flip through."""
    sub = (leg.subtype or '').strip().lower()
    if sub in CASH_LEG_OUTFLOW_SUBTYPES:
        return round(-abs(float(leg.amount or 0.0)), 2)
    if sub in CASH_LEG_INFLOW_SUBTYPES:
        return round(abs(float(leg.amount or 0.0)), 2)
    return _cash_in(leg.amount)


def _is_brokerage_activity(txn: BankTransaction) -> bool:
    """Whether a companion transaction is a brokerage sweep — i.e. whether it is
    even eligible to be one half of a trade.

    The cash-services companion is a real checking account: debit-card
    purchases, ACH transfers and the rest run through it alongside the sweep.
    v0.8.0 treated all of it as trade legs, which is why ••9401's imbalance came
    out at the account's aggregate cash flow. A grocery bill has no security leg
    because it never was a trade, so it is neither a candidate to match nor a
    finding to report."""
    haystack = f'{txn.name or ""} {txn.merchant_name or ""}'.lower()
    return any(m in haystack for m in COMPANION_CASH_LEG_MARKERS)


def paired_brokerages(account_id: str | None = None) -> list:
    """Every account that has a cash-services companion, or just the one named.
    An account_id that resolves to something unpaired yields [] — there is
    nothing for this module to say about it."""
    q = PlaidAccount.query.filter(PlaidAccount.paired_account_id.isnot(None),
                                  PlaidAccount.paired_account_id != '')
    if account_id:
        q = q.filter(PlaidAccount.account_id == account_id)
    return q.all()


def rebuild(account_id: str | None = None) -> dict:
    """Recompute the pairing table for one paired brokerage, or every one.

    Idempotent: the whole account's rows are deleted and rewritten from the two
    source tables, so a re-run after a fresh Plaid pull converges rather than
    accumulating. That is cheaper than it sounds (the table is derived and small
    relative to the transaction mirror) and it is the only approach that stays
    correct when Plaid RESTATES a transaction — an incremental upsert would
    leave a pairing behind for a leg that no longer exists.

    Returns {'accounts', 'paired', 'paired_same_account',
    'paired_cross_account', 'unpaired_security', 'unpaired_cash', 'imbalance'}.
    The two `paired_*` counters are v0.8.1 and are how an operator sees WHICH
    shape a custodian actually uses without opening a single row. Never raises:
    this runs on the sync path behind a fail-soft caller, and a pairing failure
    must not be able to fail a sync that otherwise worked."""
    stats = {'accounts': 0, 'paired': 0, 'paired_same_account': 0,
             'paired_cross_account': 0, 'unpaired_security': 0,
             'unpaired_cash': 0, 'imbalance': 0.0}
    for account in paired_brokerages(account_id):
        try:
            one = _rebuild_account(account)
        except Exception:  # pragma: no cover - fail-soft
            db.session.rollback()
            log.warning('trade pairing failed for %s', account.account_id,
                        exc_info=True)
            continue
        stats['accounts'] += 1
        for key in ('paired', 'paired_same_account', 'paired_cross_account',
                    'unpaired_security', 'unpaired_cash'):
            stats[key] += one[key]
        stats['imbalance'] = round(stats['imbalance'] + one['imbalance'], 2)
    return stats


def _rebuild_account(account: PlaidAccount) -> dict:
    """One account's rows, rewritten. See `rebuild` for the contract."""
    rows, stats = _analyze_account(account)
    TradeLegPairing.query.filter_by(account_id=account.account_id).delete(
        synchronize_session=False)
    for row in rows:
        db.session.add(TradeLegPairing(**row))
    db.session.commit()
    return stats


def projected_clearing_imbalance(account_id: str) -> float:
    """Σ delta for one account, computed from the SOURCE tables — no read of
    `trade_leg_pairings` and no write to it.

    This is what `invest_je.clearing_imbalance` returns, and having it here is
    the whole reason the headline scalar and its itemisation cannot disagree:
    they are the same arithmetic over the same rows, run once. 0.0 for an
    account with no cash-services companion, which has no clearing account and
    therefore nothing to be out of balance."""
    accounts = paired_brokerages(account_id)
    if not accounts:
        return 0.0
    return round(sum(_analyze_account(a)[1]['imbalance'] for a in accounts), 2)


def _analyze_account(account: PlaidAccount) -> tuple[list[dict], dict]:
    """Pair one brokerage's legs. Returns (row kwargs, stats) and touches
    NOTHING — no session, no table. Both the rebuild path and the scalar
    `projected_clearing_imbalance` run through here, which is what keeps the
    two from drifting apart.

    Three passes, in the order the live data justifies:

      1. same-account, EXACT amount — Wells Fargo's shape, and the common case;
      2. same-account, within a rounding penny, over whatever pass 1 left;
      3. cross-account against the companion's brokerage-activity lines — the
         v0.8.0 behaviour, preserved for custodians that really do split the
         legs across two Plaid accounts.

    An exact pass ahead of the loose one is not fussiness: run as one pass, a
    penny of slack lets an approximate partner claim a leg that some other leg
    matched to the cent, and the orphan that falls out is an artifact of
    iteration order rather than a finding."""
    from . import statements as stmts
    partner_id = (account.paired_account_id or '').strip()
    own_ids = list(stmts.supersede_chain(account.account_id))
    partner_ids = [a for a in stmts.supersede_chain(partner_id)
                   if a not in own_ids]

    legs = (SecurityTransaction.query
            .filter(SecurityTransaction.account_id.in_(tuple(own_ids)),
                    SecurityTransaction.type.in_(CLEARING_TYPES))
            .order_by(SecurityTransaction.date.asc(),
                      SecurityTransaction.id.asc())
            .all())
    companion = (BankTransaction.query
                 .filter(BankTransaction.account_id.in_(
                     tuple(partner_ids) or ('',)),
                         BankTransaction.pending.is_(False),
                         BankTransaction.removed.is_(False))
                 .order_by(BankTransaction.date.asc(),
                           BankTransaction.id.asc())
                 .all())
    if len(own_ids) > 1:
        legs = stmts.dedupe_across_accounts(legs)
    if len(partner_ids) > 1:
        companion = stmts.dedupe_across_accounts(companion)
    # Ordinary depository traffic is not a trade leg — see
    # `_is_brokerage_activity`. Dropped here, once, so it can neither claim a
    # security leg nor surface as an orphan.
    companion = [t for t in companion if _is_brokerage_activity(t)]

    security_side = [t for t in legs if (t.type or '') in SECURITY_LEG_TYPES]
    cash_side = [t for t in legs if (t.type or '') in CASH_LEG_TYPES]

    settled: dict = {}       # security leg id -> its same-account cash partner
    claimed_cash: set = set()
    for tolerance in (AMOUNT_TOLERANCE, AMOUNT_TOLERANCE_ROUNDING):
        for leg in security_side:
            if leg.id in settled:
                continue
            match = _find_same_account_cash_leg(leg, cash_side, claimed_cash,
                                                tolerance)
            if match is not None:
                settled[leg.id] = match
                claimed_cash.add(match.id)

    rows: list[dict] = []
    stats = {'paired': 0, 'paired_same_account': 0, 'paired_cross_account': 0,
             'unpaired_security': 0, 'unpaired_cash': 0, 'imbalance': 0.0}

    def _tally(row: dict) -> None:
        stats['imbalance'] = round(stats['imbalance'] + row['delta'], 2)
        rows.append(row)

    # The cross-account pass runs over everything the same-account passes did
    # not settle, in the source order, so the fallback behaves byte-for-byte
    # like v0.8.0 on a custodian that has no same-account partners to find.
    claimed_companion: set = set()

    for leg in legs:
        if leg.id in claimed_cash:
            continue            # consumed as some buy's settlement half
        expected = _cash_in(leg.amount)
        row = {
            'account_id': account.account_id,
            'cash_account_id': partner_id,
            'buy_or_sell': (leg.type or ''),
            'security_id': leg.security_id,
            'security_txn_id': leg.plaid_investment_transaction_id,
            'trade_date': leg.date,
            'expected_cash_amount': expected,
            'cash_source': 'bank',
            'cash_txn_id': None,
            'cash_date': None,
            'actual_cash_amount': None,
            'missing_leg': '',
            'status': 'paired',
            'paired_at': None,
            'delta': 0.0,
            'notes': '',
        }
        same = settled.get(leg.id)
        if same is not None:
            # Wells Fargo. The partner is another investment row on this very
            # account, so `cash_txn_id` holds a plaid_investment_transaction_id
            # and `cash_source` says which table to look it up in.
            actual = _settlement_cash_in(same)
            row.update(cash_account_id=account.account_id,
                       cash_source='security',
                       cash_txn_id=same.plaid_investment_transaction_id,
                       cash_date=same.date, actual_cash_amount=actual,
                       paired_at=_now(), delta=round(expected - actual, 2))
            stats['paired'] += 1
            stats['paired_same_account'] += 1
            _tally(row)
            continue
        match = _find_cash_leg(leg, expected, companion, claimed_companion)
        if match is not None:
            claimed_companion.add(match.id)
            actual = _cash_in(match.amount)
            row.update(cash_account_id=match.account_id,
                       cash_txn_id=match.plaid_transaction_id,
                       cash_date=match.effective_date(),
                       actual_cash_amount=actual, paired_at=_now(),
                       delta=round(expected - actual, 2))
            stats['paired'] += 1
            stats['paired_cross_account'] += 1
            _tally(row)
            continue
        row.update(missing_leg='cash', status='unpaired', delta=expected,
                   notes=(f'no settlement within {MATCH_WINDOW_BUSINESS_DAYS} '
                          f'business days — no matching cash row on this '
                          f'account and none on the cash companion'))
        stats['unpaired_security'] += 1
        _tally(row)

    # The other orphan kind, and the one a trade-keyed table would miss
    # entirely: a brokerage-activity sweep on the companion with no security leg
    # to explain it. Under the cross-account shape this is a real finding — an
    # Item whose /investments/transactions/get was never granted looks exactly
    # like this. Under the same-account shape there is normally nothing here at
    # all, because the companion carries no settlements to begin with.
    for leg in companion:
        if leg.id in claimed_companion:
            continue
        actual = _cash_in(leg.amount)
        _tally({
            'account_id': account.account_id,
            'cash_account_id': leg.account_id,
            'buy_or_sell': '',
            'security_id': None,
            'security_txn_id': None,
            'cash_txn_id': leg.plaid_transaction_id,
            'cash_source': 'bank',
            'missing_leg': 'security',
            'status': 'unpaired',
            'paired_at': None,
            'trade_date': leg.effective_date(),
            'cash_date': leg.effective_date(),
            'expected_cash_amount': 0.0,
            'actual_cash_amount': actual,
            'delta': round(-actual, 2),
            'notes': (leg.name or '')[:500],
        })
        stats['unpaired_cash'] += 1

    return rows, stats


def _find_same_account_cash_leg(leg: SecurityTransaction, candidates: list,
                                claimed: set, tolerance: float):
    """The unclaimed `type=cash` row on the SAME account that settles `leg`, or
    None — the Wells Fargo pattern.

    Requires an exact `security_id`, a matching currency, a magnitude inside
    `tolerance`, and a date inside the settlement window. A `type=cash` row with
    NO security_id is a plain deposit or withdrawal that never had a trade
    behind it, so it can never be anyone's settlement half and is not a
    candidate; the same goes for a security leg carrying no security_id.

    The direction test is the subtle one. `_settlement_cash_in` reads 'withdrawal'
    and 'deposit' as directions outright, which is what makes Wells Fargo's
    both-halves-positive convention pair; for any OTHER subtype it falls back to
    Plaid's sign, and the comparison below then insists that fallback agree with
    the security leg. So a `dividend` whose sign says money came IN cannot pair
    with a buy that says money went OUT, however neatly the magnitudes line up.

    Nearest-in-time wins, ties broken toward the LATER date — settlement follows
    a trade. Greedy rather than globally optimal, for the reason in
    `_find_cash_leg`."""
    if leg.date is None or not (leg.security_id or '').strip():
        return None
    expected = _cash_in(leg.amount)
    earliest, latest = _business_day_window(leg.date,
                                            MATCH_WINDOW_BUSINESS_DAYS)
    currency = (leg.iso_currency_code or 'USD').strip().upper()
    best = None
    best_key = None
    for cand in candidates:
        if cand.id in claimed:
            continue
        if (cand.security_id or '') != (leg.security_id or ''):
            continue
        if (cand.iso_currency_code or 'USD').strip().upper() != currency:
            continue
        if cand.date is None or cand.date < earliest or cand.date > latest:
            continue
        if abs(abs(float(cand.amount or 0.0)) - abs(expected)) > tolerance:
            continue
        if abs(_settlement_cash_in(cand) - expected) > tolerance:
            continue
        key = (abs((cand.date - leg.date).days), -(cand.date - leg.date).days)
        if best_key is None or key < best_key:
            best, best_key = cand, key
    return best


def _find_cash_leg(leg: SecurityTransaction, expected: float, candidates: list,
                   claimed: set):
    """The unclaimed companion transaction that settles `leg`, or None — the
    cross-account fallback, unchanged from v0.8.0 except that `candidates` now
    arrives already filtered to brokerage activity.

    Nearest-in-time among exact-amount matches, ties broken toward the LATER
    date. Settlement follows a trade, so when a $1,000 debit sits one day before
    and one day after a $1,000 buy, the one after is the settlement and the one
    before belongs to some earlier trade — a rule that costs nothing and is
    right more often than an arbitrary tiebreak.

    Greedy and single-pass rather than a global optimum. The matching is exact
    on amount and tight on date, so the cases where greedy differs from optimal
    are cases where two identical trades settled in one window and either
    assignment yields the same set of orphans and the same Σ delta — which is
    the only property downstream depends on."""
    if leg.date is None:
        return None
    earliest, latest = _business_day_window(leg.date,
                                            MATCH_WINDOW_BUSINESS_DAYS)
    best = None
    best_key = None
    for cand in candidates:
        if cand.id in claimed:
            continue
        when = cand.effective_date()
        if when is None or when < earliest or when > latest:
            continue
        if abs(_cash_in(cand.amount) - expected) > AMOUNT_TOLERANCE:
            continue
        # (distance, prefer-later) — min() on distance, then on the negated
        # signed offset so a tie resolves to the later date.
        key = (abs((when - leg.date).days), -(when - leg.date).days)
        if best_key is None or key < best_key:
            best, best_key = cand, key
    return best


# ── read paths ──────────────────────────────────────────────────────────────

def pairings_for_account(account_id: str, *, unpaired_only: bool = False,
                         from_date: date | None = None,
                         to_date: date | None = None) -> list:
    """One account's pairing rows, oldest first. Date bounds filter on
    `trade_date`, which is the security leg's trade date on a trade-keyed row
    and the cash leg's effective date on an orphan cash row — in both cases the
    date an operator would look the movement up under."""
    q = TradeLegPairing.query.filter_by(account_id=account_id)
    if unpaired_only:
        q = q.filter(TradeLegPairing.status != 'paired')
    if from_date is not None:
        q = q.filter(TradeLegPairing.trade_date >= from_date)
    if to_date is not None:
        q = q.filter(TradeLegPairing.trade_date <= to_date)
    return q.order_by(TradeLegPairing.trade_date.asc().nullslast(),
                      TradeLegPairing.id.asc()).all()


def unpaired_total(account_id: str) -> float:
    """Σ delta over an account's UNPAIRED rows — the Cash Clearing balance
    recomputed from the movements that are actually responsible for it.

    Equal to `invest_je.clearing_imbalance(account_id)` whenever the table is
    current, because a paired row's delta is zero by construction. Computing it
    from the unpaired rows alone rather than from all of them is the point: it
    is the same number, arrived at in a way that can be itemised. When it is NOT
    equal, the table is stale and a rebuild is the fix — which is exactly what
    `list_unpaired_trades`' `totals_agree` reports."""
    return round(sum(float(p.delta or 0.0)
                     for p in pairings_for_account(account_id,
                                                   unpaired_only=True)), 2)


def days_since(row: TradeLegPairing, as_of: date | None = None) -> int | None:
    """How long an unpaired leg has been waiting. None when it carries no date.

    Age is what separates "settlement hasn't landed yet" from "this is never
    going to settle": a two-day-old orphan is normal T+1 latency, a two-year-old
    one is a missing endpoint call."""
    when = row.trade_date or row.cash_date
    if when is None:
        return None
    return ((as_of or date.today()) - when).days


def summary(account_id: str) -> dict:
    """{'paired', 'paired_same_account', 'paired_cross_account', 'unpaired',
    'unpaired_security', 'unpaired_cash', 'unpaired_total'} for one account —
    the one-line verdict.

    The `paired_*` split is v0.8.1 and answers the question that matters when a
    number looks wrong: which shape is this custodian actually using? An account
    reading all-cross-account when the operator expected Wells Fargo means the
    same-account passes found nothing, and that is a different investigation
    from a genuine missing leg."""
    rows = pairings_for_account(account_id)
    unpaired = [r for r in rows if r.status != 'paired']
    paired = [r for r in rows if r.status == 'paired']
    return {
        'paired': len(paired),
        'paired_same_account': sum(1 for r in paired
                                   if r.pairing_scheme() == 'wf_same_account'),
        'paired_cross_account': sum(1 for r in paired
                                    if r.pairing_scheme() == 'cross_account'),
        'unpaired': len(unpaired),
        'unpaired_security': sum(1 for r in unpaired
                                 if r.missing_leg == 'cash'),
        'unpaired_cash': sum(1 for r in unpaired
                             if r.missing_leg == 'security'),
        'unpaired_total': round(sum(float(r.delta or 0.0)
                                    for r in unpaired), 2),
    }
