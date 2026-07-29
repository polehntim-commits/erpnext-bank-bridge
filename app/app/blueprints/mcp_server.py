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
from ..erpnext_client import ERPNextAPIError, ERPNextError
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


# ── v0.8.0 · shared argument coercion for the investment toolkit ────────────
def _iso_date(value, field: str, *, required: bool = True):
    """An ISO 'YYYY-MM-DD' argument as a date. Raises ToolError with the field
    name on anything else — a tool that silently treats an unparseable bound as
    "no bound" answers a question nobody asked."""
    raw = (value or '').strip() if isinstance(value, str) else value
    if not raw:
        if required:
            raise ToolError(f'{field} is required (YYYY-MM-DD)')
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        raise ToolError(f'{field} must be YYYY-MM-DD, got {value!r}')


def _statement_for(account: PlaidAccount, period: str) -> PlaidStatement:
    """The stored statement for one account and period. `period` is either
    'YYYY-MM' (the month the period STARTS in) or an exact 'YYYY-MM-DD'
    matching `period_end`.

    Both spellings because both are natural from different directions: an
    operator reading /admin/statements says "February", while a caller chaining
    off get_reconciliation_status already holds the anchor's exact period_end.
    Raises ToolError naming the periods that DO exist, so a near miss is one
    round-trip to fix rather than a guessing game."""
    raw = (period or '').strip()
    rows = (PlaidStatement.query
            .filter(PlaidStatement.plaid_account_id == account.account_id)
            .order_by(PlaidStatement.period_start.desc().nullslast()).all())
    if not raw:
        raise ToolError('period is required (YYYY-MM or YYYY-MM-DD)')
    match = None
    if len(raw) > 7:                       # an exact period_end
        want = _iso_date(raw, 'period')
        match = next((s for s in rows if s.period_end == want), None)
    else:
        start, _end = _period_bounds(raw)
        match = next((s for s in rows if s.period_start == start), None)
    if match is None:
        available = [s.period_label() for s in rows if s.period_start][:24]
        raise ToolError(
            f'no statement for account {account.mask} in period {raw!r}. '
            f"Stored periods: {', '.join(available) or '(none)'}")
    return match


def _cash_chain(account: PlaidAccount) -> list:
    """The account_ids whose BankTransactions belong to this account's cash
    side — its own supersede chain plus, for a PAIRED brokerage, its
    cash-services companion's.

    The pairing is included because on the Wells Fargo Advisors layout the
    brokerage account holds ZERO BankTransactions by construction (every cash
    movement lands on the companion; see PlaidAccount.paired_account_id). A
    transaction listing for ••9401 that honoured only its own account_id would
    return an empty array and read as "Plaid sent nothing", which is the exact
    misreading this sprint exists to end."""
    ids = list(stmts.supersede_chain(account.account_id))
    partner_id = (account.paired_account_id or '').strip()
    if partner_id:
        ids += [a for a in stmts.supersede_chain(partner_id) if a not in ids]
    return ids


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
        # v0.7.3 · '' means the rule writes NO cost center and ERPNext applies
        # its own Account/Company default — NOT that the offset line lands
        # uncosted. An AI auditing value-chain coverage needs that distinction
        # to be visible, which is why the field is listed even when empty.
        'cost_center': r.cost_center or '',
        # v0.8.5 · '' means the bank leg MIRRORS `cost_center` (the default);
        # '(none)' means the bank leg is deliberately left to ERPNext's own
        # default; anything else is an explicit bank-leg cost center.
        'bank_cost_center': r.bank_cost_center or '',
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


# ── v0.8.0 · the investment reconciliation toolkit (all read-only) ──────────
#
# Seven tools that between them let a diagnosis of a brokerage period run
# end-to-end over MCP: what statements exist, what the PDF says, what the
# parser got out of it, what Plaid returned on each of its two feeds, what the
# account holds, and which trades are missing a leg. Every one is read-only, so
# none carries a kill switch — the gate on this whole surface is the bearer
# token, and a tool that cannot write cannot be the thing that breaks a period.

def _list_statements(args: dict):
    account = _account_by_mask(args.get('account_mask'))
    year = args.get('year')
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError):
            raise ToolError(f'year must be an integer, got {year!r}')
    anchors = {a.statement_id: a
               for a in stmts.anchors_for_account(account.account_id)}
    rows = []
    for st in (PlaidStatement.query
               .filter(PlaidStatement.plaid_account_id == account.account_id)
               .order_by(PlaidStatement.period_start.desc().nullslast(),
                         PlaidStatement.id.desc()).all()):
        if year is not None and (st.period_end is None
                                 or st.period_end.year != year):
            continue
        anchor = anchors.get(st.id)
        rows.append({
            'statement_id': st.statement_id,
            'local_statement_id': st.id,
            'period': st.period_label(),
            'period_start': (st.period_start.isoformat()
                             if st.period_start else None),
            'period_end': (st.period_end.isoformat()
                           if st.period_end else None),
            # The CASH figures, which is what the anchor chain reconciles —
            # NOT total account value, which is the two portfolio_* keys.
            'anchored_opening': anchor.anchored_opening if anchor else None,
            'anchored_closing': anchor.anchored_closing if anchor else None,
            'portfolio_opening_value': st.portfolio_opening_value,
            'portfolio_closing_value': st.portfolio_closing_value,
            'transaction_sum': anchor.transaction_sum if anchor else None,
            'mark_to_market_delta': (anchor.mark_to_market_delta
                                     if anchor else None),
            'variance': anchor.variance if anchor else None,
            'reconciled': bool(anchor and anchor.reconciles()),
            'chain_gap_from_prior': bool(anchor
                                         and anchor.chain_gap_from_prior),
            'has_pdf': stmts.pdf_exists(st),
            'pdf_bytes': int(st.pdf_bytes or 0),
            'parse_method': st.parse_method or '',
            'parse_suspect': bool(st.parse_suspect),
            'parser_version': st.parser_version(),
        })
    return ({'account_mask': account.mask, 'account_id': account.account_id,
             'year': year, 'statements': rows, 'count': len(rows)},
            f'{account.mask}: {len(rows)} statement period(s)'
            + (f' in {year}' if year else ''))


# A statement PDF crosses the wire base64-encoded inside a JSON-RPC result, so
# it costs ~4/3 its size in the response AND lands in the caller's context
# window. Eight megabytes of PDF is already an unreasonable thing to hand an
# AI client; past that the useful answer is the path, not the bytes.
_MAX_PDF_BYTES = 8 * 1024 * 1024


def _get_statement_pdf(args: dict):
    import base64
    account = _account_by_mask(args.get('account_mask'))
    st = _statement_for(account, args.get('period'))
    path = stmts.resolve_pdf_path(st)
    if not path:
        raise ToolError(
            f'no PDF stored for account {account.mask} period '
            f'{st.period_label()}. The statement row exists (Plaid statement '
            f'{st.statement_id}) but its download never succeeded or the data '
            f'volume no longer holds the file — re-run the statements pull.')
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError as e:
        raise ToolError(f'stored PDF for {st.period_label()} is unreadable '
                        f'({e.__class__.__name__}): {path}')
    if len(data) > _MAX_PDF_BYTES:
        raise ToolError(
            f'PDF for {st.period_label()} is {len(data)} bytes, over the '
            f'{_MAX_PDF_BYTES}-byte limit this tool will inline. Read it from '
            f'{path} directly, or use get_statement_extracted_data for the '
            f'parsed figures.')
    import os
    return ({'account_mask': account.mask, 'period': st.period_label(),
             'period_start': (st.period_start.isoformat()
                              if st.period_start else None),
             'period_end': (st.period_end.isoformat()
                            if st.period_end else None),
             'filename': os.path.basename(path),
             'path': path,
             'size_bytes': len(data),
             'content_type': 'application/pdf',
             'encoding': 'base64',
             'content_base64': base64.b64encode(data).decode('ascii')},
            f'{account.mask} {st.period_label()}: {len(data)} byte PDF')


def _get_statement_extracted_data(args: dict):
    """What the PARSER got out of one statement — every figure, every activity
    row, and the provenance of the parse. Lets a parser error be spotted
    without downloading and reading the PDF."""
    account = _account_by_mask(args.get('account_mask'))
    st = _statement_for(account, args.get('period'))
    metadata = st.metadata_dict()
    txns = [{
        'sequence': s.sequence,
        'posted_date': s.posted_date.isoformat() if s.posted_date else None,
        'settle_date': s.settle_date.isoformat() if s.settle_date else None,
        # As the statement prints it: CASH IN POSITIVE. `plaid_convention_
        # amount` is the same figure in the opposite sign convention the
        # BankTransaction mirror uses, spelled out so a caller comparing the
        # two feeds never has to guess which way round a row is.
        'amount': s.amount,
        'plaid_convention_amount': round(-float(s.amount or 0.0), 2),
        'description': s.description, 'section': s.section,
        'match_status': s.match_status or '',
        'matched_bank_transaction_id': s.matched_bank_transaction_id,
    } for s in (StatementTransaction.query
                .filter_by(statement_id=st.id)
                .order_by(StatementTransaction.sequence).all())]
    anchor = StatementAnchor.query.filter_by(statement_id=st.id).first()
    return ({
        'account_mask': account.mask,
        'period': st.period_label(),
        'period_start': (st.period_start.isoformat()
                         if st.period_start else None),
        'period_end': st.period_end.isoformat() if st.period_end else None,
        'anchored_opening': anchor.anchored_opening if anchor else None,
        'anchored_closing': anchor.anchored_closing if anchor else None,
        'portfolio_opening_value': st.portfolio_opening_value,
        'portfolio_closing_value': st.portfolio_closing_value,
        'cash_services_account_number': st.cash_services_account_number,
        'parse_method': st.parse_method or '',
        'parse_suspect': bool(st.parse_suspect),
        'parser_version': st.parser_version(),
        'parse_layout': metadata.get('layout') or '',
        'parse_verified': metadata.get('verified'),
        'fields_failed': metadata.get('fields_failed') or [],
        # EVERY figure the recognizer recovered, verbatim. Not filtered to a
        # known list: an unrecognised key is exactly what a caller diagnosing a
        # parser regression needs to see.
        'parsed_metadata': metadata,
        'extracted_transactions': txns,
        'extracted_transaction_count': len(txns),
        # Statement parsing recovers FIGURES, not positions — Bank Bridge has
        # no per-statement holdings extractor, and the Portfolio Detail pages
        # are not parsed. Holdings come from Plaid's own feed instead; the key
        # is present and empty so a caller can tell "none extracted" from "this
        # build doesn't report them", and told where to actually look.
        'extracted_holdings': [],
        'extracted_holdings_note':
            'statement PDFs are parsed for balances and activity rows only; '
            'positions are not extracted from the Portfolio Detail pages. Use '
            'list_holdings for positions (sourced from Plaid '
            'investments/holdings/get).',
    }, f'{account.mask} {st.period_label()}: {len(txns)} activity row(s), '
       f'{len(metadata)} parsed field(s)')


