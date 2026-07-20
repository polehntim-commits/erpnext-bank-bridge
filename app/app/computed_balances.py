# SPDX-License-Identifier: MIT
"""Transaction-derived balances (v0.4.20).

The gap this closes: bank-issued statements are the only figures the bank
itself asserts, but their PDFs fail to parse routinely — Plaid's sandbox mocks
have no explicit opening/closing labels at all, and every real institution
prints its own vocabulary and its own layout. When the parser returns None for
opening or closing, the reconciliation path lands on `no_data` — a stop, not an
answer — and any monthly balance report that depends on statements goes dark
for that period.

The transaction mirror is a strictly better source than the parser gets to
argue with. It is exact whenever the mirror is complete (the same precondition
as `opening_balance.estimate_opening_balance` — the estimate is the special
case of `balance_at(account, earliest_txn_date - 1)`). Plaid returns the FULL
transaction history at link time and `/transactions/sync` keeps it complete
thereafter, so the mirror IS complete for the accounts this module runs on.

WHAT THIS MODULE PROVIDES:

  * `balance_at(account, as_of)` — the balance at close of business on
    `as_of`, walking every non-pending, non-removed BankTransaction dated
    AFTER `as_of` backward from `balance_current`. Sign convention delegates
    to `opening_balance.opens_by_debit` so this can never drift from the
    asset/liability decision the GL leaf was booked under.

  * `monthly_closing_balances(account, months)` — [{month_end, balance}] for
    the last `months` calendar months, newest first. Any month before the
    earliest mirrored transaction is dropped — the pre-mirror balance is only
    as trustworthy as the data we have on it, and computing "balance in 2019"
    from a mirror that starts in 2024 is arithmetic without evidence.

WHAT THIS MODULE DELIBERATELY DOES NOT DO:

  * It does not replace bank-issued statements as the primary
    reconciliation anchor. When both a parseable statement AND a computed
    figure exist for the same period they must be shown SIDE BY SIDE so an
    operator can see whether the bank agrees with the books. Silencing that
    check by picking one over the other trades away the whole point of the
    statements pipeline.

  * It does not backdate pending transactions. Pending rows are provisional,
    may be restated by Plaid, and could vanish entirely — the same reason
    `estimate_opening_balance` excludes them. A "current" balance built from
    provisional data would be a moving target.

  * It does not walk transactions dated ON `as_of`. `as_of` means "at end of
    that day", so a transaction posted on the 31st is INCLUDED in the balance
    for the 31st and EXCLUDED from the balance for the 30th.
"""

from datetime import date, timedelta
from calendar import monthrange

from .models import BankTransaction, PlaidAccount, db


def balance_at(account: PlaidAccount, as_of: date | None) -> float | None:
    """The balance this account held at close of business on `as_of`,
    computed from `balance_current` and mirrored transactions dated after
    `as_of`. `None` when we can't defensibly compute a value.

    Sign convention: asset accounts (depository / investment) apply outflows
    as decreases, so balance_before = current + Σamount; liability accounts
    (credit / loan) apply outflows as increases, so balance_before =
    current - Σamount. `opens_by_debit` picks which side this account sits on.

    Guards that produce None (each covers a case where the arithmetic would
    otherwise return a confidently-wrong number):

      * `as_of` or `account` is None, or `balance_current` is None — nothing
        to walk back from.
      * The mirror holds no transactions for this account — we would return
        `balance_current` unchanged, asserting "the balance was the same at
        any point in the past" for an account we simply have no historical
        signal on. Silence is honest here.

    Note: we deliberately DON'T bail when `as_of` sits before the earliest
    mirrored transaction. Plaid returns the FULL transaction history at link
    time and `/transactions/sync` keeps it complete thereafter, so the
    balance at a date before the earliest mirrored transaction is exactly
    the same as the balance on the earliest mirrored day (nothing moved).
    Stricter completeness guards live in `monthly_closing_balances`, where
    reporting balances "back before the mirror" would materially mislead —
    reconciliation against a Plaid-issued statement is a different case
    with a much tighter date bound already."""
    if as_of is None or account is None:
        return None
    if account.balance_current is None:
        return None
    earliest = earliest_mirrored_date(account)
    if earliest is None:
        return None
    # Deferred import: opening_balance imports helpers from erpnext_accounts,
    # which imports this module — a top-level import cycles.
    from . import opening_balance as obal
    current = float(account.balance_current)
    rows = (BankTransaction.query
            .filter(BankTransaction.account_id == account.account_id,
                    BankTransaction.date > as_of,
                    BankTransaction.pending.is_(False),
                    BankTransaction.removed.is_(False))
            .all())
    signed = sum(float(t.amount or 0.0) for t in rows)
    if obal.opens_by_debit(account):
        return round(current + signed, 2)
    return round(current - signed, 2)


