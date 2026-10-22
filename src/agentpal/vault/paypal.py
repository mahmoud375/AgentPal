"""Sandbox-only Payment Method Tokens v3. No Orders/Capture or POST retries."""

from contextvars import ContextVar
import json
import logging
import re
from time import monotonic
from urllib.parse import parse_qsl, urlsplit

import httpx

from agentpal.vault.security import VaultSettings, VaultUnavailable


_PRIVATE_IO = ContextVar("agentpal_private_paypal_io", default=False)


class _PrivateProviderLogs(logging.Filter):
    def filter(self, record):
        return not _PRIVATE_IO.get()


# HTTPX logs full URLs; httpcore DEBUG can include sensitive transport details.
# Filter just this integration's context, retaining unrelated application logs.
for _name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
              "httpcore.proxy", "httpcore.socks"):
    logging.getLogger(_name).addFilter(_PrivateProviderLogs())


def reference(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{7,36}", value):
        raise VaultUnavailable()
    return value


def customer(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value):
        raise VaultUnavailable()
    return value


def approval_url(value, setup_id: str) -> str:
    try:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
            raise ValueError
        p = urlsplit(value)
        if (p.scheme != "https" or p.netloc not in ("sandbox.paypal.com", "www.sandbox.paypal.com")
                or p.path != "/agreements/approve" or p.fragment or "\\" in value):
            raise ValueError
        params = parse_qsl(p.query, strict_parsing=True)
        if params != [("approval_session_id", setup_id)]:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise VaultUnavailable() from None


class PayPal:
    def __init__(self, settings: VaultSettings, *, transport: httpx.BaseTransport | None = None):
        self.settings, self.transport = settings, transport

    def _request(self, method: str, path: str, *, bearer: str | None = None,
                 payload: dict | None = None, request_id=None) -> dict:
        # Call sites below are the only source of method/path; never follow HATEOAS
        # links or redirects server-side. A new client owns no persistent OAuth state.
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        kwargs = {}
        if bearer is None:
            kwargs = {"auth": (self.settings.client_id.get_secret_value(), self.settings.client_secret.get_secret_value()),
                      "data": {"grant_type": "client_credentials"}}
        else:
            headers["Authorization"] = "Bearer " + bearer
            if payload is not None:
                kwargs["json"] = payload
            if request_id is not None:
                headers["PayPal-Request-Id"] = str(request_id)
        marker = _PRIVATE_IO.set(True)
        deadline = monotonic() + 20
        try:
            with httpx.Client(base_url=self.settings.base_url, transport=self.transport, trust_env=False,
                              follow_redirects=False, timeout=httpx.Timeout(10, connect=5),
                              limits=httpx.Limits(max_connections=1)) as client:
                with client.stream(method, path, headers=headers, **kwargs) as response:
                    if response.status_code not in ((200, 201) if method == "POST" else (200,)):
                        raise VaultUnavailable()
                    if response.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
                        raise VaultUnavailable()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if monotonic() > deadline or len(body) + len(chunk) > 65536:
                            raise VaultUnavailable()
                        body.extend(chunk)
                    value = json.loads(body)
                    if not isinstance(value, dict):
                        raise VaultUnavailable()
                    return value
        except (httpx.HTTPError, ValueError, RecursionError, UnicodeError):
            raise VaultUnavailable() from None
        finally:
            _PRIVATE_IO.reset(marker)

    def oauth(self) -> str:
        result = self._request("POST", "/v1/oauth2/token")
        value = result.get("access_token")
        if (not isinstance(value, str) or not 1 <= len(value) <= 8192 or
                any(ord(c) <= 32 or ord(c) >= 127 for c in value) or result.get("token_type", "").lower() != "bearer"):
            raise VaultUnavailable()
        return value

    def setup(self, payload: dict, request_id) -> dict:
        result = self._request("POST", "/v3/vault/setup-tokens", bearer=self.oauth(),
                               payload=payload, request_id=request_id)
        try:
            identifier = reference(result["id"])
            cid = customer(result["customer"]["id"])
            if result["status"] != "PAYER_ACTION_REQUIRED":
                raise VaultUnavailable()
            links = [link for link in result["links"] if link.get("rel") == "approve" and link.get("method") == "GET"]
            if len(links) != 1:
                raise VaultUnavailable()
            return {"id": identifier, "customer_id": cid, "approval_url": approval_url(links[0]["href"], identifier)}
        except (KeyError, TypeError, AttributeError):
            raise VaultUnavailable() from None

    def approved(self, setup: dict) -> None:
        result = self._request("GET", "/v3/vault/setup-tokens/" + reference(setup["id"]), bearer=self.oauth())
        try:
            if (result["id"] != setup["id"] or result["customer"]["id"] != setup["customer_id"]
                    or result["status"] != "APPROVED" or not isinstance(result["payment_source"]["paypal"], dict)):
                raise VaultUnavailable()
        except (KeyError, TypeError):
            raise VaultUnavailable() from None

    def tokenize(self, setup: dict, request_id) -> dict:
        payload = {"payment_source": {"token": {"id": reference(setup["id"]), "type": "SETUP_TOKEN"}}}
        result = self._request("POST", "/v3/vault/payment-tokens", bearer=self.oauth(),
                               payload=payload, request_id=request_id)
        try:
            return {"id": reference(result["id"]), "customer_id": customer(result["customer"]["id"])}
        except (KeyError, TypeError):
            raise VaultUnavailable() from None

    def verify(self, token: dict, setup: dict) -> None:
        result = self._request("GET", "/v3/vault/payment-tokens/" + reference(token["id"]), bearer=self.oauth())
        try:
            if (result["id"] != token["id"] or result["customer"]["id"] != token["customer_id"]
                    or token["customer_id"] != setup["customer_id"]
                    or not isinstance(result["payment_source"]["paypal"], dict)):
                raise VaultUnavailable()
        except (KeyError, TypeError):
            raise VaultUnavailable() from None
