# SPDX-License-Identifier: MIT
"""Thin wrapper over the official `plaid-python` SDK.

Exposes a small, dict-in/dict-out surface (create_link_token,
exchange_public_token, get_item, get_accounts, transactions_sync) so the sync
engine and blueprints never touch Plaid's model classes directly — and so unit
tests can inject a fake client with the same method signatures without the SDK
installed.

The SDK is imported LAZILY inside each method (and the constructor), so this
module imports cleanly in an environment without `plaid` — the app boots and
the test suite runs; only an actual Plaid call needs the wheel. The higher-
level orchestration (DB upserts, ERPNext push, idempotency) lives in
app/sync_engine.py.

Plaid amount convention: a POSITIVE amount is money leaving the account
(a withdrawal); a NEGATIVE amount is money entering (a deposit). We preserve
that raw sign in the local mirror and translate to ERPNext's deposit/withdrawal
split at push time (see app/erpnext_bank.py)."""
from __future__ import annotations

import logging

log = logging.getLogger('bankbridge.plaid')


class PlaidError(Exception):
    """Any failure talking to Plaid (config or API)."""


class PlaidConfigError(PlaidError):
    """Raised when a call is attempted before Plaid is configured."""


def _host_for(environment: str):
    """Map our environment string → the SDK's host constant."""
    import plaid
    if environment == 'production':
        return plaid.Environment.Production
    return plaid.Environment.Sandbox


class PlaidClient:
    """Wraps a low-level plaid_api.PlaidApi. Build the production one with
    `PlaidClient.from_settings(...)`; tests inject a fake via `api=`."""

    def __init__(self, client_id: str = '', secret: str = '',
                 environment: str = 'sandbox', *, api=None,
                 redirect_uri: str = '', webhook_url: str = ''):
        self.client_id = client_id
        self.secret = secret
        self.environment = environment
        self.redirect_uri = redirect_uri or ''
        self.webhook_url = webhook_url or ''
        self._api = api  # injected (tests) or built lazily on first use

    # ── construction ─────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: dict, active_secret: str) -> 'PlaidClient':
        """Build a client from the merged plaid_settings dict + the active
        environment's secret."""
        if not settings.get('client_id') or not active_secret:
            raise PlaidConfigError(
                'Plaid is not configured — set Client ID + the active '
                "environment's secret in Plaid settings.")
        return cls(
            client_id=settings['client_id'], secret=active_secret,
            environment=settings.get('environment', 'sandbox'),
            redirect_uri=settings.get('redirect_uri', ''),
            webhook_url=settings.get('webhook_url', ''))

    def _get_api(self):
        if self._api is not None:
            return self._api
        if not self.client_id or not self.secret:
            raise PlaidConfigError('Plaid client requires client_id + secret')
        import plaid
        from plaid.api import plaid_api
        configuration = plaid.Configuration(
            host=_host_for(self.environment),
            api_key={'clientId': self.client_id, 'secret': self.secret})
        self._api = plaid_api.PlaidApi(plaid.ApiClient(configuration))
        return self._api

    # ── link / token exchange ────────────────────────────────────────

    def create_link_token(self, user_id: str, *, redirect_uri: str = None,
                          webhook: str = None) -> str:
        """Create a short-lived link_token for Plaid Link. `redirect_uri` is
        required for OAuth-only banks (Wells Fargo) and must be registered in
        the Plaid dashboard. Returns the link_token string."""
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.country_code import CountryCode
        from plaid.model.products import Products

        api = self._get_api()
        kwargs = dict(
            user=LinkTokenCreateRequestUser(client_user_id=str(user_id)),
            client_name='ERPNext Bank Bridge',
            products=[Products('transactions')],
            country_codes=[CountryCode('US')],
            language='en')
        ru = redirect_uri if redirect_uri is not None else self.redirect_uri
        if ru:
            kwargs['redirect_uri'] = ru
        wh = webhook if webhook is not None else self.webhook_url
        if wh:
            kwargs['webhook'] = wh
        try:
            resp = api.link_token_create(LinkTokenCreateRequest(**kwargs))
        except Exception as e:  # plaid.ApiException + anything else
            raise PlaidError(f'create_link_token failed: {e}') from e
        return _resp_get(resp, 'link_token')

    def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        """Exchange a Link public_token for a durable (access_token, item_id)."""
        from plaid.model.item_public_token_exchange_request import \
            ItemPublicTokenExchangeRequest
        api = self._get_api()
        try:
            resp = api.item_public_token_exchange(
                ItemPublicTokenExchangeRequest(public_token=public_token))
        except Exception as e:
            raise PlaidError(f'exchange_public_token failed: {e}') from e
        return _resp_get(resp, 'access_token'), _resp_get(resp, 'item_id')

    # ── item / institution / accounts ────────────────────────────────

    def get_item(self, access_token: str) -> dict:
        """Fetch the Item → {'item_id', 'institution_id'}."""
        from plaid.model.item_get_request import ItemGetRequest
        api = self._get_api()
        try:
            resp = api.item_get(ItemGetRequest(access_token=access_token))
        except Exception as e:
            raise PlaidError(f'get_item failed: {e}') from e
        item = _resp_get(resp, 'item') or {}
        item = item if isinstance(item, dict) else _to_dict(item)
        return {'item_id': item.get('item_id'),
                'institution_id': item.get('institution_id')}

    def get_institution_name(self, institution_id: str) -> str:
        """Resolve an institution_id → display name (best-effort; '' on miss)."""
        if not institution_id:
            return ''
        from plaid.model.institutions_get_by_id_request import \
            InstitutionsGetByIdRequest
        from plaid.model.country_code import CountryCode
        api = self._get_api()
        try:
            resp = api.institutions_get_by_id(InstitutionsGetByIdRequest(
                institution_id=institution_id, country_codes=[CountryCode('US')]))
        except Exception as e:
            log.warning('get_institution_name failed for %s: %s', institution_id, e)
            return ''
        inst = _resp_get(resp, 'institution') or {}
        inst = inst if isinstance(inst, dict) else _to_dict(inst)
        return inst.get('name', '') or ''

    def get_institution_details(self, institution_id: str) -> dict:
        """Best-effort institution metadata for enriching an ERPNext Bank
        record → {'name', 'url'}. Plaid exposes the institution's website as
        `url` (no SWIFT in the public schema). Returns {} on any miss."""
        if not institution_id:
            return {}
        from plaid.model.institutions_get_by_id_request import \
            InstitutionsGetByIdRequest
        from plaid.model.country_code import CountryCode
        api = self._get_api()
        try:
            resp = api.institutions_get_by_id(InstitutionsGetByIdRequest(
                institution_id=institution_id, country_codes=[CountryCode('US')]))
        except Exception as e:
            log.warning('get_institution_details failed for %s: %s',
                        institution_id, e)
            return {}
        inst = _resp_get(resp, 'institution') or {}
        inst = inst if isinstance(inst, dict) else _to_dict(inst)
        return {'name': inst.get('name', '') or '', 'url': inst.get('url', '') or ''}

    def get_accounts(self, access_token: str) -> list[dict]:
        """List accounts for an Item → list of normalized dicts."""
        from plaid.model.accounts_get_request import AccountsGetRequest
        api = self._get_api()
        try:
            resp = api.accounts_get(AccountsGetRequest(access_token=access_token))
        except Exception as e:
            raise PlaidError(f'get_accounts failed: {e}') from e
        accounts = _resp_get(resp, 'accounts') or []
        return [_normalize_account(a) for a in accounts]

    def transactions_sync(self, access_token: str, cursor: str = None,
                          count: int = 500) -> dict:
        """One page of /transactions/sync. Returns a normalized dict:
        {'added': [...], 'modified': [...], 'removed': [...],
         'next_cursor': str, 'has_more': bool}. Caller loops while has_more."""
        from plaid.model.transactions_sync_request import TransactionsSyncRequest
        api = self._get_api()
        kwargs = dict(access_token=access_token, count=count)
        if cursor:
            kwargs['cursor'] = cursor
        try:
            resp = api.transactions_sync(TransactionsSyncRequest(**kwargs))
        except Exception as e:
            raise PlaidError(f'transactions_sync failed: {e}') from e
        return {
            'added': [_normalize_txn(t) for t in (_resp_get(resp, 'added') or [])],
            'modified': [_normalize_txn(t) for t in (_resp_get(resp, 'modified') or [])],
            'removed': [_normalize_removed(t) for t in (_resp_get(resp, 'removed') or [])],
            'next_cursor': _resp_get(resp, 'next_cursor') or '',
            'has_more': bool(_resp_get(resp, 'has_more')),
        }


