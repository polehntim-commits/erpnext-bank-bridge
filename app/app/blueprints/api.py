# SPDX-License-Identifier: MIT
"""JSON + OAuth-handoff routes for the Plaid Link flow and manual sync.

  GET/POST /api/plaid/create_link_token   → { link_token }
  POST     /api/plaid/exchange_token       → exchange public_token → PlaidItem
  GET      /plaid/oauth_return             → OAuth handoff page (re-inits Link)
  POST     /api/sync/plaid_now             → run the full sync now
  POST     /api/plaid/webhook              → Plaid webhook receiver (optional)

The admin UI is unauthenticated + LAN-only (the Umbrel trust boundary), so
these carry no auth either. Nothing here echoes an access_token or a Plaid
secret."""
import logging

from flask import (Blueprint, current_app, jsonify, render_template_string,
                   request, session)

from .. import db
from .. import crypto
from .. import plaid_settings
from .. import sync_engine
from ..models import PlaidAccount, PlaidItem, PlaidLinkState
from ..plaid_client import PlaidError, PlaidConfigError

log = logging.getLogger('bankbridge.api')

bp = Blueprint('api', __name__)


def _prune_link_states() -> None:
    """Keep the link-state table tiny — drop all but the newest 10 rows."""
    old = (PlaidLinkState.query
           .order_by(PlaidLinkState.created_at.desc()).offset(10).all())
    for row in old:
        db.session.delete(row)


@bp.route('/api/plaid/create_link_token', methods=['GET', 'POST'])
def create_link_token():
    """Mint a Plaid Link token. Persists it so the OAuth return page can
    re-initialize Link with the SAME token (required for OAuth banks)."""
    if not plaid_settings.is_configured():
        return jsonify({'error': 'Plaid is not configured. Set your Client ID '
                        'and secret in Plaid settings first.'}), 400
    try:
        client = sync_engine.get_plaid_client()
        link_token = client.create_link_token(user_id='erpnext-bank-bridge')
    except (PlaidError, PlaidConfigError) as e:
        return jsonify({'error': str(e)}), 502
    try:
        db.session.add(PlaidLinkState(link_token=link_token))
        _prune_link_states()
        db.session.commit()
    except Exception:  # pragma: no cover - non-fatal bookkeeping
        db.session.rollback()
    return jsonify({'link_token': link_token})


SESSION_OWNING_COMPANY_KEY = 'link_owning_company'


@bp.post('/api/plaid/set_link_company')
def set_link_company():
    """Remember the owning ERPNext Company the operator picked on the Link page
    (v0.4.0 multi-entity L1), stashed in the Flask session until the exchange
    reads it. Empty clears it (→ resolves to the ERPNext default Company)."""
    data = request.get_json(silent=True) or request.form
    company = (data.get('company') or '').strip()
    if company:
        session[SESSION_OWNING_COMPANY_KEY] = company
    else:
        session.pop(SESSION_OWNING_COMPANY_KEY, None)
    return jsonify({'ok': True, 'owning_company': company})


@bp.post('/api/plaid/exchange_token')
def exchange_token():
    """Exchange a Link public_token for a durable access_token, store the Item
    (access_token encrypted), and pull its accounts. Idempotent on item_id."""
    data = request.get_json(silent=True) or request.form
    public_token = (data.get('public_token') or '').strip()
    if not public_token:
        return jsonify({'error': 'public_token required'}), 400
    try:
        client = sync_engine.get_plaid_client()
        access_token, item_id = client.exchange_public_token(public_token)
    except (PlaidError, PlaidConfigError) as e:
        return jsonify({'error': str(e)}), 502

    item = PlaidItem.query.filter_by(item_id=item_id).first()
    if item is None:
        item = PlaidItem(item_id=item_id)
        db.session.add(item)
    item.access_token_encrypted = crypto.encrypt(access_token)
    item.status = 'active'
    item.last_error = None
    # v0.4.0: stamp the owning Company chosen on the Link page (session-carried).
    # A blank/absent choice leaves it NULL → resolves to the ERPNext default at
    # push time (unchanged single-company behavior). Only set on a fresh link so
    # re-linking an existing Item doesn't silently move its accounts' entity.
    chosen_company = (session.get(SESSION_OWNING_COMPANY_KEY) or '').strip()
    if chosen_company and not item.owning_company:
        item.owning_company = chosen_company
    # Resolve institution (best-effort).
    try:
        meta = client.get_item(access_token)
        inst_id = meta.get('institution_id') or ''
        item.institution_id = inst_id
        if inst_id:
            item.institution_name = client.get_institution_name(inst_id)
    except PlaidError as e:
        log.warning('item/institution lookup failed: %s', e)
    db.session.commit()

    # Pull accounts immediately so the mapping UI has something to show.
    try:
        sync_engine.refresh_accounts(item, client, access_token)
    except PlaidError as e:
        log.warning('initial account pull failed: %s', e)

    accounts = PlaidAccount.query.filter_by(item_id=item_id).count()
    return jsonify({'ok': True, 'item_id': item_id,
                    'institution_name': item.institution_name,
                    'accounts': accounts})


