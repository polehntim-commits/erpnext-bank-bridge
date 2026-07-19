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


def rollup_interval_or_none(app) -> int | None:
    """The cadence (hours) for the Counterparty activity rollup, or None when it
    is disabled. Pure, so the enable/disable decision is testable without
    APScheduler — the same shape as poll_interval_or_none above."""
    if not app.config.get('COUNTERPARTY_OVERLAY_ENABLED', True):
        return None
    try:
        hours = int(app.config.get('COUNTERPARTY_ROLLUP_INTERVAL_HOURS', 24))
    except (TypeError, ValueError):
        hours = 24
    return hours if hours > 0 else None


def _run_counterparty_rollup(app) -> None:
    """Refresh every Counterparty's cached activity totals from the live ledger.

    Cheap and boring by construction: one read of the party-bearing slice of the
    GL, then a write only for the Counterparties whose numbers actually moved
    (see counterparty.rollup_counterparties). Swallows everything — a rollup is
    a convenience, and a failed one must never take the scheduler thread with
    it."""
    from ..sync_engine import get_erp_client_or_none
    from .. import audit
    from .. import counterparty
    with app.app_context():
        audit.set_context('scheduler')
        if not counterparty.is_enabled():
            return
        try:
            client = get_erp_client_or_none()
            if client is None:
                log.info('[scheduler] ERPNext not configured — skipping '
                         'counterparty rollup')
                return
            result = counterparty.rollup_counterparties(client)
            log.info('[scheduler] counterparty rollup complete: %s', result)
        except Exception:  # pragma: no cover - never let the job die
            log.exception('[scheduler] counterparty rollup crashed')


def _run_counterparty_provision(app) -> None:
    """Provision the Counterparty doctype once, shortly after boot (v0.4.6).

    THE BUG THIS FIXES: v0.4.5 only ever reached counterparty.bootstrap through
    erpnext_accounts.bootstrap, which runs on account IMPORT and from the ERPNext
    settings page. An install that had already imported its accounts under an
    earlier version therefore never ran it — the doctype was never created, no
    CREATE was ever attempted, and the only trace was a stream of 404s from the
    read paths. `ensure_counterparty_doctype` was written to be called at
    startup ("Safe to call on every startup", its docstring said); nothing
    called it.

    It runs HERE rather than in create_app for the same reason the sync poll
    does: gunicorn runs several workers, and this must happen once per container,
    not once per worker. The elected scheduler is where "once per container"
    already lives. It also keeps boot off the network — an unreachable ERPNext
    delays this job, not the app coming up.

    Swallows everything: a container must still boot when ERPNext is down."""
    from ..sync_engine import get_erp_client_or_none
    from .. import audit
    from .. import counterparty
    with app.app_context():
        audit.set_context('scheduler')
        try:
            if not counterparty.is_enabled():
                log.info('[scheduler] counterparty overlay disabled '
                         '(COUNTERPARTY_OVERLAY_ENABLED=false) — not provisioning')
                return
            client = get_erp_client_or_none()
            report = counterparty.provision_report(client)
            if report['ok']:
                log.info('[scheduler] counterparty doctype ready (%s)',
                         report['state'])
            else:
                log.warning(
                    '[scheduler] counterparty doctype UNAVAILABLE (%s) — %s',
                    report['state'],
                    counterparty.PROVISION_HELP.get(report['state'], ''))
            audit.record('counterparty_doctype_provision', subject_type=None,
                         after=report,
                         notes=f"startup provision → {report['state']}")
            # Only pair once the doctype exists; pairing is itself idempotent.
            if report['ok'] and app.config.get('COUNTERPARTY_AUTO_PAIR', True):
                paired = counterparty.pair_existing_parties(client)
                log.info('[scheduler] counterparty pairing pass: %s',
                         {k: v for k, v in paired.items() if k != 'actions'})
        except Exception:  # pragma: no cover - never let the job die
            log.exception('[scheduler] counterparty provision crashed')


