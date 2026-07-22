# SPDX-License-Identifier: MIT
"""Strategy detection engine (v0.4.31 · v0.5.0 Phase B skeleton).

Reads SecurityTransaction rows produced by Phase A's investments sync,
identifies:
  * completed 5:4 cycles (buy N, sell ~0.8N at ~25% profit)  → TradedCycle
  * open cycles (buy N, no matching sell yet)                 → open TradedCycle
  * retained lots (the 20% kept after each 5:4 cycle)         → RetainedLot
  * options positions (short calls, cash-secured puts, longs) → OptionsPosition
  * options income events (sell_to_open, buy_to_close, etc.)  → OptionsIncomeEntry

This module ships as a skeleton in v0.4.31 — the model registry, config
loading, orchestration entrypoint, and result-recording plumbing are wired,
but the actual detection functions are stubbed. v0.4.32 implements the 5:4
detector, v0.4.33 the options detector + naked-position guard, and v0.4.34
the strategy dashboard UI.

Idempotency: the detector is safe to re-run. On each invocation it:
  1. Deletes existing derived rows (TradedCycle, RetainedLot,
     OptionsPosition, OptionsIncomeEntry) — the ground truth is the
     SecurityTransaction table; derived state is regenerated fresh so a
     re-run under changed settings reflects the change.
  2. Re-computes from the raw transaction history.
  3. Writes a StrategyTracker row with the run's stats + config snapshot.

The delete-and-recompute posture is deliberately more expensive than an
incremental detector but keeps the code straightforward and the results
auditable. When the transaction table grows to millions of rows the
detector can be optimized to incremental; at Bank Bridge's expected scale
(one operator, 10-20 trades/month, decades of history = <10k rows) the
full re-compute is fast."""
import logging
from datetime import date, timedelta

from . import db, strategy_settings
from .models import (OptionsIncomeEntry, OptionsPosition, RetainedLot,
                     Security, SecurityHolding, SecurityTransaction,
                     StrategyTracker, TradedCycle)

log = logging.getLogger('bankbridge.strategy_tracker')


# ── v0.4.32 · 5:4 detector ──────────────────────────────────────────────────
#
# The core matching problem: for every buy on security S at account A, find
# the sell (also on S, same account) that closes the 5:4 cycle. The rules
# encode the operator's strategy: sell ~0.8 of the buy quantity at ~1.25× the
# buy price, within the matching window (default 180 days).
#
# Matching is FIFO by buy date within a (account, security) group. First-fit
# matching keeps the classifier deterministic — a re-run under unchanged
# settings produces bit-identical output, which is the key to making a
# delete-and-recompute detector safe to re-run at any time.
#
# The algorithm walks each (account, security) group once, maintaining a
# rolling list of unmatched buys. For each sell we see, we look back through
# unmatched buys for the oldest one that satisfies the 5:4 tolerances. If
# found → matched, create TradedCycle + RetainedLot for the retained portion.
# If not found → the sell is either an operator override (early exit, tax-
# harvesting) or a non-5:4 sell we can't classify; we mark any related buys
# as 'partial' cycles.
#
# Corporate actions (splits, transfers) are treated as neither buys nor sells
# — Plaid classifies them as 'transfer' or 'cash' type; only type='buy' and
# type='sell' feed the detector.
#
# Options transactions go to a separate detector (v0.4.33) — the check
# `not sec.is_option` filters them out here so a written call doesn't
# accidentally match against an underlying stock buy.