# ── response normalization helpers ────────────────────────────────────
#
# Plaid SDK responses are model objects with attribute access + a .to_dict();
# our fakes return plain dicts. These helpers read either shape so the same
# normalization runs in production and in tests.

def _to_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, 'to_dict'):
        try:
            return obj.to_dict()
        except Exception:
            pass
    return obj


def _resp_get(resp, key):
    d = _to_dict(resp)
    if isinstance(d, dict):
        return d.get(key)
    return getattr(resp, key, None)


def _get(d, key, default=None):
    d = _to_dict(d)
    if isinstance(d, dict):
        return d.get(key, default)
    return getattr(d, key, default)


def _normalize_account(a) -> dict:
    d = _to_dict(a)
    balances = _to_dict(_get(d, 'balances', {})) or {}
    return {
        'account_id': _get(d, 'account_id'),
        'name': _get(d, 'name', '') or '',
        'official_name': _get(d, 'official_name', '') or '',
        'mask': _get(d, 'mask', '') or '',
        'type': str(_get(d, 'type', '') or ''),
        'subtype': str(_get(d, 'subtype', '') or ''),
        'balance_available': _get(balances, 'available'),
        'balance_current': _get(balances, 'current'),
        'iso_currency_code': _get(balances, 'iso_currency_code', 'USD') or 'USD',
    }


def _category_str(d) -> str:
    """Plaid's legacy `category` is a list; the newer `personal_finance_category`
    is a dict. Prefer the PFC detailed label, else join the legacy list."""
    pfc = _to_dict(_get(d, 'personal_finance_category', None))
    if isinstance(pfc, dict) and pfc.get('detailed'):
        return str(pfc.get('detailed'))
    cat = _get(d, 'category', None)
    if isinstance(cat, (list, tuple)):
        return ' > '.join(str(c) for c in cat)
    return str(cat or '')


def _normalize_txn(t) -> dict:
    d = _to_dict(t)
    date_val = _get(d, 'date')
    return {
        'transaction_id': _get(d, 'transaction_id'),
        'account_id': _get(d, 'account_id'),
        'amount': float(_get(d, 'amount', 0.0) or 0.0),
        'iso_currency_code': _get(d, 'iso_currency_code', 'USD') or 'USD',
        'date': str(date_val) if date_val is not None else None,
        'name': _get(d, 'name', '') or '',
        'merchant_name': _get(d, 'merchant_name', '') or '',
        'category': _category_str(d),
        'pending': bool(_get(d, 'pending', False)),
    }


def _normalize_removed(t) -> dict:
    d = _to_dict(t)
    return {'transaction_id': _get(d, 'transaction_id')}
