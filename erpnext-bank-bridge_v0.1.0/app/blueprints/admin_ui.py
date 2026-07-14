# SPDX-License-Identifier: MIT
"""Minimal HTML admin — deliberately functional, not fancy.
Unauthenticated + LAN-only (the Umbrel trust boundary).

Pages:
  /  and  /admin        — dashboard (health, counts, recent sync log)
  /admin/plaid_settings — Plaid Client ID / secrets / environment / webhook
  /admin/link_bank      — Plaid Link entry point
  /admin/accounts       — map each Plaid account → ERPNext Bank Account
  /admin/transactions   — filterable transaction list + per-row retry
  /admin/erpnext_settings — ERPNext connection (URL + API key/secret)
  /admin/sync_log       — recent PlaidSyncLog rows
"""
from urllib.parse import quote_plus

from flask import (Blueprint, current_app, redirect, render_template_string,
                   request, url_for)

from .. import categorization
from .. import db
from .. import erpnext_accounts
from .. import erpnext_bank
from .. import erpnext_settings as erps
from .. import plaid_settings as ps
from .. import sync_engine
from ..erpnext_client import ERPNextConfigError, ERPNextError
from ..models import (BankTransaction, CategorizationRule,
                      GeneratedJournalEntry, PlaidAccount, PlaidItem,
                      PlaidSyncLog, Supplier)

bp = Blueprint('admin_ui', __name__)


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
  <a href="/admin/generated_entries" class="{{ 'active' if page == 'generated_entries' else '' }}">Generated JEs</a>
  <a href="/admin/sync_log" class="{{ 'active' if page == 'sync_log' else '' }}">Sync Log</a>
  <a href="/admin/plaid_settings" class="{{ 'active' if page == 'plaid_settings' else '' }}">Plaid</a>
  <a href="/admin/erpnext_settings" class="{{ 'active' if page == 'erpnext_settings' else '' }}">ERPNext</a>
</nav>
"""


def _page(body_tmpl: str, page: str, **ctx):
    full = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>ERPNext Bank Bridge</title>
<style>{BASE_CSS}</style></head><body>
{NAV_HTML}
<main>{body_tmpl}</main>
</body></html>"""
    return render_template_string(full, page=page, **ctx)


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

<div class="kpis">
  <div class="kpi"><b>{{ counts.items }}</b><br>linked banks</div>
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
      {% if it.status == 'active' %}<span class="pill pill-ok">active</span>
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
    total_txn = BankTransaction.query.count()
    posted = BankTransaction.query.filter(
        BankTransaction.posted_at.isnot(None),
        BankTransaction.removed.is_(False)).count()
    pending = BankTransaction.query.filter(
        BankTransaction.posted_at.is_(None)).count()
    mapped = PlaidAccount.query.filter(
        PlaidAccount.erpnext_bank_account_name.isnot(None)).count()
    return {
        'items': PlaidItem.query.count(),
        'accounts': PlaidAccount.query.count(),
        'mapped': mapped,
        'transactions': total_txn,
        'posted': posted,
        'pending': pending,
    }