def earliest_mirrored_date(account: PlaidAccount) -> date | None:
    """The oldest non-pending, non-removed transaction date on the mirror for
    this account, or None when the mirror is empty. Reused from
    `statements.earliest_transaction_date` semantics — computed balances
    before this date are arithmetic without evidence and should be omitted."""
    return (db.session.query(db.func.min(BankTransaction.date))
            .filter(BankTransaction.account_id == account.account_id,
                    BankTransaction.pending.is_(False),
                    BankTransaction.removed.is_(False))
            .scalar())


def _month_end(year: int, month: int) -> date:
    """Last calendar day of (year, month)."""
    return date(year, month, monthrange(year, month)[1])


def _prev_month(y: int, m: int) -> tuple[int, int]:
    """(year, month) one calendar month earlier — handles the December wrap."""
    if m == 1:
        return y - 1, 12
    return y, m - 1


def monthly_closing_balances(account: PlaidAccount,
                             months: int = 24,
                             through: date | None = None) -> list[dict]:
    """[{'month_end': date, 'balance': float, 'partial': bool}] for the last
    `months` calendar months ending at `through` (defaults to today), newest
    first.

    Months entirely before `earliest_mirrored_date(account)` are dropped —
    the mirror has no signal for them, and reporting a computed number over a
    period we can't verify would misrepresent what this app knows.

    `partial=True` marks a row whose `month_end` was clipped to `through`
    rather than sitting at the last calendar day of that month. In practice
    this only ever fires on the current month, and the balance is
    balance-as-of-`through`, not the eventual close. Rendering that as a
    plain month label lies to the reader; the flag lets a caller print
    "2026-07 (to 20th)" or drop the row entirely, and either choice stays
    honest about what the arithmetic produced.

    Months WITH activity that spans a gap in the mirror will report a wrong
    balance, but no worse than `estimate_opening_balance` reports it: this is
    the arithmetic-completeness assumption both share, and both fail
    identically when it breaks."""
    if account is None or months <= 0:
        return []
    end_date = through or date.today()
    y, m = end_date.year, end_date.month
    floor = earliest_mirrored_date(account)
    out = []
    for _ in range(months):
        calendar_close = _month_end(y, m)
        partial = calendar_close > end_date
        month_end = end_date if partial else calendar_close
        if floor is not None and month_end < floor:
            break
        value = balance_at(account, month_end)
        if value is None:
            break
        out.append({'month_end': month_end, 'balance': value,
                    'partial': partial})
        y, m = _prev_month(y, m)
    return out


def opening_and_closing_for_period(account: PlaidAccount,
                                   period_start: date | None,
                                   period_end: date | None
                                   ) -> tuple[float | None, float | None]:
    """(opening, closing) for a statement period, computed. Opening is the
    balance at close of the day BEFORE `period_start` (i.e. what the account
    held going in); closing is the balance at close of `period_end`. Either
    or both may be None when their date is missing or the account has no
    cached current balance."""
    opening = balance_at(account, period_start - timedelta(days=1)) \
        if period_start else None
    closing = balance_at(account, period_end)
    return opening, closing
