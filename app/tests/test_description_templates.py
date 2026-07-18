# SPDX-License-Identifier: MIT
"""Description Template auto-fill + rendering (v0.4.0.4).

  * default_description_template: the per-match-type auto-fill, with
    {{offset_short}} resolved from the offset account's logical name
  * render_description_template: {{variable}} substitution, missing-variable +
    separator compaction, amount formatting, merchant→description fallback
  * render_description: template wins when present; sensible default when blank
    (never overwrites operator content)
  * /api/rules/preview_description: renders against the most recent matching
    transaction, else a placeholder

    cd app
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402
from app import categorization  # noqa: E402
from app.models import (BankTransaction, CategorizationRule,  # noqa: E402
                        PlaidAccount, PlaidItem)


def _txn(**kw):
    """A stand-in transaction (only the attributes the renderer reads)."""
    defaults = dict(merchant_name='Chevron', name='CHEVRON 0123456',
                    amount=42.5, iso_currency_code='USD',
                    category='Transportation > Gas Stations',
                    date=date(2026, 7, 10))
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ── default_description_template ────────────────────────────────────────

class TestDefaultTemplates(unittest.TestCase):
    def test_merchant_exact_and_contains(self):
        for mt in ('merchant_exact', 'merchant_contains'):
            self.assertEqual(
                categorization.default_description_template(mt, 'Fuel Expense - EC'),
                '{{merchant_name}} - Fuel Expense')

    def test_description_regex_strips_number_and_suffix(self):
        # logical name drops both the leading '5100 - ' and trailing ' - EC'.
        self.assertEqual(
            categorization.default_description_template(
                'description_regex', '5100 - Fuel Expense - EC'),
            'Fuel Expense - {{amount}}')

    def test_plaid_category_template(self):
        self.assertEqual(
            categorization.default_description_template(
                'plaid_category_matches', 'Meals & Entertainment'),
            '{{plaid_category}} - Meals & Entertainment - {{merchant_name}}')

    def test_amount_range_preserves_internal_dash(self):
        # 'Owner - Draws' has no Company suffix to strip — the inner ' - ' stays.
        self.assertEqual(
            categorization.default_description_template(
                'amount_range', 'Owner - Draws - EC'),
            'Owner - Draws - {{merchant_name}} - {{amount}}')

    def test_unknown_match_type_falls_back(self):
        self.assertEqual(
            categorization.default_description_template('mystery', 'Fuel Expense - EC'),
            '{{merchant_name}} - Fuel Expense')

    def test_default_is_deterministic_for_reset(self):
        # "Reset to default" reproduces the same string every time.
        a = categorization.default_description_template('merchant_exact', 'Fuel - EC')
        b = categorization.default_description_template('merchant_exact', 'Fuel - EC')
        self.assertEqual(a, b)
        self.assertEqual(a, '{{merchant_name}} - Fuel')


# ── render_description_template ─────────────────────────────────────────

class TestRenderTemplate(unittest.TestCase):
    def test_all_variables_resolve(self):
        out = categorization.render_description_template(
            '{{merchant_name}} - {{plaid_category}} - {{amount}} - {{date}}', _txn())
        self.assertEqual(out, 'Chevron - Transportation - 42.50 USD - 2026-07-10')

    def test_missing_merchant_falls_back_to_description(self):
        out = categorization.render_description_template(
            '{{merchant_name}}', _txn(merchant_name=''))
        self.assertEqual(out, 'CHEVRON 0123456')

    def test_missing_category_compacts_separators(self):
        out = categorization.render_description_template(
            '{{merchant_name}} - {{plaid_category}} - {{amount}}',
            _txn(category=''))
        self.assertEqual(out, 'Chevron - 42.50 USD')

    def test_leading_and_trailing_separators_trimmed(self):
        # empty leading category and trailing merchant both trimmed away.
        out = categorization.render_description_template(
            '{{plaid_category}} - {{amount}} - {{merchant_name}}',
            _txn(category='', merchant_name='', name=''))
        self.assertEqual(out, '42.50 USD')

    def test_blank_template_renders_empty(self):
        self.assertEqual(categorization.render_description_template('', _txn()), '')
        self.assertEqual(
            categorization.render_description_template('   ', _txn()), '')

    def test_whitespace_inside_braces_tolerated(self):
        out = categorization.render_description_template(
            '{{  merchant_name  }}', _txn())
        self.assertEqual(out, 'Chevron')

    def test_unknown_variable_becomes_empty(self):
        out = categorization.render_description_template(
            '{{merchant_name}} - {{nope}}', _txn())
        self.assertEqual(out, 'Chevron')

    def test_resolved_value_dash_preserved(self):
        # A ' - ' INSIDE a resolved value must survive compaction.
        out = categorization.render_description_template(
            '{{merchant_name}}', _txn(merchant_name='AT&T - Wireless'))
        self.assertEqual(out, 'AT&T - Wireless')


# ── amount formatting ───────────────────────────────────────────────────

class TestAmountFormatting(unittest.TestCase):
    def _amt(self, amount, currency='USD'):
        return categorization.render_description_template(
            '{{amount}}', _txn(amount=amount, iso_currency_code=currency,
                               merchant_name='', name=''))

    def test_positive(self):
        self.assertEqual(self._amt(42.5), '42.50 USD')

    def test_negative_keeps_sign(self):
        self.assertEqual(self._amt(-30), '-30.00 USD')

    def test_decimal_precision_rounds(self):
        self.assertEqual(self._amt(12.999), '13.00 USD')

    def test_non_usd_currency(self):
        self.assertEqual(self._amt(5, 'CAD'), '5.00 CAD')


# ── render_description (the rule wrapper) ────────────────────────────────

class TestRenderDescription(unittest.TestCase):
    def _rule(self, template):
        return CategorizationRule(name='Fuel', match_type='merchant_contains',
                                  match_value='Chevron',
                                  description_template=template)

    def test_template_present_is_used(self):
        out = categorization.render_description(
            self._rule('{{merchant_name}} - fuel'), _txn())
        self.assertEqual(out, 'Chevron - fuel')

    def test_blank_template_uses_default_remark(self):
        out = categorization.render_description(self._rule(''), _txn())
        self.assertIn('Fuel', out)          # rule name appears in the default
        self.assertIn('Chevron', out)


# ── /api/rules/preview_description ──────────────────────────────────────

class PreviewBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'FERNET_KEY': '',
            'SCHEDULER_ENABLED': False,
        })
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)

    def _account(self, account_id='acct-x'):
        db.session.add(PlaidItem(item_id='item-x',
                                 access_token_encrypted=crypto.encrypt('tok'),
                                 institution_id='ins', institution_name='Bank',
                                 status='active'))
        db.session.add(PlaidAccount(account_id=account_id, item_id='item-x',
                                    name='Checking', type='depository',
                                    subtype='checking', sync_enabled=True))
        db.session.commit()

    def _txn_row(self, tid, merchant, amount, d):
        db.session.add(BankTransaction(
            plaid_transaction_id=tid, account_id='acct-x', amount=amount,
            merchant_name=merchant, name=merchant.upper(), category='GAS',
            date=d))
        db.session.commit()


class TestPreviewEndpoint(PreviewBase):
    def test_uses_most_recent_matching_transaction(self):
        self._account()
        self._txn_row('t1', 'Chevron', 10.0, date(2026, 7, 1))
        self._txn_row('t2', 'Chevron', 55.5, date(2026, 7, 15))  # newer
        r = self.client.get('/api/rules/preview_description', query_string={
            'match_type': 'merchant_contains', 'match_value': 'Chevron',
            'offset_account': 'Fuel Expense - EC',
            'template': '{{merchant_name}} - {{amount}}'})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['used_sample'])
        self.assertEqual(body['preview'], 'Chevron - 55.50 USD')  # from t2

    def test_blank_template_uses_generated_default(self):
        self._account()
        self._txn_row('t1', 'Chevron', 10.0, date(2026, 7, 1))
        r = self.client.get('/api/rules/preview_description', query_string={
            'match_type': 'merchant_contains', 'match_value': 'Chevron',
            'offset_account': 'Fuel Expense - EC'})   # no template → default
        body = r.get_json()
        self.assertEqual(body['template'], '{{merchant_name}} - Fuel Expense')
        self.assertEqual(body['preview'], 'Chevron - Fuel Expense')
        self.assertTrue(body['used_sample'])

    def test_falls_back_to_placeholder_when_no_match(self):
        self._account()
        self._txn_row('t1', 'Starbucks', 5.0, date(2026, 7, 1))  # won't match
        r = self.client.get('/api/rules/preview_description', query_string={
            'match_type': 'merchant_contains', 'match_value': 'Chevron',
            'offset_account': 'Fuel Expense - EC',
            'template': '{{merchant_name}} - {{offset_short}}'})
        body = r.get_json()
        self.assertFalse(body['used_sample'])
        self.assertIn('Sample Merchant', body['preview'])

    def test_no_transactions_at_all_uses_placeholder(self):
        # No account, no rows — the endpoint still renders against a placeholder.
        r = self.client.get('/api/rules/preview_description', query_string={
            'match_type': 'merchant_exact', 'match_value': 'Anything',
            'offset_account': 'Meals - EC',
            'template': '{{merchant_name}}'})
        body = r.get_json()
        self.assertFalse(body['used_sample'])
        self.assertEqual(body['preview'], 'Sample Merchant')


if __name__ == '__main__':
    unittest.main()