def _run_bank_statement_provision(app) -> None:
    """Provision the Bank Statement doctype and push any statements ERPNext
    doesn't hold, once per container shortly after boot (v0.4.10).

    Deliberately the same shape as _run_counterparty_provision, for the same
    reason it exists: v0.4.5 shipped a doctype whose only provisioning call sat
    on the account-IMPORT path, so an install that had already imported its
    accounts never created it. Reaching provisioning from the elected scheduler
    is what makes "restart the container" a sufficient upgrade step.

    The sync pass rides along rather than getting its own job: it is only worth
    running once the doctype exists, and pairing them means a fresh v0.4.10 boot
    has ERPNext populated ~20 seconds in without waiting for the monthly pull.

    Swallows everything: a container must still boot when ERPNext is down."""
    from ..sync_engine import get_erp_client_or_none
    from .. import audit
    from .. import erpnext_statements
    with app.app_context():
        audit.set_context('scheduler')
        try:
            if not erpnext_statements.is_enabled():
                log.info('[scheduler] ERPNext statement overlay disabled '
                         '(ERPNEXT_STATEMENTS_ENABLED=false) — not '
                         'provisioning')
                return
            client = get_erp_client_or_none()
            report = erpnext_statements.provision_report(client)
            if report['ok']:
                log.info('[scheduler] Bank Statement doctype ready (%s)',
                         report['state'])
            else:
                log.warning(
                    '[scheduler] Bank Statement doctype UNAVAILABLE (%s) — %s',
                    report['state'],
                    erpnext_statements.PROVISION_HELP.get(report['state'], ''))
            audit.record('bank_statement_doctype_provision', subject_type=None,
                         after=report,
                         notes=f"startup provision → {report['state']}")
            if not report['ok']:
                return
            if not app.config.get('ERPNEXT_STATEMENTS_AUTO_SYNC', True):
                log.info('[scheduler] ERPNEXT_STATEMENTS_AUTO_SYNC is off — '
                         'statements stay local until the backfill script runs')
                return
            result = erpnext_statements.sync_all(client)
            log.info('[scheduler] ERPNext statement sync: %s',
                     {k: v for k, v in result.items() if k != 'errors'})
            if result['created'] or result['adopted'] or result['reconciled']:
                audit.record('erpnext_statements_synced', subject_type=None,
                             after=result,
                             notes=(f"created {result['created']}, adopted "
                                    f"{result['adopted']}, reconciled "
                                    f"{result['reconciled']}"))
        except Exception:  # pragma: no cover - never let the job die
            log.exception('[scheduler] Bank Statement provision crashed')


def match_count_rollup_interval_or_none(app) -> int | None:
    """The cadence (hours) for the rule match-count rollup, or None when it is
    disabled. Same shape as the two above, and pure for the same reason."""
    try:
        hours = int(app.config.get('RULE_MATCH_COUNT_ROLLUP_INTERVAL_HOURS', 24))
    except (TypeError, ValueError):
        hours = 24
    return hours if hours > 0 else None


def _run_match_count_rollup(app) -> None:
    """Refresh every rule's cached match count from the local
    GeneratedJournalEntry table (v0.4.6).

    Unlike the two jobs above this one never touches ERPNext or Plaid — it is a
    single local read plus a write for the rules whose number moved — so it has
    no configured/reachable precondition to check. Swallows everything, for the
    same reason the others do."""
    from .. import audit
    from .. import rule_stats
    with app.app_context():
        audit.set_context('scheduler')
        try:
            result = rule_stats.rollup_match_counts()
            audit.record('rule_match_counts_rolled_up', subject_type=None,
                         after=result,
                         notes=(f"recounted {result['scanned']} rule(s) — "
                                f"{result['updated']} changed"))
            log.info('[scheduler] rule match-count rollup complete: %s', result)
        except Exception:  # pragma: no cover - never let the job die
            log.exception('[scheduler] rule match-count rollup crashed')


def statements_interval_or_none(app) -> int | None:
    """The cadence (DAYS, not hours) for the statement pull, or None when it is
    disabled — either by STATEMENTS_ENABLED or by a non-positive interval. Pure,
    matching the three above; days rather than hours because a bank issues a
    statement monthly and checking more often just spends Plaid calls."""
    if not app.config.get('STATEMENTS_ENABLED', True):
        return None
    try:
        days = int(app.config.get('STATEMENTS_PULL_INTERVAL_DAYS', 30))
    except (TypeError, ValueError):
        days = 30
    return days if days > 0 else None


