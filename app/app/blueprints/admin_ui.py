# SPDX-License-Identifier: MIT
"""Minimal HTML admin — deliberately functional, not fancy.
Unauthenticated + LAN-only (the Umbrel trust boundary).

Pages:
  /  and  /admin        — dashboard (health, counts, recent sync log)
  /admin/plaid_settings — Plaid Client ID / secrets / environment / webhook +
                          sync frequency (cost-aware presets)
  /admin/link_bank      — Plaid Link entry point
  /admin/accounts       — map each Plaid account → ERPNext Bank Account
  /admin/accounts/cleanup — ERPNext Bank Accounts no live Plaid account claims,
                          grouped by bank, with a delete-or-ignore choice
                          (v0.4.15; for clearing out earlier dry-runs)
  /admin/transactions   — filterable transaction list + per-row retry, plus the
                          v0.4.6 rule-state filter, unmatched-by-merchant
                          grouping and "create a rule from this" shortcuts
  /admin/erpnext_settings — ERPNext connection (URL + API key/secret)
  /admin/intercompany   — review transfers detected between two owned Companies
  /admin/statements     — bank statement PDFs + per-period reconciliation
                          against the mirrored transactions (v0.4.9)
  /admin/counterparties — unified Customer + Supplier identity, ledger, reports
  /admin/sync_log       — recent PlaidSyncLog rows
"""
import hmac
import logging
import re
from datetime import date, datetime, timezone
from urllib.parse import quote_plus

from flask import (Blueprint, Response, current_app, jsonify, redirect,
                   render_template_string, request, session, url_for)
from werkzeug.security import check_password_hash

from .. import account_cleanup
from .. import account_visibility as av
from .. import statements as stmts_mod
from .. import audit
from .. import categorization
from .. import counterparty
from .. import crypto
from .. import db
from .. import erpnext_accounts
from .. import erpnext_bank
from .. import erpnext_settings as erps
from .. import intercompany
from .. import loans
from .. import opening_balance as obal
from .. import plaid_settings as ps
from .. import reconnect
from .. import rule_stats
from .. import sync_config
from .. import sync_engine
from ..erpnext_client import ERPNextConfigError, ERPNextError
from ..plaid_client import PlaidConfigError, PlaidError
from ..models import (AuditEvent, BankTransaction, CategorizationRule,
                      GeneratedJournalEntry, IntercompanyTransferPair,
                      PlaidAccount, PlaidItem, PlaidStatement, PlaidSyncLog,
                      Supplier)

log = logging.getLogger('bankbridge.admin_ui')

bp = Blueprint('admin_ui', __name__)


def _password_ok(configured: str, provided: str) -> bool:
    """Verify the provided password against the configured value.

    A configured value that looks like a werkzeug password hash
    (`pbkdf2:…` / `scrypt:…`) is verified as one; anything else is treated as a
    plaintext secret and compared in constant time. Either way the comparison is
    timing-safe."""
    if configured.startswith(('pbkdf2:', 'scrypt:')):
        return check_password_hash(configured, provided)
    return hmac.compare_digest(configured, provided)


@bp.before_request
def _require_admin_auth():
    """OPTIONAL HTTP Basic Auth gate for the whole admin UI.

    Off by default: the admin UI assumes Umbrel's app_proxy provides the LAN
    trust boundary. If BOTH ADMIN_BASIC_AUTH_USER and ADMIN_BASIC_AUTH_PASS are
    configured, every /admin route requires those credentials — a
    belt-and-suspenders layer for operators who expose the app over public
    HTTPS. If either is blank, this is a no-op (backward compatible). The
    unauthenticated Plaid callback + JSON API live on a separate blueprint and
    are never affected."""
    user = current_app.config.get('ADMIN_BASIC_AUTH_USER', '')
    pw = current_app.config.get('ADMIN_BASIC_AUTH_PASS', '')
    if not user or not pw:
        return None  # auth disabled — stock LAN mode
    auth = request.authorization
    # Compare both fields even when the username is wrong so the response time
    # doesn't reveal which half failed.
    user_ok = bool(auth) and hmac.compare_digest(auth.username or '', user)
    pass_ok = bool(auth) and _password_ok(pw, auth.password or '')
    if user_ok and pass_ok:
        return None
    return Response(
        'Authentication required.', 401,
        {'WWW-Authenticate': 'Basic realm="Bank Bridge admin"'})


@bp.before_request
def _ensure_fresh_session():
    """Guarantee every admin view starts with a usable SQLAlchemy session.

    A failed ERPNext bootstrap (e.g. a broken/missing linked doctype throwing an
    unexpected error mid-request) could otherwise leave the scoped session in an
    aborted state, and the *next* DB query on the page would blow up with a
    psycopg2 OperationalError. A rollback on a clean session is a harmless no-op,
    so this is pure defense-in-depth."""
    try:
        db.session.rollback()
    except Exception:  # pragma: no cover - never block a request on cleanup
        db.session.remove()
    # Tag every AuditEvent written while serving this request as admin-driven,
    # with the caller's IP, so the audit trail records who/where.
    audit.set_context('admin_ui', request.remote_addr)


# ── Shared chrome ────────────────────────────────────────────────

BASE_CSS = """
 body{font-family:system-ui,sans-serif;margin:0;background:#fafafa;color:#222}
 header{background:#0f2a1d;color:#eee;padding:14px 24px;border-bottom:3px solid #2e9e5b}
 header h1{margin:0;font-size:20px}
 nav{background:#16352a;padding:8px 24px;display:flex;gap:4px;flex-wrap:wrap}
 nav a{color:#eee;text-decoration:none;padding:6px 12px;border-radius:4px;font-size:14px}
 nav a:hover{background:#2b4f3f}
 nav a.active{background:#2e9e5b;color:#06231a;font-weight:600}
 main{padding:24px;max-width:1000px;margin:0 auto}
 h2{margin-top:28px}
 table{border-collapse:collapse;width:100%;margin:12px 0;background:#fff}
 th,td{border:1px solid #ddd;padding:6px 10px;font-size:14px;text-align:left}
 th{background:#f4f4f4}
 .kpis{display:flex;gap:16px;flex-wrap:wrap}
 .kpi{border:1px solid #ccc;background:#fff;padding:12px 16px;border-radius:6px;min-width:130px}
 code{background:#f0f0f0;padding:2px 4px;border-radius:3px;font-size:13px}
 form.card{background:#fff;border:1px solid #ccc;border-radius:6px;padding:16px;margin:12px 0}
 form.card label{display:block;margin:8px 0;font-size:14px}
 form.card input, form.card select{width:100%;padding:6px;font-size:14px;box-sizing:border-box}
 form.card button, button.primary{margin-top:0;padding:8px 16px;background:#2e9e5b;color:#fff;border:0;border-radius:4px;font-weight:600;cursor:pointer}
 button.secondary{background:#fff;color:#333;border:1px solid #bbb;padding:8px 16px;border-radius:4px;cursor:pointer}
 .creds{background:#e8f5e9;border:1px solid #2e7d32;border-radius:6px;padding:14px 16px;margin:12px 0;color:#1b5e20}
 .warn{color:#a04000;font-weight:600}
 .banner-warn{background:#fff8e1;border:1px solid #f5a623;border-left:6px solid #f5a623;border-radius:6px;padding:12px 16px;margin:12px 0}
 .banner-warn h3{margin:0 0 6px;color:#a56d00;font-size:15px}
 .banner-ok{background:#e8f5e9;border:1px solid #2e7d32;border-left:6px solid #2e7d32;border-radius:6px;padding:12px 16px;margin:12px 0;color:#1b5e20;font-size:14px}
 .pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600}
 .pill-ok{background:#c8e6c9;color:#1b5e20}
 .pill-err{background:#ffcdd2;color:#b71c1c}
 .pill-muted{background:#eee;color:#555}
 .pill-warn{background:#ffe6a7;color:#8a5a00}
 td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
"""

NAV_HTML = """
<header><h1>ERPNext Bank Bridge <span style="font-weight:400;font-size:13px;color:#8fbfa5">Plaid → ERPNext</span></h1></header>
<nav>
  <a href="/admin" class="{{ 'active' if page == 'dashboard' else '' }}">Dashboard</a>
  <a href="/admin/link_bank" class="{{ 'active' if page == 'link_bank' else '' }}">Link a bank</a>
  <a href="/admin/accounts" class="{{ 'active' if page == 'accounts' else '' }}">Accounts</a>
  <a href="/admin/transactions" class="{{ 'active' if page == 'transactions' else '' }}">Transactions</a>
  <a href="/admin/rules" class="{{ 'active' if page == 'rules' else '' }}">Rules</a>
  <a href="/admin/suppliers" class="{{ 'active' if page == 'suppliers' else '' }}">Suppliers</a>
  <a href="/admin/counterparties" class="{{ 'active' if page == 'counterparties' else '' }}">Counterparties</a>
  <a href="/admin/generated_entries" class="{{ 'active' if page == 'generated_entries' else '' }}">Generated JEs</a>
  <a href="/admin/intercompany" class="{{ 'active' if page == 'intercompany' else '' }}">Intercompany</a>
  <a href="/admin/statements" class="{{ 'active' if page == 'statements' else '' }}">Statements</a>
  <a href="/admin/reconciliation" class="{{ 'active' if page == 'reconciliation' else '' }}">Reconciliation</a>
  <a href="/admin/strategy" class="{{ 'active' if page == 'investments' else '' }}">Strategy</a>
  <a href="/admin/advisory" class="{{ 'active' if page == 'advisory' else '' }}">Advisory</a>
  <a href="/admin/audit" class="{{ 'active' if page == 'audit' else '' }}">Audit</a>
  <a href="/admin/data_hygiene" class="{{ 'active' if page == 'hygiene' else '' }}" style="color:#8fbfa5">Hygiene</a>
  <a href="/admin/sync_log" class="{{ 'active' if page == 'sync_log' else '' }}">Sync Log</a>
  <a href="/admin/plaid_settings" class="{{ 'active' if page == 'plaid_settings' else '' }}">Plaid</a>
  <a href="/admin/erpnext_settings" class="{{ 'active' if page == 'erpnext_settings' else '' }}">ERPNext</a>
  {% if nav_companies %}
  <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
    <span style="color:#8fbfa5;font-size:12px">Current Company:</span>
    {% if nav_companies|length > 1 %}
    <form method="get" action="/admin/set_company" style="margin:0">
      <input type="hidden" name="next" value="{{ request.path }}">
      <select name="company" onchange="this.form.submit()"
        style="padding:4px 8px;font-size:13px;border-radius:4px;border:1px solid #2b4f3f;background:#0f2a1d;color:#eee">
        <option value="">All Companies</option>
        {% for c in nav_companies %}
        <option value="{{ c }}" {{ 'selected' if c == current_company else '' }}>{{ c }}</option>
        {% endfor %}
      </select>
    </form>
    {% else %}
    <b style="color:#eee;font-size:13px">{{ nav_companies[0] }}</b>
    {% endif %}
  </div>
  {% endif %}
</nav>
"""


def _current_company() -> str:
    """The Company the operator has scoped the whole admin UI to, or '' for
    'all Companies'. Stored in the session by /admin/set_company (the navbar
    switcher and the per-page Company filters all write here), so it's a single
    source of truth that every screen reads."""
    return (session.get('scope_company') or '').strip()


def _known_companies() -> list:
    """The ERPNext Companies this bridge actually holds accounts under: the
    distinct non-empty owning_company across linked Items and accounts, plus the
    active session scope (so a currently-selected Company stays visible even if
    no row references it yet). Derived from LOCAL rows only — no ERPNext
    round-trip — so the navbar is cheap and works when ERPNext is unreachable.
    Empty on a pre-multi-entity install, which keeps the navbar unchanged."""
    vals = set()
    for (c,) in db.session.query(PlaidItem.owning_company).distinct():
        if c and c.strip():
            vals.add(c.strip())
    for (c,) in db.session.query(PlaidAccount.owning_company).distinct():
        if c and c.strip():
            vals.add(c.strip())
    scope = _current_company()
    if scope:
        vals.add(scope)
    return sorted(vals)


def _resolve_account_companies() -> dict:
    """{account_id: resolved owning Company} for every Plaid account, using the
    v0.4.0 resolution order (per-account override → Item Company → ERPNext
    default). The default is read once from local settings (no ERPNext call).
    Feeds the Company transaction filter."""
    items = {it.item_id: (it.owning_company or '').strip()
             for it in PlaidItem.query.all()}
    default = None
    out = {}
    for a in PlaidAccount.query.all():
        company = (a.owning_company or '').strip() or items.get(a.item_id, '')
        if not company:
            if default is None:
                default = (erps.load().get('default_company') or '').strip()
            company = default
        out[a.account_id] = company
    return out


def _page(body_tmpl: str, page: str, **ctx):
    # Every page renders the navbar Company switcher from local data.
    ctx.setdefault('nav_companies', _known_companies())
    ctx.setdefault('current_company', _current_company())
    full = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>ERPNext Bank Bridge</title>
<style>{BASE_CSS}</style></head><body>
{NAV_HTML}
<main>{body_tmpl}</main>
</body></html>"""
    return render_template_string(full, page=page, **ctx)


@bp.get('/admin/set_company')
def set_company_scope():
    """Set (or clear) the session-wide Company scope that filters the admin UI.
    Driven by the navbar switcher and the per-page Company filter dropdowns. A
    blank `company` clears the scope back to 'all Companies'. Redirects to the
    caller's `next` path (local-only, to avoid an open redirect)."""
    session['scope_company'] = (request.args.get('company') or '').strip()
    # The offset-account feed is cached per Company; a scope change must not let
    # the dropdown keep serving the previous Company's accounts (v0.4.0.2).
    _invalidate_accounts_cache()
    nxt = request.args.get('next') or '/admin'
    if not nxt.startswith('/') or nxt.startswith('//'):
        nxt = '/admin'
    return redirect(nxt)


# ── Dashboard ────────────────────────────────────────────────────

DASHBOARD_BODY = """
<h2>Overview</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}

{% if not plaid_ok %}
<div class="banner-warn">
  <h3>Plaid isn't configured yet</h3>
  Add your Plaid <b>Client ID</b> and secret on the
  <a href="/admin/plaid_settings">Plaid settings</a> page, then
  <a href="/admin/link_bank">link a bank</a>.
</div>
{% endif %}
{% if not erpnext_ok %}
<div class="banner-warn">
  <h3>ERPNext isn't configured yet</h3>
  Transactions will be mirrored locally but not posted until you set the
  connection on the <a href="/admin/erpnext_settings">ERPNext settings</a> page
  and map accounts.
</div>
{% endif %}
{% if manual_only and counts.banks %}
<div class="banner-warn">
  <h3>Auto-sync is off (manual only)</h3>
  Transactions refresh only when you click <b>Sync now</b> below.
  Last synced: <b>{{ last_synced_human }}</b>.
  Change the cadence on the <a href="/admin/plaid_settings">Plaid settings</a>
  page.
</div>
{% endif %}

<div class="kpis">
  <div class="kpi"><b>{{ counts.banks }}</b><br>linked banks</div>
  <div class="kpi"><b>{{ counts.accounts }}</b><br>accounts ({{ counts.mapped }} mapped)</div>
  <div class="kpi"><b>{{ counts.transactions }}</b><br>transactions</div>
  <div class="kpi"><b>{{ counts.posted }}</b><br>posted to ERPNext</div>
  <div class="kpi"><b>{{ counts.pending }}</b><br>pending push</div>
</div>

<div style="margin:16px 0">
  <form method="post" action="/admin/sync_now" style="display:inline">
    <button type="submit" class="primary">Sync now</button>
  </form>
  <a href="/admin/link_bank" class="secondary" style="text-decoration:none;display:inline-block;margin-left:8px">Link a bank</a>
</div>

<h2>Linked banks</h2>
<table>
  <tr><th>Institution</th><th>Item</th><th>Status</th><th>Last synced</th><th>Last error</th></tr>
  {% for it in items %}
  <tr>
    <td>{{ it.institution_name or '(unknown)' }}</td>
    <td><code>{{ it.item_id[:14] }}…</code></td>
    <td>
      {# v0.4.18 · disconnected wins over any Plaid webhook status. The status
         column reflected the raw Plaid state field and left disconnected
         items showing 'active' because Plaid never sends a status change on
         /item/remove; here we surface the operator's disconnect explicitly. #}
      {% if it.disconnected %}<span class="pill pill-muted"
        title="Disconnected at Plaid on {{ it.disconnected_at.strftime('%Y-%m-%d %H:%M') if it.disconnected_at else 'an earlier date' }} — no new transactions will arrive.">disconnected</span>
      {% elif it.status == 'active' %}<span class="pill pill-ok">active</span>
      {% elif it.status == 'error' %}<span class="pill pill-err">error</span>
      {% else %}<span class="pill pill-muted">{{ it.status }}</span>{% endif %}
    </td>
    <td>{{ it.last_synced_at.strftime('%Y-%m-%d %H:%M') if it.last_synced_at else '—' }}</td>
    <td style="font-size:12px;color:#a04000;max-width:260px;overflow:hidden;text-overflow:ellipsis">{{ (it.last_error or '')[:120] }}</td>
  </tr>
  {% endfor %}
  {% if not items %}<tr><td colspan="5" style="color:#888">No banks linked yet.</td></tr>{% endif %}
</table>

<h2>Recent sync activity</h2>
<table>
  <tr><th>At</th><th>Item</th><th>Direction</th><th>Count</th><th>Status</th><th>Detail</th></tr>
  {% for r in recent_log %}
  <tr>
    <td style="font-size:12px">{{ r.at.strftime('%Y-%m-%d %H:%M:%S') if r.at else '' }}</td>
    <td><code style="font-size:12px">{{ (r.item_id or '')[:12] }}</code></td>
    <td>{{ r.direction }}</td>
    <td class="num">{{ r.count }}</td>
    <td>{% if r.status == 'success' %}<span class="pill pill-ok">success</span>{% else %}<span class="pill pill-err">{{ r.status }}</span>{% endif %}</td>
    <td style="font-size:12px">{{ (r.error_message or '')[:120] }}</td>
  </tr>
  {% endfor %}
  {% if not recent_log %}<tr><td colspan="6" style="color:#888">Nothing synced yet.</td></tr>{% endif %}
</table>
"""


def _counts() -> dict:
    # v0.5.8 · the headline "total transactions" KPI is a user-facing
    # aggregation, so it counts each re-link twin once (retired-account rows
    # excluded). The posted/pending breakdowns below key off ERPNext push state,
    # not merchant identity, so they stay on the raw mirror.
    total_txn = av.visible_bank_transactions_query().count()
    posted = BankTransaction.query.filter(
        BankTransaction.posted_at.isnot(None),
        BankTransaction.removed.is_(False)).count()
    pending = BankTransaction.query.filter(
        BankTransaction.posted_at.is_(None)).count()
    mapped = PlaidAccount.query.filter(
        PlaidAccount.erpnext_bank_account_name.isnot(None)).count()
    # v0.4.18 · key is `banks`, not `items`. Jinja's `counts.items` resolves to
    # dict.items (the METHOD) via getattr, never to `counts['items']` — the
    # dashboard was rendering "<built-in method items of dict object at 0x…>"
    # instead of the linked-bank count. Rename to a semantic key that can't
    # shadow a dict attribute.
    return {
        'banks': PlaidItem.query.count(),
        'accounts': av.visible_accounts_query().count(),
        'mapped': mapped,
        'transactions': total_txn,
        'posted': posted,
        'pending': pending,
    }


def _ago(dt) -> str:
    """Compact 'time since' label for the manual-only 'last synced' reminder."""
    if dt is None:
        return 'never'
    now = datetime.now(timezone.utc)
    # Stored timestamps are naive UTC (SQLite/Postgres); compare in UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 90:
        return 'just now'
    mins = secs // 60
    if mins < 90:
        return f'{mins} minute{"s" if mins != 1 else ""} ago'
    hours = mins // 60
    if hours < 48:
        return f'{hours} hour{"s" if hours != 1 else ""} ago'
    days = hours // 24
    return f'{days} day{"s" if days != 1 else ""} ago'


@bp.get('/')
@bp.get('/admin')
def dashboard():
    items = PlaidItem.query.order_by(PlaidItem.created_at.desc()).all()
    recent_log = (PlaidSyncLog.query
                  .order_by(PlaidSyncLog.at.desc()).limit(15).all())
    last_synced = db.session.query(
        db.func.max(PlaidItem.last_synced_at)).scalar()
    return _page(DASHBOARD_BODY, page='dashboard', counts=_counts(),
                 items=items, recent_log=recent_log,
                 plaid_ok=ps.is_configured(),
                 erpnext_ok=erps.is_configured(),
                 manual_only=not sync_config.is_auto_sync_enabled(
                     ps.sync_interval_hours()),
                 last_synced_human=_ago(last_synced),
                 flash_msg=request.args.get('flash', ''))


@bp.post('/admin/sync_now')
def sync_now_ui():
    if not ps.is_configured():
        return redirect('/admin?flash=' + quote_plus('Plaid not configured'))
    try:
        result = sync_engine.sync_all()
    except Exception as e:  # surface any Plaid/ERPNext error to the operator
        return redirect('/admin?flash=' + quote_plus(f'Sync failed: {e}'))
    n = result.get('items', 0)
    return redirect('/admin?flash=' + quote_plus(f'Sync complete across {n} bank(s).'))


# ── Link a bank ──────────────────────────────────────────────────

LINK_BANK_BODY = """
<h2>Link a bank</h2>
{% if not plaid_ok %}
<div class="banner-warn"><h3>Configure Plaid first</h3>
Set your Client ID + secret on the <a href="/admin/plaid_settings">Plaid settings</a> page.</div>
{% else %}
<p style="font-size:14px;color:#555">
  Opens Plaid Link. Sign in to your bank (Wells Fargo, Columbia Bank, or any
  Plaid-supported institution). OAuth banks bounce through
  <code>{{ redirect_uri }}</code> and back automatically. After linking, map
  each account to an ERPNext Bank Account on the
  <a href="/admin/accounts">Accounts</a> page.
</p>
<p>Environment: <span class="pill {{ 'pill-ok' if env == 'production' else 'pill-muted' }}">{{ env }}</span></p>

{% if companies %}
<div class="card" style="max-width:520px">
  <label for="owningCompany" style="display:block;font-size:14px;font-weight:600;margin-bottom:4px">
    Owning Company
  </label>
  <select id="owningCompany" style="width:100%;padding:6px;font-size:14px;box-sizing:border-box">
    {% for c in companies %}
    <option value="{{ c }}" {{ 'selected' if c == selected_company else '' }}>{{ c }}</option>
    {% endfor %}
  </select>
  <p style="font-size:12px;color:#777;margin:6px 0 0">
    The ERPNext Company that will own every account from this bank. Entities own
    the accounts — Bank Bridge just provides the pipeline. You can correct an
    individual account later on the <a href="/admin/accounts">Accounts</a> page.
  </p>
</div>
{% elif erpnext_ok %}
<p style="font-size:13px;color:#a04000">Couldn't load your ERPNext Companies — new
  accounts will fall back to the default Company. You can set each account's
  Company on the <a href="/admin/accounts">Accounts</a> page after linking.</p>
{% endif %}

<button id="linkBtn" class="primary" disabled>Loading Plaid Link…</button>
<p id="linkStatus" style="font-size:13px;color:#555;margin-top:12px"></p>
{% endif %}

<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
{% raw %}
<script>
(function () {
  var btn = document.getElementById('linkBtn');
  var statusEl = document.getElementById('linkStatus');
  if (!btn) return;

  function setStatus(t) { statusEl.textContent = t; }

  // v0.4.0 multi-entity: persist the chosen owning Company to the session so
  // the exchange can stamp it on the new Item.
  var companyEl = document.getElementById('owningCompany');
  function saveCompany() {
    if (!companyEl) return Promise.resolve();
    return fetch('/bankbridge/api/plaid/set_link_company', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ company: companyEl.value })
    }).catch(function () {});
  }
  if (companyEl) { companyEl.addEventListener('change', saveCompany); }

  fetch('/bankbridge/api/plaid/create_link_token', { method: 'POST' })
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      if (!res.ok) { setStatus(res.j.error || 'Could not create a link token.'); return; }
      var handler = Plaid.create({
        token: res.j.link_token,
        onSuccess: function (public_token) {
          setStatus('Linked! Saving accounts…');
          saveCompany().then(function () {
            return fetch('/bankbridge/api/plaid/exchange_token', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ public_token: public_token })
            });
          }).then(function (r) { return r.json(); })
            .then(function (j) {
              if (j.error) { setStatus('Exchange failed: ' + j.error); return; }
              window.location.href = '/admin/accounts';
            })
            .catch(function () { setStatus('Exchange request failed.'); });
        },
        onExit: function (err) {
          if (err) { setStatus('Link exited: ' + (err.display_message || err.error_message || err.error_code || '')); }
        }
      });
      btn.disabled = false;
      btn.textContent = 'Open Plaid Link';
      btn.addEventListener('click', function () { handler.open(); });
    })
    .catch(function () { setStatus('Could not reach the server to create a link token.'); });
})();
</script>
{% endraw %}
"""


@bp.get('/admin/link_bank')
def link_bank_page():
    s = ps.load()
    # v0.4.0 multi-entity: offer the owning-Company picker (best-effort). Default
    # to the ERPNext default Company and seed the session so an unchanged pick
    # still reaches the exchange.
    companies, selected_company = [], (erps.load().get('default_company') or '').strip()
    if erps.is_configured():
        try:
            companies = erpnext_bank.list_companies()
        except (ERPNextConfigError, ERPNextError):
            companies = []
    if selected_company and selected_company not in companies:
        # Keep the configured default visible even if the list call came back
        # empty or didn't include it.
        companies = [selected_company] + companies
    if not selected_company and companies:
        selected_company = companies[0]
    if selected_company:
        session['link_owning_company'] = selected_company
    return _page(LINK_BANK_BODY, page='link_bank', plaid_ok=ps.is_configured(),
                 env=s['environment'], redirect_uri=s['redirect_uri'],
                 companies=companies, selected_company=selected_company,
                 erpnext_ok=erps.is_configured())


# ── Accounts (ERPNext Bank Account mapping) ──────────────────────

def _opening_balance_cell(a) -> str:
    """The Opening Balance column for one account: a state pill, plus a Book
    button whenever booking one is the useful next action.

    An account with no opening balance is reporting "movement since we started
    tracking" rather than what it holds, so `none` is rendered as a prompt, not
    as a neutral dash — that gap is exactly the thing v0.4.4 exists to close."""
    status = obal.opening_balance_status(a)
    entry = obal.existing_entry(a)
    amount = f' · {entry.amount:,.2f}' if entry and entry.amount else ''
    pills = {
        'booked': ('pill-ok', 'booked' + amount,
                   'Submitted in ERPNext — the balance sheet includes it'),
        'pending': ('pill-muted', 'pending review' + amount,
                    'A Draft Journal Entry is waiting for approval on the '
                    'Generated Journal Entries page'),
        'rejected': ('pill-muted', 'rejected',
                     'You rejected this opening balance; Book again to replace it'),
        'reversed': ('pill-muted', 'reversed', 'The opening balance was undone'),
        'error': ('pill-err', 'error',
                  (entry.error_message or '')[:200] if entry else ''),
    }
    if status == 'none':
        cls, label, title = ('pill-warn', 'not booked',
                             "This account's balance sheet shows only movement "
                             'since it was linked, not what it actually holds')
    else:
        cls, label, title = pills.get(status, ('pill-muted', status, ''))
    t = f' title="{title}"' if title else ''
    out = f'<span class="pill {cls}"{t}>{label}</span>'
    # Booking is offered when there's nothing booked yet, when the previous
    # attempt errored, or to replace a rejection — never over a live entry.
    if status in ('none', 'error', 'rejected') and a.erpnext_gl_account_name:
        verb = 'Book' if status == 'none' else 'Book again'
        out += (
            f'<form method="post" '
            f'action="/api/accounts/{a.account_id}/book_opening_balance" '
            f'style="display:flex;gap:4px;align-items:center;margin:4px 0 0">'
            f'<input type="text" name="amount" placeholder="'
            f'{(a.balance_current if a.balance_current is not None else 0):.2f}" '
            f'style="width:78px;padding:3px;font-size:12px" '
            f'title="Override the amount — blank uses the current Plaid balance">'
            f'<input type="date" name="posting_date" '
            f'style="width:120px;padding:3px;font-size:12px" '
            f'title="Backdate the entry — blank uses OPENING_BALANCE_DATE">'
            f'<button type="submit" class="secondary" '
            f'style="padding:3px 8px;font-size:12px">{verb}</button></form>')
    return out


ACCOUNTS_BODY = """
<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
  <h2 style="margin:0">Linked banks</h2>
  {% if groups %}
  <form method="post" action="/admin/accounts/import_all" style="margin:0">
    <button type="submit" class="primary" style="padding:10px 18px;font-size:15px"
      {{ '' if erpnext_ok else 'disabled title=\"Configure ERPNext first\"' }}>
      Import all supported accounts to ERPNext
    </button>
  </form>
  {% endif %}
</div>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
{% if erpnext_ok %}
<p style="font-size:13px;color:#666;margin:6px 0 0">
  Left-over Bank Accounts in ERPNext from an earlier dry-run?
  <a href="/admin/accounts/cleanup">Clean up unlinked accounts</a>.
</p>
{% endif %}
{% if bootstrap_unavailable %}
<div class="banner-warn">
  <h3>ERPNext bootstrap partially failed — some import features may not work</h3>
  This ERPNext instance is missing one or more linked doctypes
  (<b>{{ bootstrap_unavailable|join(', ') }}</b>). Imports still run, but the
  dependent fields are dropped from each Bank Account. Check the
  <a href="/admin/sync_log">Sync Log</a> for details.
</div>
{% endif %}
<p style="font-size:14px;color:#555">
  <b>Create in ERPNext</b> makes the matching <b>Bank</b> + <b>Bank Account</b>
  records for you (deduped — safe to click again) and maps + enables sync in one
  step. Or map to an existing ERPNext Bank Account with the dropdown. Only
  mapped + enabled accounts have their transactions posted.
  {% if not erpnext_ok %}<br><span class="warn">ERPNext isn't configured yet —
    set it on the <a href="/admin/erpnext_settings">ERPNext settings</a> page to
    enable one-click import.</span>{% endif %}
  {% if erp_error %}<br><span class="warn">Couldn't load ERPNext Bank Accounts: {{ erp_error }}</span>{% endif %}
</p>

{# v0.4.44 · sandbox visibility. Discoverable HERE, on the page whose contents
   it changes, because 'where did my account go' is the failure mode of hiding
   and a toggle buried on a settings page does not answer it. #}
{% if visibility.total_sandbox %}
<form method="post" action="/admin/settings/sandbox_visibility"
      style="margin:8px 0;font-size:13px;color:#555">
  <input type="hidden" name="next" value="/admin/accounts">
  <input type="hidden" name="include_sandbox_accounts"
         value="{{ '' if visibility.showing else '1' }}">
  {% if visibility.showing %}
    Showing <b>{{ visibility.total_sandbox }}</b> Plaid Sandbox test account(s)
    (Company <code>{{ visibility.company }}</code>).
    <button type="submit" class="secondary"
            style="padding:3px 10px;font-size:12px">Hide sandbox accounts</button>
  {% else %}
    <b>{{ visibility.hidden }}</b> Plaid Sandbox test account(s) are hidden
    (Company <code>{{ visibility.company }}</code>). Nothing was deleted.
    <button type="submit" class="secondary"
            style="padding:3px 10px;font-size:12px">Show sandbox accounts</button>
  {% endif %}
</form>
{% endif %}
{% if groups and not any_imported %}
<div class="banner-warn"><h3>Nothing imported yet</h3>
  Click <b>Import all supported accounts</b> above to bring your Plaid accounts
  into ERPNext in one click — or use the per-row <b>Create in ERPNext</b> button.
</div>
{% endif %}

{% for grp in groups %}
<h3 style="margin-bottom:2px">{{ grp.item.institution_name or '(unknown institution)' }}
  <span style="font-weight:400;color:#888;font-size:13px">· {{ grp.item.item_id[:14] }}…</span>
  {% if grp.item.disconnected %}
  <span class="pill pill-muted" style="vertical-align:middle;margin-left:6px"
        title="Disconnected at Plaid on {{ grp.item.disconnected_at.strftime('%Y-%m-%d %H:%M') if grp.item.disconnected_at else 'an earlier date' }} — no new transactions will arrive. Existing data is retained.">🔌 Disconnected</span>
  {% else %}
  <!-- Data attributes + delegation rather than an inline onclick: an
       institution name is arbitrary text, and interpolating it into a JS string
       inside an HTML attribute breaks the moment it contains a quote. Jinja's
       autoescaping handles attribute VALUES correctly, so this shape can't be
       broken by the bank's name. -->
  {% if grp.item.needs_reauth %}
  <span class="pill" style="vertical-align:middle;margin-left:6px;
        background:#fff3cd;border-color:#f0d68a;color:#7a5b00"
        title="{{ grp.item.reauth_reason or 'Re-authentication required' }} — syncing is paused for this bank, so no Plaid calls are being spent on it.">⚠ Needs reconnect</span>
  <button type="button"
          style="vertical-align:middle;margin-left:8px;padding:3px 10px;font-size:12px"
          data-bb-reconnect="{{ grp.item.item_id }}"
          data-bb-name="{{ grp.item.institution_name or 'this bank' }}">
    Reconnect
  </button>
  {% endif %}
  <button type="button" class="secondary"
          style="vertical-align:middle;margin-left:8px;padding:3px 10px;font-size:12px"
          data-bb-disconnect="{{ grp.item.item_id }}"
          data-bb-name="{{ grp.item.institution_name or 'this bank' }}">
    Disconnect this bank
  </button>
  {% endif %}
</h3>
{% if grp.item.needs_reauth %}
<div class="banner-warn" style="margin:4px 0 10px">
  <p style="font-size:14px;margin:0">
    <b>{{ grp.item.institution_name or 'This bank' }} needs you to sign in
    again.</b> {{ grp.reauth_help }}
    Syncing for this bank is <b>paused</b> until you reconnect — no Plaid calls
    are being spent on it in the meantime. Reconnecting repairs this same
    connection, so your account mappings and Company assignment are unaffected.
  </p>
</div>
{% endif %}
<div style="margin:0 0 8px;font-size:13px;color:#555">
  Owning Company: <b>{{ grp.item.owning_company or '(unassigned — uses ERPNext default)' }}</b>
  {% if companies %}
  <a href="#" style="font-size:12px;margin-left:6px"
     onclick="document.getElementById('itemco-{{ loop.index }}').style.display='flex';this.style.display='none';return false">[Change]</a>
  <form id="itemco-{{ loop.index }}" method="post"
        action="/admin/items/{{ grp.item.item_id }}/set_company"
        style="display:none;gap:6px;align-items:center;margin-top:6px"
        onsubmit="return confirm('Reassign EVERY account under {{ (grp.item.institution_name or 'this bank')|replace(\"'\", '') }} to this Company? This retroactively changes which entity their Bank Accounts and generated Journal Entries book to. (Per-account overrides are kept.)')">
    <select name="owning_company" style="padding:4px;font-size:12px">
      <option value="">— unassigned (use ERPNext default) —</option>
      {% for c in companies %}
      <option value="{{ c }}" {{ 'selected' if c == grp.item.owning_company else '' }}>{{ c }}</option>
      {% endfor %}
      {% if grp.item.owning_company and grp.item.owning_company not in companies %}
      <option value="{{ grp.item.owning_company }}" selected>{{ grp.item.owning_company }} (current)</option>
      {% endif %}
    </select>
    <button type="submit" class="secondary" style="padding:4px 10px;font-size:12px">Reassign Company</button>
    <span style="font-size:11px;color:#999">correction only</span>
  </form>
  {% endif %}
  {% if grp.has_investment %}
  {# v0.5.1 · the investment-JE kill switch. OFF by default; these are real
     P&L entries, so posting is opt-in per Item. #}
  <br><form method="post"
        action="/admin/items/{{ grp.item.item_id }}/invest_je_posting"
        style="display:inline-flex;gap:6px;align-items:center;margin-top:6px"
        onsubmit="return confirm('{{ 'STOP posting' if grp.item.invest_je_posting_enabled else 'START posting' }} investment transactions as Journal Entries for this connection? These are real accounting entries that hit the P&L.')">
    <input type="hidden" name="enabled"
           value="{{ '0' if grp.item.invest_je_posting_enabled else '1' }}">
    <span style="font-size:12px">Investment JE posting:
      <b style="color:{{ '#1b5e20' if grp.item.invest_je_posting_enabled else '#a04000' }}">
        {{ 'ON' if grp.item.invest_je_posting_enabled else 'OFF' }}</b></span>
    <button type="submit" class="secondary" style="padding:3px 10px;font-size:12px">
      {{ 'Turn off' if grp.item.invest_je_posting_enabled else 'Turn on' }}</button>
    <span style="font-size:11px;color:#999">real P&amp;L entries — opt-in</span>
  </form>
  {% endif %}
</div>
<table>
  <tr><th>Account</th><th>Mask</th><th>Type</th><th class="num">Balance</th>
      <th>ERPNext Bank Account</th><th>Import</th><th>Opening Balance</th>
      <th>Company</th></tr>
  {% for a in grp.accounts %}
  <tr>
    <td>{{ a.name or a.official_name or '(unnamed)' }}</td>
    <td><code>••{{ a.mask or '??' }}</code>
      {% if a.account_id in sandbox_ids %}
      <br><span class="pill pill-warn" style="font-size:10px"
            title="A Plaid Sandbox test account, shown because 'Show sandbox accounts' is on. Not real money.">SANDBOX</span>
      {% endif %}
      {# v0.4.44 · pairing. A WF Advisors brokerage account and its Brokerage
         Cash Services companion are one economic account: the brokerage side
         holds the statements and securities activity, the companion holds
         every cash movement. Reconciliation needs both. #}
      {% set options = pair_options.get(a.account_id, []) %}
      {% if a.paired_account_id %}
      <br><span style="font-size:11px;color:#1b5e20" title="Cash-side transactions from this account are counted in {{ a.name or a.account_id }}'s reconciliation.">
        &#128279; ••{{ pair_masks.get(a.paired_account_id, '????') }}</span>
      {% endif %}
      {% if options %}
      <br><a href="#" style="font-size:11px"
         onclick="document.getElementById('pair-{{ a.account_id }}').style.display='block';this.style.display='none';return false">[{{ 'change pair' if a.paired_account_id else 'pair' }}]</a>
      <form id="pair-{{ a.account_id }}" method="post"
            action="/admin/accounts/pair" style="display:none;margin-top:4px">
        <input type="hidden" name="account_id" value="{{ a.account_id }}">
        <select name="paired_account_id" style="font-size:11px;padding:3px;width:100%">
          <option value="">— unpaired —</option>
          {% for o in options %}
          <option value="{{ o.account_id }}"
            {{ 'selected' if o.account_id == a.paired_account_id else '' }}>
            ••{{ o.mask or '????' }} {{ o.name or o.account_id }}
          </option>
          {% endfor %}
        </select>
        <button type="submit" class="secondary"
                style="padding:3px 8px;font-size:11px;margin-top:3px">Save pair</button>
        <div style="font-size:10px;color:#777;margin-top:3px;max-width:190px">
          Candidates are depository accounts under this Plaid connection,
          and only ones not already paired to another account — a Brokerage
          Cash Services companion arrives on the same connection as the
          brokerage account it serves, and serves exactly one.
        </div>
      </form>
      {% endif %}
    </td>
    <td>{{ a.type }}{% if a.subtype %} / {{ a.subtype }}{% endif %}
      {% if a.balance_only %}<br><span class="pill pill-muted"
        title="Plaid doesn't return investment transactions without the investments product — only the balance is mirrored">balance-only, no transactions</span>{% endif %}</td>
    <td class="num">{{ '%.2f'|format(a.balance_current) if a.balance_current is not none else '—' }} {{ a.iso_currency_code }}
      {# v0.4.43 · the one-line answer to "is this account's Plaid data telling
         the whole story?" — Σ of what the BANK saw and Plaid never reported,
         across every anchored statement period. Shown here because this is the
         page an operator is already on when they wonder. #}
      {% set anc = anchor_map.get(a.account_id) %}
      {% if not anc and reconciles_under.get(a.account_id) %}
      {# The cash side of a pair has no anchors of its own — its transactions
         are reconciled on the brokerage account. Point there rather than
         leaving this row looking unreconciled. #}
      <br><a href="/admin/reconciliation/{{ reconciles_under[a.account_id] }}"
             style="font-size:11px;color:#666;text-decoration:none"
             title="This is the cash side of a paired brokerage account. Its transactions are reconciled there.">
        &#128279; reconciled with ••{{ pair_masks.get(reconciles_under[a.account_id], '????') }}</a>
      {% endif %}
      {% if anc and anc.periods %}
      <br><a href="/admin/reconciliation/{{ a.account_id }}"
             style="font-size:11px;text-decoration:none"
             title="Statement-anchored variance across {{ anc.periods }} period(s): money the bank recorded that Plaid never reported. Click for the full chain.">
        {% if anc.variance|abs > 0.005 or anc.gaps %}
        <span class="pill pill-err" style="font-size:10px">&#9888;
          {{ '%+.2f'|format(anc.variance) }} unexplained</span>
        {% else %}
        <span class="pill pill-ok" style="font-size:10px">&#10003; statements
          reconcile</span>
        {% endif %}
      </a>
      {% endif %}
    </td>
    <td>
      <form method="post" action="/admin/accounts/map" style="display:flex;gap:8px;align-items:center;margin:0">
        <input type="hidden" name="account_id" value="{{ a.account_id }}">
        <select name="erpnext_bank_account_name" style="flex:1;padding:5px;font-size:13px">
          <option value="">— unmapped —</option>
          {% for ba in bank_accounts %}
          <option value="{{ ba.name }}" {{ 'selected' if ba.name == a.erpnext_bank_account_name else '' }}>
            {{ ba.name }}{% if ba.bank_account_no %} ({{ ba.bank_account_no }}){% endif %}
          </option>
          {% endfor %}
          {% if a.erpnext_bank_account_name and a.erpnext_bank_account_name not in bank_account_names %}
          <option value="{{ a.erpnext_bank_account_name }}" selected>{{ a.erpnext_bank_account_name }} (current)</option>
          {% endif %}
        </select>
        <label style="font-size:13px;white-space:nowrap;margin:0">
          <input type="checkbox" name="sync_enabled" value="1" {{ 'checked' if a.sync_enabled else '' }} style="width:auto"> sync
        </label>
        <button type="submit" class="primary" style="padding:5px 12px">Save</button>
      </form>
    </td>
    <td style="white-space:nowrap">
      {% if a.erpnext_bank_account_name %}
        <span class="pill pill-ok">imported</span>
      {% elif supported_map.get(a.account_id) %}
        <form method="post" action="/admin/accounts/create" style="margin:0">
          <input type="hidden" name="account_id" value="{{ a.account_id }}">
          <button type="submit" class="primary" style="padding:5px 12px"
            {{ '' if erpnext_ok else 'disabled title=\"Configure ERPNext first\"' }}>
            Create in ERPNext
          </button>
        </form>
      {% else %}
        <span class="pill pill-muted" title="Not a Bank Account in ERPNext's model">not supported</span>
      {% endif %}
      {% if unpostable.get(a.account_id) %}
      <!-- v0.4.13 · this account's transactions ARE being mirrored, they just
           have nowhere to post. Saying so beats the operator wondering where a
           loan's payments went — the realistic case is a mortgage sharing an
           Item with a checking account. -->
      <div style="font-size:11px;color:#8a5a00;margin-top:4px"
           title="Mirrored locally but not posted to ERPNext, because this account is not mapped (or sync is off). They post automatically once it is.">
        {{ unpostable[a.account_id] }} txn{{ '' if unpostable[a.account_id] == 1 else 's' }} waiting
      </div>
      {% endif %}
    </td>
    <td style="white-space:nowrap">{{ opening_cell(a)|safe }}</td>
    <td>
      {% if companies %}
      <form method="post" action="/api/accounts/{{ a.account_id }}/set_company"
            style="display:flex;gap:6px;align-items:center;margin:0"
            title="Override owning Company (correction only)">
        <select name="owning_company" style="flex:1;padding:4px;font-size:12px">
          <option value="">— inherit ({{ grp.item.owning_company or 'default' }}) —</option>
          {% for c in companies %}
          <option value="{{ c }}" {{ 'selected' if c == a.owning_company else '' }}>{{ c }}</option>
          {% endfor %}
          {% if a.owning_company and a.owning_company not in companies %}
          <option value="{{ a.owning_company }}" selected>{{ a.owning_company }} (current)</option>
          {% endif %}
        </select>
        <button type="submit" class="secondary" style="padding:4px 8px;font-size:12px">Set</button>
      </form>
      {% else %}
        <span style="font-size:12px;color:#999">{{ a.owning_company or grp.item.owning_company or '—' }}</span>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
</table>
{% endfor %}
{% if not groups %}<p style="color:#888">No accounts yet — <a href="/admin/link_bank">link a bank</a>.</p>{% endif %}

<!-- v0.4.7 · disconnect confirmation. One modal for the whole page; the button
     on each bank's header fills in the institution name before showing it.
     Disconnecting is irreversible (Plaid invalidates the access_token), so it
     is deliberately a two-step action and the copy says plainly what is and is
     NOT destroyed. -->
<div id="bbDisconnectModal"
     style="display:none;position:fixed;inset:0;z-index:100;
            background:rgba(0,0,0,.45);align-items:center;justify-content:center">
  <div class="card" style="max-width:520px;margin:0;background:#fff">
    <h2 style="margin-top:0;font-size:19px">Disconnect <span id="bbDiscName">this bank</span>?</h2>
    <p style="font-size:14px;color:#444;line-height:1.5">
      Plaid will stop sending new transactions. Your existing transactions and
      generated Journal Entries stay in ERPNext.
    </p>
    <p style="font-size:13px;color:#444;line-height:1.5">
      You can link this bank again later, but Plaid issues <b>new account ids</b>
      for a new connection. Bank Bridge will try to recognise each account by its
      last-4, type and subtype and carry its ERPNext mapping across — if an
      account can't be matched unambiguously, you'll re-map that one by hand.
      <b>If you only need to fix expired credentials, use Reconnect instead</b> —
      it repairs this connection in place and nothing is re-mapped.
    </p>
    <div id="bbDiscError"
         style="display:none;font-size:13px;color:#a00;background:#fff3f3;
                border:1px solid #f0caca;border-radius:6px;padding:8px 10px;
                margin:10px 0"></div>
    <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">
      <button type="button" class="secondary" id="bbDiscCancel"
              style="padding:6px 16px" onclick="bbDisconnectClose()">Cancel</button>
      <button type="button" class="primary" id="bbDiscGo"
              style="padding:6px 16px">Disconnect</button>
    </div>
  </div>
</div>
{% if loan_summaries %}
<!-- v0.4.14 · loans. Their own transactions are never posted (the payment is
     booked from the account the money left), so the balance and the lender's
     figures ARE the account, and they need somewhere to be visible. -->
<h2 style="margin-top:28px">Loans</h2>
<p style="font-size:14px;color:#555">
  A loan payment is two things at once: interest (a cost) and principal
  (settling debt). Bank Bridge books the <b>interest</b> automatically from the
  lender's own year-to-date figures. Book the <b>payment</b> itself with a rule
  on the account the money leaves, pointing at the loan's GL account — that
  debits the loan and credits your bank, which is the other half.
</p>
<table>
  <tr><th>Loan</th><th class="num">Balance</th><th class="num">Rate</th>
      <th>Next payment</th><th class="num">YTD interest</th>
      <th>Interest split</th></tr>
  {% for ln in loan_summaries %}
  <tr>
    <td>{{ ln.label }}{% if ln.liability_type %}
        <span style="color:#888;font-size:12px">· {{ ln.liability_type }}</span>{% endif %}</td>
    <td class="num">{% if ln.balance is not none %}{{ '%.2f'|format(ln.balance) }}{% else %}—{% endif %}</td>
    <td class="num">{% if ln.interest_rate is not none %}{{ '%.3f'|format(ln.interest_rate) }}%{% else %}—{% endif %}</td>
    <td>{% if ln.next_payment_due_date %}{{ ln.next_payment_due_date }}
        {% if ln.minimum_payment_amount is not none %}
        · {{ '%.2f'|format(ln.minimum_payment_amount) }}{% endif %}
        {% else %}—{% endif %}</td>
    <td class="num">{% if ln.ytd_interest_paid is not none %}{{ '%.2f'|format(ln.ytd_interest_paid) }}{% else %}—{% endif %}</td>
    <td>
      {% if not ln.gl_account %}
      <span class="pill pill-muted" title="Import this loan to ERPNext first">not imported</span>
      {% elif ln.interest_split_available %}
      <span class="pill pill-ok" title="Interest is booked automatically from the lender's year-to-date figures">automatic</span>
      {% else %}
      <span class="pill pill-warn"
            title="This lender does not report year-to-date interest through Plaid, so Bank Bridge will not guess. Book interest by hand from the lender's statement.">manual</span>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
</table>
<p style="font-size:13px;color:#555">
  Loan transactions are mirrored but never posted — the payment is booked from
  the account the money left, and posting the loan's own copy of the same event
  would double-count it.
</p>
{% endif %}

{% if any_needs_reauth %}
<!-- v0.4.11 · Plaid Link, for the Reconnect button only. Loaded conditionally
     so an accounts page with nothing to reconnect — the normal case — fetches
     no third-party script at all. -->
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
{% endif %}
<script>
var bbDiscItem = null;
document.addEventListener('click', function (e) {
  var t = e.target;
  while (t && t !== document && !(t.getAttribute && t.getAttribute('data-bb-disconnect'))) {
    t = t.parentNode;
  }
  if (t && t.getAttribute && t.getAttribute('data-bb-disconnect')) {
    bbDisconnect(t.getAttribute('data-bb-disconnect'),
                 t.getAttribute('data-bb-name'));
  }
});

// v0.4.11 · Reconnect = Plaid Link in UPDATE MODE. The link token is minted
// against the Item's existing access_token, so Link re-authenticates the
// connection we already have rather than creating a second one. On success
// there is nothing to exchange — the access_token never changed — so we just
// tell the server to un-park the Item.
document.addEventListener('click', function (e) {
  var t = e.target;
  while (t && t !== document && !(t.getAttribute && t.getAttribute('data-bb-reconnect'))) {
    t = t.parentNode;
  }
  if (t && t.getAttribute && t.getAttribute('data-bb-reconnect')) {
    bbReconnect(t, t.getAttribute('data-bb-reconnect'));
  }
});
function bbReconnect(btn, itemId) {
  var original = btn.textContent;
  btn.disabled = true; btn.textContent = 'Opening…';
  function fail(msg) {
    btn.disabled = false; btn.textContent = original;
    alert('Reconnect failed: ' + msg);
  }
  fetch('/bankbridge/api/plaid/create_link_token', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({item_id: itemId})
  }).then(function (r) { return r.json(); }).then(function (d) {
    if (!d.link_token) { fail(d.error || 'no link token'); return; }
    if (typeof Plaid === 'undefined') {
      fail('the Plaid Link script did not load'); return;
    }
    Plaid.create({
      token: d.link_token,
      onSuccess: function () {
        btn.textContent = 'Reconnecting…';
        fetch('/bankbridge/api/plaid/reconnect_complete', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({item_id: itemId})
        }).then(function () { window.location.reload(); })
          .catch(function () { window.location.reload(); });
      },
      onExit: function (err) {
        btn.disabled = false; btn.textContent = original;
        if (err) { alert('Reconnect cancelled: ' + (err.error_message || err.error_code || '')); }
      }
    }).open();
  }).catch(function (e) { fail(e); });
}
function bbDisconnect(itemId, name) {
  bbDiscItem = itemId;
  document.getElementById('bbDiscName').textContent = name || 'this bank';
  var err = document.getElementById('bbDiscError');
  err.style.display = 'none'; err.textContent = '';
  var go = document.getElementById('bbDiscGo');
  go.disabled = false; go.textContent = 'Disconnect';
  document.getElementById('bbDisconnectModal').style.display = 'flex';
}
function bbDisconnectClose() {
  document.getElementById('bbDisconnectModal').style.display = 'none';
  bbDiscItem = null;
}
document.getElementById('bbDiscGo').addEventListener('click', function () {
  if (!bbDiscItem) return;
  var go = this, err = document.getElementById('bbDiscError');
  go.disabled = true; go.textContent = 'Disconnecting…';
  err.style.display = 'none';
  fetch('/api/items/' + encodeURIComponent(bbDiscItem) + '/disconnect', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({reason: 'disconnected from the Accounts page'})
  }).then(function (r) { return r.json().catch(function () { return {}; }); })
    .then(function (d) {
      if (d && d.ok) { window.location = '/admin/accounts?flash=' + encodeURIComponent(d.message || 'Bank disconnected.'); return; }
      // Leave the modal open on failure — the Item is still connected, and the
      // operator needs to see why rather than have the dialog vanish.
      err.textContent = (d && d.error) || 'Could not disconnect this bank.';
      err.style.display = 'block';
      go.disabled = false; go.textContent = 'Disconnect';
    }).catch(function () {
      err.textContent = 'Could not reach Bank Bridge. Nothing was disconnected.';
      err.style.display = 'block';
      go.disabled = false; go.textContent = 'Disconnect';
    });
});
</script>
"""


@bp.get('/admin/accounts')
def accounts_page():
    items = PlaidItem.query.order_by(PlaidItem.created_at.desc()).all()
    groups = []
    supported_map = {}
    any_imported = False
    for it in items:
        # v0.4.44 · sandbox accounts are hidden unless the operator asked for
        # them. Filtered here rather than in the template so every count and
        # aggregate downstream sees the same set.
        accts = av.visible_accounts(
            PlaidAccount.query.filter_by(item_id=it.item_id)
            .order_by(PlaidAccount.name))
        for a in accts:
            supported_map[a.account_id] = erpnext_accounts.is_supported(a)
            if a.erpnext_bank_account_name:
                any_imported = True
        groups.append({
            'item': it, 'accounts': accts,
            # v0.4.11 · the operator-facing sentence for why this bank stopped.
            # '' for a healthy Item, so the banner renders nothing extra.
            'reauth_help': (reconnect.REAUTH_HELP.get(it.reauth_reason or '', '')
                            if it.needs_reauth else ''),
            # v0.5.1 · only show the investment-JE kill switch on a connection
            # that actually holds an investment account.
            'has_investment': any((a.type or '').lower() in
                                  ('investment', 'brokerage') for a in accts),
        })
    any_needs_reauth = any(g['item'].needs_reauth and not g['item'].disconnected
                           for g in groups)
    bank_accounts, erp_error = [], ''
    companies = []
    if erps.is_configured():
        try:
            bank_accounts = erpnext_bank.list_bank_accounts()
        except (ERPNextConfigError, ERPNextError) as e:
            erp_error = str(e)
        try:
            companies = erpnext_bank.list_companies()
        except (ERPNextConfigError, ERPNextError):
            companies = []
    bank_account_names = {ba['name'] for ba in bank_accounts}
    # v0.4.44 · pairing. `pair_options` is the set an account may be paired
    # WITH: other visible accounts under the same Company. `sandbox_ids` lets
    # the template tag a sandbox row when the toggle is on, so a test account
    # can never be mistaken for a real one.
    visible = [a for g in groups for a in g['accounts']]
    # v0.4.45 · one rule, defined once in statements.pair_candidates and shared
    # with the auto-linker. Scoped to the Plaid Item — a cash-services
    # companion always arrives on the same Link connection as the brokerage
    # account it serves — and excluding superseded rows, which are half an
    # account and a trap to pair to.
    item_companies = {it.item_id: (it.owning_company or '').strip()
                      for it in PlaidItem.query.all()}
    all_accounts = PlaidAccount.query.order_by(PlaidAccount.name).all()
    pair_options = {a.account_id: stmts_mod.pair_candidates(
        a, all_accounts, item_companies) for a in visible}
    pair_names = {a.account_id: (a.name or a.account_id) for a in visible}
    pair_masks = {a.account_id: (a.mask or '????') for a in all_accounts}
    # {cash_account_id: brokerage_account_id} — the inverse of pairing, so the
    # companion row can link to where it IS reconciled instead of nowhere.
    reconciles_under = {(a.paired_account_id or '').strip(): a.account_id
                        for a in all_accounts
                        if (a.paired_account_id or '').strip()}
    sandbox_ids = av.sandbox_account_ids()
    visibility = av.summary()
    # v0.4.43 · statement-anchored variance per account. Company-agnostic by
    # design — the accounts this matters most for are the ones whose ERPNext
    # instance does not exist yet (see models.StatementAnchor).
    from .. import statements as stmts
    anchor_map = {}
    try:
        for grp in groups:
            for a in grp['accounts']:
                summary = stmts.anchor_summary(a.account_id)
                if summary['periods']:
                    anchor_map[a.account_id] = summary
    except Exception:  # pragma: no cover - a summary must not 500 the page
        log.warning('anchor summary unavailable for /admin/accounts',
                    exc_info=True)
        anchor_map = {}
    return _page(ACCOUNTS_BODY, page='accounts', groups=groups,
                 anchor_map=anchor_map, pair_options=pair_options,
                 pair_names=pair_names, pair_masks=pair_masks,
                 sandbox_ids=sandbox_ids, visibility=visibility,
                 reconciles_under=reconciles_under,
                 bank_accounts=bank_accounts, bank_account_names=bank_account_names,
                 supported_map=supported_map, any_imported=any_imported,
                 unpostable=sync_engine.unpostable_by_account(),
                 opening_cell=_opening_balance_cell,
                 erpnext_ok=erps.is_configured(), companies=companies,
                 any_needs_reauth=any_needs_reauth,
                 loan_summaries=loans.all_summaries(),
                 bootstrap_unavailable=sorted(
                     erpnext_accounts.unavailable_doctypes()),
                 erp_error=erp_error, flash_msg=request.args.get('flash', ''))


# ── Unlinked-account cleanup (v0.4.15) ───────────────────────────────

CLEANUP_BODY = """
<h2>Unlinked ERPNext Bank Accounts</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}

<p style="font-size:14px;color:#444;max-width:760px">
  These Bank Accounts exist in ERPNext but no connected Plaid account points at
  them &mdash; usually left over from an earlier dry-run or from an import that
  targeted a different Company. They are harmless: nothing syncs into them.
  Delete them if you want a clean slate, or leave them alone.
</p>

{% if not erpnext_ok %}
<div class="banner-warn">ERPNext isn't configured yet, so there's nothing to
  list. Set it up on the <a href="/admin/erpnext_settings">ERPNext settings</a>
  page.</div>
{% elif erp_error %}
<div class="banner-warn">Couldn't read your Bank Accounts from ERPNext:
  {{ erp_error }}</div>
{% elif not groups %}
<div class="card"><b>Nothing to clean up.</b> Every Bank Account in ERPNext is
  claimed by a connected Plaid account.</div>
{% else %}
<form method="post" action="/admin/accounts/cleanup"
      onsubmit="return confirm('Delete the selected Bank Accounts from ERPNext? \
This cannot be undone. Records that still have linked documents will be kept.');">
{% for grp in groups %}
  <div class="card">
    <h3 style="margin-top:0">{{ grp.bank }}
      <span style="font-weight:normal;color:#666">
        &middot; {{ grp.count }} unlinked</span></h3>
    <table>
      <tr><th style="width:28px"></th><th>Account</th><th>Company</th>
          <th>Last 4</th><th>Subtype</th></tr>
      {% for a in grp.accounts %}
      <tr>
        <td><input type="checkbox" name="docname" value="{{ a.name }}"></td>
        <td><b>{{ a.account_name or a.name }}</b><br>
            <span style="font-size:12px;color:#666">{{ a.name }}</span></td>
        <td>{{ a.company or '—' }}</td>
        <td>{{ a.last_4 or '—' }}</td>
        <td>{{ a.account_subtype or a.account_type or '—' }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
{% endfor %}
  <p><button type="submit">Delete selected</button>
     <a href="/admin/accounts" style="margin-left:12px">Back to accounts</a></p>
</form>
{% endif %}

{% if results %}
<div class="card">
  <h3 style="margin-top:0">Result</h3>
  <table><tr><th>Record</th><th>Outcome</th></tr>
  {% for r in results %}
    <tr><td>{{ r.name }}</td><td>{{ r.message }}</td></tr>
  {% endfor %}
  </table>
</div>
{% endif %}
"""


def _cleanup_page(results=None, flash_msg=''):
    """Render the cleanup list. Shared by the GET and the POST-result view so
    the outcome table appears above a freshly-read list, not a stale one."""
    groups, erp_error = [], ''
    configured = erps.is_configured()
    if configured:
        try:
            groups = account_cleanup.group_by_bank(
                account_cleanup.unlinked_bank_accounts())
        except (ERPNextConfigError, ERPNextError) as e:
            erp_error = str(e)
    return _page(CLEANUP_BODY, page='accounts', groups=groups,
                 erpnext_ok=configured, erp_error=erp_error,
                 results=results or [], flash_msg=flash_msg)


@bp.get('/admin/accounts/cleanup')
def accounts_cleanup_page():
    return _cleanup_page(flash_msg=request.args.get('flash', ''))


@bp.post('/admin/accounts/cleanup')
def accounts_cleanup_delete():
    """Delete the ticked records, then re-render the list.

    Re-rendering rather than redirecting is deliberate: a record ERPNext refused
    to delete needs its reason shown next to it, and a redirect would have
    nowhere to carry that. Selecting nothing is a no-op, not an error."""
    names = request.form.getlist('docname')
    if not names:
        return _cleanup_page(flash_msg='Nothing was selected.')
    try:
        outcome = account_cleanup.delete_many(names)
    except (ERPNextConfigError, ERPNextError) as e:
        return _cleanup_page(flash_msg=f'Could not reach ERPNext: {e}')
    msg = f"{outcome['deleted']} deleted"
    if outcome['skipped']:
        msg += f", {outcome['skipped']} kept"
    return _cleanup_page(results=outcome['results'], flash_msg=msg)


FUZZY_MODAL_BODY = """
<h2>Possible duplicate account</h2>
<div class="card" style="max-width:640px">
  <p style="font-size:14px;color:#444;margin-top:0">
    You're about to create the GL Account
    <b>{{ intended or 'this Plaid account' }}</b>. An existing account with a
    similar name is already in ERPNext:
  </p>
  <div style="background:#f6f8fa;border:1px solid #e1e4e8;border-radius:6px;
              padding:12px 14px;margin:10px 0">
    <div style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:0.04em">Existing match</div>
    <div style="font-size:15px;font-weight:600;margin-top:4px">{{ candidate.account_name }}</div>
    <div style="font-size:12px;color:#777"><code>{{ candidate.name }}</code>
      · {{ candidate.score }}% similar</div>
  </div>
  <p style="font-size:13px;color:#555">
    <b>Reuse existing</b> avoids a near-duplicate in your Chart of Accounts —
    click this only if the existing account IS the same real account.
    <b>Create new anyway</b> creates a separate GL leaf — click this if the two
    are different accounts that happen to have similar names (e.g. two
    brokerages at the same bank with different last-4s).
  </p>
  <div style="display:flex;gap:10px;align-items:center;margin-top:14px">
    <form method="post" action="/admin/accounts/create" style="margin:0">
      <input type="hidden" name="account_id" value="{{ account_id }}">
      <input type="hidden" name="fuzzy_decision" value="reuse">
      <input type="hidden" name="fuzzy_candidate" value="{{ candidate.name }}">
      <button type="submit" class="primary" style="padding:6px 16px">Reuse existing</button>
    </form>
    <form method="post" action="/admin/accounts/create" style="margin:0">
      <input type="hidden" name="account_id" value="{{ account_id }}">
      <input type="hidden" name="fuzzy_decision" value="create_new">
      <input type="hidden" name="fuzzy_candidate" value="{{ candidate.name }}">
      <button type="submit" class="secondary" style="padding:6px 16px">Create new anyway</button>
    </form>
    <a href="/admin/accounts" style="font-size:13px;color:#888;margin-left:4px">Cancel</a>
  </div>
</div>
"""


@bp.post('/admin/accounts/create')
def create_account_in_erpnext():
    """One-click: create (or find) the ERPNext Bank + Bank Account for a single
    Plaid account and map it.

    v0.3.1: on the first click we probe for a fuzzy-matching existing GL Account
    and, if found, render a confirmation modal instead of creating — the operator
    chooses Reuse (default auto-dedup) or Create new anyway (skip fuzzy). The
    decision rides back on a hidden `fuzzy_decision` field."""
    account_id = (request.form.get('account_id') or '').strip()
    decision = (request.form.get('fuzzy_decision') or '').strip()
    candidate_name = (request.form.get('fuzzy_candidate') or '').strip()
    if not erps.is_configured():
        return redirect('/admin/accounts?flash=' + quote_plus(
            'ERPNext is not configured — set the connection first.'))
    # First click (no decision yet): probe. If a near-duplicate exists, show the
    # modal so the operator can decide; otherwise fall through and create.
    if not decision:
        try:
            candidate = erpnext_accounts.probe_fuzzy_gl_match(account_id)
        except Exception:  # never block the create on a best-effort probe
            candidate = None
        if candidate:
            # v0.4.24: `intended` is the NAME OF THE ACCOUNT WE'RE ABOUT TO
            # CREATE, not the existing candidate. Prior wording ('Before
            # creating a new GL Account for X' with X = candidate name) read
            # as if the incoming account matched the candidate exactly, which
            # made a mask-mismatch dupe (9401 vs 6030) look like it was for
            # the same account — the exact confusion that led to the wrong-
            # button-click hazard. Now the incoming name is in bold and the
            # candidate is shown separately as "the existing match".
            incoming_name = erpnext_accounts.intended_bank_account_name(
                account_id) or 'this Plaid account'
            return _page(FUZZY_MODAL_BODY, page='accounts', account_id=account_id,
                         candidate=candidate, intended=incoming_name)
    if decision == 'create_new' and candidate_name:
        # Record the operator's explicit rejection of the suggested reuse.
        audit.record('fuzzy_match_rejected_by_user', subject_type='Account',
                     subject_id=candidate_name,
                     notes=f'operator chose to create a new account instead of '
                           f'reusing {candidate_name}')
    try:
        result = erpnext_accounts.import_plaid_account_to_erpnext(
            account_id, fuzzy_decision=(decision or None))
    except (ERPNextConfigError, ERPNextError) as e:
        return redirect('/admin/accounts?flash=' + quote_plus(f'Create failed: {e}'))
    except Exception as e:  # surface any unexpected ERPNext error to the operator
        return redirect('/admin/accounts?flash=' + quote_plus(f'Create failed: {e}'))
    status = result.get('status')
    if status == 'imported':
        msg = f"Created ERPNext Bank Account {result['bank_account']}."
    elif status == 'skipped':
        msg = 'Account is already mapped.'
    elif status == 'unsupported':
        msg = 'That account type is not supported for ERPNext Bank Accounts.'
    else:
        msg = result.get('message', 'Done.')
    return redirect('/admin/accounts?flash=' + quote_plus(msg))


@bp.post('/admin/accounts/import_all')
def import_all_accounts():
    """One-click bulk: create ERPNext Bank Accounts for every unmapped supported
    account, then land on the dashboard with a success flash."""
    if not erps.is_configured():
        return redirect('/admin/accounts?flash=' + quote_plus(
            'ERPNext is not configured — set the connection first.'))
    try:
        stats = erpnext_accounts.import_all_supported_accounts()
    except (ERPNextConfigError, ERPNextError) as e:
        return redirect('/admin/accounts?flash=' + quote_plus(f'Import failed: {e}'))
    except Exception as e:
        return redirect('/admin/accounts?flash=' + quote_plus(f'Import failed: {e}'))
    if stats['created'] and not stats['failed']:
        # Success → dashboard with the spec's flash.
        return redirect('/admin?flash=' + quote_plus(
            f"Imported {stats['created']} accounts. Ready to sync."))
    # Nothing created, or partial failure → stay on Accounts with the detail.
    return redirect('/admin/accounts?flash=' + quote_plus(stats['summary']))


@bp.post('/admin/accounts/map')
def map_account():
    account_id = (request.form.get('account_id') or '').strip()
    erp_name = (request.form.get('erpnext_bank_account_name') or '').strip()
    sync_enabled = bool(request.form.get('sync_enabled'))
    acct = PlaidAccount.query.filter_by(account_id=account_id).first()
    if acct is None:
        return redirect('/admin/accounts?flash=' + quote_plus('Account not found'))
    acct.erpnext_bank_account_name = erp_name or None
    acct.sync_enabled = sync_enabled
    db.session.commit()
    msg = f'Mapped {acct.name or acct.mask} → {erp_name}' if erp_name else 'Unmapped account'
    return redirect('/admin/accounts?flash=' + quote_plus(msg))


@bp.post('/api/accounts/<plaid_account_id>/set_company')
def set_account_company(plaid_account_id):
    """Correction-only (v0.4.0 L1): override the owning ERPNext Company for one
    account. Normally an account inherits its Item's Company from Plaid Link; this
    is the escape hatch for fixing a mis-assignment. Blank clears the override so
    the account falls back to inheriting the Item's Company. Audited."""
    acct = PlaidAccount.query.filter_by(account_id=plaid_account_id).first()
    if acct is None:
        return redirect('/admin/accounts?flash=' + quote_plus('Account not found'))
    new_company = (request.form.get('owning_company') or '').strip() or None
    before = acct.owning_company
    acct.owning_company = new_company
    db.session.commit()
    audit.record('owning_company_override', subject_type='PlaidAccount',
                 subject_id=acct.account_id,
                 before={'owning_company': before},
                 after={'owning_company': new_company},
                 notes=f'correction: {before!r} → {new_company!r}')
    if new_company:
        msg = f'{acct.name or acct.mask} now owned by {new_company}'
    else:
        msg = f'{acct.name or acct.mask} reverted to inherit its bank’s Company'
    return redirect('/admin/accounts?flash=' + quote_plus(msg))


@bp.post('/api/accounts/<plaid_account_id>/book_opening_balance')
def book_opening_balance_route(plaid_account_id):
    """Book (or re-book) one account's opening balance by hand — v0.4.4.

    The auto-booking at import time covers the normal case; this is for accounts
    linked before v0.4.4, for a Plaid balance the operator wants to override with
    what a real statement says, and for backdating to a fiscal year start. Both
    `amount` and `posting_date` are optional: blank falls back to the account's
    cached Plaid balance and to OPENING_BALANCE_DATE respectively.

    Re-booking is deliberately allowed only over a `rejected` entry — a pending
    or approved opening balance is left alone, because replacing one silently
    would mean two equity entries for the same account."""
    acct = PlaidAccount.query.filter_by(account_id=plaid_account_id).first()
    if acct is None:
        return redirect('/admin/accounts?flash=' + quote_plus('Account not found'))
    raw_amount = (request.form.get('amount') or '').strip()
    amount = None
    if raw_amount:
        try:
            amount = float(raw_amount.replace(',', '').replace('$', ''))
        except ValueError:
            return redirect('/admin/accounts?flash=' + quote_plus(
                f'“{raw_amount}” is not a number — leave it blank to use the '
                f'current Plaid balance.'))
    raw_date = (request.form.get('posting_date') or '').strip()
    posting_date = None
    if raw_date:
        try:
            posting_date = date.fromisoformat(raw_date)
        except ValueError:
            return redirect('/admin/accounts?flash=' + quote_plus(
                f'“{raw_date}” is not a date (YYYY-MM-DD).'))
    erp = sync_engine.get_erp_client_or_none()
    if erp is None:
        return redirect('/admin/accounts?flash=' + quote_plus(
            'ERPNext is not configured — check the connection first.'))
    result = obal.book_opening_balance(erp, acct, amount=amount,
                                       posting_date=posting_date, force=True)
    label = acct.name or acct.mask or acct.account_id
    return redirect('/admin/accounts?flash=' + quote_plus(
        f'{label}: {result["message"]}'))


@bp.post('/admin/items/<item_id>/set_company')
def set_item_company(item_id):
    """Correction-only (v0.4.0.1): reassign the owning ERPNext Company for a whole
    linked Item. This retroactively changes the Company every account under the
    Item inherits (accounts with an explicit per-account override keep it). The
    normal path is choosing the Company at Plaid Link time; this is the fix-up for
    a wrong initial choice. Audited."""
    it = PlaidItem.query.filter_by(item_id=item_id).first()
    if it is None:
        return redirect('/admin/accounts?flash=' + quote_plus('Item not found'))
    new_company = (request.form.get('owning_company') or '').strip() or None
    before = it.owning_company
    it.owning_company = new_company
    db.session.commit()
    audit.record('item_owning_company_changed', subject_type='PlaidItem',
                 subject_id=it.item_id,
                 before={'owning_company': before},
                 after={'owning_company': new_company},
                 notes=f'retroactive Item reassignment: {before!r} → {new_company!r}')
    label = it.institution_name or it.item_id
    if new_company:
        msg = f'{label} accounts now owned by {new_company}'
    else:
        msg = f'{label} owning Company cleared (accounts use the ERPNext default)'
    return redirect('/admin/accounts?flash=' + quote_plus(msg))


@bp.post('/admin/items/<item_id>/invest_je_posting')
def set_invest_je_posting(item_id):
    """Turn investment-transaction Journal Entry posting ON or OFF for one
    linked Item (v0.5.1).

    The default is OFF and this is the ONLY way to turn it on — a deliberate
    opt-in, because these are real accounting entries that hit the P&L. Nothing
    is posted retroactively by flipping the switch; the next investment sync (or
    a manual post) is what emits JEs for the transactions already held."""
    it = PlaidItem.query.filter_by(item_id=item_id).first()
    if it is None:
        return redirect('/admin/accounts?flash=' + quote_plus('Item not found'))
    enabled = bool(request.form.get('enabled') == '1')
    before = it.invest_je_posting_enabled
    it.invest_je_posting_enabled = enabled
    db.session.commit()
    audit.record('invest_je_posting_toggled', subject_type='PlaidItem',
                 subject_id=it.item_id,
                 before={'invest_je_posting_enabled': before},
                 after={'invest_je_posting_enabled': enabled},
                 notes=f'investment JE posting {"enabled" if enabled else "disabled"}')
    label = it.institution_name or it.item_id
    msg = (f'{label}: investment JE posting is now '
           f'{"ON — trades will post as Journal Entries" if enabled else "OFF"}.')
    return redirect('/admin/accounts?flash=' + quote_plus(msg))


@bp.post('/api/items/<plaid_item_id>/disconnect')
def disconnect_item(plaid_item_id):
    """Disconnect a linked bank: call Plaid /item/remove, then mark the Item
    locally (v0.4.7).

    This is the code behind the promise in PRIVACY.md — "Disconnect a bank by
    removing the linked item" — and the capability Plaid's production review
    checks for.

    What it deliberately does NOT do is delete anything. The PlaidItem, its
    PlaidAccounts, every mirrored BankTransaction and every GeneratedJournalEntry
    stay exactly where they are, and the Journal Entries already pushed to
    ERPNext are untouched. Disconnecting stops the FUTURE feed; the books keep
    their history. Re-linking the bank later mints a brand-new Item (Plaid issues
    a new item_id + access_token) and leaves this row alone as the record of the
    old link.

    ORDER MATTERS: Plaid is called FIRST and the local flag is only written if
    it succeeded. The reverse order would leave an Item marked disconnected
    locally while Plaid happily kept the token alive — the app would stop
    syncing it, so nobody would notice the bank was still connected upstream,
    which is precisely the state the disconnect exists to prevent.

    Lives in admin_ui (not the public api blueprint) so it inherits this
    blueprint's admin-auth before_request: /item/remove is irreversible and
    billable, and must never be reachable unauthenticated.

    Returns JSON either way — the confirmation modal posts via fetch()."""
    it = PlaidItem.query.filter_by(item_id=plaid_item_id).first()
    if it is None:
        return jsonify({'ok': False, 'error': 'No such linked bank.'}), 404
    if it.disconnected:
        return jsonify({
            'ok': False,
            'error': f'{it.institution_name or plaid_item_id} is already '
                     'disconnected.'}), 409

    reason = (request.form.get('reason')
              or (request.get_json(silent=True) or {}).get('reason')
              or 'operator requested disconnect').strip()

    try:
        access_token = crypto.decrypt(it.access_token_encrypted)
    except Exception as e:  # noqa: BLE001 - unreadable token, surface it
        log.warning('disconnect: could not decrypt token for %s: %s',
                    plaid_item_id, e)
        return jsonify({'ok': False,
                        'error': 'Could not read the stored access token for '
                                 'this bank.'}), 500

    try:
        sync_engine.get_plaid_client().item_remove(access_token)
    except (PlaidError, PlaidConfigError) as e:
        log.warning('disconnect: Plaid refused /item/remove for %s: %s',
                    plaid_item_id, e)
        # Left CONNECTED on purpose — see the order note above.
        return jsonify({'ok': False,
                        'error': f'Plaid could not disconnect this bank: {e}'}), 502

    before = it.to_dict()
    it.disconnected = True
    # Timezone-aware, matching models._now() — every other timestamp column on
    # this table is aware, and mixing naive values into the same table makes
    # later comparisons raise.
    it.disconnected_at = datetime.now(timezone.utc)
    db.session.commit()
    audit.record('item_disconnected', subject_type='PlaidItem',
                 subject_id=it.item_id, before=before, after=it.to_dict(),
                 notes=(f'Disconnected {it.institution_name or it.item_id} via '
                        f'Plaid /item/remove — {reason}. Accounts, transactions '
                        'and generated Journal Entries retained.'))
    label = it.institution_name or it.item_id
    return jsonify({'ok': True, 'item_id': it.item_id,
                    'disconnected_at': it.disconnected_at.isoformat(),
                    'message': f'{label} disconnected. Plaid will stop sending '
                               'new transactions; your existing data is '
                               'unchanged.'})


# ── Transactions ─────────────────────────────────────────────────

TRANSACTIONS_BODY = """
<h2>Transactions</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
{% if nav_companies %}
<div style="position:sticky;top:0;z-index:10;background:#fafafa;padding:10px 0;
            margin:0 0 6px;border-bottom:2px solid #2e9e5b">
  <span style="font-size:18px;font-weight:600">
    {% if cur_company %}Viewing transactions for:
      <span style="color:#2e9e5b">{{ cur_company }}</span>
    {% else %}Viewing transactions across all Companies{% endif %}
  </span>
</div>
{% endif %}
<div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
{% if nav_companies %}
<form method="get" action="/admin/set_company" class="card" style="margin:0">
  <input type="hidden" name="next" value="/admin/transactions">
  <label style="margin:0">Company
    <select name="company" onchange="this.form.submit()">
      <option value="">(all)</option>
      {% for c in nav_companies %}
      <option value="{{ c }}" {{ 'selected' if c==cur_company else '' }}>{{ c }}</option>
      {% endfor %}
    </select>
  </label>
</form>
{% endif %}
<form method="get" action="/admin/transactions" class="card"
      style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin:0">
  <label style="margin:0">Status
    <select name="status">
      <option value="">(any)</option>
      <option value="posted" {{ 'selected' if cur_status=='posted' else '' }}>posted</option>
      <option value="pending" {{ 'selected' if cur_status=='pending' else '' }}>pending</option>
      <option value="error" {{ 'selected' if cur_status=='error' else '' }}>error</option>
      <option value="removed" {{ 'selected' if cur_status=='removed' else '' }}>removed</option>
    </select>
  </label>
  <label style="margin:0">Account
    <select name="account_id">
      <option value="">(all)</option>
      {% for a in accounts %}
      <option value="{{ a.account_id }}" {{ 'selected' if a.account_id==cur_account else '' }}>{{ a.name or a.mask }}</option>
      {% endfor %}
    </select>
  </label>
  <label style="margin:0">Rule state
    <select name="state" onchange="this.form.submit()"
            title="What the categorization rules did with this transaction — separate from the sync Status on its left, which is about the Plaid → ERPNext push.">
      {% for value, label in state_filters %}
      <option value="{{ value }}" {{ 'selected' if value==cur_state else '' }}>{{ label }}</option>
      {% endfor %}
    </select>
  </label>
  <label style="margin:0">Origin
    <select name="source" title="Where the row came from: the Plaid feed, or a line Bank Bridge synthesized from a bank statement (v0.5.5).">
      <option value="">(any)</option>
      <option value="plaid" {{ 'selected' if cur_source=='plaid' else '' }}>plaid feed</option>
      <option value="statement" {{ 'selected' if cur_source=='statement' else '' }}>📄 statement-derived</option>
    </select>
  </label>
  <label style="margin:0">Search
    <input name="q" value="{{ cur_q }}" placeholder="name / merchant">
  </label>
  <button type="submit" class="primary">Filter</button>
</form>
</div>
{% if cur_state %}
<p style="font-size:12px;color:#888;margin:6px 0 0">
  Rule-state filters only consider transactions the engine has actually seen —
  posted to ERPNext and not removed. {{ state_help }}
</p>
{% endif %}

{# v0.4.25 · show total-vs-shown and offer bigger limits. `rows|length` is
   what's actually on the page; `total_matching` is what's in the DB for the
   current filters. #}
<div style="margin:8px 0;font-size:13px;color:#555">
  Showing <b>{{ rows|length }}</b>{% if total_matching and total_matching > rows|length %}
  of <b>{{ total_matching }}</b>{% endif %} matching transactions
  {% if total_matching and total_matching > rows|length %}
  · <a href="?{% for k, v in request.args.items() %}{% if k != 'limit' %}{{ k }}={{ v }}&{% endif %}{% endfor %}limit=5000">show 5000</a>
  · <a href="?{% for k, v in request.args.items() %}{% if k != 'limit' %}{{ k }}={{ v }}&{% endif %}{% endfor %}limit=10000">show 10000 (max)</a>
  {% endif %}
</div>

<div style="margin:8px 0">
  <form method="post" action="/admin/transactions/rerun_rules" style="display:inline"
        onsubmit="return confirm('Run the categorization rules against posted transactions that have no Journal Entry yet? This uses your CURRENT rules and is logged.')">
    <button type="submit" class="secondary">Rerun rules on eligible transactions</button>
  </form>
  <span style="font-size:12px;color:#888;margin-left:8px">Explicit + logged. Editing a rule never re-runs it against past transactions — this button does, on demand.</span>
</div>

{% if cur_state == 'unmatched' %}
{# v0.4.6 · the guided first-sync view. One rule per MERCHANT clears a whole
   group, so the groups come first and the one-off rows come last. #}
<style>
  /* A <summary> laid out with display:flex loses its native disclosure
     triangle in every engine, so the group headers would read as plain text.
     This puts the affordance back explicitly. */
  details.tx-group > summary { list-style: none; }
  details.tx-group > summary::-webkit-details-marker { display: none; }
  details.tx-group > summary .caret::before { content: '\\25B8'; }
  details.tx-group[open] > summary .caret::before { content: '\\25BE'; }
  details.tx-group > summary .caret { color: #2e9e5b; margin-right: 6px; }
</style>
<h3 style="margin-bottom:2px">Unmatched by merchant
  <span style="font-size:13px;font-weight:400;color:#777">
    ({{ groups|length }} group{{ '' if groups|length == 1 else 's' }},
    {{ ungrouped|length }} one-off{{ '' if ungrouped|length == 1 else 's' }})</span>
</h3>
<p style="font-size:12px;color:#888;margin:0 0 8px">
  Each group is one rule's worth of work. Write the rule, then
  <b>Rerun rules</b> above — the whole group drops off this list.
</p>
{% for g in groups %}
<details class="card tx-group" style="margin:0 0 6px;padding:8px 12px">
  <summary style="cursor:pointer;display:flex;justify-content:space-between;
                  align-items:center;gap:10px;flex-wrap:wrap">
    <span>
      <span class="caret"></span><b>{{ g.count }}</b> unmatched from
      <b style="color:#2e9e5b">{{ g.label }}</b>
      {% if g.kind == 'description' %}
      <span class="pill pill-muted" title="Plaid gave these no merchant name — they are grouped by the shared start of their description.">by description</span>
      {% endif %}
      <span style="color:#888;font-size:12px">· {{ '%.2f'|format(g.total) }} total</span>
    </span>
    {# stopPropagation so following the link doesn't also toggle the <details>. #}
    <a href="{{ g.rule_url }}" class="secondary" onclick="event.stopPropagation()"
       style="text-decoration:none;padding:3px 10px;font-size:12px;white-space:nowrap">
      Create rule from this group</a>
  </summary>
  <table style="margin-top:8px">
    <tr><th>Date</th><th>Description</th><th class="num">Amount</th></tr>
    {% for r in g.rows %}
    <tr>
      <td>{{ r.date.isoformat() if r.date else '—' }}</td>
      <td style="max-width:340px">{{ r.name }}</td>
      <td class="num">{{ '%.2f'|format(r.amount) }} {{ r.iso_currency_code }}</td>
    </tr>
    {% endfor %}
  </table>
</details>
{% endfor %}
{% if not groups %}
<p style="font-size:13px;color:#888">
  {% if ungrouped %}No merchant repeats more than once here — these are all
  one-offs, listed below.{% else %}Nothing unmatched. Every posted transaction
  fired a rule.{% endif %}
</p>
{% endif %}
{% if ungrouped %}
<h3 style="margin-bottom:2px">One-offs
  <span style="font-size:13px;font-weight:400;color:#777">({{ ungrouped|length }})</span>
</h3>
<p style="font-size:12px;color:#888;margin:0 0 8px">
  Seen once each. A rule may not be worth it — but the <b>+ Rule</b> button in
  the table below pre-fills one if it is.
</p>
{% endif %}
{% endif %}

<table>
  <tr><th>Date</th><th>Account</th><th>Description</th><th class="num">Amount</th>
      <th>Status</th><th>ERPNext</th><th></th></tr>
  {% for r in rows %}
  <tr>
    <td>{{ r.date.isoformat() if r.date else '—' }}</td>
    <td><code>••{{ acct_mask.get(r.account_id, '??') }}</code></td>
    <td style="max-width:280px">{{ r.name }}{% if r.merchant_name %} <span style="color:#888">· {{ r.merchant_name }}</span>{% endif %}{% if r.pending %} <span class="pill pill-muted">pending</span>{% endif %}{% if r.source == 'statement' %} <span class="pill pill-muted" title="Synthesized from a bank-statement line Plaid never returned (v0.5.5). Reconciliation-only — the real feed supersedes it if it later arrives.">📄 statement</span>{% endif %}</td>
    <td class="num">{{ '%.2f'|format(r.amount) }} {{ r.iso_currency_code }}</td>
    <td>
      {% if r.removed %}<span class="pill pill-muted">removed</span>
      {% elif r.sync_error %}<span class="pill pill-err">error</span>
      {% elif r.posted_at %}<span class="pill pill-ok">posted</span>
      {% else %}<span class="pill pill-muted">pending</span>{% endif %}
    </td>
    <td style="font-size:12px">
      {% if r.erpnext_bank_transaction_id %}<code>{{ r.erpnext_bank_transaction_id }}</code>{% else %}—{% endif %}
      {% if r.sync_error %}<div style="color:#a04000">{{ r.sync_error[:100] }}</div>{% endif %}
    </td>
    <td style="white-space:nowrap">
      {% if not r.posted_at or r.sync_error %}
      <form method="post" action="/admin/transactions/retry" style="display:inline;margin:0">
        <input type="hidden" name="id" value="{{ r.id }}">
        <button type="submit" class="secondary" style="padding:3px 10px;font-size:12px">Retry</button>
      </form>
      {% endif %}
      {# v0.4.6 · only for rows no rule caught — for anything else the rule that
         DID fire is the thing to edit, and a second rule would just shadow it. #}
      {% if r.plaid_transaction_id in rule_urls %}
      <a href="{{ rule_urls[r.plaid_transaction_id] }}" class="secondary"
         title="Open the Rules editor pre-filled from this transaction"
         style="text-decoration:none;padding:3px 10px;font-size:12px">+ Rule</a>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
  {% if not rows %}<tr><td colspan="7" style="color:#888">No transactions match.</td></tr>{% endif %}
</table>
<p style="font-size:12px;color:#888">Showing up to {{ limit }} most recent.</p>
"""


# One-line explanation per rule-state filter, shown under the filter bar so the
# operator knows what the list in front of them is (and isn't).
_STATE_HELP = {
    'unmatched': 'No rule matched these, so no Journal Entry was generated — '
                 'this is the list to write rules from.',
    'matched': 'A rule fired and a live Journal Entry exists (pending review or '
               'approved).',
    'je_error': 'A rule matched, but the Journal Entry could not be created — '
                'errored, blocked cross-Company, or skipped for a missing '
                'account. See Generated Journal Entries for the reason.',
    'je_cancelled': 'A rule matched and the Journal Entry was rejected or '
                    'reversed.',
}


def _rule_prefill_url(prefill: dict, company: str = '') -> str:
    """A /admin/rules link that opens the editor pre-filled (v0.4.6). Only the
    fields we can infer are sent; everything else falls back to the editor's own
    defaults (Party Type Auto from v0.4.0.9, and the v0.4.0.4 Description
    Template auto-fill, which needs an Offset Account the operator has yet to
    pick)."""
    parts = ['prefill=1']
    for key in ('match_type', 'match_value', 'name'):
        val = (prefill.get(key) or '').strip()
        if val:
            parts.append(f'{key}=' + quote_plus(val))
    if (company or '').strip():
        parts.append('applies_to_company=' + quote_plus(company.strip()))
    return '/admin/rules?' + '&'.join(parts)


def _transaction_company(row, cache: dict) -> str:
    """The ERPNext Company owning `row`'s bank account, memoized across the page
    render — the prefill's Applies-to-Company. Local lookup only."""
    aid = getattr(row, 'account_id', None)
    if aid not in cache:
        cache[aid] = erpnext_accounts.owning_company_for_account_id(aid) or ''
    return cache[aid]


@bp.get('/admin/transactions')
def transactions_page():
    cur_status = (request.args.get('status') or '').strip()
    cur_account = (request.args.get('account_id') or '').strip()
    cur_q = (request.args.get('q') or '').strip()
    cur_company = _current_company()
    # v0.4.25 · was hardcoded to 300, which capped the visible history at
    # ~10 months on an active account and hid ~90% of the mirror on an
    # account with 2 years of Plaid history behind it. Now defaults to 2000
    # (comfortable on any reasonable browser) and accepts a `?limit=N` query
    # param up to 10000 for operators who want to eyeball everything. The
    # total-row count is passed through so the page can show 'showing X of Y'.
    try:
        limit = int(request.args.get('limit') or 2000)
    except ValueError:
        limit = 2000
    limit = max(50, min(limit, 10000))
    q = BankTransaction.query
    if cur_company:
        # Only transactions whose linked account resolves to the scoped Company.
        scoped_ids = [aid for aid, c in _resolve_account_companies().items()
                      if c == cur_company]
        q = q.filter(BankTransaction.account_id.in_(scoped_ids))
    if cur_account:
        q = q.filter(BankTransaction.account_id == cur_account)
    if cur_status == 'posted':
        q = q.filter(BankTransaction.posted_at.isnot(None),
                     BankTransaction.removed.is_(False))
    elif cur_status == 'pending':
        q = q.filter(BankTransaction.posted_at.is_(None),
                     BankTransaction.removed.is_(False),
                     BankTransaction.sync_error.is_(None))
    elif cur_status == 'error':
        q = q.filter(BankTransaction.sync_error.isnot(None))
    elif cur_status == 'removed':
        q = q.filter(BankTransaction.removed.is_(True))
    if cur_q:
        like = f'%{cur_q}%'
        q = q.filter(db.or_(BankTransaction.name.ilike(like),
                            BankTransaction.merchant_name.ilike(like)))
    # v0.5.5 · origin filter: 'statement' isolates the rows Bank Bridge
    # synthesized from a bank statement (a line Plaid never returned), 'plaid'
    # the feed itself. Unrecognized values degrade to "no filter".
    cur_source = (request.args.get('source') or '').strip()
    if cur_source == 'statement':
        q = q.filter(BankTransaction.source == 'statement')
    elif cur_source == 'plaid':
        q = q.filter(db.or_(BankTransaction.source.is_(None),
                            BankTransaction.source != 'statement'))
    else:
        cur_source = ''
    # v0.4.6 · the rule-state filter, orthogonal to `status` above (that one is
    # about the Plaid → ERPNext push; this one about what the rules engine did
    # afterwards). Unrecognized values degrade to "no filter".
    cur_state = (request.args.get('state') or '').strip()
    if not rule_stats.is_state_filter(cur_state):
        cur_state = ''
    q = rule_stats.apply_state_filter(q, cur_state)
    # v0.4.25 · count the FULL matched set before applying the display cap so
    # the page can say 'showing 2000 of 4728 matching transactions' and the
    # operator knows there are more.
    total_matching = q.count()
    rows = q.order_by(BankTransaction.date.desc().nullslast(),
                      BankTransaction.id.desc()).limit(limit).all()
    accounts = av.visible_accounts(
        PlaidAccount.query.order_by(PlaidAccount.name))
    acct_mask = {a.account_id: (a.mask or '??') for a in accounts}
    # "+ Rule" is offered per row only where a rule is the right answer: an
    # ELIGIBLE transaction (posted, not removed) that no rule caught. On any
    # other row the rule that already fired is the thing to edit, so adding a
    # second one would only shadow it.
    with_je = rule_stats.tx_ids_with_je_among(
        r.plaid_transaction_id for r in rows)
    company_cache: dict = {}
    unmatched_rows = [r for r in rows
                      if r.plaid_transaction_id not in with_je
                      and r.posted_at is not None and not r.removed]
    rule_urls = {
        r.plaid_transaction_id: _rule_prefill_url(
            rule_stats.prefill_for(r), _transaction_company(r, company_cache))
        for r in unmatched_rows}
    # The merchant grouping is only built for the unmatched view — it is the
    # first-sync workflow, and grouping a mixed list would be noise.
    groups, ungrouped = ([], [])
    if cur_state == 'unmatched':
        groups, ungrouped = rule_stats.group_unmatched(rows)
        for g in groups:
            # A group's Company comes from its rows; they can in principle span
            # Companies, so scope the rule only when they agree — otherwise leave
            # it company-agnostic and let the operator choose.
            companies = {_transaction_company(r, company_cache) for r in g['rows']}
            company = companies.pop() if len(companies) == 1 else ''
            g['rule_url'] = _rule_prefill_url(
                rule_stats.prefill_for_group(g), company)
    return _page(TRANSACTIONS_BODY, page='transactions', rows=rows,
                 accounts=accounts, acct_mask=acct_mask, limit=limit,
                 total_matching=total_matching,
                 cur_status=cur_status, cur_account=cur_account, cur_q=cur_q,
                 cur_company=cur_company, cur_state=cur_state,
                 cur_source=cur_source,
                 state_filters=rule_stats.STATE_FILTERS,
                 state_help=_STATE_HELP.get(cur_state, ''),
                 groups=groups, ungrouped=ungrouped, rule_urls=rule_urls,
                 flash_msg=request.args.get('flash', ''))


@bp.post('/admin/transactions/retry')
def retry_transaction():
    raw_id = (request.form.get('id') or '').strip()
    if not raw_id.isdigit():
        return redirect('/admin/transactions?flash=' + quote_plus('Bad row id'))
    ok, msg = sync_engine.retry_row(int(raw_id))
    prefix = 'Retried: ' if ok else 'Retry failed: '
    return redirect('/admin/transactions?flash=' + quote_plus(prefix + msg))


@bp.post('/admin/transactions/rerun_rules')
def rerun_rules():
    """Explicit, logged re-run of the CURRENT rules against posted, non-removed
    transactions that don't yet have a generated Journal Entry. Rule edits never
    retroactively re-run on their own — this is the deliberate opt-in path."""
    erp = sync_engine.get_erp_client_or_none()
    if erp is None:
        return redirect('/admin/transactions?flash=' + quote_plus(
            'ERPNext not configured — cannot generate Journal Entries.'))
    done = {row.plaid_transaction_id for row in
            db.session.query(GeneratedJournalEntry.plaid_transaction_id)
            .filter(GeneratedJournalEntry.erpnext_journal_entry_name.isnot(None))}
    eligible = (BankTransaction.query
                .filter(BankTransaction.posted_at.isnot(None),
                        BankTransaction.removed.is_(False)).all())
    generated = matched = considered = 0
    for row in eligible:
        if row.plaid_transaction_id in done:
            continue
        considered += 1
        gje = categorization.generate_journal_entry(erp, row)
        if gje is not None:
            matched += 1
            if gje.erpnext_journal_entry_name:
                generated += 1
    # v0.4.6 · a Rerun is the one moment match counts change in bulk, and the
    # operator's next stop is the Rules tab to see what stuck. Rolling up inline
    # (a local read + a write per changed rule) beats showing them a column that
    # is a day out of date at exactly the moment they're relying on it.
    rule_stats.rollup_match_counts()
    audit.record('rules_rerun', subject_type=None,
                 after={'considered': considered, 'matched': matched,
                        'generated': generated},
                 notes=(f'reran current rules on {considered} eligible '
                        f'transaction(s) → {generated} JE(s)'))
    return redirect('/admin/transactions?flash=' + quote_plus(
        f'Reran rules on {considered} transaction(s): {generated} '
        f'Journal Entr(ies) generated.'))


# ── Categorization rules ─────────────────────────────────────────

def _merchant_has_rule(name: str, merchant_rules: list) -> bool:
    """True when an active merchant rule already matches `name` — exact rules by
    equality, contains rules by substring (both case-insensitive)."""
    low = (name or '').strip().lower()
    if not low:
        return False
    for r in merchant_rules:
        mv = (r['match_value'] or '').strip().lower()
        if not mv:
            continue
        if r['match_type'] == 'merchant_exact' and low == mv:
            return True
        if r['match_type'] == 'merchant_contains' and mv in low:
            return True
    return False


@bp.get('/api/rules/known_merchants')
def known_merchants_api():
    """Autocomplete feed for the merchant match-value field: every merchant seen
    in the local transaction mirror, most-frequent first, tagged with whether a
    rule already covers it and a suggested rule Name from its category."""
    merchant_rules = CategorizationRule.merchants_with_rules()
    out = []
    for m in BankTransaction.known_merchants(limit=200):
        out.append({
            'name': m['name'], 'count': m['count'],
            'total_amount': m['total_amount'], 'category': m['category'],
            'has_rule': _merchant_has_rule(m['name'], merchant_rules),
            'suggested_name': categorization.suggest_rule_name(
                m['name'], m['category']),
        })
    return jsonify({'merchants': out})


@bp.get('/api/rules/known_categories')
def known_categories_api():
    """Autocomplete feed for the plaid_category_matches field: distinct Plaid
    categories seen locally (full hierarchy preserved), most-frequent first,
    each with a suggested short alias."""
    out = []
    for c in BankTransaction.known_categories(limit=200):
        out.append({'path': c['path'], 'count': c['count'],
                    'alias': categorization.category_alias(c['path'])})
    return jsonify({'categories': out})


# ── offset-account dropdown: Company-scoped feed (v0.4.0.2) ────────────────
#
# The Offset Account picker must only offer GL Accounts from the Company that
# owns the rule — otherwise a rule authored while scoped to Company X could pick
# Company Y's expense account and post a cross-Company Journal Entry. The
# effective Company is resolved per-request (see _offset_account_company) and the
# ERPNext result is cached per Company for the session lifetime, so paging around
# the admin UI doesn't re-hit ERPNext for the same list. `set_company` clears the
# cache so a scope change is reflected immediately.
_ACCOUNTS_CACHE_KEY = 'bankbridge_offset_account_cache'
# Cache-key marker for the explicit all-Companies feed (company='' means "no
# filter" to list_accounts, so we key it distinctly from the None default).
_ALL_COMPANIES_KEY = '\x00ALL'
# Cache-key marker for the Mode B (agnostic-rule) deduplicated LOGICAL account
# name feed — distinct from any real Company name and from _ALL_COMPANIES_KEY.
_LOGICAL_NAMES_KEY = '\x00LOGICAL'


def _accounts_cache() -> dict | None:
    """The per-app {company_key: [account dicts]} cache, or None without an app
    context. Process-local (single-operator bridge) and reset on restart."""
    try:
        return current_app.extensions.setdefault(_ACCOUNTS_CACHE_KEY, {})
    except RuntimeError:  # pragma: no cover - no app context (defensive)
        return None


def _invalidate_accounts_cache() -> None:
    """Drop every cached offset-account list — called when the session Company
    scope changes so the dropdown can't serve another Company's accounts."""
    cache = _accounts_cache()
    if cache is not None:
        cache.clear()


def _offset_account_company(rule_company: str | None) -> str | None:
    """The effective Company whose GL Accounts the Offset Account dropdown should
    offer, resolved in priority order (v0.4.0.2):

      1. the rule's own `applies_to_company` (`rule_company`) when set — a rule
         scoped to Company X must draw its offset from X's chart;
      2. else the active session scope (the navbar switcher) when set;
      3. else None — no scope anywhere, so offer EVERY Company's accounts (each
         carries its Company suffix in the docname) and force a conscious pick.

    Returns the Company name for cases 1-2, or None for case 3."""
    rc = (rule_company or '').strip()
    if rc:
        return rc
    scope = _current_company()
    if scope:
        return scope
    return None


def _erpnext_account_names(rule_company: str | None = None, *,
                           fresh: bool = False) -> list:
    """ERPNext GL Account docnames for the rule offset-account dropdown, scoped to
    the effective Company (rule scope → session scope → all). Cached per Company
    for the session lifetime. Best-effort — an empty list (ERPNext down /
    unconfigured) just means the field is free-text, which still works.

    When the effective Company is None (case 3: no scope at all), every Company's
    leaves are returned; the docname already ends in ` - <company_abbr>`, so the
    operator sees which Company each account belongs to and picks deliberately.

    v0.4.0.6 — `fresh=True` skips the cache read and re-hits ERPNext, then stores
    the result. The Rules editor asks for a fresh feed on every load so an account
    just created in ERPNext is selectable without a Company toggle or restart."""
    if not erps.is_configured():
        return []
    company = _offset_account_company(rule_company)
    cache = _accounts_cache()
    key = company if company else _ALL_COMPANIES_KEY
    if not fresh and cache is not None and key in cache:
        return cache[key]
    try:
        # company=None → list_accounts(company=None) → NO filter (all Companies);
        # a real name → that Company only.
        rows = erpnext_bank.list_accounts(company=company)
        names = [a['name'] for a in rows]
    except (ERPNextConfigError, ERPNextError):
        return []
    if cache is not None:
        cache[key] = names
    return names


def _logical_account_names(*, fresh: bool = False) -> list:
    """Deduplicated LOGICAL GL account names across every Company — the Mode B
    (Company-agnostic rule) offset feed (v0.4.0.3). Cached under a distinct key
    for the session; invalidated with the rest on a scope change. Best-effort — an
    empty list (ERPNext down / unconfigured) leaves the field free-text.

    `fresh=True` bypasses the cache read and refills it (v0.4.0.6)."""
    if not erps.is_configured():
        return []
    cache = _accounts_cache()
    if not fresh and cache is not None and _LOGICAL_NAMES_KEY in cache:
        return cache[_LOGICAL_NAMES_KEY]
    try:
        names = erpnext_bank.list_account_names(company=None)
    except (ERPNextConfigError, ERPNextError):
        return []
    if cache is not None:
        cache[_LOGICAL_NAMES_KEY] = names
    return names


@bp.get('/api/rules/known_accounts')
def known_accounts_api():
    """Autocomplete feed for the offset_account field. Its shape depends on the
    rule's inferred offset mode, driven by the `?company=` param (the rule's
    Applies-to-Company select value):

      * company set → Mode A (SCOPED rule): fully-qualified GL Account docnames
        (`<number> - <account_name> - <company_abbr>`) under THAT Company, so the
        offset can only be one of that Company's real accounts (v0.4.0.2).
      * company empty → Mode B (AGNOSTIC rule, v0.4.0.3): deduplicated LOGICAL
        account names across every Company (`Meals & Entertainment`), resolved to
        each transaction's own Company at JE time. `mode` tells the client which
        it got so it can label the field; `accounts` stays the load-bearing field.

    Best-effort — an empty list (ERPNext down / unconfigured) just leaves the
    field as free-text. Fed to the shared BankBridgeDropdown (v0.3.4).

    v0.4.0.6 — `?fresh=1` re-hits ERPNext instead of serving the per-Company
    cache (and refills it). The Rules editor sets it on page load, so an account
    created in ERPNext moments earlier is immediately selectable."""
    rule_company = (request.args.get('company') or '').strip()
    fresh = (request.args.get('fresh') or '').strip() in ('1', 'true', 'yes')
    if rule_company:
        return jsonify({'accounts': _erpnext_account_names(rule_company,
                                                           fresh=fresh),
                        'company': rule_company, 'mode': 'specific'})
    return jsonify({'accounts': _logical_account_names(fresh=fresh),
                    'company': '', 'mode': 'logical'})


@bp.get('/api/rules/refresh_accounts')
def refresh_accounts_api():
    """Manual "↻ refresh accounts" affordance next to the Offset Account field
    (v0.4.0.6). Drops EVERY cached offset feed — not just the requested Company's
    — then returns the freshly-fetched list in the same shape as
    /api/rules/known_accounts. Covers the case the fresh-on-load path can't: the
    operator creates the account in another tab while the editor is already open.

    A full invalidation is deliberate: the new account also belongs in the Mode B
    logical-name feed, and the operator may switch the rule's scope next."""
    _invalidate_accounts_cache()
    rule_company = (request.args.get('company') or '').strip()
    if rule_company:
        return jsonify({'accounts': _erpnext_account_names(rule_company,
                                                           fresh=True),
                        'company': rule_company, 'mode': 'specific'})
    return jsonify({'accounts': _logical_account_names(fresh=True),
                    'company': '', 'mode': 'logical'})


@bp.get('/api/rules/skip_party_suggestion')
def skip_party_suggestion_api():
    """Transfer-detection for the Rules editor (v0.4.0.7): does `?offset_account=`
    resolve to another Bank Account you own under `?company=`? When it does, the
    rule is booking a transfer (a credit-card payment, a deposit, an
    inter-account move) — there is no counterparty, so the editor pre-checks
    "Skip Party field". Advisory: the operator can always uncheck it, and only
    what they save is stored.

    Read-only and local-only (it reads the imported accounts' GL links), so it
    stays fast and works with ERPNext unreachable."""
    offset_account = (request.args.get('offset_account') or '').strip()
    company = (request.args.get('company') or '').strip()
    suggest = categorization.suggest_skip_party(offset_account, company)
    return jsonify({'skip_party': suggest, 'offset_account': offset_account,
                    'company': company})


def _sample_transaction_for(match_type: str, match_value: str):
    """A representative transaction to render a Description Template preview
    against: the most recent local transaction the (match_type, match_value)
    predicate actually catches, or a synthetic placeholder when none match (or
    there are no transactions yet). Returns (transaction, used_real_sample)."""
    from types import SimpleNamespace
    probe = CategorizationRule(match_type=(match_type or 'merchant_contains'),
                               match_value=(match_value or ''))
    rows = (BankTransaction.query
            .filter(BankTransaction.removed.is_(False))
            .order_by(BankTransaction.date.desc(), BankTransaction.id.desc())
            .limit(500).all())
    for r in rows:
        if categorization.rule_matches(
                probe, merchant_name=(r.merchant_name or ''),
                description=(r.name or ''), category=(r.category or ''),
                amount=(r.amount or 0.0)):
            return r, True
    placeholder = SimpleNamespace(
        merchant_name='Sample Merchant', name='SAMPLE MERCHANT PURCHASE',
        category='General Merchandise', amount=42.00,
        iso_currency_code='USD', date=date(2026, 1, 15))
    return placeholder, False


@bp.get('/api/rules/preview_description')
def preview_description_api():
    """Live preview for the Description Template field on the Rules editor.
    Renders `?template=` (or, when blank, the auto-fill default for the given
    `?match_type=` + `?offset_account=`) against a sample transaction — the most
    recent one matching `?match_value=`, else a placeholder. Read-only."""
    match_type = (request.args.get('match_type') or '').strip()
    match_value = (request.args.get('match_value') or '').strip()
    offset_account = (request.args.get('offset_account') or '').strip()
    template = request.args.get('template')
    if template is None or not template.strip():
        template = categorization.default_description_template(
            match_type, offset_account)
    row, used_sample = _sample_transaction_for(match_type, match_value)
    preview = categorization.render_description_template(template, row)
    return jsonify({'preview': preview, 'template': template,
                    'used_sample': used_sample})


RULES_BODY = """
<h2>Categorization rules</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
{% if nav_companies %}
<div style="position:sticky;top:0;z-index:10;background:#fafafa;padding:10px 0;
            margin:0 0 6px;border-bottom:2px solid #2e9e5b;
            display:flex;justify-content:space-between;align-items:center;
            flex-wrap:wrap;gap:10px">
  <span style="font-size:18px;font-weight:600">
    {% if cur_company %}Viewing rules for:
      <span style="color:#2e9e5b">{{ cur_company }}</span>
      <span style="font-size:12px;font-weight:400;color:#777">
        (+ company-agnostic rules, which apply everywhere)</span>
    {% else %}Viewing rules across all Companies{% endif %}
  </span>
  <form method="get" action="/admin/set_company" style="margin:0">
    <input type="hidden" name="next" value="/admin/rules">
    <label style="margin:0;font-size:13px">Company
      <select name="company" onchange="this.form.submit()" style="width:auto">
        <option value="">(all)</option>
        {% for c in nav_companies %}
        <option value="{{ c }}" {{ 'selected' if c==cur_company else '' }}>{{ c }}</option>
        {% endfor %}
      </select>
    </label>
  </form>
</div>
{% endif %}
{% if not je_engine_on %}
<div class="banner-warn">
  <h3>Journal-entry generation is OFF</h3>
  Rules can be authored + tested here, but they won't fire until
  <code>ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES=true</code> is set. This is
  deliberate — an incorrect auto-JE is worse than none. Auto-Supplier creation
  is independent and {{ 'ON' if supplier_on else 'OFF' }}.
</div>
{% endif %}

{% if scope_warning %}
{# v0.4.6 · caught-at-authoring. v0.4.0.2's push-time guard would block every JE
   this rule generates; saying so here, before the save persists, turns a silent
   failure discovered days later into one explicit decision now. #}
<div class="banner-warn" id="scope-warning">
  <h3>⚠ This rule's Offset Account belongs to another Company</h3>
  <p style="margin:6px 0">{{ scope_warning }}</p>
  <p style="margin:6px 0;font-size:13px">
    Journal Entries from this rule will be <b>blocked</b> at posting time by the
    cross-Company guard. Fix it by re-scoping <b>Applies to Company</b>, or by
    picking an Offset Account under the rule's own Company.
  </p>
  <a href="/admin/rules" class="secondary"
     style="text-decoration:none;padding:5px 12px">Cancel</a>
  <button type="submit" form="rule-form" class="secondary"
          style="padding:5px 12px;margin-left:6px">Save anyway</button>
</div>
{% endif %}

<h3>{{ 'Edit rule #' ~ form.id if form.id else 'Add a rule' }}</h3>
<form class="card" id="rule-form" method="post" action="/admin/rules/save">
  <input type="hidden" name="id" value="{{ form.id or '' }}">
  {# v0.4.0.9 · set only after a Mode B party_type warning, so the SAME form
     resubmitted keeps the operator's choice instead of warning forever. #}
  {% if confirm_party_mismatch %}<input type="hidden" name="confirm_party_mismatch" value="1">{% endif %}
  {# v0.4.6 · same one-shot confirm-token pattern for the scope-mismatch banner. #}
  {% if confirm_scope_mismatch %}<input type="hidden" name="confirm_scope_mismatch" value="1">{% endif %}
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <label style="flex:2;min-width:180px">Name
      <input name="name" id="rule-name" value="{{ form.name or '' }}" placeholder="Fuel — Chevron">
      <span id="name-hint" style="display:none;font-weight:400;font-size:12px;color:#0a7;margin-top:3px"></span>
    </label>
    <label style="flex:1;min-width:90px">Priority
      <input name="priority" type="number" value="{{ form.priority if form.priority is not none else 100 }}">
    </label>
    <label style="flex:1;min-width:120px;display:flex;align-items:center;gap:6px;margin-top:22px">
      <input type="checkbox" name="active" value="1" {{ 'checked' if form.active else '' }} style="width:auto"> active
    </label>
  </div>
  <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start">
    <label style="flex:1;min-width:180px">Match type
      <select name="match_type" id="mt">
        {% for mt in match_types %}
        <option value="{{ mt }}" {{ 'selected' if mt == form.match_type else '' }}>{{ mt }}</option>
        {% endfor %}
      </select>
    </label>
    <label style="flex:2;min-width:200px;position:relative">Match value
      <span id="mv-refresh-wrap" style="float:right;font-weight:400">
        <a href="#" id="ac-refresh" style="font-size:11px" title="Re-fetch merchants + categories from your local transactions">↻ refresh</a>
      </span>
      <input name="match_value" id="mv" autocomplete="off" value="{{ form.match_value or '' }}"
             placeholder="Chevron   ·   ^UBER   ·   [10, 500] for amount_range">
      <!-- custom merchant autocomplete (shown for merchant_* types) -->
      <div id="mv-dd" style="display:none;position:absolute;left:0;right:0;z-index:20;background:#fff;border:1px solid #ccc;border-top:none;border-radius:0 0 4px 4px;max-height:240px;overflow:auto;box-shadow:0 4px 12px rgba(0,0,0,.12)"></div>
      <!-- category picker (shown for plaid_category_matches) -->
      <select id="mv-cat" style="display:none;margin-top:6px;width:100%"></select>
      <!-- regex tester (shown for description_regex) -->
      <div id="mv-regex" style="display:none;margin-top:6px">
        <input id="regex-sample" autocomplete="off" placeholder="Paste a sample description to test…" style="width:100%">
        <span id="regex-result" style="font-size:12px"></span>
      </div>
      <span id="mv-hint" style="display:block;font-weight:400;font-size:11px;color:#888;margin-top:3px"></span>
    </label>
  </div>
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <label style="flex:2;min-width:220px;position:relative">Offset account<!--
      v0.4.6 · the resolved Company, inline in the LABEL rather than only in the
      helper line below. Server-rendered from the form's scope so it is right
      without JS, then live-updated by updateOffsetModeHint() when the operator
      changes Applies-to-Company. -->
      <span id="oa-company-label" style="font-weight:400">{% if form.applies_to_company %}(in <b style="color:#2e9e5b">{{ form.applies_to_company }}</b>){% else %}<span style="color:#b26a00">(logical name — resolves per-Company at JE time)</span>{% endif %}</span>
      <span style="font-weight:400;color:#888">— the categorized (non-bank) side</span>
      <!-- v0.4.0.6 · refetch the chart from ERPNext for an account created in
           another tab while this editor is open -->
      <a href="#" id="oa-refresh" style="font-weight:400;font-size:11px;margin-left:8px"
         title="Refetch this Company's Chart of Accounts from ERPNext. The list already refreshes every time you open this page.">↻ refresh accounts</a>
      <span id="oa-mode-hint" style="display:block;font-weight:400;font-size:11px;color:#0a7;margin:2px 0 0"></span>
      <input name="offset_account" id="offset-account" autocomplete="off" value="{{ form.offset_account or '' }}"
             title="The account for the categorized side. The bank side is automatically determined from the transaction's linked Plaid account. Direction defaults to auto (withdrawal → offset is debit; deposit → offset is credit)."
             placeholder="Fuel Expense - EC">
      <!-- custom account autocomplete (v0.3.4 · replaces the native <datalist>
           Safari collapsed mid-type); options fed by /api/rules/known_accounts -->
      <div id="oa-dd" style="display:none;position:absolute;left:0;right:0;z-index:20;background:#fff;border:1px solid #ccc;border-top:none;border-radius:0 0 4px 4px;max-height:240px;overflow:auto;box-shadow:0 4px 12px rgba(0,0,0,.12)"></div>
    </label>
    <label style="flex:1;min-width:160px">Direction
      <select name="offset_direction"
              title="The account for the categorized side. The bank side is automatically determined from the transaction's linked Plaid account. Direction defaults to auto (withdrawal → offset is debit; deposit → offset is credit).">
        {% for d in offset_directions %}
        <option value="{{ d }}" {{ 'selected' if d == (form.offset_direction or 'auto') else '' }}>{{ d }}</option>
        {% endfor %}
      </select>
    </label>
  </div>
  <p style="font-size:12px;color:#888;margin:-4px 0 6px">
    The bank side is set automatically from the transaction's linked Plaid
    account. <b>auto</b>: withdrawal → offset is debited; deposit/refund → offset
    is credited. Use <b>always_debit</b> / <b>always_credit</b> only for reversals.
  </p>
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <label style="flex:1;min-width:150px">Party type
      <select name="party_type" id="party-type"
              title="Which side of the ledger the counterparty sits on. Auto decides per transaction from the offset account: a Receivable account books a Customer, a Payable one books a Supplier, anything else books no party.">
        {# A NEW rule (no party_type key at all) defaults to Auto; an EXISTING
           rule always shows what it stored, so a saved "— none —" stays none. #}
        <option value="Auto" {{ 'selected' if (form.party_type or '')|lower == 'auto' or 'party_type' not in form else '' }}>Auto (from offset account)</option>
        <option value="" {{ 'selected' if 'party_type' in form and not form.party_type else '' }}>— none —</option>
        <option value="Supplier" {{ 'selected' if form.party_type == 'Supplier' else '' }}>Supplier</option>
        <option value="Customer" {{ 'selected' if form.party_type == 'Customer' else '' }}>Customer</option>
      </select>
    </label>
    <label style="flex:2;min-width:200px">Party name <span style="font-weight:400;color:#888">(blank → auto-created for the merchant, payroll processor, or bank)</span>
      <input name="party_name" value="{{ form.party_name or '' }}" placeholder="(optional)">
    </label>
  </div>
  <!-- v0.4.0.8 · sell-side support. The live hint is swapped by JS as the
       Party type / Offset account change (see the party-type-hint script). -->
  <p id="party-type-hint" style="font-size:12px;color:#888;margin:-4px 0 6px">
    <b>Auto</b> reads the offset account each time the rule fires — a
    <b>Receivable</b> account books a <b>Customer</b>, a <b>Payable</b> account
    books a <b>Supplier</b>, and everything else books <b>no party</b>. That
    includes ordinary Income and Expense accounts: ERPNext only allows a Party
    on a Receivable or Payable account, and refuses to submit a Journal Entry
    that breaks the rule. Pick <b>Supplier</b> or <b>Customer</b> to force one
    side where the account allows it. Banks and brokerages get BOTH records
    created, since they bill you and pay you.
  </p>
  <!-- v0.4.0.7 · pre-checked by JS when the offset resolves to another Bank
       Account of the same Company (/api/rules/skip_party_suggestion). -->
  <div style="margin:2px 0 8px">
    <label style="font-weight:400">
      <input type="checkbox" name="skip_party" id="skip-party" value="1"
             style="width:auto" {{ 'checked' if form.skip_party else '' }}>
      Skip Party field (for transfers between accounts you own)
    </label>
    <span id="skip-party-hint" style="display:block;font-size:11px;color:#888;margin:2px 0 0 22px">
      Recommended when the offset account is another Bank Account of the same Company.
    </span>
  </div>
  <!-- v0.4.1 · intercompany. Defaults CHECKED for a new rule (form.get with a
       True fallback), because a transfer between two Companies you own is not
       revenue or expense and a generic rule firing on one leg would book P&L
       activity that never happened. -->
  <div style="margin:2px 0 8px">
    <label style="font-weight:400">
      <input type="checkbox" name="ignore_for_paired" id="ignore-for-paired" value="1"
             style="width:auto" {{ 'checked' if form.get('ignore_for_paired', True) else '' }}>
      Ignore for intercompany-paired transactions
    </label>
    <span style="display:block;font-size:11px;color:#888;margin:2px 0 0 22px">
      Recommended. Transfers between two Companies you own are booked through
      <b>Due from</b> / <b>Due to</b> accounts on the
      <a href="/admin/intercompany">Intercompany</a> page instead — leave this on
      so this rule doesn’t also book one leg to profit &amp; loss. Clear it only
      if this rule really should still fire on a paired transaction.
    </span>
  </div>
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <label style="flex:1;min-width:220px">Applies to Company
      <span style="font-weight:400;color:#888">— multi-entity scope</span>
      <select name="applies_to_company"
              title="Restrict this rule to transactions whose bank account belongs to one Company. Leave on 'all Companies' to apply everywhere.">
        <option value="">— all Companies (company-agnostic) —</option>
        {% for c in companies %}
        <option value="{{ c }}" {{ 'selected' if c == form.applies_to_company else '' }}>{{ c }}</option>
        {% endfor %}
        {% if form.applies_to_company and form.applies_to_company not in companies %}
        <option value="{{ form.applies_to_company }}" selected>{{ form.applies_to_company }} (current)</option>
        {% endif %}
      </select>
    </label>
  </div>
  <label>Description template
    <span style="float:right;font-weight:400">
      <a href="#" id="dt-reset" style="font-size:11px" title="Restore the auto-generated default for this match type + offset account">↺ reset to default</a>
    </span>
    <span style="font-weight:400;color:#888">— vars:
      {{ '{{merchant_name}}' }}, {{ '{{description}}' }}, {{ '{{amount}}' }},
      {{ '{{plaid_category}}' }}, {{ '{{date}}' }} · auto-fills from match type +
      offset account; edit freely</span>
    <textarea name="description_template" id="description-template" rows="2"
              style="width:100%;font-family:inherit;resize:vertical"
              placeholder="{{ '{{merchant_name}} - {{offset_short}}' }}">{{ form.description_template or '' }}</textarea>
    <span id="dt-preview" style="display:block;font-size:12px;color:#555;margin-top:3px"></span>
  </label>
  <label>Internal tag (optional, Bank-Bridge only)
    <input name="bb_internal_tag" id="bb-internal-tag" autocomplete="off"
           value="{{ form.bb_internal_tag or '' }}"
           style="width:100%"
           placeholder="e.g. owner_distribution, member_distribution, family_support, advisory_fee, internal_sweep">
    <span style="display:block;font-size:11px;color:#888;margin-top:3px">
      Never sent to ERPNext. Used only for reconciliation attribution and the
      audit trail — it tags matching transactions so the reconciliation view
      can explain a period's variance.
    </span>
  </label>
  <button type="submit" class="primary">{{ 'Save changes' if form.id else 'Add rule' }}</button>
  {% if form.id %}<a href="/admin/rules" class="secondary" style="text-decoration:none;display:inline-block;margin-left:8px">Cancel edit</a>{% endif %}
</form>

<h3>Test a rule</h3>
<form class="card" method="post" action="/admin/rules/test"
      style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
  <label style="margin:0;flex:1;min-width:150px">Merchant
    <input name="merchant_name" value="{{ test.merchant_name or '' }}" placeholder="Chevron">
  </label>
  <label style="margin:0;flex:1;min-width:150px">Description
    <input name="description" value="{{ test.description or '' }}" placeholder="CHEVRON 0123456">
  </label>
  <label style="margin:0;min-width:110px">Amount
    <input name="amount" value="{{ test.amount or '' }}" placeholder="42.50">
  </label>
  <label style="margin:0;flex:1;min-width:150px">Plaid category
    <input name="category" value="{{ test.category or '' }}" placeholder="GAS_STATIONS">
  </label>
  <button type="submit" class="primary">Test</button>
</form>
{% if test_result %}
<div class="{{ 'banner-ok' if test_result.matched else 'banner-warn' }}">
  {% if test_result.matched %}
    ✓ Matched rule <b>#{{ test_result.rule.id }} · {{ test_result.rule.name }}</b>
    (priority {{ test_result.rule.priority }}).
    <pre style="white-space:pre-wrap;background:#f7f7f7;border:1px solid #ddd;border-radius:4px;padding:10px;font-size:12px;margin-top:8px">{{ test_result.je_preview }}</pre>
  {% else %}
    <h3>No rule matched</h3>This transaction would be left for manual reconciliation.
  {% endif %}
</div>
{% endif %}

<h3 style="display:flex;justify-content:space-between;align-items:center">
  <span>Rules ({{ rules|length }} live)</span>
  <span style="font-size:13px;font-weight:400">
    <form method="post" action="/admin/rules/rollup_match_counts" style="display:inline;margin:0">
      <button type="submit" class="secondary" style="padding:3px 10px;font-size:12px"
              title="Recount matches now instead of waiting for the daily rollup.">↻ refresh match counts</button>
    </form>
    <form method="post" action="/admin/rules/backfill_tags" style="display:inline;margin:0 0 0 6px">
      <button type="submit" class="secondary" style="padding:3px 10px;font-size:12px"
              title="Re-run every active rule against all historical transactions and update their internal tags. Only the tag column is touched — no Journal Entries are created or changed.">⌕ backfill internal tags</button>
    </form>
    {% if show_archived %}<a href="/admin/rules" style="margin-left:8px">hide history</a>
    {% else %}<a href="/admin/rules?archived=1" style="margin-left:8px">show archived / history</a>{% endif %}
  </span>
</h3>
<p style="font-size:12px;color:#888;margin:0 0 6px">
  <b>Matches</b> counts the transactions each rule has actually fired on, from
  the cached daily rollup{% if match_count_rolled_at %} (last run {{ match_count_rolled_at }}){% endif %}.
  A <b>0</b> means the rule is dead or scoped too narrowly to reach anything —
  the usual causes are a Company scope that no bank account belongs to, or a
  higher-priority rule shadowing it.
</p>
<table>
  <tr><th class="num">#</th><th class="num">Prio</th><th>Name</th><th>Match</th><th>Offset account</th><th>Dir</th>
      <th>Company</th><th>Party</th>
      <th class="num"><a href="{{ matches_sort_url }}" style="text-decoration:none"
         title="Historical = lifetime JEs this rule generated (the audit number, sorted on). Active = JEs since it was last switched ON (0 while OFF).">Matches{{ matches_sort_arrow }}<br><span style="font-size:10px;font-weight:400;color:#888">hist | active</span></a></th>
      <th>Active</th><th></th></tr>
  {% for r in rules %}
  <tr>
    <td class="num">{{ r.id }}</td>
    <td class="num">{{ r.priority }}</td>
    <td>{{ r.name }}</td>
    <td style="font-size:12px"><code>{{ r.match_type }}</code><br>{{ (r.match_value or '')[:50] }}</td>
    <td style="font-size:12px">{{ r.offset_account or r.debit_account }}{% if r.offset_account and not r.applies_to_company %}<span title="Logical name — resolves to each Company's own account at posting time" style="color:#b26a00;margin-left:4px">· logical</span>{% endif %}</td>
    <td style="font-size:12px">{{ r.offset_direction or 'auto' }}</td>
    <td style="font-size:12px">{% if r.applies_to_company %}{{ r.applies_to_company }}{% else %}<span style="color:#999">all</span>{% endif %}</td>
    <td style="font-size:12px">{% if r.skip_party %}<span class="pill pill-muted" title="This rule books a transfer between accounts you own — the generated Journal Entry carries no Party.">no party</span>{% else %}{{ (r.party_type or '') }}{% if r.party_name %}: {{ r.party_name }}{% endif %}{% endif %}</td>
    {# v0.5.9 · two counts. Historical (bold, the audit lifetime number) stays
       even when the rule is OFF; Active (green) is matches since it was last
       switched ON, and reads "— (off)" muted while the rule is OFF. #}
    <td class="num" style="font-size:12px;white-space:nowrap">
      <span title="Lifetime JEs this rule has generated — the audit number, unaffected by toggling.">
        {% if r.match_count %}<b>{{ r.match_count }}</b>{% else %}<b style="color:#999" title="This rule has never matched a transaction. Either nothing it targets has synced yet, or its match value / Company scope is too narrow — try it against a real description in “Test a rule” above.">0</b>{% endif %}</span>
      <br>
      {% if r.active %}
      <span style="color:#2e9e5b" title="JEs generated since this rule was last switched ON.">{{ active_counts.get(r.id, 0) }} active</span>
      {% else %}
      <span style="color:#999" title="Rule is OFF — it fires on nothing, so its active count is zero.">— (off)</span>
      {% endif %}
    </td>
    <td>
      <form method="post" action="/admin/rules/toggle" style="margin:0">
        <input type="hidden" name="id" value="{{ r.id }}">
        <button type="submit" class="secondary" style="padding:3px 10px;font-size:12px">
          {% if r.active %}<span class="pill pill-ok">on</span>{% else %}<span class="pill pill-muted">off</span>{% endif %}
        </button>
      </form>
    </td>
    <td style="white-space:nowrap">
      <a href="/admin/rules?edit={{ r.id }}" class="secondary" style="text-decoration:none;padding:3px 10px;font-size:12px">Edit</a>
      <form method="post" action="/admin/rules/delete" style="display:inline;margin:0"
            onsubmit="return confirm('Archive rule {{ r.name }}? (kept for history)')">
        <input type="hidden" name="id" value="{{ r.id }}">
        <button type="submit" class="secondary" style="padding:3px 10px;font-size:12px">Archive</button>
      </form>
    </td>
  </tr>
  {% endfor %}
  {% if not rules %}<tr><td colspan="11" style="color:#888">No live rules — add one above.</td></tr>{% endif %}
</table>
<p style="font-size:12px;color:#888">Edits never overwrite: editing a rule archives the old version and creates a new one, so past auto-JE decisions stay reconstructable. See the <a href="/admin/audit?subject_type=CategorizationRule">audit trail</a>.</p>

{% if show_archived %}
<h3>Archived / historical rules ({{ archived|length }})</h3>
<table>
  <tr><th class="num">#</th><th>Name</th><th>Match</th><th>Superseded by</th><th>Updated</th><th></th></tr>
  {% for r in archived %}
  <tr style="color:#777">
    <td class="num">{{ r.id }}</td>
    <td>{{ r.name }}</td>
    <td style="font-size:12px"><code>{{ r.match_type }}</code> {{ (r.match_value or '')[:40] }}</td>
    <td>{% if r.superseded_by %}#{{ r.superseded_by }}{% else %}<span class="pill pill-muted">deleted</span>{% endif %}</td>
    <td style="font-size:12px">{{ r.updated_at.strftime('%Y-%m-%d %H:%M') if r.updated_at else '' }}</td>
    <td><a href="/admin/audit?subject_type=CategorizationRule&subject_id={{ r.id }}" style="font-size:12px">lifecycle</a></td>
  </tr>
  {% endfor %}
  {% if not archived %}<tr><td colspan="6" style="color:#888">No archived rules.</td></tr>{% endif %}
</table>
{% endif %}
<script src="/static/rule_dropdown.js?v=0.3.4"></script>
{% raw %}
<script>
// v0.3.4 · rule-builder autocomplete. Context-aware match-value widget +
// name suggestion, fed by /api/rules/known_merchants|known_categories. Vanilla
// JS, no dependencies; data cached in sessionStorage for the session.
(function () {
  var mt = document.getElementById('mt');
  if (!mt) return;                       // not on the rules page
  var mv = document.getElementById('mv');
  var dd = document.getElementById('mv-dd');
  var cat = document.getElementById('mv-cat');
  var regexBox = document.getElementById('mv-regex');
  var regexSample = document.getElementById('regex-sample');
  var regexResult = document.getElementById('regex-result');
  var hint = document.getElementById('mv-hint');
  var refresh = document.getElementById('ac-refresh');
  var nameEl = document.getElementById('rule-name');
  var nameHint = document.getElementById('name-hint');
  var oa = document.getElementById('offset-account');
  var oaDD = document.getElementById('oa-dd');
  var oaModeHint = document.getElementById('oa-mode-hint');
  var oaRefresh = document.getElementById('oa-refresh');
  var companySel = document.querySelector('select[name=applies_to_company]');
  var dt = document.getElementById('description-template');
  var dtReset = document.getElementById('dt-reset');
  var dtPreview = document.getElementById('dt-preview');

  // ── Description Template auto-fill + live preview (v0.4.0.4) ──────────
  // The template is a tiny {{variable}} string; the editor auto-fills a sensible
  // default per match type when the operator picks an Offset Account, and never
  // clobbers text the operator typed. `dtAuto` tracks whether the current value
  // is one WE generated (safe to regenerate) vs. hand-edited (leave alone).
  var DEFAULT_TEMPLATES = {
    merchant_exact: '{{merchant_name}} - {{offset_short}}',
    merchant_contains: '{{merchant_name}} - {{offset_short}}',
    description_regex: '{{offset_short}} - {{amount}}',
    plaid_category_matches: '{{plaid_category}} - {{offset_short}} - {{merchant_name}}',
    amount_range: '{{offset_short}} - {{merchant_name}} - {{amount}}'
  };
  // A value that arrived pre-filled (editing an existing rule) is the operator's,
  // not ours — start dtAuto false unless the field is empty.
  var dtAuto = dt ? !dt.value.trim() : false;

  // Mirror categorization.logical_account_name: strip a trailing ' - ABBR'
  // Company suffix (uppercase/digits) then a leading '<number> - ' account number.
  function logicalName(name) {
    var s = (name || '').trim();
    if (!s) return '';
    var t = s.replace(/\\s+-\\s+[A-Z0-9]{1,10}$/, '').replace(/^\\d+\\s+-\\s+/, '').trim();
    return t || s;
  }

  function defaultTemplate() {
    var pat = DEFAULT_TEMPLATES[mt.value] || '{{merchant_name}} - {{offset_short}}';
    return pat.split('{{offset_short}}').join(logicalName(oa ? oa.value : ''));
  }

  // Regenerate the default only when the field is empty or still holds a value we
  // generated — an explicit user edit (dtAuto=false) is never overwritten.
  function maybeAutofill() {
    if (!dt) return;
    if (dtAuto || !dt.value.trim()) {
      dt.value = defaultTemplate();
      dtAuto = true;
    }
    refreshPreview();
  }

  var previewTimer = null;
  function refreshPreview() {
    if (!dtPreview) return;
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(doFetchPreview, 250);
  }
  function doFetchPreview() {
    var qs = 'template=' + encodeURIComponent(dt ? dt.value : '') +
             '&match_type=' + encodeURIComponent(mt.value) +
             '&match_value=' + encodeURIComponent(mv ? mv.value : '') +
             '&offset_account=' + encodeURIComponent(oa ? oa.value : '');
    fetch('/api/rules/preview_description?' + qs)
      .then(function (r) { return r.json(); })
      .then(function (res) {
        var txt = (res && res.preview) || '';
        var src = (res && res.used_sample)
          ? ' · from your most recent matching transaction'
          : ' · sample data (no matching transaction yet)';
        dtPreview.innerHTML = txt
          ? 'Preview: <b>' + esc(txt) + '</b><span style="color:#999">' +
            esc(src) + '</span>'
          : '<span style="color:#999">Preview: (empty description)</span>';
      }).catch(function () { dtPreview.textContent = ''; });
  }

  // v0.4.0.7 · transfer detection. When the chosen offset is another Bank
  // Account of the same Company the rule books a transfer, which has no
  // counterparty — pre-check "Skip Party field". Suggestion only: once the
  // operator touches the box themselves we never move it again.
  var skipParty = document.getElementById('skip-party');
  var skipPartyHint = document.getElementById('skip-party-hint');
  // Editing an existing rule counts as already-decided: its stored skip_party
  // is the operator's own choice, so the heuristic stays out of the way.
  var ruleIdField = document.querySelector('#rule-form input[name="id"]');
  var skipPartyTouched = !!(ruleIdField && ruleIdField.value);
  if (skipParty) {
    skipParty.addEventListener('change', function () { skipPartyTouched = true; });
  }
  // Debounced like the description preview — onOffsetChanged runs on every
  // keystroke, and this suggestion isn't worth a request per character.
  var skipPartyTimer = null;
  function refreshSkipPartySuggestion() {
    if (!skipParty || skipPartyTouched) return;
    if (skipPartyTimer) clearTimeout(skipPartyTimer);
    skipPartyTimer = setTimeout(doFetchSkipPartySuggestion, 250);
  }
  function doFetchSkipPartySuggestion() {
    if (!skipParty || skipPartyTouched) return;
    var offset = oa ? oa.value : '';
    if (!offset) return;
    fetch('/api/rules/skip_party_suggestion?offset_account=' +
          encodeURIComponent(offset) + '&company=' +
          encodeURIComponent(accountCompany()))
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (skipPartyTouched || !res || !res.skip_party) return;
        skipParty.checked = true;
        if (skipPartyHint) {
          skipPartyHint.style.color = '#0a7';
          skipPartyHint.textContent =
            'Auto-checked: “' + offset + '” is another Bank Account you own, ' +
            'so this rule looks like a transfer. Uncheck to book a Party anyway.';
        }
      }).catch(function () { /* advisory only — leave the box as-is */ });
  }

  function onOffsetChanged() { maybeAutofill(); refreshSkipPartySuggestion(); }

  // v0.4.0.3 · the offset field is two-mode, keyed off Applies-to-Company:
  //   * a Company is selected → Mode A: pick one of THAT Company's real accounts;
  //   * '— all Companies —'   → Mode B: pick a LOGICAL name resolved per-Company
  //     at JE time. The helper line makes the active mode explicit.
  // v0.4.6 · keep the inline Company label in the Offset Account <label> in step
  // with the scope select. Same Mode A / Mode B split as the helper line below.
  var oaCompanyLabel = document.getElementById('oa-company-label');
  function updateOffsetCompanyLabel() {
    if (!oaCompanyLabel) return;
    var co = accountCompany();
    oaCompanyLabel.innerHTML = co
      ? '(in <b style="color:#2e9e5b">' + esc(co) + '</b>)'
      : '<span style="color:#b26a00">(logical name — resolves per-Company ' +
        'at JE time)</span>';
  }

  function updateOffsetModeHint() {
    updateOffsetCompanyLabel();
    if (!oaModeHint) return;
    var co = accountCompany();
    if (co) {
      oaModeHint.style.color = '#0a7';
      oaModeHint.textContent = 'Scoped: pick a real account in ' + co + '.';
    } else {
      oaModeHint.style.color = '#b26a00';
      oaModeHint.textContent =
        'Logical name — resolves to each Company’s own account at ' +
        'posting time (e.g. “Meals & Entertainment”).';
    }
  }
  var CK = 'bb_known_merchants', CC = 'bb_known_categories',
      CA = 'bb_known_accounts';
  var merchants = [], categories = [], accounts = [];

  function money(n) { return '$' + Math.round(n || 0).toLocaleString(); }
  function esc(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
  }

  // The offset-account feed is Company-scoped (v0.4.0.2): the rule's
  // Applies-to-Company select drives which chart's accounts are offered, so the
  // sessionStorage cache is keyed per Company and re-fetched whenever the scope
  // changes. Merchants + categories are Company-agnostic and stay globally cached.
  function accountCompany() {
    return companySel ? (companySel.value || '') : '';
  }
  function accountsCacheKey() { return CA + '::' + accountCompany(); }

  // `force` skips the sessionStorage copy; `fresh` additionally tells the server
  // to skip ITS per-Company cache and re-hit ERPNext (v0.4.0.6). The editor loads
  // with fresh=true because the sessionStorage copy outlives a Company toggle —
  // which is why a newly-created ERPNext account used to stay invisible.
  function loadAccounts(force, fresh) {
    var ck = accountsCacheKey();
    var have = !force && !fresh && sessionStorage.getItem(ck);
    if (have) {
      try { accounts = JSON.parse(have); } catch (e) { accounts = []; }
      onData(); return;
    }
    fetch('/api/rules/known_accounts?company=' +
          encodeURIComponent(accountCompany()) + (fresh ? '&fresh=1' : ''))
      .then(function (r) { return r.json(); })
      .then(function (res) {
        accounts = (res && res.accounts) || [];
        try { sessionStorage.setItem(ck, JSON.stringify(accounts)); } catch (e) {}
        onData();
      }).catch(function () { onData(); });
  }

  function load(force, freshAccounts) {
    loadAccounts(force, freshAccounts);
    var haveM = !force && sessionStorage.getItem(CK);
    var haveC = !force && sessionStorage.getItem(CC);
    if (haveM && haveC) {
      try {
        merchants = JSON.parse(haveM); categories = JSON.parse(haveC);
      } catch (e) { merchants = []; categories = []; }
      onData(); return;
    }
    Promise.all([
      fetch('/api/rules/known_merchants').then(function (r) { return r.json(); }),
      fetch('/api/rules/known_categories').then(function (r) { return r.json(); })
    ]).then(function (res) {
      merchants = (res[0] && res[0].merchants) || [];
      categories = (res[1] && res[1].categories) || [];
      try {
        sessionStorage.setItem(CK, JSON.stringify(merchants));
        sessionStorage.setItem(CC, JSON.stringify(categories));
      } catch (e) {}
      onData();
    }).catch(function () { onData(); });
  }

  function onData() { populateCategories(); applyMode(); }

  function populateCategories() {
    if (!cat) return;
    var cur = mv.value;
    cat.innerHTML = '<option value="">— pick a category seen locally —</option>' +
      categories.map(function (c) {
        var lbl = c.path + '  ·  ' + c.count + ' txn' + (c.count === 1 ? '' : 's') +
                  (c.alias ? '  → ' + c.alias : '');
        return '<option value="' + esc(c.path) + '"' +
               (c.path === cur ? ' selected' : '') + '>' + esc(lbl) + '</option>';
      }).join('');
  }

  function applyMode() {
    var m = mt.value;
    var isMerchant = (m === 'merchant_exact' || m === 'merchant_contains');
    var isCat = (m === 'plaid_category_matches');
    var isRegex = (m === 'description_regex');
    dd.style.display = 'none';
    cat.style.display = isCat ? 'block' : 'none';
    regexBox.style.display = isRegex ? 'block' : 'none';
    if (isMerchant) {
      hint.textContent = merchants.length
        ? 'Type to search ' + merchants.length + ' merchant(s) seen locally, or enter a new one.'
        : 'No local merchants yet — type any merchant name.';
    } else if (isCat) {
      hint.textContent = 'Pick a Plaid category seen locally (full hierarchy shown).';
    } else if (isRegex) {
      hint.textContent = 'A Python regex tested against the transaction description.';
    } else {
      hint.textContent = 'Amount range as JSON: [min, max] — e.g. [10, 500].';
    }
    updateNameSuggestion();
    if (isRegex) runRegex();
  }

  function isMerchantMode() {
    return mt.value === 'merchant_exact' || mt.value === 'merchant_contains';
  }

  // Two custom (non-native) dropdowns share the tested module: the merchant
  // match-value picker (below) and the offset-account picker (further down).
  // The category picker stays a native <select> — single-select from a fixed
  // list, never typed into, so Safari's collapse-on-type <datalist> bug (the
  // reason offset_account moved off <datalist> in v0.3.4) never applied to it.
  var mvDD = BankBridgeDropdown.createDropdown({
    input: mv,
    menu: dd,
    enabled: isMerchantMode,
    getOptions: function () { return merchants; },
    getLabel: function (x) { return x.name; },
    onInput: function () { updateNameSuggestion(); runRegex(); refreshPreview(); },
    onSelect: function () { updateNameSuggestion(); refreshPreview(); },
    emptyRow: function (q) {
      var t = (q || '').trim();
      if (!t) return null;
      return 'No matches — press <b>Enter</b> to use “' + esc(t) + '” as new';
    },
    renderRow: function (x) {
      var badge = x.has_rule
        ? '<span style="background:#fde68a;color:#7a5b00;border-radius:3px;padding:0 5px;font-size:10px;margin-left:6px">already has rule</span>'
        : '';
      return '<b>' + esc(x.name) + '</b>' + badge +
        '<span style="color:#888;font-size:12px;float:right">' + x.count +
        ' txns · ' + money(x.total_amount) + '</span>';
    }
  });

  // Offset-account picker (v0.3.4). Was a native datalist-backed input; Safari
  // collapsed the list mid-type (non-matching chars, deletes, arrow keys), so it
  // moved to the same shared dropdown as the merchant field.
  // Options are ERPNext GL Account docnames (plain strings) — the docname is
  // already the display label, so getLabel/renderRow are the identity. A typed
  // value the list doesn't contain is kept as-is (free-text account still works).
  if (oa && oaDD) {
    BankBridgeDropdown.createDropdown({
      input: oa,
      menu: oaDD,
      getOptions: function () { return accounts; },
      getLabel: function (x) { return x == null ? '' : String(x); },
      renderRow: function (x) { return esc(x); },
      // v0.4.0.4 · a picked/typed offset drives the Description Template auto-fill.
      onSelect: function () { onOffsetChanged(); },
      onInput: function () { onOffsetChanged(); },
      emptyRow: function (q) {
        var t = (q || '').trim();
        if (!t) return null;
        return 'No matches — press <b>Enter</b> to use “' + esc(t) + '” as new';
      }
    });
  }

  function merchantFor(name) {
    var low = (name || '').trim().toLowerCase();
    for (var i = 0; i < merchants.length; i++)
      if (merchants[i].name.toLowerCase() === low) return merchants[i];
    return null;
  }

  function updateNameSuggestion() {
    var suggestion = '';
    var m = merchantFor(mv.value);
    if (m && m.suggested_name) suggestion = m.suggested_name;
    else if (mt.value === 'plaid_category_matches') {
      var c = categories.filter(function (x) { return x.path === mv.value; })[0];
      if (c && c.alias) suggestion = c.alias;
    }
    if (suggestion && suggestion !== nameEl.value) {
      nameHint.style.display = 'block';
      nameHint.innerHTML = 'Suggested: <b>' + esc(suggestion) +
        '</b> · <a href="#" id="use-name">use</a>';
      var u = document.getElementById('use-name');
      u.addEventListener('click', function (e) {
        e.preventDefault(); nameEl.value = suggestion; nameHint.style.display = 'none';
      });
    } else {
      nameHint.style.display = 'none';
    }
  }

  function runRegex() {
    if (!regexSample) return;
    var pat = mv.value, s = regexSample.value;
    if (!pat) { regexResult.textContent = ''; return; }
    var re;
    try { re = new RegExp(pat, 'i'); }
    catch (e) { regexResult.innerHTML = '<span style="color:#c00">✗ invalid regex</span>'; return; }
    regexResult.innerHTML = re.test(s)
      ? '<span style="color:#0a7">✓ matches</span>'
      : '<span style="color:#a00">✗ no match</span>';
  }

  // Focus / input / keyboard / outside-click for the merchant picker are owned
  // by the shared dropdown module (mvDD). Only the mode switch stays here.
  mt.addEventListener('change', function () {
    applyMode(); mvDD.close(); maybeAutofill();
  });
  if (cat) cat.addEventListener('change', function () {
    mv.value = cat.value; updateNameSuggestion(); refreshPreview(); });
  if (regexSample) regexSample.addEventListener('input', runRegex);
  if (refresh) refresh.addEventListener('click', function (e) {
    e.preventDefault(); refresh.textContent = '↻ …'; load(true);
    setTimeout(function () { refresh.textContent = '↻ refresh'; }, 600); });
  // Re-scope the offset-account feed when the rule's Company changes, so the
  // picker never offers another Company's chart (Mode A) — or flips to the
  // logical-name feed (Mode B) when set back to all-Companies (v0.4.0.2/.3).
  if (companySel) companySel.addEventListener('change', function () {
    accounts = []; updateOffsetModeHint(); loadAccounts(false, true);
  });
  // Manual "↻ refresh accounts" — for an account created in ANOTHER tab while
  // this editor sat open, which no page-load hook can catch (v0.4.0.6). Drops the
  // server caches too, then repopulates this Company's sessionStorage copy.
  if (oaRefresh) oaRefresh.addEventListener('click', function (e) {
    e.preventDefault();
    var ck = accountsCacheKey();
    oaRefresh.textContent = '↻ refreshing…';
    fetch('/api/rules/refresh_accounts?company=' +
          encodeURIComponent(accountCompany()))
      .then(function (r) { return r.json(); })
      .then(function (res) {
        accounts = (res && res.accounts) || [];
        try { sessionStorage.setItem(ck, JSON.stringify(accounts)); } catch (e) {}
        onData();
        oaRefresh.textContent = '↻ refreshed (' + accounts.length + ')';
      }).catch(function () { oaRefresh.textContent = '↻ refresh failed'; })
      .then(function () {
        setTimeout(function () {
          oaRefresh.textContent = '↻ refresh accounts'; }, 2000); });
  });
  // Description Template: a manual edit takes ownership (stop auto-filling);
  // "reset to default" hands it back and regenerates. Both refresh the preview.
  if (dt) dt.addEventListener('input', function () {
    dtAuto = false; refreshPreview();
  });
  if (dtReset) dtReset.addEventListener('click', function (e) {
    e.preventDefault();
    if (dt) { dt.value = defaultTemplate(); dtAuto = true; }
    refreshPreview();
  });

  updateOffsetModeHint();
  refreshPreview();
  // Merchants/categories may come from sessionStorage; the ACCOUNT feed is always
  // refetched from ERPNext on editor load (v0.4.0.6).
  load(false, true);
})();
</script>
{% endraw %}
"""


def _matches_sort(sort: str) -> tuple:
    """(sort_url, arrow) for the Matches column header (v0.4.6). Cycles
    default → most-matched-first → least-matched-first → default, so the
    "which of my rules are dead?" question is two clicks away in either
    direction and a third click restores the priority ordering the rest of
    the page reasons in."""
    if sort == 'matches':
        return '/admin/rules?sort=matches_asc', ' ↓'
    if sort == 'matches_asc':
        return '/admin/rules', ' ↑'
    return '/admin/rules?sort=matches', ''


def _rules_page(flash_msg='', test_result=None, test=None, form=None,
                show_archived=False, confirm_party_mismatch=False,
                scope_warning='', confirm_scope_mismatch=False, sort=''):
    cur_company = _current_company()
    live = (CategorizationRule.query
            .filter(CategorizationRule.archived.is_(False))
            .order_by(CategorizationRule.priority.asc(),
                      CategorizationRule.id.asc()).all())
    if sort == 'matches':
        live.sort(key=lambda r: -(r.match_count or 0))
    elif sort == 'matches_asc':
        live.sort(key=lambda r: (r.match_count or 0))
    if cur_company:
        # In a Company scope, show rules scoped to it PLUS company-agnostic rules
        # (which apply everywhere, this Company included).
        live = [r for r in live
                if not (r.applies_to_company or '').strip()
                or (r.applies_to_company or '').strip() == cur_company]
    archived = []
    if show_archived:
        archived = (CategorizationRule.query
                    .filter(CategorizationRule.archived.is_(True))
                    .order_by(CategorizationRule.updated_at.desc()).all())
    # ERPNext Company list for the scope picker on the rule form (best-effort).
    companies = []
    if erps.is_configured():
        try:
            companies = erpnext_bank.list_companies()
        except (ERPNextConfigError, ERPNextError):
            companies = []
    return _page(RULES_BODY, page='rules', rules=live, archived=archived,
                 show_archived=show_archived, cur_company=cur_company,
                 companies=companies,
                 active_counts=rule_stats.active_match_counts(),
                 match_types=categorization.MATCH_TYPES,
                 offset_directions=categorization.OFFSET_DIRECTIONS,
                 form=form or {'active': True, 'priority': 100},
                 test=test or {}, test_result=test_result,
                 je_engine_on=current_app.config.get(
                     'ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES', False),
                 supplier_on=current_app.config.get(
                     'ERPNEXT_AUTO_CREATE_SUPPLIERS', True),
                 confirm_party_mismatch=confirm_party_mismatch,
                 scope_warning=scope_warning,
                 confirm_scope_mismatch=confirm_scope_mismatch,
                 matches_sort_url=_matches_sort(sort)[0],
                 matches_sort_arrow=_matches_sort(sort)[1],
                 match_count_rolled_at=_last_match_count_rollup(),
                 flash_msg=flash_msg)


def _last_match_count_rollup() -> str:
    """"3h ago" for the most recent match-count rollup, or '' if none has run.
    Read from the audit trail rather than a dedicated column — the rollup already
    records an event, and a stale-looking count is exactly the thing an operator
    needs the age of."""
    ev = (AuditEvent.query
          .filter(AuditEvent.event_type == 'rule_match_counts_rolled_up')
          .order_by(AuditEvent.at.desc()).first())
    return _ago(ev.at) if ev is not None else ''


@bp.get('/admin/rules')
def rules_page():
    # v0.4.0.6 · opening the editor drops the cached offset-account feeds, so the
    # dropdown reflects accounts added in ERPNext since the last visit. The page
    # JS also requests ?fresh=1, which is what actually carries the guarantee
    # under multiple worker processes (this cache is process-local).
    _invalidate_accounts_cache()
    # New rules default their scope to the active Company (if any), so building
    # rules while scoped to a Company keeps them in that Company by default.
    form = {'active': True, 'priority': 100,
            'applies_to_company': _current_company() or None}
    # v0.4.6 · "Create rule from this transaction / group" lands here with the
    # match fields pre-filled (see _rule_prefill_url). Everything is validated
    # the same way a hand-typed form is — the prefill is a starting point the
    # operator still edits and saves, never a save of its own.
    if request.args.get('prefill') in ('1', 'true', 'yes'):
        match_type = (request.args.get('match_type') or '').strip()
        if match_type not in categorization.MATCH_TYPES:
            match_type = 'merchant_exact'
        form.update({
            'match_type': match_type,
            'match_value': (request.args.get('match_value') or '').strip(),
            'name': (request.args.get('name') or '').strip(),
            # Party Type is deliberately absent so the editor's Auto default
            # (v0.4.0.9) applies, and Description Template stays empty so the
            # v0.4.0.4 auto-fill kicks in once an Offset Account is picked.
            'applies_to_company': (
                (request.args.get('applies_to_company') or '').strip()
                or _current_company() or None),
        })
    edit_id = (request.args.get('edit') or '').strip()
    if edit_id.isdigit():
        rule = db.session.get(CategorizationRule, int(edit_id))
        if rule is not None:
            form = rule.to_dict()
    show_archived = request.args.get('archived') in ('1', 'true', 'yes')
    sort = (request.args.get('sort') or '').strip()
    return _rules_page(flash_msg=request.args.get('flash', ''), form=form,
                       show_archived=show_archived, sort=sort)


def _rule_form_values():
    """Pull + sanitize the shared rule fields from the POST form."""
    raw_prio = (request.form.get('priority') or '').strip()
    try:
        priority = int(raw_prio)
    except ValueError:
        priority = 100
    # v0.4.0.8 · canonicalize the case ('auto' → 'Auto') and drop anything the
    # engine doesn't know, so a hand-rolled POST can't store a party_type that
    # would later be handed to ERPNext as a doctype. Blank stays None = no Party.
    party_type = (request.form.get('party_type') or '').strip()
    party_type = {p.lower(): p for p in categorization.PARTY_TYPES}.get(
        party_type.lower(), '') or None
    direction = (request.form.get('offset_direction') or 'auto').strip()
    if direction not in categorization.OFFSET_DIRECTIONS:
        direction = 'auto'
    return {
        'name': (request.form.get('name') or '').strip(),
        'priority': priority,
        'active': bool(request.form.get('active')),
        'match_type': (request.form.get('match_type') or 'merchant_contains').strip(),
        'match_value': (request.form.get('match_value') or '').strip(),
        # v0.3.1 · bank-agnostic offset side.
        'offset_account': (request.form.get('offset_account') or '').strip(),
        'offset_direction': direction,
        # Deprecated pre-v0.3.1 pair — still accepted for backwards compat so a
        # legacy form/caller keeps working during the transition.
        'debit_account': (request.form.get('debit_account') or '').strip(),
        'credit_account': (request.form.get('credit_account') or '').strip(),
        'party_type': party_type,
        'party_name': (request.form.get('party_name') or '').strip() or None,
        # v0.4.0.7 · the checkbox is authoritative — the transfer heuristic only
        # pre-checks it in the editor, it never overrides a saved choice.
        'skip_party': bool(request.form.get('skip_party')),
        # v0.4.1 · default ON. The checkbox renders checked, so a plain save
        # keeps a rule out of the way of intercompany transfers; only an operator
        # who deliberately clears it gets the old behaviour on paired rows. A
        # hand-rolled POST omitting the field therefore reads as "don't ignore",
        # which is why the editor always emits it (see the hidden companion input).
        'ignore_for_paired': bool(request.form.get('ignore_for_paired')),
        'description_template': (request.form.get('description_template') or '').strip(),
        # v0.4.0.1 · optional multi-entity scope. Blank → company-agnostic.
        'applies_to_company': (request.form.get('applies_to_company') or '').strip() or None,
        # v0.4.49 · optional Bank-Bridge-internal attribution tag. Normalised to
        # a slug (lowercase, spaces → underscores) so 'Owner Distribution' and
        # 'owner_distribution' aggregate as one reason in the reconciliation
        # view. Never sent to ERPNext.
        'bb_internal_tag': _slug_tag(request.form.get('bb_internal_tag')),
    }


def _slug_tag(raw: str) -> str:
    """A tag input reduced to a stable slug: lowercased, trimmed, inner runs of
    non-word characters collapsed to single underscores. '' stays ''."""
    cleaned = re.sub(r'[^a-z0-9]+', '_', (raw or '').strip().lower())
    return cleaned.strip('_')


def _redisplay_form(vals: dict) -> dict:
    """`vals` plus the edited rule's `id`, for re-rendering the editor after a
    warn-and-confirm (v0.4.6).

    The id matters: the editor's hidden `id` field is what makes the confirmed
    re-submit SUPERSEDE the rule being edited rather than create a second copy
    of it. `_rule_form_values` deliberately doesn't carry the id — it builds the
    kwargs for `CategorizationRule(**vals)`, where an id has no business — so the
    re-render needs it added back here."""
    raw_id = (request.form.get('id') or '').strip()
    return dict(vals, id=int(raw_id)) if raw_id.isdigit() else dict(vals)


def _party_type_conflict(vals: dict) -> tuple[str, str]:
    """(severity, message) for the rule form's party_type / offset_account pair
    — the HTTP-layer wrapper around categorization.party_type_conflict
    (v0.4.0.9). Best-effort: an unconfigured or unreachable ERPNext yields no
    verdict, so the save proceeds exactly as it did before."""
    if not erps.is_configured():
        return '', ''
    try:
        return categorization.party_type_conflict(
            erpnext_bank.get_client(), vals.get('party_type') or '',
            vals.get('offset_account') or '',
            vals.get('applies_to_company') or '')
    except (ERPNextConfigError, ERPNextError):
        return '', ''


def _offset_scope_conflict(vals: dict) -> str:
    """The scope-mismatch warning for a rule whose Offset Account belongs to a
    different ERPNext Company than the rule is scoped to, or '' when they agree
    (v0.4.6).

    This is v0.4.0.2's push-time cross-Company guard, moved forward to authoring
    time. Left alone, the rule saves happily, generates Journal Entries, and
    every one of them is blocked — days later, in a different part of the UI.

    Best-effort by construction, and it must stay that way: an unconfigured or
    unreachable ERPNext, or an account whose Company can't be read, returns ''
    and the save proceeds exactly as it did before. A Mode B (Company-agnostic)
    rule also returns '' — its offset is a bare LOGICAL name that resolves per
    Company at JE time (v0.4.0.3), so there is no single Company to disagree
    with."""
    offset = (vals.get('offset_account') or '').strip()
    if not offset:
        return ''
    rule_company = _offset_account_company(vals.get('applies_to_company'))
    if not rule_company:
        return ''
    if not erps.is_configured():
        return ''
    try:
        account_company = erpnext_accounts.account_company(
            erpnext_bank.get_client(), offset)
    except (ERPNextConfigError, ERPNextError):
        return ''
    if not account_company or account_company == rule_company:
        return ''
    return (f'“{offset}” belongs to {account_company}, but this rule is scoped '
            f'to {rule_company}.')


@bp.post('/admin/rules/rollup_match_counts')
def rollup_match_counts_route():
    """Recount every rule's matches now, instead of waiting for the daily job.
    Local-only (it reads the GeneratedJournalEntry table), so it is safe to press
    repeatedly and works with ERPNext unreachable."""
    result = rule_stats.rollup_match_counts()
    audit.record('rule_match_counts_rolled_up', subject_type=None, after=result,
                 notes=(f"recounted {result['scanned']} rule(s) — "
                        f"{result['updated']} changed"))
    return redirect('/admin/rules?flash=' + quote_plus(
        f"Match counts refreshed: {result['scanned']} rule(s) scanned, "
        f"{result['updated']} updated."))


@bp.post('/admin/rules/backfill_tags')
def backfill_internal_tags_route():
    """Re-run all active rules against every stored transaction and update the
    Bank-Bridge-internal tags (v0.4.49).

    ONLY THE TAG COLUMN. This never builds, posts, or alters a Journal Entry —
    it exists so a rule added purely to attribute variance can label a year of
    history without retro-posting accounting entries for it. Local-only and
    idempotent, so it is safe to press repeatedly and works with ERPNext
    unreachable."""
    result = categorization.backfill_internal_tags()
    audit.record('internal_tags_backfilled', subject_type=None, after=result,
                 notes=(f"examined {result['examined']} transaction(s) — "
                        f"{result['tagged']} tagged, {result['cleared']} "
                        f"cleared; no Journal Entries touched"))
    db.session.commit()
    return redirect('/admin/rules?flash=' + quote_plus(
        f"Internal tags backfilled: {result['examined']} transaction(s) "
        f"examined, {result['tagged']} tagged, {result['cleared']} cleared. "
        "No Journal Entries were created or changed."))


@bp.post('/admin/rules/save')
def save_rule():
    """Create a rule, or NON-DESTRUCTIVELY edit one: an edit clones the rule to a
    new row and archives the old (active=False, superseded_by=new.id) so the
    prior version — and therefore any past auto-JE decision — is preserved."""
    vals = _rule_form_values()
    if vals['match_type'] not in categorization.MATCH_TYPES:
        return redirect('/admin/rules?flash=' + quote_plus(
            f"Unknown match type '{vals['match_type']}'"))
    # v0.4.0.9 · refuse a party_type the offset account can't carry. ERPNext
    # only accepts a Party on a Receivable/Payable account and enforces it at
    # SUBMIT, so without this check the rule saves, the JEs generate, and the
    # operator only finds out when every one of them fails to approve.
    conflict, conflict_msg = _party_type_conflict(vals)
    if conflict == 'block':
        return _rules_page(flash_msg='⚠ ' + conflict_msg,
                           form=_redisplay_form(vals))
    if conflict == 'warn' and not request.form.get('confirm_party_mismatch'):
        # A Mode B logical offset that resolves badly under SOME Company. The
        # operator may know it never fires there, so re-render with the warning
        # and a confirm flag rather than refusing outright.
        return _rules_page(
            flash_msg='⚠ ' + conflict_msg + ' Save again to keep it anyway.',
            form=_redisplay_form(vals), confirm_party_mismatch=True)
    # v0.4.6 · scope mismatch, caught at authoring. Warn-and-confirm rather than
    # block: an operator mid-reorganization may be pointing a rule at an account
    # whose Company is about to change, and refusing outright would leave them
    # unable to save the rule at all.
    scope_msg = _offset_scope_conflict(vals)
    if scope_msg and not request.form.get('confirm_scope_mismatch'):
        return _rules_page(form=_redisplay_form(vals), scope_warning=scope_msg,
                           confirm_scope_mismatch=True,
                           confirm_party_mismatch=bool(
                               request.form.get('confirm_party_mismatch')))
    raw_id = (request.form.get('id') or '').strip()
    if raw_id.isdigit():
        old = db.session.get(CategorizationRule, int(raw_id))
        if old is None:
            return redirect('/admin/rules?flash=' + quote_plus('Rule not found'))
        before = old.to_dict()
        # Clone → new current version.
        new_rule = CategorizationRule(**vals)
        db.session.add(new_rule)
        db.session.flush()               # assign new_rule.id
        # Archive the previous version, linking it forward.
        old.active = False
        old.archived = True
        old.superseded_by = new_rule.id
        db.session.commit()
        audit.record('rule_updated', subject_type='CategorizationRule',
                     subject_id=new_rule.id, before=before,
                     after=new_rule.to_dict(),
                     notes=f'rule #{old.id} superseded by #{new_rule.id}')
        msg = f'Updated rule “{new_rule.name}” (v#{new_rule.id}; #{old.id} archived)'
    else:
        new_rule = CategorizationRule(**vals)
        db.session.add(new_rule)
        db.session.commit()
        audit.record('rule_created', subject_type='CategorizationRule',
                     subject_id=new_rule.id, after=new_rule.to_dict())
        msg = f'Added rule “{vals["name"]}”'
    # Conflict detection (v0.3.2): warn — never block — if a higher-or-equal
    # priority active rule already matches the same value and would shadow this
    # one. The old (now-archived) version is excluded via new_rule.id.
    conflicts = categorization.conflicting_rules(
        new_rule.match_type, new_rule.match_value, new_rule.priority or 0,
        exclude_id=new_rule.id)
    if conflicts:
        c = conflicts[0]
        msg += (f'  ⚠ Heads up: rule “{c.name or ("#" + str(c.id))}” at priority '
                f'{c.priority} already matches “{new_rule.match_value}”. This new '
                f'rule at priority {new_rule.priority} won’t fire for those '
                f'transactions unless you raise its priority (lower number) or '
                f'disable the older rule.')
    return redirect('/admin/rules?flash=' + quote_plus(msg))


@bp.post('/admin/rules/delete')
def delete_rule():
    """Archive a rule (never a hard delete): active=False, archived=True. The
    row stays for history + audit reconstruction."""
    raw_id = (request.form.get('id') or '').strip()
    if raw_id.isdigit():
        rule = db.session.get(CategorizationRule, int(raw_id))
        if rule is not None:
            before = rule.to_dict()
            rule.active = False
            rule.archived = True
            db.session.commit()
            audit.record('rule_deleted', subject_type='CategorizationRule',
                         subject_id=rule.id, before=before, after=rule.to_dict(),
                         notes='archived (soft delete — history preserved)')
    return redirect('/admin/rules?flash=' + quote_plus('Rule archived'))


@bp.post('/admin/rules/toggle')
def toggle_rule():
    raw_id = (request.form.get('id') or '').strip()
    if raw_id.isdigit():
        rule = db.session.get(CategorizationRule, int(raw_id))
        if rule is not None:
            before = rule.to_dict()
            rule.active = not rule.active
            # v0.5.9 · OFF→ON restarts the "currently active" count from now, so
            # matches accumulated in a prior ON stretch stay historical-only.
            if rule.active:
                rule.activated_at = datetime.now(timezone.utc)
            db.session.commit()
            audit.record('rule_updated', subject_type='CategorizationRule',
                         subject_id=rule.id, before=before, after=rule.to_dict(),
                         notes=f'active → {rule.active} (toggle)')
    return redirect('/admin/rules?flash=' + quote_plus('Toggled'))


@bp.post('/admin/rules/test')
def test_rule():
    """Evaluate the sample transaction against every active rule and preview the
    Journal Entry the winning rule would generate. Read-only — no ERPNext write."""
    from types import SimpleNamespace
    raw_amt = (request.form.get('amount') or '').strip()
    try:
        amount = float(raw_amt) if raw_amt else 0.0
    except ValueError:
        amount = 0.0
    test = {
        'merchant_name': (request.form.get('merchant_name') or '').strip(),
        'description': (request.form.get('description') or '').strip(),
        'amount': raw_amt,
        'category': (request.form.get('category') or '').strip(),
    }
    sample = SimpleNamespace(
        merchant_name=test['merchant_name'], name=test['description'],
        category=test['category'], amount=amount, date=None, account_id=None,
        plaid_transaction_id='(sample)',
        erpnext_bank_transaction_id='(the Bank Transaction)')
    rule = categorization.find_matching_rule(sample)
    result = {'matched': rule is not None, 'rule': rule}
    if rule is not None:
        import json as _json
        remark = categorization.render_description(rule, sample)
        # The sample isn't a real linked account, so show a placeholder for the
        # auto-resolved bank side (only used by the v0.3.1 offset path).
        doc = categorization.build_journal_entry(
            rule, sample, erps.load().get('default_company', '(company)'),
            remark=remark, bank_account="(the transaction's bank account)")
        result['je_preview'] = _json.dumps(doc, indent=2)
    return _rules_page(test=test, test_result=result)


# ── Suppliers ────────────────────────────────────────────────────

SUPPLIERS_BODY = """
<h2>Auto-created Suppliers</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
<p style="font-size:14px;color:#555">
  Merchants seen on synced transactions, cached to their ERPNext Supplier so
  Bank Transactions are linkable. Auto-Supplier creation is
  {% if supplier_on %}<span class="pill pill-ok">ON</span>{% else %}<span class="pill pill-muted">OFF</span>{% endif %}.
  Fix a wrong normalization or re-point the ERPNext link with <b>Edit</b>.
</p>
<form method="get" action="/admin/suppliers" class="card"
      style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
  <label style="margin:0;flex:1">Search
    <input name="q" value="{{ cur_q }}" placeholder="merchant / normalized / ERPNext name">
  </label>
  <button type="submit" class="primary">Filter</button>
</form>
<table>
  <tr><th>Merchant (raw)</th><th>Normalized</th><th>ERPNext Supplier</th>
      <th class="num">Txns</th><th class="num">Total</th><th>Last seen</th><th></th></tr>
  {% for s in rows %}
  {% if edit_id == s.id %}
  <tr>
    <td>{{ s.merchant_name }}</td>
    <td colspan="2">
      <form method="post" action="/admin/suppliers/edit" style="display:flex;gap:8px;margin:0;flex-wrap:wrap">
        <input type="hidden" name="id" value="{{ s.id }}">
        <input name="normalized_name" value="{{ s.normalized_name }}" style="flex:1;min-width:120px" placeholder="normalized">
        <input name="erpnext_supplier_name" value="{{ s.erpnext_supplier_name or '' }}" style="flex:1;min-width:120px" placeholder="ERPNext Supplier">
        <button type="submit" class="primary" style="padding:4px 12px">Save</button>
        <a href="/admin/suppliers" class="secondary" style="text-decoration:none;padding:4px 12px">Cancel</a>
      </form>
    </td>
    <td class="num">{{ s.transaction_count }}</td>
    <td class="num">{{ '%.2f'|format(s.total_amount or 0.0) }}</td>
    <td>{{ s.last_transaction_at.strftime('%Y-%m-%d') if s.last_transaction_at else '—' }}</td>
    <td></td>
  </tr>
  {% else %}
  <tr>
    <td>{{ s.merchant_name }}</td>
    <td>{{ s.normalized_name }}</td>
    <td>{% if s.erpnext_supplier_name %}<code>{{ s.erpnext_supplier_name }}</code>{% else %}<span class="pill pill-muted">unlinked</span>{% endif %}</td>
    <td class="num">{{ s.transaction_count }}</td>
    <td class="num">{{ '%.2f'|format(s.total_amount or 0.0) }}</td>
    <td>{{ s.last_transaction_at.strftime('%Y-%m-%d') if s.last_transaction_at else '—' }}</td>
    <td><a href="/admin/suppliers?edit={{ s.id }}{% if cur_q %}&q={{ cur_q }}{% endif %}" class="secondary" style="text-decoration:none;padding:3px 10px;font-size:12px">Edit</a></td>
  </tr>
  {% endif %}
  {% endfor %}
  {% if not rows %}<tr><td colspan="7" style="color:#888">No suppliers cached yet.</td></tr>{% endif %}
</table>
"""


@bp.get('/admin/suppliers')
def suppliers_page():
    cur_q = (request.args.get('q') or '').strip()
    q = Supplier.query
    if cur_q:
        like = f'%{cur_q}%'
        q = q.filter(db.or_(Supplier.merchant_name.ilike(like),
                            Supplier.normalized_name.ilike(like),
                            Supplier.erpnext_supplier_name.ilike(like)))
    rows = q.order_by(Supplier.transaction_count.desc(),
                      Supplier.normalized_name.asc()).limit(500).all()
    edit_id = (request.args.get('edit') or '').strip()
    edit_id = int(edit_id) if edit_id.isdigit() else None
    return _page(SUPPLIERS_BODY, page='suppliers', rows=rows, cur_q=cur_q,
                 edit_id=edit_id,
                 supplier_on=current_app.config.get(
                     'ERPNEXT_AUTO_CREATE_SUPPLIERS', True),
                 flash_msg=request.args.get('flash', ''))


@bp.post('/admin/suppliers/edit')
def edit_supplier():
    raw_id = (request.form.get('id') or '').strip()
    if not raw_id.isdigit():
        return redirect('/admin/suppliers?flash=' + quote_plus('Bad id'))
    s = db.session.get(Supplier, int(raw_id))
    if s is None:
        return redirect('/admin/suppliers?flash=' + quote_plus('Supplier not found'))
    before = s.to_dict()
    normalized = (request.form.get('normalized_name') or '').strip()
    erp = (request.form.get('erpnext_supplier_name') or '').strip()
    if normalized:
        # Guard the unique constraint: another row already owns this key.
        clash = (Supplier.query
                 .filter(Supplier.normalized_name == normalized,
                         Supplier.id != s.id).first())
        if clash is not None:
            return redirect('/admin/suppliers?flash=' + quote_plus(
                f'Another supplier already uses “{normalized}”'))
        s.normalized_name = normalized
    s.erpnext_supplier_name = erp or None
    db.session.commit()
    audit.record('supplier_edited', subject_type='Supplier', subject_id=s.id,
                 before=before, after=s.to_dict(), notes='manual relink/rename')
    return redirect('/admin/suppliers?flash=' + quote_plus('Supplier updated'))


# ── Generated Journal Entries (audit) ────────────────────────────

def _state_pill(state):
    """The coloured state pill — the single source of truth the client-side JS
    `statePill()` mirrors when it refreshes a row in place."""
    pills = {
        'approved': ('pill-ok', 'approved', ''),
        'rejected': ('pill-muted', 'rejected', ''),
        'reversed': ('pill-muted', 'reversed', ''),
        'error': ('pill-err', 'error', ''),
        'blocked': ('pill-err', 'blocked', ''),
        'skipped_missing_account': (
            'pill-err', 'skipped · missing account',
            "No account with this logical name under the transaction's Company"),
    }
    cls, label, title = pills.get(state, ('pill-muted', state or '', ''))
    t = f' title="{title}"' if title else ''
    return f'<span class="pill {cls}"{t}>{label}</span>'


def _row_action_buttons(g):
    """Server-render the per-row action buttons for state `g.state`. Kept as a
    single source of truth the client-side JS mirrors when it refreshes a row
    in place after a successful action (see the `<script>` block below)."""
    gid, je = g.id, (g.erpnext_journal_entry_name or '')
    st = g.state

    def btn(action, label):
        return (
            f'<form method="post" action="/admin/generated_entries/{action}" '
            f'class="je-action" style="display:inline;margin:0">'
            f'<input type="hidden" name="id" value="{gid}">'
            f'<button type="submit" class="secondary" '
            f'style="padding:3px 10px;font-size:12px">{label}</button></form> ')

    out = ''
    if st == 'pending_review' and je:
        out += btn('approve', 'Approve') + btn('reject', 'Reject')
    elif st == 'approved':
        out += btn('reverse', 'Reverse') + btn('reject', 'Reject (cancel)')
    elif st == 'skipped_missing_account':
        out += btn('retry', 'Retry') + btn('reject', 'Reject')
    elif st in ('blocked', 'error'):
        out += btn('reject', 'Reject')
    return out or '<span style="color:#bbb;font-size:12px">—</span>'


GENERATED_BODY = """
<h2>Generated Journal Entries</h2>
<div id="je-flash" class="creds" style="display:{{ 'block' if flash_msg else 'none' }}">
  <b>{{ flash_msg }}</b></div>
<p style="font-size:14px;color:#555">
  Audit trail of Journal Entries the rules engine created. <b>Approve</b> submits
  the Draft JE in ERPNext (Draft → Submitted); <b>Reject</b> abandons a Draft or
  cancels a submitted JE; <b>Reverse</b> books an offsetting entry to undo an
  approval; <b>Retry</b> re-runs a rule once its missing account exists. State
  reflects the local audit record.
</p>
<form method="get" action="/admin/generated_entries" class="card"
      style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
  <label style="margin:0">State
    <select name="state">
      <option value="">(any)</option>
      {% for st in states %}
      <option value="{{ st }}" {{ 'selected' if cur_state == st else '' }}>{{ st }}</option>
      {% endfor %}
    </select>
  </label>
  <button type="submit" class="primary">Filter</button>
</form>

<form id="je-bulk" method="post" action="/admin/generated_entries/bulk"
      style="margin:8px 0">
  <button type="submit" name="action" value="approve" class="secondary">Approve selected</button>
  <button type="submit" name="action" value="reject" class="secondary" style="margin-left:8px">Reject selected</button>
  <span style="margin-left:12px;color:#888;font-size:12px">
    With nothing checked, these act on <b>all pending</b> entries.</span>
</form>

<table>
  <tr>
    <th style="width:26px"><input type="checkbox" id="je-check-all" title="Select all"></th>
    <th>Created</th><th>Merchant</th><th class="num">Amount</th><th>Rule</th>
    <th>Journal Entry</th><th>State</th><th></th></tr>
  {% for g in rows %}
  <tr data-je-id="{{ g.id }}" data-je-name="{{ g.erpnext_journal_entry_name or '' }}">
    <td><input type="checkbox" class="je-check" name="ids" value="{{ g.id }}"
               form="je-bulk"></td>
    <td style="font-size:12px">{{ g.created_at.strftime('%Y-%m-%d %H:%M') if g.created_at else '' }}</td>
    <td>{{ g.merchant_name }}<div style="font-size:11px;color:#888">{{ (g.description or '')[:60] }}</div></td>
    <td class="num">{{ '%.2f'|format(g.amount or 0.0) }}</td>
    <td style="font-size:12px">
      {% if is_opening(g) %}<span class="pill pill-warn"
        title="Not a transaction — what this account already held when it was
linked. Approving it corrects the account's balance sheet position."
        >opening balance</span>{% else %}{{ g.rule_name }}{% endif %}</td>
    <td style="font-size:12px">{% if g.erpnext_journal_entry_name %}<code>{{ g.erpnext_journal_entry_name }}</code>{% else %}—{% endif %}
      {% if g.error_message %}<div style="color:#a04000">{{ g.error_message[:100] }}</div>{% endif %}</td>
    <td class="je-state-cell">{{ state_pill(g.state)|safe }}</td>
    <td class="je-actions-cell" style="white-space:nowrap">{{ row_actions(g)|safe }}</td>
  </tr>
  {% endfor %}
  {% if not rows %}<tr><td colspan="8" style="color:#888">No generated entries yet.</td></tr>{% endif %}
</table>
<p style="font-size:12px;color:#888">Showing up to {{ limit }} most recent.</p>

<script>
(function () {
  var flash = document.getElementById('je-flash');
  function showFlash(msg, ok) {
    if (!flash) return;
    flash.style.display = 'block';
    flash.style.background = ok ? '' : '#fdecea';
    flash.innerHTML = '<b>' + msg + '</b>';
  }
  // Client-side mirror of the server's state pill (state_pill in admin_ui.py).
  function statePill(state) {
    var map = {
      approved: ['pill-ok', 'approved'],
      rejected: ['pill-muted', 'rejected'],
      reversed: ['pill-muted', 'reversed'],
      error: ['pill-err', 'error'],
      blocked: ['pill-err', 'blocked'],
      skipped_missing_account: ['pill-err', 'skipped · missing account']
    };
    var m = map[state] || ['pill-muted', state];
    return '<span class="pill ' + m[0] + '">' + m[1] + '</span>';
  }
  // Client-side mirror of _row_action_buttons.
  function actionBtn(id, action, label) {
    return '<form method="post" action="/admin/generated_entries/' + action +
      '" class="je-action" style="display:inline;margin:0">' +
      '<input type="hidden" name="id" value="' + id + '">' +
      '<button type="submit" class="secondary" style="padding:3px 10px;' +
      'font-size:12px">' + label + '</button></form> ';
  }
  function rowActions(id, state, jeName) {
    if (state === 'pending_review' && jeName)
      return actionBtn(id, 'approve', 'Approve') + actionBtn(id, 'reject', 'Reject');
    if (state === 'approved')
      return actionBtn(id, 'reverse', 'Reverse') + actionBtn(id, 'reject', 'Reject (cancel)');
    if (state === 'skipped_missing_account')
      return actionBtn(id, 'retry', 'Retry') + actionBtn(id, 'reject', 'Reject');
    if (state === 'blocked' || state === 'error')
      return actionBtn(id, 'reject', 'Reject');
    return '<span style="color:#bbb;font-size:12px">—</span>';
  }
  function refreshRow(tr, state) {
    var id = tr.getAttribute('data-je-id');
    var jeName = tr.getAttribute('data-je-name');
    var sc = tr.querySelector('.je-state-cell');
    var ac = tr.querySelector('.je-actions-cell');
    if (sc) sc.innerHTML = statePill(state);
    if (ac) ac.innerHTML = rowActions(id, state, jeName);
  }
  // Intercept per-row action submits → POST via fetch → refresh just that row,
  // no full-page reload. Delegated so refreshed rows keep working.
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form.classList || !form.classList.contains('je-action')) return;
    ev.preventDefault();
    var tr = form.closest('tr');
    var btn = form.querySelector('button');
    if (btn) btn.disabled = true;
    var body = new FormData(form);
    fetch(form.action, {
      method: 'POST', body: body,
      headers: {'X-Requested-With': 'fetch'}
    }).then(function (r) { return r.json().then(function (j) {
        return {ok: r.ok, j: j}; }); })
      .then(function (res) {
        showFlash(res.j.message || (res.ok ? 'Done' : 'Failed'), res.ok);
        if (res.ok && tr && res.j.state) refreshRow(tr, res.j.state);
        else if (btn) btn.disabled = false;
      })
      .catch(function () {
        showFlash('Network error — nothing changed', false);
        if (btn) btn.disabled = false;
      });
  });
  var all = document.getElementById('je-check-all');
  if (all) all.addEventListener('change', function () {
    document.querySelectorAll('.je-check').forEach(function (c) {
      c.checked = all.checked; });
  });
})();
</script>
"""


@bp.get('/admin/generated_entries')
def generated_entries_page():
    cur_state = (request.args.get('state') or '').strip()
    limit = 300
    q = GeneratedJournalEntry.query
    if cur_state:
        q = q.filter(GeneratedJournalEntry.state == cur_state)
    rows = q.order_by(GeneratedJournalEntry.created_at.desc(),
                      GeneratedJournalEntry.id.desc()).limit(limit).all()
    return _page(GENERATED_BODY, page='generated_entries', rows=rows,
                 cur_state=cur_state, limit=limit,
                 state_pill=_state_pill, row_actions=_row_action_buttons,
                 is_opening=obal.is_opening_balance_entry,
                 states=('pending_review', 'approved', 'rejected', 'reversed',
                         'error', 'blocked', 'skipped_missing_account'),
                 flash_msg=request.args.get('flash', ''))


# ── approve / reject / reverse state machine ─────────────────────
#
# Valid admin transitions (see README changelog · v0.4.0.5):
#   pending_review          → approved         (ERPNext submit succeeded)
#   pending_review          → rejected         (Draft abandoned; nothing to cancel)
#   approved                → rejected         (cancels the submitted JE in ERPNext)
#   approved                → reversed         (books a reversing JE — the "undo")
#   skipped_missing_account → pending_review   (retry once the account exists)
#   blocked / error         → rejected         (accept the block)
# `rejected` and `reversed` are terminal. Re-approving an already-approved row
# (or re-rejecting a rejected one) is an idempotent no-op — success, no ERPNext
# call — so a double-click or a bulk re-run never errors.


class _ActionResult:
    """Outcome of one state transition. `status` is the HTTP status a JSON
    (fetch) caller should see; `changed` is False for an idempotent no-op."""
    __slots__ = ('ok', 'message', 'status', 'changed')

    def __init__(self, ok, message, status=200, changed=True):
        self.ok, self.message, self.status, self.changed = (
            ok, message, status, changed)


def _approve_entry(g) -> _ActionResult:
    """Submit a Draft JE in ERPNext and flip the row to `approved`."""
    if g.state == 'approved':
        return _ActionResult(True, 'Already approved', changed=False)
    if g.state in ('rejected', 'reversed'):
        return _ActionResult(False, f'Cannot approve a {g.state} entry', 409)
    if g.state != 'pending_review':
        return _ActionResult(False, f'Cannot approve from state “{g.state}”', 409)
    if not g.erpnext_journal_entry_name:
        return _ActionResult(False, 'No ERPNext Journal Entry to submit', 409)
    erp = sync_engine.get_erp_client_or_none()
    if erp is None:
        return _ActionResult(
            False, 'ERPNext is not configured — check the connection', 503)
    try:
        categorization._submit_je(erp, g.erpnext_journal_entry_name)
    except (ERPNextConfigError, ERPNextError) as e:
        # Surface the actual ERPNext reason — never fail silently (the v0.4.0.4 bug).
        return _ActionResult(False, f'ERPNext refused the submit: {e}', 502)
    before = g.to_dict()
    g.state = 'approved'
    g.error_message = None
    g.updated_at = categorization._now()
    audit.record('journal_entry_approved', subject_type='GeneratedJournalEntry',
                 subject_id=g.id, before=before, after=g.to_dict(),
                 notes=f'submitted {g.erpnext_journal_entry_name} in ERPNext',
                 commit=False)
    audit.record('journal_entry_submitted_to_erpnext',
                 subject_type='GeneratedJournalEntry', subject_id=g.id,
                 after={'journal_entry': g.erpnext_journal_entry_name},
                 notes='submitted via admin approve', commit=False)
    return _ActionResult(True, 'Approved — submitted in ERPNext')


def _reject_entry(g) -> _ActionResult:
    """Flip the row to `rejected`. A JE that was already submitted (`approved`)
    is cancelled in ERPNext first; a never-submitted Draft is simply abandoned
    (left as a Draft — nothing to cancel)."""
    if g.state == 'rejected':
        return _ActionResult(True, 'Already rejected', changed=False)
    if g.state == 'reversed':
        return _ActionResult(False, 'Cannot reject a reversed entry', 409)
    cancel_note = ''
    if g.state == 'approved' and g.erpnext_journal_entry_name:
        erp = sync_engine.get_erp_client_or_none()
        if erp is None:
            return _ActionResult(
                False, 'ERPNext is not configured — cannot cancel a submitted '
                'Journal Entry', 503)
        try:
            erp.call_method(
                'frappe.client.cancel', http_method='POST',
                json_body={'doctype': categorization.JOURNAL_ENTRY_DT,
                           'name': g.erpnext_journal_entry_name})
        except (ERPNextConfigError, ERPNextError) as e:
            return _ActionResult(False, f'ERPNext refused the cancel: {e}', 502)
        cancel_note = f'cancelled {g.erpnext_journal_entry_name} in ERPNext'
    before = g.to_dict()
    g.state = 'rejected'
    g.updated_at = categorization._now()
    audit.record('journal_entry_rejected', subject_type='GeneratedJournalEntry',
                 subject_id=g.id, before=before, after=g.to_dict(),
                 notes=cancel_note or 'rejected via admin (Draft abandoned)',
                 commit=False)
    return _ActionResult(
        True, 'Rejected' + (' — cancelled in ERPNext' if cancel_note else ''))


def _reverse_entry(g) -> _ActionResult:
    """Undo an approved (submitted) JE by booking a reversing entry in ERPNext
    and flipping the row to `reversed`."""
    if g.state == 'reversed':
        return _ActionResult(True, 'Already reversed', changed=False)
    if g.state != 'approved':
        return _ActionResult(
            False, 'Only an approved (submitted) entry can be reversed', 409)
    if not g.erpnext_journal_entry_name:
        return _ActionResult(False, 'No ERPNext Journal Entry to reverse', 409)
    erp = sync_engine.get_erp_client_or_none()
    if erp is None:
        return _ActionResult(
            False, 'ERPNext is not configured — check the connection', 503)
    try:
        rev = categorization._reverse_je(erp, g.erpnext_journal_entry_name)
    except (ERPNextConfigError, ERPNextError) as e:
        return _ActionResult(False, f'ERPNext refused the reversal: {e}', 502)
    before = g.to_dict()
    g.state = 'reversed'
    g.updated_at = categorization._now()
    audit.record('journal_entry_reversed', subject_type='GeneratedJournalEntry',
                 subject_id=g.id, before=before, after=g.to_dict(),
                 notes=f'reversed {g.erpnext_journal_entry_name}'
                       + (f' → {rev}' if rev else ''), commit=False)
    return _ActionResult(True, 'Reversed — booked a reversing entry in ERPNext')


def _retry_entry(g) -> _ActionResult:
    """Re-run the rules engine for a `skipped_missing_account` row now that the
    operator has (presumably) created the missing account. On success the row
    moves to `pending_review` with a fresh Draft JE."""
    if g.state != 'skipped_missing_account':
        return _ActionResult(
            False, 'Only a skipped (missing account) entry can be retried', 409)
    erp = sync_engine.get_erp_client_or_none()
    if erp is None:
        return _ActionResult(
            False, 'ERPNext is not configured — check the connection', 503)
    row = BankTransaction.query.filter_by(
        plaid_transaction_id=g.plaid_transaction_id).first()
    if row is None:
        return _ActionResult(
            False, 'Original bank transaction is no longer available', 409)
    categorization.generate_journal_entry(erp, row)
    db.session.refresh(g)
    if g.state == 'pending_review' and g.erpnext_journal_entry_name:
        return _ActionResult(True, 'Retried — Journal Entry generated, pending '
                                   'review')
    if g.state == 'skipped_missing_account':
        return _ActionResult(
            False, 'Still skipped — the account for this rule does not exist '
            'under the transaction’s Company yet', 409)
    return _ActionResult(True, f'Retried — now “{g.state}”')


# The named action → handler map the endpoints and bulk route share.
_ACTIONS = {'approve': _approve_entry, 'reject': _reject_entry,
            'reverse': _reverse_entry, 'retry': _retry_entry}


def _wants_json() -> bool:
    """A fetch/XHR caller (the per-row JS) tags itself so it gets JSON; a plain
    form POST (no-JS fallback) gets a redirect + flash."""
    if request.headers.get('X-Requested-With') == 'fetch':
        return True
    accept = request.headers.get('Accept', '')
    return 'application/json' in accept and 'text/html' not in accept


def _entry_response(g, result: _ActionResult):
    if _wants_json():
        body = {'ok': result.ok, 'message': result.message,
                'id': (g.id if g else None),
                'state': (g.state if g else None)}
        return jsonify(body), (200 if result.ok else result.status)
    return redirect('/admin/generated_entries?flash=' + quote_plus(result.message))


def _do_single(action: str):
    raw_id = (request.form.get('id') or '').strip()
    if not raw_id.isdigit():
        return _entry_response(None, _ActionResult(False, 'Bad id', 400))
    g = db.session.get(GeneratedJournalEntry, int(raw_id))
    if g is None:
        return _entry_response(None, _ActionResult(False, 'Not found', 404))
    result = _ACTIONS[action](g)
    db.session.commit()
    return _entry_response(g, result)


@bp.post('/admin/generated_entries/approve')
def approve_entry():
    return _do_single('approve')


@bp.post('/admin/generated_entries/reject')
def reject_entry():
    return _do_single('reject')


@bp.post('/admin/generated_entries/reverse')
def reverse_entry():
    return _do_single('reverse')


@bp.post('/admin/generated_entries/retry')
def retry_entry():
    return _do_single('retry')


@bp.post('/admin/generated_entries/bulk')
def bulk_entries():
    """Apply one action to many rows. Explicit `ids` (the checkbox selection)
    win; with none checked, `approve`/`reject` fall back to every pending row
    (the "all pending" convenience buttons). Partial success is reported — a
    failure on one row never rolls back the rows that succeeded."""
    action = (request.form.get('action') or '').strip()
    handler = _ACTIONS.get(action)
    if handler is None:
        return _entry_response(None, _ActionResult(
            False, f'Unknown bulk action “{action}”', 400))
    ids = [int(i) for i in request.form.getlist('ids') if i.isdigit()]
    if ids:
        rows = (GeneratedJournalEntry.query
                .filter(GeneratedJournalEntry.id.in_(ids))
                .order_by(GeneratedJournalEntry.id).all())
    else:
        rows = (GeneratedJournalEntry.query
                .filter(GeneratedJournalEntry.state == 'pending_review')
                .order_by(GeneratedJournalEntry.id).all())
    done = failed = 0
    failures = []
    for g in rows:
        result = handler(g)
        if result.ok:
            done += 1
        else:
            failed += 1
            failures.append({'id': g.id, 'message': result.message})
    db.session.commit()
    verb = {'approve': 'Approved', 'reject': 'Rejected',
            'reverse': 'Reversed', 'retry': 'Retried'}.get(action, action)
    summary = f'{verb} {done} entr{"y" if done == 1 else "ies"}'
    if failed:
        summary += f' · {failed} failed'
    if _wants_json():
        return jsonify({'ok': failed == 0, 'action': action, 'done': done,
                        'failed': failed, 'failures': failures,
                        'message': summary}), 200
    return redirect('/admin/generated_entries?flash=' + quote_plus(summary))


# ── Intercompany transfers (v0.4.1) ──────────────────────────────
#
# Review queue for transfers the detector matched between two Companies the
# operator owns. The actions mirror the Generated JEs page's shape (per-row
# forms + a bulk bar) but act on the PAIR, so both entities' Journal Entries move
# together — approving one entity's half of a transfer and not the other is the
# reconciliation problem this whole feature exists to prevent.


def _pair_state_pill(state):
    pills = {'pending': ('pill-muted', 'pending'),
             'approved': ('pill-ok', 'approved'),
             'rejected': ('pill-muted', 'unpaired')}
    cls, label = pills.get(state, ('pill-muted', state or ''))
    return f'<span class="pill {cls}">{label}</span>'


def _confidence_pill(confidence):
    """Confidence as a coloured percentage. Green at/above the auto-pair
    threshold, amber below it — a pair only lands below the threshold when an
    operator paired it by hand or the threshold was raised after the fact, and
    either way it deserves a second look."""
    try:
        pct = float(confidence or 0.0) * 100.0
    except (TypeError, ValueError):
        pct = 0.0
    threshold = intercompany.confidence_threshold() * 100.0
    cls = 'pill-ok' if pct >= threshold else 'pill-err'
    return f'<span class="pill {cls}">{pct:.0f}%</span>'


INTERCOMPANY_BODY = """
<h2>Intercompany transfers</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
<p style="font-size:14px;color:#555">
  Money moved between two Companies you own arrives as <b>two</b> Plaid
  transactions — equal amount, opposite sign, one per Company. Booking each on
  its own would put an expense on one entity's profit &amp; loss and income on
  the other's, for money that never left your hands. Bank Bridge matches the two
  up and books the movement through a <b>Due from</b> / <b>Due to</b> pair
  instead, so it lands on both balance sheets and nets to zero across the
  entities. <b>Approve</b> submits both Journal Entries together;
  <b>Unpair</b> undoes the match and returns both transactions to the normal
  rules.
</p>
{% if not multi_company %}
<div class="banner-warn">
  <h3>Detection is idle</h3>
  Intercompany detection needs linked bank accounts under <b>more than one</b>
  ERPNext Company. Right now they all resolve to a single Company, so there is
  nothing to pair. Link a second Company's bank on
  <a href="/admin/link_bank">Link a bank</a> (or set the owning Company on
  <a href="/admin/accounts">Accounts</a>) and detection starts on the next sync.
</div>
{% endif %}
<form method="get" action="/admin/intercompany" class="card"
      style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
  <label style="margin:0">State
    <select name="state">
      <option value="">(any)</option>
      {% for st in states %}
      <option value="{{ st }}" {{ 'selected' if cur_state == st else '' }}>{{ st }}</option>
      {% endfor %}
    </select>
  </label>
  <label style="margin:0">Company
    <select name="company">
      <option value="">(any)</option>
      {% for c in companies %}
      <option value="{{ c }}" {{ 'selected' if cur_company_filter == c else '' }}>{{ c }}</option>
      {% endfor %}
    </select>
  </label>
  <label style="margin:0">Min confidence
    <input name="min_confidence" value="{{ cur_min_confidence }}"
           placeholder="0.75" style="width:90px">
  </label>
  <button type="submit" class="primary">Filter</button>
  <a href="/admin/intercompany" class="secondary"
     style="text-decoration:none;display:inline-block;padding:8px 16px">Clear</a>
</form>

<form id="ic-bulk" method="post" action="/admin/intercompany/bulk"
      style="margin:8px 0">
  <button type="submit" name="action" value="approve" class="secondary">Approve selected</button>
  <button type="submit" name="action" value="reject" class="secondary" style="margin-left:8px">Unpair selected</button>
  <span style="margin-left:12px;color:#888;font-size:12px">
    With nothing checked, these act on <b>all pending</b> pairs shown.</span>
</form>

<table>
  <tr>
    <th style="width:26px"><input type="checkbox" id="ic-check-all" title="Select all"></th>
    <th>Source (money out)</th><th>Target (money in)</th>
    <th class="num">Amount</th><th>Confidence</th>
    <th>Journal Entries</th><th>State</th><th></th></tr>
  {% for p in rows %}
  <tr data-ic-id="{{ p.id }}">
    <td><input type="checkbox" class="ic-check" name="ids" value="{{ p.id }}" form="ic-bulk"></td>
    <td style="font-size:12px">
      <b>{{ p.from_company or '(unknown Company)' }}</b>
      <div style="color:#888">{{ txns[p.from_transaction_id].bank if txns.get(p.from_transaction_id) else '' }}</div>
      <div>{{ txns[p.from_transaction_id].date if txns.get(p.from_transaction_id) else '' }}
        · {{ (txns[p.from_transaction_id].name if txns.get(p.from_transaction_id) else '')[:44] }}</div>
    </td>
    <td style="font-size:12px">
      <b>{{ p.to_company or '(unknown Company)' }}</b>
      <div style="color:#888">{{ txns[p.to_transaction_id].bank if txns.get(p.to_transaction_id) else '' }}</div>
      <div>{{ txns[p.to_transaction_id].date if txns.get(p.to_transaction_id) else '' }}
        · {{ (txns[p.to_transaction_id].name if txns.get(p.to_transaction_id) else '')[:44] }}</div>
    </td>
    <td class="num">{{ '%.2f'|format(p.amount or 0.0) }}</td>
    <td>{{ confidence_pill(p.confidence)|safe }}</td>
    <td style="font-size:12px">
      {% if p.from_journal_entry %}<code>{{ p.from_journal_entry }}</code><br>
        <code>{{ p.to_journal_entry }}</code>
      {% else %}<span style="color:#999">not booked</span>{% endif %}
      {% if p.note %}<div style="color:#a04000;margin-top:3px">{{ p.note[:160] }}</div>{% endif %}
    </td>
    <td class="ic-state-cell">{{ pair_pill(p.state)|safe }}</td>
    <td class="ic-actions-cell" style="white-space:nowrap">{{ pair_actions(p)|safe }}</td>
  </tr>
  {% endfor %}
  {% if not rows %}<tr><td colspan="8" style="color:#888">No intercompany transfers detected.</td></tr>{% endif %}
</table>
<p style="font-size:12px;color:#888">
  Showing up to {{ limit }} most recent. Auto-pair threshold:
  <b>{{ '%.0f'|format(threshold * 100) }}%</b> confidence, ±{{ tolerance }} day
  date window.</p>

<script>
(function () {
  var all = document.getElementById('ic-check-all');
  if (all) all.addEventListener('change', function () {
    document.querySelectorAll('.ic-check').forEach(function (c) {
      c.checked = all.checked; });
  });
  // Unpair discards a match and (when already approved) cancels two submitted
  // Journal Entries — confirm before doing that on the operator's behalf.
  document.addEventListener('submit', function (ev) {
    var f = ev.target;
    if (!f.classList || !f.classList.contains('ic-unpair')) return;
    if (!window.confirm('Unpair this transfer? Any generated Journal Entries ' +
        'are cancelled and both transactions go back to the normal rules.'))
      ev.preventDefault();
  });
})();
</script>
"""


def _pair_action_buttons(p):
    """Per-row actions for pair state `p.state`. Mirrors _row_action_buttons on
    the Generated JEs page."""

    def btn(action, label, cls=''):
        return (
            f'<form method="post" action="/admin/intercompany/{action}" '
            f'class="{cls}" style="display:inline;margin:0">'
            f'<input type="hidden" name="id" value="{p.id}">'
            f'<button type="submit" class="secondary" '
            f'style="padding:3px 10px;font-size:12px">{label}</button></form> ')

    out = ''
    if p.state == 'pending':
        if p.from_journal_entry and p.to_journal_entry:
            out += btn('approve', 'Approve')
        else:
            out += btn('retry', 'Retry')
        out += btn('reject', 'Unpair', cls='ic-unpair')
    elif p.state == 'approved':
        out += btn('reject', 'Unpair (cancel)', cls='ic-unpair')
    return out or '<span style="color:#bbb;font-size:12px">—</span>'


def _pair_transaction_context(pairs) -> dict:
    """{plaid_transaction_id: {date, name, bank}} for every leg on the page, so
    the table can show what each side actually was without N queries per row."""
    ids = set()
    for p in pairs:
        ids.add(p.from_transaction_id)
        ids.add(p.to_transaction_id)
    if not ids:
        return {}
    rows = (BankTransaction.query
            .filter(BankTransaction.plaid_transaction_id.in_(ids)).all())
    banks = {a.account_id: (a.erpnext_bank_account_name or a.name or '')
             for a in PlaidAccount.query.all()}
    return {r.plaid_transaction_id: {
        'date': r.date.isoformat() if r.date else '',
        'name': r.name or r.merchant_name or '',
        'bank': banks.get(r.account_id, '')} for r in rows}


def _intercompany_rows():
    """The filtered pair list for the page, plus the active filter values.
    Filtering on Company matches EITHER side — an operator scoped to the Farm
    wants every transfer the Farm was involved in, whichever direction it went."""
    cur_state = (request.args.get('state') or '').strip()
    company = (request.args.get('company') or '').strip() or _current_company()
    raw_min = (request.args.get('min_confidence') or '').strip()
    try:
        min_confidence = float(raw_min) if raw_min else None
    except ValueError:
        min_confidence = None
    q = IntercompanyTransferPair.query
    if cur_state:
        q = q.filter(IntercompanyTransferPair.state == cur_state)
    if company:
        q = q.filter(db.or_(IntercompanyTransferPair.from_company == company,
                            IntercompanyTransferPair.to_company == company))
    if min_confidence is not None:
        q = q.filter(IntercompanyTransferPair.confidence >= min_confidence)
    rows = q.order_by(IntercompanyTransferPair.detected_at.desc(),
                      IntercompanyTransferPair.id.desc()).limit(300).all()
    return rows, cur_state, company, raw_min


@bp.get('/admin/intercompany')
def intercompany_page():
    rows, cur_state, company, raw_min = _intercompany_rows()
    return _page(INTERCOMPANY_BODY, page='intercompany', rows=rows,
                 txns=_pair_transaction_context(rows),
                 cur_state=cur_state, cur_company_filter=company,
                 cur_min_confidence=raw_min, limit=300,
                 companies=_known_companies(),
                 multi_company=intercompany.multi_company_accounts(),
                 threshold=intercompany.confidence_threshold(),
                 tolerance=intercompany.date_tolerance_days(),
                 states=('pending', 'approved', 'rejected'),
                 pair_pill=_pair_state_pill,
                 confidence_pill=_confidence_pill,
                 pair_actions=_pair_action_buttons,
                 flash_msg=request.args.get('flash', ''))


_PAIR_ACTIONS = {
    'approve': intercompany.approve_pair,
    'reject': intercompany.reject_pair,
    'retry': intercompany.retry_pair,
}


def _pair_response(ok: bool, message: str, pair=None, status=200):
    if _wants_json():
        return jsonify({'ok': ok, 'message': message,
                        'id': (pair.id if pair else None),
                        'state': (pair.state if pair else None)}), (
            200 if ok else status)
    return redirect('/admin/intercompany?flash=' + quote_plus(message))


def _do_pair_action(action: str):
    raw_id = (request.form.get('id') or '').strip()
    if not raw_id.isdigit():
        return _pair_response(False, 'Bad id', status=400)
    pair = db.session.get(IntercompanyTransferPair, int(raw_id))
    if pair is None:
        return _pair_response(False, 'Not found', status=404)
    ok, message = _PAIR_ACTIONS[action](sync_engine.get_erp_client_or_none(),
                                        pair)
    db.session.commit()
    return _pair_response(ok, message, pair, status=409)


@bp.post('/admin/intercompany/approve')
def approve_pair_route():
    return _do_pair_action('approve')


@bp.post('/admin/intercompany/reject')
def reject_pair_route():
    return _do_pair_action('reject')


@bp.post('/admin/intercompany/retry')
def retry_pair_route():
    return _do_pair_action('retry')


@bp.post('/admin/intercompany/bulk')
def bulk_pairs():
    """Apply one action to many pairs. Explicit `ids` (the checkbox selection)
    win; with none checked, act on every pending pair matching the CURRENT
    filters — so a Company-scoped view never silently approves transfers the
    operator can't see. Partial success is reported; one pair's failure never
    rolls back the pairs that succeeded."""
    action = (request.form.get('action') or '').strip()
    handler = _PAIR_ACTIONS.get(action)
    if handler is None:
        return _pair_response(False, f'Unknown bulk action “{action}”',
                              status=400)
    ids = [int(i) for i in request.form.getlist('ids') if i.isdigit()]
    if ids:
        pairs = (IntercompanyTransferPair.query
                 .filter(IntercompanyTransferPair.id.in_(ids))
                 .order_by(IntercompanyTransferPair.id).all())
    else:
        pairs = [p for p in _intercompany_rows()[0] if p.state == 'pending']
        pairs.sort(key=lambda p: p.id)
    erp = sync_engine.get_erp_client_or_none()
    done = failed = 0
    failures = []
    for pair in pairs:
        ok, message = handler(erp, pair)
        if ok:
            done += 1
        else:
            failed += 1
            failures.append({'id': pair.id, 'message': message})
    db.session.commit()
    verb = {'approve': 'Approved', 'reject': 'Unpaired',
            'retry': 'Retried'}.get(action, action)
    summary = f'{verb} {done} transfer{"" if done == 1 else "s"}'
    if failed:
        summary += f' · {failed} failed'
    if _wants_json():
        return jsonify({'ok': failed == 0, 'action': action, 'done': done,
                        'failed': failed, 'failures': failures,
                        'message': summary}), 200
    return redirect('/admin/intercompany?flash=' + quote_plus(summary))


# ── Audit trail ──────────────────────────────────────────────────

AUDIT_BODY = """
<h2>Audit trail</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
<p style="font-size:14px;color:#555">
  Append-only, permanent record of every auditable action — supplier auto-
  creation, rule changes, JE generation / approval / rejection, sync runs.
  {% if subject_type and subject_id %}
  <b>Lifecycle of {{ subject_type }} #{{ subject_id }}</b> —
  <a href="/admin/audit">clear</a>.
  {% endif %}
</p>
<form method="get" action="/admin/audit" class="card"
      style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
  <label style="margin:0">Event type
    <select name="event_type">
      <option value="">(any)</option>
      {% for et in event_types %}
      <option value="{{ et }}" {{ 'selected' if cur.event_type == et else '' }}>{{ et }}</option>
      {% endfor %}
    </select>
  </label>
  <label style="margin:0">Subject type
    <select name="subject_type">
      <option value="">(any)</option>
      {% for st in subject_types %}
      <option value="{{ st }}" {{ 'selected' if cur.subject_type == st else '' }}>{{ st }}</option>
      {% endfor %}
    </select>
  </label>
  <label style="margin:0">Subject id
    <input name="subject_id" value="{{ cur.subject_id }}" style="width:110px">
  </label>
  <label style="margin:0">Actor
    <input name="actor" value="{{ cur.actor }}" placeholder="admin_ui / scheduler" style="width:120px">
  </label>
  <label style="margin:0">From
    <input name="date_from" type="date" value="{{ cur.date_from }}">
  </label>
  <label style="margin:0">To
    <input name="date_to" type="date" value="{{ cur.date_to }}">
  </label>
  <button type="submit" class="primary">Filter</button>
  <a class="secondary" style="text-decoration:none;padding:8px 16px"
     href="/admin/audit?format=csv&{{ query_string }}">Export CSV</a>
</form>

<table>
  <tr><th>At</th><th>Event</th><th>Actor</th><th>Subject</th><th>Notes</th><th></th></tr>
  {% for e in rows %}
  <tr>
    <td style="font-size:12px;white-space:nowrap">{{ e.at.strftime('%Y-%m-%d %H:%M:%S') if e.at else '' }}</td>
    <td style="font-size:12px"><code>{{ e.event_type }}</code></td>
    <td style="font-size:12px">{{ e.actor }}{% if e.source_ip %}<div style="color:#888">{{ e.source_ip }}</div>{% endif %}</td>
    <td style="font-size:12px">
      {% if e.subject_type %}
      <a href="/admin/audit?subject_type={{ e.subject_type }}&subject_id={{ e.subject_id }}">{{ e.subject_type }} #{{ e.subject_id }}</a>
      {% else %}—{% endif %}
    </td>
    <td style="font-size:12px;max-width:280px">{{ (e.notes or '')[:140] }}</td>
    <td><a href="/admin/audit?id={{ e.id }}" style="font-size:12px">detail</a></td>
  </tr>
  {% endfor %}
  {% if not rows %}<tr><td colspan="6" style="color:#888">No audit events match.</td></tr>{% endif %}
</table>
<p style="font-size:12px;color:#888">Showing up to {{ limit }} most recent · {{ total }} total events (permanent — never purged).</p>
"""

AUDIT_DETAIL_BODY = """
<h2>Audit event #{{ e.id }}</h2>
<p><a href="/admin/audit">← back to audit trail</a></p>
<table>
  <tr><th>At</th><td>{{ e.at.strftime('%Y-%m-%d %H:%M:%S UTC') if e.at else '' }}</td></tr>
  <tr><th>Event type</th><td><code>{{ e.event_type }}</code></td></tr>
  <tr><th>Actor</th><td>{{ e.actor }}</td></tr>
  <tr><th>Source IP</th><td>{{ e.source_ip or '—' }}</td></tr>
  <tr><th>Subject</th><td>
    {% if e.subject_type %}<a href="/admin/audit?subject_type={{ e.subject_type }}&subject_id={{ e.subject_id }}">{{ e.subject_type }} #{{ e.subject_id }}</a>{% else %}—{% endif %}
  </td></tr>
  <tr><th>Notes</th><td>{{ e.notes or '—' }}</td></tr>
</table>

<h3>Before</h3>
<pre style="white-space:pre-wrap;background:#f7f7f7;border:1px solid #ddd;border-radius:4px;padding:10px;font-size:12px">{{ before_json }}</pre>
<h3>After</h3>
<pre style="white-space:pre-wrap;background:#f7f7f7;border:1px solid #ddd;border-radius:4px;padding:10px;font-size:12px">{{ after_json }}</pre>

{% if sync_rows %}
<h3>Related ERPNext sync-log lines (subject_id {{ e.subject_id }})</h3>
<table>
  <tr><th>At</th><th>Direction</th><th>Status</th><th>Detail</th></tr>
  {% for r in sync_rows %}
  <tr>
    <td style="font-size:12px">{{ r.at.strftime('%Y-%m-%d %H:%M:%S') if r.at else '' }}</td>
    <td style="font-size:12px">{{ r.direction }}</td>
    <td>{% if r.status=='success' %}<span class="pill pill-ok">success</span>{% else %}<span class="pill pill-err">{{ r.status }}</span>{% endif %}</td>
    <td style="font-size:12px;max-width:340px">{{ (r.error_message or '')[:200] }}</td>
  </tr>
  {% endfor %}
</table>
{% endif %}
"""


def _audit_query(cur):
    """Build the filtered AuditEvent query from the parsed filter dict `cur`."""
    from datetime import datetime, timedelta
    q = AuditEvent.query
    if cur['event_type']:
        q = q.filter(AuditEvent.event_type == cur['event_type'])
    if cur['subject_type']:
        q = q.filter(AuditEvent.subject_type == cur['subject_type'])
    if cur['subject_id']:
        q = q.filter(AuditEvent.subject_id == cur['subject_id'])
    if cur['actor']:
        q = q.filter(AuditEvent.actor == cur['actor'])
    for key, op in (('date_from', 'ge'), ('date_to', 'lt')):
        val = cur[key]
        if not val:
            continue
        try:
            d = datetime.strptime(val, '%Y-%m-%d')
        except ValueError:
            continue
        if op == 'ge':
            q = q.filter(AuditEvent.at >= d)
        else:
            q = q.filter(AuditEvent.at < d + timedelta(days=1))
    return q


@bp.get('/admin/audit')
def audit_page():
    import json as _json
    # Single-event detail view.
    detail_id = (request.args.get('id') or '').strip()
    if detail_id.isdigit():
        e = db.session.get(AuditEvent, int(detail_id))
        if e is None:
            return redirect('/admin/audit?flash=' + quote_plus('Event not found'))
        d = e.to_dict()
        sync_rows = []
        if e.subject_id:
            sync_rows = (PlaidSyncLog.query
                         .filter(PlaidSyncLog.subject_id == e.subject_id)
                         .order_by(PlaidSyncLog.at.desc()).limit(50).all())
        return _page(AUDIT_DETAIL_BODY, page='audit', e=e,
                     before_json=_json.dumps(d['payload_before'], indent=2)
                     if d['payload_before'] is not None else '(none)',
                     after_json=_json.dumps(d['payload_after'], indent=2)
                     if d['payload_after'] is not None else '(none)',
                     sync_rows=sync_rows)

    cur = {k: (request.args.get(k) or '').strip() for k in
           ('event_type', 'subject_type', 'subject_id', 'actor',
            'date_from', 'date_to')}
    q = _audit_query(cur)

    # CSV export — every field, honoring the same filters.
    if (request.args.get('format') or '') == 'csv':
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['id', 'at', 'event_type', 'actor', 'subject_type',
                    'subject_id', 'payload_before', 'payload_after', 'notes',
                    'source_ip'])
        for e in q.order_by(AuditEvent.at.asc(), AuditEvent.id.asc()).all():
            w.writerow([e.id, e.at.isoformat() if e.at else '', e.event_type,
                        e.actor, e.subject_type or '', e.subject_id or '',
                        e.payload_before or '', e.payload_after or '',
                        e.notes or '', e.source_ip or ''])
        from flask import Response
        return Response(
            buf.getvalue(), mimetype='text/csv',
            headers={'Content-Disposition':
                     'attachment; filename=audit_events.csv'})

    limit = 500
    total = q.count()
    rows = q.order_by(AuditEvent.at.desc(), AuditEvent.id.desc()).limit(limit).all()
    # Preserve the active filters on the CSV-export link.
    query_string = '&'.join(f'{k}={quote_plus(v)}' for k, v in cur.items() if v)
    return _page(AUDIT_BODY, page='audit', rows=rows, cur=cur, total=total,
                 limit=limit, event_types=audit.EVENT_TYPES,
                 subject_types=audit.SUBJECT_TYPES, query_string=query_string,
                 subject_type=cur['subject_type'], subject_id=cur['subject_id'],
                 flash_msg=request.args.get('flash', ''))


# ── Plaid settings ───────────────────────────────────────────────

PLAID_SETTINGS_BODY = """
<h2>Plaid settings</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
<div class="banner-warn">
  <h3>Where these come from</h3>
  <ul>
    <li>Create a Plaid account at <code>dashboard.plaid.com</code>. Your
      <b>Client ID</b> and per-environment <b>Secrets</b> are under
      <b>Developers → Keys</b>.</li>
    <li>Add the redirect URI <code>{{ s.redirect_uri }}</code> under
      <b>Developers → API → Allowed redirect URIs</b> (required for OAuth banks
      like Wells Fargo).</li>
    <li>Start in <b>sandbox</b> to test the flow; switch to <b>production</b>
      once your Plaid app is approved for the institutions you need.</li>
  </ul>
</div>

<form class="card" method="post" action="/admin/plaid_settings">
  <label>Client ID
    <input name="client_id" value="{{ s.client_id }}" placeholder="e.g. 5f9…">
  </label>
  <label>Sandbox Secret <span style="font-weight:400;color:#888">(stored: {{ masked.sandbox_secret }} — blank keeps unchanged)</span>
    <input name="sandbox_secret" type="password" value="" placeholder="paste to set/replace">
  </label>
  <label>Production Secret <span style="font-weight:400;color:#888">(stored: {{ masked.production_secret }} — blank keeps unchanged)</span>
    <input name="production_secret" type="password" value="" placeholder="paste to set/replace">
  </label>
  <label>Environment
    <select name="environment">
      <option value="sandbox" {{ 'selected' if s.environment=='sandbox' else '' }}>sandbox</option>
      <option value="production" {{ 'selected' if s.environment=='production' else '' }}>production</option>
    </select>
  </label>
  <label>OAuth redirect URI
    <input name="redirect_uri" value="{{ s.redirect_uri }}" placeholder="http://umbrel.local:5202/bankbridge/plaid/oauth_return">
  </label>
  <label>Webhook URL <span style="font-weight:400;color:#888">(optional — leave blank for polling)</span>
    <input name="webhook_url" value="{{ s.webhook_url }}" placeholder="http://umbrel.local:5202/bankbridge/api/plaid/webhook">
  </label>
  <label>Sync frequency <span style="font-weight:400;color:#888">(how often we pull transactions from Plaid)</span>
    <select name="sync_interval_hours" id="syncFreq">
      {% for p in sync_presets %}
      <option value="{{ p.value }}" {{ 'selected' if p.value == s.sync_interval_hours else '' }}>{{ p.label }}</option>
      {% endfor %}
    </select>
  </label>
  <p id="costEst" style="font-size:13px;color:#555;margin:4px 0 0"
     data-accounts="{{ account_count }}" data-price="{{ price_per_call }}"></p>
  <p style="font-size:12px;color:#888;margin:4px 0 0">Fewer syncs = lower Plaid
    cost. Daily is plenty for most reconciliation; you can always click
    <b>Sync now</b> on the dashboard for an on-demand refresh.</p>
  <button type="submit">Save Plaid settings</button>
</form>
<script>
(function () {
  var sel = document.getElementById('syncFreq');
  var out = document.getElementById('costEst');
  if (!sel || !out) return;
  var accounts = parseInt(out.getAttribute('data-accounts'), 10) || 1;
  var price = parseFloat(out.getAttribute('data-price')) || 0.30;
  function update() {
    var h = parseInt(sel.value, 10) || 0;
    if (h <= 0) {
      out.textContent = 'Manual only — no automatic Plaid calls (use “Sync now”).';
      return;
    }
    var callsPerMonthPerAcct = (24 / h) * 30;
    var total = callsPerMonthPerAcct * accounts * price;
    out.textContent = '≈ ' + Math.round(callsPerMonthPerAcct) +
      ' Plaid calls/month per account · est. $' + total.toFixed(2) +
      '/month for ' + accounts + ' linked account' + (accounts === 1 ? '' : 's') +
      ' (at $' + price.toFixed(2) + '/call).';
  }
  sel.addEventListener('change', update);
  update();
})();
</script>
<p style="font-size:13px">Status:
  {% if configured %}<span class="pill pill-ok">configured ({{ s.environment }})</span>
  {% else %}<span class="pill pill-err">not configured</span>{% endif %}
</p>
"""


@bp.get('/admin/plaid_settings')
def plaid_settings_page():
    return _page(PLAID_SETTINGS_BODY, page='plaid_settings', s=ps.load(),
                 masked=ps.masked(), configured=ps.is_configured(),
                 sync_presets=sync_config.PRESETS,
                 account_count=(av.visible_accounts_query().count() or 1),
                 price_per_call=current_app.config.get(
                     'PLAID_PRICE_PER_CALL', sync_config.DEFAULT_PRICE_PER_CALL),
                 flash_msg=request.args.get('flash', ''))


@bp.post('/admin/plaid_settings')
def save_plaid_settings():
    client_id = request.form.get('client_id') or ''
    environment = request.form.get('environment') or 'sandbox'
    redirect_uri = request.form.get('redirect_uri') or ''
    webhook_url = request.form.get('webhook_url') or ''
    raw_sandbox = request.form.get('sandbox_secret')
    raw_prod = request.form.get('production_secret')
    sandbox_secret = raw_sandbox if (raw_sandbox or '').strip() else None
    production_secret = raw_prod if (raw_prod or '').strip() else None
    # Only touch the interval when the picker submitted a value.
    raw_interval = request.form.get('sync_interval_hours')
    sync_interval = raw_interval if raw_interval not in (None, '') else None
    ps.save(client_id, environment, redirect_uri, webhook_url,
            sandbox_secret=sandbox_secret, production_secret=production_secret,
            sync_interval_hours=sync_interval)
    return redirect('/admin/plaid_settings?flash=Saved')


# ── ERPNext settings ─────────────────────────────────────────────

ERPNEXT_SETTINGS_BODY = """
<h2>ERPNext connection</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
<div class="banner-warn">
  <h3>Before you click “Test Connection”</h3>
  <ul>
    <li>In ERPNext, open your API user (System Manager role) → <b>Settings → API Access</b> → <b>Generate Keys</b>.
      Reuse an existing ERPNext API key/secret, or mint a dedicated one.</li>
    <li>Copy the <b>API Key</b> and one-time <b>API Secret</b> below and Save.</li>
    <li>Set <b>Default Company</b> to the exact Company docname (used if ERPNext
      ever needs it; Bank Transactions themselves take the company from the mapped Bank Account).</li>
  </ul>
</div>

<form class="card" method="post" action="/admin/erpnext_settings">
  <label>ERPNext URL (Umbrel app-proxy)
    <input name="url" value="{{ s.url }}" placeholder="http://umbrel.local:5300">
  </label>
  <label>API Key
    <input name="api_key" value="{{ s.api_key }}" placeholder="e.g. 3f9a…">
  </label>
  <label>API Secret <span style="font-weight:400;color:#888">(stored: {{ masked }} — blank keeps unchanged)</span>
    <input name="api_secret" type="password" value="" placeholder="paste to set/replace">
  </label>
  <label>Default Company (docname)
    <input name="default_company" value="{{ s.default_company }}" placeholder="e.g. Example Company LLC">
  </label>
  <button type="submit">Save settings</button>
</form>

<div style="display:flex;gap:12px;flex-wrap:wrap;margin:12px 0">
  <form method="post" action="/admin/erpnext_settings/test" style="margin:0">
    <button type="submit" class="secondary">Test Connection</button>
  </form>
  <form method="post" action="/admin/erpnext_settings/verify_doctype" style="margin:0">
    <button type="submit" class="secondary">Verify Bank Transaction doctype</button>
  </form>
  <form method="post" action="/admin/erpnext_settings/ensure_fields" style="margin:0">
    <button type="submit" class="secondary">Ensure Bank Account fields &amp; types</button>
  </form>
</div>

{% if probe %}
<div class="{{ 'banner-ok' if probe.ok else 'banner-warn' }}">
  {% if probe.ok %}✓ {{ probe.detail }}{% else %}<h3>Failed</h3>{{ probe.detail }}{% endif %}
</div>
{% endif %}
"""


def _erpnext_settings_page(flash_msg='', probe=None):
    return _page(ERPNEXT_SETTINGS_BODY, page='erpnext_settings',
                 s=erps.load(), masked=erps.masked_secret(),
                 flash_msg=flash_msg, probe=probe)


@bp.get('/admin/erpnext_settings')
def erpnext_settings_page():
    return _erpnext_settings_page(flash_msg=request.args.get('flash', ''))


@bp.post('/admin/erpnext_settings')
def save_erpnext_settings():
    url = request.form.get('url') or ''
    api_key = request.form.get('api_key') or ''
    raw_secret = request.form.get('api_secret')
    api_secret = raw_secret if (raw_secret or '').strip() else None
    default_company = request.form.get('default_company') or ''
    erps.save(url, api_key, api_secret, default_company)
    return redirect('/admin/erpnext_settings?flash=Saved')


def _doctype_status_line(status: dict) -> str:
    """Per-doctype availability, appended to the Test Connection message so the
    operator sees which linked doctypes their ERPNext supports. Uses the
    bootstrap() result dict ({doctype: bool, 'partial': bool})."""
    def mark(ok):
        return 'ready ✓' if ok else 'unavailable ⚠'
    parts = [
        f"Bank Account Types: {mark(status.get(erpnext_accounts.BANK_ACCOUNT_TYPE_DT))}",
        f"Bank Account Subtypes: {mark(status.get(erpnext_accounts.ACCOUNT_SUBTYPE_DT))}",
        f"Custom fields: {mark(status.get(erpnext_accounts.CUSTOM_FIELD_DT))}",
    ]
    return ' · ' + ' · '.join(parts)


@bp.post('/admin/erpnext_settings/test')
def test_erpnext_connection():
    if not erps.is_configured():
        return _erpnext_settings_page(
            probe={'ok': False,
                   'detail': 'Not configured — set URL + API key + secret first.'})
    ok, detail = erpnext_bank.test_connection()
    msg = f'Connected as {detail}' if ok else str(detail)
    if ok:
        # Run the full bootstrap as part of setup validation and report which
        # linked doctypes this ERPNext actually supports, so the operator sees at
        # a glance what will work. Bootstrap is resilient — a missing doctype is
        # reported as unavailable, not raised.
        try:
            status = erpnext_accounts.bootstrap(erpnext_accounts.get_client())
            msg += _doctype_status_line(status)
        except (ERPNextConfigError, ERPNextError) as e:
            msg += f' · (couldn’t provision linked doctypes: {e})'
    return _erpnext_settings_page(probe={'ok': ok, 'detail': msg})


@bp.post('/admin/erpnext_settings/ensure_fields')
def ensure_erpnext_fields():
    """Idempotently provision the Bank Account Type records (Current/Credit), the
    Bank Account Subtype link targets, and the custom fields (plaid_account_id,
    last_4) the import flow depends on."""
    if not erps.is_configured():
        return _erpnext_settings_page(
            probe={'ok': False,
                   'detail': 'Not configured — set URL + API key + secret first.'})
    try:
        status = erpnext_accounts.bootstrap(erpnext_accounts.get_client())
    except (ERPNextConfigError, ERPNextError) as e:
        return _erpnext_settings_page(probe={'ok': False, 'detail': str(e)})
    detail = ('Provisioned the Bank Account Type records (Current, Credit), '
              'Bank Account Subtype records, and custom fields (plaid_account_id, '
              'last_4) where this ERPNext supports them.'
              + _doctype_status_line(status))
    return _erpnext_settings_page(probe={'ok': not status['partial'],
                                         'detail': detail})


@bp.post('/admin/erpnext_settings/verify_doctype')
def verify_erpnext_doctype():
    if not erps.is_configured():
        return _erpnext_settings_page(
            probe={'ok': False,
                   'detail': 'Not configured — set URL + API key + secret first.'})
    ok, detail = erpnext_bank.verify_doctype()
    return _erpnext_settings_page(probe={'ok': ok, 'detail': detail})


# ── Sync log ─────────────────────────────────────────────────────

SYNC_LOG_BODY = """
<h2>Sync log</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
<form method="get" action="/admin/sync_log" class="card"
      style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
  <label style="margin:0">Direction
    <select name="direction">
      <option value="">(any)</option>
      <option value="plaid_pull" {{ 'selected' if cur_direction=='plaid_pull' else '' }}>plaid_pull</option>
      <option value="erpnext_push" {{ 'selected' if cur_direction=='erpnext_push' else '' }}>erpnext_push</option>
    </select>
  </label>
  <label style="margin:0">Status
    <select name="status">
      <option value="">(any)</option>
      <option value="success" {{ 'selected' if cur_status=='success' else '' }}>success</option>
      <option value="failed" {{ 'selected' if cur_status=='failed' else '' }}>failed</option>
    </select>
  </label>
  <button type="submit" class="primary">Filter</button>
  <form method="post" action="/admin/sync_now" style="margin:0">
    <button type="submit" class="secondary">Sync Now</button>
  </form>
</form>

<table>
  <tr><th>At</th><th>Item</th><th>Direction</th><th class="num">Count</th><th>Status</th><th>Detail</th></tr>
  {% for r in rows %}
  <tr>
    <td style="font-size:12px">{{ r.at.strftime('%Y-%m-%d %H:%M:%S') if r.at else '' }}</td>
    <td><code style="font-size:12px">{{ (r.item_id or '')[:14] }}</code></td>
    <td>{{ r.direction }}</td>
    <td class="num">{{ r.count }}</td>
    <td>{% if r.status=='success' %}<span class="pill pill-ok">success</span>{% else %}<span class="pill pill-err">{{ r.status }}</span>{% endif %}</td>
    <td style="font-size:12px;max-width:340px">{{ (r.error_message or '')[:200] }}</td>
  </tr>
  {% endfor %}
  {% if not rows %}<tr><td colspan="6" style="color:#888">No sync activity yet.</td></tr>{% endif %}
</table>
"""


@bp.get('/admin/sync_log')
def sync_log_page():
    cur_direction = (request.args.get('direction') or '').strip()
    cur_status = (request.args.get('status') or '').strip()
    q = PlaidSyncLog.query
    if cur_direction:
        q = q.filter(PlaidSyncLog.direction == cur_direction)
    if cur_status:
        q = q.filter(PlaidSyncLog.status == cur_status)
    rows = q.order_by(PlaidSyncLog.at.desc()).limit(200).all()
    return _page(SYNC_LOG_BODY, page='sync_log', rows=rows,
                 cur_direction=cur_direction, cur_status=cur_status,
                 flash_msg=request.args.get('flash', ''))


# ── v0.4.9 · bank statements + reconciliation ────────────────────
#
# The one screen that answers "do my books agree with my bank?" without opening
# ERPNext. Each row is a statement the institution issued; the verdict beside it
# is that statement's own opening balance plus the movement Bank Bridge mirrored
# in the period, measured against the bank's closing balance. A delta is a gap in
# the mirror, in dollars, for a named month.


STATEMENTS_BODY = """
<h2>Bank statements</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
{% if not enabled %}
<div class="banner-warn">
  <h3>Statements are turned off</h3>
  <p style="font-size:14px;margin:0">
    <code>STATEMENTS_ENABLED</code> is false, so no statements are pulled and
    opening balances fall back to the v0.4.4 estimate.
  </p>
</div>
{% endif %}
<p style="font-size:14px;color:#555">
  Statement PDFs pulled from Plaid, with each period reconciled against the
  transactions Bank Bridge mirrored. <b>Expected</b> is the period's opening
  balance plus that movement; when it doesn't land on the <b>closing</b>
  balance, the difference is transactions the mirror is missing for that
  month.
  <br><span style="color:#888;font-size:12px">
  Balances tagged <b>bank</b> came from the statement PDF; balances tagged
  <b>computed</b> were derived from the transaction mirror when the PDF was
  unreadable. A row where both sides are computed reconciles trivially
  (mirror vs mirror) — it's a balance report, not a bank cross-check.
  </span>
</p>
{% if not rows %}
<div class="banner-warn">
  <h3>No statements yet</h3>
  <p style="font-size:14px;margin:0">
    Statements need the <code>statements</code> product enabled on your Plaid
    application, requested when the bank was linked, and supported by the
    institution. If you enabled it after linking, re-link the bank — or run
    <code>python -m scripts.backfill_statements</code> to pull statements for
    accounts already linked.
  </p>
</div>
{% endif %}
{% for group in rows %}
<h3 style="margin-bottom:4px">{{ group.account_label }}</h3>
<div style="font-size:13px;color:#666">
  {{ group.company or 'no Company resolved' }}
  {% if group.mismatches %}
  &middot; <span class="warn">&#9888; {{ group.mismatches }} period(s) don't
  reconcile</span>
  {% elif group.reconciled %}
  &middot; <span style="color:#1b5e20">&#10003; {{ group.reconciled }}
  period(s) reconcile</span>
  {% endif %}
  {% if group.computed %}
  &middot; <span style="color:#666">{{ group.computed }} from mirror</span>
  {% endif %}
  {% if group.suspect %}
  &middot; <span class="warn">&#9888; {{ group.suspect }} period(s) don't chain
  on from the month before</span>
  {% endif %}
  {% if group.clearing_imbalance and group.clearing_imbalance|abs > 0.005 %}
  <br><span class="warn" style="font-size:12px">&#9888; Cash Clearing off by
    {{ '%+.2f'|format(group.clearing_imbalance) }} — a trade's security and
    companion cash legs don't match; the clearing account won't net to zero
    until it's resolved.</span>
  {% endif %}
  {% if group.anchor_periods %}
  <br><span style="font-size:12px">
    Statement-anchored:
    <span style="color:#1b5e20">&#10003; {{ group.anchor_reconciled }}
      reconciled</span>{% if group.anchor_unreconciled %} ·
    <span class="warn">&#9888; {{ group.anchor_unreconciled }} need
      attention</span>{% endif %}
    of {{ group.anchor_periods }}
    &middot; <a href="/admin/reconciliation/{{ group.statements[0].statement.plaid_account_id
        if group.statements else '' }}" style="font-size:11px">detail &rarr;</a>
  </span>
  {% endif %}
</div>
<table>
  <tr><th>Period</th><th class="num">Opening</th><th class="num">Movement</th>
      <th class="num">Expected</th><th class="num">Closing</th>
      {% if group.has_portfolio %}<th class="num">Portfolio value</th>{% endif %}
      <th class="num">Delta</th><th>Status</th><th>PDF</th></tr>
  {% for r in group.statements %}
  <tr>
    <td><a href="/admin/statements/{{ r.statement.id }}">{{ r.row.period_label
        or '—' }}</a>
      {% if r.row.parse_suspect %}
      <br><span class="pill pill-err" style="font-size:10px"
            title="This statement's opening balance does not equal the previous month's closing balance — either the PDF was misread or a statement is missing. Check the PDF before relying on these figures.">&#9888; chain</span>
      {% endif %}
    </td>
    <td class="num">
      {% if r.opening is not none %}{{ '%.2f'|format(r.opening) }}
        {% if r.opening_source == 'bank' %}
          <span class="pill pill-ok" style="font-size:10px"
                title="Opening balance parsed from the statement PDF">bank</span>
        {% elif r.opening_source == 'computed' %}
          <span class="pill pill-muted" style="font-size:10px"
                title="Opening balance derived from the transaction mirror (PDF was unreadable)">computed</span>
        {% endif %}
      {% else %}—{% endif %}
    </td>
    <td class="num">{{ '%.2f'|format(r.movement) }}</td>
    <td class="num">{{ '%.2f'|format(r.expected_closing)
                       if r.expected_closing is not none else '—' }}</td>
    <td class="num">
      {% if r.closing is not none %}{{ '%.2f'|format(r.closing) }}
        {% if r.closing_source == 'bank' %}
          <span class="pill pill-ok" style="font-size:10px"
                title="Closing balance parsed from the statement PDF">bank</span>
        {% elif r.closing_source == 'computed' %}
          <span class="pill pill-muted" style="font-size:10px"
                title="Closing balance derived from the transaction mirror (PDF was unreadable)">computed</span>
        {% endif %}
      {% else %}—{% endif %}
    </td>
    {% if group.has_portfolio %}
    <td class="num">
      {# Total account value — cash PLUS securities at market. It is not what
         the Opening/Closing columns reconcile (those are the cash side, the
         only figure the transaction mirror can reproduce), so it is shown
         apart from them rather than folded in. #}
      {% if r.row.portfolio_closing_value is not none %}
        {{ '%.2f'|format(r.row.portfolio_closing_value) }}
        <br><span style="font-size:11px;color:#666"
              title="Total account value at the start of the period">from
          {{ '%.2f'|format(r.row.portfolio_opening_value)
             if r.row.portfolio_opening_value is not none else '—' }}</span>
      {% else %}—{% endif %}
    </td>
    {% endif %}
    <td class="num">
      {% if r.delta is not none %}
        {{ '%+.2f'|format(r.delta) }}
        {% if r.adjustment_total %}
        <br><span style="font-size:11px;color:#666">
          adj: {{ '%+.2f'|format(r.adjustment_total) }}<br>
          → {{ '%+.2f'|format(r.adjusted_delta) }}
        </span>
        {% endif %}
      {% else %}—{% endif %}
    </td>
    <td>
      {% if r.status == 'ok' %}
        <span class="pill pill-ok">reconciled</span>
      {% elif r.status == 'reconciled' %}
        <span class="pill pill-ok" title="Delta explained by manual adjustments">reconciled (adj)</span>
      {% elif r.status == 'mismatch' %}
        <span class="pill pill-err" title="Off by {{ '%.2f'|format(r.adjusted_delta if r.adjusted_delta is not none else r.delta) }}">
          &#9888; off by {{ '%.2f'|format((r.adjusted_delta if r.adjusted_delta is not none else r.delta)|abs) }}</span>
        <br><a href="/admin/statements/{{ r.statement.id }}/adjust"
              style="font-size:11px">attribute →</a>
      {% elif r.status == 'computed' %}
        <span class="pill pill-muted"
              title="Both opening and closing were derived from the transaction mirror — this is a balance report, not a bank cross-check">from mirror</span>
      {% else %}
        <span class="pill pill-muted"
              title="Balances could not be read from this PDF and the mirror has no signal for this period">no balances</span>
      {% endif %}
    </td>
    <td>
      {% if r.has_pdf %}
      <a href="/admin/statements/{{ r.row.statement_id|urlencode }}/view"
         target="_blank">view</a>
      {% else %}<span style="color:#999">not stored</span>{% endif %}
    </td>
  </tr>
  {% endfor %}
</table>
{% endfor %}
<form method="post" action="/admin/statements/pull" class="card">
  <b>Pull statements now</b>
  <p style="font-size:13px;color:#666;margin:6px 0">
    Checks every connected bank for statements not already stored. Runs
    automatically every {{ interval_days or '—' }} days.
  </p>
  <button type="submit">Pull statements</button>
</form>
<form method="post" action="/admin/statements/reparse"
      class="card" {% if stale %}style="border-color:#f5a623;background:#fff8e1"{% endif %}>
  <b>Re-parse stored PDFs</b>
  {% if stale %}
  <p class="warn" style="font-size:13px;margin:6px 0">
    &#9888; {{ stale }} statement(s) still show figures from an older parser.
    The scheduled statement job re-reads them automatically; press this to do
    it now.
  </p>
  {% endif %}
  <p style="font-size:13px;color:#666;margin:6px 0">
    Re-reads the balances out of every PDF already on disk, without downloading
    anything. Run this after an upgrade that improves the parser — a pull skips
    statements it already holds, so a better reader would otherwise never reach
    them. Only the parsed figures change; the documents are untouched, and any
    journal entry already posted stays exactly as posted.
  </p>
  <button type="submit">Re-parse stored PDFs</button>
</form>
<form method="post" action="/admin/statements/sync_erpnext" class="card">
  <b>Send statements to ERPNext</b>
  <p style="font-size:13px;color:#666;margin:6px 0">
    Creates a <code>Bank Statement</code> record in ERPNext for every statement
    held here, with the PDF attached, so the bookkeeper can read them without
    logging in to Bank Bridge. Runs automatically at startup and after each
    pull; idempotent, so running it again uploads nothing twice.
  </p>
  <button type="submit">Sync to ERPNext</button>
</form>
<p style="font-size:14px">
  Reports: <a href="/admin/statements/reports/discrepancies">discrepancies</a>
  · <a href="/admin/statements/reports/coverage">coverage gaps</a>
  · <a href="/admin/balance_history">balance history</a>
</p>
"""


BALANCE_HISTORY_BODY = """
<h2>Balance history</h2>
<p style="font-size:14px;color:#555">
  Month-end balances for every mapped account, computed from the transaction
  mirror. This is what your books say each account held at the close of each
  calendar month &mdash; the same arithmetic that fills opening/closing
  balances on <a href="/admin/statements">/admin/statements</a> when a bank
  PDF is unreadable, applied to every month whether a statement exists or not.
  Useful for balance-sheet freezes, year-over-year, and spot-checking a bank
  statement against a computed peer.
</p>
<p style="font-size:13px;color:#888">
  Months entirely before an account's earliest mirrored transaction are
  omitted &mdash; the pre-mirror balance is arithmetic without evidence and
  reporting it would misrepresent what this app actually knows.
</p>
<form method="get" style="margin:8px 0">
  <label style="font-size:13px">Show
  <select name="months" onchange="this.form.submit()">
    {% for opt in [3, 6, 12, 24, 36] %}
    <option value="{{ opt }}" {% if opt == months %}selected{% endif %}>
      last {{ opt }} months
    </option>
    {% endfor %}
  </select></label>
</form>
{% if not groups %}
<div class="card">
  <h3>No history to compute</h3>
  <p style="font-size:14px;margin:0">
    No mapped account has any mirrored transactions yet. Once transactions
    start flowing this page will fill in.
  </p>
</div>
{% endif %}
{% for group in groups %}
<h3 style="margin-bottom:4px">{{ group.label }}</h3>
<div style="font-size:13px;color:#666">
  {{ group.company or 'no Company resolved' }}
  {% if group.balance_current is not none %}
    &middot; current balance {{ '%.2f'|format(group.balance_current) }}
    {{ group.currency }}
  {% endif %}
</div>
<table>
  <tr><th>Month</th><th class="num">Closing balance</th></tr>
  {% for row in group.rows %}
  <tr>
    <td>
      {{ row.month_end.strftime('%Y-%m') }}{% if row.partial %}
      <span style="color:#888;font-size:12px"
            title="Partial month — this row is the balance AS OF {{ row.month_end.strftime('%Y-%m-%d') }}, not the eventual month-end close. The month is still in progress.">(to {{ row.month_end.strftime('%b %d') }})</span>
      {% endif %}
    </td>
    <td class="num">{{ '%.2f'|format(row.balance) }}{% if row.partial %}
      <span style="color:#888;font-size:11px">so far</span>
    {% endif %}</td>
  </tr>
  {% endfor %}
</table>
{% endfor %}
<p style="font-size:14px"><a href="/admin/statements">← back to statements</a></p>
"""


def _clearing_imbalance(account_id: str) -> float:
    """Best-effort Cash Clearing imbalance for a paired brokerage; 0.0 on any
    error (a display convenience must never 500 the statements page)."""
    try:
        from .. import invest_je
        return invest_je.clearing_imbalance(account_id)
    except Exception:  # pragma: no cover
        return 0.0


def _statement_groups() -> list:
    """One group per Plaid account that has statements, each carrying its
    reconciled rows. Accounts with no statements are omitted — an empty section
    per unlinked account would bury the ones that have something to say."""
    from .. import statements as stmts
    companies = _resolve_account_companies()
    scope = _current_company()
    groups = []
    for account in av.visible_accounts(
            PlaidAccount.query.order_by(PlaidAccount.name)):
        company = companies.get(account.account_id, '')
        if scope and company != scope:
            continue
        rows = stmts.reconcile(account)
        if not rows:
            continue
        for r in rows:
            r['has_pdf'] = stmts.pdf_exists(r['statement'])
        label = (account.name or account.official_name
                 or account.mask or account.account_id)
        if account.mask:
            label = f'{label} ••{account.mask}'
        # v0.5.0 · per-account anchor reconciled/unreconciled count — the
        # at-a-glance "which accounts still need attention", and the same
        # figures Bank Bridge writes to ERPNext.
        anchor = stmts.anchor_summary(account.account_id)
        groups.append({
            'account_label': label, 'company': company, 'statements': rows,
            'anchor_periods': anchor['periods'],
            'anchor_unreconciled': anchor['unexplained'],
            'anchor_reconciled': anchor['periods'] - anchor['unexplained'],
            # v0.5.1 · Cash Clearing imbalance for a paired brokerage — non-zero
            # means a trade's SecurityTransaction and its companion
            # BankTransaction don't match, which would leave the clearing
            # account off zero. 0.0 for an unpaired account.
            'clearing_imbalance': _clearing_imbalance(account.account_id),
            'mismatches': sum(1 for r in rows if r['status'] == 'mismatch'),
            'reconciled': sum(1 for r in rows if r['status'] == 'ok'),
            # v0.4.20: computed rows aren't bank cross-checks; count separately
            # so the header line can say "3 bank-verified, 5 from mirror" and
            # not conflate the two into one flattering total.
            'computed': sum(1 for r in rows if r['status'] == 'computed'),
            # v0.4.41: only a brokerage statement states a total account value,
            # so the column is shown per group rather than site-wide — an empty
            # "Portfolio" column on every checking account teaches nothing.
            'has_portfolio': any(
                r['row'].get('portfolio_closing_value') is not None
                for r in rows),
            'suspect': sum(1 for r in rows if r['row'].get('parse_suspect')),
        })
    return groups


@bp.get('/admin/statements')
def statements_page():
    """Statement PDFs + per-period reconciliation, grouped by account."""
    from .. import statements as stmts
    return _page(STATEMENTS_BODY, page='statements',
                 rows=_statement_groups(), enabled=stmts.is_enabled(),
                 interval_days=stmts.pull_interval_days(),
                 stale=len(stmts.stale_statements()),
                 flash_msg=request.args.get('flash', ''))


@bp.get('/admin/statements/<path:statement_id>/view')
def statement_pdf(statement_id: str):
    """Serve one statement's PDF inline.

    Looks the statement up BY ID and serves only the path that row stored, then
    re-checks that the path really sits under the statement root (see
    statements.resolve_pdf_path). A file-serving route reached from a URL
    segment is exactly where a traversal would be attempted, and the stored path
    round-trips through the database — so neither the URL nor the column is
    trusted on its own."""
    from .. import statements as stmts
    row = PlaidStatement.query.filter_by(statement_id=statement_id).first()
    if row is None:
        return Response('No such statement.', 404, {'Content-Type': 'text/plain'})
    path = stmts.resolve_pdf_path(row)
    if path is None:
        return Response(
            'The PDF for that statement is not on disk. Pull statements again '
            'to re-download it.', 404, {'Content-Type': 'text/plain'})
    with open(path, 'rb') as fh:
        data = fh.read()
    filename = f'statement-{row.period_label() or row.statement_id}.pdf'
    return Response(data, 200, {
        'Content-Type': 'application/pdf',
        'Content-Disposition': f'inline; filename="{filename}"',
        'Content-Length': str(len(data)),
        # These PDFs are the operator's own bank records; keep them out of any
        # shared cache sitting between the browser and the app.
        'Cache-Control': 'private, no-store',
        'X-Content-Type-Options': 'nosniff',
    })


@bp.post('/admin/statements/pull')
def statements_pull():
    """Operator-triggered statement pull — the same work the monthly job does."""
    from .. import statements as stmts
    if not stmts.is_enabled():
        return redirect('/admin/statements?flash=' + quote_plus(
            'Statements are disabled (STATEMENTS_ENABLED).'))
    try:
        result = stmts.fetch_all()
    except Exception as e:
        return redirect('/admin/statements?flash=' + quote_plus(
            f'Statement pull failed: {e}'))
    msg = (f"Listed {result['listed']}, stored {result['stored']}, "
           f"already had {result['skipped_existing']}.")
    if result['failed']:
        msg += f" {result['failed']} could not be downloaded."
    return redirect('/admin/statements?flash=' + quote_plus(msg))


@bp.post('/admin/statements/reparse')
def statements_reparse():
    """Re-read balances from PDFs already on disk (v0.4.41).

    Separate from the pull because a pull deliberately skips any statement it
    already holds — so shipping a better parser reaches nothing already stored
    until an operator asks for this. Rewrites only the parsed columns; the PDFs
    and every posted journal entry are untouched."""
    from .. import statements as stmts
    try:
        result = stmts.reparse_stored()
    except Exception as e:
        db.session.rollback()
        return redirect('/admin/statements?flash=' + quote_plus(
            f'Re-parse failed: {e}'))
    msg = (f"Re-parsed {result['examined']} PDF(s); "
           f"{result['changed']} row(s) changed; "
           f"{result['fields']} figure(s) extracted.")
    if result['failed_fields']:
        msg += f" {result['failed_fields']} field(s) failed to extract."
    if result['suspect']:
        msg += (f" {result['suspect']} statement(s) don't chain on from the "
                "month before — check those PDFs.")
    if result['unreadable']:
        msg += f" {result['unreadable']} had no PDF on disk."
    audit.record('statements_reparsed', after=result, notes=msg)
    db.session.commit()
    return redirect('/admin/statements?flash=' + quote_plus(msg))


# ── v0.4.10 · statements inside ERPNext ──────────────────────────
#
# Two reports over the Bank Statement records this release uploads. Both read
# ERPNext first and fall back to Bank Bridge's own rows when it cannot be
# reached — and say which they used, because an operator reading a report during
# an ERPNext outage deserves to know whether they are looking at ERPNext.

SOURCE_NOTE = """
<p style="font-size:13px;color:#555">
  {% if data_source == 'erpnext' %}
  Read live from ERPNext's <code>Bank Statement</code> records.
  {% else %}
  <b>ERPNext could not be reached</b>, so this is computed from Bank Bridge's
  own statement rows. Every number is the same one ERPNext would show — Bank
  Bridge is the source of truth for all of them — but records may not be
  uploaded yet.
  {% endif %}
</p>
"""

DISCREPANCIES_BODY = """
<h2>Statement discrepancies</h2>
""" + SOURCE_NOTE + """
<p style="font-size:14px;color:#555">
  Statements whose closing balance disagrees with the mirrored transactions by
  more than <b>{{ '%.2f'|format(threshold) }}</b>. A discrepancy means the
  transaction mirror has a gap for that period — not that the bank is wrong.
  Statements whose PDF could not be parsed are absent: an unreadable statement
  says nothing about whether the books agree.
</p>
<p style="font-size:13px;color:#555">
  Raise or lower the bar with <code>ERPNEXT_STATEMENT_VARIANCE_THRESHOLD</code>.
</p>
{% if not rows %}
<div class="card">
  <h3>No discrepancies</h3>
  <p style="font-size:14px;margin:0">
    Every statement that could be reconciled lands within
    {{ '%.2f'|format(threshold) }} of the mirror.
  </p>
</div>
{% else %}
<table>
  <tr><th>Bank Account</th><th>Period</th><th class="num">Opening</th>
      <th class="num">Closing</th><th class="num">Variance</th>
      <th>Status</th></tr>
  {% for r in rows %}
  <tr>
    <td>{{ r.bank_account or '—' }}</td>
    <td>{{ r.period_start or '?' }} → {{ r.period_end or '?' }}</td>
    <td class="num">{{ '%.2f'|format(r.opening_balance) }}</td>
    <td class="num">{{ '%.2f'|format(r.closing_balance) }}</td>
    <td class="num"><b>{{ '%+.2f'|format(r.variance) }}</b></td>
    <td>{{ r.status }}</td>
  </tr>
  {% endfor %}
</table>
<p style="font-size:13px;color:#555">
  Variance is <i>expected closing minus the bank's closing</i>: positive means
  the mirror shows more money than the bank did, negative means less.
</p>
{% endif %}
<p style="font-size:14px"><a href="/admin/statements">← back to statements</a>
  · <a href="/admin/statements/reports/coverage">coverage report</a></p>
"""

COVERAGE_BODY = """
<h2>Statement coverage</h2>
""" + SOURCE_NOTE + """
<p style="font-size:14px;color:#555">
  Months with no statement, over the last {{ months|length }} closed months. A
  gap here is the failure statements exist to catch: an unparseable statement is
  visible on <a href="/admin/statements">/admin/statements</a>, but one that was
  never fetched at all is visible nowhere — and it is the one that leaves a
  quarter unreconciled at tax time.
</p>
<p style="font-size:13px;color:#555">
  Months before an account's first statement are not counted: an account linked
  in May cannot be missing January. The month in progress is excluded too — its
  statement has not been issued yet.
</p>
{% if not rows %}
<div class="card">
  <h3>No statements yet</h3>
  <p style="font-size:14px;margin:0">
    Nothing to measure coverage over. Pull statements on
    <a href="/admin/statements">/admin/statements</a> first.
  </p>
</div>
{% else %}
<table>
  <tr><th>Account</th><th class="num">Held</th><th class="num">Expected</th>
      <th class="num">Gaps</th><th>Missing months</th></tr>
  {% for r in rows %}
  <tr>
    <td>{{ r.label }}</td>
    <td class="num">{{ r.months_held }}</td>
    <td class="num">{{ r.months_expected }}</td>
    <td class="num">{% if r.gap_count %}<b>{{ r.gap_count }}</b>{% else %}0{% endif %}</td>
    <td>{{ r.missing|join(', ') or '— complete —' }}</td>
  </tr>
  {% endfor %}
</table>
{% endif %}
<p style="font-size:14px"><a href="/admin/statements">← back to statements</a>
  · <a href="/admin/statements/reports/discrepancies">discrepancy report</a></p>
"""


def _statement_report_client():
    """The ERPNext client for a statement report, or None. Both reports degrade
    to Bank Bridge's local rows rather than 500-ing when ERPNext is
    unconfigured or the doctype was never provisioned."""
    return sync_engine.get_erp_client_or_none()


@bp.get('/admin/balance_history')
def balance_history():
    """Month-end balances per account, computed from the transaction mirror
    (v0.4.21). See `computed_balances.monthly_closing_balances` for the
    exact arithmetic; this route is the presentation layer over it,
    filtered to mapped accounts under the current Company scope and
    ordered by account name so the balance-sheet columns are stable."""
    from .. import computed_balances as cb
    # 3-36 months, defaulting to 12; anything else is silently clamped so a
    # hand-edited URL can't ask for a five-year window on a large database.
    try:
        months = int(request.args.get('months', 12) or 12)
    except ValueError:
        months = 12
    months = max(3, min(months, 36))
    companies = _resolve_account_companies()
    scope = _current_company()
    groups = []
    for account in av.visible_accounts(
            PlaidAccount.query.order_by(PlaidAccount.name)):
        # Unmapped accounts have no ERPNext ledger to reconcile against, so
        # a balance history for them is a report about nothing — omit rather
        # than dilute the page.
        if not account.erpnext_bank_account_name:
            continue
        company = companies.get(account.account_id, '')
        if scope and company != scope:
            continue
        rows = cb.monthly_closing_balances(account, months=months)
        if not rows:
            continue
        label = (account.name or account.official_name
                 or account.mask or account.account_id)
        if account.mask:
            label = f'{label} ••{account.mask}'
        groups.append({
            'label': label, 'company': company, 'rows': rows,
            'balance_current': account.balance_current,
            'currency': account.iso_currency_code or account.currency or 'USD',
        })
    return _page(BALANCE_HISTORY_BODY, page='statements', groups=groups,
                 months=months)


RECONCILIATION_BODY = """
<h2>Statement-anchored reconciliation</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
<p style="font-size:14px;color:#555;max-width:1000px">
  What each account <b>actually held</b> at every statement boundary, according
  to the bank's own PDF — held here, in Bank Bridge, independent of ERPNext.
  Each row is one identity and the two ways it can fail:
  <br><code>anchored opening + transactions = computed closing</code>, and
  <code>anchored closing − computed closing = variance</code>.
  <br><b>Variance</b> is money the <i>bank</i> saw and <i>Plaid</i> did not — an
  off-platform wire, a tax payment, a transfer between institutions. It is a
  finding, not an error. A <b>chain gap</b> is the other failure: this period's
  opening doesn't meet the previous period's closing, so a statement is missing
  between them and every variance after it is measured from the wrong baseline.
</p>
<p style="font-size:13px;color:#666">
  Nothing on this page is posted to ERPNext. It is the durable record these
  accounts need until their own instance exists.
</p>

<form method="get" action="" style="margin:12px 0">
  <label style="font-size:14px">Account
    <select name="account_id" onchange="this.form.submit()"
            style="padding:6px;font-size:14px;margin-left:6px">
      {% for a in accounts %}
      <option value="{{ a.account_id }}"
        {{ 'selected' if a.account_id == account.account_id else '' }}>
        {{ labels.get(a.account_id, a.name or a.account_id) }}
      </option>
      {% endfor %}
    </select>
  </label>
</form>

{% if not anchors %}
<div class="banner-warn">
  <h3>No anchors for this account yet</h3>
  Anchors are built from statements that have been parsed. Pull and re-parse
  statements on <a href="/admin/statements">/admin/statements</a>, then
  rebuild below.
</div>
{% else %}
<div class="kpis" style="margin:12px 0">
  <div class="kpi"><b>{{ summary.periods }}</b><br>
    <span style="font-size:12px;color:#666">periods anchored</span></div>
  <div class="kpi">
    <b style="color:{{ '#b71c1c' if summary.variance|abs > 0.005 else '#1b5e20' }}">
      {{ '%+.2f'|format(summary.variance) }}</b><br>
    <span style="font-size:12px;color:#666">total variance</span></div>
  <div class="kpi">
    <b style="color:{{ '#b71c1c' if summary.unexplained else '#1b5e20' }}">
      {{ summary.unexplained }}</b><br>
    <span style="font-size:12px;color:#666">periods unexplained</span></div>
  <div class="kpi">
    <b style="color:{{ '#b71c1c' if summary.gaps else '#1b5e20' }}">
      {{ summary.gaps }}</b><br>
    <span style="font-size:12px;color:#666">chain gaps</span></div>
</div>

<table>
  <tr><th>Period</th><th class="num">Anchored opening</th>
      <th class="num">Transactions</th><th class="num">Computed closing</th>
      <th class="num">Anchored closing</th><th class="num">Variance</th>
      <th>Reason</th><th>Parser</th></tr>
  {% for a in anchors %}
  <tr {% if a.chain_gap_from_prior %}style="border-top:3px solid #f5a623"{% endif %}>
    <td>
      {{ a.period_start or '—' }}
      {% if a.chain_gap_from_prior %}
      <br><span class="pill pill-err" style="font-size:10px"
            title="This period's opening balance does not meet the previous period's closing balance — a statement is missing between them.">&#9888; chain gap</span>
      {% endif %}
    </td>
    <td class="num">{{ '%.2f'|format(a.anchored_opening)
                       if a.anchored_opening is not none else '—' }}</td>
    <td class="num">{{ '%+.2f'|format(a.transaction_sum) }}</td>
    <td class="num">{{ '%.2f'|format(a.computed_closing)
                       if a.computed_closing is not none else '—' }}</td>
    <td class="num">{{ '%.2f'|format(a.anchored_closing)
                       if a.anchored_closing is not none else '—' }}</td>
    <td class="num">
      {% if a.variance is none %}—
      {% elif a.reconciles() %}
        <span style="color:#1b5e20">0.00</span>
      {% else %}
        <span style="color:#b71c1c;font-weight:600">{{ '%+.2f'|format(a.variance) }}</span>
      {% endif %}
    </td>
    <td style="font-size:12px">
      {# v0.4.49 · Reason auto-populates from the internal tags carried by this
         period's transactions (CategorizationRule.bb_internal_tag). A manual
         variance_reason, when set, still wins. #}
      {% if a.variance_reason %}{{ a.variance_reason }}
      {% elif tag_reasons.get(a.id) %}{{ tag_reasons[a.id] }}
      {% elif a.reconciles() %}—
      {% else %}untagged{% endif %}
    </td>
    <td style="font-size:11px;color:#666">{{ a.parser_version or '—' }}</td>
  </tr>
  {% endfor %}
  <tr style="background:#f4f4f4;font-weight:600">
    <td>Total ({{ summary.periods }} periods)</td>
    <td class="num">—</td>
    <td class="num">{{ '%+.2f'|format(total_transactions) }}</td>
    <td class="num">—</td><td class="num">—</td>
    <td class="num" style="color:{{ '#b71c1c' if summary.variance|abs > 0.005 else '#1b5e20' }}">
      {{ '%+.2f'|format(summary.variance) }}</td>
    <td colspan="2"></td>
  </tr>
</table>
{% endif %}

<div style="display:flex;gap:10px;align-items:center;margin:16px 0">
  <a href="/admin/reconciliation/{{ account.account_id }}/csv"
     style="padding:8px 16px;background:#2e9e5b;color:#fff;border-radius:4px;
            text-decoration:none;font-weight:600;font-size:14px">Download CSV</a>
  <form method="post" action="/admin/statements/rebuild_anchors" style="margin:0">
    <input type="hidden" name="account_id" value="{{ account.account_id }}">
    <button type="submit" class="secondary">Rebuild this account's anchors</button>
  </form>
  <form method="post" action="/admin/statements/rebuild_anchors" style="margin:0">
    <button type="submit" class="secondary">Rebuild all accounts</button>
  </form>
</div>
<p style="font-size:13px"><a href="/admin/statements">&larr; statements</a></p>
"""


STATEMENT_DETAIL_BODY = """
<h2>Statement {{ statement.period_label() or '—' }} · {{ account_label }}</h2>

<div class="card" style="max-width:1100px">
  <p style="font-size:14px;color:#555;margin:0">
    <b>Period (from Plaid):</b> {{ statement.period_start or '—' }} to
    {{ statement.period_end or '—' }}
    {% if v.stated_period[0] %}
      · <b>Period (printed on the statement):</b> {{ v.stated_period[0] }} to
      {{ v.stated_period[1] }}
      {% if v.period_matches is false %}
      <span class="pill pill-err" title="The bank's cycle is not the calendar month Plaid reported. Every reconciliation for this period compares transactions from the wrong window.">&#9888; periods disagree</span>
      {% endif %}
    {% endif %}
    {% if v.advisory_program or v.advisory_fee_rate %}
    <br><b>Advisory program:</b> {{ v.advisory_program or '—' }}
    {% if v.advisory_fee_rate %}
    · <b>Effective fee rate:</b> {{ v.advisory_fee_rate }}
    {% endif %}
    <span class="pill pill-muted" title="This account is managed — the statement names an advisory program and a disclosed fee rate. A self-directed brokerage prints neither.">managed</span>
    {% endif %}
    <br><b>Read by:</b>
    <code>{{ v.layout or 'no recognizer — nothing was parsed' }}</code>
    (parser {{ statement.parser_version() or 'pre-0.4.41' }})
    {% if v.layout and not v.verified %}
    <span class="pill pill-muted"
          title="This layout's field table has not been checked against a real document of this kind. Figures from it are unconfirmed.">unverified layout</span>
    {% endif %}
    {% if has_pdf %}
    · <a href="/admin/statements/{{ statement.statement_id|urlencode }}/view"
       target="_blank">open the PDF &rarr;</a>
    {% endif %}
  </p>
  {% if statement.parse_suspect %}
  <p class="warn" style="font-size:13px;margin:10px 0 0">
    &#9888; This statement's opening balance does not equal the previous
    month's closing balance. Either a figure was misread or a statement is
    missing — check the PDF before citing these numbers.
  </p>
  {% endif %}
  {% if v.fields_failed %}
  <p class="warn" style="font-size:13px;margin:10px 0 0">
    &#9888; {{ v.fields_failed|length }} field(s) failed to extract:
    <code>{{ v.fields_failed|join(', ') }}</code>. Everything else on this page
    parsed normally.
  </p>
  {% endif %}
</div>

<h3>Validation</h3>
<p style="font-size:13px;color:#555;max-width:1100px">
  Three independent accounts of the same month. <b>Statement</b> is what the
  bank wrote down. <b>Plaid</b> is what the API reports live — a snapshot of
  today, so it is only compared on the newest statement. <b>Mirror</b> is what
  Bank Bridge computes from the transactions it stored. Two agreeing is
  ordinary; two <i>disagreeing</i> is the finding, and each cause has a
  different fix: a misread PDF, a gap in the transaction feed, or something
  that moved the real balance and never reached Plaid.
  Flagged when a difference exceeds <b>${{ '%.2f'|format(v.thresholds[0]) }}</b>
  <i>and</i> <b>{{ '%.2f'|format(v.thresholds[1] * 100) }}%</b>.
</p>
{% if v.flagged %}
<p class="warn" style="font-size:14px">&#9888; {{ v.flagged }} figure(s) differ
  by more than the threshold.</p>
{% else %}
<p style="font-size:14px;color:#1b5e20">&#10003; Nothing on this statement
  differs from the mirror by more than the threshold.</p>
{% endif %}
<table style="max-width:1100px">
  <tr><th>Figure</th><th class="num">Statement</th><th class="num">Plaid</th>
      <th class="num">Mirror</th><th class="num">Delta</th>
      <th class="num">Variance</th></tr>
  {% for r in v.rows %}
  <tr {% if r.flagged %}style="background:#fdf0e6"{% endif %}>
    <td>{{ r.label }}
      {% if r.note %}<br><span style="font-size:11px;color:#666">{{ r.note }}</span>{% endif %}
    </td>
    <td class="num">{{ '%.2f'|format(r.statement)
                       if r.statement is not none else '—' }}</td>
    <td class="num">{{ '%.2f'|format(r.plaid)
                       if r.plaid is not none else '—' }}</td>
    <td class="num">{{ '%.2f'|format(r.computed)
                       if r.computed is not none else '—' }}</td>
    <td class="num">{{ '%+.2f'|format(r.delta)
                       if r.delta is not none else '—' }}</td>
    <td class="num">
      {% if r.pct is not none %}{{ '%.2f'|format(r.pct * 100) }}%
      {% if r.flagged %}<span class="pill pill-err">&#9888;</span>{% endif %}
      {% else %}—{% endif %}
    </td>
  </tr>
  {% endfor %}
</table>

<p style="font-size:14px">
  <a href="/admin/statements/{{ statement.id }}/adjust">attribute an
  unreconciled amount &rarr;</a>
  · <a href="/admin/statements">&larr; back to statements</a>
</p>
"""


STATEMENT_ADJUST_BODY = """
<h2>Attribute unreconciled amount</h2>
<div class="card" style="max-width:800px">
  <p style="font-size:14px;color:#555">
    <b>Statement:</b> {{ statement.period_label() or '—' }}
    · <b>Account:</b> {{ account_label }}
    · <b>Bank closing:</b> {{ '%.2f'|format(statement.closing_balance)
                             if statement.closing_balance is not none else '—' }}
    · <b>Expected closing (per feed):</b> {{ '%.2f'|format(expected)
                                             if expected is not none else '—' }}
    · <b>Delta:</b> <span style="color:{{ '#a04000' if delta and delta < 0 else '#1b5e20' }}">{{ '%+.2f'|format(delta) }}</span>
  </p>
  <p style="font-size:13px;margin:10px 0 0">
    <a href="/admin/statements/{{ statement.id }}">&larr; statement detail and
    validation</a>
  </p>
  {% if adjustments %}
  <h3 style="margin-top:16px">Existing attributions</h3>
  <table>
    <tr><th class="num">Amount</th><th>Offset account</th>
        <th>Description</th><th>JE</th><th></th></tr>
    {% for adj in adjustments %}
    <tr>
      <td class="num">{{ '%+.2f'|format(adj.amount) }}</td>
      <td>{{ adj.offset_account }}</td>
      <td>{{ adj.description or '' }}</td>
      <td>{{ adj.erpnext_je_name or '—' }}</td>
      <td>
        <form method="post" action="/admin/statements/{{ statement.id }}/adjust/{{ adj.id }}/delete"
              style="display:inline"
              onsubmit="return confirm('Delete this attribution?')">
          <button type="submit" class="secondary" style="font-size:11px">delete</button>
        </form>
      </td>
    </tr>
    {% endfor %}
    <tr style="border-top:1px solid #ccc">
      <td class="num"><b>{{ '%+.2f'|format(adjustment_total) }}</b> total attributed</td>
      <td colspan="4"><b>Remaining unaccounted: {{ '%+.2f'|format(remaining) }}</b></td>
    </tr>
  </table>
  {% endif %}
  <h3 style="margin-top:16px">Add attribution</h3>
  <form method="post" action="/admin/statements/{{ statement.id }}/adjust"
        style="display:flex;flex-direction:column;gap:8px;max-width:600px">
    <label>Amount (default = remaining {{ '%+.2f'|format(remaining) }})
      <input type="text" name="amount" value="{{ '%.2f'|format(remaining) }}"
             style="width:100%">
    </label>
    <label>Offset account (ERPNext account name)
      <input type="text" name="offset_account"
             list="account-suggestions"
             placeholder="e.g. 3201 - Member Distribution - OML"
             style="width:100%" required>
      <datalist id="account-suggestions">
        {% for a in account_suggestions %}
        <option value="{{ a }}">
        {% endfor %}
      </datalist>
    </label>
    <label>Description / memo
      <input type="text" name="description"
             placeholder="e.g. Federal tax payment for mom, Apr 2026 estimated"
             style="width:100%">
    </label>
    <label>ERPNext Journal Entry name (optional — if you've already booked one)
      <input type="text" name="erpnext_je_name" style="width:100%">
    </label>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button type="submit" class="primary">Save attribution</button>
      <a href="/admin/statements" style="font-size:13px;padding:8px">Cancel</a>
    </div>
  </form>
</div>
"""


@bp.post('/admin/accounts/pair')
def set_account_pair():
    """Pair a brokerage account with its cash-services companion, or unpair.

    A MANUAL PAIRING WINS. Auto-detection reads the statement's own
    'Brokerage Cash Services number' and never overwrites a value that is
    already set — so once an operator says which account carries the cash,
    that answer stands, including the answer 'none'. The operator can see
    things the PDF does not say."""
    from .. import statements as stmts
    account_id = (request.form.get('account_id') or '').strip()
    partner_id = (request.form.get('paired_account_id') or '').strip()
    account = PlaidAccount.query.filter_by(account_id=account_id).first()
    if account is None:
        return redirect('/admin/accounts?flash=' + quote_plus(
            'No such account.'))
    if partner_id and partner_id == account_id:
        return redirect('/admin/accounts?flash=' + quote_plus(
            'An account cannot be paired with itself.'))
    if partner_id and PlaidAccount.query.filter_by(
            account_id=partner_id).first() is None:
        return redirect('/admin/accounts?flash=' + quote_plus(
            'No such partner account.'))
    before = account.paired_account_id
    account.paired_account_id = partner_id or None
    audit.record('account_pair_changed', subject_type='PlaidAccount',
                 subject_id=account_id,
                 before={'paired_account_id': before},
                 after={'paired_account_id': account.paired_account_id},
                 notes='manual pairing via /admin/accounts')
    db.session.commit()
    # The pairing changes what every anchor sum for this account includes, so
    # the chain is stale the moment it is set. Rebuild rather than leave a
    # reconciliation on screen that the new pairing already contradicts.
    try:
        stmts.rebuild_statement_anchors(account_id)
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.warning('anchor rebuild after pairing failed', exc_info=True)
    msg = (f'Paired ••{account.mask or "????"} with its cash account.'
           if partner_id else f'Unpaired ••{account.mask or "????"}.')
    return redirect('/admin/accounts?flash=' + quote_plus(msg))


@bp.post('/admin/settings/sandbox_visibility')
def set_sandbox_visibility():
    """Show or hide the Plaid Sandbox test accounts.

    Hiding NEVER deletes: those accounts are the only ones with a transaction
    history varied enough to exercise the parser and the reconciliation engine
    against, so they stay in the database and one click brings them back."""
    show = bool(request.form.get('include_sandbox_accounts'))
    company = (request.form.get('sandbox_company') or '').strip()
    updates = {'include_sandbox_accounts': show}
    if company:
        updates['sandbox_company'] = company
    av.save(updates)
    summary = av.summary()
    msg = (f"Showing {summary['total_sandbox']} sandbox account(s)." if show
           else f"Hiding {summary['hidden']} sandbox account(s) "
                f"(Company '{summary['company']}'). Nothing was deleted.")
    audit.record('sandbox_visibility_changed', after=updates, notes=msg)
    db.session.commit()
    return redirect(request.form.get('next') or
                    '/admin/accounts?flash=' + quote_plus(msg))


@bp.get('/admin/reconciliation')
@bp.get('/admin/reconciliation/<path:account_id>')
def reconciliation_page(account_id: str = ''):
    """Statement-anchored reconciliation for one account (v0.4.43).

    Reachable as BOTH `/admin/reconciliation/<account_id>` and
    `/admin/reconciliation?account_id=…` — the path form is canonical and the
    query form is accepted because links to it are already in circulation.

    NOT filtered by ERPNext Company — see StatementAnchor. The accounts this
    page matters most for are the ones whose books do not exist yet."""
    from .. import statements as stmts
    accounts = av.filter_accounts(stmts.accounts_with_anchors())
    flash_msg = request.args.get('flash', '')
    if not accounts:
        return _page(RECONCILIATION_BODY, page='reconciliation', accounts=[],
                     account=PlaidAccount(account_id=''), anchors=[],
                     summary=stmts.anchor_summary(''), total_transactions=0.0,
                     labels={}, tag_reasons={}, flash_msg=flash_msg)
    account_id = (account_id or request.args.get('account_id', '')).strip()

    # A bookmark or an older link may point at the CASH-SERVICES side of a
    # pair. That account has no reconciliation of its own — it is half of one —
    # so send the reader to the brokerage account that aggregates it rather
    # than rendering an empty table and letting them conclude the data is
    # missing.
    if account_id and not any(a.account_id == account_id for a in accounts):
        owner = stmts.brokerage_for_partner(account_id)
        requested = PlaidAccount.query.filter_by(account_id=account_id).first()
        mask = f'••{requested.mask}' if requested and requested.mask \
            else 'that account'
        if owner is not None:
            return redirect(f'/admin/reconciliation/{owner.account_id}?flash='
                            + quote_plus(
                                f'{mask} is the cash side of '
                                f'••{owner.mask or "????"} — its transactions '
                                'are reconciled there, against that account\'s '
                                'statements.'))
        if requested is not None:
            flash_msg = (f'{mask} has no statement anchors, so there is '
                         'nothing to reconcile against. Showing '
                         f'{stmts.account_label(accounts[0])} instead.')

    account = next((a for a in accounts if a.account_id == account_id),
                   accounts[0])
    anchors = stmts.anchors_for_account(account.account_id)
    # Labels name the PAIR, because that is what the numbers aggregate.
    partners = {a.account_id: a for a in PlaidAccount.query.all()}
    labels = {a.account_id: stmts.account_label(
        a, partners.get((a.paired_account_id or '').strip()))
        for a in accounts}
    # v0.4.49 · the Reason column: internal tags carried by each period's
    # transactions, keyed by anchor id.
    tag_reasons = {an.id: stmts.period_tag_summary(
        account, an.period_start, an.period_end) for an in anchors}
    return _page(RECONCILIATION_BODY, page='reconciliation', accounts=accounts,
                 account=account, anchors=anchors, labels=labels,
                 tag_reasons=tag_reasons,
                 summary=stmts.anchor_summary(account.account_id),
                 flash_msg=flash_msg,
                 total_transactions=round(
                     sum(float(a.transaction_sum or 0.0) for a in anchors), 2))


@bp.get('/admin/reconciliation/<path:account_id>/csv')
def reconciliation_csv(account_id: str):
    """The same chain as a CSV — what gets handed to a CPA, or diffed against
    the second ERPNext instance once it exists."""
    import csv
    import io
    from .. import statements as stmts
    anchors = stmts.anchors_for_account(account_id)
    account = PlaidAccount.query.filter_by(account_id=account_id).first()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['period_start', 'period_end', 'anchored_opening',
                     'transaction_sum', 'computed_closing', 'anchored_closing',
                     'variance', 'variance_reason', 'chain_gap_from_prior',
                     'parser_version'])
    for a in anchors:
        writer.writerow([
            a.period_start or '', a.period_end or '',
            '' if a.anchored_opening is None else f'{a.anchored_opening:.2f}',
            f'{float(a.transaction_sum or 0.0):.2f}',
            '' if a.computed_closing is None else f'{a.computed_closing:.2f}',
            '' if a.anchored_closing is None else f'{a.anchored_closing:.2f}',
            '' if a.variance is None else f'{a.variance:.2f}',
            a.variance_reason or '', 'yes' if a.chain_gap_from_prior else '',
            a.parser_version or ''])
    # Reuse the statement store's own path sanitiser rather than a second
    # opinion on what a safe filename is — this value reaches a
    # Content-Disposition header.
    label = stmts._safe((account.mask or account.account_id)
                        if account else account_id)
    return Response(buf.getvalue(), 200, {
        'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition':
            f'attachment; filename="reconciliation-{label}.csv"',
        # The account holder's own balance history; keep it out of any shared
        # cache between the browser and the app, exactly as the PDFs are.
        'Cache-Control': 'private, no-store',
        'X-Content-Type-Options': 'nosniff',
    })


@bp.post('/admin/statements/rebuild_anchors')
def rebuild_anchors():
    """Rebuild the anchor chain — one account, or all of them.

    Also runs automatically after every stale re-parse (see
    statements.reparse_stale); this is the manual trigger for when you don't
    want to wait for the schedule."""
    from .. import statements as stmts
    account_id = (request.form.get('account_id') or '').strip()
    try:
        result = stmts.rebuild_statement_anchors(account_id or None)
    except Exception as e:
        db.session.rollback()
        return redirect('/admin/reconciliation?flash=' + quote_plus(
            f'Anchor rebuild failed: {e}'))
    msg = (f"Anchored {result['written']} period(s) across "
           f"{result['accounts']} account(s).")
    if result['variances']:
        msg += (f" {result['variances']} period(s) show a variance the "
                "transaction feed doesn't explain.")
    if result['gaps']:
        msg += f" {result['gaps']} chain gap(s) — a statement is missing."
    if result['skipped']:
        msg += f" {result['skipped']} statement(s) had no readable balance."
    audit.record('statement_anchors_rebuilt', after=result, notes=msg)
    db.session.commit()
    target = f'/admin/reconciliation/{account_id}' if account_id \
        else '/admin/reconciliation'
    return redirect(target + '?flash=' + quote_plus(msg))


@bp.get('/admin/statements/<int:statement_id>')
def statement_detail(statement_id: int):
    """One statement, everything parsed out of it, and the three-way check
    (v0.4.41).

    This is the page the family-office use case needs: a statement is only
    supporting documentation for a journal entry if someone can see WHAT it
    says and whether anything else disagrees. `validate_statement` builds the
    comparison; this route only renders it."""
    from ..models import PlaidStatement
    from .. import statements as stmts
    statement = PlaidStatement.query.get_or_404(statement_id)
    account = PlaidAccount.query.filter_by(
        account_id=statement.plaid_account_id).first()
    label = (account.name or account.mask or account.account_id
             if account else '—')
    if account is not None and account.mask:
        label = f'{label} ••{account.mask}'
    return _page(STATEMENT_DETAIL_BODY, page='statements',
                 statement=statement, account_label=label,
                 v=stmts.validate_statement(statement, account),
                 has_pdf=stmts.pdf_exists(statement))


@bp.get('/admin/statements/<int:statement_id>/adjust')
def statement_adjust_form(statement_id: int):
    """Show the manual-attribution form for one statement's delta.
    Pre-fills with the remaining unaccounted amount so a full attribution
    is one click, or partial attributions can be added sequentially."""
    from ..models import (PlaidAccount, PlaidStatement,
                          StatementAdjustment)
    from .. import statements as stmts
    statement = PlaidStatement.query.get_or_404(statement_id)
    account = PlaidAccount.query.filter_by(
        account_id=statement.plaid_account_id).first()
    verdict = stmts.reconcile_statement(statement, account)
    adjustments = (StatementAdjustment.query
                   .filter_by(statement_id=statement_id)
                   .order_by(StatementAdjustment.created_at).all())
    total = round(sum(a.amount for a in adjustments), 2)
    delta = verdict.get('delta') or 0.0
    remaining = round(delta - total, 2)
    account_label = (account.name or account.mask or account.account_id
                     if account else '—')
    # Suggest accounts from the OM chart of accounts for the datalist.
    account_suggestions = []
    try:
        erp = sync_engine.get_erp_client_or_none()
        if erp is not None:
            company = _current_company() or ''
            filters = [['is_group', '=', 0]]
            if company:
                filters.append(['company', '=', company])
            rows = erp.list_docs('Account', filters=filters,
                                 fields=['name'], limit_page_length=0)
            account_suggestions = [r['name'] for r in (rows or [])]
    except Exception:  # pragma: no cover
        pass
    return _page(STATEMENT_ADJUST_BODY, page='statements',
                 statement=statement,
                 account_label=account_label,
                 expected=verdict.get('expected_closing'),
                 delta=delta,
                 adjustments=adjustments,
                 adjustment_total=total, remaining=remaining,
                 account_suggestions=account_suggestions)


@bp.post('/admin/statements/<int:statement_id>/adjust')
def statement_adjust_create(statement_id: int):
    """Record a manual attribution. Doesn't touch ERPNext directly — the
    operator can create the JE separately and paste the JE name back in
    when ready. This keeps the workflow safe: the attribution records
    intent, the JE records the accounting."""
    from ..models import PlaidStatement, StatementAdjustment
    statement = PlaidStatement.query.get_or_404(statement_id)
    try:
        amount = float(request.form.get('amount') or 0)
    except ValueError:
        amount = 0.0
    offset_account = (request.form.get('offset_account') or '').strip()
    description = (request.form.get('description') or '').strip()
    je_name = (request.form.get('erpnext_je_name') or '').strip() or None
    if amount == 0 or not offset_account:
        return redirect(f'/admin/statements/{statement_id}/adjust?flash='
                        + quote_plus('Amount and offset account are required.'))
    adj = StatementAdjustment(
        statement_id=statement.id, amount=amount,
        offset_account=offset_account, description=description,
        erpnext_je_name=je_name,
    )
    db.session.add(adj)
    db.session.commit()
    return redirect(f'/admin/statements/{statement_id}/adjust?flash='
                    + quote_plus(f'Recorded ${amount:+.2f} to '
                                 f'{offset_account}.'))


@bp.post('/admin/statements/<int:statement_id>/adjust/<int:adjustment_id>/delete')
def statement_adjust_delete(statement_id: int, adjustment_id: int):
    """Remove a manual attribution. The residual delta on the statement
    increases by the removed amount, moving the reconciliation status
    back toward 'mismatch' if the balance was previously fully explained."""
    from ..models import StatementAdjustment
    adj = StatementAdjustment.query.get_or_404(adjustment_id)
    if adj.statement_id != statement_id:
        return redirect('/admin/statements?flash=' + quote_plus(
            'Adjustment does not belong to that statement.'))
    db.session.delete(adj)
    db.session.commit()
    return redirect(f'/admin/statements/{statement_id}/adjust?flash='
                    + quote_plus('Attribution deleted.'))


@bp.get('/admin/statements/reports/discrepancies')
def statement_discrepancies():
    """Statements whose reconciliation variance exceeds the threshold."""
    from .. import erpnext_statements as es
    report = es.discrepancy_report(_statement_report_client())
    return _page(DISCREPANCIES_BODY, page='statements', rows=report['rows'],
                 threshold=report['threshold'], data_source=report['source'])


@bp.get('/admin/statements/reports/coverage')
def statement_coverage():
    """Accounts with month gaps in their statement coverage."""
    from .. import erpnext_statements as es
    report = es.coverage_report(_statement_report_client())
    return _page(COVERAGE_BODY, page='statements', rows=report['rows'],
                 months=report['months'], data_source=report['source'])


@bp.post('/admin/statements/sync_erpnext')
def statements_sync_erpnext():
    """Operator-triggered ERPNext upload — the same work the boot job does."""
    from .. import erpnext_statements as es
    if not es.is_enabled():
        return redirect('/admin/statements?flash=' + quote_plus(
            'The ERPNext statement overlay is disabled '
            '(ERPNEXT_STATEMENTS_ENABLED).'))
    try:
        result = es.sync_all(_statement_report_client())
    except Exception as e:  # pragma: no cover - sync_all is already total
        return redirect('/admin/statements?flash=' + quote_plus(
            f'ERPNext statement sync failed: {e}'))
    msg = (f"Created {result['created']}, adopted {result['adopted']}, "
           f"already synced {result['already_synced']}.")
    if result['no_account']:
        msg += (f" {result['no_account']} skipped — their Plaid account has no "
                f"ERPNext Bank Account yet.")
    if result['failed']:
        msg += f" {result['failed']} failed."
    if result['errors']:
        msg += f" {result['errors'][0]}"
    return redirect('/admin/statements?flash=' + quote_plus(msg))


# ── v0.4.37 · Strategy dashboard (Phase B step 4) ───────────────

STRATEGY_DASHBOARD_BODY = """
<h2>Strategy dashboard</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
<p style="font-size:14px;color:#555">
  Portfolio view through the strategy lens — retained "coffee can" lots
  with 400% diversification triggers, options positions with coverage
  status, completed 5:4 cycles with realized P&L, and concentration
  warnings. All read from the last strategy detection run
  ({{ last_run.ran_at.strftime('%Y-%m-%d %H:%M') if last_run else 'never' }}).
</p>

<div style="margin:12px 0">
  <form method="post" action="/admin/strategy/rerun" style="display:inline"
        onsubmit="return confirm('Re-run 5:4 + options detection?')">
    <button type="submit" class="secondary">Re-run detection</button>
  </form>
  <a href="/admin/holdings" style="margin-left:12px;font-size:13px">
    ← holdings</a>
  <a href="/admin/investment_transactions" style="margin-left:12px;font-size:13px">
    investment transactions →</a>
</div>

{# ─ NAKED POSITION WARNINGS (empty state = discipline holding) ─ #}
{% if naked_positions %}
<div class="banner-warn" style="border-color:#c53030">
  <h3 style="color:#c53030">⚠ {{ naked_positions|length }} NAKED POSITION(S)</h3>
  <p style="font-size:13px;margin:6px 0">
    You've written short options without full coverage. This violates the
    stated 'no naked options, no margin' rule.
  </p>
  <table>
    <tr><th>Ticker</th><th>Type</th><th class="num">Strike</th>
        <th>Expires</th><th class="num">Contracts</th>
        <th class="num">Required</th><th class="num">Covered</th>
        <th class="num">Shortfall</th></tr>
    {% for p in naked_positions %}
    <tr>
      <td>{{ p.underlying_ticker }}</td>
      <td>{{ p.contract_type }}</td>
      <td class="num">{{ '%.2f'|format(p.strike_price) }}</td>
      <td>{{ p.expiration_date.isoformat() if p.expiration_date else '—' }}</td>
      <td class="num">{{ '%.0f'|format(p.contracts_open) }}</td>
      <td class="num">
        {% if p.contract_type == 'call' %}{{ (p.contracts_open|abs * 100)|int }} sh{% else %}${{ '%.0f'|format(p.strike_price * (p.contracts_open|abs) * 100) }}{% endif %}
      </td>
      <td class="num">
        {% if p.contract_type == 'call' %}{{ p.covered_by_shares }} sh{% else %}${{ '%.0f'|format(p.covered_by_cash) }}{% endif %}
      </td>
      <td class="num" style="color:#c53030">
        {% if p.contract_type == 'call' %}{{ ((p.contracts_open|abs * 100)|int - p.covered_by_shares) }} sh{% else %}${{ '%.0f'|format((p.strike_price * (p.contracts_open|abs) * 100) - p.covered_by_cash) }}{% endif %}
      </td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

{# ─ 400% DIVERSIFICATION TRIGGERS ─ #}
{% if triggers_hit %}
<h3 style="color:#c07000;margin-top:20px">
  🎯 {{ triggers_hit|length }} lot(s) hit the {{ '%.0f'|format(diversification_trigger_pct) }}% diversification trigger
</h3>
<p style="font-size:13px;color:#666">
  The rule: sell {{ '%.0f'|format(diversification_sell_pct) }}% of the retained
  shares to trim concentration. Calculated shares to sell are shown below —
  execution is discretionary (call your advisor first).
</p>
<table>
  <tr>
    <th>Ticker</th><th>Purchased</th>
    <th class="num">Cost/share</th><th class="num">Current</th>
    <th class="num">Gain</th><th class="num">Shares held</th>
    <th class="num">Sell 10%</th>
    <th class="num">Proceeds est.</th>
  </tr>
  {% for t in triggers_hit %}
  <tr>
    <td><b>{{ t.ticker }}</b></td>
    <td>{{ t.purchase_date.isoformat() }}</td>
    <td class="num">{{ '%.2f'|format(t.cost_per_share) }}</td>
    <td class="num">{{ '%.2f'|format(t.current_price) if t.current_price else '—' }}</td>
    <td class="num" style="color:#1b5e20">
      <b>{{ '%.0f'|format(t.gain_pct) }}%</b>
    </td>
    <td class="num">{{ '%.4f'|format(t.shares_remaining) }}</td>
    <td class="num">{{ '%.4f'|format(t.sell_qty) }}</td>
    <td class="num">${{ '%.2f'|format(t.sell_proceeds_est) if t.sell_proceeds_est else '—' }}</td>
  </tr>
  {% endfor %}
</table>
{% endif %}

{# ─ APPROACHING TRIGGERS (heads-up) ─ #}
{% if approaching_triggers %}
<h3 style="color:#666;margin-top:20px">
  Approaching triggers ({{ approaching_triggers|length }} at ≥350%)
</h3>
<table>
  <tr><th>Ticker</th><th class="num">Cost/share</th>
      <th class="num">Current</th><th class="num">Gain</th>
      <th class="num">Shares</th></tr>
  {% for t in approaching_triggers %}
  <tr>
    <td>{{ t.ticker }}</td>
    <td class="num">{{ '%.2f'|format(t.cost_per_share) }}</td>
    <td class="num">{{ '%.2f'|format(t.current_price) if t.current_price else '—' }}</td>
    <td class="num">{{ '%.0f'|format(t.gain_pct) }}%</td>
    <td class="num">{{ '%.4f'|format(t.shares_remaining) }}</td>
  </tr>
  {% endfor %}
</table>
{% endif %}

{# ─ RETAINED LOTS (full list, sorted by unrealized gain %) ─ #}
<h3 style="margin-top:20px">Retained lots ({{ retained_lots|length }})</h3>
<p style="font-size:13px;color:#666">
  Every "coffee can" position being held. From completed 5:4 cycles
  (tag=<code>5_4</code>) or open positions still waiting for the
  25% profit target (tag=<code>5_4_open</code>).
</p>
<table>
  <tr>
    <th>Ticker</th><th>Tag</th><th>Purchased</th>
    <th class="num">Cost/share</th><th class="num">Current</th>
    <th class="num">Gain %</th><th class="num">Shares</th>
    <th class="num">Cost basis</th><th class="num">Market value</th>
    <th class="num">Unrealized</th>
  </tr>
  {% for lot in retained_lots %}
  <tr>
    <td>
      {% if lot.gain_pct is not none and lot.gain_pct >= diversification_trigger_pct %}
      <span style="color:#c07000">🎯</span>
      {% elif lot.gain_pct is not none and lot.gain_pct >= 350 %}
      <span style="color:#666">◐</span>
      {% endif %}
      <b>{{ lot.ticker }}</b>
    </td>
    <td style="font-size:12px;color:#888">{{ lot.strategy_tag }}</td>
    <td>{{ lot.purchase_date.isoformat() }}</td>
    <td class="num">{{ '%.2f'|format(lot.cost_per_share) }}</td>
    <td class="num">{{ '%.2f'|format(lot.current_price) if lot.current_price else '—' }}</td>
    <td class="num">
      {% if lot.gain_pct is not none %}
        <span style="color:{{ '#1b5e20' if lot.gain_pct >= 0 else '#a04000' }}">
          {{ '%.0f'|format(lot.gain_pct) }}%
        </span>
      {% else %}—{% endif %}
    </td>
    <td class="num">{{ '%.4f'|format(lot.shares_remaining) }}</td>
    <td class="num">{{ '%.2f'|format(lot.cost_basis_total) }}</td>
    <td class="num">{{ '%.2f'|format(lot.market_value) if lot.market_value else '—' }}</td>
    <td class="num">
      {% if lot.unrealized is not none %}
        <span style="color:{{ '#1b5e20' if lot.unrealized >= 0 else '#a04000' }}">
          {{ '%+.2f'|format(lot.unrealized) }}
        </span>
      {% else %}—{% endif %}
    </td>
  </tr>
  {% endfor %}
</table>

{# ─ COMPLETED 5:4 CYCLES ─ #}
<h3 style="margin-top:20px">
  Completed 5:4 cycles ({{ complete_cycles|length }}) — total realized:
  <span style="color:{{ '#1b5e20' if total_realized >= 0 else '#a04000' }}">
    ${{ '%+.2f'|format(total_realized) }}
  </span>
</h3>
<table>
  <tr>
    <th>Ticker</th><th>Buy</th><th>Sell</th>
    <th class="num">Buy qty</th><th class="num">Sell qty</th>
    <th class="num">Buy $</th><th class="num">Sell $</th>
    <th class="num">Gain %</th><th class="num">P&L</th>
  </tr>
  {% for c in complete_cycles %}
  <tr>
    <td><b>{{ c.ticker }}</b></td>
    <td>{{ c.buy_date.isoformat() }}</td>
    <td>{{ c.sell_date.isoformat() }}</td>
    <td class="num">{{ '%.0f'|format(c.buy_qty) }}</td>
    <td class="num">{{ '%.0f'|format(c.sell_qty) }}</td>
    <td class="num">{{ '%.2f'|format(c.buy_price) }}</td>
    <td class="num">{{ '%.2f'|format(c.sell_price) }}</td>
    <td class="num">{{ '%.1f'|format(c.gain_pct) }}%</td>
    <td class="num">
      <span style="color:{{ '#1b5e20' if c.realized_pnl >= 0 else '#a04000' }}">
        ${{ '%+.2f'|format(c.realized_pnl) }}
      </span>
    </td>
  </tr>
  {% endfor %}
</table>

{# ─ OPTIONS POSITIONS (all open) ─ #}
{% if options_positions %}
<h3 style="margin-top:20px">
  Open options positions ({{ options_positions|length }})
</h3>
<p style="font-size:13px;color:#666">
  Coverage math shown per position. Sorted by expiration ascending —
  nearest expiry first, so the next action is at the top.
</p>
<table>
  <tr>
    <th>Ticker</th><th>Type</th><th class="num">Strike</th>
    <th>Expires</th><th class="num">Days</th><th class="num">Contracts</th>
    <th>Coverage</th><th>Status</th>
  </tr>
  {% for p in options_positions %}
  <tr>
    <td><b>{{ p.underlying_ticker }}</b></td>
    <td>{{ p.contract_type|upper }}</td>
    <td class="num">{{ '%.2f'|format(p.strike_price) if p.strike_price else '—' }}</td>
    <td>{{ p.expiration_date.isoformat() if p.expiration_date else '—' }}</td>
    <td class="num" style="color:{{ '#c53030' if p.days_to_exp is not none and p.days_to_exp < 30 else '#666' }}">
      {{ p.days_to_exp if p.days_to_exp is not none else '—' }}
    </td>
    <td class="num" style="color:{{ '#a04000' if p.contracts_open < 0 else '#1b5e20' }}">
      {{ '%+.0f'|format(p.contracts_open) }}
    </td>
    <td style="font-size:12px">
      {% if p.contracts_open < 0 %}
        {% if p.contract_type == 'call' %}
          {{ p.covered_by_shares }} / {{ ((p.contracts_open|abs) * 100)|int }} sh
        {% else %}
          ${{ '%.0f'|format(p.covered_by_cash) }} / ${{ '%.0f'|format((p.strike_price or 0) * (p.contracts_open|abs) * 100) }}
        {% endif %}
      {% else %}
        long
      {% endif %}
    </td>
    <td>
      {% if p.is_naked %}
        <span class="pill pill-err">NAKED</span>
      {% elif p.contracts_open < 0 %}
        <span class="pill pill-ok">covered</span>
      {% else %}
        <span class="pill pill-muted">long</span>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
</table>
{% endif %}

{# ─ PORTFOLIO CONCENTRATION ─ #}
{% if concentration %}
<h3 style="margin-top:20px">Portfolio concentration — top {{ concentration|length }} positions</h3>
<table>
  <tr><th>Ticker</th><th class="num">Market value</th><th class="num">% of portfolio</th></tr>
  {% for c in concentration %}
  <tr>
    <td><b>{{ c.ticker }}</b></td>
    <td class="num">${{ '{:,.2f}'.format(c.value) }}</td>
    <td class="num" style="color:{{ '#c07000' if c.pct >= 20 else '#666' }}">
      {{ '%.1f'|format(c.pct) }}%
      {% if c.pct >= 20 %}<span style="font-size:11px">⚠ over 20%</span>{% endif %}
    </td>
  </tr>
  {% endfor %}
  <tr style="border-top:2px solid #333">
    <td colspan="2" style="text-align:right"><b>Total portfolio value:</b></td>
    <td class="num"><b>${{ '{:,.2f}'.format(total_portfolio_value) }}</b></td>
  </tr>
</table>
{% endif %}
"""


@bp.get('/admin/strategy')
def strategy_dashboard_page():
    """Full strategy dashboard: retained lots + triggers + options + cycles.
    Reads the latest detector output from strategy_tracker tables + joins
    with current SecurityHolding for market value math."""
    from ..models import (OptionsPosition, RetainedLot, Security,
                          SecurityHolding, StrategyTracker, TradedCycle)
    from .. import strategy_settings

    cfg = strategy_settings.load()
    trigger_pct = float(cfg['diversification_trigger_pct'])
    sell_pct = float(cfg['diversification_sell_pct']) / 100.0

    last_run = (StrategyTracker.query
                .order_by(StrategyTracker.ran_at.desc()).first())

    # Preload securities + holdings for O(1) lookups per lot.
    secs = {s.security_id: s for s in Security.query.all()}
    holdings = {h.security_id: h for h in SecurityHolding.query.all()}

    def _lot_metrics(lot):
        sec = secs.get(lot.security_id)
        holding = holdings.get(lot.security_id)
        cost_basis_total = round(
            (lot.cost_basis_per_share or 0) * lot.shares_remaining, 2)
        current_price = None
        market_value = None
        unrealized = None
        gain_pct = None
        if holding and holding.institution_price is not None:
            current_price = holding.institution_price
            market_value = round(current_price * lot.shares_remaining, 2)
            unrealized = round(market_value - cost_basis_total, 2)
            if cost_basis_total > 0:
                gain_pct = round(100.0 * unrealized / cost_basis_total, 1)
        return {
            'lot': lot, 'ticker': sec.ticker_symbol if sec else '—',
            'purchase_date': lot.purchase_date,
            'cost_per_share': lot.cost_basis_per_share,
            'current_price': current_price,
            'gain_pct': gain_pct,
            'shares_remaining': lot.shares_remaining,
            'cost_basis_total': cost_basis_total,
            'market_value': market_value,
            'unrealized': unrealized,
            'strategy_tag': lot.strategy_tag,
        }

    all_lots = [_lot_metrics(lot) for lot in
                RetainedLot.query.order_by(RetainedLot.purchase_date).all()]

    # Sort retained lots by gain_pct descending (biggest winners first),
    # None gains sink to bottom.
    all_lots.sort(key=lambda x: (
        x['gain_pct'] if x['gain_pct'] is not None else -99999.0), reverse=True)

    # 400%+ triggers with sell recommendation math.
    triggers_hit = []
    for m in all_lots:
        if m['gain_pct'] is not None and m['gain_pct'] >= trigger_pct:
            sell_qty = round(m['shares_remaining'] * sell_pct, 4)
            proceeds = (round(sell_qty * m['current_price'], 2)
                        if m['current_price'] else None)
            triggers_hit.append({**m, 'sell_qty': sell_qty,
                                 'sell_proceeds_est': proceeds})
    approaching_triggers = [m for m in all_lots
                            if m['gain_pct'] is not None
                            and 350.0 <= m['gain_pct'] < trigger_pct]

    # Complete cycles: only 5:4-matched ones, ordered by sell_date desc.
    complete_cycles_raw = (TradedCycle.query
                           .filter_by(cycle_status='complete')
                           .order_by(TradedCycle.sell_date.desc()).all())
    complete_cycles = []
    total_realized = 0.0
    for c in complete_cycles_raw:
        sec = secs.get(c.security_id)
        gain_pct = (100.0 * (c.sell_price - c.buy_price) / c.buy_price
                    if c.buy_price else 0.0)
        complete_cycles.append({
            'ticker': sec.ticker_symbol if sec else '—',
            'buy_date': c.buy_date, 'sell_date': c.sell_date,
            'buy_qty': c.buy_qty, 'sell_qty': c.sell_qty,
            'buy_price': c.buy_price, 'sell_price': c.sell_price,
            'gain_pct': round(gain_pct, 1),
            'realized_pnl': c.realized_pnl or 0.0,
        })
        total_realized += (c.realized_pnl or 0.0)

    # Options positions ordered by expiration ascending.
    from datetime import date as _date
    options_positions_raw = (OptionsPosition.query
                             .filter(OptionsPosition.status == 'open')
                             .order_by(OptionsPosition.expiration_date.asc())
                             .all())
    options_positions = []
    for p in options_positions_raw:
        days_to_exp = None
        if p.expiration_date:
            days_to_exp = (p.expiration_date - _date.today()).days
        options_positions.append({
            'underlying_ticker': p.underlying_ticker,
            'contract_type': p.contract_type,
            'strike_price': p.strike_price,
            'expiration_date': p.expiration_date,
            'days_to_exp': days_to_exp,
            'contracts_open': p.contracts_open,
            'covered_by_shares': p.covered_by_shares,
            'covered_by_cash': p.covered_by_cash,
            'is_naked': p.is_naked,
        })
    naked_positions = [p for p in options_positions if p['is_naked']]

    # Concentration: top positions by market_value across all holdings
    # (long only), scoped to the current Company.
    scope = _current_company()
    companies_by_account = _resolve_account_companies()
    account_values: dict = {}
    for h in holdings.values():
        if scope:
            if companies_by_account.get(h.account_id, '') != scope:
                continue
        if h.institution_value is None or h.institution_value <= 0:
            continue
        sec = secs.get(h.security_id)
        if sec is None or sec.is_option:
            continue
        ticker = sec.ticker_symbol or '—'
        account_values[ticker] = account_values.get(ticker, 0) + h.institution_value
    total_portfolio_value = sum(account_values.values())
    concentration = []
    if total_portfolio_value > 0:
        sorted_positions = sorted(account_values.items(),
                                  key=lambda x: -x[1])
        for ticker, value in sorted_positions[:10]:
            concentration.append({
                'ticker': ticker, 'value': value,
                'pct': round(100.0 * value / total_portfolio_value, 1),
            })

    return _page(STRATEGY_DASHBOARD_BODY, page='investments',
                 last_run=last_run,
                 retained_lots=all_lots,
                 triggers_hit=triggers_hit,
                 approaching_triggers=approaching_triggers,
                 diversification_trigger_pct=trigger_pct,
                 diversification_sell_pct=float(cfg['diversification_sell_pct']),
                 complete_cycles=complete_cycles,
                 total_realized=total_realized,
                 options_positions=options_positions,
                 naked_positions=naked_positions,
                 concentration=concentration,
                 total_portfolio_value=total_portfolio_value,
                 flash_msg=request.args.get('flash', ''))


# ── v0.4.40 · Data hygiene actions ───────────────────────────────

DATA_HYGIENE_BODY = """
<h2>Data hygiene</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
<p style="font-size:14px;color:#555">
  Local-database housekeeping. None of these actions touch ERPNext or
  Plaid — they only clean up Bank Bridge's local mirror. Safe to run
  repeatedly; each action is idempotent.
</p>

<h3>Duplicate PlaidAccount rows across items</h3>
<p style="font-size:13px;color:#666">
  A re-link creates a NEW PlaidItem with NEW PlaidAccount rows. The old
  Item's PlaidAccounts stay in the DB (needed for historical transaction
  attribution). But when an old row isn't marked
  <code>superseded_by_account_id</code>, it can show up in reconciliation
  and admin queries alongside the current row, causing double-counting.
</p>

{% if duplicate_groups %}
<table>
  <tr>
    <th>Bank</th><th>Mask</th><th>Company</th>
    <th class="num">Rows</th><th>Active row</th>
    <th>Superseded status</th>
  </tr>
  {% for g in duplicate_groups %}
  <tr>
    <td>{{ g.bank }}</td>
    <td><code>{{ g.mask }}</code></td>
    <td>{{ g.company or '—' }}</td>
    <td class="num">{{ g.rows|length }}</td>
    <td><code style="font-size:11px">{{ g.active_id[:14] }}…</code></td>
    <td>
      {{ g.superseded_count }} of {{ g.rows|length - 1 }} old row(s)
      marked superseded
      {% if g.superseded_count < g.rows|length - 1 %}
      <span style="color:#c07000">⚠ needs cleanup</span>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
</table>

<form method="post" action="/admin/data_hygiene/mark_superseded"
      onsubmit="return confirm('Mark old duplicate PlaidAccounts as superseded by the newest active-item counterparts? This is data-only cleanup; ERPNext isn\\'t touched.')"
      style="margin:12px 0">
  <button type="submit" class="primary">Mark duplicates as superseded</button>
  <span style="font-size:12px;color:#888;margin-left:12px">
    Runs a fingerprint-based match (bank + mask + company). Older
    account_ids on disconnected items are pointed at the newest active
    counterpart via <code>superseded_by_account_id</code>. Sync_enabled
    is also set to false on superseded rows so they stop pulling data.
  </span>
</form>
{% else %}
<div class="card">
  <b>No duplicate account groups detected.</b> All PlaidAccounts appear
  to be either uniquely fingerprinted or properly marked superseded.
</div>
{% endif %}

<h3 style="margin-top:24px">Disconnected items with active PlaidAccounts</h3>
<p style="font-size:13px;color:#666">
  When an operator disconnects a bank via
  <code>/admin/accounts</code>, the PlaidItem is marked disconnected but
  its PlaidAccounts stay <code>sync_enabled=True</code>. Since v0.4.18
  the bulk-import path already skips accounts on disconnected items, but
  sync_enabled being <code>True</code> on retired rows is untidy and
  triggers per-sync log noise.
</p>

{% if orphan_active_accounts %}
<table>
  <tr>
    <th>Institution</th><th>Item</th><th>Mask</th>
    <th>ERPNext Bank Account</th><th class="num">Sync enabled?</th>
  </tr>
  {% for a in orphan_active_accounts %}
  <tr>
    <td>{{ a.institution or '—' }}</td>
    <td><code style="font-size:11px">{{ a.item_id[:14] }}…</code></td>
    <td><code>{{ a.mask or '—' }}</code></td>
    <td>{{ a.erpnext or '—' }}</td>
    <td class="num">yes</td>
  </tr>
  {% endfor %}
</table>

<form method="post" action="/admin/data_hygiene/disable_orphan_sync"
      onsubmit="return confirm('Disable sync on PlaidAccounts whose Item is disconnected? They can be re-enabled per account on /admin/accounts if needed.')"
      style="margin:12px 0">
  <button type="submit" class="primary">Disable sync on orphaned accounts</button>
</form>
{% else %}
<div class="card">
  <b>No orphaned active accounts.</b> Every sync_enabled PlaidAccount
  belongs to a connected Item.
</div>
{% endif %}
"""


def _duplicate_account_groups() -> list:
    """Group PlaidAccounts by (institution, mask, owning_company). Any
    group with more than one row is a duplicate — usually from a re-link
    creating a new Item while the old one still holds the same account.
    The 'active' row is the one whose PlaidItem is not disconnected;
    if multiple non-disconnected rows exist (rare), the newest by
    created_at wins."""
    from ..models import PlaidAccount, PlaidItem
    accounts = (db.session.query(PlaidAccount, PlaidItem)
                .join(PlaidItem, PlaidItem.item_id == PlaidAccount.item_id)
                .order_by(PlaidAccount.created_at.desc()).all())
    groups: dict = {}
    for acct, item in accounts:
        key = ((item.institution_id or item.institution_name or ''),
               (acct.mask or ''),
               (acct.owning_company or ''))
        groups.setdefault(key, []).append((acct, item))
    dupes = []
    for (institution, mask, company), rows in groups.items():
        if len(rows) < 2 or not mask:
            continue
        # Pick the active row: prefer non-disconnected, newest by created_at.
        active_rows = [r for r in rows if not r[1].disconnected]
        active = (active_rows[0] if active_rows else rows[0])
        active_acct = active[0]
        superseded_count = sum(1 for a, _ in rows
                               if a.account_id != active_acct.account_id
                               and (a.superseded_by_account_id ==
                                    active_acct.account_id))
        dupes.append({
            'bank': (rows[0][1].institution_name or institution),
            'mask': mask, 'company': company,
            'rows': rows,
            'active_id': active_acct.account_id,
            'superseded_count': superseded_count,
        })
    return dupes


def _orphan_active_accounts() -> list:
    """PlaidAccounts on disconnected items that still have
    sync_enabled=True. Deprecation flag not the same as disconnected —
    a disconnected item should have all its accounts flipped off."""
    from ..models import PlaidAccount, PlaidItem
    rows = (db.session.query(PlaidAccount, PlaidItem)
            .join(PlaidItem, PlaidItem.item_id == PlaidAccount.item_id)
            .filter(PlaidItem.disconnected.is_(True),
                    PlaidAccount.sync_enabled.is_(True)).all())
    return [{
        'institution': item.institution_name,
        'item_id': item.item_id, 'mask': acct.mask or '',
        'erpnext': acct.erpnext_bank_account_name or '',
    } for acct, item in rows]


@bp.get('/admin/data_hygiene')
def data_hygiene_page():
    return _page(DATA_HYGIENE_BODY, page='accounts',
                 duplicate_groups=_duplicate_account_groups(),
                 orphan_active_accounts=_orphan_active_accounts(),
                 flash_msg=request.args.get('flash', ''))


@bp.post('/admin/data_hygiene/mark_superseded')
def data_hygiene_mark_superseded():
    """For every duplicate PlaidAccount group, mark all non-active rows
    as superseded_by the active row's account_id AND flip their
    sync_enabled to False. Idempotent — a row already correctly
    superseded is skipped."""
    from ..models import PlaidAccount
    updated = 0
    for group in _duplicate_account_groups():
        active_id = group['active_id']
        for acct, item in group['rows']:
            if acct.account_id == active_id:
                continue
            if acct.superseded_by_account_id == active_id and not acct.sync_enabled:
                continue
            acct.superseded_by_account_id = active_id
            acct.sync_enabled = False
            updated += 1
    db.session.commit()
    return redirect('/admin/data_hygiene?flash=' + quote_plus(
        f'Marked {updated} duplicate PlaidAccount(s) as superseded.'))


@bp.post('/admin/data_hygiene/disable_orphan_sync')
def data_hygiene_disable_orphan_sync():
    """Flip sync_enabled=False on every PlaidAccount whose PlaidItem
    is disconnected. Idempotent."""
    from ..models import PlaidAccount, PlaidItem
    disconnected_ids = {row.item_id for row in
                        PlaidItem.query.filter(
                            PlaidItem.disconnected.is_(True)).all()}
    if not disconnected_ids:
        return redirect('/admin/data_hygiene?flash=' + quote_plus(
            'No disconnected items — nothing to disable.'))
    updated = 0
    for acct in (PlaidAccount.query
                 .filter(PlaidAccount.item_id.in_(disconnected_ids),
                         PlaidAccount.sync_enabled.is_(True)).all()):
        acct.sync_enabled = False
        updated += 1
    db.session.commit()
    return redirect('/admin/data_hygiene?flash=' + quote_plus(
        f'Disabled sync on {updated} orphaned account(s).'))


# ── v0.4.32 · Strategy tracker run trigger ───────────────────────

@bp.post('/admin/strategy/rerun')
def rerun_strategy_detection():
    """Manually trigger the 5:4 + options detector. Reads current
    SecurityTransaction rows, wipes derived state, recomputes cycles /
    lots / options positions. Logged as a StrategyTracker audit row."""
    from .. import strategy_tracker
    try:
        run = strategy_tracker.run_detection(notes='manual — operator triggered')
    except Exception as e:  # pragma: no cover
        return redirect('/admin/strategy?flash=' + quote_plus(
            f'Detection failed: {e}'))
    msg = (f"Detection complete. Scanned {run.transactions_scanned} "
           f"transactions → {run.cycles_created} cycle(s), "
           f"{run.retained_lots_created} retained lot(s), "
           f"{run.options_positions_touched} option position(s), "
           f"{run.naked_positions_flagged} naked flagged.")
    return redirect('/admin/strategy?flash=' + quote_plus(msg))


# ── v0.4.29 · Investments (Phase A step 3) ───────────────────────
#
# Two pages surface the data investments.sync_investments_for_item lands in
# the securities/holdings/security_transactions tables:
#
#   /admin/holdings — current positions per mapped investment account, sorted
#     by market value descending. Shows ticker, name, quantity, cost basis,
#     current price + as-of stamp, market value, and unrealized gain / gain %.
#   /admin/investment_transactions — trade history, filterable by security
#     and account and date range, with the same pagination-with-cap pattern
#     the /admin/transactions page uses.
#
# Both pages read straight from the DB — no live Plaid calls — so a slow or
# unreachable Plaid doesn't stall the UI.

HOLDINGS_BODY = """
<h2>Investment holdings</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
<p style="font-size:14px;color:#555">
  Current positions across investment accounts, from the last
  <code>/investments/holdings/get</code> Plaid pulled. Prices shown are the
  institution price Plaid returned with each holding + the timestamp it was
  as of — this is snapshot data, not real-time; TradingView (or your
  brokerage app) is authoritative for current market. Balance-sheet math uses
  the snapshot.
</p>
<div style="margin:12px 0">
  <form method="post" action="/admin/strategy/rerun" style="display:inline"
        onsubmit="return confirm('Run the 5:4 strategy detector against your current investment transactions? This wipes existing derived cycles and recomputes from the raw data.')">
    <button type="submit" class="secondary">Run strategy detection</button>
  </form>
  <span style="font-size:12px;color:#888;margin-left:8px">
    v0.4.32 · runs the 5:4 detector: matches buys to sells (~0.8× qty at ~25%
    profit), records TradedCycles + RetainedLots, prepares data for the
    strategy dashboard.
  </span>
</div>
{% if last_run %}
<div class="card" style="max-width:800px">
  <b>Last detection run:</b> {{ last_run.ran_at.strftime('%Y-%m-%d %H:%M') }}
  &middot; scanned {{ last_run.transactions_scanned }} transactions
  &middot; <b>{{ last_run.cycles_created }}</b> cycles
  &middot; <b>{{ last_run.retained_lots_created }}</b> retained lots
  {% if last_run.notes %}
  <div style="font-size:12px;color:#888;margin-top:4px">{{ last_run.notes }}</div>
  {% endif %}
</div>
{% endif %}
{% if not enabled_items %}
<div class="banner-warn">
  <h3>No investment accounts synced yet</h3>
  <p style="font-size:14px;margin:0">
    Investment holdings require the <code>investments</code> product to be
    enabled on your Plaid Production application AND requested at link time
    (v0.4.26+). Any existing Item minted before v0.4.26 needs a disconnect
    + re-link to get investments data. New Items link with it by default.
  </p>
</div>
{% endif %}
{% for group in groups %}
<h3 style="margin-bottom:4px">{{ group.account_label }}</h3>
<div style="font-size:13px;color:#666">
  {{ group.company or 'no Company resolved' }}
  {% if group.market_value is not none %}
  &middot; total market value {{ '%.2f'|format(group.market_value) }}
  {{ group.currency }}
  {% endif %}
  {% if group.last_sync %}
  &middot; last sync {{ group.last_sync.strftime('%Y-%m-%d %H:%M') }}
  {% endif %}
</div>
<table>
  <tr>
    <th>Ticker</th><th>Name</th><th>Type</th>
    <th class="num">Quantity</th>
    <th class="num">Cost basis</th>
    <th class="num">Price</th>
    <th class="num">Market value</th>
    <th class="num">Unrealized</th>
    <th>As of</th>
  </tr>
  {% for h in group.rows %}
  <tr>
    <td>
      {% if h.security.is_option %}
        <span title="{{ h.security.option_contract_type|upper }} @{{ h.security.option_strike_price }} exp {{ h.security.option_expiration_date }}">{{ h.security.option_underlying_ticker or h.security.ticker_symbol or '—' }}</span>
        <span class="pill pill-muted" style="font-size:10px"
              title="Option contract">opt</span>
      {% else %}
        <b>{{ h.security.ticker_symbol or '—' }}</b>
      {% endif %}
    </td>
    <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ h.security.name or '—' }}</td>
    <td style="color:#888;font-size:12px">{{ h.security.type or '—' }}</td>
    <td class="num">{{ '%.4f'|format(h.holding.quantity) }}</td>
    <td class="num">{{ '%.2f'|format(h.holding.cost_basis)
                       if h.holding.cost_basis is not none else '—' }}</td>
    <td class="num">{{ '%.2f'|format(h.holding.institution_price)
                       if h.holding.institution_price is not none else '—' }}</td>
    <td class="num">{{ '%.2f'|format(h.holding.institution_value)
                       if h.holding.institution_value is not none else '—' }}</td>
    <td class="num">
      {% if h.unrealized is not none %}
        <span style="color:{{ '#1b5e20' if h.unrealized >= 0 else '#a04000' }}">
          {{ '%+.2f'|format(h.unrealized) }}
          {% if h.unrealized_pct is not none %}
          <span style="font-size:11px">({{ '%+.1f'|format(h.unrealized_pct) }}%)</span>
          {% endif %}
        </span>
      {% else %}—{% endif %}
    </td>
    <td style="font-size:12px;color:#888">{{ h.holding.institution_price_as_of.isoformat() if h.holding.institution_price_as_of else '—' }}</td>
  </tr>
  {% endfor %}
</table>
{% endfor %}
{% if groups %}
<p style="font-size:14px">
  <a href="/admin/investment_transactions">→ investment transactions</a>
</p>
{% endif %}
"""


INVESTMENT_TXNS_BODY = """
<h2>Investment transactions</h2>
<p style="font-size:14px;color:#555">
  Trade history from <code>/investments/transactions/get</code>. Includes
  buys, sells, dividends, splits, transfers, fees, and cancellations — every
  event Plaid ships against an investment account. The v0.5.0 lot tracker
  reads these to auto-classify 5:4 cycles.
</p>
<div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
<form method="get" action="/admin/investment_transactions" class="card"
      style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin:0">
  <label style="margin:0">Type
    <select name="type">
      <option value="">(any)</option>
      {% for t in ['buy', 'sell', 'cash', 'transfer', 'fee', 'cancel'] %}
      <option value="{{ t }}" {{ 'selected' if t == cur_type else '' }}>{{ t }}</option>
      {% endfor %}
    </select>
  </label>
  <label style="margin:0">Ticker
    <input name="ticker" value="{{ cur_ticker }}" placeholder="AAPL"
           style="width:120px">
  </label>
  <button type="submit" class="primary">Filter</button>
</form>
</div>
<p style="font-size:13px;color:#555;margin:8px 0">
  Showing <b>{{ rows|length }}</b>{% if total > rows|length %} of
  <b>{{ total }}</b>{% endif %} matching transactions
</p>
<table>
  <tr>
    <th>Date</th><th>Ticker</th><th>Type / Subtype</th><th>Name</th>
    <th class="num">Quantity</th><th class="num">Price</th>
    <th class="num">Amount</th><th class="num">Fees</th>
  </tr>
  {% for r in rows %}
  <tr>
    <td style="white-space:nowrap">{{ r.txn.date.isoformat() if r.txn.date else '—' }}</td>
    <td>
      {% if r.security and r.security.is_option %}
        <span>{{ r.security.option_underlying_ticker or r.security.ticker_symbol or '—' }}</span>
        <span class="pill pill-muted" style="font-size:10px">opt</span>
      {% else %}
        <b>{{ r.security.ticker_symbol if r.security else '—' }}</b>
      {% endif %}
    </td>
    <td style="font-size:12px">
      <b>{{ r.txn.type or '—' }}</b>
      {% if r.txn.subtype %}<span style="color:#888">/ {{ r.txn.subtype }}</span>{% endif %}
    </td>
    <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ r.txn.name or '—' }}</td>
    <td class="num">{{ '%.4f'|format(r.txn.quantity) }}</td>
    <td class="num">{{ '%.2f'|format(r.txn.price) if r.txn.price else '—' }}</td>
    <td class="num">{{ '%.2f'|format(r.txn.amount) }}</td>
    <td class="num">{{ '%.2f'|format(r.txn.fees) if r.txn.fees is not none else '—' }}</td>
  </tr>
  {% endfor %}
</table>
<p style="font-size:14px"><a href="/admin/holdings">← back to holdings</a></p>
"""


@bp.get('/admin/holdings')
def holdings_page():
    """Current investment positions per mapped account (v0.4.29). Reads
    directly from the local securities + security_holdings tables — no
    live Plaid calls. Grouped by PlaidAccount, ordered by market value
    descending within each group so the largest position leads."""
    from ..models import (PlaidAccount, PlaidItem, Security, SecurityHolding)
    scope = _current_company()
    companies_by_account = _resolve_account_companies()
    invest_accounts = av.visible_accounts(
        PlaidAccount.query
        .filter(PlaidAccount.type.in_(('investment', 'brokerage')))
        .order_by(PlaidAccount.name))
    enabled_items = (
        db.session.query(PlaidItem.item_id)
        .filter(PlaidItem.investments_synced_at.isnot(None),
                PlaidItem.disconnected.isnot(True))
        .all())
    groups = []
    for account in invest_accounts:
        company = companies_by_account.get(account.account_id, '')
        if scope and company != scope:
            continue
        holdings = (SecurityHolding.query
                    .filter_by(account_id=account.account_id).all())
        if not holdings:
            continue
        # Enrich with the linked Security row and unrealized gain math.
        sids = {h.security_id for h in holdings}
        securities = {s.security_id: s for s in
                      Security.query.filter(Security.security_id.in_(sids)).all()}
        enriched = []
        for h in sorted(holdings,
                        key=lambda x: -(x.institution_value or 0)):
            sec = securities.get(h.security_id)
            unrealized = None
            unrealized_pct = None
            if (h.institution_value is not None
                    and h.cost_basis is not None):
                unrealized = round(h.institution_value - h.cost_basis, 2)
                if h.cost_basis > 0:
                    unrealized_pct = round(
                        100.0 * unrealized / h.cost_basis, 1)
            enriched.append({'holding': h, 'security': sec,
                             'unrealized': unrealized,
                             'unrealized_pct': unrealized_pct})
        total_value = sum((h.institution_value or 0) for h in holdings)
        last_sync = None
        item = PlaidItem.query.filter_by(item_id=account.item_id).first()
        if item and item.investments_synced_at:
            last_sync = item.investments_synced_at
        label = (account.name or account.official_name
                 or account.mask or account.account_id)
        if account.mask:
            label = f'{label} ••{account.mask}'
        groups.append({
            'account_label': label, 'company': company, 'rows': enriched,
            'market_value': round(total_value, 2),
            'currency': account.iso_currency_code or account.currency or 'USD',
            'last_sync': last_sync,
        })
    from ..models import StrategyTracker
    last_run = (StrategyTracker.query
                .order_by(StrategyTracker.ran_at.desc()).first())
    return _page(HOLDINGS_BODY, page='investments', groups=groups,
                 enabled_items=enabled_items, last_run=last_run,
                 flash_msg=request.args.get('flash', ''))


@bp.get('/admin/investment_transactions')
def investment_transactions_page():
    """Investment transaction history with type + ticker filters (v0.4.29)."""
    from ..models import Security, SecurityTransaction, PlaidAccount
    cur_type = (request.args.get('type') or '').strip().lower()
    cur_ticker = (request.args.get('ticker') or '').strip().upper()
    try:
        limit = int(request.args.get('limit') or 500)
    except ValueError:
        limit = 500
    limit = max(50, min(limit, 5000))
    scope = _current_company()
    q = SecurityTransaction.query
    if scope:
        scoped_ids = [aid for aid, c in _resolve_account_companies().items()
                      if c == scope]
        q = q.filter(SecurityTransaction.account_id.in_(scoped_ids))
    # v0.4.44 · a sandbox account's investment activity is test data; excluded
    # here for the same reason the account itself is excluded from every list.
    hidden = av.hidden_account_ids()
    if hidden:
        q = q.filter(SecurityTransaction.account_id.notin_(tuple(hidden)))
    if cur_type:
        q = q.filter(SecurityTransaction.type == cur_type)
    if cur_ticker:
        matching_sids = [s.security_id for s in Security.query.filter(
            db.or_(Security.ticker_symbol.ilike(cur_ticker),
                   Security.option_underlying_ticker.ilike(cur_ticker))).all()]
        if not matching_sids:
            matching_sids = ['__no_match__']
        q = q.filter(SecurityTransaction.security_id.in_(matching_sids))
    total = q.count()
    txns = (q.order_by(SecurityTransaction.date.desc().nullslast(),
                       SecurityTransaction.id.desc())
            .limit(limit).all())
    # Enrich with securities in one query
    sids = {t.security_id for t in txns if t.security_id}
    secs = {s.security_id: s for s in
            Security.query.filter(Security.security_id.in_(sids)).all()}
    rows = [{'txn': t, 'security': secs.get(t.security_id)} for t in txns]
    return _page(INVESTMENT_TXNS_BODY, page='investments', rows=rows,
                 total=total, cur_type=cur_type, cur_ticker=cur_ticker)


# ── v0.4.5 · Counterparty overlay ────────────────────────────────
#
# The screens ERPNext structurally cannot draw, because it has no concept that
# the Customer "Wells Fargo" and the Supplier "Wells Fargo" are one party. Every
# page here reads the ledger LIVE (GL Entry) rather than the cached rollup
# fields, so a number on screen is never staler than the last ERPNext write —
# the cached fields exist for people looking at the Counterparty inside ERPNext
# itself.


def _cp_client():
    """The ERPNext client for a counterparty screen, or None. Every page here
    degrades to an empty state rather than a 500 when ERPNext is unconfigured or
    the overlay was never provisioned."""
    return sync_engine.get_erp_client_or_none()


def _erpnext_base() -> str:
    """The ERPNext base URL for drill-through links, without a trailing slash.
    '' when unconfigured, which the templates render as plain text instead of a
    dead link."""
    return (erps.load().get('url') or '').strip().rstrip('/')


def _role_pills(row) -> str:
    """The 💵 / 💳 role indicators for one Counterparty row."""
    pills = []
    if (row.get('customer_link') or '').strip():
        pills.append('<span class="pill pill-ok" title="Customer — they pay you">'
                     '&#128181; Customer</span>')
    if (row.get('supplier_link') or '').strip():
        pills.append('<span class="pill pill-warn" title="Supplier — you pay them">'
                     '&#128179; Supplier</span>')
    return ' '.join(pills) or '<span class="pill pill-muted">no links</span>'


COUNTERPARTIES_BODY = """
<h2>Counterparties</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
{% if not overlay_ok %}
<div class="banner-warn">
  <h3>The Counterparty overlay isn't provisioned</h3>
  <p style="font-size:14px;margin:0">
    Bank Bridge creates a <code>Counterparty</code> doctype in ERPNext at
    startup. That hasn't happened here — either ERPNext isn't configured yet,
    the API user lacks permission to create a DocType (it needs the System
    Manager role), or <code>COUNTERPARTY_OVERLAY_ENABLED</code> is off.
    Check the connection on <a href="/admin/erpnext_settings">ERPNext</a> and
    restart, or run
    <code>python -m scripts.pair_existing_customer_supplier --dry-run</code>
    to see what it would do.
  </p>
</div>
{% endif %}
<p style="font-size:14px;color:#555">
  One identity per party, layered over ERPNext's separate Customer and Supplier
  records. A party with both roles is a <b>dual-role</b> counterparty — a bank
  that pays you interest and charges you fees is the canonical case. Nothing
  here changes how anything posts; the overlay only adds identity.
</p>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0">
  <a href="/admin/counterparties/reports/aged" class="secondary" style="text-decoration:none">Aged balances</a>
  <a href="/admin/counterparties/reports/1099" class="secondary" style="text-decoration:none">1099-eligible</a>
  <a href="/admin/counterparties/reports/top" class="secondary" style="text-decoration:none">Top by activity</a>
</div>
<form method="get" action="/admin/counterparties" class="card"
      style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
  <label style="margin:0;flex:1">Search
    <input name="q" value="{{ cur_q }}" placeholder="counterparty name">
  </label>
  <label style="margin:0;width:220px">Type
    <select name="type">
      <option value="">All types</option>
      {% for t in types %}
      <option value="{{ t }}" {{ 'selected' if t == cur_type else '' }}>{{ t }}</option>
      {% endfor %}
    </select>
  </label>
  <button type="submit" class="primary">Filter</button>
</form>
<table>
  <tr><th>Counterparty</th><th>Type</th><th>Roles</th>
      <th class="num">AR activity</th><th class="num">AP activity</th>
      <th>First txn</th><th></th></tr>
  {% for r in rows %}
  <tr>
    <td><b>{{ r.counterparty_name or r.name }}</b>
      {% if r.dual_role_flag %}<span class="pill pill-ok" title="Both roles">dual</span>{% endif %}</td>
    <td>{{ r.counterparty_type or '—' }}</td>
    <td>{{ role_pills(r)|safe }}</td>
    <td class="num">{{ '%.2f'|format(r.total_activity_ar or 0.0) }}</td>
    <td class="num">{{ '%.2f'|format(r.total_activity_ap or 0.0) }}</td>
    <td>{{ r.date_of_first_transaction or '—' }}</td>
    <td><a href="/admin/counterparties/{{ (r.name or '')|urlencode }}" class="secondary"
           style="text-decoration:none;padding:3px 10px;font-size:12px">Ledger</a></td>
  </tr>
  {% endfor %}
  {% if not rows %}
  <tr><td colspan="7" style="color:#888">
    {% if overlay_ok %}No counterparties yet — they appear as parties are
    created, or run the pairing script.{% else %}Nothing to show until the
    overlay is provisioned.{% endif %}
  </td></tr>
  {% endif %}
</table>
"""


@bp.get('/admin/counterparties')
def counterparties_page():
    client = _cp_client()
    cur_q = (request.args.get('q') or '').strip()
    cur_type = (request.args.get('type') or '').strip()
    rows = counterparty.list_counterparties(client, search=cur_q,
                                            counterparty_type=cur_type)
    return _page(COUNTERPARTIES_BODY, page='counterparties', rows=rows,
                 cur_q=cur_q, cur_type=cur_type,
                 types=counterparty.COUNTERPARTY_TYPES,
                 role_pills=_role_pills,
                 overlay_ok=counterparty.available(client),
                 flash_msg=request.args.get('flash', ''))


COUNTERPARTY_DETAIL_BODY = """
<h2>{{ cp.counterparty_name or cp.name }}
  {% if cp.dual_role_flag %}<span class="pill pill-ok">dual role</span>{% endif %}</h2>
<p style="font-size:14px;color:#555">
  <a href="/admin/counterparties">&larr; All counterparties</a> &nbsp;·&nbsp;
  Type: <b>{{ cp.counterparty_type or '—' }}</b> &nbsp;·&nbsp; {{ role_pills(cp)|safe }}
</p>
<div class="kpis">
  <div class="kpi"><div style="font-size:12px;color:#666">AR balance</div>
    <div style="font-size:20px;font-weight:600">{{ '%.2f'|format(ledger.ar_balance) }}</div>
    <div style="font-size:11px;color:#888">they owe you</div></div>
  <div class="kpi"><div style="font-size:12px;color:#666">AP balance</div>
    <div style="font-size:20px;font-weight:600">{{ '%.2f'|format(ledger.ap_balance) }}</div>
    <div style="font-size:11px;color:#888">you owe them</div></div>
  <div class="kpi"><div style="font-size:12px;color:#666">Net position</div>
    <div style="font-size:20px;font-weight:600" class="{{ 'warn' if ledger.net_position < 0 else '' }}">
      {{ '%.2f'|format(ledger.net_position) }}</div>
    <div style="font-size:11px;color:#888">AR &minus; AP</div></div>
  <div class="kpi"><div style="font-size:12px;color:#666">Entries</div>
    <div style="font-size:20px;font-weight:600">{{ ledger.entries|length }}</div>
    <div style="font-size:11px;color:#888">since {{ ledger.first_date or '—' }}</div></div>
</div>
<h2>Combined ledger</h2>
<p style="font-size:13px;color:#666">
  Both roles in one chronological view, straight from ERPNext's GL Entry table.
  <b>Direction</b> is what the role implies — a Customer is a source of money,
  a Supplier is a use of it. Click a voucher to open it in ERPNext.
</p>
<table>
  <tr><th>Date</th><th>Role</th><th>Dir</th><th>Account</th>
      <th class="num">Debit</th><th class="num">Credit</th>
      <th class="num">Amount</th><th>Voucher</th></tr>
  {% for e in ledger.entries %}
  <tr>
    <td>{{ e.posting_date or '—' }}</td>
    <td>{% if e.role == 'Customer' %}<span class="pill pill-ok">Customer</span>
        {% else %}<span class="pill pill-warn">Supplier</span>{% endif %}</td>
    <td>{% if e.direction == 'IN' %}<span style="color:#1b5e20;font-weight:600">&darr; IN</span>
        {% else %}<span style="color:#a04000;font-weight:600">&uarr; OUT</span>{% endif %}</td>
    <td style="font-size:12px">{{ e.account }}</td>
    <td class="num">{{ '%.2f'|format(e.debit) }}</td>
    <td class="num">{{ '%.2f'|format(e.credit) }}</td>
    <td class="num"><b>{{ '%.2f'|format(e.amount) }}</b></td>
    <td style="font-size:12px">
      {% if erp_base and e.voucher_type and e.voucher_no %}
      <a href="{{ erp_base }}/app/{{ e.voucher_type|lower|replace(' ', '-') }}/{{ e.voucher_no|urlencode }}"
         target="_blank" rel="noopener">{{ e.voucher_no }}</a>
      {% else %}{{ e.voucher_no or '—' }}{% endif %}
    </td>
  </tr>
  {% endfor %}
  {% if not ledger.entries %}
  <tr><td colspan="8" style="color:#888">No ledger activity for either role yet.</td></tr>
  {% endif %}
</table>
"""


@bp.get('/admin/counterparties/<path:name>')
def counterparty_detail_page(name):
    client = _cp_client()
    cp = counterparty.get_counterparty(client, name) if client else None
    if cp is None:
        return redirect('/admin/counterparties?flash=' + quote_plus(
            f'No counterparty named “{name}”'))
    ledger = counterparty.combined_ledger(client, cp,
                                          company=_current_company())
    return _page(COUNTERPARTY_DETAIL_BODY, page='counterparties', cp=cp,
                 ledger=ledger, role_pills=_role_pills,
                 erp_base=_erpnext_base())


# ── reports ──────────────────────────────────────────────────────

AGED_BODY = """
<h2>Aged counterparty balances</h2>
<p style="font-size:14px;color:#555">
  <a href="/admin/counterparties">&larr; All counterparties</a> &nbsp;·&nbsp;
  <a href="/api/counterparties/reports/aged">JSON</a>
</p>
<div class="banner-warn">
  <h3>This ages GL entries by posting date, not invoices by due date</h3>
  <p style="font-size:14px;margin:0">
    Bank Bridge posts Journal Entries, which have no due date and no invoice
    behind them — so ERPNext's own Accounts Receivable Summary shows a farm that
    reconciles bank activity almost nothing. This report answers "how old is
    this money?" instead. It will disagree with ERPNext's ageing for any party
    you also invoice properly, and that's expected, not a bug.
  </p>
</div>
<table>
  <tr><th>Counterparty</th><th>Type</th>
      {% for label, lo, hi in buckets %}<th class="num">{{ label }}</th>{% endfor %}
      <th class="num">AR</th><th class="num">AP</th><th class="num">Net</th></tr>
  {% for r in rows %}
  <tr>
    <td><a href="/admin/counterparties/{{ r.counterparty|urlencode }}">{{ r.counterparty }}</a>
      {% if r.dual_role %}<span class="pill pill-ok">dual</span>{% endif %}</td>
    <td style="font-size:12px">{{ r.counterparty_type or '—' }}</td>
    {% for label, lo, hi in buckets %}
    <td class="num">{{ '%.2f'|format(r.buckets[label]) }}</td>
    {% endfor %}
    <td class="num">{{ '%.2f'|format(r.ar_balance) }}</td>
    <td class="num">{{ '%.2f'|format(r.ap_balance) }}</td>
    <td class="num {{ 'warn' if r.net < 0 else '' }}"><b>{{ '%.2f'|format(r.net) }}</b></td>
  </tr>
  {% endfor %}
  {% if not rows %}<tr><td colspan="{{ buckets|length + 5 }}" style="color:#888">
    No counterparty balances to age yet.</td></tr>{% endif %}
</table>
"""


@bp.get('/admin/counterparties/reports/aged')
def counterparty_aged_page():
    rows = counterparty.aged_balances(_cp_client(), company=_current_company())
    return _page(AGED_BODY, page='counterparties', rows=rows,
                 buckets=counterparty.AGING_BUCKETS)


NEC_1099_BODY = """
<h2>1099-eligible counterparties</h2>
<p style="font-size:14px;color:#555">
  <a href="/admin/counterparties">&larr; All counterparties</a> &nbsp;·&nbsp;
  <a href="/api/counterparties/reports/1099">JSON</a>
</p>
<p style="font-size:14px;color:#555">
  Counterparties you <b>pay</b> (a Supplier link exists) whose type is neither
  Financial Institution nor Government. This is the list "every Supplier" should
  have been all along — it can't accidentally include your bank or the IRS,
  because those are typed, not guessed at, when the party is created.
</p>
<div class="banner-warn">
  <h3>A candidate list, not a filing</h3>
  <p style="font-size:14px;margin:0">
    The $600 threshold, W-9 status and the corporation exemption are judgement
    calls a human makes. The AP activity column is here so that judgement has a
    number to start from — it is cached by the nightly rollup, so check it
    against ERPNext before filing anything.
  </p>
</div>
<table>
  <tr><th>Counterparty</th><th>Type</th><th>ERPNext Supplier</th>
      <th class="num">AP activity</th></tr>
  {% for r in rows %}
  <tr>
    <td><a href="/admin/counterparties/{{ r.counterparty|urlencode }}">{{ r.counterparty }}</a>
      {% if r.dual_role %}<span class="pill pill-ok">dual</span>{% endif %}</td>
    <td>{{ r.counterparty_type or '—' }}</td>
    <td><code>{{ r.supplier_link }}</code></td>
    <td class="num">{{ '%.2f'|format(r.total_activity_ap) }}</td>
  </tr>
  {% endfor %}
  {% if not rows %}<tr><td colspan="4" style="color:#888">
    No 1099-eligible counterparties.</td></tr>{% endif %}
</table>
{% if excluded %}
<h2>Excluded from the list</h2>
<p style="font-size:13px;color:#666">
  Suppliers the filter deliberately dropped, with the reason. Shown because a
  report that silently omits rows is one you can't trust.
</p>
<table>
  <tr><th>Counterparty</th><th>Excluded because type is</th></tr>
  {% for r in excluded %}
  <tr><td>{{ r.counterparty }}</td>
      <td><span class="pill pill-muted">{{ r.counterparty_type }}</span></td></tr>
  {% endfor %}
</table>
{% endif %}
"""


@bp.get('/admin/counterparties/reports/1099')
def counterparty_1099_page():
    client = _cp_client()
    return _page(NEC_1099_BODY, page='counterparties',
                 rows=counterparty.nec_1099_candidates(client),
                 excluded=counterparty.excluded_1099_counterparties(client))


TOP_BODY = """
<h2>Top counterparties by activity</h2>
<p style="font-size:14px;color:#555">
  <a href="/admin/counterparties">&larr; All counterparties</a> &nbsp;·&nbsp;
  <a href="/api/counterparties/reports/top">JSON</a> &nbsp;·&nbsp;
  Fiscal year from <b>{{ fy_start }}</b>
</p>
<p style="font-size:14px;color:#555">
  Ranked by combined AR + AP <b>volume</b> — total money moved, not balance
  outstanding. A customer who always pays on time would rank last on any
  balance-based measure while being one of the most important parties you have.
</p>
<table>
  <tr><th>#</th><th>Counterparty</th><th>Type</th>
      <th class="num">AR volume</th><th class="num">AP volume</th>
      <th class="num">Combined</th><th class="num">Entries</th></tr>
  {% for r in rows %}
  <tr>
    <td class="num">{{ loop.index }}</td>
    <td><a href="/admin/counterparties/{{ r.counterparty|urlencode }}">{{ r.counterparty }}</a>
      {% if r.dual_role %}<span class="pill pill-ok">dual</span>{% endif %}</td>
    <td style="font-size:12px">{{ r.counterparty_type or '—' }}</td>
    <td class="num">{{ '%.2f'|format(r.ar_volume) }}</td>
    <td class="num">{{ '%.2f'|format(r.ap_volume) }}</td>
    <td class="num"><b>{{ '%.2f'|format(r.total_volume) }}</b></td>
    <td class="num">{{ r.entry_count }}</td>
  </tr>
  {% endfor %}
  {% if not rows %}<tr><td colspan="7" style="color:#888">
    No counterparty activity this fiscal year.</td></tr>{% endif %}
</table>
"""


@bp.get('/admin/counterparties/reports/top')
def counterparty_top_page():
    rows = counterparty.top_by_activity(_cp_client(), company=_current_company())
    return _page(TOP_BODY, page='counterparties', rows=rows,
                 fy_start=counterparty.fiscal_year_start().isoformat())


# ── JSON report endpoints ────────────────────────────────────────
#
# The same three reports as data, for anyone who would rather pull them into a
# spreadsheet than read a table. Same code path as the HTML pages — these are
# not a second implementation that can drift.

@bp.get('/api/counterparties/reports/aged')
def counterparty_aged_api():
    return jsonify({'buckets': [b[0] for b in counterparty.AGING_BUCKETS],
                    'rows': counterparty.aged_balances(
                        _cp_client(), company=_current_company())})


@bp.get('/api/counterparties/reports/1099')
def counterparty_1099_api():
    client = _cp_client()
    return jsonify({
        'excluded_types': sorted(counterparty.NON_1099_TYPES),
        'rows': counterparty.nec_1099_candidates(client),
        'excluded': counterparty.excluded_1099_counterparties(client)})


@bp.get('/api/counterparties/reports/top')
def counterparty_top_api():
    return jsonify({
        'fiscal_year_start': counterparty.fiscal_year_start().isoformat(),
        'rows': counterparty.top_by_activity(_cp_client(),
                                             company=_current_company())})


# ── v0.5.2 · Phase E: Investment Advisory Agreement dashboard ────────────────

ADVISORY_LIST_BODY = """
<h2>Investment Advisory Agreements</h2>
<p style="font-size:14px;color:#555;max-width:820px">
  Each agreement's fee accrual, performance and compliance are computed and
  stored here, so the quarterly reporting is a review of what Bank Bridge worked
  out — not a recomputation. Fee posting to ERPNext is opt-in per agreement.
</p>
{% if not agreements %}
<div class="banner-warn"><h3>No agreements yet</h3>
  An Investment Advisory Agreement records the fee and performance terms between
  a Client (an ERPNext Company) and a Manager. None is configured.</div>
{% else %}
<table>
  <tr><th>Agreement</th><th>Client</th><th>Manager</th>
      <th class="num">AUM</th><th>Fee posting</th><th>Status</th></tr>
  {% for a in agreements %}
  <tr>
    <td><a href="/admin/advisory/{{ a.id }}">{{ a.name or 'Agreement #' ~ a.id }}</a></td>
    <td>{{ a.client_company }}</td>
    <td>{{ a.manager_name }}</td>
    <td class="num">{{ '%.2f'|format(aum_by_id.get(a.id, 0.0)) }}</td>
    <td>{% if a.fee_accrual_enabled %}<span class="pill pill-ok">on</span>
        {% else %}<span class="pill pill-muted">off</span>{% endif %}</td>
    <td>{{ a.status }}</td>
  </tr>
  {% endfor %}
</table>
{% endif %}
"""


ADVISORY_DASHBOARD_BODY = """
<h2>{{ d.agreement.name or 'Advisory Agreement' }}</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
{% if d.risk_violations %}
<div class="banner-warn" style="border-left-color:#b71c1c">
  <h3 style="color:#b71c1c">&#9888; {{ d.risk_violations|length }} risk-control
    violation(s) as of {{ d.risk_check_date }}</h3>
  <ul style="margin:6px 0 0;font-size:13px">
    {% for v in d.risk_violations %}
    <li><b>{{ v.ticker }}</b> at {{ '%.2f'|format(v.pct) }}% (limit
      {{ '%.1f'|format(v.limit) }}%) — {{ v.action }}</li>
    {% endfor %}
  </ul>
</div>
{% endif %}

<div class="card" style="max-width:900px">
  <p style="font-size:14px;margin:0">
    <b>Client:</b> {{ d.agreement.client_company }}
    · <b>Manager:</b> {{ d.agreement.manager_name }}
    · <b>Base fee:</b> {{ '%.4f'|format(d.agreement.total_base_fee_rate) }}
      (bank {{ '%.4f'|format(d.agreement.bank_fee_rate) }},
       manager {{ '%.4f'|format(d.agreement.manager_base_fee_rate) }})
    · <b>Performance fee:</b> {{ '%.2f'|format(d.agreement.performance_fee_rate) }}
    · <b>Hurdle:</b> {{ d.agreement.hurdle_benchmark }}
  </p>
</div>

<div class="kpis" style="margin:12px 0">
  <div class="kpi"><b>{{ '%.2f'|format(d.aum) }}</b><br>
    <span style="font-size:12px;color:#666">current AUM</span></div>
  <div class="kpi"><b>{{ '%.2f'|format(d.ytd_base_fee) }}</b><br>
    <span style="font-size:12px;color:#666">YTD base fee accrued</span></div>
  <div class="kpi"><b>{{ '%.2f'|format(d.ytd_performance_fee) }}</b><br>
    <span style="font-size:12px;color:#666">YTD performance fee accrued</span></div>
  <div class="kpi"><b>{{ '%.2f'|format(d.high_water_mark) }}</b><br>
    <span style="font-size:12px;color:#666">high-water mark</span></div>
</div>

{% if d.latest_snapshot %}
{% set s = d.latest_snapshot %}
<h3>{{ s.period_label }} performance</h3>
<table style="max-width:700px">
  <tr><td>Opening AUM</td><td class="num">{{ '%.2f'|format(s.opening_aum) }}</td></tr>
  <tr><td>Closing AUM</td><td class="num">{{ '%.2f'|format(s.closing_aum) }}</td></tr>
  <tr><td>Portfolio return</td><td class="num">{{ '%.2f'|format(s.net_return_pct) }}%</td></tr>
  <tr><td>Hurdle return</td><td class="num">{{ '%.2f'|format(s.hurdle_return_pct) }}%</td></tr>
  <tr><td>Excess return</td><td class="num">{{ '%+.2f'|format(s.excess_return_pct) }}%</td></tr>
  <tr><td>Above high-water mark?</td>
      <td class="num">{{ 'yes' if s.above_high_water_mark else 'no' }}</td></tr>
  <tr style="font-weight:600"><td>Performance fee accrued</td>
      <td class="num" style="color:{{ '#1b5e20' if s.performance_fee_accrued > 0 else '#666' }}">
        {{ '%.2f'|format(s.performance_fee_accrued) }}</td></tr>
  <tr><td colspan="2" style="font-size:12px;color:#666">{{ s.notes }}</td></tr>
</table>
{% endif %}

{% if d.high_water_marks %}
<h3>High-water mark timeline</h3>
<table style="max-width:520px;font-size:13px">
  <tr><th>Date</th><th>Period</th><th class="num">Mark</th></tr>
  {% for m in d.high_water_marks %}
  <tr><td>{{ m.mark_date }}</td><td>{{ m.established_by_period }}</td>
      <td class="num">{{ '%.2f'|format(m.mark_value) }}</td></tr>
  {% endfor %}
</table>
{% endif %}

<h3>Fee posting controls</h3>
<p style="font-size:13px;color:#555;max-width:820px">
  Each switch is OFF by default. Accruals and the performance math run
  regardless — the switch gates only whether a Journal Entry reaches the
  Client's books. Nothing hits the P&amp;L without you turning it on.
</p>
{% for key, label, on in [
    ('fee_accrual_enabled', 'Base fee JE posting', d.agreement.fee_accrual_enabled),
    ('performance_fee_enabled', 'Performance fee posting', d.agreement.performance_fee_enabled),
    ('risk_control_alerts_enabled', 'Risk-control alerts', d.agreement.risk_control_alerts_enabled)] %}
<form method="post" action="/admin/advisory/{{ d.agreement.id }}/toggle"
      style="display:inline-flex;gap:8px;align-items:center;margin:0 12px 8px 0">
  <input type="hidden" name="switch" value="{{ key }}">
  <input type="hidden" name="enabled" value="{{ '0' if on else '1' }}">
  <span style="font-size:13px">{{ label }}:
    <b style="color:{{ '#1b5e20' if on else '#a04000' }}">{{ 'ON' if on else 'OFF' }}</b></span>
  <button type="submit" class="secondary" style="padding:3px 10px;font-size:12px">
    {{ 'Turn off' if on else 'Turn on' }}</button>
</form>
{% endfor %}

<h3>Fee accruals</h3>
<table>
  <tr><th>Date</th><th>Type</th><th>Period</th><th class="num">Amount</th>
      <th>Posted to ERPNext</th></tr>
  {% for a in d.accruals %}
  <tr>
    <td>{{ a.accrual_date }}</td><td>{{ a.fee_type }}</td>
    <td>{{ a.period_label }}</td>
    <td class="num">{{ '%.2f'|format(a.amount) }}</td>
    <td>{% if a.posted_to_erpnext %}<span class="pill pill-ok">posted
        {{ a.erpnext_je_id }}</span>{% else %}
        <span class="pill pill-muted">not posted</span>
        <br><span style="font-size:11px;color:#888">{{ a.notes }}</span>{% endif %}</td>
  </tr>
  {% endfor %}
  {% if not d.accruals %}
  <tr><td colspan="5" style="color:#888">No accruals recorded yet.</td></tr>
  {% endif %}
</table>
<p style="font-size:13px"><a href="/admin/advisory">&larr; all agreements</a></p>
"""


@bp.get('/admin/advisory')
def advisory_list():
    """Every advisory agreement, with current AUM."""
    from .. import advisory
    from ..models import AdvisoryAgreement
    agreements = (AdvisoryAgreement.query
                  .order_by(AdvisoryAgreement.name).all())
    aum_by_id = {a.id: advisory.agreement_aum(a) for a in agreements}
    return _page(ADVISORY_LIST_BODY, page='advisory',
                 agreements=agreements, aum_by_id=aum_by_id)


@bp.get('/admin/advisory/<int:agreement_id>')
def advisory_dashboard(agreement_id):
    """One agreement's dashboard — a render of stored figures, not a
    recomputation (the design test for this whole feature)."""
    from .. import advisory
    from ..models import AdvisoryAgreement
    agreement = db.session.get(AdvisoryAgreement, agreement_id)
    if agreement is None:
        return redirect('/admin/advisory?flash=' + quote_plus('No such agreement.'))
    return _page(ADVISORY_DASHBOARD_BODY, page='advisory',
                 d=advisory.dashboard(agreement),
                 flash_msg=request.args.get('flash', ''))


@bp.post('/admin/advisory/<int:agreement_id>/toggle')
def advisory_toggle(agreement_id):
    """Flip one of the three per-agreement kill switches. Nothing is posted
    retroactively — the switch gates the NEXT settlement/accrual/alert."""
    from ..models import AdvisoryAgreement
    agreement = db.session.get(AdvisoryAgreement, agreement_id)
    if agreement is None:
        return redirect('/admin/advisory?flash=' + quote_plus('No such agreement.'))
    switch = (request.form.get('switch') or '').strip()
    if switch not in ('fee_accrual_enabled', 'performance_fee_enabled',
                      'risk_control_alerts_enabled'):
        return redirect(f'/admin/advisory/{agreement_id}?flash='
                        + quote_plus('Unknown switch.'))
    enabled = request.form.get('enabled') == '1'
    before = getattr(agreement, switch)
    setattr(agreement, switch, enabled)
    db.session.commit()
    audit.record('advisory_switch_toggled', subject_type='AdvisoryAgreement',
                 subject_id=agreement.id,
                 before={switch: before}, after={switch: enabled},
                 notes=f'{switch} {"enabled" if enabled else "disabled"}')
    return redirect(f'/admin/advisory/{agreement_id}?flash='
                    + quote_plus(f'{switch} is now {"ON" if enabled else "OFF"}.'))