OAUTH_RETURN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Completing bank link…</title>
<style>body{font-family:system-ui,sans-serif;margin:40px;color:#222}
.box{max-width:520px}code{background:#f0f0f0;padding:2px 4px;border-radius:3px}</style>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
</head><body>
<div class="box">
<h2>Finishing your bank connection…</h2>
<p id="status">Reconnecting to Plaid to complete the OAuth handoff.</p>
<p><a href="/admin/link_bank">← Back to Link a bank</a></p>
</div>
{% raw %}
<script>
(function () {
  var statusEl = document.getElementById('status');
  var linkToken = {{ link_token_json | safe }};
  if (!linkToken) {
    statusEl.textContent = 'Missing the original link token — please restart from Link a bank.';
    return;
  }
  var handler = Plaid.create({
    token: linkToken,
    receivedRedirectUri: window.location.href,
    onSuccess: function (public_token) {
      statusEl.textContent = 'Linked! Saving your accounts…';
      fetch('/api/plaid/exchange_token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ public_token: public_token })
      }).then(function (r) { return r.json(); })
        .then(function () { window.location.href = '/admin/accounts'; })
        .catch(function () { statusEl.textContent = 'Saved link, but the exchange failed — check ERPNext/Plaid settings.'; });
    },
    onExit: function (err) {
      statusEl.textContent = err ? ('Link exited: ' + (err.display_message || err.error_message || err.error_code)) : 'Link closed.';
    }
  });
  handler.open();
})();
</script>
{% endraw %}
</body></html>"""


@bp.get('/plaid/oauth_return')
def oauth_return():
    """OAuth redirect target for OAuth-only banks (Wells Fargo). Plaid appends
    an oauth_state_id to this URL; we re-open Link with the original link_token
    and the full current URL as receivedRedirectUri so it can complete."""
    import json as _json
    latest = (PlaidLinkState.query
              .order_by(PlaidLinkState.created_at.desc()).first())
    token = latest.link_token if latest else ''
    return render_template_string(
        OAUTH_RETURN_HTML, link_token_json=_json.dumps(token))


@bp.post('/api/sync/plaid_now')
def sync_now():
    """Admin-triggered full sync across all active Items."""
    if not plaid_settings.is_configured():
        return jsonify({'error': 'Plaid is not configured'}), 400
    try:
        result = sync_engine.sync_all()
    except (PlaidError, PlaidConfigError) as e:
        return jsonify({'error': str(e)}), 502
    return jsonify({'ok': True, 'result': result})


@bp.post('/api/plaid/webhook')
def plaid_webhook():
    """Optional Plaid webhook receiver. For the polling pilot this is a
    convenience: on a TRANSACTIONS update we kick a sync for the named Item so
    fresh transactions land sooner than the 6-hour poll. Best-effort and
    unauthenticated (LAN-only); Plaid webhook verification can be added later."""
    data = request.get_json(silent=True) or {}
    webhook_type = (data.get('webhook_type') or '').upper()
    item_id = data.get('item_id') or ''
    log.info('plaid webhook: %s / %s', webhook_type, data.get('webhook_code'))
    if webhook_type == 'TRANSACTIONS' and item_id:
        item = PlaidItem.query.filter_by(item_id=item_id).first()
        if item is not None and item.status != 'revoked':
            try:
                sync_engine.sync_item(item)
            except (PlaidError, PlaidConfigError) as e:
                log.warning('webhook-triggered sync failed: %s', e)
    return jsonify({'ok': True})
