# SPDX-License-Identifier: MIT
"""Every categorization rule belongs to exactly one Company (v1.0.9).

WHY. Until v1.0.9 a rule with a blank `applies_to_company` was
"company-agnostic" and fired on every Company's transactions, resolving a
logical offset ("Member Distributions") to whichever account that Company
happened to have. On a one-Company install that was harmless; it stops being
harmless the day a second entity's bank account is linked. On OML 65 of 67
rules were blank, so linking Constancy Farms, LLC would have sent a Constancy
cash withdrawal to OML's Member Distributions (rule 182 "CASH"), a payment to
Tim to OML management fees (171/172), and Oregon DOR payroll tax to a licence
fee (168).

THE RULE NOW. A rule matches a transaction only when
  * the rule HAS a Company, and
  * the transaction's account EXPLICITLY belongs to that same Company — its own
    owning Company, or its Item's (`matching_company`). An account nobody
    assigned matches NOTHING once any Company has ever been assigned, rather
    than borrowing the default Company's rules — which is precisely how a
    Constancy account linked without picking a Company would otherwise have
    been categorized with OML's rules. Only an install that has never assigned
    a Company at all still falls back to its default (the pre-v1.0.9 shape).

THE ONE-TIME PIN. So that OML's own accounts keep matching, `pin_items` stamps
every Item that has no Company with the install's single Company — once, at the
v1.0.9 upgrade, recorded by an AuditEvent marker so a LATER unassigned Item
(say, Constancy linked without a choice) is never silently pinned to OML.
Blank-scope rules match nothing: fail closed. `scope_report` lists them (it
should list none) and `apply_scope` — run at boot by migrations.py — assigns
them to the install's Company.

WHERE THE MIGRATION TARGET COMES FROM, strongest first:
  1. RULE_SCOPE_MIGRATION_COMPANY (env) — an operator's explicit answer;
  2. the one Company every already-scoped live rule shares (on OML, rules 110
     and 184 both say "Orchard Meadow, LLC");
  3. the default Company, when it is the only Company in play.
Anything else is ambiguous: nothing is assigned, the report says why, and the
unscoped rules stay inert until someone chooses.
"""
from __future__ import annotations

import logging
import os

from flask import current_app

from . import audit
from . import db
from .models import CategorizationRule, PlaidAccount, PlaidItem

log = logging.getLogger('bankbridge.rule_scope')


def _scope(value) -> str:
    return (value or '').strip()


def default_company() -> str:
    from . import erpnext_settings
    return _scope(erpnext_settings.load().get('default_company'))


def companies_in_play() -> set:
    """Every Company an account can resolve to: the default plus every explicit
    owning Company on an Item or account."""
    out = {c for (c,) in db.session.query(PlaidItem.owning_company).filter(
        PlaidItem.owning_company.isnot(None)).distinct() if _scope(c)}
    out |= {c for (c,) in db.session.query(PlaidAccount.owning_company).filter(
        PlaidAccount.owning_company.isnot(None)).distinct() if _scope(c)}
    out = {_scope(c) for c in out}
    if default_company():
        out.add(default_company())
    return out


def any_company_assigned() -> bool:
    """Whether any Item or account carries an explicit owning Company."""
    for col in (PlaidItem.owning_company, PlaidAccount.owning_company):
        if db.session.query(col).filter(col.isnot(None),
                                        db.func.trim(col) != '').first():
            return True
    return False


def matching_company(account_id: str | None) -> str:
    """The Company a transaction on this account belongs to FOR RULE MATCHING,
    or '' when it cannot be said safely (then no rule matches)."""
    from . import erpnext_accounts
    acct = (PlaidAccount.query.filter_by(account_id=account_id).first()
            if account_id else None)
    explicit = erpnext_accounts.explicit_owning_company(acct)
    if explicit:
        return explicit
    if not any_company_assigned():
        return default_company()
    return ''


def unassigned_items():
    """Connected Items with no owning Company — their accounts match no rule
    unless the account itself names one."""
    return (PlaidItem.query
            .filter(PlaidItem.disconnected.is_(False),
                    db.or_(PlaidItem.owning_company.is_(None),
                           db.func.trim(PlaidItem.owning_company) == ''))
            .order_by(PlaidItem.id.asc()).all())


PIN_MARKER = 'items_company_pinned'


def pin_done() -> bool:
    from .models import AuditEvent
    return AuditEvent.query.filter_by(event_type=PIN_MARKER).first() is not None


def rule_in_scope(rule, row_company: str) -> bool:
    """A rule fires only inside its own Company. Blank on either side → no."""
    scope = _scope(getattr(rule, 'applies_to_company', None))
    return bool(scope) and bool(row_company) and scope == row_company