def _list_plaid_transactions(args: dict):
    """The standard /transactions/sync feed for one account and window."""
    account = _account_by_mask(args.get('account_mask'))
    start = _iso_date(args.get('from_date'), 'from_date')
    end = _iso_date(args.get('to_date'), 'to_date')
    if start > end:
        raise ToolError(f'from_date {start} is after to_date {end}')
    wanted = (args.get('transaction_type') or '').strip().lower()
    chain = _cash_chain(account)
    masks = {a.account_id: (a.mask or '')
             for a in PlaidAccount.query.filter(
                 PlaidAccount.account_id.in_(tuple(chain))).all()}
    eff = stmts._effective_date_col()
    rows = (BankTransaction.query
            .filter(BankTransaction.account_id.in_(tuple(chain)),
                    eff >= start, eff <= end,
                    BankTransaction.removed.is_(False))
            .order_by(BankTransaction.date.asc(),
                      BankTransaction.id.asc()).all())
    if len(chain) > 1:
        rows = stmts.dedupe_across_accounts(rows)
    # The JE (if any) each transaction generated, in one query rather than one
    # per row — a year of a busy account is a few thousand transactions.
    from ..models import GeneratedJournalEntry
    jes = {j.plaid_transaction_id: j for j in GeneratedJournalEntry.query.filter(
        GeneratedJournalEntry.plaid_transaction_id.in_(
            tuple(t.plaid_transaction_id for t in rows) or ('',))).all()}
    out = []
    for t in rows:
        category = t.category or ''
        if wanted and wanted not in category.lower() and wanted not in (
                t.name or '').lower():
            continue
        je = jes.get(t.plaid_transaction_id)
        out.append({
            'transaction_id': t.plaid_transaction_id,
            'account_mask': masks.get(t.account_id, ''),
            'date': t.date.isoformat() if t.date else None,
            'effective_date': (t.effective_date().isoformat()
                               if t.effective_date() else None),
            # Plaid's convention: POSITIVE means money LEFT the account.
            'amount': t.amount,
            'cash_in_amount': round(-float(t.amount or 0.0), 2),
            'merchant': t.merchant_name or '',
            'description': t.name or '',
            'category': category,
            'pending': bool(t.pending),
            'source': t.source or 'plaid',
            'statement_match_status': t.statement_match_status or '',
            'bb_internal_tag': t.bb_internal_tag or '',
            'matched_rule': (je.rule_name or '') if je else None,
            'matched_rule_id': je.rule_id if je else None,
            'posted_je': je.erpnext_journal_entry_name if je else None,
            'posted_je_state': je.state if je else None,
        })
    return ({'account_mask': account.mask,
             'account_ids_searched': chain,
             'paired_cash_mask': _paired_mask(account),
             'from_date': start.isoformat(), 'to_date': end.isoformat(),
             'transaction_type': wanted or None,
             'transactions': out, 'count': len(out)},
            f'{account.mask} {start}..{end}: {len(out)} bank transaction(s)')


def _list_investment_transactions(args: dict):
    """The /investments/transactions/get feed for one account and window —
    the feed a brokerage's whole activity lives on."""
    from ..models import SecurityTransaction, Security
    account = _account_by_mask(args.get('account_mask'))
    start = _iso_date(args.get('from_date'), 'from_date')
    end = _iso_date(args.get('to_date'), 'to_date')
    if start > end:
        raise ToolError(f'from_date {start} is after to_date {end}')
    chain = stmts.supersede_chain(account.account_id)
    rows = (SecurityTransaction.query
            .filter(SecurityTransaction.account_id.in_(tuple(chain)),
                    SecurityTransaction.date >= start,
                    SecurityTransaction.date <= end)
            .order_by(SecurityTransaction.date.asc(),
                      SecurityTransaction.id.asc()).all())
    if len(chain) > 1:
        rows = stmts.dedupe_across_accounts(rows)
    tickers = {s.security_id: s for s in Security.query.filter(
        Security.security_id.in_(
            tuple(r.security_id for r in rows if r.security_id) or ('',))
    ).all()}
    out = []
    for t in rows:
        sec = tickers.get(t.security_id)
        out.append({
            'investment_transaction_id': t.plaid_investment_transaction_id,
            'date': t.date.isoformat() if t.date else None,
            'name': t.name or '',
            'type': t.type or '', 'subtype': t.subtype or '',
            'security_id': t.security_id,
            'ticker': (sec.ticker_symbol if sec else '') or '',
            'security_name': (sec.name if sec else '') or '',
            'quantity': t.quantity, 'price': t.price,
            # Plaid's convention on this endpoint matches /transactions/sync:
            # POSITIVE means cash LEFT the account (a buy). Spelled out both
            # ways so a caller never has to infer it.
            'amount': t.amount,
            'cash_in_amount': round(-float(t.amount or 0.0), 2),
            'fees': t.fees,
            'iso_currency_code': t.iso_currency_code or 'USD',
        })
    item = PlaidItem.query.filter_by(item_id=account.item_id).first()
    return ({
        'account_mask': account.mask,
        'from_date': start.isoformat(), 'to_date': end.isoformat(),
        'investment_transactions': out, 'count': len(out),
        # WHY AN EMPTY LIST IS AMBIGUOUS, made unambiguous. Zero rows means
        # either "no trades in this window" or "this Item's investments feed
        # was never pulled" — opposite findings with the same shape. The
        # Item's investments_synced_at settles it, so it ships with the
        # answer rather than needing a second call.
        'investments_synced_at': (item.investments_synced_at.isoformat()
                                  if item and item.investments_synced_at
                                  else None),
        'investments_ever_synced': bool(item and item.investments_synced_at),
    }, f'{account.mask} {start}..{end}: {len(out)} investment transaction(s)')


def _list_holdings(args: dict):
    from ..models import Security, SecurityHolding
    account = _account_by_mask(args.get('account_mask'))
    as_of = _iso_date(args.get('as_of'), 'as_of', required=False)
    chain = stmts.supersede_chain(account.account_id)
    rows = (SecurityHolding.query
            .filter(SecurityHolding.account_id.in_(tuple(chain)))
            .order_by(SecurityHolding.institution_value.desc().nullslast())
            .all())
    secs = {s.security_id: s for s in Security.query.filter(
        Security.security_id.in_(
            tuple(r.security_id for r in rows) or ('',))).all()}
    out = []
    total = 0.0
    for h in rows:
        sec = secs.get(h.security_id)
        total += float(h.institution_value or 0.0)
        out.append({
            'security_id': h.security_id,
            'ticker': (sec.ticker_symbol if sec else '') or '',
            'name': (sec.name if sec else '') or '',
            'type': (sec.type if sec else '') or '',
            'quantity': h.quantity,
            'institution_price': h.institution_price,
            'institution_price_as_of': (
                h.institution_price_as_of.isoformat()
                if h.institution_price_as_of else None),
            'institution_value': h.institution_value,
            'cost_basis': h.cost_basis,
            'iso_currency_code': h.iso_currency_code or 'USD',
            'refreshed_at': (h.refreshed_at.isoformat()
                             if h.refreshed_at else None),
        })
    snapshot_dates = [h.institution_price_as_of for h in rows
                      if h.institution_price_as_of]
    result = {
        'account_mask': account.mask,
        'holdings': out, 'count': len(out),
        'total_institution_value': round(total, 2),
        'snapshot_priced_as_of': (max(snapshot_dates).isoformat()
                                  if snapshot_dates else None),
        'requested_as_of': as_of.isoformat() if as_of else None,
        'is_historical': False,
    }
    if as_of is not None:
        # SAY SO rather than quietly ignoring the argument. `security_holdings`
        # is a snapshot-per-sync table (UNIQUE on account_id + security_id, see
        # the model) — each pull REPLACES the row, so no position history
        # exists to serve an as_of from. Answering with the current snapshot
        # and calling it historical would be the worst option available;
        # answering with it and saying plainly that it is current is the least
        # bad, because the current snapshot is usually still what the caller
        # wanted.
        result['as_of_note'] = (
            'Bank Bridge stores holdings as a CURRENT SNAPSHOT ONLY — each '
            'investments/holdings/get pull replaces the previous rows, so '
            'there is no position history to serve an as_of date from. The '
            'holdings below are the latest snapshot, NOT positions as of '
            f'{as_of.isoformat()}. For historical VALUE, read '
            'portfolio_opening_value / portfolio_closing_value from '
            'list_statements — those are the bank\'s own stated total account '
            'value per period.')
    return result, (f'{account.mask}: {len(out)} holding(s), '
                    f'{result["total_institution_value"]:.2f} total'
                    + (' (current snapshot, not historical)' if as_of else ''))


