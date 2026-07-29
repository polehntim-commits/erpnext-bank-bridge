# SPDX-License-Identifier: MIT
"""How many DRAFT Journal Entries is ERPNext holding, and is that normal?
(v0.8.5)

THE INCIDENT THIS EXISTS FOR. The v0.8.4 sync at 22:19 UTC on 2026-07-29
re-emitted 112 pre-2024-12-01 settlement-leg drafts that were duplicates-by-
effect of the aggregate reconciliation JEs. The bulk_submit date filter
(from_date=2024-12-01) did not reach them. They sat in draft state for hours and
were caught by a human noticing the queue grow, then cleaned up by 112
sequential deletes. Nothing in Bank Bridge knew what a normal draft count looked
like on this install, so nothing could tell that this one wasn't.

Two design principles decide the shape of everything below.

**DATA DRIVEN, NOT HAND-CODED.** The warn-above number is not a constant. Every
snapshot is persisted (`DraftHealthSample`), and once there are enough of them
the threshold becomes the P95 of what this install actually does, times a
modest slack factor. `DEFAULT_THRESHOLD` is only the answer for the first few
weeks, before there is any history to learn from — it is a placeholder for a
measurement, not a judgement about the right number.

Two guards keep the learning honest, and both are the difference between a
baseline that works and one that quietly stops:

  * Breached samples are EXCLUDED from the baseline. A threshold that learned
    from its own explosions would ratchet upward until it never fired again —
    the classic way an adaptive alarm dies without anyone noticing.
  * A learned threshold never falls below `MIN_LEARNED_THRESHOLD`. An install
    whose history is a flat zero would otherwise compute a P95 of 0 and treat
    the first legitimate draft as an emergency, which trains the operator to
    ignore it.

**KAIROS OVER CHRONOS.** This module never schedules anything. `snapshot()` is a
pure read, answered whenever someone asks — the admin page, the MCP tool, the
end of a sync. The ACTION lives in `observe()`, and it fires on a STATE
TRANSITION: the moment a healthy count crosses into a breached one, and again
when it comes back. Not on every poll while breached (that is chronos, and it
trains the operator to filter the alert out), and never on a review timer. The
clock gathers the observation; the state decides whether anything happens.

WHAT AN OPERATOR DOES WITH IT. `by_prefix` groups the drafts by what their
`user_remark` starts with, which is how the pipelines identify themselves in
practice — 'Cash' and 'Cash withdrawal' are settlement legs, 'Bought' / 'Sold'
are trades, everything else is the bank-side rules engine. A breach whose whole
mass sits under one prefix names the pipeline that ran away, which is the first
question the v0.8.4 cleanup had to answer by hand.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone

from . import db
from .erpnext_client import ERPNextAPIError, ERPNextError
from .models import DraftHealthSample

log = logging.getLogger('bankbridge.draft_health')

JOURNAL_ENTRY_DT = 'Journal Entry'

# The placeholder threshold, in force ONLY until there is enough history to
# learn from. Tim's starting number; it is deliberately not tuned, because
# tuning a constant is the thing this module exists to stop doing.
DEFAULT_THRESHOLD = 50

# How many clean observations before the baseline is trusted. Below this the
# P95 of a handful of samples is an artifact of when they happened to be taken.
MIN_SAMPLES_FOR_BASELINE = 20

# Only the most recent N clean samples feed the baseline, so it tracks the
# install as it grows rather than averaging in a year-ago volume.
BASELINE_WINDOW = 200

# Headroom above the observed P95. A queue that normally peaks at 40 should not
# page at 41; it should page when something is happening that has not happened
# before.
BASELINE_SLACK = 1.25

# The floor a learned threshold cannot go below — see the module note.
MIN_LEARNED_THRESHOLD = 10

# How close together two SAME-STATE observations may be and still both count as
# history. `observe` runs on every page load and every MCP call, so without this
# an operator refreshing /admin/draft_health.html twenty times would satisfy
# MIN_SAMPLES_FOR_BASELINE in a minute and "learn" a threshold from one moment's
# reading. A STATE CHANGE is always recorded regardless — the crossings are the
# history that matters most, and suppressing one would let the same alert fire
# twice.
MIN_SAMPLE_INTERVAL_SECONDS = 300

# The remark grouping keeps at most this many leading words. Two is enough to
# separate 'Cash' from 'Cash withdrawal' without splintering every merchant name
# into its own bucket.
_PREFIX_WORDS = 2

# Punctuation stripped off a leading word before it is used as a bucket key.
_PREFIX_STRIP = ' \t\r\n.,;:!?"\'()[]{}—–-'


# ── the user_remark grouping ────────────────────────────────────────────────

def remark_prefix(remark: str) -> str:
    """The bucket key for one draft's `user_remark`.

    The leading run of non-numeric words, capped at `_PREFIX_WORDS`, cut short
    at the first colon. That single rule produces exactly the groups the
    pipelines already write — 'Cash', 'Cash withdrawal', 'Bought', 'Sold' — with
    no per-pipeline table to keep in sync as remarks change. The first numeric
    token ends the prefix because every remark Bank Bridge writes puts a
    quantity or an amount right after the verb."""
    text = (remark or '').strip()
    if not text:
        return '(no remark)'
    head = text.split(':', 1)[0] if ':' in text[:40] else text
    words = []
    for raw in head.split():
        word = raw.strip(_PREFIX_STRIP)
        if not word:
            continue
        if word.startswith('$') or any(ch.isdigit() for ch in word):
            break
        words.append(word)
        if len(words) >= _PREFIX_WORDS:
            break
    return ' '.join(words) or '(other)'


# ── the learned threshold ───────────────────────────────────────────────────

def _percentile(values: list, fraction: float) -> float:
    """Nearest-rank percentile. No numpy, no interpolation — with a few dozen
    integer counts, interpolating between two of them invents precision the
    sample size does not support."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(fraction * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def learned_threshold(company: str = '') -> tuple:
    """(threshold, source, sample_count) for this Company scope.

    `source` is 'baseline_p95' once there is enough clean history and 'default'
    before that. `sample_count` is how many clean samples the answer rests on —
    surfaced everywhere the threshold is, so an operator can see whether the
    number is measured or assumed."""
    rows = (DraftHealthSample.query
            .filter(DraftHealthSample.company == (company or ''),
                    DraftHealthSample.breached.is_(False))
            .order_by(DraftHealthSample.created_at.desc())
            .limit(BASELINE_WINDOW).all())
    counts = [int(r.draft_count or 0) for r in rows]
    if len(counts) < MIN_SAMPLES_FOR_BASELINE:
        return DEFAULT_THRESHOLD, 'default', len(counts)
    learned = math.ceil(_percentile(counts, 0.95) * BASELINE_SLACK)
    return max(MIN_LEARNED_THRESHOLD, int(learned)), 'baseline_p95', len(counts)