def _is_noise_transaction(txn) -> bool:
    """True when a transaction is likely NOT a real 5:4-eligible trade —
    dividends, splits, corporate actions, transfers, fees, and other events
    Plaid ships with type='buy' or type='sell' but that aren't investable
    activity. Filtering these out UPSTREAM of the matcher prevents them
    from getting FIFO-paired against unrelated real buys and producing
    fictional realized_pnl (the 22,252% PYPL "gain" that was really a
    stock split, or the -98% BCC "loss" that was really a dividend).

    Detection has three checks, any of which trips 'noise':

      1. Plaid subtype is a known non-trade event (dividend, split,
         transfer, reinvest, contribution, withdrawal, fee, assignment).
      2. The sell/buy quantity is a tiny fraction (< 1 share and cash
         impact < $100). Dividends in Plaid's investment feed often show
         as a fractional 'sell' of the security paying the dividend.
      3. The price is unreasonably low (< $0.50) — typical of dividend
         per-share values misidentified as trade prices.

    Options are handled separately by the options detector (v0.4.33); this
    predicate is called only after the options filter."""
    subtype = (txn.subtype or '').lower().strip()
    # Substring match — Plaid uses composite labels like 'dividend
    # reinvestment' and 'stock dividend' which don't fit a fixed set.
    # Any keyword hit in the subtype counts as a non-trade event.
    for needle in ('dividend', 'reinvest', 'split', 'transfer',
                   'contribution', 'withdrawal', 'fee', 'assignment',
                   'exercise', 'expiration', 'interest',
                   'return of capital', 'merger', 'spin off'):
        if needle in subtype:
            return True
    # Fractional / tiny quantities with small cash impact — typical of
    # dividend fractional-share settlements.
    if abs(txn.quantity) < 1.0 and abs(txn.amount) < 100:
        return True
    # Prices below the sub-dollar threshold are usually dividend per-share
    # values misidentified as trade prices, not penny stock trades.
    if 0 < abs(txn.price) < 0.50:
        return True
    return False


def _detect_54_cycles(txns, config, securities_by_id):
    """Walk buy/sell transactions and produce TradedCycle + RetainedLot rows.

    Returns (cycles_created, retained_lots_created)."""
    retain_ratio = float(config['retain_ratio'])
    profit_target = float(config['profit_target_pct']) / 100.0
    qty_tol = float(config['matching_tolerance_qty_pct']) / 100.0
    price_tol = float(config['matching_tolerance_price_pct']) / 100.0
    window_days = int(config['matching_window_days'])

    # Group transactions by (account_id, security_id). Skip options — those
    # are the options detector's job (v0.4.33), and mixing them here would
    # miscategorize a covered-call assignment as a 5:4 sell. Also skip
    # noise transactions (dividends, splits, corporate actions) — see
    # _is_noise_transaction for the filter list.
    by_group: dict = {}
    for t in txns:
        sec = securities_by_id.get(t.security_id)
        if sec is None or sec.is_option:
            continue
        if t.type not in ('buy', 'sell'):
            continue
        if t.date is None or t.quantity == 0 or t.price == 0:
            continue
        if _is_noise_transaction(t):
            continue
        key = (t.account_id, t.security_id)
        by_group.setdefault(key, []).append(t)

    cycles_created = 0
    retained_created = 0

    for (account_id, security_id), group in by_group.items():
        # Sort by date; a buy dated the same day as a sell posts first so the
        # matching walk sees it as available.
        group.sort(key=lambda r: (r.date, 0 if r.type == 'buy' else 1, r.id))
        unmatched_buys = []
        for txn in group:
            if txn.type == 'buy':
                unmatched_buys.append(txn)
                continue
            # A sell. Find the oldest unmatched buy that satisfies the 5:4
            # tolerances relative to this sell.
            sell = txn
            match_idx = None
            for idx, buy in enumerate(unmatched_buys):
                if (sell.date - buy.date).days > window_days:
                    continue
                if not _matches_54(buy, sell, retain_ratio, profit_target,
                                   qty_tol, price_tol):
                    continue
                match_idx = idx
                break
            if match_idx is None:
                # A sell we can't classify as 5:4. Could be an early exit, a
                # rebalance, a tax-harvest. Record a 'partial' TradedCycle
                # against the OLDEST unmatched buy (so the historical view
                # still attributes the sell to a specific purchase) if any
                # exists; otherwise skip (a naked sell with no covering buy
                # is data corruption we shouldn't invent a story about).
                if unmatched_buys:
                    buy = unmatched_buys.pop(0)
                    _record_cycle(buy, sell, retain_ratio_actual=None,
                                  status='partial', securities_by_id=securities_by_id)
                    cycles_created += 1
                continue
            # Match found. Create a completed cycle. The retained portion is
            # (buy.quantity - sell.quantity) shares.
            buy = unmatched_buys.pop(match_idx)
            retained_qty = round(buy.quantity - sell.quantity, 4)
            actual_ratio = (retained_qty / buy.quantity
                            if buy.quantity else 0.0)
            cycle = _record_cycle(buy, sell,
                                  retain_ratio_actual=round(actual_ratio, 4),
                                  status='complete',
                                  securities_by_id=securities_by_id)
            cycles_created += 1
            if retained_qty > 0.0001:
                _record_retained_lot(buy, retained_qty, cycle,
                                     strategy_tag='5_4')
                retained_created += 1

        # Any buys left unmatched at the end of the walk are OPEN cycles —
        # positions still held that never got a matching sell. Cross-check
        # against the current SecurityHolding: if shares_remaining > 0, this
        # buy is still active; record as 'open'. If holdings are zero, the
        # sell happened outside the tolerance window and we already recorded
        # a 'partial' above.
        current_qty = _current_holding_qty(account_id, security_id)
        for buy in unmatched_buys:
            if current_qty <= 0.0001:
                # Position is fully closed — the sell must be somewhere,
                # already classified as partial. Skip to avoid duplicating.
                continue
            _record_cycle(buy, None, retain_ratio_actual=None,
                          status='open', securities_by_id=securities_by_id)
            cycles_created += 1
            # Open buys are also implicit retained lots — the operator is
            # holding them, whether by choice or waiting for the sell target.
            _record_retained_lot(buy, buy.quantity, None,
                                 strategy_tag='5_4_open')
            retained_created += 1

    return cycles_created, retained_created


