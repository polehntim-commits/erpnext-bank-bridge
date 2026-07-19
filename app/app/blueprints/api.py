# SPDX-License-Identifier: MIT
"""JSON + OAuth-handoff routes for the Plaid Link flow and manual sync.

  GET/POST /bankbridge/api/plaid/create_link_token → { link_token }
  POST     /bankbridge/api/plaid/exchange_token    → exchange public_token → PlaidItem
  GET      /bankbridge/plaid/oauth_return         → OAuth handoff page (re-inits Link)
  POST     /bankbridge/api/plaid/webhook          → Plaid webhook receiver (optional)
  POST     /api/sync/plaid_now                    → run the full sync now

v0.4.8 moved every Plaid-facing path under `/bankbridge/` so this app can share
a Tailscale Funnel hostname with other Umbrel apps; app/legacy_paths.py keeps
the old paths working via permanent redirects. `/api/sync/plaid_now` is an
admin-UI button, not a Plaid callback, so it keeps its path.

The admin UI is unauthenticated + LAN-only (the Umbrel trust boundary), so
these carry no auth either. Nothing here echoes an access_token or a Plaid
secret."""
import logging

from flask import (Blueprint, current_app, jsonify, render_template_string,
                   request, session)

from .. import audit
from .. import db
from .. import crypto
from .. import plaid_settings
from .. import reconnect
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


@bp.route('/bankbridge/api/plaid/create_link_token', methods=['GET', 'POST'])
def create_link_token():
    """Mint a Plaid Link token. Persists it so the OAuth return page can
    re-initialize Link with the SAME token (required for OAuth banks)."""
    if not plaid_settings.is_configured():
        return jsonify({'error': 'Plaid is not configured. Set your Client ID '
                        'and secret in Plaid settings first.'}), 400
    # v0.4.11 · `item_id` switches Link into UPDATE MODE for an Item that
    # already exists — repairing it in place instead of creating a second one.
    # This is what a Reconnect button asks for, and it is both the correct and
    # the cheaper path: the Item keeps its id and all its account_ids (so every
    # mapping survives), and no second billable Item is created at Plaid.
    data = request.get_json(silent=True) or request.form or {}
    item_id = (data.get('item_id') or request.args.get('item_id') or '').strip()
    access_token = None
    if item_id:
        item = PlaidItem.query.filter_by(item_id=item_id).first()
        if item is None:
            return jsonify({'error': 'unknown item_id'}), 404
        if item.disconnected:
            return jsonify({'error': 'that bank was disconnected; link it as '
                                     'a new connection instead'}), 409
        try:
            access_token = crypto.decrypt(item.access_token_encrypted)
        except Exception:
            return jsonify({'error': 'stored credentials for that bank could '
                                     'not be read; link it as a new '
                                     'connection instead'}), 500
    try:
        client = sync_engine.get_plaid_client()
        # v0.4.9 · also request the `statements` product, so the linked Item can
        # serve bank-issued statement PDFs. Safe to ask for unconditionally:
        # create_link_token retries WITHOUT it if Plaid says no (the product
        # isn't enabled on the application, or the institution doesn't offer
        # it), so linking a bank keeps working exactly as it did pre-v0.4.9 and
        # starts carrying statements on the next link after Plaid approves them.
        #
        # In update mode the statements flag is ignored: Plaid rejects a
        # products list alongside an access_token, because an Item's products
        # are fixed when it is created.
        link_token = client.create_link_token(
            user_id='erpnext-bank-bridge',
            statements=current_app.config.get('STATEMENTS_ENABLED', True),
            # v0.4.14 · also request `liabilities`, so a linked mortgage or
            # student loan can report its own interest and principal figures.
            # Degrades the same way statements does: an application without the
            # product still links banks, and still gets statements.
            liabilities=current_app.config.get('LOANS_ENABLED', True),
            access_token=access_token)
    except (PlaidError, PlaidConfigError) as e:
        return jsonify({'error': str(e)}), 502
    try:
        db.session.add(PlaidLinkState(link_token=link_token))
        _prune_link_states()
        db.session.commit()
    except Exception:  # pragma: no cover - non-fatal bookkeeping
        db.session.rollback()
    return jsonify({'link_token': link_token, 'update_mode': bool(access_token)})


