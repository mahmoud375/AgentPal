"""Read-only Channel3 search/retrieve spike; no checkout or application integration.

Uses the documented x-api-key header with POST /v1/search and
GET /v1/products/{product_id}. The CLI's lookup command retrieves by ID;
it does not call the separate, credit-consuming POST /v1/lookup URL API.
Docs: https://docs.trychannel3.com/api-reference/v1/search
      https://docs.trychannel3.com/api-reference/v1/product-detail

Run with the existing environment:
    .venv/bin/python -B scripts/spike_channel3.py search "wireless keyboard" --limit 3
    .venv/bin/python -B scripts/spike_channel3.py lookup PRODUCT_ID

Each invocation makes at most one API request, without retries or redirects.
Output is an allowlisted summary, not a raw response or merchant verification.
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from dotenv import dotenv_values


API_BASE = "https://api.trychannel3.com"
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
MAX_RESPONSE_BYTES = 2_000_000
MAX_OFFERS_SHOWN = 3


def scalar(value: object) -> object:
    """Only expose bounded scalar fields; never print arbitrary nested objects."""
    if isinstance(value, str):
        return value[:200]
    if value is None or isinstance(value, (bool, int, Decimal)):
        return value
    return "[unexpected field type]"


def summarize_product(product: dict) -> dict:
    offers = product.get("offers")
    summary = {
        "product_fields_present": sorted(product),
        "missing_product_fields": [
            name for name in ("id", "title", "offers") if name not in product
        ],
        "offers_returned": len(offers) if isinstance(offers, list) else None,
        "offers_shown": [],
    }
    for name in ("id", "title"):
        if name in product:
            summary[name] = scalar(product[name])
    brands = product.get("brands")
    if isinstance(brands, list):
        summary["brands"] = [
            {name: scalar(brand[name]) for name in ("id", "name") if name in brand}
            for brand in brands[:3]
            if isinstance(brand, dict)
        ]
    for offer in (offers if isinstance(offers, list) else [])[:MAX_OFFERS_SHOWN]:
        if not isinstance(offer, dict):
            continue
        item = {
            "offer_fields_present": sorted(offer),
            "missing_offer_fields": [
                name for name in ("domain", "price", "availability", "url")
                if name not in offer
            ],
        }
        for name in ("domain", "availability", "condition"):
            if name in offer:
                item[name] = scalar(offer[name])
        price = offer.get("price")
        if isinstance(price, dict):
            # The reference uses price.price; one guide uses price.amount.
            # Preserve actual names, without manufacturing either field.
            item["price_fields_present"] = sorted(price)
            item["price"] = {
                name: scalar(price[name])
                for name in ("price", "amount", "currency", "compare_at_price")
                if name in price
            }
        elif "price" in offer:
            item["price"] = scalar(price)
        url = offer.get("url")
        item["buy_url_present"] = isinstance(url, str) and bool(url)
        if isinstance(url, str):
            try:
                item["buy_url_host"] = urlsplit(url).hostname
            except ValueError:
                item["buy_url_host"] = "[invalid URL]"
        # Do not emit attributed URLs, query parameters, or arbitrary metadata.
        summary["offers_shown"].append(item)
    return summary


def emit(summary: dict, api_key: str = "") -> None:
    output = json.dumps(summary, indent=2, ensure_ascii=True, default=str)
    if api_key:
        output = output.replace(api_key, "[REDACTED]")
        output = output.replace(json.dumps(api_key)[1:-1], "[REDACTED]")
    print(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="One small product search")
    search.add_argument("query", nargs="?", default="wireless keyboard")
    search.add_argument("--limit", type=int, choices=range(1, 6), default=3)
    lookup = commands.add_parser("lookup", help="Retrieve a search result by product.id")
    lookup.add_argument("product_id")
    args = parser.parse_args()
    if args.command == "search" and not 1 <= len(args.query.strip()) <= 200:
        parser.error("query must contain between 1 and 200 characters")
    if args.command == "lookup" and not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", args.product_id):
        parser.error("product_id must be an opaque alphanumeric, hyphen, or underscore ID")
    return args


def main() -> int:
    args = parse_args()
    # Parsing stays inside this process. No .env content, key, or exception
    # details are printed, and unrelated credentials are never transmitted.
    logging.getLogger("dotenv.main").disabled = True
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True
    try:
        api_key = dotenv_values(ENV_FILE, interpolate=False).get("CHANNEL3_API_KEY")
    except (OSError, UnicodeError):
        emit({"error": "Unable to load the local .env configuration"})
        return 1
    if not isinstance(api_key, str) or not api_key.strip():
        emit({"error": "CHANNEL3_API_KEY is missing or empty in local .env"})
        return 1
    api_key = api_key.strip()
    if not api_key.isascii() or any(char.isspace() or ord(char) < 32 for char in api_key):
        emit({"error": "CHANNEL3_API_KEY has an invalid header format"})
        return 1

    if args.command == "search":
        method, endpoint = "POST", "/v1/search"
        request_options = {"json": {"query": args.query, "limit": args.limit}}
    else:
        method, endpoint = "GET", f"/v1/products/{quote(args.product_id, safe='')}"
        request_options = {}
    summary = {
        "command": args.command,
        "endpoint": f"{method} {endpoint}",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "observation_source": "local request time; not a provider freshness timestamp",
    }
    try:
        with httpx.Client(
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(45.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(method, API_BASE + endpoint, **request_options) as response:
                summary["http_status"] = response.status_code
                if response.status_code != 200:
                    summary["error"] = "API did not return HTTP 200; response body withheld"
                    emit(summary, api_key)
                    return 1
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        summary["error"] = "Response exceeded the spike's size limit"
                        emit(summary, api_key)
                        return 1
        data = json.loads(body, parse_float=Decimal)
        if not isinstance(data, dict):
            raise ValueError("Unexpected JSON shape")
        summary["response_fields_present"] = sorted(data)
        if args.command == "search":
            products = data.get("products")
            if not isinstance(products, list) or not all(isinstance(p, dict) for p in products):
                raise ValueError("Unexpected products shape")
            summary["products_returned"] = len(products)
            summary["products"] = [summarize_product(p) for p in products[:args.limit]]
            summary["more_pages_present"] = bool(data.get("next_page_token"))
            usable = bool(products) and all(
                isinstance(p.get("id"), str) and isinstance(p.get("title"), str)
                for p in products[:args.limit]
            )
        else:
            summary["product"] = summarize_product(data)
            summary["requested_id_matches"] = data.get("id") == args.product_id
            usable = summary["requested_id_matches"] and isinstance(data.get("title"), str)
        summary["product_identity_check_passed"] = usable
        emit(summary, api_key)
        return 0 if usable else 1
    except httpx.RequestError:
        summary["error"] = "Network/timeout failure; details withheld; no retry made"
    except (ValueError, UnicodeError):
        summary["error"] = "Invalid JSON or unexpected response shape; body withheld"
    emit(summary, api_key)
    return 1


if __name__ == "__main__":
    sys.exit(main())