def _matches_54(buy, sell, retain_ratio, profit_target,
                qty_tol, price_tol) -> bool:
    """True when `sell` looks like the 4-of-5 half of a 5:4 cycle for `buy`.
    Quantity: sell qty ≈ buy qty × (1 - retain_ratio) within qty_tol.
    Price:    sell price ≈ buy price × (1 + profit_target)   within price_tol.
    Ratios are compared as ABS(observed - expected) / expected < tol."""
    if buy.quantity == 0 or buy.price == 0:
        return False
    expected_sell_qty = buy.quantity * (1.0 - retain_ratio)
    expected_sell_price = buy.price * (1.0 + profit_target)
    qty_delta = abs(abs(sell.quantity) - expected_sell_qty) / expected_sell_qty
    price_delta = abs(sell.price - expected_sell_price) / expected_sell_price
    return qty_delta <= qty_tol and price_delta <= price_tol


def _record_cycle(buy, sell, *, retain_ratio_actual, status,
                  securities_by_id) -> TradedCycle:
    """Create a TradedCycle row for a matched (or open) buy/sell pair.
    Returns the created row so caller can link a RetainedLot to it.

    realized_pnl handling (v0.4.33 cleanup):
      * status='complete' → compute proceeds − (sell_qty × buy_price).
        The buy_price is a legitimate cost basis because the 5:4 match
        confirms this buy is the source of the sold shares.
      * status='partial'  → leave realized_pnl = NULL. The FIFO pairing
        with the oldest unmatched buy is inherently ambiguous — that
        buy is NOT necessarily the source of the sold shares (could be
        a different lot at a different price). Reporting a P&L number
        would mislead. The cycle_status='partial' flag surfaces that
        this sell isn't strictly 5:4, without pretending to know the
        exact cost basis.
      * status='open'     → no sell, no realized P&L (always NULL)."""
    realized = None
    if sell is not None and status == 'complete':
        proceeds = abs(sell.amount) if sell.amount else abs(
            sell.quantity * sell.price)
        cost_of_sold = abs(sell.quantity) * buy.price
        realized = round(proceeds - cost_of_sold, 2)
    cycle = TradedCycle(
        security_id=buy.security_id,
        buy_transaction_id=buy.plaid_investment_transaction_id,
        sell_transaction_id=(sell.plaid_investment_transaction_id
                             if sell is not None else None),
        buy_date=buy.date, buy_qty=buy.quantity, buy_price=buy.price,
        sell_date=sell.date if sell is not None else None,
        sell_qty=abs(sell.quantity) if sell is not None else None,
        sell_price=sell.price if sell is not None else None,
        realized_pnl=realized,
        retain_ratio_actual=retain_ratio_actual,
        cycle_status=status,
    )
    db.session.add(cycle)
    db.session.flush()  # populate cycle.id for RetainedLot FK
    return cycle