@bp.post('/bankbridge/api/plaid/reconnect_complete')
def reconnect_complete():
    """Called by the Reconnect page when update-mode Link succeeds (v0.4.11).

    There is no token to exchange. That is the whole point of update mode: the
    Item and its access_token are the ones we already hold, so a successful Link
    means the existing credentials work again. All that remains is to clear the
    parked flag so the poll loop picks the Item back up.

    Idempotent — clearing an already-clear flag is a no-op — because Link's
    onSuccess can fire more than once across a retry."""
    data = request.get_json(silent=True) or request.form or {}
    item_id = (data.get('item_id') or '').strip()
    if not item_id:
        return jsonify({'error': 'item_id required'}), 400
    item = PlaidItem.query.filter_by(item_id=item_id).first()
    if item is None:
        return jsonify({'error': 'unknown item_id'}), 404
    cleared = reconnect.clear_reauth(item)
    if cleared:
        audit.record('item_reconnected', subject_type='PlaidItem',
                     subject_id=item_id, after={'needs_reauth': False},
                     notes='update-mode reconnect succeeded')
    # Refresh accounts immediately: a reconnect is also how NEW_ACCOUNTS_AVAILABLE
    # gets resolved, and the operator expects to see them without waiting for
    # the next poll. Best-effort — the flag is already cleared either way.
    try:
        client = sync_engine.get_plaid_client()
        sync_engine.refresh_accounts(
            item, client, crypto.decrypt(item.access_token_encrypted))
    except Exception as e:  # pragma: no cover - never fail the reconnect
        log.info('post-reconnect account refresh failed for %s: %s', item_id, e)
    return jsonify({'ok': True, 'item_id': item_id, 'was_parked': cleared})


SESSION_OWNING_COMPANY_KEY = 'link_owning_company'


@bp.post('/bankbridge/api/plaid/set_link_company')
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


@bp.post('/bankbridge/api/plaid/exchange_token')
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
      fetch('/bankbridge/api/plaid/exchange_token', {
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


@bp.get('/bankbridge/plaid/oauth_return')
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


@bp.post('/bankbridge/api/plaid/webhook')
def plaid_webhook():
    """Optional Plaid webhook receiver. For the polling pilot this is a
    convenience: on a TRANSACTIONS update we kick a sync for the named Item so
    fresh transactions land sooner than the 6-hour poll. Best-effort and
    unauthenticated (LAN-only); Plaid webhook verification can be added later."""
    data = request.get_json(silent=True) or {}
    webhook_type = (data.get('webhook_type') or '').upper()
    webhook_code = (data.get('webhook_code') or '').upper()
    item_id = data.get('item_id') or ''
    log.info('plaid webhook: %s / %s', webhook_type, webhook_code)

    # v0.4.11 · ITEM webhooks are the free, instant signal that a bank wants the
    # operator to sign in again. Before this release they were logged and
    # dropped, so the first anyone knew was that transactions had stopped —
    # while the poll loop kept paying for a failed call every cycle.
    #
    # Handling one costs NOTHING at Plaid: it sets a flag on a row and makes
    # zero API calls. It is cost-NEGATIVE, because parking the Item stops the
    # doomed polling (see sync_engine.sync_all).
    if webhook_type == 'ITEM' and item_id:
        item = PlaidItem.query.filter_by(item_id=item_id).first()
        if item is not None and not item.disconnected:
            code = webhook_code
            if code == 'ERROR':
                # The interesting part of an ERROR webhook is the nested error
                # code — a plain 'ERROR' says nothing actionable.
                nested = (data.get('error') or {})
                code = (nested.get('error_code') or '').upper() or code
            if code in reconnect.REAUTH_WEBHOOK_CODES:
                reconnect.mark_needs_reauth(item, code, source='webhook')
                audit.record('item_needs_reauth', subject_type='PlaidItem',
                             subject_id=item_id, after={'code': code},
                             notes=f'Plaid ITEM webhook: {code}')
        return jsonify({'ok': True})

    # v0.4.11 · the TRANSACTIONS branch kicks a full sync, which DOES cost Plaid
    # calls beyond the scheduled poll. That has been the behaviour since the
    # pilot, so it stays on by default — but an operator watching their bill can
    # now turn it off and keep the (free) ITEM handling above.
    if (webhook_type == 'TRANSACTIONS'
            and not current_app.config.get('PLAID_WEBHOOK_TRIGGERS_SYNC', True)):
        log.info('plaid webhook: TRANSACTIONS sync kick disabled '
                 '(PLAID_WEBHOOK_TRIGGERS_SYNC=false) — the scheduled poll '
                 'will pick these up')
        return jsonify({'ok': True})
    if webhook_type == 'TRANSACTIONS' and item_id:
        item = PlaidItem.query.filter_by(item_id=item_id).first()
        # v0.4.7 · a disconnected Item is skipped here for the same reason
        # sync_all skips it: its access_token no longer exists at Plaid. This
        # endpoint is unauthenticated, so the check also stops a spoofed webhook
        # from provoking doomed Plaid calls for a bank the operator disconnected.
        if (item is not None and item.status != 'revoked'
                and not item.disconnected):
            try:
                sync_engine.sync_item(item)
            except (PlaidError, PlaidConfigError) as e:
                log.warning('webhook-triggered sync failed: %s', e)
    return jsonify({'ok': True})