def _list_unpaired_trades(args: dict):
    """Trades and cash movements missing their other leg — the itemisation of
    the Cash Clearing imbalance."""
    from .. import trade_pairing
    from ..models import Security
    account = _account_by_mask(args.get('account_mask'))
    start = _iso_date(args.get('from_date'), 'from_date', required=False)
    end = _iso_date(args.get('to_date'), 'to_date', required=False)
    if not (account.paired_account_id or '').strip():
        raise ToolError(
            f'account {account.mask} has no cash-services companion, so it has '
            f'no Cash Clearing account and no trade legs to pair. On an '
            f'unpaired investment account Plaid returns both legs in one '
            f'investments/transactions/get row, so every trade is complete by '
            f'construction — use list_investment_transactions instead.')
    rows = trade_pairing.pairings_for_account(
        account.account_id, unpaired_only=True, from_date=start, to_date=end)
    secs = {s.security_id: s for s in Security.query.filter(
        Security.security_id.in_(
            tuple(r.security_id for r in rows if r.security_id) or ('',))
    ).all()}
    out = []
    for r in rows:
        sec = secs.get(r.security_id)
        d = {
            'date': r.trade_date.isoformat() if r.trade_date else None,
            'security': ((sec.ticker_symbol or sec.name) if sec else '') or '',
            'security_id': r.security_id,
            'action': r.buy_or_sell or '',
            'security_amount': r.expected_cash_amount,
            'expected_cash_amount': r.expected_cash_amount,
            'actual_cash_amount': r.actual_cash_amount,
            'missing_leg': r.missing_leg or '',
            # v0.8.1 · 'wf_same_account' | 'cross_account' | 'unpaired'. Always
            # the last of the three on THIS list (it is the unpaired list), and
            # returned anyway so the field means the same thing here as it does
            # in `summary` and in a row's to_dict().
            'pairing_scheme': r.pairing_scheme(),
            'delta': r.delta,
            'days_since_transaction': trade_pairing.days_since(r),
            'security_txn_id': r.security_txn_id,
            'cash_txn_id': r.cash_txn_id,
            'notes': r.notes or '',
        }
        out.append(d)
    summary = trade_pairing.summary(account.account_id)
    from .. import invest_je
    try:
        reported = invest_je.clearing_imbalance(account.account_id)
    except Exception:  # pragma: no cover - a cross-check must not fail the tool
        reported = None
    return ({
        'account_mask': account.mask,
        'cash_services_mask': _paired_mask(account),
        'from_date': start.isoformat() if start else None,
        'to_date': end.isoformat() if end else None,
        'unpaired': out, 'count': len(out),
        'summary': summary,
        # Σ delta over unpaired rows, which equals clearing_imbalance by
        # construction (a paired row contributes zero). Both are returned so a
        # caller can SEE they agree — if they ever don't, the pairing table is
        # stale and rebuild_anchors/a fresh sync is the fix, and silently
        # showing only one number would hide that.
        'unpaired_total': summary['unpaired_total'],
        'reported_clearing_imbalance': reported,
        'totals_agree': (reported is not None
                         and abs(reported - summary['unpaired_total']) <= 0.005),
    }, f'{account.mask}: {len(out)} unpaired leg(s), '
       f'{summary["unpaired_total"]:+.2f} in clearing')


def _agreement_summary(ag) -> dict:
    """One agreement's dashboard, JSON-safe. `advisory.dashboard` hands back the
    live model under 'agreement' (the admin template renders attributes off it);
    over the wire that has to be its to_dict(), or json.dumps(default=str)
    serializes the whole record as a repr string. The shared helper is what
    makes create/update return the SAME shape get_advisory_agreement_summary
    does, rather than something merely similar."""
    from .. import advisory
    d = dict(advisory.dashboard(ag))
    d['agreement'] = ag.to_dict()
    return d


def _get_advisory_agreement_summary(args: dict):
    from ..models import AdvisoryAgreement
    from .. import advisory
    aid = args.get('agreement_id')
    if aid:
        ag = db.session.get(AdvisoryAgreement, int(aid))
        if ag is None:
            raise ToolError(f'no advisory agreement id {aid}')
        return _agreement_summary(ag), f'agreement {aid} dashboard'
    ags = AdvisoryAgreement.query.all()
    out = [{'id': a.id, 'name': getattr(a, 'name', ''),
            'aum': advisory.agreement_aum(a),
            'status': a.status, 'is_active': a.is_active(),
            'client_entity': a.client_entity or '',
            'advisor_entity': a.advisor_entity or '',
            'managed_account_masks': _masks_for(a)} for a in ags]
    return {'agreements': out, 'count': len(out)}, f'{len(out)} agreement(s)'


def _masks_for(agreement) -> list:
    """The 4-digit masks of an agreement's managed accounts. Masks are the
    handle every other tool here takes, so an agreement listing that gave only
    opaque account_ids would not compose with them."""
    out = []
    for aid in agreement.account_ids():
        acct = PlaidAccount.query.filter_by(account_id=aid).first()
        out.append((acct.mask if acct else '') or aid)
    return out


# ── mutating tools (each gated by a kill switch, all default OFF) ────────────
def _checked_cost_center(raw, company: str = '') -> str:
    """`raw` trimmed, after refusing a Cost Center ERPNext positively denies
    (v0.7.3). '' passes straight through — it means "write no cost center".

    Validation is BEST-EFFORT BY DESIGN. An unconfigured or unreachable ERPNext
    yields no verdict and the value is accepted: refusing an operator's input
    because of a transient outage is a worse failure than storing a cost center
    ERPNext will itself reject at JE time. Only a positive denial — ERPNext
    answered and has no such leaf Cost Center — refuses here."""
    cc = (raw or '').strip()
    if not cc:
        return ''
    from .. import erpnext_bank
    from .. import erpnext_settings
    if not erpnext_settings.is_configured():
        return cc
    try:
        client = erpnext_bank.get_client()
    except Exception:
        return cc                      # unreachable → no verdict, accept
    if erpnext_bank.cost_center_exists(client, cc, company) is False:
        raise ToolError(
            f'{cc!r} is not a Cost Center this ERPNext will accept on a '
            f'Journal Entry line (unknown, or a group node). Check the exact '
            f'docname — cost centers are Company-suffixed, e.g. "Harvest - OML".')
    return cc


def _checked_bank_cost_center(raw, company: str = '') -> str:
    """`_checked_cost_center` plus the v0.8.5 sentinel. '(none)' means "write no
    cost center on the bank leg" and is NOT a docname, so it must never be
    looked up — ERPNext would positively deny it and refuse the whole call."""
    from .. import categorization
    cc = (raw or '').strip()
    if cc == categorization.BANK_LEG_NO_COST_CENTER:
        return cc
    return _checked_cost_center(cc, company)


def _create_rule(args: dict):
    rule = CategorizationRule(
        match_type=(args.get('match_type') or 'merchant_contains'),
        match_value=(args.get('match_value') or ''),
        offset_account=(args.get('offset_account') or ''),
        # v0.7.3 · optional; '' (the pre-v0.7.3 behaviour) leaves the JE line
        # blank so ERPNext applies its own Account/Company default. A new rule
        # is always Company-agnostic here — create_rule takes no scope — so the
        # lookup is unscoped, which only widens the bare-name fallback.
        cost_center=(_checked_cost_center(args.get('cost_center')) or None),
        # v0.8.5 · optional bank-leg override. Omitted (None) = MIRROR
        # cost_center onto the bank leg, which is what the fix wants by
        # default; pass '(none)' to leave the bank leg to ERPNext's own
        # default, or a docname to send it somewhere else entirely.
        bank_cost_center=(_checked_bank_cost_center(
            args.get('bank_cost_center')) or None),
        bb_internal_tag=(args.get('tag') or ''),
        name=(args.get('name') or args.get('match_value') or 'AI rule'))
    db.session.add(rule)
    db.session.commit()
    return {'created_rule': rule.to_dict()}, f'created rule {rule.id}'


# Every column a new rule VERSION inherits from the one it supersedes is
# whatever isn't listed here, read off the table rather than spelled out — so a
# column added by a future release is carried across an update instead of being
# silently reset to its default (the failure mode a hand-written field list
# invites, and the reason update_rule doesn't have one).
#
# The exclusions are the row's identity and history (`id`, timestamps,
# `superseded_by`, `archived`) plus the two counters that belong to a version
# rather than a rule: `match_count` re-accrues via the rollup, which walks
# `superseded_by` forward and credits the live rule, and `activated_at`
# re-stamps to now — matching what an edit through /admin/rules already does.
_RULE_CLONE_SKIP = frozenset({'id', 'created_at', 'updated_at',
                              'superseded_by', 'archived', 'match_count',
                              'activated_at'})

# MCP argument name → model attribute, for update_rule's patch. `tag` keeps the
# name create_rule already uses for bb_internal_tag.
_RULE_UPDATABLE = {
    'name': 'name',
    'match_type': 'match_type',
    'match_value': 'match_value',
    'offset_account': 'offset_account',
    'cost_center': 'cost_center',
    'bank_cost_center': 'bank_cost_center',
    'active': 'active',
    'priority': 'priority',
    'tag': 'bb_internal_tag',
    'applies_to_company': 'applies_to_company',
    # v0.9.0 · the party fields. Without these an AI operator could set a rule's
    # cost center but not who the money went to, which is the half of
    # ACC-JV-2026-02312 that mattered — and closing it would have meant a human
    # in the Rules editor for every one of them.
    'party_type': 'party_type',
    'party_name': 'party_name',
    'auto_create_party': 'auto_create_party',
}