def unscoped_rules():
    """Live (non-archived) rules with no Company."""
    return (CategorizationRule.query
            .filter(CategorizationRule.archived.is_(False),
                    db.or_(CategorizationRule.applies_to_company.is_(None),
                           db.func.trim(CategorizationRule.applies_to_company)
                           == ''))
            .order_by(CategorizationRule.id.asc()).all())


def migration_target() -> tuple:
    """(company, reason) — the Company unscoped rules are assigned to, or
    ('', why-not) when the answer is not unambiguous."""
    try:
        override = _scope(current_app.config.get('RULE_SCOPE_MIGRATION_COMPANY')
                          or os.environ.get('RULE_SCOPE_MIGRATION_COMPANY'))
    except RuntimeError:
        override = _scope(os.environ.get('RULE_SCOPE_MIGRATION_COMPANY'))
    if override:
        return override, 'RULE_SCOPE_MIGRATION_COMPANY'
    scoped = {_scope(c) for (c,) in db.session.query(
        CategorizationRule.applies_to_company).filter(
        CategorizationRule.archived.is_(False),
        CategorizationRule.applies_to_company.isnot(None)).distinct()
        if _scope(c)}
    if len(scoped) == 1:
        return scoped.pop(), 'the one Company every scoped rule already uses'
    if len(scoped) > 1:
        return '', ('scoped rules already name several Companies ('
                    + ', '.join(sorted(scoped)) + ') — set '
                    'RULE_SCOPE_MIGRATION_COMPANY to choose')
    in_play = companies_in_play()
    if len(in_play) == 1:
        return next(iter(in_play)), 'the only Company in play'
    if not in_play:
        return '', 'no default Company is configured'
    return '', ('several Companies are in play (' + ', '.join(sorted(in_play))
                + ') — set RULE_SCOPE_MIGRATION_COMPANY to choose')


def scope_report() -> dict:
    """What the migration would do (or, after it ran, the confirmation that
    nothing is left). READ-ONLY."""
    target, reason = migration_target()
    rules = unscoped_rules()
    items = unassigned_items()
    return {
        'items_pin_done': pin_done(),
        'unassigned_items': [{'item_id': i.item_id,
                              'institution': i.institution_name or '',
                              'new_company': (
                                  target if (target and not pin_done()
                                             and companies_in_play()
                                             <= {target}) else None)}
                             for i in items],
        'unscoped_count': len(rules),
        'target_company': target or None,
        'target_reason': reason,
        'companies_in_play': sorted(companies_in_play()),
        'rules': [{'id': r.id, 'name': r.name or '', 'active': bool(r.active),
                   'match_type': r.match_type, 'match_value': r.match_value,
                   'offset_account': r.offset_account or '',
                   'current_scope': None, 'new_scope': target or None}
                  for r in rules],
    }


def apply_scope(*, dry_run: bool = True, actor: str = 'migration') -> dict:
    """Assign every unscoped live rule to the migration target. Idempotent; a
    no-op when nothing is unscoped or the target is ambiguous. In place (not a
    clone-and-supersede): a rule's scope was never a decision anyone made, so
    there is no prior version to preserve — the AuditEvent records the change."""
    report = scope_report()
    report['dry_run'] = dry_run
    target = report['target_company']
    report['items_pinned'] = 0
    if dry_run or not target:
        report['applied'] = 0
        return report
    # The one-time Item pin (see the module note) — never after the marker.
    if not report['items_pin_done'] and companies_in_play() <= {target}:
        pinned = [i.item_id for i in unassigned_items()]
        for item in unassigned_items():
            item.owning_company = target
        audit.record(PIN_MARKER, subject_type='PlaidItem', subject_id=target,
                     after={'company': target, 'item_ids': pinned},
                     notes=f'{len(pinned)} Item(s) pinned to {target} at the '
                           'v1.0.9 upgrade', actor=actor, commit=False)
        report['items_pinned'] = len(pinned)
        db.session.commit()
    if not report['rules']:
        report['applied'] = 0
        return report
    ids = [r['id'] for r in report['rules']]
    for rule in CategorizationRule.query.filter(
            CategorizationRule.id.in_(ids)).all():
        rule.applies_to_company = target
    audit.record('rules_company_scoped', subject_type='CategorizationRule',
                 subject_id=target,
                 after={'company': target, 'reason': report['target_reason'],
                        'rule_ids': ids},
                 notes=f'{len(ids)} unscoped rule(s) → {target}', actor=actor,
                 commit=False)
    db.session.commit()
    from . import categorization
    categorization.invalidate_rule_cache()
    log.info('scoped %d rule(s) to %s (%s)', len(ids), target,
             report['target_reason'])
    report['applied'] = len(ids)
    return report
