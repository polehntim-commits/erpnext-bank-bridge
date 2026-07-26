# SPDX-License-Identifier: MIT
"""Model Context Protocol server (v0.6.0).

Bolts an MCP endpoint onto the existing Flask app so an AI client can query
Bank Bridge — and, behind per-tool kill switches, mutate it — directly, instead
of a human relaying `docker exec` snippets. Every diagnostic/fix cycle from
v0.4.42 → v0.5.7 paid the same relay tax; this removes it.

TRANSPORT. The MCP wire protocol is JSON-RPC 2.0. Bank Bridge is a synchronous
Flask/WSGI app, so rather than bolt the asyncio `mcp` server runtime into WSGI
(fragile), this implements the JSON-RPC methods an MCP client calls — `initialize`,
`tools/list`, `tools/call` — directly on one POST route. That is exactly the
Streamable-HTTP transport's request/response surface, so any MCP client speaks
to it unchanged. (The `mcp` SDK is still a declared dependency for client-side
and reference use.)

PATH PREFIX. Mounted at `/mcp`, a Bank-Bridge-scoped prefix per the multi-app
convention, so it cannot collide with another Umbrel app on the shared tailnet
host. It is LAN-only and deliberately NOT wired into the `/bankbridge/` Funnel
prefix — nothing here is meant to be publicly reachable.

SECURITY. Two gates (see mcp_settings): a bearer token in the environment
(absent → 404, the feature does not exist) and a per-mutating-tool kill switch
(all default OFF). Read tools never mutate and are never gated. Every tool call,
allowed or blocked, is written to AiActionLog.
"""
import functools
import json
import logging
from datetime import date

from flask import Blueprint, current_app, jsonify, request

from .. import db
from .. import mcp_settings
from .. import statements as stmts
from ..models import (AiActionLog, BankTransaction, CategorizationRule,
                      PlaidAccount, PlaidItem, PlaidStatement,
                      StatementAnchor, StatementTransaction)

log = logging.getLogger('bankbridge.mcp')

bp = Blueprint('mcp_server', __name__)

PROTOCOL_VERSION = '2024-11-05'
SERVER_NAME = 'bank-bridge'


# ── errors a tool raises to signal a clean, client-visible failure ──────────
class ToolError(Exception):
    """A tool couldn't do what was asked for an expected reason (unknown mask,
    kill switch off). Surfaced to the client as an MCP tool error, logged with
    ok=False — never a 500."""


# ── helpers shared by the tool handlers ─────────────────────────────────────
def _account_by_mask(mask: str) -> PlaidAccount:
    """Resolve a 4-digit mask to the account that OWNS a reconciliation — the
    paired brokerage when there is one, else any account carrying that mask.
    Raises ToolError if nothing matches."""
    mask = (mask or '').strip()
    if not mask:
        raise ToolError('account_mask is required')
    matches = PlaidAccount.query.filter_by(mask=mask).all()
    if not matches:
        raise ToolError(f'no account with mask {mask!r}')
    # Prefer a paired brokerage (it holds the anchor chain); else the first.
    for a in matches:
        if (a.paired_account_id or '').strip():
            return a
    return matches[0]


def _period_bounds(period: str) -> tuple:
    """('YYYY-MM') → (first_of_month, last_of_month). Raises ToolError on a
    malformed value."""
    try:
        year, month = (int(x) for x in (period or '').split('-')[:2])
        start = date(year, month, 1)
        end = (date(year + (month == 12), (month % 12) + 1, 1)
               - (date.resolution))
        return start, end
    except (ValueError, TypeError):
        raise ToolError(f'period must be YYYY-MM, got {period!r}')


def _paired_mask(account: PlaidAccount) -> str:
    if not (account.paired_account_id or '').strip():
        return ''
    partner = PlaidAccount.query.filter_by(
        account_id=account.paired_account_id).first()
    return (partner.mask if partner else '') or ''