def _update_rule(args: dict):
    """Patch one rule NON-DESTRUCTIVELY — the same clone-and-supersede an edit
    on /admin/rules performs, so the version that generated a past JE stays
    readable. Returns the NEW rule (a new id) and the archived old one."""
    from .. import audit
    from .. import categorization
    raw_id = args.get('rule_id')
    try:
        rule_id = int(raw_id)
    except (TypeError, ValueError):
        raise ToolError(f'rule_id must be an integer, got {raw_id!r}')
    old = db.session.get(CategorizationRule, rule_id)
    if old is None:
        raise ToolError(f'no rule id {rule_id}')
    if old.archived:
        raise ToolError(
            f'rule {rule_id} is archived — it is a historical version and will '
            f'never fire again. Update the rule that superseded it'
            + (f' (id {old.superseded_by})' if old.superseded_by else ''))

    patch = {attr: args[key] for key, attr in _RULE_UPDATABLE.items()
             if key in args}
    if not patch:
        raise ToolError('nothing to update — pass at least one field '
                        f"({', '.join(sorted(_RULE_UPDATABLE))})")

    vals = {c.name: getattr(old, c.name)
            for c in CategorizationRule.__table__.columns
            if c.name not in _RULE_CLONE_SKIP}
    before = old.to_dict()

    if 'match_type' in patch:
        mt = (patch['match_type'] or '').strip()
        if mt not in categorization.MATCH_TYPES:
            raise ToolError(
                f'unknown match_type {mt!r} — one of '
                f"{', '.join(categorization.MATCH_TYPES)}")
        patch['match_type'] = mt
    if 'priority' in patch:
        try:
            patch['priority'] = int(patch['priority'])
        except (TypeError, ValueError):
            raise ToolError(f"priority must be an integer, got "
                            f"{patch['priority']!r}")
    if 'active' in patch:
        patch['active'] = bool(patch['active'])
    if 'applies_to_company' in patch:
        patch['applies_to_company'] = (
            (patch['applies_to_company'] or '').strip() or None)
    if 'party_type' in patch:
        # Validated against the vocabulary, case-insensitively, because this
        # value is handed to ERPNext AS A DOCTYPE — an unchecked 'supplier' or
        # 'Vendor' would produce Journal Entries ERPNext refuses at submit, which
        # is the v0.4.0.9 failure mode arriving through a new door. '' clears it.
        declared = (patch['party_type'] or '').strip()
        if declared:
            match = {p.lower(): p for p in categorization.PARTY_TYPES
                     if p}.get(declared.lower())
            if match is None:
                raise ToolError(
                    f'unknown party_type {declared!r} — one of '
                    + ', '.join(p or '(none)'
                                for p in categorization.PARTY_TYPES))
            declared = match
        patch['party_type'] = declared or None
    if 'party_name' in patch:
        patch['party_name'] = (patch['party_name'] or '').strip() or None
    if 'auto_create_party' in patch:
        # TRI-STATE, and None must survive: it means "inherit the global gate",
        # which is what every pre-v0.9.0 rule holds. bool() here would collapse
        # inherit into never.
        raw = patch['auto_create_party']
        patch['auto_create_party'] = (
            None if raw is None or (isinstance(raw, str) and not raw.strip())
            else bool(raw))
    if 'cost_center' in patch:
        # Validated against the company the rule will have AFTER the patch, not
        # the one it had before — an update that re-scopes and re-costs a rule
        # in one call must be checked against its new scope.
        company = (patch.get('applies_to_company')
                   if 'applies_to_company' in patch
                   else vals.get('applies_to_company')) or ''
        # '' → None: an explicit empty string is how a caller CLEARS the cost
        # center, handing the line back to ERPNext's own defaults.
        patch['cost_center'] = (
            _checked_cost_center(patch['cost_center'], company) or None)
    if 'bank_cost_center' in patch:
        # Same scoping rule as `cost_center` above; '' → None restores the
        # v0.8.5 default (mirror the offset leg's cost center).
        company = (patch.get('applies_to_company')
                   if 'applies_to_company' in patch
                   else vals.get('applies_to_company')) or ''
        patch['bank_cost_center'] = (
            _checked_bank_cost_center(patch['bank_cost_center'], company)
            or None)

    vals.update(patch)
    new_rule = CategorizationRule(**vals)
    db.session.add(new_rule)
    db.session.flush()                 # assign new_rule.id
    old.active = False
    old.archived = True
    old.superseded_by = new_rule.id
    db.session.commit()
    audit.record('rule_updated', subject_type='CategorizationRule',
                 subject_id=new_rule.id, before=before,
                 after=new_rule.to_dict(),
                 notes=f'rule #{old.id} superseded by #{new_rule.id} '
                       f"(MCP update_rule: {', '.join(sorted(patch))})",
                 actor='mcp')
    return ({'updated_rule': new_rule.to_dict(),
             'superseded_rule_id': old.id,
             'updated_fields': sorted(patch)},
            f'rule #{old.id} → #{new_rule.id} '
            f"({', '.join(sorted(patch))})")


# ── v0.7.4 · advisory-agreement registration ────────────────────────────────
def _create_advisory_agreement(args: dict):
    """Register an advisory agreement against one managed Plaid account.

    The mask → account resolution happens HERE (via _account_by_mask, the same
    helper every other tool uses, so the brokerage/cash-companion preference is
    identical) and the terms validation in advisory.register_agreement, which
    the admin UI can reach without importing a blueprint."""
    from .. import advisory
    account = _account_by_mask(args.get('plaid_account_mask'))
    try:
        agreement, notes = advisory.register_agreement(
            account,
            name=args.get('agreement_name'),
            client_entity=args.get('client_entity'),
            advisor_entity=args.get('advisor_entity'),
            effective_date=args.get('effective_date'),
            objective=args.get('objective'),
            investment_horizon_years=args.get('investment_horizon_years'),
            fee_type=args.get('fee_type'),
            fee_percent_of_aum=args.get('fee_percent_of_aum'),
            fee_flat_annual=args.get('fee_flat_annual'),
            billing_frequency=args.get('billing_frequency'),
            termination_date=args.get('termination_date'),
            applies_to_company=args.get('applies_to_company'),
            document_reference=args.get('document_reference'),
            actor='mcp')
    except advisory.AdvisoryError as e:
        raise ToolError(str(e))
    result = {'created_agreement_id': agreement.id,
              'managed_account_mask': account.mask,
              'not_yet_configured': notes,
              'summary': _agreement_summary(agreement)}
    return result, (f'registered agreement #{agreement.id} '
                    f'{agreement.name!r} on account {account.mask}')


def _update_advisory_agreement(args: dict):
    """Amend one agreement NON-DESTRUCTIVELY — clone-and-supersede, the same
    shape update_rule uses, so the terms that governed a past fee posting stay
    reconstructible. Returns the NEW id and the superseded one."""
    from .. import advisory
    from ..models import AdvisoryAgreement
    raw_id = args.get('agreement_id')
    try:
        agreement_id = int(raw_id)
    except (TypeError, ValueError):
        raise ToolError(f'agreement_id must be an integer, got {raw_id!r}')
    old = db.session.get(AdvisoryAgreement, agreement_id)
    if old is None:
        raise ToolError(f'no advisory agreement id {agreement_id}')
    patch = {k: args[k] for k in advisory.AMENDABLE if k in args}
    try:
        new, superseded, applied = advisory.amend_agreement(old, patch,
                                                            actor='mcp')
    except advisory.AdvisoryError as e:
        raise ToolError(str(e))
    result = {'updated_agreement_id': new.id,
              'superseded_agreement_id': superseded.id,
              'amended_fields': applied,
              'summary': _agreement_summary(new)}
    return result, (f'agreement #{superseded.id} → #{new.id} '
                    f"({', '.join(applied)})")


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
    # v0.8.0 · the trade-leg pairings are derived from the same source tables
    # and go stale in the same way, so the tool an operator (or an AI) reaches
    # for when a number looks wrong refreshes both. Fail-soft — a diagnostic
    # table must not be able to fail an anchor rebuild that succeeded.
    pairing = None
    try:
        from .. import trade_pairing
        pairing = trade_pairing.rebuild(account_id)
    except Exception:  # pragma: no cover - fail-soft
        db.session.rollback()
        log.warning('trade-leg pairing rebuild failed', exc_info=True)
    return ({'rebuild': result, 'trade_leg_pairing': pairing},
            f"anchors written: {result.get('written', 0)}"
            # v0.8.1 · the same-account/cross-account split, because "paired:
            # 604" alone cannot tell an operator whether the Wells Fargo rule
            # fired or the fallback quietly carried the whole account.
            + (f", trade legs paired: {pairing['paired']} "
               f"({pairing['paired_same_account']} same-account, "
               f"{pairing['paired_cross_account']} cross-account), "
               f"unpaired: {pairing['unpaired_security']} security / "
               f"{pairing['unpaired_cash']} cash" if pairing else ''))


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


# ── v0.8.4 · the admin actions that were button-only ────────────────────────
#
# Every one of these mirrors a POST route on /admin that already had proven
# logic behind it. Tim ran the OML pipeline end to end on 2026-07-28 and found
# each of these was a thing only a human at a browser could do — which is
# exactly the relay tax the MCP server exists to remove. Nothing here reimplements
# an admin route: the shared work was lifted into the modules that own it
# (categorization.rerun_rules, invest_je.*) and both surfaces call it.

def _erp_client_or_error():
    """The ERPNext client, or a ToolError naming the fix. Every mutating tool
    below needs one and none of them should 500 without it."""
    from .. import erpnext_bank
    from .. import erpnext_settings
    if not erpnext_settings.is_configured():
        raise ToolError('ERPNext is not configured — set the URL, API key and '
                        'secret with set_erpnext_config (or on '
                        '/admin/erpnext_settings) first.')
    try:
        return erpnext_bank.get_client()
    except Exception as e:
        raise ToolError(f'could not reach ERPNext: {e}')