def _record_retained_lot(buy, retained_qty, cycle, *, strategy_tag) -> None:
    """Create a RetainedLot row for the kept shares of a 5:4 cycle (or
    an open position being tracked as an implicit retained lot)."""
    lot = RetainedLot(
        security_id=buy.security_id,
        account_id=buy.account_id,
        purchase_date=buy.date,
        cost_basis_per_share=buy.price,
        shares_original=retained_qty,
        shares_remaining=retained_qty,
        source_cycle_id=cycle.id if cycle is not None else None,
        strategy_tag=strategy_tag,
    )
    db.session.add(lot)


def _current_holding_qty(account_id, security_id) -> float:
    """Latest known SecurityHolding.quantity for this pair, or 0 if none."""
    h = (SecurityHolding.query
         .filter_by(account_id=account_id, security_id=security_id)
         .first())
    return float(h.quantity) if h and h.quantity is not None else 0.0


# ── v0.4.34 · options detector ──────────────────────────────────────────────
#
# Walks SecurityTransaction rows where the linked Security has is_option=True.
# Produces OptionsPosition rows (one per account+security) with the current
# net position + total premium accounting, plus per-event OptionsIncomeEntry
# rows for the audit trail.
#
# Plaid's investment transactions for options are typed as:
#   type='buy'  → buy-to-open (going long) OR buy-to-close (closing a short)
#   type='sell' → sell-to-open (writing a short) OR sell-to-close (closing long)
#   type='cash' → expiration, assignment, exercise events (with subtype)
#
# Direction is inferred from the running signed position: a sell when the
# current contracts_open >= 0 is opening a short (STO), a sell when it's < 0
# is closing a long (STC), etc. This works because Plaid ships events in
# chronological order and each event updates the running position.
#
# Naked-position guard (v0.4.34): for a SHORT position (contracts_open < 0),
# checks the current underlying share coverage (short call) or cash-secured
# coverage (short put). is_naked = True when coverage is insufficient. The
# operator's stated 'no naked options, no margin' rule turns any is_naked=True
# into an alert on the strategy dashboard.


