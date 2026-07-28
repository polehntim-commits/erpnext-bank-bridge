# SPDX-License-Identifier: MIT
"""Trade leg pairing — itemising the Cash Clearing imbalance (v0.8.0).

WHAT THIS IS FOR. `invest_je.clearing_imbalance` answers "how far is this
brokerage's Cash Clearing account from zero" with one number over all history.
That number is real and it is large — ••6030 sits at +$297,358.90 and ••9401 at
-$985,015.56 — and it is also unactionable. Nobody can chase a scalar. This
module decomposes it into the individual movements responsible, so
`list_unpaired_trades` can hand back the dates, securities and amounts that
someone can actually go and look up.

THE TWO LEGS. A trade in a PAIRED Wells Fargo Advisors brokerage moves money
across two Plaid accounts (see models.TradeLegPairing and invest_je's module
docstring):

    security leg   SecurityTransaction on the brokerage    /investments/…
    cash leg       BankTransaction on the cash companion   /transactions/sync

`invest_je` books each against `Cash Clearing - Brokerage` from opposite sides,
so a complete pair nets it to zero. Both legs present → nothing to report. One
leg present → its half sits in clearing forever, and that is a finding.

THE IDENTITY THIS PRESERVES. Every row's `delta` is `expected - actual` in
CASH IN POSITIVE terms, and the sum over an account reproduces
`clearing_imbalance` exactly:

    Σ delta  ==  Σ security_cash_in  -  Σ companion_cash_in  ==  clearing_imbalance

That is deliberate and it is tested. A decomposition that did not add back up to
the number on the account page would be a second opinion, not an explanation,
and an operator would have no way to know which to believe. It is also why the
matched types are exactly the four `invest_je` posts through clearing
(buy/sell/fee/cash) rather than just buy/sell: dropping fees and dividends would
be tidier and would silently stop the totals agreeing.

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

# The Plaid investment-transaction types whose cash leg `invest_je` routes
# through Cash Clearing. Kept in lockstep with invest_je.clearing_imbalance's
# `posted_types` — the Σ-delta identity in this module's docstring holds only
# while the two agree, so they are asserted equal in the test suite rather than
# left to drift.
CLEARING_TYPES = ('buy', 'sell', 'fee', 'cash')

# How far apart a trade and its settlement may sit and still be one movement.
# Equities settle T+1 (T+2 before May 2024) and a Friday trade settles the
# following week, so three BUSINESS days each way covers a normal settlement
# plus a holiday, without reaching so far that two same-sized trades in one week
# can claim each other's cash.
MATCH_WINDOW_BUSINESS_DAYS = 3

# Cents, not dollars. The two legs describe one movement and the custodian
# reports both to the cent; anything looser starts pairing a $1,000.00 buy with
# a $1,000.40 one.
AMOUNT_TOLERANCE = 0.005


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

    Returns {'accounts', 'paired', 'unpaired_security', 'unpaired_cash',
    'imbalance'}. Never raises: this runs on the sync path behind a fail-soft
    caller, and a pairing failure must not be able to fail a sync that
    otherwise worked."""
    stats = {'accounts': 0, 'paired': 0, 'unpaired_security': 0,
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
        for key in ('paired', 'unpaired_security', 'unpaired_cash'):
            stats[key] += one[key]
        stats['imbalance'] = round(stats['imbalance'] + one['imbalance'], 2)
    return stats


def _rebuild_account(account: PlaidAccount) -> dict:
    """One account's rows, rewritten. See `rebuild` for the contract."""
    from . import statements as stmts
    partner_id = (account.paired_account_id or '').strip()
    own_ids = list(stmts.supersede_chain(account.account_id))
    partner_ids = [a for a in stmts.supersede_chain(partner_id)
                   if a not in own_ids]

    TradeLegPairing.query.filter_by(account_id=account.account_id).delete(
        synchronize_session=False)

    security_legs = (SecurityTransaction.query
                     .filter(SecurityTransaction.account_id.in_(tuple(own_ids)),
                             SecurityTransaction.type.in_(CLEARING_TYPES))
                     .order_by(SecurityTransaction.date.asc(),
                               SecurityTransaction.id.asc())
                     .all())
    cash_legs = (BankTransaction.query
                 .filter(BankTransaction.account_id.in_(
                     tuple(partner_ids) or ('',)),
                         BankTransaction.pending.is_(False),
                         BankTransaction.removed.is_(False))
                 .order_by(BankTransaction.date.asc(),
                           BankTransaction.id.asc())
                 .all())
    if len(own_ids) > 1:
        security_legs = stmts.dedupe_across_accounts(security_legs)
    if len(partner_ids) > 1:
        cash_legs = stmts.dedupe_across_accounts(cash_legs)

    claimed: set = set()
    stats = {'paired': 0, 'unpaired_security': 0, 'unpaired_cash': 0,
             'imbalance': 0.0}

    for leg in security_legs:
        expected = _cash_in(leg.amount)
        match = _find_cash_leg(leg, expected, cash_legs, claimed)
        row = TradeLegPairing(
            account_id=account.account_id,
            cash_account_id=(match.account_id if match is not None
                             else partner_id),
            buy_or_sell=(leg.type or ''),
            security_id=leg.security_id,
            security_txn_id=leg.plaid_investment_transaction_id,
            trade_date=leg.date,
            expected_cash_amount=expected,
            cash_source='bank')
        if match is not None:
            claimed.add(match.id)
            actual = _cash_in(match.amount)
            row.cash_txn_id = match.plaid_transaction_id
            row.cash_date = match.effective_date()
            row.actual_cash_amount = actual
            row.missing_leg = ''
            row.status = 'paired'
            row.paired_at = _now()
            row.delta = round(expected - actual, 2)
            stats['paired'] += 1
        else:
            row.missing_leg = 'cash'
            row.status = 'unpaired'
            row.delta = expected
            row.notes = (f'no settlement within '
                         f'{MATCH_WINDOW_BUSINESS_DAYS} business days on the '
                         f'cash companion')
            stats['unpaired_security'] += 1
        stats['imbalance'] = round(stats['imbalance'] + row.delta, 2)
        db.session.add(row)

    # The other orphan kind, and the one a trade-keyed table would miss
    # entirely: companion cash with no security leg to explain it. This is what
    # ••9401's -$985k is made of, so it is not an edge case.
    for leg in cash_legs:
        if leg.id in claimed:
            continue
        actual = _cash_in(leg.amount)
        row = TradeLegPairing(
            account_id=account.account_id,
            cash_account_id=leg.account_id,
            buy_or_sell='',
            security_txn_id=None,
            cash_txn_id=leg.plaid_transaction_id,
            cash_source='bank',
            missing_leg='security',
            status='unpaired',
            trade_date=leg.effective_date(),
            cash_date=leg.effective_date(),
            expected_cash_amount=0.0,
            actual_cash_amount=actual,
            delta=round(-actual, 2),
            notes=(leg.name or '')[:500])
        stats['unpaired_cash'] += 1
        stats['imbalance'] = round(stats['imbalance'] + row.delta, 2)
        db.session.add(row)

    db.session.commit()
    return stats


def _find_cash_leg(leg: SecurityTransaction, expected: float, candidates: list,
                   claimed: set):
    """The unclaimed companion transaction that settles `leg`, or None.

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
    is the same number, arrived at in a way that can be itemised."""
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
    """{'paired', 'unpaired', 'unpaired_security', 'unpaired_cash',
    'unpaired_total'} for one account — the one-line verdict."""
    rows = pairings_for_account(account_id)
    unpaired = [r for r in rows if r.status != 'paired']
    return {
        'paired': sum(1 for r in rows if r.status == 'paired'),
        'unpaired': len(unpaired),
        'unpaired_security': sum(1 for r in unpaired
                                 if r.missing_leg == 'cash'),
        'unpaired_cash': sum(1 for r in unpaired
                             if r.missing_leg == 'security'),
        'unpaired_total': round(sum(float(r.delta or 0.0)
                                    for r in unpaired), 2),
    }