def _rerun_rules(args: dict):
    from .. import categorization
    from .. import sync_engine
    try:
        stats = categorization.rerun_rules(sync_engine.get_erp_client_or_none())
    except categorization.JournalEntryGateOff as e:
        raise ToolError(str(e))
    return stats, (f"considered {stats['considered']}, matched "
                   f"{stats['matched']}, generated {stats['generated']}")


def _reset_investment_drafts(args: dict):
    from .. import audit
    from .. import invest_je
    client = _erp_client_or_error()
    account_id = (args.get('account_id') or '').strip() or None
    result = invest_je.reset_investment_drafts(client, account_id)
    audit.record('invest_je_drafts_reset', subject_type='GeneratedJournalEntry',
                 subject_id=account_id or 'all', after=result,
                 notes='MCP reset_investment_drafts', actor='mcp')
    if result['aborted']:
        return result, f"ABORTED: {result['reason']}"
    return result, (f"{result['tracker_deleted']} tracked + "
                    f"{result['orphan_deleted']} orphan draft(s) deleted "
                    f"({result['total_deleted']} total)")


def _get_clearing_status(args: dict):
    from .. import invest_je
    client = _erp_client_or_error()
    result = invest_je.clearing_status(client, (args.get('account_id') or '').strip())
    if 'error' in result:
        raise ToolError(result['error'])
    return result, (f"ledger {result['ledger_balance']:,.2f}, projected "
                    f"{result['projected_imbalance']:,.2f}, "
                    f"{result['unposted_settlements']} settlement leg(s) "
                    f"still unposted")



def _get_draft_health(args: dict):
    """How many DRAFT Journal Entries ERPNext is holding, and whether that is
    normal for this install (v0.8.5). Read-only, and never gated: knowing the
    state of the queue cannot change the books, and an AI operator that cannot
    see the queue is exactly the operator the v0.8.4 incident had."""
    from .. import draft_health
    client = _erp_client_or_error()
    company = (args.get('company') or '').strip()
    threshold = args.get('threshold')
    try:
        threshold = int(threshold) if threshold not in (None, '') else None
    except (TypeError, ValueError):
        raise ToolError(f'threshold must be a whole number, got {threshold!r}')
    try:
        snap = draft_health.observe(client, company, threshold=threshold)
    except (ERPNextAPIError, ERPNextError) as e:
        # Deliberately an error, not a zero. A caller handed draft_count=0 by an
        # unreadable ledger would conclude the queue is clean, which is the one
        # wrong answer this tool could give.
        raise ToolError(f'could not read draft Journal Entries: {e}')
    return snap, snap['headline'] + (
        ' — ALERT: threshold crossed on this reading' if snap.get('crossed')
        else ' — recovered on this reading' if snap.get('recovered') else '')


def _get_statement_recon_report(args: dict):
    """Does what the STATEMENTS report match what Bank Bridge BOOKED, per
    category and per reconciled period (v0.9.0)? Read-only, and never gated —
    for the same reason get_draft_health isn't: reading a diagnostic cannot
    change the books, and an AI operator who cannot see the drift is exactly the
    operator the v0.8.4 incident had."""
    from .. import statement_recon
    client = _erp_client_or_error()
    account_id = (args.get('account_id') or '').strip()
    if not account_id and (args.get('account_mask') or '').strip():
        # Raises a ToolError naming the mask when it doesn't resolve, which is a
        # better answer than silently reporting on every account instead.
        account_id = _account_by_mask(args.get('account_mask')).account_id
    findings_only = bool(args.get('findings_only', False))
    try:
        rep = statement_recon.observe(client, account_id)
    except (ERPNextAPIError, ERPNextError) as e:
        # An error, not an empty report. A caller handed zero booked amounts by
        # an unreadable ledger would conclude the books are empty.
        raise ToolError(f'could not read the ledger for the comparison: {e}')
    if findings_only:
        rep = {**rep, 'rows': rep['findings']}
    line = rep['headline']
    if rep.get('fired'):
        line += f" — {len(rep['fired'])} NEW finding(s) on this reading"
    if rep.get('skipped'):
        line += f" · {len(rep['skipped'])} period(s) not compared"
    return rep, line


def _post_clearing_cleanup_je(args: dict):
    from .. import invest_je
    client = _erp_client_or_error()
    account_id = (args.get('account_id') or '').strip()
    dry_run = bool(args.get('dry_run', True))
    status = invest_je.clearing_status(client, account_id)
    if 'error' in status:
        raise ToolError(status['error'])
    # The ordering guard, enforced rather than documented: a cleanup written
    # while the v0.8.4 backfill still has legs to post corrects an imbalance
    # that backfill is about to correct again, and the ledger ends up wrong by
    # the same six figures in the other direction.
    if not dry_run and status['unposted_settlements']:
        raise ToolError(
            f"{status['unposted_settlements']} settlement leg(s) are still "
            'unposted — run the investment post first (they relieve Cash '
            'Clearing on their own), then re-check with get_clearing_status. '
            'Pass dry_run=true to preview the cleanup meanwhile.')
    result = invest_je.clearing_cleanup_je(client, account_id, dry_run=dry_run)
    result['unposted_settlements'] = status['unposted_settlements']
    if result['skipped']:
        return result, result['skipped']
    return result, (f"draft {result['journal_entry']} moves "
                    f"${result['amount']:,.2f} to {result['counter_account']}")


def _set_je_gate(args: dict, on: bool):
    from .. import audit
    from .. import erpnext_settings
    before = erpnext_settings.je_generation_enabled()
    erpnext_settings.set_je_generation(on)
    audit.record('je_gate_changed', subject_type=None,
                 before={'auto_generate_journal_entries': before},
                 after={'auto_generate_journal_entries': on},
                 notes='MCP ' + ('enable_je_gate' if on else 'disable_je_gate'),
                 actor='mcp')
    return ({'auto_generate_journal_entries': on, 'was': before},
            f"Journal Entry generation {'ON' if on else 'OFF'}")


def _enable_je_gate(args: dict):
    return _set_je_gate(args, True)


def _disable_je_gate(args: dict):
    return _set_je_gate(args, False)


def _set_erpnext_config(args: dict):
    """Set the ERPNext connection. Only the fields PRESENT in `args` change —
    an omitted api_secret keeps the stored one, exactly as the form does."""
    from .. import audit
    from .. import erpnext_settings
    current = erpnext_settings.load()
    url = args.get('url')
    api_key = args.get('api_key')
    secret = args.get('api_secret')
    company = args.get('default_company')
    saved = erpnext_settings.save(
        current['url'] if url is None else url,
        current['api_key'] if api_key is None else api_key,
        None if secret is None else secret,
        current['default_company'] if company is None else company)
    changed = [k for k in ('url', 'api_key', 'default_company')
               if saved[k] != current[k]]
    if secret is not None:
        changed.append('api_secret')
    # The secret is never echoed back, here or anywhere. Same rule the admin
    # page follows — an MCP transcript is as readable as a screen.
    result = {'url': saved['url'], 'api_key': saved['api_key'],
              'default_company': saved['default_company'],
              'api_secret': erpnext_settings.masked_secret(),
              'changed_fields': changed,
              'configured': erpnext_settings.is_configured()}
    audit.record('erpnext_settings_changed', subject_type=None,
                 after={'changed_fields': changed, 'url': saved['url']},
                 notes='MCP set_erpnext_config', actor='mcp')
    return result, ('changed: ' + (', '.join(changed) if changed else 'nothing'))


# ── v0.8.5 · sync on demand ─────────────────────────────────────────────────
#
# THE GAP THIS CLOSES. On 2026-07-29, mid-way through the Cash Clearing cleanup,
# 77 settlement legs for the ••9401 brokerage still needed posting.
# `trigger_reparse` re-read the statement PDFs and produced no drafts (it is the
# wrong pipeline). `reset_investment_drafts` refused, correctly, on the first
# submitted entry. Nothing on this server could run the thing that actually
# posts them — `sync_engine.post_investment_jes` — and a $1M ledger correction
# stopped dead until a human clicked Sync Now in a browser. The button and this
# tool now call ONE function (`sync_engine.run_sync`), so that cannot recur.
#
# It is a KAIROTIC trigger and nothing more: the caller decides that now is the
# moment, and the tool does exactly what was asked, once. It schedules nothing,
# widens nothing, and leaves the background cadence exactly where it is.

def _flag(args: dict, name: str, default: bool) -> bool:
    """A boolean argument, tolerant of the string spellings an LLM client sends.
    `bool("false")` is True, and a tool whose dry_run silently inverted on a
    quoted argument would be the worst failure on this list."""
    raw = args.get(name, default)
    if raw is None:
        return default
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ('true', '1', 'yes', 'on'):
            return True
        if low in ('false', '0', 'no', 'off', ''):
            return False
        raise ToolError(f'{name} must be true or false, got {raw!r}')
    return bool(raw)


def _sync_now(args: dict):
    from .. import sync_engine
    account_id = (args.get('account_id') or '').strip()
    include_investments = _flag(args, 'include_investments', True)
    dry_run = _flag(args, 'dry_run', False)
    try:
        result = sync_engine.run_sync(
            account_id=account_id,
            include_investments=include_investments,
            dry_run=dry_run, actor='mcp')
    except sync_engine.SyncScopeError as e:
        raise ToolError(str(e))
    return result, _sync_summary(result)


