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
                          statements_months: int = 24,
                          access_token: str = None,
                          liabilities: bool = False,
                          investments: bool = False) -> str:
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
        statements available. Plaid also enforces a HARD ceiling of 24 months
        on the request itself (a 400 with
        `Statements product only supports a maximum of 2 years of data`), so a
        wider ask is clamped to 24 in `_with_statements` — a caller that
        wanted 36 quietly gets 24 rather than the whole link-token creation
        failing. See v0.4.17.

        `access_token` (v0.4.11) switches Link into UPDATE MODE: instead of
        creating a new Item, Link re-authenticates the EXISTING one. This is the
        only way to repair an ITEM_LOGIN_REQUIRED without losing everything, and
        it matters for two independent reasons.

        Correctness: update mode preserves `item_id` AND every `account_id`, so
        the operator's Company assignment, ERPNext account mapping and opening
        balance all keep working. A fresh link mints new ids for the same real
        accounts, which orphans all of it (see app/reconnect.py for what it
        costs and how the fallback recovers it).

        Cost: a fresh link creates a second billable Item at Plaid for a bank
        already being paid for. Update mode does not.

        Plaid REJECTS `products` alongside `access_token` — an Item's products
        are fixed at creation and update mode cannot change them — so both the
        product list and the `statements` request are dropped here rather than
        left to fail at the API."""
        api = self._get_api()
        if access_token:
            return self._link_token_create(
                api, self._update_mode_kwargs(user_id, redirect_uri, webhook,
                                              access_token))
        base = self._link_token_kwargs(user_id, redirect_uri, webhook)

        # The optional products, each independently subject to approval on the
        # Plaid application. Asking for one the application doesn't hold is a
        # hard 400 that would otherwise make "Link a bank" stop working
        # entirely, so we degrade through progressively smaller sets rather
        # than all-or-nothing: an install approved for `statements` but not
        # `liabilities` still gets statements.
        wanted = []
        if statements:
            wanted.append('statements')
        if liabilities:
            wanted.append('liabilities')
        if investments:
            wanted.append('investments')
        for attempt in self._product_ladder(wanted):
            kwargs = dict(base)
            if 'statements' in attempt:
                kwargs = self._with_statements(kwargs, statements_months)
            if 'liabilities' in attempt:
                kwargs = self._with_liabilities(kwargs)
            if 'investments' in attempt:
                kwargs = self._with_investments(kwargs)
            try:
                return self._link_token_create(api, kwargs)
            except PlaidError as e:
                if not attempt:
                    raise       # even transactions-only failed — a real error
                log.warning(
                    'create_link_token with %s failed (%s) — retrying with a '
                    'smaller product set. Those products stay unavailable for '
                    'this Item until they are enabled on the Plaid application '
                    'and the bank is re-linked.', ' + '.join(attempt) or 'none',
                    e)
        raise PlaidError('create_link_token exhausted every product '
                         'combination')  # pragma: no cover - ladder ends at []

    @staticmethod
    def _product_ladder(wanted: list) -> list:
        """Product sets to try, richest first, always ending at the empty set
        (transactions only — the pre-v0.4.9 token that must always work).

        For two optional products that is [both] → [a] → [b] → [], so exactly
        one unapproved product costs one extra call and still yields the other.
        Ordinary (fully-approved) links succeed on the first attempt and pay
        nothing for this."""
        if not wanted:
            return [[]]
        if len(wanted) == 1:
            return [list(wanted), []]
        return [list(wanted)] + [[p] for p in wanted] + [[]]

    @staticmethod
    def _with_liabilities(kwargs: dict) -> dict:
        """Request the `liabilities` product (v0.4.14).

        v0.4.23 · request as OPTIONAL, not required. Putting liabilities in
        `products` means Plaid Link errors out with "No liability accounts"
        when the user's institution connection has no eligible accounts —
        which is the routine case for a business account with checking +
        savings and no loans or cards. `optional_products` grants liabilities
        when eligible accounts exist and stays silent otherwise, so linking
        a deposit-only bank succeeds instead of failing at the account
        selection screen. Unlike `products` there's no user-facing consent
        screen for optional products, which is correct for liabilities
        (there's nothing to gate on when the account can't produce the
        data anyway)."""
        from plaid.model.products import Products
        kwargs = dict(kwargs)
        kwargs['optional_products'] = list(
            kwargs.get('optional_products') or []) + [Products('liabilities')]
        return kwargs

    @staticmethod
    def _with_investments(kwargs: dict) -> dict:
        """Request the `investments` product as OPTIONAL (v0.4.26).

        Same reason liabilities is optional (see `_with_liabilities`): an
        institution connection with no brokerage/IRA/401k accounts would
        fail the account-selection screen with 'No investment accounts' if
        the product were required. Optional grants the product on any
        Item that DOES have investment accounts and stays silent otherwise,
        so a bank-only user's link keeps succeeding.

        Investments unlocks Plaid's `/investments/holdings/get` (current
        positions with security_id, ticker, quantity, cost basis, price)
        and `/investments/transactions/get` (buys, sells, dividends,
        splits, transfers with full security detail). Bank Bridge's
        consumer for those endpoints is the v0.5.0 lot-tracking module —
        this v0.4.26 change opens the pipe so Items minted from now on
        already carry the product when v0.5.0 lands."""
        from plaid.model.products import Products
        kwargs = dict(kwargs)
        kwargs['optional_products'] = list(
            kwargs.get('optional_products') or []) + [Products('investments')]
        return kwargs

    # v0.4.25 · request the maximum historical window Plaid will grant. Default
    # if unset is 90 days; the ceiling is 730 (Plaid enforces the cap and rejects
    # anything higher). This is the value that determines how far back the
    # initial `/transactions/sync` will reach, and it is FIXED AT LINK TIME —
    # Plaid explicitly refuses to change it on an existing Item, so this window
    # is what every subsequent sync will keep filling forward from. For a book-
    # keeping bridge that wants 'the last two years plus everything since', the
    # right default is the maximum: bank statements go back that far, and a
    # backfilled ledger that starts at 730 days is materially better than one
    # that starts at 90.
    TRANSACTIONS_DAYS_REQUESTED = 730

    def _link_token_kwargs(self, user_id, redirect_uri, webhook) -> dict:
        """The LinkTokenCreateRequest kwargs common to both attempts."""
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.country_code import CountryCode
        from plaid.model.products import Products
        from plaid.model.link_token_transactions import LinkTokenTransactions
        kwargs = dict(
            user=LinkTokenCreateRequestUser(client_user_id=str(user_id)),
            client_name='ERPNext Bank Bridge',
            products=[Products('transactions')],
            # v0.4.25 · ask Plaid to pull the maximum 730 days of history at
            # link time. Unset defaults to 90 days and cannot be updated after
            # link — every Item minted without this kwarg is stuck at 90 days.
            transactions=LinkTokenTransactions(
                days_requested=PlaidClient.TRANSACTIONS_DAYS_REQUESTED),
            country_codes=[CountryCode('US')],
            language='en')
        ru = redirect_uri if redirect_uri is not None else self.redirect_uri
        if ru:
            kwargs['redirect_uri'] = ru
        wh = webhook if webhook is not None else self.webhook_url
        if wh:
            kwargs['webhook'] = wh
        return kwargs

    def _update_mode_kwargs(self, user_id, redirect_uri, webhook,
                            access_token) -> dict:
        """LinkTokenCreateRequest kwargs for update mode (v0.4.11).

        Deliberately built by SUBTRACTION from the normal kwargs rather than
        assembled separately, so anything added to a link token later (a new
        country, a language change) reaches update mode too and the two cannot
        drift. `products` is the one thing removed: Plaid rejects it outright
        alongside an access_token.

        `redirect_uri` is kept — an OAuth bank needs it on re-auth exactly as it
        did on the first link."""
        kwargs = self._link_token_kwargs(user_id, redirect_uri, webhook)
        kwargs.pop('products', None)
        kwargs['access_token'] = access_token
        return kwargs

    # Plaid caps the Statements window at 24 months / ~2 years and 400s with
    # `Statements product only supports a maximum of 2 years of data` on
    # anything wider. Any month → days conversion has to sit safely UNDER that
    # ceiling regardless of leap years — 30 * months does; 31 * months does
    # not (24 months × 31 = 744 days, over the 730-ish cap).
    STATEMENTS_MAX_MONTHS = 24
    STATEMENTS_DAYS_PER_MONTH = 30

    @staticmethod
    def _with_statements(kwargs: dict, months: int) -> dict:
        """Add the `statements` product + its required date window to a set of
        link-token kwargs. Plaid requires BOTH: the product in the list, and a
        `statements` config naming the period Link should request.

        Plaid caps the Statements window at 24 months
        (`STATEMENTS_MAX_MONTHS`); a wider ask is a hard 400 that takes the
        entire link-token creation with it, so we silently clamp here — a
        caller asking for 36 months is quietly downgraded to 24, which is
        strictly better than a Link that won't open at all. See v0.4.17."""
        from datetime import date, timedelta
        from plaid.model.link_token_create_request_statements import \
            LinkTokenCreateRequestStatements
        from plaid.model.products import Products
        end = date.today()
        months_capped = max(1, min(int(months or 1),
                                   PlaidClient.STATEMENTS_MAX_MONTHS))
        start = end - timedelta(
            days=PlaidClient.STATEMENTS_DAYS_PER_MONTH * months_capped)
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

    # ── liabilities (v0.4.14) ────────────────────────────────────────

    def liabilities_get(self, access_token: str) -> dict:
        """Plaid's liability detail for this Item, keyed by account_id:
        {account_id: {'liability_type', 'interest_rate', 'ytd_interest_paid',
        'ytd_principal_paid', 'next_payment_due_date', 'last_payment_amount',
        'last_payment_date', 'origination_principal_amount', 'maturity_date',
        'minimum_payment_amount', 'raw': {...}}}.

        `raw` carries Plaid's whole object verbatim, because the three liability
        shapes (mortgage / student / credit) each field things the others don't
        — escrow balance, PSLF status, loan term — and promoting all of them
        would be a column sprawl for data only a human reads. Postgres can query
        into the stored JSON if a real use ever appears.

        Returns {} — never raises — when the Item can't serve Liabilities. That
        is the COMMON case, not an edge one: the product must be approved on the
        Plaid application AND requested at Link time AND offered by the
        institution. Same deliberate asymmetry as statements_list: a missing
        optional product is a fact about the install, not an error, and every
        consumer degrades to 'no liability detail' (see app/loans.py)."""
        from plaid.model.liabilities_get_request import LiabilitiesGetRequest
        api = self._get_api()
        try:
            resp = api.liabilities_get(
                LiabilitiesGetRequest(access_token=access_token))
        except Exception as e:
            log.info('liabilities unavailable for this item: %s', e)
            return {}
        payload = _to_dict(_get(_to_dict(resp), 'liabilities', {})) or {}
        out: dict = {}
        for kind in ('mortgage', 'student', 'credit'):
            for entry in (_get(payload, kind, []) or []):
                row = _normalize_liability(entry, kind)
                if row.get('account_id'):
                    out[row['account_id']] = row
        return out

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