def _detect_options(txns, config, securities_by_id):
    """Walk options transactions AND current holdings to produce
    OptionsPosition + OptionsIncomeEntry rows. Returns
    (positions_touched, naked_flagged).

    v0.4.35 fix: Plaid's WF Advisors integration (and many others) ships
    current option positions via /investments/holdings/get but skips the
    historical trade events on /investments/transactions/get for older
    contracts. Tim's data confirms this: 15 option contracts show as
    SecurityHoldings (mostly short — written calls + puts) with zero
    corresponding SecurityTransaction rows. Detecting only from
    transactions leaves the actual current position invisible.

    Approach:
      1. Seed OptionsPositions from CURRENT HOLDINGS for every option
         security with a non-zero SecurityHolding. This guarantees the
         actual position is recorded regardless of transaction history.
      2. If transactions exist, walk them for premium accounting (history
         improves the picture — total premium received, income entries).
      3. Naked-position check uses the current position from step 1 +
         current SecurityHolding for the underlying.

    Positions with holdings but no transactions get NULL premium totals
    with a note explaining Plaid didn't ship the historical events."""
    if not config.get('options_writing_enabled', True):
        return 0, 0

    # Group option transactions by (account_id, security_id).
    by_group: dict = {}
    for t in txns:
        sec = securities_by_id.get(t.security_id)
        if sec is None or not sec.is_option:
            continue
        if t.date is None:
            continue
        key = (t.account_id, t.security_id)
        by_group.setdefault(key, []).append(t)

    # v0.4.35: seed the (account, option_security) universe from HOLDINGS
    # so positions without transaction history still get recorded. Union
    # with the transaction-derived keys above.
    for h in SecurityHolding.query.all():
        sec = securities_by_id.get(h.security_id)
        if sec is None or not sec.is_option:
            continue
        if h.quantity is None or abs(h.quantity) < 0.0001:
            continue
        key = (h.account_id, h.security_id)
        by_group.setdefault(key, [])

    positions_touched = 0
    naked_flagged = 0
    for (account_id, security_id), group in by_group.items():
        group.sort(key=lambda r: (r.date, r.id))
        sec = securities_by_id[security_id]

        # v0.4.35 · when we have no transaction history for this option
        # (holdings-only path), seed contracts_open from the current
        # SecurityHolding.quantity. Premium totals are unknown in that
        # case — we didn't observe the open events — and we note it.
        current_qty = _current_holding_qty(account_id, security_id)
        holdings_only = len(group) == 0

        opened_at = group[0].date if group else None
        pos = OptionsPosition(
            security_id=security_id,
            account_id=account_id,
            contract_type=sec.option_contract_type,
            underlying_ticker=sec.option_underlying_ticker or sec.ticker_symbol,
            strike_price=sec.option_strike_price,
            expiration_date=sec.option_expiration_date,
            opened_at=opened_at,
            status='open',
            notes=('Position inferred from current holdings — Plaid did '
                   'not ship historical trade events for this contract, '
                   'so premium totals below are unknown (NULL).'
                   if holdings_only else ''),
        )
        db.session.add(pos)
        db.session.flush()   # pos.id for OptionsIncomeEntry FK

        running_contracts = 0.0
        premium_received = 0.0
        premium_paid = 0.0

        for txn in group:
            action, contracts_delta, premium_impact = _classify_options_event(
                txn, running_contracts)
            if action is None:
                continue
            running_contracts += contracts_delta
            if premium_impact > 0:
                premium_received += premium_impact
            else:
                premium_paid += abs(premium_impact)
            entry = OptionsIncomeEntry(
                options_position_id=pos.id,
                plaid_investment_transaction_id=(
                    txn.plaid_investment_transaction_id),
                date=txn.date,
                action=action,
                contracts=abs(txn.quantity),
                premium_per_contract=(abs(txn.price)
                                      if txn.price else None),
                total_premium=abs(txn.amount) if txn.amount else None,
                realized=action in ('buy_to_close', 'expired_worthless',
                                    'assigned', 'exercised'),
            )
            db.session.add(entry)

        # If we walked transactions, use their running total. If holdings-
        # only, use the current SecurityHolding quantity directly.
        pos.contracts_open = (round(running_contracts, 4) if not holdings_only
                              else round(current_qty, 4))
        pos.premium_received_total = (round(premium_received, 2)
                                      if not holdings_only else None)
        pos.premium_paid_total = (round(premium_paid, 2)
                                  if not holdings_only else None)
        pos.net_premium = (round(premium_received - premium_paid, 2)
                           if not holdings_only else None)

        # Status: closed when contracts_open reaches zero; otherwise open.
        # A more nuanced closed-reason (expired/assigned/exercised) can be
        # derived from the last OptionsIncomeEntry.action if needed.
        if abs(pos.contracts_open) < 0.0001:
            pos.status = 'closed'
            pos.closed_at = group[-1].date if group else None

        positions_touched += 1

    # v0.4.36 · aggregate naked-position guard. First pass built every
    # OptionsPosition; second pass now checks coverage ACROSS positions
    # that share an underlying (multi-strike / multi-tenor short calls
    # collectively need N × 100 shares, not each independently). Same
    # for cash-secured puts sharing an account's cash pool.
    #
    # A single-position naive check said "each call I have 100 shares,
    # each check passes" and missed the real total. This aggregate
    # allocates coverage per (account, underlying) for calls and per
    # account for puts, then apportions the shortfall.
    #
    # Allocation policy: FIFO by expiration date ascending — the
    # SOONEST-EXPIRING short is covered first, since it's the one that
    # would need the shares/cash first. Later expirations that can't be
    # covered from the remaining pool are flagged is_naked.
    if config.get('flag_naked_positions', True):
        naked_flagged = _apply_aggregate_naked_check(securities_by_id)
    return positions_touched, naked_flagged