# ── read-only tools ─────────────────────────────────────────────────────────
def _get_reconciliation_status(args: dict):
    account = _account_by_mask(args.get('account_mask'))
    anchors = stmts.anchors_for_account(account.account_id)
    ps = args.get('period_start')
    pe = args.get('period_end')
    rows = []
    for a in anchors:
        if ps and (a.period_start is None or a.period_start.isoformat() < ps):
            continue
        if pe and (a.period_end is None or a.period_end.isoformat() > pe):
            continue
        rows.append(a.to_dict())
    result = {
        'account_mask': account.mask,
        'account_id': account.account_id,
        'summary': stmts.anchor_summary(account.account_id),
        'anchors': rows,
    }
    return result, (f'{account.mask}: {len(rows)} anchor(s), '
                    f"variance {result['summary']['variance']}")


def _get_account_topology(args: dict):
    accounts = (PlaidAccount.query
                .order_by(PlaidAccount.name.asc()).all())
    anchored = {a.account_id for a in stmts.accounts_with_anchors()}
    out = []
    for a in accounts:
        out.append({
            'mask': a.mask, 'name': a.name, 'type': a.type,
            'subtype': a.subtype, 'owning_company': a.owning_company,
            'paired_account_mask': _paired_mask(a),
            'superseded_by_account_id': a.superseded_by_account_id,
            'sync_enabled': bool(a.sync_enabled),
            'import_status': a.import_status or 'pending',
            'has_anchor_chain': a.account_id in anchored,
        })
    return {'accounts': out, 'count': len(out)}, f'{len(out)} account(s)'


def _get_variance_breakdown(args: dict):
    account = _account_by_mask(args.get('account_mask'))
    start, end = _period_bounds(args.get('period'))
    statement = (PlaidStatement.query
                 .filter(PlaidStatement.plaid_account_id == account.account_id,
                         PlaidStatement.period_start == start).first())
    anchor = _anchor_dict_for(account, statement)
    # Cash lives on the companion chain (or the account itself when unpaired).
    chain = stmts.supersede_chain(
        (account.paired_account_id or '').strip() or account.account_id)
    eff = stmts._effective_date_col()
    bts = (BankTransaction.query
           .filter(BankTransaction.account_id.in_(tuple(chain)),
                   eff >= start, eff <= end,
                   BankTransaction.pending.is_(False),
                   BankTransaction.removed.is_(False)).all())
    if len(chain) > 1:
        bts = stmts.dedupe_across_accounts(bts)
    bank_rows = [{
        'date': (t.statement_posted_date or t.date).isoformat()
        if (t.statement_posted_date or t.date) else None,
        'plaid_date': t.date.isoformat() if t.date else None,
        'amount': t.amount, 'name': t.name,
        'match_status': t.statement_match_status or '',
        'source': t.source or 'plaid',
    } for t in bts]
    st_rows = []
    if statement is not None:
        for s in (StatementTransaction.query
                  .filter_by(statement_id=statement.id)
                  .order_by(StatementTransaction.sequence).all()):
            st_rows.append({
                'posted_date': s.posted_date.isoformat()
                if s.posted_date else None,
                'amount': s.amount, 'plaid_convention_amount': round(-s.amount, 2),
                'description': s.description, 'section': s.section,
                'match_status': s.match_status,
            })
    result = {
        'account_mask': account.mask, 'period': args.get('period'),
        'anchor': anchor,
        'bank_transactions': bank_rows,
        'statement_transactions': st_rows,
    }
    return result, (f'{account.mask} {args.get("period")}: '
                    f'{len(bank_rows)} bank / {len(st_rows)} statement rows')


def _anchor_dict_for(account, statement):
    if statement is None:
        return None
    anc = StatementAnchor.query.filter_by(statement_id=statement.id).first()
    return anc.to_dict() if anc else stmts._anchor_values(statement, account)


def _list_unreconciled_statements(args: dict):
    tol = stmts.reconcile_tolerance()
    rows = []
    for account in stmts.accounts_with_anchors():
        for a in stmts.anchors_for_account(account.account_id):
            if a.variance is not None and abs(a.variance) > tol:
                d = a.to_dict()
                d['account_mask'] = account.mask
                rows.append(d)
    rows.sort(key=lambda d: abs(d.get('variance') or 0.0), reverse=True)
    return {'unreconciled': rows, 'count': len(rows),
            'tolerance': tol}, f'{len(rows)} unreconciled period(s)'