# ── the read ────────────────────────────────────────────────────────────────

def _parse_dt(value):
    """Frappe hands back `creation` as 'YYYY-MM-DD HH:MM:SS.ffffff'. Returns a
    naive datetime, or None for anything unparseable — an unreadable timestamp
    must not cost us the count, which is the number that matters."""
    if isinstance(value, datetime):
        return value
    text = (str(value or '')).strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    dt = _parse_dt(value)
    return dt.date() if dt else None


def snapshot(client, company: str = '', *, threshold: int | None = None) -> dict:
    """Read ERPNext's draft Journal Entries and describe them. Pure read — this
    persists nothing and fires nothing (that is `observe`).

    Raises the usual ERPNext errors when the ledger cannot be read: a caller
    that could not reach ERPNext must NOT be handed a draft_count of 0, which
    reads identically to a perfectly healthy queue and is the one wrong answer
    this function could give."""
    scope = (company or '').strip()
    filters = [['docstatus', '=', 0]]
    if scope:
        filters.append(['company', '=', scope])
    rows = client.list_docs(
        JOURNAL_ENTRY_DT, filters=filters,
        fields=['name', 'posting_date', 'creation', 'total_debit',
                'user_remark', 'company'],
        limit_page_length=0) or []

    total = 0.0
    count = 0
    oldest_posting = None
    oldest_created = None
    by_prefix: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        count += 1
        amount = 0.0
        try:
            amount = float(row.get('total_debit') or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        total += amount
        posting = _parse_date(row.get('posting_date'))
        if posting and (oldest_posting is None or posting < oldest_posting):
            oldest_posting = posting
        created = _parse_dt(row.get('creation'))
        if created and (oldest_created is None or created < oldest_created):
            oldest_created = created
        bucket = by_prefix.setdefault(remark_prefix(row.get('user_remark')),
                                      {'count': 0, 'amount': 0.0})
        bucket['count'] += 1
        bucket['amount'] = round(bucket['amount'] + amount, 2)

    if threshold is None:
        threshold, source, samples = learned_threshold(scope)
    else:
        threshold, source, samples = int(threshold), 'explicit', 0
    breached = count > threshold
    now = datetime.now(timezone.utc)
    age_days = None
    if oldest_created is not None:
        # `creation` carries no offset in Frappe, so the reference clock is the
        # naive one — but a stray offset must cost the age, not the reading.
        reference = (now if oldest_created.tzinfo else now.replace(tzinfo=None))
        try:
            age_days = round((reference - oldest_created).total_seconds()
                             / 86400.0, 2)
        except TypeError:  # pragma: no cover - defensive
            age_days = None
    return {
        'company': scope,
        'observed_at': now.isoformat(),
        'draft_count': count,
        'total_amount': round(total, 2),
        'oldest_posting_date': (oldest_posting.isoformat()
                                if oldest_posting else None),
        'oldest_created_at': (oldest_created.isoformat()
                              if oldest_created else None),
        'oldest_draft_age_days': age_days,
        'by_prefix': dict(sorted(by_prefix.items(),
                                 key=lambda kv: -kv[1]['count'])),
        'threshold': int(threshold),
        'threshold_source': source,
        'baseline_samples': samples,
        'breached': breached,
        # The one-line verdict, phrased so an AI operator relaying it does not
        # have to decide what "breached: true" means.
        'status': 'warn' if breached else 'ok',
        'headline': (f'{count} draft Journal Entries — ABOVE the '
                     f'{threshold} threshold ({source})'
                     if breached else
                     f'{count} draft Journal Entries — within the '
                     f'{threshold} threshold ({source})'),
        # Non-fatal, and worth stating: the numbers below MIN_SAMPLES_FOR_BASELINE
        # rest on a constant, not on this install's own behaviour.
        'baseline_ready': source != 'default',
    }


# ── the action, which is kairotic ───────────────────────────────────────────

def _last_sample(company: str):
    return (DraftHealthSample.query
            .filter(DraftHealthSample.company == (company or ''))
            .order_by(DraftHealthSample.created_at.desc(),
                      DraftHealthSample.id.desc()).first())


def _age_seconds(when) -> float:
    """Seconds since `when`, tolerating the naive/aware mix a round-trip through
    SQLite produces. Returns +inf for an unreadable timestamp, which errs toward
    RECORDING the sample — losing an observation costs baseline precision, and
    that is the cheaper mistake."""
    if when is None:
        return float('inf')
    now = datetime.now(timezone.utc)
    if when.tzinfo is None:
        now = now.replace(tzinfo=None)
    try:
        return (now - when).total_seconds()
    except TypeError:  # pragma: no cover - defensive
        return float('inf')


def _should_record(previous, breached: bool) -> bool:
    """Whether this observation earns a history row. See
    MIN_SAMPLE_INTERVAL_SECONDS — a state change always does."""
    if previous is None:
        return True
    if bool(previous.breached) != bool(breached):
        return True
    return _age_seconds(previous.created_at) >= MIN_SAMPLE_INTERVAL_SECONDS


def observe(client, company: str = '', *, threshold: int | None = None) -> dict:
    """Take a snapshot, persist it as history, and fire on a state CROSSING.

    Returns the snapshot with three extra keys: `crossed` (this observation is
    the one that went from healthy to breached), `recovered` (the one that came
    back), and `recorded` (whether it earned a history row). The first two are
    False on every observation in between — a queue that stays over the line does
    not re-alert, because the second identical alert is the one that teaches an
    operator to stop reading them.

    `recorded` is False for a same-state reading taken within
    MIN_SAMPLE_INTERVAL_SECONDS of the last one, so refreshing the admin page
    cannot manufacture a baseline. A state CHANGE is always recorded.

    Best-effort on the WRITE half: a snapshot that cannot be persisted is still
    returned. Losing a history row costs a little baseline precision; failing
    the caller (a sync, an admin page) costs the operator the answer."""
    snap = snapshot(client, company, threshold=threshold)
    snap['crossed'] = False
    snap['recovered'] = False
    previous = None
    try:
        previous = _last_sample(company)
    except Exception:  # noqa: BLE001 — a history read must not fail the answer
        db.session.rollback()
        log.warning('could not read draft-health history', exc_info=True)
    was_breached = bool(previous.breached) if previous is not None else False
    snap['crossed'] = bool(snap['breached'] and not was_breached)
    snap['recovered'] = bool(was_breached and not snap['breached'])

    snap['recorded'] = _should_record(previous, snap['breached'])
    if snap['recorded']:
        try:
            db.session.add(DraftHealthSample(
                company=(company or '').strip(),
                draft_count=snap['draft_count'],
                total_amount=snap['total_amount'],
                oldest_posting_date=_parse_date(snap['oldest_posting_date']),
                oldest_created_at=_parse_dt(snap['oldest_created_at']),
                threshold=snap['threshold'],
                threshold_source=snap['threshold_source'],
                breached=snap['breached'],
                by_prefix=snap['by_prefix']))
            db.session.commit()
        except Exception:  # noqa: BLE001 — see the docstring
            db.session.rollback()
            log.warning('could not persist draft-health sample', exc_info=True)

    if snap['crossed']:
        _fire('draft_health_threshold_crossed', snap,
              f"draft Journal Entries crossed the alert threshold: "
              f"{snap['draft_count']} > {snap['threshold']} "
              f"({snap['threshold_source']})")
    elif snap['recovered']:
        _fire('draft_health_recovered', snap,
              f"draft Journal Entries back within threshold: "
              f"{snap['draft_count']} <= {snap['threshold']}")
    return snap


def _fire(event_type: str, snap: dict, message: str) -> None:
    """The kairotic notification: one log line + one permanent AuditEvent, at
    the moment the state changed and at no other moment."""
    from . import audit
    if event_type == 'draft_health_recovered':
        log.info('[draft-health] %s', message)
    else:
        log.warning('[draft-health] %s — largest group: %s', message,
                    _largest_group(snap))
    try:
        audit.record(event_type, subject_type=None,
                     after={k: snap.get(k) for k in
                            ('company', 'draft_count', 'total_amount',
                             'threshold', 'threshold_source',
                             'baseline_samples', 'oldest_posting_date',
                             'oldest_created_at', 'by_prefix')},
                     notes=message)
    except Exception:  # noqa: BLE001 — never break the caller on an audit write
        log.warning('could not audit %s', event_type, exc_info=True)


def _largest_group(snap: dict) -> str:
    groups = (snap.get('by_prefix') or {})
    if not groups:
        return 'none'
    name, stats = max(groups.items(), key=lambda kv: kv[1].get('count', 0))
    return f"{name} ({stats.get('count', 0)})"


# ── the fail-soft wrapper the sync path uses ────────────────────────────────

def observe_quietly(client, company: str = '') -> dict | None:
    """`observe`, but an ERPNext that cannot answer costs nothing.

    The sync calls this AFTER it has already mirrored and posted everything; a
    health read that failed must not turn a successful sync into a failed one.
    Returns None when the ledger could not be read."""
    if client is None:
        return None
    try:
        return observe(client, company)
    except (ERPNextAPIError, ERPNextError):
        log.warning('draft-health observation skipped — ERPNext unreadable',
                    exc_info=True)
        return None
    except Exception:  # noqa: BLE001 — defensive; this is a reporting read
        db.session.rollback()
        log.warning('draft-health observation failed', exc_info=True)
        return None