def _as_float(value):
    """A float, or None. Plaid returns Decimals and occasional strings."""
    if value is None or value == '':
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _as_iso(value) -> str:
    """'YYYY-MM-DD', or ''. Plaid hands dates back as date objects or strings
    depending on the SDK path."""
    if value is None:
        return ''
    if hasattr(value, 'isoformat'):
        return value.isoformat()[:10]
    return str(value)[:10]


def _normalize_liability(entry, kind: str) -> dict:
    """One /liabilities/get object, flattened to the fields loan accounting
    needs, with the original kept under `raw`.

    The three shapes differ, and the differences matter: a mortgage carries
    `interest_rate` as an OBJECT ({percentage, type}) while a student loan
    carries `interest_rate_percentage` as a bare number, and only mortgage and
    student report year-to-date interest and principal at all. Reconciling that
    here means app/loans.py never has to know which kind it is holding."""
    d = _to_dict(entry)
    rate = _get(d, 'interest_rate_percentage')
    if rate is None:
        rate_obj = _to_dict(_get(d, 'interest_rate', {})) or {}
        rate = _get(rate_obj, 'percentage')
    return {
        'account_id': _get(d, 'account_id'),
        'liability_type': kind,
        'interest_rate': _as_float(rate),
        'ytd_interest_paid': _as_float(_get(d, 'ytd_interest_paid')),
        'ytd_principal_paid': _as_float(_get(d, 'ytd_principal_paid')),
        'next_payment_due_date': _as_iso(_get(d, 'next_payment_due_date')),
        'last_payment_amount': _as_float(_get(d, 'last_payment_amount')),
        'last_payment_date': _as_iso(_get(d, 'last_payment_date')),
        'origination_principal_amount': _as_float(
            _get(d, 'origination_principal_amount')),
        'origination_date': _as_iso(_get(d, 'origination_date')),
        'maturity_date': _as_iso(_get(d, 'maturity_date') or
                                 _get(d, 'expected_payoff_date')),
        'minimum_payment_amount': _as_float(
            _get(d, 'minimum_payment_amount') or
            _get(d, 'next_monthly_payment')),
        'raw': _to_dict(d),
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