def _list_rules(args: dict):
    active_only = args.get('active_only', True)
    q = CategorizationRule.query.filter(CategorizationRule.archived.is_(False))
    if active_only:
        q = q.filter(CategorizationRule.active.is_(True))
    rules = q.order_by(CategorizationRule.priority.asc(),
                       CategorizationRule.id.asc()).all()
    slim = [{
        'id': r.id, 'name': r.name, 'priority': r.priority,
        'active': bool(r.active), 'match_type': r.match_type,
        'match_value': r.match_value, 'offset_account': r.offset_account or '',
        'bb_internal_tag': r.bb_internal_tag or '',
        'applies_to_company': r.applies_to_company or None,
    } for r in rules]
    return {'rules': slim, 'count': len(slim)}, f'{len(slim)} rule(s)'


def _list_unmatched_statement_transactions(args: dict):
    account = _account_by_mask(args.get('account_mask'))
    st_ids = [s.id for s in PlaidStatement.query.filter_by(
        plaid_account_id=account.account_id).all()]
    q = StatementTransaction.query.filter(
        StatementTransaction.statement_id.in_(tuple(st_ids) or (-1,)),
        StatementTransaction.match_status == 'no_match')
    if args.get('period'):
        start, end = _period_bounds(args.get('period'))
        q = q.filter(StatementTransaction.posted_date >= start,
                     StatementTransaction.posted_date <= end)
    rows = [{
        'posted_date': s.posted_date.isoformat() if s.posted_date else None,
        'amount': s.amount, 'description': s.description, 'section': s.section,
    } for s in q.order_by(StatementTransaction.posted_date.asc()).all()]
    return {'account_mask': account.mask, 'unmatched': rows,
            'count': len(rows)}, f'{account.mask}: {len(rows)} no_match line(s)'


def _get_advisory_agreement_summary(args: dict):
    from ..models import AdvisoryAgreement
    from .. import advisory
    aid = args.get('agreement_id')
    if aid:
        ag = AdvisoryAgreement.query.get(int(aid))
        if ag is None:
            raise ToolError(f'no advisory agreement id {aid}')
        return advisory.dashboard(ag), f'agreement {aid} dashboard'
    ags = AdvisoryAgreement.query.all()
    out = [{'id': a.id, 'name': getattr(a, 'name', ''),
            'aum': advisory.agreement_aum(a)} for a in ags]
    return {'agreements': out, 'count': len(out)}, f'{len(out)} agreement(s)'


# ── mutating tools (each gated by a kill switch, all default OFF) ────────────
def _create_rule(args: dict):
    rule = CategorizationRule(
        match_type=(args.get('match_type') or 'merchant_contains'),
        match_value=(args.get('match_value') or ''),
        offset_account=(args.get('offset_account') or ''),
        bb_internal_tag=(args.get('tag') or ''),
        name=(args.get('name') or args.get('match_value') or 'AI rule'))
    db.session.add(rule)
    db.session.commit()
    return {'created_rule': rule.to_dict()}, f'created rule {rule.id}'


def _set_variance_tag(args: dict):
    anchor = StatementAnchor.query.get(int(args.get('anchor_id')))
    if anchor is None:
        raise ToolError(f"no anchor id {args.get('anchor_id')}")
    anchor.variance_reason = (args.get('reason') or '')
    db.session.commit()
    return {'anchor': anchor.to_dict()}, f'tagged anchor {anchor.id}'


def _trigger_reparse(args: dict):
    result = stmts.reparse_stored()
    return {'reparse': result}, f"reparsed: {result.get('examined', 0)} examined"


def _rebuild_anchors(args: dict):
    account_id = None
    if args.get('account_mask'):
        account_id = _account_by_mask(args.get('account_mask')).account_id
    result = stmts.rebuild_statement_anchors(account_id)
    return {'rebuild': result}, f"anchors written: {result.get('written', 0)}"