def _apply_aggregate_naked_check(securities_by_id) -> int:
    """Second-pass naked check across all OptionsPositions. Aggregates
    coverage per (account, underlying) for calls and per account for
    puts, then flags is_naked on positions where the shared coverage
    pool runs out. Returns the count of positions flagged naked."""
    all_positions = OptionsPosition.query.all()

    # --- SHORT CALLS: coverage from underlying shares ---
    # Group by (account_id, underlying_ticker); allocate FIFO by
    # expiration date ascending.
    call_groups: dict = {}
    for pos in all_positions:
        if pos.contract_type != 'call' or pos.contracts_open >= 0:
            continue
        key = (pos.account_id, (pos.underlying_ticker or '').upper())
        call_groups.setdefault(key, []).append(pos)

    naked = 0
    for (account_id, underlying), positions in call_groups.items():
        # Total shares held of the underlying, this account.
        available_shares = _underlying_share_coverage(
            account_id, underlying, securities_by_id)
        # Snapshot the pool BEFORE consumption so every position sees
        # the same aggregate value in its covered_by_shares field.
        pool_snapshot = available_shares
        remaining = available_shares
        # Allocate FIFO by expiration ascending; nearest-expiring first.
        positions.sort(
            key=lambda p: (p.expiration_date or __import__('datetime').date.max))
        for pos in positions:
            required = int(abs(pos.contracts_open) * 100)
            allocated = min(required, remaining)
            remaining -= allocated
            pos.covered_by_shares = allocated
            pos.is_naked = allocated < required
            if pos.is_naked:
                naked += 1
            # Record the total pool on notes so an operator can see the
            # underlying's total coverage vs. sum of required shares.
            existing = (pos.notes or '')
            coverage_line = (
                f'\n[coverage] {underlying}: pool={pool_snapshot} shares '
                f'across {len(positions)} short call(s) — allocated '
                f'{allocated}/{required} to this position (FIFO by '
                f'expiration).')
            pos.notes = (existing + coverage_line).strip()

    # --- SHORT PUTS: coverage from account cash ---
    put_groups: dict = {}
    for pos in all_positions:
        if pos.contract_type != 'put' or pos.contracts_open >= 0:
            continue
        put_groups.setdefault(pos.account_id, []).append(pos)

    for account_id, positions in put_groups.items():
        available_cash = _account_cash_coverage(account_id)
        pool_snapshot = available_cash
        remaining = available_cash
        positions.sort(
            key=lambda p: (p.expiration_date or __import__('datetime').date.max))
        for pos in positions:
            required = (float(pos.strike_price or 0.0) *
                        abs(pos.contracts_open) * 100)
            allocated = min(required, remaining)
            remaining -= allocated
            pos.covered_by_cash = round(allocated, 2)
            pos.is_naked = allocated < required
            if pos.is_naked:
                naked += 1
            existing = (pos.notes or '')
            coverage_line = (
                f'\n[coverage] account cash pool='
                f'{pool_snapshot:.2f} across {len(positions)} short put(s) — '
                f'allocated {allocated:.2f}/{required:.2f} to this '
                f'position (FIFO by expiration).')
            pos.notes = (existing + coverage_line).strip()

    return naked


def _classify_options_event(txn, running_contracts):
    """Return (action, contracts_delta, premium_impact) for one options
    transaction, given the current running_contracts.

    action ∈ {sell_to_open, buy_to_close, buy_to_open, sell_to_close,
              expired_worthless, assigned, exercised, None}
    contracts_delta: signed change to the position's contracts_open
    premium_impact: signed cash impact — positive when premium was
                    received, negative when paid.

    Returns None-tuple for events we can't classify."""
    subtype = (txn.subtype or '').lower().strip()
    if 'expir' in subtype:
        # Position expired worthless. If we were short, no cash change;
        # premium already received on open is now realized. If we were long,
        # premium paid on open is a loss.
        return ('expired_worthless', -running_contracts, 0.0)
    if 'assign' in subtype:
        return ('assigned', -running_contracts, 0.0)
    if 'exerc' in subtype:
        return ('exercised', -running_contracts, 0.0)
    if txn.type == 'sell':
        # Sell: STO if opening a new short (running >= 0),
        #       STC if closing an existing long (running > 0).
        if running_contracts > 0.0001:
            return ('sell_to_close', -abs(txn.quantity),
                    -abs(txn.amount) if txn.amount else 0.0)
        return ('sell_to_open', -abs(txn.quantity),
                abs(txn.amount) if txn.amount else 0.0)
    if txn.type == 'buy':
        # Buy: BTC if closing an existing short (running < 0),
        #      BTO if opening a new long (running >= 0).
        if running_contracts < -0.0001:
            return ('buy_to_close', abs(txn.quantity),
                    -abs(txn.amount) if txn.amount else 0.0)
        return ('buy_to_open', abs(txn.quantity),
                -abs(txn.amount) if txn.amount else 0.0)
    return (None, 0.0, 0.0)