@bp.get('/')
@bp.get('/admin')
def dashboard():
    items = PlaidItem.query.order_by(PlaidItem.created_at.desc()).all()
    recent_log = (PlaidSyncLog.query
                  .order_by(PlaidSyncLog.at.desc()).limit(15).all())
    return _page(DASHBOARD_BODY, page='dashboard', counts=_counts(),
                 items=items, recent_log=recent_log,
                 plaid_ok=ps.is_configured(),
                 erpnext_ok=erps.is_configured(),
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

  fetch('/api/plaid/create_link_token', { method: 'POST' })
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      if (!res.ok) { setStatus(res.j.error || 'Could not create a link token.'); return; }
      var handler = Plaid.create({
        token: res.j.link_token,
        onSuccess: function (public_token) {
          setStatus('Linked! Saving accounts…');
          fetch('/api/plaid/exchange_token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ public_token: public_token })
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
    return _page(LINK_BANK_BODY, page='link_bank', plaid_ok=ps.is_configured(),
                 env=s['environment'], redirect_uri=s['redirect_uri'])


# ── Accounts (ERPNext Bank Account mapping) ──────────────────────

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

{% if groups and not any_imported %}
<div class="banner-warn"><h3>Nothing imported yet</h3>
  Click <b>Import all supported accounts</b> above to bring your Plaid accounts
  into ERPNext in one click — or use the per-row <b>Create in ERPNext</b> button.
</div>
{% endif %}

{% for grp in groups %}
<h3>{{ grp.item.institution_name or '(unknown institution)' }}
  <span style="font-weight:400;color:#888;font-size:13px">· {{ grp.item.item_id[:14] }}…</span></h3>
<table>
  <tr><th>Account</th><th>Mask</th><th>Type</th><th class="num">Balance</th>
      <th>ERPNext Bank Account</th><th>Import</th></tr>
  {% for a in grp.accounts %}
  <tr>
    <td>{{ a.name or a.official_name or '(unnamed)' }}</td>
    <td><code>••{{ a.mask or '??' }}</code></td>
    <td>{{ a.type }}{% if a.subtype %} / {{ a.subtype }}{% endif %}</td>
    <td class="num">{{ '%.2f'|format(a.balance_current) if a.balance_current is not none else '—' }} {{ a.iso_currency_code }}</td>
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
    </td>
  </tr>
  {% endfor %}
</table>
{% endfor %}
{% if not groups %}<p style="color:#888">No accounts yet — <a href="/admin/link_bank">link a bank</a>.</p>{% endif %}
"""


@bp.get('/admin/accounts')
def accounts_page():
    items = PlaidItem.query.order_by(PlaidItem.created_at.desc()).all()
    groups = []
    supported_map = {}
    any_imported = False
    for it in items:
        accts = (PlaidAccount.query.filter_by(item_id=it.item_id)
                 .order_by(PlaidAccount.name).all())
        for a in accts:
            supported_map[a.account_id] = erpnext_accounts.is_supported(a)
            if a.erpnext_bank_account_name:
                any_imported = True
        groups.append({'item': it, 'accounts': accts})
    bank_accounts, erp_error = [], ''
    if erps.is_configured():
        try:
            bank_accounts = erpnext_bank.list_bank_accounts()
        except (ERPNextConfigError, ERPNextError) as e:
            erp_error = str(e)
    bank_account_names = {ba['name'] for ba in bank_accounts}
    return _page(ACCOUNTS_BODY, page='accounts', groups=groups,
                 bank_accounts=bank_accounts, bank_account_names=bank_account_names,
                 supported_map=supported_map, any_imported=any_imported,
                 erpnext_ok=erps.is_configured(),
                 bootstrap_unavailable=sorted(
                     erpnext_accounts.unavailable_doctypes()),
                 erp_error=erp_error, flash_msg=request.args.get('flash', ''))


@bp.post('/admin/accounts/create')
def create_account_in_erpnext():
    """One-click: create (or find) the ERPNext Bank + Bank Account for a single
    Plaid account and map it."""
    account_id = (request.form.get('account_id') or '').strip()
    if not erps.is_configured():
        return redirect('/admin/accounts?flash=' + quote_plus(
            'ERPNext is not configured — set the connection first.'))
    try:
        result = erpnext_accounts.import_plaid_account_to_erpnext(account_id)
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


# ── Transactions ─────────────────────────────────────────────────

TRANSACTIONS_BODY = """
<h2>Transactions</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
<form method="get" action="/admin/transactions" class="card"
      style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
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
  <label style="margin:0">Search
    <input name="q" value="{{ cur_q }}" placeholder="name / merchant">
  </label>
  <button type="submit" class="primary">Filter</button>
</form>

<table>
  <tr><th>Date</th><th>Account</th><th>Description</th><th class="num">Amount</th>
      <th>Status</th><th>ERPNext</th><th></th></tr>
  {% for r in rows %}
  <tr>
    <td>{{ r.date.isoformat() if r.date else '—' }}</td>
    <td><code>••{{ acct_mask.get(r.account_id, '??') }}</code></td>
    <td style="max-width:280px">{{ r.name }}{% if r.merchant_name %} <span style="color:#888">· {{ r.merchant_name }}</span>{% endif %}{% if r.pending %} <span class="pill pill-muted">pending</span>{% endif %}</td>
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
    <td>
      {% if not r.posted_at or r.sync_error %}
      <form method="post" action="/admin/transactions/retry" style="margin:0">
        <input type="hidden" name="id" value="{{ r.id }}">
        <button type="submit" class="secondary" style="padding:3px 10px;font-size:12px">Retry</button>
      </form>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
  {% if not rows %}<tr><td colspan="7" style="color:#888">No transactions match.</td></tr>{% endif %}
</table>
<p style="font-size:12px;color:#888">Showing up to {{ limit }} most recent.</p>
"""


@bp.get('/admin/transactions')
def transactions_page():
    cur_status = (request.args.get('status') or '').strip()
    cur_account = (request.args.get('account_id') or '').strip()
    cur_q = (request.args.get('q') or '').strip()
    limit = 300
    q = BankTransaction.query
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
    rows = q.order_by(BankTransaction.date.desc().nullslast(),
                      BankTransaction.id.desc()).limit(limit).all()
    accounts = PlaidAccount.query.order_by(PlaidAccount.name).all()
    acct_mask = {a.account_id: (a.mask or '??') for a in accounts}
    return _page(TRANSACTIONS_BODY, page='transactions', rows=rows,
                 accounts=accounts, acct_mask=acct_mask, limit=limit,
                 cur_status=cur_status, cur_account=cur_account, cur_q=cur_q,
                 flash_msg=request.args.get('flash', ''))


@bp.post('/admin/transactions/retry')
def retry_transaction():
    raw_id = (request.form.get('id') or '').strip()
    if not raw_id.isdigit():
        return redirect('/admin/transactions?flash=' + quote_plus('Bad row id'))
    ok, msg = sync_engine.retry_row(int(raw_id))
    prefix = 'Retried: ' if ok else 'Retry failed: '
    return redirect('/admin/transactions?flash=' + quote_plus(prefix + msg))


# ── Categorization rules ─────────────────────────────────────────

def _erpnext_account_names() -> list:
    """ERPNext GL Account docnames for the rule debit/credit datalists.
    Best-effort — an empty list (ERPNext down / unconfigured) just means the
    fields are free-text, which still works."""
    if not erps.is_configured():
        return []
    try:
        return [a['name'] for a in erpnext_bank.list_accounts()]
    except (ERPNextConfigError, ERPNextError):
        return []


RULES_BODY = """
<h2>Categorization rules</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
{% if not je_engine_on %}
<div class="banner-warn">
  <h3>Journal-entry generation is OFF</h3>
  Rules can be authored + tested here, but they won't fire until
  <code>ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES=true</code> is set. This is
  deliberate — an incorrect auto-JE is worse than none. Auto-Supplier creation
  is independent and {{ 'ON' if supplier_on else 'OFF' }}.
</div>
{% endif %}

<h3>{{ 'Edit rule #' ~ form.id if form.id else 'Add a rule' }}</h3>
<form class="card" method="post" action="/admin/rules/save">
  <input type="hidden" name="id" value="{{ form.id or '' }}">
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <label style="flex:2;min-width:180px">Name
      <input name="name" value="{{ form.name or '' }}" placeholder="Fuel — Chevron">
    </label>
    <label style="flex:1;min-width:90px">Priority
      <input name="priority" type="number" value="{{ form.priority if form.priority is not none else 100 }}">
    </label>
    <label style="flex:1;min-width:120px;display:flex;align-items:center;gap:6px;margin-top:22px">
      <input type="checkbox" name="active" value="1" {{ 'checked' if form.active else '' }} style="width:auto"> active
    </label>
  </div>
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <label style="flex:1;min-width:180px">Match type
      <select name="match_type">
        {% for mt in match_types %}
        <option value="{{ mt }}" {{ 'selected' if mt == form.match_type else '' }}>{{ mt }}</option>
        {% endfor %}
      </select>
    </label>
    <label style="flex:2;min-width:200px">Match value
      <input name="match_value" value="{{ form.match_value or '' }}"
             placeholder="Chevron   ·   ^UBER   ·   [10, 500] for amount_range">
    </label>
  </div>
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <label style="flex:1;min-width:200px">Debit account (expense / party side)
      <input name="debit_account" list="acctlist" value="{{ form.debit_account or '' }}" placeholder="Fuel Expense - EC">
    </label>
    <label style="flex:1;min-width:200px">Credit account (usually the Bank Account)
      <input name="credit_account" list="acctlist" value="{{ form.credit_account or '' }}" placeholder="Checking - EC">
    </label>
  </div>
  <datalist id="acctlist">
    {% for n in account_names %}<option value="{{ n }}">{% endfor %}
  </datalist>
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <label style="flex:1;min-width:150px">Party type
      <select name="party_type">
        <option value="" {{ 'selected' if not form.party_type else '' }}>— none —</option>
        <option value="Supplier" {{ 'selected' if form.party_type == 'Supplier' else '' }}>Supplier</option>
        <option value="Customer" {{ 'selected' if form.party_type == 'Customer' else '' }}>Customer</option>
      </select>
    </label>
    <label style="flex:2;min-width:200px">Party name <span style="font-weight:400;color:#888">(blank → auto-Supplier for the merchant)</span>
      <input name="party_name" value="{{ form.party_name or '' }}" placeholder="(optional)">
    </label>
  </div>
  <label>Description template (Jinja) <span style="font-weight:400;color:#888">— vars: merchant_name, name, date, amount, category, supplier_name</span>
    <input name="description_template" value="{{ form.description_template or '' }}"
           placeholder="Fuel purchase - {{ '{{ merchant_name }}' }} - {{ '{{ date }}' }}">
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

<h3>Rules ({{ rules|length }})</h3>
<table>
  <tr><th class="num">Prio</th><th>Name</th><th>Match</th><th>Debit</th><th>Credit</th>
      <th>Party</th><th>Active</th><th></th></tr>
  {% for r in rules %}
  <tr>
    <td class="num">{{ r.priority }}</td>
    <td>{{ r.name }}</td>
    <td style="font-size:12px"><code>{{ r.match_type }}</code><br>{{ (r.match_value or '')[:50] }}</td>
    <td style="font-size:12px">{{ r.debit_account }}</td>
    <td style="font-size:12px">{{ r.credit_account }}</td>
    <td style="font-size:12px">{{ (r.party_type or '') }}{% if r.party_name %}: {{ r.party_name }}{% endif %}</td>
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
            onsubmit="return confirm('Delete rule {{ r.name }}?')">
        <input type="hidden" name="id" value="{{ r.id }}">
        <button type="submit" class="secondary" style="padding:3px 10px;font-size:12px">Delete</button>
      </form>
    </td>
  </tr>
  {% endfor %}
  {% if not rules %}<tr><td colspan="8" style="color:#888">No rules yet — add one above.</td></tr>{% endif %}
</table>
"""


def _rules_page(flash_msg='', test_result=None, test=None, form=None):
    rules = (CategorizationRule.query
             .order_by(CategorizationRule.priority.asc(),
                       CategorizationRule.id.asc()).all())
    return _page(RULES_BODY, page='rules', rules=rules,
                 match_types=categorization.MATCH_TYPES,
                 account_names=_erpnext_account_names(),
                 form=form or {'active': True, 'priority': 100},
                 test=test or {}, test_result=test_result,
                 je_engine_on=current_app.config.get(
                     'ERPNEXT_AUTO_GENERATE_JOURNAL_ENTRIES', False),
                 supplier_on=current_app.config.get(
                     'ERPNEXT_AUTO_CREATE_SUPPLIERS', True),
                 flash_msg=flash_msg)


@bp.get('/admin/rules')
def rules_page():
    form = {'active': True, 'priority': 100}
    edit_id = (request.args.get('edit') or '').strip()
    if edit_id.isdigit():
        rule = db.session.get(CategorizationRule, int(edit_id))
        if rule is not None:
            form = rule.to_dict()
    return _rules_page(flash_msg=request.args.get('flash', ''), form=form)


def _rule_form_values():
    """Pull + sanitize the shared rule fields from the POST form."""
    raw_prio = (request.form.get('priority') or '').strip()
    try:
        priority = int(raw_prio)
    except ValueError:
        priority = 100
    party_type = (request.form.get('party_type') or '').strip() or None
    return {
        'name': (request.form.get('name') or '').strip(),
        'priority': priority,
        'active': bool(request.form.get('active')),
        'match_type': (request.form.get('match_type') or 'merchant_contains').strip(),
        'match_value': (request.form.get('match_value') or '').strip(),
        'debit_account': (request.form.get('debit_account') or '').strip(),
        'credit_account': (request.form.get('credit_account') or '').strip(),
        'party_type': party_type,
        'party_name': (request.form.get('party_name') or '').strip() or None,
        'description_template': (request.form.get('description_template') or '').strip(),
    }


@bp.post('/admin/rules/save')
def save_rule():
    vals = _rule_form_values()
    if vals['match_type'] not in categorization.MATCH_TYPES:
        return redirect('/admin/rules?flash=' + quote_plus(
            f"Unknown match type '{vals['match_type']}'"))
    raw_id = (request.form.get('id') or '').strip()
    if raw_id.isdigit():
        rule = db.session.get(CategorizationRule, int(raw_id))
        if rule is None:
            return redirect('/admin/rules?flash=' + quote_plus('Rule not found'))
        for k, v in vals.items():
            setattr(rule, k, v)
        msg = f'Updated rule “{rule.name}”'
    else:
        rule = CategorizationRule(**vals)
        db.session.add(rule)
        msg = f'Added rule “{vals["name"]}”'
    db.session.commit()
    return redirect('/admin/rules?flash=' + quote_plus(msg))


@bp.post('/admin/rules/delete')
def delete_rule():
    raw_id = (request.form.get('id') or '').strip()
    if raw_id.isdigit():
        rule = db.session.get(CategorizationRule, int(raw_id))
        if rule is not None:
            db.session.delete(rule)
            db.session.commit()
    return redirect('/admin/rules?flash=' + quote_plus('Rule deleted'))


@bp.post('/admin/rules/toggle')
def toggle_rule():
    raw_id = (request.form.get('id') or '').strip()
    if raw_id.isdigit():
        rule = db.session.get(CategorizationRule, int(raw_id))
        if rule is not None:
            rule.active = not rule.active
            db.session.commit()
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
        category=test['category'], amount=amount, date=None,
        plaid_transaction_id='(sample)',
        erpnext_bank_transaction_id='(the Bank Transaction)')
    rule = categorization.find_matching_rule(sample)
    result = {'matched': rule is not None, 'rule': rule}
    if rule is not None:
        import json as _json
        remark = categorization.render_description(rule, sample)
        doc = categorization.build_journal_entry(
            rule, sample, erps.load().get('default_company', '(company)'),
            remark=remark)
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
    return redirect('/admin/suppliers?flash=' + quote_plus('Supplier updated'))


# ── Generated Journal Entries (audit) ────────────────────────────

GENERATED_BODY = """
<h2>Generated Journal Entries</h2>
{% if flash_msg %}<div class="creds"><b>{{ flash_msg }}</b></div>{% endif %}
<p style="font-size:14px;color:#555">
  Audit trail of Journal Entries the rules engine created. <b>Approve</b> submits
  a Draft JE in ERPNext; <b>Reject</b> cancels it. State reflects the local
  audit record.
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

<div style="margin:8px 0">
  <form method="post" action="/admin/generated_entries/bulk" style="display:inline">
    <input type="hidden" name="action" value="approve">
    <button type="submit" class="secondary">Approve all pending</button>
  </form>
  <form method="post" action="/admin/generated_entries/bulk" style="display:inline;margin-left:8px">
    <input type="hidden" name="action" value="reject">
    <button type="submit" class="secondary">Reject all pending</button>
  </form>
</div>

<table>
  <tr><th>Created</th><th>Merchant</th><th class="num">Amount</th><th>Rule</th>
      <th>Journal Entry</th><th>State</th><th></th></tr>
  {% for g in rows %}
  <tr>
    <td style="font-size:12px">{{ g.created_at.strftime('%Y-%m-%d %H:%M') if g.created_at else '' }}</td>
    <td>{{ g.merchant_name }}<div style="font-size:11px;color:#888">{{ (g.description or '')[:60] }}</div></td>
    <td class="num">{{ '%.2f'|format(g.amount or 0.0) }}</td>
    <td style="font-size:12px">{{ g.rule_name }}</td>
    <td style="font-size:12px">{% if g.erpnext_journal_entry_name %}<code>{{ g.erpnext_journal_entry_name }}</code>{% else %}—{% endif %}
      {% if g.error_message %}<div style="color:#a04000">{{ g.error_message[:100] }}</div>{% endif %}</td>
    <td>
      {% if g.state == 'approved' %}<span class="pill pill-ok">approved</span>
      {% elif g.state == 'rejected' %}<span class="pill pill-muted">rejected</span>
      {% elif g.state == 'error' %}<span class="pill pill-err">error</span>
      {% else %}<span class="pill pill-muted">{{ g.state }}</span>{% endif %}
    </td>
    <td style="white-space:nowrap">
      {% if g.erpnext_journal_entry_name and g.state not in ('approved',) %}
      <form method="post" action="/admin/generated_entries/approve" style="display:inline;margin:0">
        <input type="hidden" name="id" value="{{ g.id }}">
        <button type="submit" class="secondary" style="padding:3px 10px;font-size:12px">Approve</button>
      </form>
      {% endif %}
      {% if g.erpnext_journal_entry_name and g.state not in ('rejected',) %}
      <form method="post" action="/admin/generated_entries/reject" style="display:inline;margin:0">
        <input type="hidden" name="id" value="{{ g.id }}">
        <button type="submit" class="secondary" style="padding:3px 10px;font-size:12px">Reject</button>
      </form>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
  {% if not rows %}<tr><td colspan="7" style="color:#888">No generated entries yet.</td></tr>{% endif %}
</table>
<p style="font-size:12px;color:#888">Showing up to {{ limit }} most recent.</p>
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
                 states=('pending_review', 'approved', 'rejected', 'error'),
                 flash_msg=request.args.get('flash', ''))


def _approve_entry(g) -> bool:
    """Submit a Draft JE in ERPNext + mark approved. Returns True on success."""
    if not g.erpnext_journal_entry_name:
        return False
    erp = sync_engine.get_erp_client_or_none()
    if erp is None:
        return False
    try:
        categorization._submit_je(erp, g.erpnext_journal_entry_name)
    except (ERPNextConfigError, ERPNextError):
        return False
    g.state = 'approved'
    g.updated_at = categorization._now()
    return True


def _reject_entry(g) -> bool:
    """Cancel the JE in ERPNext + mark rejected. Returns True on success."""
    g.state = 'rejected'
    g.updated_at = categorization._now()
    if not g.erpnext_journal_entry_name:
        return True
    erp = sync_engine.get_erp_client_or_none()
    if erp is not None:
        try:
            erp.call_method('frappe.client.cancel', http_method='POST',
                            json_body={'doctype': categorization.JOURNAL_ENTRY_DT,
                                       'name': g.erpnext_journal_entry_name})
        except (ERPNextConfigError, ERPNextError):
            pass  # audit still flips to rejected; JE may already be draft/cancelled
    return True


@bp.post('/admin/generated_entries/approve')
def approve_entry():
    raw_id = (request.form.get('id') or '').strip()
    if not raw_id.isdigit():
        return redirect('/admin/generated_entries?flash=' + quote_plus('Bad id'))
    g = db.session.get(GeneratedJournalEntry, int(raw_id))
    if g is None:
        return redirect('/admin/generated_entries?flash=' + quote_plus('Not found'))
    ok = _approve_entry(g)
    db.session.commit()
    msg = 'Approved (submitted in ERPNext)' if ok else 'Could not submit — check ERPNext connection'
    return redirect('/admin/generated_entries?flash=' + quote_plus(msg))


@bp.post('/admin/generated_entries/reject')
def reject_entry():
    raw_id = (request.form.get('id') or '').strip()
    if not raw_id.isdigit():
        return redirect('/admin/generated_entries?flash=' + quote_plus('Bad id'))
    g = db.session.get(GeneratedJournalEntry, int(raw_id))
    if g is None:
        return redirect('/admin/generated_entries?flash=' + quote_plus('Not found'))
    _reject_entry(g)
    db.session.commit()
    return redirect('/admin/generated_entries?flash=' + quote_plus('Rejected'))


@bp.post('/admin/generated_entries/bulk')
def bulk_entries():
    action = (request.form.get('action') or '').strip()
    pending = GeneratedJournalEntry.query.filter(
        GeneratedJournalEntry.state == 'pending_review',
        GeneratedJournalEntry.erpnext_journal_entry_name.isnot(None)).all()
    n = 0
    for g in pending:
        if action == 'approve' and _approve_entry(g):
            n += 1
        elif action == 'reject' and _reject_entry(g):
            n += 1
    db.session.commit()
    verb = 'Approved' if action == 'approve' else 'Rejected'
    return redirect('/admin/generated_entries?flash=' + quote_plus(f'{verb} {n} entr(ies)'))


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
    <input name="redirect_uri" value="{{ s.redirect_uri }}" placeholder="http://umbrel.local:5202/plaid/oauth_return">
  </label>
  <label>Webhook URL <span style="font-weight:400;color:#888">(optional — leave blank for polling)</span>
    <input name="webhook_url" value="{{ s.webhook_url }}" placeholder="http://umbrel.local:5202/api/plaid/webhook">
  </label>
  <button type="submit">Save Plaid settings</button>
</form>
<p style="font-size:13px">Status:
  {% if configured %}<span class="pill pill-ok">configured ({{ s.environment }})</span>
  {% else %}<span class="pill pill-err">not configured</span>{% endif %}
</p>
"""


@bp.get('/admin/plaid_settings')
def plaid_settings_page():
    return _page(PLAID_SETTINGS_BODY, page='plaid_settings', s=ps.load(),
                 masked=ps.masked(), configured=ps.is_configured(),
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
    ps.save(client_id, environment, redirect_uri, webhook_url,
            sandbox_secret=sandbox_secret, production_secret=production_secret)
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
        f"Account Subtypes: {mark(status.get(erpnext_accounts.ACCOUNT_SUBTYPE_DT))}",
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
    Account Subtype link targets, and the custom fields (plaid_account_id,
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
              'Account Subtype records, and custom fields (plaid_account_id, '
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