def _run_statements_pull(app) -> None:
    """Download any bank statements Plaid has that we don't already hold
    (v0.4.9).

    Cheap in the steady state by construction: a statement whose row exists and
    whose PDF is on disk is skipped without a download, so a monthly run over a
    settled install lists once per Item and stops. Swallows everything — the
    statements are an audit-trail and reconciliation convenience, and a failed
    pull must never take the scheduler thread (or the nightly sync sharing it)
    down with it."""
    from .. import audit
    from .. import plaid_settings
    from .. import statements
    with app.app_context():
        audit.set_context('scheduler')
        if not statements.is_enabled():
            return
        try:
            if not plaid_settings.is_configured():
                log.info('[scheduler] Plaid not configured — skipping '
                         'statement pull')
            else:
                result = statements.fetch_all()
                audit.record('statements_pulled', subject_type=None,
                             after=result,
                             notes=(f"listed {result['listed']}, stored "
                                    f"{result['stored']}, skipped "
                                    f"{result['skipped_existing']}"))
                log.info('[scheduler] statement pull complete: %s', result)
        except Exception:  # pragma: no cover - never let the job die
            log.exception('[scheduler] statement pull crashed')
        # v0.4.10 — push whatever we now hold into ERPNext. Its own try block,
        # and deliberately NOT gated on the pull succeeding: a Plaid outage (or
        # a Plaid that was never configured) still leaves previously-fetched
        # statements worth syncing, and an ERPNext outage must not make the
        # pull look failed.
        try:
            from .. import erpnext_statements
            if (erpnext_statements.is_enabled()
                    and app.config.get('ERPNEXT_STATEMENTS_AUTO_SYNC', True)):
                synced = erpnext_statements.sync_all()
                log.info('[scheduler] ERPNext statement sync: %s',
                         {k: v for k, v in synced.items() if k != 'errors'})
        except Exception:  # pragma: no cover - never let the job die
            log.exception('[scheduler] ERPNext statement sync crashed')


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
        # v0.4.6 — provision the Counterparty doctype once per container, ~15s
        # after boot. A one-shot 'date' job, not an interval: provisioning is
        # idempotent but there is nothing to re-check hourly, and it must land
        # BEFORE the rollup below (which reads Counterparties) first fires.
        sched.add_job(lambda: _run_counterparty_provision(app), 'date',
                      id='counterparty_provision',
                      run_date=datetime.utcnow() + timedelta(seconds=15))
        log.info('[scheduler] counterparty doctype provision queued (+15s)')
        # v0.4.10 — the same treatment for the Bank Statement doctype, ~20s
        # after boot. Five seconds behind the Counterparty job on purpose: both
        # POST a DocType, and creating two at once on a Raspberry-Pi-class box
        # is the kind of thing that makes one of them time out for no reason.
        # This is the wiring the v0.4.5 → v0.4.6 fix was about — the provision
        # must be REACHED at startup, not only from an import path an
        # already-imported install never walks again.
        sched.add_job(lambda: _run_bank_statement_provision(app), 'date',
                      id='bank_statement_provision',
                      run_date=datetime.utcnow() + timedelta(seconds=20))
        log.info('[scheduler] Bank Statement doctype provision queued (+20s)')
        # v0.4.5 — the Counterparty activity rollup, on its own cadence. Added
        # to the SAME elected scheduler so there is still exactly one background
        # thread per container. Its first run is offset well past the sync's
        # 30-second kick so a fresh boot doesn't hit ERPNext with both at once.
        rollup_hours = rollup_interval_or_none(app)
        if rollup_hours is not None:
            sched.add_job(lambda: _run_counterparty_rollup(app), 'interval',
                          hours=rollup_hours, id='counterparty_rollup',
                          max_instances=1, coalesce=True,
                          next_run_time=datetime.utcnow() + timedelta(minutes=5))
            log.info('[scheduler] counterparty rollup every %dh', rollup_hours)
        # v0.4.6 — the rule match-count rollup, on the same elected scheduler.
        # Local-only and cheap, so its first run is offset by just two minutes:
        # the Rules page's Match Count column is blank until it has run once, and
        # on a fresh upgrade that is the first thing an operator looks at.
        match_hours = match_count_rollup_interval_or_none(app)
        if match_hours is not None:
            sched.add_job(lambda: _run_match_count_rollup(app), 'interval',
                          hours=match_hours, id='rule_match_count_rollup',
                          max_instances=1, coalesce=True,
                          next_run_time=datetime.utcnow() + timedelta(minutes=2))
            log.info('[scheduler] rule match-count rollup every %dh', match_hours)
        # v0.4.9 — the bank-statement pull, on the same elected scheduler. Its
        # first run is offset by 10 minutes: it is the least urgent job here (a
        # statement arrives once a month), it costs Plaid calls, and a fresh boot
        # should get the sync and both rollups in before spending them.
        statement_days = statements_interval_or_none(app)
        if statement_days is not None:
            sched.add_job(lambda: _run_statements_pull(app), 'interval',
                          days=statement_days, id='statements_pull',
                          max_instances=1, coalesce=True,
                          next_run_time=datetime.utcnow() + timedelta(minutes=10))
            log.info('[scheduler] bank statement pull every %dd', statement_days)
        _schedulers[id(app)] = sched
        return sched
