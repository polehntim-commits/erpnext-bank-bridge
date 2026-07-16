# SPDX-License-Identifier: MIT
"""Thin ERPNext (Frappe) REST client for the bank-transaction bridge.

Wraps `requests` with:
  * token auth  (Authorization: token <key>:<secret>)
  * exponential-backoff retry (3 attempts, 1s/3s/9s) on connection errors
    and 5xx responses — transient Umbrel-network / Frappe-restart blips
  * typed errors: raises ERPNextAPIError on 4xx (carrying status + body)
  * an optional per-request `on_log` hook so the sync layer can persist an
    ERPNextSyncLog row for debugging

Deliberately dependency-light and DB-free: it knows nothing about our models
so it stays trivially unit-testable by patching `requests.request`. The
higher-level orchestration (idempotent find-or-create, custom-field bootstrap,
ERPNextSyncLog writes) lives in app/erpnext_sync.py.

Frappe REST surface used:
  POST   /api/resource/{doctype}                 create
  GET    /api/resource/{doctype}/{name}          read one
  PUT    /api/resource/{doctype}/{name}          update
  GET    /api/resource/{doctype}?filters&fields  list
  GET    /api/method/{dotted.method}             whitelisted RPC
  POST   /api/method/upload_file                 file attach
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

log = logging.getLogger('bankbridge.erpnext')


class ERPNextError(Exception):
    """Base for every bridge error."""


class ERPNextConfigError(ERPNextError):
    """Raised when the client is asked to act but isn't configured."""


class ERPNextAPIError(ERPNextError):
    """A 4xx (or exhausted-retry 5xx / connection) response from Frappe.

    `status_code` is the HTTP status (None for a pure connection failure);
    `response_body` is the raw text (possibly a Frappe traceback / _server_
    messages blob) so callers can log it."""

    # How much of the (often huge) Frappe traceback/_server_messages blob to
    # fold into the human error string. The full body stays on .response_body.
    BODY_SNIPPET = 500

    def __init__(self, message, status_code=None, response_body=''):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or ''

    def __str__(self):
        """Base message plus a truncated response body, so every place that
        logs/flashes str(e) (sync log, admin flash, server log) shows the actual
        Frappe error — not just 'POST … -> 417'."""
        base = super().__str__()
        if self.response_body:
            return f'{base}: {self.response_body[:self.BODY_SNIPPET]}'
        return base


@dataclass
class ERPNextConfig:
    url: str
    api_key: str
    api_secret: str
    default_company: str = ''

    @property
    def base(self) -> str:
        return self.url.rstrip('/')

    def is_complete(self) -> bool:
        return bool(self.url and self.api_key and self.api_secret)


# Retry schedule (seconds) — attempt 1 immediate, then wait these before
# attempts 2 and 3. Referenced through the instance so tests can zero it out.
RETRY_BACKOFFS = (1, 3, 9)


