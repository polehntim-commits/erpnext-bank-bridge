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
                          webhook: str = None, statements: bool = False,
                          statements_months: int = 24) -> str:
        """Create a short-lived link_token for Plaid Link. `redirect_uri` is
        required for OAuth-only banks (Wells Fargo) and must be registered in
        the Plaid dashboard. Returns the link_token string.

        `statements` (v0.4.9) additionally requests the `statements` product, so
        the resulting Item can serve /statements/list — bank-issued statement
        PDFs for opening balances and monthly reconciliation. It is opt-in and
        FAILS SOFT: `statements` must be enabled on the Plaid application before
        Plaid will mint a token for it, and asking for a product the application
        doesn't hold is a hard 400 that would otherwise make "Link a bank" stop
        working entirely. So a rejected statements request is retried once
        without it, which lands on exactly the pre-v0.4.9 token.

        That retry is the whole reason this is safe to default on for the
        operator: an install whose Plaid application hasn't been approved for
        Statements yet keeps linking banks exactly as it did, and the feature
        starts working on the next link after approval with no code change.

        `statements_months` is how far back Link asks the institution to make
        statements available (Plaid caps this per institution; asking for more
        than a bank offers just yields fewer)."""
        api = self._get_api()
        kwargs = self._link_token_kwargs(user_id, redirect_uri, webhook)
        if statements:
            with_statements = self._with_statements(dict(kwargs),
                                                    statements_months)
            try:
                return self._link_token_create(api, with_statements)
            except PlaidError as e:
                log.warning(
                    'create_link_token with the `statements` product failed '
                    '(%s) — retrying without it. Statements stay unavailable '
                    'for this Item until `statements` is enabled on the Plaid '
                    'application and the bank is re-linked.', e)
        return self._link_token_create(api, kwargs)

    def _link_token_kwargs(self, user_id, redirect_uri, webhook) -> dict:
        """The LinkTokenCreateRequest kwargs common to both attempts."""
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.country_code import CountryCode
        from plaid.model.products import Products
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
        return kwargs

    @staticmethod
    def _with_statements(kwargs: dict, months: int) -> dict:
        """Add the `statements` product + its required date window to a set of
        link-token kwargs. Plaid requires BOTH: the product in the list, and a
        `statements` config naming the period Link should request."""
        from datetime import date, timedelta
        from plaid.model.link_token_create_request_statements import \
            LinkTokenCreateRequestStatements
        from plaid.model.products import Products
        end = date.today()
        start = end - timedelta(days=31 * max(1, int(months or 1)))
        kwargs['products'] = list(kwargs.get('products') or []) + \
            [Products('statements')]
        kwargs['statements'] = LinkTokenCreateRequestStatements(
            start_date=start, end_date=end)
        return kwargs

    @staticmethod
    def _link_token_create(api, kwargs: dict) -> str:
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
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

    def item_remove(self, access_token: str) -> dict:
        """Disconnect an Item at Plaid via /item/remove (v0.4.7).

        This is the API side of the disconnect promise in PRIVACY.md: it tells
        Plaid to invalidate the access_token and stop pulling from the
        institution. It is IRREVERSIBLE and one-way — the token is dead
        afterwards, and re-linking the same bank mints a brand-new Item.

        Removing the Item at Plaid does not touch anything on our side; the
        caller is responsible for marking the local PlaidItem (and it must NOT
        delete the local rows — the mirrored transactions and the Journal
        Entries generated from them stay in ERPNext either way).

        Returns {'request_id': str} — /item/remove's response carries no other
        field. Raises PlaidError on any API failure so the caller can refuse to
        mark the Item disconnected when Plaid never accepted the removal."""
        from plaid.model.item_remove_request import ItemRemoveRequest
        api = self._get_api()
        try:
            resp = api.item_remove(ItemRemoveRequest(access_token=access_token))
        except Exception as e:
            raise PlaidError(f'item_remove failed: {e}') from e
        return {'request_id': _resp_get(resp, 'request_id') or ''}

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

    # ── statements (v0.4.9) ──────────────────────────────────────────

    def statements_list(self, access_token: str) -> list[dict]:
        """Every statement Plaid holds for this Item, flattened across accounts:
        [{'account_id', 'statement_id', 'month', 'year', 'date_posted'}].

        NOTE what is NOT here, because it shapes everything downstream: Plaid's
        /statements/list carries no balances and no explicit period bounds. The
        period is `month` + `year` and the only other field is a nullable
        `date_posted`. Opening and closing balances exist ONLY inside the PDF,
        which is why app/statements.py parses them out of extracted text rather
        than reading them off this response.

        Returns [] — never raises — when the Item can't serve Statements. That
        is the common case, not an edge one: the product must be approved on the
        Plaid application AND requested at Link time AND supported by the
        institution, so an install that has none of that must degrade to "no
        statements" rather than break the caller. A genuine outage looks the
        same from here and is safe to treat identically, because every caller's
        fallback (the v0.4.4 estimate) is correct on its own."""
        from plaid.model.statements_list_request import StatementsListRequest
        api = self._get_api()
        try:
            resp = api.statements_list(
                StatementsListRequest(access_token=access_token))
        except Exception as e:
            log.warning('statements_list unavailable for this Item: %s', e)
            return []
        out = []
        for acct in (_resp_get(resp, 'accounts') or []):
            d = _to_dict(acct)
            account_id = _get(d, 'account_id')
            for st in (_get(d, 'statements', []) or []):
                s = _to_dict(st)
                statement_id = _get(s, 'statement_id')
                if not statement_id:
                    continue
                posted = _get(s, 'date_posted')
                out.append({
                    'account_id': account_id,
                    'statement_id': statement_id,
                    'month': _int_or_none(_get(s, 'month')),
                    'year': _int_or_none(_get(s, 'year')),
                    'date_posted': str(posted) if posted is not None else None,
                })
        return out

    def statements_download(self, access_token: str,
                            statement_id: str) -> bytes:
        """One statement's PDF as raw bytes, via /statements/download.

        Raises PlaidError on any failure — UNLIKE statements_list, which returns
        empty. The asymmetry is deliberate: listing failing means "this Item has
        no statements", a normal state with a correct fallback, whereas a
        download failing means "Plaid told us this exact statement exists and
        then wouldn't give it to us", which is a transient fault worth retrying
        (see statements.download_with_retry). Swallowing it would silently store
        an empty PDF.

        The SDK declares this endpoint's return as `file_type`, so what comes
        back is a file-like object rather than bytes; _read_bytes normalizes
        every shape (bytes, a read()-able, or a path) to bytes."""
        from plaid.model.statements_download_request import \
            StatementsDownloadRequest
        api = self._get_api()
        try:
            resp = api.statements_download(StatementsDownloadRequest(
                access_token=access_token, statement_id=statement_id))
        except Exception as e:
            raise PlaidError(
                f'statements_download failed for {statement_id}: {e}') from e
        data = _read_bytes(resp)
        if not data:
            raise PlaidError(
                f'statements_download returned no bytes for {statement_id}')
        return data


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


def _int_or_none(value):
    """An int, or None for anything that isn't one (Plaid's month/year are
    required by the schema, but a fake or a future response shape may omit
    them and a statement with no period is still worth listing)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_bytes(resp) -> bytes:
    """Coerce whatever /statements/download handed back into PDF bytes.

    The SDK types this endpoint as `file_type` and, depending on version and on
    the _preload_content setting, returns a tempfile-backed file object, an
    object with .read(), raw bytes, or a path string. Tests inject plain bytes.
    Anything unreadable yields b'' so the caller raises a clear PlaidError
    rather than persisting a truncated PDF."""
    if isinstance(resp, (bytes, bytearray)):
        return bytes(resp)
    read = getattr(resp, 'read', None)
    if callable(read):
        try:
            data = read()
            return data if isinstance(data, bytes) else bytes(data or b'')
        except Exception:  # pragma: no cover - defensive
            return b''
    # A path to a downloaded temp file (the SDK's file_type default).
    if isinstance(resp, str):
        try:
            with open(resp, 'rb') as fh:
                return fh.read()
        except OSError:  # pragma: no cover - defensive
            return b''
    data = getattr(resp, 'data', None)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    return b''


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