def _pair_accounts(args: dict):
    brk = _account_by_mask(args.get('brokerage_mask'))
    cash = _account_by_mask(args.get('cash_services_mask'))
    brk.paired_account_id = cash.account_id
    db.session.commit()
    return ({'brokerage_mask': brk.mask, 'cash_services_mask': cash.mask},
            f'paired {brk.mask} → {cash.mask}')


def _enable_je_posting(args: dict):
    return _set_je_posting(args, True)


def _disable_je_posting(args: dict):
    return _set_je_posting(args, False)


# ── v0.7.1 · the public OAuth callback (Tailscale sidecar) ──────────────────
def _get_public_url_status(args: dict):
    """The tri-state the /admin/plaid_settings wizard renders, as data."""
    from .. import funnel
    d = funnel.detect()
    sc = d['sidecar']
    result = {
        'mode': d['mode'],
        'sidecar_present': bool(sc.get('present')),
        'sidecar_authenticated': bool(sc.get('authenticated')),
        'funnel_active': bool(sc.get('funnel_active')),
        'hostname': d['hostname'],
        'hostname_source': d['source'],
        'public_url': d['base_url'] or None,
        'redirect_uri': d['redirect_uri'] or None,
        'saved_redirect_uri': d['current_redirect_uri'] or None,
        'redirect_uri_matches': d['redirect_uri_matches'],
        'localapi_ok': bool(sc.get('localapi_ok')),
        'backend_state': sc.get('backend_state') or '',
        # Present only when the sidecar is unauthenticated: a one-time browser
        # login link. Relaying it is the fastest fix an assistant can offer, and
        # it is not a credential — it authorizes nothing without the operator
        # approving this machine in their own Tailscale session.
        'auth_url': sc.get('auth_url') or None,
    }
    return result, (f"mode={result['mode']} funnel_active="
                    f"{result['funnel_active']} host="
                    f"{result['hostname'] or '(unknown)'}")


def _test_public_url(args: dict):
    """HEAD the callback and report reachability. Read-only."""
    from .. import funnel
    d = funnel.detect()
    if not d['hostname']:
        raise ToolError('no public hostname is configured — nothing to test')
    probe = funnel.probe(d['hostname'])
    result = {'url': probe['url'], 'ok': probe['ok'],
              'reachable': probe['reachable'], 'status': probe['status'],
              'detail': probe['detail'],
              'funnel_active': bool(d['sidecar'].get('funnel_active'))}
    return result, f"{probe['url']}: {probe['detail']}"


def _enable_public_url(args: dict):
    """Enable Funnel for the OAuth callback and save the redirect URI."""
    from .. import audit
    from .. import funnel
    r = funnel.enable_public_url()
    if not r['ok']:
        raise ToolError(r['detail'])
    if r.get('saved'):
        audit.record('plaid_public_url_saved',
                     after={'redirect_uri': r['url'],
                            'funnel_hostname': r['hostname'],
                            'source': 'tailscale_sidecar'},
                     notes='MCP enable_public_url', actor='mcp')
    return ({'url': r['url'], 'hostname': r['hostname'],
             'saved_as_redirect_uri': r['saved'], 'detail': r['detail'],
             'register_in_plaid_dashboard': r['url']},
            r['detail'])


def _disable_public_url(args: dict):
    """Withdraw the public callback. Leaves PLAID_REDIRECT_URI in place."""
    from .. import audit
    from .. import funnel
    r = funnel.disable_public_url()
    if not r['ok']:
        raise ToolError(r['detail'])
    audit.record('plaid_public_url_disabled',
                 before={'funnel_active': True},
                 after={'funnel_active': False},
                 notes='MCP disable_public_url', actor='mcp')
    return ({'funnel_active': False, 'detail': r['detail'],
             'saved_redirect_uri_unchanged': True}, r['detail'])


def _set_je_posting(args: dict, on: bool):
    item = PlaidItem.query.filter_by(item_id=(args.get('item_id') or '')).first()
    if item is None:
        raise ToolError(f"no item {args.get('item_id')}")
    item.invest_je_posting_enabled = on
    db.session.commit()
    return ({'item_id': item.item_id, 'invest_je_posting_enabled': on},
            f"{'enabled' if on else 'disabled'} JE posting for {item.item_id}")