def _sync_summary(result: dict) -> str:
    """The one-line result summary written to AiActionLog. Says what LANDED and
    what did not — a summary that reads 'success' over a partial run is how an
    operator learns about a broken account a week late."""
    scope = result['scope']
    if result.get('dry_run'):
        would = sum(a.get('would_post', 0) for a in result['accounts'])
        jes = sum(a.get('would_draft_investment_jes', 0)
                  for a in result['accounts'])
        return (f'DRY RUN ({scope}): would post {would} bank transaction(s) '
                f'and draft {jes} investment JE(s) from what is already '
                'mirrored; Plaid was not contacted')
    t = result['totals']
    line = (f"{result['status']} ({scope}): {t['transactions_fetched']} "
            f"fetched, {t['bank_transactions_posted']} posted, "
            f"{t['investment_jes_drafted']} investment JE(s) drafted, "
            f"{t['investment_jes_skipped_duplicate']} already posted, "
            f"{len(result['errors'])} error(s) in "
            f"{result['elapsed_seconds']}s")
    if result['errors']:
        line += f" — first: [{result['errors'][0]['code']}] " \
                f"{result['errors'][0]['message'][:200]}"
    return line


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
            'cost_center, internal tag, priority). An empty cost_center means '
            'the rule writes none and ERPNext applies the offset Account\'s or '
            "the Company's own default — not that the line is uncosted. Set "
            'active_only=false to include inactive rules. Read-only.',
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
    # ── v0.8.0 · investment reconciliation toolkit (read-only) ──────────────
    'list_statements': {
        **_tool(
            'Enumerate the statement periods stored for one account (by '
            '4-digit mask), newest first. Per period: Plaid statement_id, '
            'period_start/period_end, the anchored CASH opening and closing '
            '(what the reconciliation chain tests), the bank-stated '
            'portfolio_opening_value / portfolio_closing_value (TOTAL account '
            'value, cash plus securities at market — a different and larger '
            'number on a brokerage, NULL on a depository statement), '
            'transaction_sum, mark_to_market_delta, variance, whether the '
            'period reconciles, whether a chain gap precedes it, whether a PDF '
            'is actually on disk, and the parser version + method that '
            'produced the figures. Pass year=YYYY to filter. START HERE when '
            'asked why an account does not reconcile — it names the periods '
            'every other tool takes. Read-only.',
            {'account_mask': _STR, 'year': {'type': 'integer'}},
            required=('account_mask',)),
        'handler': _list_statements},
    'get_statement_pdf': {
        **_tool(
            'Return the stored statement PDF for one account and period, '
            'base64-encoded, with its filename, on-disk path and byte size. '
            '`period` takes either "YYYY-MM" (the month the period starts in) '
            'or an exact period_end as "YYYY-MM-DD". REFUSES with the list of '
            'periods that DO exist when the period is unknown; refuses when '
            'the statement row exists but no PDF was ever downloaded (or the '
            'data volume no longer holds it); refuses a PDF over 8 MB and '
            'hands back the path instead. Prefer '
            'get_statement_extracted_data when you want the figures rather '
            'than the document — it costs a fraction of the context. '
            'Read-only.',
            {'account_mask': _STR,
             'period': {'type': 'string',
                        'description': 'YYYY-MM or an exact period_end '
                                       'YYYY-MM-DD'}},
            required=('account_mask', 'period')),
        'handler': _get_statement_pdf},
    'get_statement_extracted_data': {
        **_tool(
            'What Bank Bridge PARSED out of one statement PDF — everything, '
            'so a parser error can be spotted without opening the document. '
            'Returns the anchored cash opening/closing, the bank-stated total '
            'account value at both ends, the cash-services account number, the '
            'complete parsed_metadata blob VERBATIM (every summary figure the '
            'recognizer recovered: deposits, withdrawals, dividends, interest, '
            'fees, securities purchased and sold, realized/unrealized gains …), '
            'the parse provenance (parser_version, layout, method, verified, '
            'fields_failed, parse_suspect) and every extracted Activity Detail '
            'row with its posted date, amount, description, section and match '
            'status. Amounts on activity rows are CASH IN POSITIVE as the '
            'statement prints them, with plaid_convention_amount alongside '
            'for comparison against the transaction feeds. Note holdings are '
            'NOT extracted from statement PDFs — use list_holdings. `period` '
            'takes "YYYY-MM" or an exact period_end "YYYY-MM-DD". Read-only.',
            {'account_mask': _STR,
             'period': {'type': 'string',
                        'description': 'YYYY-MM or an exact period_end '
                                       'YYYY-MM-DD'}},
            required=('account_mask', 'period')),
        'handler': _get_statement_extracted_data},
    'list_plaid_transactions': {
        **_tool(
            'Every BankTransaction Bank Bridge holds for one account in a date '
            'range — the standard /transactions/sync feed. Per row: '
            'transaction_id, Plaid date AND effective date (the bank\'s own '
            'statement date when the matcher found one), amount in BOTH sign '
            'conventions (`amount` is Plaid\'s, POSITIVE = money LEFT the '
            'account; `cash_in_amount` is the flipped one the statements use), '
            'merchant, description, category, pending, whether the row came '
            'from the Plaid feed or was synthesized from a statement, its '
            'internal tag, the rule that matched it and the Journal Entry it '
            'generated. For a PAIRED brokerage this searches the CASH '
            'COMPANION too and labels each row with the mask it came from — '
            'the brokerage account itself holds zero bank transactions by '
            'construction, so a listing without the companion would look '
            'empty. transaction_type is an optional case-insensitive substring '
            'filter over category and description. Read-only.',
            {'account_mask': _STR,
             'from_date': {'type': 'string', 'description': 'YYYY-MM-DD'},
             'to_date': {'type': 'string', 'description': 'YYYY-MM-DD'},
             'transaction_type': {
                 'type': 'string',
                 'description': 'optional substring filter on Plaid category '
                                'or description, e.g. "transfer"'}},
            required=('account_mask', 'from_date', 'to_date')),
        'handler': _list_plaid_transactions},
    'list_investment_transactions': {
        **_tool(
            'Every investment transaction Bank Bridge holds for one account in '
            'a date range — the /investments/transactions/get feed, which is '
            'where a brokerage\'s buys, sells, dividends, interest, fees, '
            'deposits, withdrawals and transfers actually live. Per row: '
            'investment_transaction_id, date, name, Plaid type '
            '(buy/sell/cash/fee/transfer/cancel) and subtype, security_id with '
            'its resolved ticker and name, quantity, price, fees, and amount '
            'in BOTH sign conventions (`amount` is Plaid\'s, POSITIVE = cash '
            'LEFT the account, same convention as the bank feed; '
            '`cash_in_amount` is the flip). '
            'AN EMPTY RESULT IS AMBIGUOUS AND THE ANSWER SAYS WHICH: zero rows '
            'means either no trades in the window or that this Item\'s '
            'investments feed was never pulled at all, so '
            'investments_ever_synced and investments_synced_at ship alongside. '
            'investments_ever_synced=false with a brokerage showing Movement '
            '0.00 is the signature of an Item that was never granted the '
            '`investments` product at Link time. Read-only.',
            {'account_mask': _STR,
             'from_date': {'type': 'string', 'description': 'YYYY-MM-DD'},
             'to_date': {'type': 'string', 'description': 'YYYY-MM-DD'}},
            required=('account_mask', 'from_date', 'to_date')),
        'handler': _list_investment_transactions},
    'list_holdings': {
        **_tool(
            'Current positions for one investment account, largest by value '
            'first: security_id, ticker, name, security type, quantity '
            '(NEGATIVE for a short option position), institution_price and the '
            'date it was priced, institution_value, cost_basis where the '
            'custodian reports one, and the total across all positions. '
            'IMPORTANT — `as_of` CANNOT return historical positions: Bank '
            'Bridge stores holdings as a current snapshot only (each '
            'investments/holdings/get pull REPLACES the previous rows), so '
            'passing as_of returns the latest snapshot with '
            'is_historical=false and an as_of_note saying so. For historical '
            'account VALUE use list_statements\' portfolio_opening_value / '
            'portfolio_closing_value, which are the bank\'s own stated figures '
            'per period. Read-only.',
            {'account_mask': _STR,
             'as_of': {'type': 'string',
                       'description': 'YYYY-MM-DD. NOTE: no position history '
                                      'is stored — see the tool description.'}},
            required=('account_mask',)),
        'handler': _list_holdings},
    'list_unpaired_trades': {
        **_tool(
            'The itemisation of a paired brokerage\'s Cash Clearing imbalance '
            '— every movement missing its other leg. A trade moves money twice, '
            'once as securities and once as cash, and WHERE the cash half lands '
            'depends on the custodian. Wells Fargo Advisors puts BOTH legs on '
            'the brokerage account (a type=buy row and a type=cash settlement '
            'row with the same security_id, date and magnitude); other '
            'custodians put the security leg on the brokerage and the cash leg '
            'on the cash-services companion as an "Increase/Decrease from '
            'Brokerage activity" line. Pairing tries same-account first, then '
            'falls back to cross-account, both within 3 business days. Each row '
            'here is a leg that matched neither way: date, security and ticker, '
            'action (buy/sell/fee/cash), expected vs actual cash amount, '
            '`missing_leg` naming which side is absent ("cash" = a trade whose '
            'settlement never appeared; "security" = a companion brokerage-'
            'activity line with no trade to explain it, which is what an Item '
            'that never pulled its investments feed looks like), '
            '`pairing_scheme`, delta, and days_since_transaction so slow '
            'settlement is distinguishable from a permanent gap. Ordinary '
            'companion traffic (debit-card purchases, transfers) is NOT listed '
            '— it never had a security leg, so it is not an orphaned trade leg. '
            '`unpaired_total` sums the deltas and equals '
            'reported_clearing_imbalance by construction; totals_agree=false '
            'means the pairing table is stale and a fresh sync or '
            'rebuild_anchors is needed. REFUSES an account with no '
            'cash-services companion — an unpaired investment account gets '
            'both legs in one Plaid row and cannot have this problem. '
            'Read-only.',
            {'account_mask': _STR,
             'from_date': {'type': 'string', 'description': 'YYYY-MM-DD'},
             'to_date': {'type': 'string', 'description': 'YYYY-MM-DD'}},
            required=('account_mask',)),
        'handler': _list_unpaired_trades},

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
            'Create a categorization rule. Optionally set cost_center — the '
            'ERPNext Cost Center docname written onto BOTH legs of every '
            'Journal Entry this rule generates (v0.8.5; through v0.8.4 only '
            'the offset leg got one and ERPNext filled the bank leg with the '
            'Company default, which split the dimension across two segments). '
            'Set bank_cost_center only when the bank leg must differ: a '
            'docname sends it elsewhere, "(none)" leaves it to ERPNext\'s own '
            'default. REFUSES a cost center ERPNext positively denies (unknown, '
            'or a group node); an unreachable ERPNext yields no verdict and the '
            'value is accepted. Omit cost_center to write none on either leg, '
            "which leaves ERPNext to apply the Account's or Company's own "
            'default. REVERSIBLE: a rule is archived, never deleted, and only '
            'affects Journal Entries generated after it. MUTATING — requires '
            'the create_rule kill switch to be ON.',
            {'match_type': _STR, 'match_value': _STR, 'offset_account': _STR,
             'cost_center': {
                 'type': 'string',
                 'description': 'ERPNext Cost Center docname for BOTH JE legs, '
                                'e.g. "Harvest - OML". Omit for none.'},
             'bank_cost_center': {
                 'type': 'string',
                 'description': 'Bank-leg override. Omit to mirror '
                                'cost_center (the default). "(none)" leaves '
                                "the bank leg to ERPNext's own default."},
             'tag': _STR, 'name': _STR},
            required=('match_type', 'match_value'), mutating=True),
        'handler': _create_rule},
    'update_rule': {
        **_tool(
            'Update one categorization rule, changing ONLY the fields you pass '
            '(name, match_type, match_value, offset_account, cost_center, '
            'bank_cost_center, party_type, party_name, auto_create_party, '
            'active, priority, tag, applies_to_company). '
            'Pass cost_center="" to clear it and hand BOTH JE legs back to '
            "ERPNext's own default; pass bank_cost_center=\"\" to restore the "
            'default of mirroring cost_center onto the bank leg. '
            'party_type is Supplier / Customer / Employee / Shareholder / Auto, '
            'or "" for no party; it rides the OFFSET leg only, because ERPNext '
            'accepts a Party solely on an account whose account_type is '
            'Receivable, Payable or Equity — or is not set at all — and a bank '
            'line never qualifies. An offset ERPNext positively refuses books NO '
            'party and logs why rather than producing an entry that cannot be '
            'submitted. '
            'NON-DESTRUCTIVE: like an edit in the admin UI this writes a NEW '
            'rule version and archives the old one (returns the new id in '
            'updated_rule.id and the archived one in superseded_rule_id), so '
            'the version that generated any past Journal Entry stays readable — '
            'expect the id to CHANGE. REFUSES an archived rule (a historical '
            'version that will never fire again — update its successor), an '
            'unknown match_type, and a cost_center ERPNext positively denies. '
            'Already-posted Journal Entries are NOT rewritten; only entries '
            'generated after this call use the new values. MUTATING — requires '
            'the update_rule kill switch to be ON.',
            {'rule_id': {'type': 'integer'},
             'name': _STR, 'match_type': _STR, 'match_value': _STR,
             'offset_account': _STR,
             'cost_center': {
                 'type': 'string',
                 'description': 'ERPNext Cost Center docname for BOTH JE '
                                'legs. "" clears it.'},
             'bank_cost_center': {
                 'type': 'string',
                 'description': 'Bank-leg override. "" restores the default '
                                '(mirror cost_center). "(none)" leaves the '
                                "bank leg to ERPNext's own default."},
             'party_type': {
                 'type': 'string',
                 'description': 'Supplier | Customer | Employee | Shareholder '
                                '| Auto, or "" for no party. Auto derives the '
                                "side from the offset account's type."},
             'party_name': {
                 'type': 'string',
                 'description': 'The party. "" clears it, which falls back to '
                                "the transaction's merchant."},
             'auto_create_party': {
                 'type': 'boolean',
                 'description': 'Create the party when ERPNext has none. Omit '
                                'to inherit the global setting. An Employee is '
                                'NEVER created (ERPNext requires a birth date '
                                'and joining date) — it must already exist.'},
             'active': _BOOL, 'priority': {'type': 'integer'},
             'tag': _STR, 'applies_to_company': _STR},
            required=('rule_id',), mutating=True),
        'handler': _update_rule},
    'create_advisory_agreement': {
        **_tool(
            'Register an investment advisory agreement — the write side of '
            'get_advisory_agreement_summary. Records the signed terms (both '
            'legal parties, objective, horizon, fee basis, billing cadence, '
            'effective/termination dates, and a Governance Document reference) '
            'against ONE managed brokerage account, identified by its 4-digit '
            'plaid_account_mask. '
            'REFUSES: an unknown mask; a mask already governed by an active '
            'agreement (one account has one governing agreement — terminate or '
            'amend the incumbent with update_advisory_agreement first); an '
            'objective / fee_type / billing_frequency outside its vocabulary; a '
            'fee_type whose amount is missing (Percent of AUM needs '
            'fee_percent_of_aum, Flat Annual needs fee_flat_annual, Hybrid '
            'needs both); a termination_date on or before the effective_date. '
            'DOES NOT BILL ANYTHING. Registration records terms; it leaves the '
            "agreement's three kill switches OFF, its settlement accounts "
            'blank, and its bank-fee and performance-fee rates at 0.0, so no '
            'Journal Entry can be posted until an operator configures those on '
            '/admin/advisory. The returned not_yet_configured list names each '
            'gap — relay it. '
            'REVERSIBLE: an agreement is superseded or terminated, never '
            'deleted, and nothing already posted is rewritten. '
            'fee_percent_of_aum is stated as a document states it — 1.0 means '
            '1% annualized. MUTATING — requires the create_advisory_agreement '
            'kill switch to be ON.',
            {'agreement_name': _STR,
             'client_entity': {
                 'type': 'string',
                 'description': 'Legal client entity, e.g. "Orchard Meadow, '
                                'LLC" — the party whose assets are managed.'},
             'advisor_entity': {
                 'type': 'string',
                 'description': 'Legal advisor entity, e.g. "Wells Fargo '
                                'Advisors LLC".'},
             'plaid_account_mask': {
                 'type': 'string',
                 'description': 'The 4-digit brokerage mask, e.g. "9401". Must '
                                'match a linked Plaid account (see '
                                'get_account_topology).'},
             'objective': {'type': 'string',
                           'description': 'Aggressive Growth | Growth | '
                                          'Moderate Growth | Income | Capital '
                                          'Preservation | Custom'},
             'investment_horizon_years': {'type': 'integer'},
             'fee_type': {'type': 'string',
                          'description': 'Percent of AUM | Flat Annual | '
                                         'Performance | Hybrid'},
             'fee_percent_of_aum': {
                 'type': 'number',
                 'description': 'Annualized percent, e.g. 1.0 for 1%.'},
             'fee_flat_annual': {'type': 'number'},
             'billing_frequency': {'type': 'string',
                                   'description': 'Monthly | Quarterly | '
                                                  'Semi-Annual | Annual'},
             'effective_date': {'type': 'string', 'description': 'YYYY-MM-DD'},
             'termination_date': {'type': 'string',
                                  'description': 'YYYY-MM-DD; omit for '
                                                 'open-ended'},
             'applies_to_company': {
                 'type': 'string',
                 'description': 'ERPNext Company docname the fee entries are '
                                'scoped to.'},
             'document_reference': {
                 'type': 'string',
                 'description': 'Governance Document doc_id or filename in '
                                'ERPNext — links this record to the signed PDF.'}},
            required=('agreement_name', 'client_entity', 'advisor_entity',
                      'plaid_account_mask', 'effective_date'), mutating=True),
        'handler': _create_advisory_agreement},
    'update_advisory_agreement': {
        **_tool(
            'Amend one advisory agreement, changing ONLY the fields you pass '
            '(agreement_name, client_entity, advisor_entity, objective, '
            'investment_horizon_years, fee_type, fee_percent_of_aum, '
            'fee_flat_annual, billing_frequency, effective_date, '
            'termination_date, applies_to_company, document_reference, status). '
            'NON-DESTRUCTIVE: like update_rule this writes a NEW agreement '
            'version and marks the old one superseded (returns the new id in '
            'updated_agreement_id and the old one in superseded_agreement_id), '
            'so the terms that governed any past fee accrual stay readable — '
            'expect the id to CHANGE. The fee/AUM/performance history follows '
            'the NEW id, so the live dashboard is unbroken; the superseded row '
            'keeps only its terms and dates. '
            'REFUSES: an unknown id; a SUPERSEDED version (history — amend its '
            'successor); an empty patch; status="superseded" (that is set by an '
            'amendment, not passed to one — pass "terminated" to end the '
            'agreement); and every vocabulary, date-order and fee-basis rule '
            'create_advisory_agreement enforces. '
            'The managed account CANNOT be changed — an agreement over a '
            'different account is a new agreement. Already-posted fee Journal '
            'Entries are NOT rewritten. MUTATING — requires the '
            'update_advisory_agreement kill switch to be ON.',
            {'agreement_id': {'type': 'integer'},
             'agreement_name': _STR, 'client_entity': _STR,
             'advisor_entity': _STR, 'objective': _STR,
             'investment_horizon_years': {'type': 'integer'},
             'fee_type': _STR, 'fee_percent_of_aum': {'type': 'number'},
             'fee_flat_annual': {'type': 'number'}, 'billing_frequency': _STR,
             'effective_date': {'type': 'string', 'description': 'YYYY-MM-DD'},
             'termination_date': {'type': 'string',
                                  'description': 'YYYY-MM-DD; ends the '
                                                 'agreement'},
             'applies_to_company': _STR, 'document_reference': _STR,
             'status': {'type': 'string',
                        'description': 'active | terminated'}},
            required=('agreement_id',), mutating=True),
        'handler': _update_advisory_agreement},
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
            'all accounts, AND re-derive the trade-leg pairings alongside — '
            'the fix when list_unpaired_trades reports totals_agree=false, or '
            'when mark_to_market_delta is missing on anchors built before '
            'v0.8.0. Both are derived from data already held; nothing is '
            'fetched from Plaid and nothing is posted to ERPNext. MUTATING — '
            'requires the rebuild_anchors kill switch ON.',
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
    # ── v0.8.4 · the admin surface, as tools ────────────────────────────────
    'get_draft_health': {
        **_tool(
            'How many DRAFT Journal Entries ERPNext is holding right now, how '
            'much money is in them, how old the oldest is, and how they group '
            'by user_remark prefix (Cash / Cash withdrawal = investment '
            'settlement legs, Bought / Sold = trades, the rest = the bank-side '
            'rules engine). Flags `breached` when the count is over the alert '
            'threshold, which is LEARNED from this install\'s own history (the '
            'P95 of prior healthy readings) once there are enough observations '
            'and is a starting default of 50 before that. `crossed` is true '
            'ONLY on the reading where a healthy queue first goes over the '
            'line — this is a state query, not a scheduled alert, so calling '
            'it repeatedly while breached does not re-alert. Run it after any '
            'bulk post or backfill: the v0.8.4 sync left 112 duplicate '
            'settlement-leg drafts standing for hours because nothing was '
            'watching this number. Read-only. Optional `company` scopes the '
            'count; optional `threshold` overrides the learned one for a '
            'what-if.',
            {'company': _STR, 'threshold': {'type': 'integer'}}),
        'handler': _get_draft_health},
    # ── v0.9.0 · statement-to-books drift ───────────────────────────────────
    'get_statement_recon_report': {
        **_tool(
            'For every RECONCILED statement period, what the STATEMENT reports '
            'per category (dividends, interest, buys, sells, fees, deposits, '
            'withdrawals, mark-to-market) against what Bank Bridge actually '
            'BOOKED into ERPNext, with the delta and a status of matched / '
            'drifted / unexplained. USE THIS to answer "did we book everything '
            'the statement says happened?" — a question neither of the other '
            'two guards answers. get_reconciliation_status reconciles CASH '
            '(whether Plaid mirrored what the bank saw) and is blind to what '
            'reached the ledger afterwards; the duplicate check catches an '
            'entry written TWICE. Neither notices a period that booked 24 of '
            '26 dividends — the cash balances and nothing is re-emitted, and '
            'two dividends of income are missing from the books. That is the '
            'v0.8.4 failure shape. The drift threshold is LEARNED (the P95 of '
            'this account and category\'s own prior deltas) once there are 20 '
            'observations, and is a 5% starting default before that, with '
            'every non-zero delta flagged while the baseline is still forming. '
            '`booked_amount` is what the GL holds and `booked_draft_amount` is '
            'what is staged but unsubmitted — a category that only matches '
            'once you count drafts is a submit backlog, not a drift, and says '
            'so in its reason. Periods that could NOT be compared are listed '
            'under `skipped` with a reason rather than omitted. CATEGORIES ARE '
            'NOT ADDITIVE: they come from overlapping statement blocks, so do '
            'not sum a column and expect the period\'s cash movement. '
            'Read-only. Optional `account_mask` or `account_id` narrows it to '
            'one account; `findings_only` drops the matched rows.',
            {'account_mask': _STR, 'account_id': _STR,
             'findings_only': _BOOL}),
        'handler': _get_statement_recon_report},
    'get_clearing_status': {
        **_tool(
            "What a paired brokerage's Cash Clearing account ACTUALLY holds in "
            'ERPNext, what Bank Bridge projects it should hold, and how many '
            'same-account settlement legs are still unposted. The two numbers '
            'disagreeing is the v0.8.4 bug: before it, every trade credited '
            'clearing and its settlement half was dropped, so the projection '
            'read ~0 while the ledger ran to -$1,011,119.41. Run this BEFORE '
            'post_clearing_cleanup_je — a non-zero unposted_settlements means '
            'the backfill, not a cleanup JE, is what fixes the balance. '
            'Read-only.',
            {'account_id': _STR}, required=('account_id',)),
        'handler': _get_clearing_status},
    'rerun_rules': {
        **_tool(
            'Re-run the CURRENT categorization rules over posted, non-removed '
            'transactions that have no Journal Entry yet, and generate the JEs '
            'that match. Idempotent — a transaction that already produced a JE '
            'is skipped, so running twice generates nothing the second time. '
            'Refuses when Journal Entry generation is switched off, rather than '
            'reporting a successful run that posted nothing. Returns '
            '{considered, matched, generated}. MUTATING — requires the '
            'rerun_rules kill switch ON.',
            {}, mutating=True),
        'handler': _rerun_rules},
    'reset_investment_drafts': {
        **_tool(
            'Delete every DRAFT investment Journal Entry — both the ones Bank '
            "Bridge's tracker names and the ORPHANS it does not (a JE whose "
            'tracker row was lost to a partial delete, which the pre-v0.8.4 '
            'reset left standing) — so the next post rebuilds them. ABORTS on '
            'the first SUBMITTED entry, deleting nothing further: submitted '
            'entries are real ledger history. Pass account_id to scope it to '
            'one brokerage. Returns {tracker_deleted, orphan_deleted, '
            'total_deleted, aborted}. MUTATING — requires the '
            'reset_investment_drafts kill switch ON.',
            {'account_id': _STR}, mutating=True),
        'handler': _reset_investment_drafts},
    'post_clearing_cleanup_je': {
        **_tool(
            'Write ONE DRAFT Journal Entry moving whatever is left in Cash '
            "Clearing onto the brokerage's own GL leaf. This is the SECOND "
            'step, not the first: the settlement legs v0.8.4 restored relieve '
            'clearing by themselves when the investment post re-runs, so this '
            'REFUSES (unless dry_run) while get_clearing_status still reports '
            'unposted settlements. dry_run defaults TRUE and returns the '
            'document it would write — posting a six-figure correction to a '
            'live ledger should take a deliberate second call. Never submits; '
            'what it writes is a draft an operator reviews. MUTATING — '
            'requires the post_clearing_cleanup_je kill switch ON.',
            {'account_id': _STR,
             'dry_run': {'type': 'boolean',
                         'description': 'default true — preview only'}},
            required=('account_id',), mutating=True),
        'handler': _post_clearing_cleanup_je},
    'enable_je_gate': {
        **_tool(
            'Turn ON Journal Entry generation for the whole install, '
            'persistently — the setting that was ERPNEXT_AUTO_GENERATE_'
            'JOURNAL_ENTRIES and needed a compose edit plus an app recreate to '
            'flip. Distinct from enable_je_posting, which is the per-Item '
            'INVESTMENT switch; this is the master gate over the bank-transaction '
            'rules engine. MUTATING — requires the enable_je_gate kill switch ON.',
            {}, mutating=True),
        'handler': _enable_je_gate},
    'disable_je_gate': {
        **_tool(
            'Turn OFF Journal Entry generation for the whole install, '
            'persistently. Transactions keep syncing and rules keep matching; '
            'nothing is written to the general ledger. The pause to reach for '
            'when a rule is posting wrongly. MUTATING — requires the '
            'disable_je_gate kill switch ON.',
            {}, mutating=True),
        'handler': _disable_je_gate},
    # ── v0.8.5 · sync on demand ─────────────────────────────────────────────
    'sync_now': {
        **_tool(
            'Run a sync RIGHT NOW: pull fresh transactions from Plaid into the '
            'local mirror, push them to ERPNext as Bank Transactions, and (by '
            'default) post the investment Journal Entries the brokerages have '
            'mirrored. Exactly what the Sync Now button on /admin does — same '
            'function, same behaviour — and the tool to reach for when drafts '
            'or settlement legs need to exist before another tool can act on '
            'them. It is a MANUAL trigger: it runs once, does only what you '
            'ask, and changes no schedule.\n\n'
            'account_id scopes the run to ONE Plaid account (its bank is '
            'polled, and only that account posts) — the retry to use after a '
            'partial failure, rather than re-running the whole sweep. Omit it '
            'to sync every connected bank.\n'
            'include_investments (default true) also runs the investment JE '
            'post and mark-to-market; false leaves the bank-transaction sync '
            'alone.\n'
            'dry_run (default false) reports what WOULD post from what is '
            'already mirrored and contacts Plaid not at all — so it cannot '
            'know how many new transactions the bank is holding, and says so.\n\n'
            'Returns a per-account summary — transactions fetched, Bank '
            'Transactions posted, investment JEs drafted, how many were '
            'skipped as already-posted duplicates, and structured errors with '
            'a code and a remedy — plus totals, status (ok | partial | '
            'failed) and elapsed seconds. A partial run reports what LANDED '
            'alongside what broke; one bank failing never stops the others. '
            'MUTATING, and the only tool here that spends billable Plaid '
            'calls — requires the sync_now kill switch ON.',
            {'account_id': {'type': 'string',
                            'description': 'Plaid account_id to scope to; '
                                           'omit for every account'},
             'include_investments': {
                 'type': 'boolean',
                 'description': 'default true — also post investment JEs'},
             'dry_run': {'type': 'boolean',
                         'description': 'default false — preview only, '
                                        'contacts nothing'}},
            mutating=True),
        'handler': _sync_now},
    'set_erpnext_config': {
        **_tool(
            'Set the ERPNext connection — URL, API key, API secret, default '
            'Company. Only the fields you PASS are changed; omit api_secret to '
            'keep the stored one. The secret is never echoed back, only a '
            'masked preview. Same fields as /admin/erpnext_settings. MUTATING — '
            'requires the set_erpnext_config kill switch ON.',
            {'url': _STR, 'api_key': _STR, 'api_secret': _STR,
             'default_company': _STR}, mutating=True),
        'handler': _set_erpnext_config},
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
