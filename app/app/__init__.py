# SPDX-License-Identifier: MIT
"""ERPNext Bank Bridge — Flask app factory.

Pulls bank transactions from Plaid and posts them into ERPNext as Bank
Transaction records for reconciliation. Shape: env Config, SQLAlchemy models,
blueprint admin UI, and a filesystem-elected background poll thread."""
import logging
import os
import sys

from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy

from config import Config

__version__ = '0.4.19'
db = SQLAlchemy()


def _configure_logging() -> None:
    """Route bankbridge.* loggers to stdout so `docker logs` shows each sync
    hop. Override with LOG_LEVEL (DEBUG for more)."""
    level_name = os.environ.get('LOG_LEVEL', 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s %(message)s'))
        root.addHandler(h)
    root.setLevel(level)
    logging.getLogger('bankbridge').setLevel(level)


def _install_db_host_pinning(app: Flask) -> None:
    """Point SQLAlchemy at a creator that only ever returns our own database.

    No-op for a non-Postgres URI or under TESTING, so the unit suite keeps its
    plain in-process engine. Never raises: if pinning cannot be installed we log
    it and fall back to libpq's own resolution, which is exactly today's
    behaviour."""
    if app.config.get('TESTING'):
        return
    uri = app.config.get('SQLALCHEMY_DATABASE_URI') or ''
    if not uri.startswith('postgresql'):
        return
    try:
        from .db_host import log_host_diagnostics, make_creator
        log_host_diagnostics(uri)
        creator, pin = make_creator(uri)
    except Exception:  # noqa: BLE001 — never block boot on the diagnostics
        logging.getLogger('bankbridge').warning(
            'could not install DB host pinning; falling back to plain '
            'hostname resolution', exc_info=True)
        return
    options = dict(app.config.get('SQLALCHEMY_ENGINE_OPTIONS') or {})
    options['creator'] = creator
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = options
    # Kept for /api/health and the tests to inspect which address we settled on.
    app.extensions.setdefault('bankbridge', {})['db_host_pin'] = pin


def create_app(test_config: dict | None = None) -> Flask:
    _configure_logging()
    app = Flask(__name__)
    app.config.from_object(Config)
    # Test hook: override config (throwaway SQLite URI, temp DATA_DIR) and
    # disable the background scheduler. Production callers pass nothing.
    if test_config:
        app.config.update(test_config)

    os.makedirs(app.config['DATA_DIR'], exist_ok=True)
    # Reset any cached Fernet instance so a test-overridden DATA_DIR gets its
    # own key rather than a previous app's.
    from . import crypto
    crypto.reset_cache()

    # v0.4.16 — on Umbrel every app shares one Docker network and most name
    # their database service `db`, so that alias resolves to several apps'
    # containers at once. Route every pooled connection through a creator that
    # verifies it reached OUR database (see app/db_host.py); without it roughly
    # three connections in four hit a neighbour's Postgres and die with
    # "password authentication failed" — the symptom long misread as APP_SEED
    # drift. Skipped under TESTING, where the engine is a test double.
    _install_db_host_pinning(app)

    db.init_app(app)

    from .blueprints import admin_ui, api
    app.register_blueprint(admin_ui.bp)
    app.register_blueprint(api.bp)
    # v0.4.8 — the Plaid-facing routes moved under /bankbridge/. Keep the
    # pre-v0.4.8 paths answering (permanent redirects) so an install whose Plaid
    # dashboard still points at /plaid/oauth_return doesn't break mid-upgrade.
    from .legacy_paths import install_legacy_redirects
    install_legacy_redirects(app)

    with app.app_context():
        # v0.3.5 — before touching the schema, self-heal the one recurring
        # failure mode: the app role's password drifting from the value baked
        # into the postgres volume at first init. On a healthy DB this is one
        # `SELECT 1`; on the drift case it rotates the role password via the
        # superuser so we never have to wipe the volume. Fail-safe: never raises.
        from .db_recovery import ensure_db_auth
        ensure_db_auth(app, db.engine)
        db.create_all()
        # create_all() adds missing tables but never new columns on an existing
        # one — apply idempotent additive column migrations here.
        from .migrations import run_migrations
        run_migrations()

    if not app.config.get('TESTING') and app.config.get('SCHEDULER_ENABLED', True):
        try:
            from .services.scheduler import ensure_scheduler_started
            ensure_scheduler_started(app)
        except Exception:  # pragma: no cover - never block boot on the scheduler
            logging.getLogger('bankbridge').warning(
                'sync scheduler failed to start', exc_info=True)

    @app.get('/api/health')
    def _health():
        return jsonify({'status': 'ok', 'version': __version__})

    @app.get('/api/startup-status')
    def _startup():
        return jsonify({'status': 'ready', 'version': __version__})

    return app