# ── the tool registry — name → schema + handler + mutation flag ─────────────
def _tool(description, properties, required=(), *, mutating=False):
    return {'description': description, 'mutating': mutating,
            'inputSchema': {'type': 'object', 'properties': properties,
                            'required': list(required)}}


_STR = {'type': 'string'}
_BOOL = {'type': 'boolean'}

TOOLS = {
    'get_reconciliation_status': {
        **_tool(
            'Return the statement-anchor reconciliation chain for one account '
            '(by 4-digit mask): per-period variance, transaction_sum, opening/'
            'closing balances, chain gaps and any variance reason. Use this to '
            "answer 'does account NNNN reconcile in month X'. Read-only.",
            {'account_mask': _STR,
             'period_start': {'type': 'string',
                              'description': 'ISO date lower bound (inclusive)'},
             'period_end': {'type': 'string',
                            'description': 'ISO date upper bound (inclusive)'}},
            required=('account_mask',)),
        'handler': _get_reconciliation_status},
    'get_account_topology': {
        **_tool(
            'List every Plaid account with its mask, type, owning company, '
            'paired cash-services mask, sync state and whether it has an anchor '
            'chain — the same picture /admin/accounts renders. Read-only.',
            {}),
        'handler': _get_account_topology},
    'get_variance_breakdown': {
        **_tool(
            'For one account and month (period="YYYY-MM"), list every '
            'BankTransaction and StatementTransaction contributing to that '
            "period's variance, with amounts, effective dates and match "
            'status. Use to diagnose WHY a period does not reconcile. Read-only.',
            {'account_mask': _STR,
             'period': {'type': 'string', 'description': 'YYYY-MM'}},
            required=('account_mask', 'period')),
        'handler': _get_variance_breakdown},
    'list_unreconciled_statements': {
        **_tool(
            'List every statement period whose anchor variance exceeds the '
            'reconcile tolerance, across all accounts, ordered by absolute '
            'variance descending. The worklist of what still needs explaining. '
            'Read-only.',
            {}),
        'handler': _list_unreconciled_statements},
    'list_rules': {
        **_tool(
            'List categorization rules (match_type, match_value, offset_account, '
            'internal tag, priority). Set active_only=false to include inactive '
            'rules. Read-only.',
            {'active_only': _BOOL}),
        'handler': _list_rules},
    'list_unmatched_statement_transactions': {
        **_tool(
            'List an account\'s StatementTransactions with match_status='
            "'no_match' (statement lines Plaid never returned), optionally for "
            'one month (period="YYYY-MM"). Read-only.',
            {'account_mask': _STR, 'period': {'type': 'string',
                                              'description': 'YYYY-MM'}},
            required=('account_mask',)),
        'handler': _list_unmatched_statement_transactions},
    'get_advisory_agreement_summary': {
        **_tool(
            'Advisory-agreement dashboard data (fees, AUM, performance, risk). '
            'Pass agreement_id for one, or omit for a list of all agreements. '
            'Read-only.',
            {'agreement_id': {'type': 'integer'}}),
        'handler': _get_advisory_agreement_summary},
    'get_public_url_status': {
        **_tool(
            'Whether the Plaid OAuth callback is reachable from the public '
            'Internet, and how. Returns mode (sidecar_funnel | sidecar_ready | '
            'sidecar_unauth | manual | none), whether the Tailscale sidecar is '
            'present and authenticated, whether Funnel is active, the public '
            'hostname and the redirect URI Plaid must have registered. '
            'Read-only.',
            {}),
        'handler': _get_public_url_status},
    'test_public_url': {
        **_tool(
            'HEAD the public OAuth callback URL and report whether it answers '
            '(200), redirects, 404s, or is unreachable. Note an unreachable '
            'result is inconclusive — a Funnel is reached from the public '
            'Internet and this probe runs inside the container. Read-only.',
            {}),
        'handler': _test_public_url},

    'create_rule': {
        **_tool(
            'Create a categorization rule. MUTATING — requires the create_rule '
            'kill switch to be ON.',
            {'match_type': _STR, 'match_value': _STR, 'offset_account': _STR,
             'tag': _STR, 'name': _STR},
            required=('match_type', 'match_value'), mutating=True),
        'handler': _create_rule},
    'set_variance_tag': {
        **_tool(
            'Set the human-readable variance_reason on one statement anchor. '
            'MUTATING — requires the set_variance_tag kill switch ON.',
            {'anchor_id': {'type': 'integer'}, 'reason': _STR},
            required=('anchor_id', 'reason'), mutating=True),
        'handler': _set_variance_tag},
    'trigger_reparse': {
        **_tool(
            'Re-parse stored statement PDFs and run the full reconciliation '
            'pipeline. MUTATING — requires the trigger_reparse kill switch ON.',
            {}, mutating=True),
        'handler': _trigger_reparse},
    'rebuild_anchors': {
        **_tool(
            'Rebuild the statement-anchor chain for one account (by mask) or '
            'all accounts. MUTATING — requires the rebuild_anchors kill switch '
            'ON.',
            {'account_mask': _STR}, mutating=True),
        'handler': _rebuild_anchors},
    'pair_accounts': {
        **_tool(
            'Pair a brokerage account with its cash-services companion (both by '
            'mask). MUTATING — requires the pair_accounts kill switch ON.',
            {'brokerage_mask': _STR, 'cash_services_mask': _STR},
            required=('brokerage_mask', 'cash_services_mask'), mutating=True),
        'handler': _pair_accounts},
    'enable_je_posting': {
        **_tool(
            'Turn ON investment Journal-Entry posting for a Plaid item. '
            'MUTATING — requires the enable_je_posting kill switch ON.',
            {'item_id': _STR}, required=('item_id',), mutating=True),
        'handler': _enable_je_posting},
    'disable_je_posting': {
        **_tool(
            'Turn OFF investment Journal-Entry posting for a Plaid item. '
            'MUTATING — requires the disable_je_posting kill switch ON.',
            {'item_id': _STR}, required=('item_id',), mutating=True),
        'handler': _disable_je_posting},
    'enable_public_url': {
        **_tool(
            'Publish the Plaid OAuth callback over Tailscale Funnel and save the '
            'resulting HTTPS URL as this install\'s PLAID_REDIRECT_URI. Only '
            '/bankbridge/plaid/oauth_return is published; the admin UI and the '
            'Plaid write endpoints stay on the LAN. The operator must still '
            'register the returned URL in their Plaid dashboard — that is '
            'outside this system. MUTATING, and it changes what the INTERNET can '
            'reach — requires the enable_public_url kill switch ON.',
            {}, mutating=True),
        'handler': _enable_public_url},
    'disable_public_url': {
        **_tool(
            'Stop serving the Plaid OAuth callback publicly. Leaves the saved '
            'PLAID_REDIRECT_URI untouched (it is still what the Plaid dashboard '
            'has registered), so re-enabling needs no dashboard edit — but OAuth '
            'bank links cannot complete while it is off. MUTATING — requires the '
            'disable_public_url kill switch ON.',
            {}, mutating=True),
        'handler': _disable_public_url},
}