class ERPNextClient:
    def __init__(self, config: ERPNextConfig, *, timeout: int = 30,
                 on_log=None, session=None):
        """`on_log(entry: dict)` — optional callback invoked once per HTTP
        request with {method, url, status, request_body, response_body,
        error}. The sync layer uses it to persist ERPNextSyncLog rows.
        `session` lets a caller inject a requests.Session (or a stub)."""
        if not config.is_complete():
            raise ERPNextConfigError(
                'ERPNext client requires url + api_key + api_secret')
        self.config = config
        self.timeout = timeout
        self.on_log = on_log
        self._session = session

    # ── low-level ────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            'Authorization': f'token {self.config.api_key}:{self.config.api_secret}',
            'Accept': 'application/json',
        }

    def _sleep(self, seconds: float) -> None:  # patched out in tests
        time.sleep(seconds)

    def _emit_log(self, method, url, status, req_body, resp_body, error) -> None:
        if self.on_log is None:
            return
        try:
            self.on_log({
                'method': method, 'url': url, 'status': status,
                'request_body': req_body, 'response_body': resp_body,
                'error': error,
            })
        except Exception:  # pragma: no cover - logging must never crash a call
            log.warning('erpnext on_log hook raised; ignoring', exc_info=True)

    def _do(self, method, url, *, params=None, json_body=None, files=None,
            data=None):
        """Issue the request through requests (or the injected session),
        retrying on connection errors + 5xx. Returns the requests.Response
        on any non-retryable outcome (2xx OR 4xx — the caller classifies)."""
        caller = self._session.request if self._session is not None else requests.request
        last_exc = None
        attempts = len(RETRY_BACKOFFS) + 1
        for attempt in range(attempts):
            try:
                resp = caller(
                    method, url, headers=self._headers(), params=params,
                    json=json_body, files=files, data=data, timeout=self.timeout)
            except requests.RequestException as e:
                last_exc = e
                if attempt < attempts - 1:
                    wait = RETRY_BACKOFFS[attempt]
                    log.warning('erpnext %s %s connection error (attempt %d/%d): '
                                '%s — retrying in %ss', method, url, attempt + 1,
                                attempts, e.__class__.__name__, wait)
                    self._sleep(wait)
                    continue
                raise ERPNextAPIError(
                    f'connection failed after {attempts} attempts: {e}',
                    status_code=None) from e
            # 5xx → retry; everything else (2xx/3xx/4xx) is terminal here.
            if resp.status_code >= 500 and attempt < attempts - 1:
                wait = RETRY_BACKOFFS[attempt]
                log.warning('erpnext %s %s -> %d (attempt %d/%d) — retrying in %ss',
                            method, url, resp.status_code, attempt + 1, attempts, wait)
                self._sleep(wait)
                continue
            return resp
        # Unreachable — the loop always returns or raises. Defensive:
        raise ERPNextAPIError(f'request failed: {last_exc}', status_code=None)

    def request(self, method, path, *, params=None, json_body=None,
                files=None, data=None):
        """Core request → parsed JSON dict. Raises ERPNextAPIError on >=400.

        `path` is appended to the configured base (leading slash optional).
        Frappe wraps resource/method payloads in a top-level {"data": ...} or
        {"message": ...}; we return the decoded envelope as-is so callers can
        pick the key they expect."""
        url = self.config.base + ('/' + path.lstrip('/'))
        req_body = None
        if json_body is not None:
            try:
                req_body = json.dumps(json_body)
            except (TypeError, ValueError):
                req_body = str(json_body)
        error = None
        resp = None
        try:
            resp = self._do(method, url, params=params, json_body=json_body,
                            files=files, data=data)
        except ERPNextAPIError as e:
            error = str(e)
            self._emit_log(method, url, e.status_code, req_body, e.response_body, error)
            raise
        text = resp.text or ''
        if resp.status_code >= 400:
            error = f'HTTP {resp.status_code}'
            self._emit_log(method, url, resp.status_code, req_body, text, error)
            # Surface every real 4xx in the server log with the Frappe body so a
            # failure is diagnosable from `docker logs` alone. 404 is skipped: it
            # is normal find-or-create control flow (get_doc probes swallow it).
            if resp.status_code != 404:
                log.warning('erpnext %s %s -> %d: %s', method, url,
                            resp.status_code, text[:ERPNextAPIError.BODY_SNIPPET])
            raise ERPNextAPIError(
                f'{method} {url} -> {resp.status_code}',
                status_code=resp.status_code, response_body=text)
        self._emit_log(method, url, resp.status_code, req_body, text, None)
        if not text:
            return {}
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError):
            return {'_raw': text}

    # ── resource helpers ─────────────────────────────────────────────

    @staticmethod
    def _seg(name: str) -> str:
        """URL-encode a doctype / docname path segment (Frappe names can
        contain spaces, e.g. 'I-9 Form' or 'HR-EMP-0001')."""
        return quote(str(name), safe='')

    def get_doc(self, doctype, name):
        """GET one document → its dict, or None on 404."""
        try:
            out = self.request(
                'GET', f'/api/resource/{self._seg(doctype)}/{self._seg(name)}')
        except ERPNextAPIError as e:
            if e.status_code == 404:
                return None
            raise
        return out.get('data', out)

    def create_doc(self, doctype, doc: dict):
        """POST a new document → the created document's dict."""
        out = self.request('POST', f'/api/resource/{self._seg(doctype)}',
                           json_body=doc)
        return out.get('data', out)

    def update_doc(self, doctype, name, doc: dict):
        """PUT changes onto an existing document → the updated dict."""
        out = self.request(
            'PUT', f'/api/resource/{self._seg(doctype)}/{self._seg(name)}',
            json_body=doc)
        return out.get('data', out)

    def list_docs(self, doctype, *, filters=None, fields=None,
                  limit_page_length=0, order_by=None):
        """GET a filtered list → list of dicts. `limit_page_length=0` asks
        Frappe for every match (no pagination)."""
        params = {'limit_page_length': limit_page_length}
        if filters is not None:
            params['filters'] = json.dumps(filters)
        if fields is not None:
            params['fields'] = json.dumps(fields)
        if order_by:
            params['order_by'] = order_by
        out = self.request('GET', f'/api/resource/{self._seg(doctype)}',
                           params=params)
        return out.get('data', []) if isinstance(out, dict) else []

    def call_method(self, method, *, params=None, http_method='GET',
                    json_body=None):
        """Hit a whitelisted RPC endpoint (/api/method/<dotted.path>) → the
        decoded `message` value (Frappe's RPC envelope key)."""
        out = self.request(http_method, f'/api/method/{method}',
                           params=params, json_body=json_body)
        return out.get('message', out) if isinstance(out, dict) else out

    def get_logged_user(self):
        """Identity of the API user behind the key — the Test Connection
        probe. Returns the user's email/id string."""
        return self.call_method('frappe.auth.get_logged_user')

    def upload_file(self, filename, content_bytes, *, doctype=None, docname=None,
                    is_private=1, fieldname=None):
        """Attach a file to a document via /api/method/upload_file. Returns
        the created File document dict (has `file_url`, `name`)."""
        files = {'file': (filename, content_bytes)}
        data = {'is_private': str(is_private)}
        if doctype:
            data['doctype'] = doctype
        if docname:
            data['docname'] = docname
        if fieldname:
            data['fieldname'] = fieldname
        out = self.request('POST', '/api/method/upload_file',
                           files=files, data=data)
        return out.get('message', out) if isinstance(out, dict) else out
