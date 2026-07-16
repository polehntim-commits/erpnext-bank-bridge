# SPDX-License-Identifier: MIT
"""Background poll: run the full Plaid → ERPNext sync every N hours.

Uses APScheduler's BackgroundScheduler, but only ONE per container — gunicorn
runs multiple workers, so we elect a single owner with a filesystem-flock: the
first worker to `flock` ${DATA_DIR}/scheduler.lock wins; the rest log and exit.
The lock fd is held for the process lifetime.

APScheduler is imported lazily inside ensure_scheduler_started so this module
imports cleanly in an environment without the wheel (dev / tests). The job runs
inside an app context and swallows its own exceptions so a transient Plaid /
ERPNext blip never kills the scheduler thread."""
import fcntl
import logging
import os
import threading

log = logging.getLogger('bankbridge.scheduler')

_schedulers: dict[int, object] = {}
_lock = threading.Lock()
_scheduler_lock_fd: int | None = None


def poll_interval_or_none(interval_hours) -> int | None:
    """The cadence (hours) to schedule the auto-poll at, or None for MANUAL
    ONLY. The scheduler's single source of truth for whether to add a job —
    pure, so the manual-only decision is testable without APScheduler."""
    from .. import sync_config
    if not sync_config.is_auto_sync_enabled(interval_hours):
        return None
    return max(1, sync_config.normalize_interval(interval_hours))


def _run_sync(app) -> None:
    from ..sync_engine import sync_all
    from ..plaid_client import PlaidError, PlaidConfigError
    from .. import plaid_settings
    from .. import audit
    with app.app_context():
        # Tag every AuditEvent this poll writes as scheduler-driven.
        audit.set_context('scheduler')
        if not plaid_settings.is_configured():
            log.info('[scheduler] Plaid not configured — skipping poll')
            return
        try:
            result = sync_all()
            log.info('[scheduler] sync complete: %s', result)
        except (PlaidError, PlaidConfigError) as e:
            log.warning('[scheduler] sync failed: %s', e)
        except Exception:  # pragma: no cover - never let the job die
            log.exception('[scheduler] sync crashed')


def ensure_scheduler_started(app):
    """Elect one scheduler across the container's workers and start it. No-op
    (returns None) for a non-winning worker. Runs the sync every effective
    interval (persisted setting > SYNC_INTERVAL_HOURS env), plus once ~30s after
    boot — unless the interval is manual-only (<= 0), in which case no poll job
    is added and syncs run only from the dashboard "Sync now" button."""
    global _scheduler_lock_fd
    if app.config.get('TESTING'):
        return None
    with _lock:
        if id(app) in _schedulers:
            return _schedulers[id(app)]
        lock_path = os.path.join(app.config.get('DATA_DIR', '/tmp'),
                                 'scheduler.lock')
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            log.info('[scheduler] pid=%d skipping — another worker owns it',
                     os.getpid())
            return None
        _scheduler_lock_fd = fd

        from datetime import datetime, timedelta
        from apscheduler.schedulers.background import BackgroundScheduler

        from .. import plaid_settings
        # The persisted admin setting wins over the SYNC_INTERVAL_HOURS env seed.
        with app.app_context():
            interval = plaid_settings.sync_interval_hours()
        hours = poll_interval_or_none(interval)

        sched = BackgroundScheduler(daemon=True, timezone='UTC')
        sched.start()
        if hours is None:
            # MANUAL ONLY — start the scheduler with no poll job so the
            # container lifecycle is consistent; syncs run from "Sync now".
            log.info('[scheduler] pid=%d elected — MANUAL-ONLY (no auto-poll '
                     'job); use the dashboard "Sync now" button', os.getpid())
        else:
            sched.add_job(lambda: _run_sync(app), 'interval', hours=hours,
                          id='plaid_sync', max_instances=1, coalesce=True,
                          next_run_time=datetime.utcnow() + timedelta(seconds=30))
            log.info('[scheduler] pid=%d elected — polling every %dh',
                     os.getpid(), hours)
        _schedulers[id(app)] = sched
        return sched