# ── audit ───────────────────────────────────────────────────────────────────
def _log(tool_name: str, args: dict, summary: str, ok: bool) -> None:
    """Append one AiActionLog row. Best-effort — an audit-write failure must
    never break (or mask) the tool result."""
    try:
        db.session.add(AiActionLog(
            tool_name=tool_name,
            args=json.dumps(args, default=str)[:8000],
            result_summary=(summary or '')[:2000],
            caller_ip=(request.remote_addr or ''), ok=ok))
        db.session.commit()
    except Exception:  # pragma: no cover - never propagate
        db.session.rollback()
        log.warning('AiActionLog write failed for %s', tool_name, exc_info=True)


def dispatch_tool(tool_name: str, args: dict) -> dict:
    """Run one tool by name and return an MCP tools/call result. Logs every
    call. Never raises — a ToolError or unexpected error becomes an MCP error
    result (isError), not an HTTP 500."""
    args = args or {}
    spec = TOOLS.get(tool_name)
    if spec is None:
        _log(tool_name, args, 'unknown tool', ok=False)
        return _tool_error_result(f'unknown tool {tool_name!r}')
    # Kill-switch gate for mutating tools.
    if spec['mutating'] and not mcp_settings.is_tool_enabled(tool_name):
        msg = (f"tool '{tool_name}' is a mutating tool and its kill switch is "
               f'OFF — enable it on /admin/mcp first')
        _log(tool_name, args, 'blocked: kill switch off', ok=False)
        return _tool_error_result(msg)
    try:
        result, summary = spec['handler'](args)
        _log(tool_name, args, summary, ok=True)
        return _tool_ok_result(result)
    except ToolError as e:
        _log(tool_name, args, f'error: {e}', ok=False)
        return _tool_error_result(str(e))
    except Exception as e:  # pragma: no cover - defensive
        db.session.rollback()
        _log(tool_name, args, f'exception: {type(e).__name__}', ok=False)
        log.warning('MCP tool %s failed', tool_name, exc_info=True)
        return _tool_error_result(f'{type(e).__name__}: {e}')