def _underlying_share_coverage(account_id, underlying_ticker,
                               securities_by_id) -> int:
    """Total long shares of `underlying_ticker` held in `account_id`, for
    covered-call check. Returns integer share count."""
    if not underlying_ticker:
        return 0
    # Find every Security row whose ticker matches the underlying.
    matching_sec_ids = [sid for sid, s in securities_by_id.items()
                        if not s.is_option
                        and (s.ticker_symbol or '').upper() ==
                        underlying_ticker.upper()]
    if not matching_sec_ids:
        return 0
    total = 0
    for sid in matching_sec_ids:
        h = SecurityHolding.query.filter_by(
            account_id=account_id, security_id=sid).first()
        if h and h.quantity and h.quantity > 0:
            total += int(h.quantity)
    return total


def _account_cash_coverage(account_id) -> float:
    """Available cash in the same brokerage cash sweep as this options
    position — used for cash-secured put coverage. Reads the linked
    PlaidAccount.balance_current on the sweep account.

    Simplification: uses THIS account's cached balance. If cash sweep is
    on a different account (common at WF Advisors — the 3158/3194 sweep is
    a separate account from the 6030/9401 brokerage), the coverage check
    understates and is_naked over-flags. That's the SAFE direction — a
    false alarm is better than a missed naked position. A tighter check
    would inspect all cash-type accounts on the same PlaidItem; see
    v0.4.35 for that refinement."""
    from .models import PlaidAccount
    acct = PlaidAccount.query.filter_by(account_id=account_id).first()
    return float(acct.balance_current or 0.0) if acct else 0.0


def run_detection(notes: str = '') -> StrategyTracker:
    """Full detection pass. Returns the StrategyTracker row recording this
    run's results. Safe to call at any time — earlier derived rows are
    wiped and rebuilt from the current SecurityTransaction snapshot.

    `notes` is a free-form label the caller can pass ('manual rerun',
    'scheduled', 'post-sync', etc.) so the audit trail on
    /admin/strategy/history explains what triggered each run."""
    config = strategy_settings.load()
    snapshot = strategy_settings.as_json_snapshot()

    # Ground-truth: every non-cancelled investment transaction, ordered
    # (security, date) so the 5:4 detector can walk each security's
    # timeline linearly.
    txns = (SecurityTransaction.query
            .filter(SecurityTransaction.type != 'cancel')
            .order_by(SecurityTransaction.security_id,
                      SecurityTransaction.date.asc()).all())

    # Wipe derived rows before recompute — see module docstring.
    # NOTE: cascading FKs would handle this automatically but for
    # clarity + portability we do it explicitly. Order matters:
    # OptionsIncomeEntry FKs OptionsPosition, so it goes first.
    OptionsIncomeEntry.query.delete()
    OptionsPosition.query.delete()
    RetainedLot.query.delete()
    TradedCycle.query.delete()
    db.session.commit()

    # v0.4.32: 5:4 detector populates TradedCycle + RetainedLot.
    # Preload Security rows in one query so the detector's is_option
    # check + option-underlying lookup is a dict access rather than
    # a query per transaction.
    securities_by_id = {s.security_id: s for s in Security.query.all()}
    cycles_created, retained_created = _detect_54_cycles(
        txns, config, securities_by_id)
    db.session.commit()

    # v0.4.34: options detector populates OptionsPosition +
    # OptionsIncomeEntry + naked-position flags.
    options_touched, naked_flagged = _detect_options(
        txns, config, securities_by_id)
    db.session.commit()

    run = StrategyTracker(
        config_snapshot=snapshot,
        transactions_scanned=len(txns),
        cycles_created=cycles_created,
        retained_lots_created=retained_created,
        options_positions_touched=options_touched,
        naked_positions_flagged=naked_flagged,
        notes=notes or '',
    )
    db.session.add(run)
    db.session.commit()
    log.info('strategy detection ran: scanned=%d cycles=%d retained=%d '
             'options=%d naked=%d', len(txns), cycles_created,
             retained_created, options_touched, naked_flagged)
    return run
