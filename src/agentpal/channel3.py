"""Bounded Channel3 listing observations, never checkout or spending authority.

Field mappings follow scripts/spike_channel3.py. Offline fixtures verify our
contract, not provider behavior. No retailer URL is fetched or retained in clear.
"""

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


API_BASE = "https://api.trychannel3.com"
MAX_RESPONSE_BYTES = 2_000_000
MAX_OFFERS = 1000
MATCHING_VERSION = "channel3-product-domain-url-condition-v1"
_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class Channel3Error(Exception):
    """Only fixed codes cross the provider boundary; bodies/headers stay private."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Channel3Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)

    api_key: SecretStr = Field(validation_alias="CHANNEL3_API_KEY", repr=False)

    @field_validator("api_key")
    @classmethod
    def header_key(cls, value):
        key = value.get_secret_value()
        if not 1 <= len(key) <= 4096 or not key.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in key):
            raise ValueError("Invalid Channel3 configuration")
        return value


def valid_product_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def canonical_domain(value: object) -> str:
    # Case only: do not merge www, subdomains, suffixes or lookalike domains.
    if not isinstance(value, str) or not value.isascii() or len(value) > 253:
        raise ValueError("invalid_domain")
    domain = value.lower()
    if not _DOMAIN.fullmatch(domain):
        raise ValueError("invalid_domain")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise ValueError("invalid_domain")


def exact_usd_price(value: object) -> Decimal:
    """Accept exact JSON Decimal/int or ordinary decimal strings; never floats."""
    if type(value) is int:
        if not 0 <= value < 10**30:
            raise ValueError("invalid_price")
        result = Decimal(value)
    elif isinstance(value, Decimal):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,30}(?:\.[0-9]{1,2})?", value):
        result = Decimal(value)
    else:
        raise ValueError("invalid_price")
    if not result.is_finite() or result < 0 or result.as_tuple().exponent < -2 or result.adjusted() > 29:
        raise ValueError("invalid_price")
    return result


def _url_fingerprint(value: object) -> bytes:
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("invalid_url")
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.username is not None or parts.password is not None or parts.port not in (None, 443):
            raise ValueError
        canonical_domain(parts.hostname)
    except ValueError:
        raise ValueError("invalid_url") from None
    # Hash the entire exact URL, including attribution/query. Any change blocks
    # matching; this is not a provider ID. Redirect/affiliate hosts may differ.
    try:
        return hashlib.sha256(value.encode("utf-8")).digest()
    except UnicodeError:
        raise ValueError("invalid_url") from None


def _bounded_text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(
        ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)


@dataclass(frozen=True, slots=True)
class Offer:
    product_id: str
    merchant_domain: str
    title: str  # Untrusted display text, never instructions or authorization.
    url_fingerprint: bytes = field(repr=False)
    source_condition: str | None
    unit_price: Decimal
    source_availability: str | None
    retrieved_at: datetime
    # Keyword-only restoration preserves immutable evidence on durable replay;
    # the live parser still only constructs supported USD, unique v1 offers.
    currency: str = field(default="USD", kw_only=True)
    matching_version: str = field(default=MATCHING_VERSION, kw_only=True)
    matching_offer_count: int = field(default=1, kw_only=True)
    provider: str = field(default="channel3", init=False)

    @property
    def availability(self) -> str:
        if self.source_availability == "InStock":
            return "available"
        if self.source_availability == "OutOfStock":
            return "unavailable"
        return "unknown"

    def public(self) -> dict:
        """Allowlisted tool output, with no raw URL, fingerprint or provider blob."""
        return dict(product_id=self.product_id, merchant_domain=self.merchant_domain,
                    title=self.title, unit_price=format(self.unit_price, "f"), currency=self.currency,
                    availability=self.availability, source_availability=self.source_availability,
                    source_retrieved_at=self.retrieved_at.isoformat(),
                    purchase_candidate=self.availability == "available")


@dataclass(frozen=True, slots=True)
class OfferRejection:
    merchant_domain: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class Product:
    product_id: str
    offers: tuple[Offer, ...]
    rejected_offers: tuple[OfferRejection, ...]


@dataclass(frozen=True, slots=True)
class SearchResult:
    products: tuple[Product, ...]
    rejected_product_count: int


def _product(raw: object, observed_at: datetime, secret: str) -> Product:
    if not isinstance(raw, dict) or not valid_product_id(raw.get("id")) or not _bounded_text(raw.get("title"), 500):
        raise ValueError("invalid_product")
    product_id, title, offers = raw["id"], raw["title"], raw.get("offers")
    if secret in product_id or not isinstance(offers, list) or len(offers) > MAX_OFFERS:
        raise ValueError("invalid_product")
    title = title.replace(secret, "[REDACTED]")
    domains = []
    for raw_offer in offers:
        try:
            domain = canonical_domain(raw_offer.get("domain")) if isinstance(raw_offer, dict) else None
            if domain is None or secret in domain:
                raise ValueError
            domains.append(domain)
        except ValueError:
            # Cannot rule out a hidden duplicate merchant when an offer has no
            # usable domain. Reject this product, not just that array element.
            raise ValueError("unassignable_offer") from None
    counts = Counter(domains)  # Scan ALL offers, including malformed/unavailable duplicates.
    accepted, rejected = [], []
    for item, domain in zip(offers, domains):
        if counts[domain] != 1:
            rejected.append(OfferRejection(domain, "ambiguous"))
            continue
        try:
            price = item.get("price")
            if not isinstance(price, dict):
                raise ValueError("invalid_price")
            if price.get("currency") != "USD":
                raise ValueError("unsupported_currency")
            amounts = [exact_usd_price(price[name]) for name in ("price", "amount") if name in price]
            if not amounts or any(amount != amounts[0] for amount in amounts):
                raise ValueError("inconsistent_price" if amounts else "invalid_price")
            condition = item.get("condition")
            if condition is not None and (not _bounded_text(condition, 200) or secret in condition):
                raise ValueError("invalid_condition")
            url = item.get("url")
            if isinstance(url, str) and secret in url:
                raise ValueError("invalid_url")
            fingerprint = _url_fingerprint(url)
            availability = item.get("availability")
            if not _bounded_text(availability, 100) or secret in availability:
                availability = None
            accepted.append(Offer(product_id, domain, title, fingerprint, condition, amounts[0], availability, observed_at))
        except (ValueError, InvalidOperation) as error:
            # All ValueErrors above are fixed internal reasons, never source text.
            rejected.append(OfferRejection(domain, str(error)))
    return Product(product_id, tuple(accepted), tuple(rejected))


def compare_offer(selected: Offer, current: Product) -> str:
    """Conservative comparison reusable before proposals and again at dispatch."""
    if selected.product_id != current.product_id or selected.matching_version != MATCHING_VERSION:
        return "identity_changed"
    rejected = [r for r in current.rejected_offers if r.merchant_domain == selected.merchant_domain]
    if rejected:
        return "ambiguous" if any(r.reason == "ambiguous" for r in rejected) else "unsupported"
    matches = [o for o in current.offers if o.merchant_domain == selected.merchant_domain]
    if not matches:
        return "missing"
    if len(matches) != 1:
        return "ambiguous"
    latest = matches[0]
    if (latest.url_fingerprint != selected.url_fingerprint or latest.source_condition != selected.source_condition
            or latest.matching_offer_count != 1 or latest.currency != selected.currency):
        return "identity_changed"
    if latest.unit_price != selected.unit_price:
        return "price_changed"  # A decrease invalidates too; never revise an approved snapshot.
    if latest.availability != "available":
        return "unavailable"
    return "passed"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("invalid_json_number")


class Channel3Client:
    """One fixed-origin request per operation, no retries, proxies or redirects.

    Async total deadline covers streaming too; read timeouts alone permit a
    slow trickle indefinitely. Transport injection is for offline tests only.
    """

    def __init__(self, settings: Channel3Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self._key = settings.api_key
        self._transport = transport

    async def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[dict, datetime]:
        try:
            async with asyncio.timeout(45):
                async with httpx.AsyncClient(headers={"x-api-key": self._key.get_secret_value(), "Accept": "application/json",
                                                     "Accept-Encoding": "identity"},
                                             timeout=httpx.Timeout(10, connect=5), follow_redirects=False,
                                             trust_env=False, transport=self._transport,
                                             limits=httpx.Limits(max_connections=2)) as client:
                    async with client.stream(method, API_BASE + path, **({"json": payload} if payload is not None else {})) as response:
                        if response.status_code == 429:
                            raise Channel3Error("rate_limited")
                        if response.status_code == 404:
                            raise Channel3Error("not_found")
                        if response.status_code != 200:
                            raise Channel3Error("provider_failure")
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise Channel3Error("unsupported_encoding")
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise Channel3Error("response_too_large")
                            body.extend(chunk)
            data = json.loads(body, parse_float=Decimal, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            if not isinstance(data, dict):
                raise ValueError
            return data, datetime.now(timezone.utc)
        except (TimeoutError, httpx.TimeoutException):
            raise Channel3Error("timeout") from None
        except httpx.RequestError:
            raise Channel3Error("network_error") from None
        except (ValueError, UnicodeError, RecursionError, InvalidOperation):
            raise Channel3Error("invalid_response") from None

    async def search(self, query: str, *, limit: int = 3) -> SearchResult:
        if not _bounded_text(query, 200) or not query.strip() or type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("invalid_search")
        data, observed_at = await self._request("POST", "/v1/search", {"query": query.strip(), "limit": limit})
        products = data.get("products")
        if not isinstance(products, list) or len(products) > limit:
            raise Channel3Error("invalid_response")
        ids = Counter(p.get("id") for p in products if isinstance(p, dict) and valid_product_id(p.get("id")))
        accepted = []
        for product in products:
            try:
                parsed = _product(product, observed_at, self._key.get_secret_value())
                if ids[parsed.product_id] != 1:
                    raise ValueError
                accepted.append(parsed)
            except ValueError:
                continue
        return SearchResult(tuple(accepted), len(products) - len(accepted))

    async def retrieve(self, product_id: str) -> Product:
        if not valid_product_id(product_id):
            raise ValueError("invalid_product_id")
        data, observed_at = await self._request("GET", "/v1/products/" + product_id)
        try:
            product = _product(data, observed_at, self._key.get_secret_value())
            if product.product_id != product_id:
                raise ValueError
            return product
        except ValueError:
            raise Channel3Error("invalid_response") from None