def _tool_ok_result(result) -> dict:
    return {'content': [{'type': 'text',
                         'text': json.dumps(result, default=str, indent=2)}],
            'isError': False}


def _tool_error_result(message: str) -> dict:
    return {'content': [{'type': 'text', 'text': message}], 'isError': True}


# ── JSON-RPC surface ────────────────────────────────────────────────────────
def _tools_list() -> dict:
    return {'tools': [{'name': name, 'description': spec['description'],
                       'inputSchema': spec['inputSchema']}
                      for name, spec in TOOLS.items()]}


def _rpc_result(req_id, result):
    return {'jsonrpc': '2.0', 'id': req_id, 'result': result}


def _rpc_error(req_id, code, message):
    return {'jsonrpc': '2.0', 'id': req_id,
            'error': {'code': code, 'message': message}}


def _handle_rpc(msg: dict):
    """Handle one JSON-RPC message. Returns a response dict, or None for a
    notification (no id → no reply)."""
    req_id = msg.get('id')
    method = msg.get('method')
    params = msg.get('params') or {}
    if method == 'initialize':
        return _rpc_result(req_id, {
            'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {'tools': {}},
            'serverInfo': {'name': SERVER_NAME,
                           'version': current_app.config.get(
                               'APP_VERSION', '')}})
    if method in ('notifications/initialized', 'initialized'):
        return None  # notification — no response
    if method == 'ping':
        return _rpc_result(req_id, {})
    if method == 'tools/list':
        return _rpc_result(req_id, _tools_list())
    if method == 'tools/call':
        name = params.get('name')
        arguments = params.get('arguments') or {}
        return _rpc_result(req_id, dispatch_tool(name, arguments))
    if req_id is None:
        return None
    return _rpc_error(req_id, -32601, f'method not found: {method}')


@bp.post('/mcp')
def mcp_endpoint():
    """The single MCP JSON-RPC 2.0 endpoint. 404 when no token is configured
    (feature disabled); 401 without a valid bearer token."""
    if not mcp_settings.is_enabled():
        return jsonify({'error': 'not found'}), 404
    token = _bearer_token()
    if not token or token != mcp_settings.auth_token():
        return jsonify(_rpc_error(None, -32001, 'unauthorized')), 401
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify(_rpc_error(None, -32700, 'parse error')), 400
    # A batch (list) or a single message.
    if isinstance(payload, list):
        responses = [r for r in (_handle_rpc(m) for m in payload)
                     if r is not None]
        return jsonify(responses), 200
    response = _handle_rpc(payload)
    if response is None:
        return ('', 202)  # notification accepted, nothing to return
    return jsonify(response), 200


def _bearer_token() -> str:
    header = request.headers.get('Authorization', '')
    if header.lower().startswith('bearer '):
        return header[7:].strip()
    return ''
