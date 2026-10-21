#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "python-dotenv>=1.0"]
# ///
"""AgentPal day-0 PayPal Sandbox spike.

Goal: find out which payment mode is actually available on YOUR sandbox
account before building anything else.

  Mode 2 (redirect):  order-redirect -> approve in browser -> capture
  Mode 1 (vaulted):   vault-setup -> approve in browser -> vault-confirm
                      -> vault-charge  (zero-click if status == COMPLETED)

Setup
  1. developer.paypal.com -> Sandbox accounts: one Business, one Personal.
  2. Enable "Vault" on the sandbox Business account (account settings).
  3. Create a REST app under that Business account (Sandbox mode).
  4. Put credentials in .env (never commit it):
       PAYPAL_CLIENT_ID=...
       PAYPAL_CLIENT_SECRET=...
       # optional
       PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com
       PAYPAL_RETURN_URL=http://localhost:8000/paypal/return
       PAYPAL_CANCEL_URL=http://localhost:8000/paypal/cancel

Run (uv resolves the inline dependencies):
  uv run scripts/spike_paypal.py token
  uv run scripts/spike_paypal.py order-redirect --amount 10.00
  uv run scripts/spike_paypal.py capture <ORDER_ID>
  uv run scripts/spike_paypal.py vault-setup
  uv run scripts/spike_paypal.py vault-confirm <SETUP_TOKEN_ID>
  uv run scripts/spike_paypal.py vault-charge <PAYMENT_TOKEN_ID> --amount 12.00
  uv run scripts/spike_paypal.py idempotency-test <PAYMENT_TOKEN_ID>

NOTE: request bodies follow PayPal's public docs as read at the time of
writing and have not been run against a live sandbox by the author. If a call
returns 4xx, the full response body and PayPal-Debug-Id are printed; compare
against the current API reference and adjust the payload.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com").rstrip("/")
RETURN_URL = os.getenv("PAYPAL_RETURN_URL", "http://localhost:8000/paypal/return")
CANCEL_URL = os.getenv("PAYPAL_CANCEL_URL", "http://localhost:8000/paypal/cancel")

if "sandbox" not in BASE:
    sys.exit("Refusing to run: PAYPAL_BASE_URL must be a sandbox URL.")


def need(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"Missing environment variable: {name}")
    return value


def show(resp: httpx.Response) -> dict:
    """Print a compact, secret-masked view of a response and return its JSON."""
    print(f"\n--> {resp.request.method} {resp.request.url}")
    print(f"<-- {resp.status_code}  PayPal-Debug-Id: {resp.headers.get('paypal-debug-id', '-')}")
    try:
        data = resp.json()
    except ValueError:
        print(resp.text[:2000])
        return {}
    masked = dict(data)
    if "access_token" in masked:
        masked["access_token"] = masked["access_token"][:8] + "...(masked)"
    print(json.dumps(masked, indent=2)[:6000])
    return data


def get_token(client: httpx.Client) -> str:
    resp = client.post(
        f"{BASE}/v1/oauth2/token",
        auth=(need("PAYPAL_CLIENT_ID"), need("PAYPAL_CLIENT_SECRET")),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
    )
    data = show(resp)
    if resp.status_code != 200:
        sys.exit("OAuth failed: check client id/secret and that the app is in Sandbox mode.")
    print(f"\nToken OK. expires_in={data.get('expires_in')}s (cache it, refresh before expiry).")
    return data["access_token"]


def call(
    client: httpx.Client,
    token: str,
    method: str,
    path: str,
    body: dict | None = None,
    request_id: str | None = None,
) -> tuple[httpx.Response, dict]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    resp = client.request(method, f"{BASE}{path}", headers=headers, json=body)
    return resp, show(resp)


def link(data: dict, *rels: str) -> str | None:
    for item in data.get("links", []):
        if item.get("rel") in rels:
            return item.get("href")
    return None


def captures(data: dict) -> list[dict]:
    out: list[dict] = []
    for unit in data.get("purchase_units", []):
        out.extend(unit.get("payments", {}).get("captures", []))
    return out


def order_body(amount: str, custom_id: str, payment_source: dict) -> dict:
    return {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": "spike-1",
                "custom_id": custom_id,  # short intent id/hash only, never LLM reasoning
                "amount": {"currency_code": "USD", "value": amount},
            }
        ],
        "payment_source": payment_source,
    }


# ---------------------------------------------------------------- commands


def cmd_token(client: httpx.Client, args: argparse.Namespace) -> None:
    get_token(client)


def cmd_order_redirect(client: httpx.Client, args: argparse.Namespace) -> None:
    token = get_token(client)
    request_id = args.request_id or str(uuid.uuid4())
    body = order_body(
        args.amount,
        "agentpal-spike-redirect",
        {
            "paypal": {
                "experience_context": {
                    "return_url": RETURN_URL,
                    "cancel_url": CANCEL_URL,
                    "user_action": "PAY_NOW",
                    "shipping_preference": "NO_SHIPPING",
                }
            }
        },
    )
    resp, data = call(client, token, "POST", "/v2/checkout/orders", body, request_id)
    if resp.is_error:
        return
    approve = link(data, "payer-action", "approve")
    print(f"\nORDER_ID   : {data.get('id')}  (status {data.get('status')})")
    print(f"REQUEST_ID : {request_id}")
    print(f"APPROVE URL: {approve}")
    print("Open the URL, log in with your sandbox PERSONAL account, approve,")
    print("then run: capture <ORDER_ID>")


def cmd_capture(client: httpx.Client, args: argparse.Namespace) -> None:
    token = get_token(client)
    resp, data = call(
        client,
        token,
        "POST",
        f"/v2/checkout/orders/{args.order_id}/capture",
        {},
        args.request_id or str(uuid.uuid4()),
    )
    if resp.is_error:
        return
    for cap in captures(data):
        print(f"\nCAPTURE_ID: {cap.get('id')}  status={cap.get('status')}")


def cmd_get_order(client: httpx.Client, args: argparse.Namespace) -> None:
    token = get_token(client)
    call(client, token, "GET", f"/v2/checkout/orders/{args.order_id}")


def cmd_vault_setup(client: httpx.Client, args: argparse.Namespace) -> None:
    token = get_token(client)
    body = {
        "payment_source": {
            "paypal": {
                "usage_type": "MERCHANT",
                "customer_type": "CONSUMER",
                "experience_context": {"return_url": RETURN_URL, "cancel_url": CANCEL_URL},
            }
        }
    }
    resp, data = call(
        client, token, "POST", "/v3/vault/setup-tokens", body, args.request_id or str(uuid.uuid4())
    )
    if resp.is_error:
        print("\nIf this is a permissions/vault error: confirm Vault is enabled on the")
        print("sandbox Business account behind this REST app. That decides Mode 1 vs Mode 2.")
        return
    print(f"\nSETUP_TOKEN_ID: {data.get('id')}")
    print(f"APPROVE URL   : {link(data, 'approve')}")
    print("Approve once as the sandbox PERSONAL account, then run: vault-confirm <SETUP_TOKEN_ID>")


def cmd_vault_confirm(client: httpx.Client, args: argparse.Namespace) -> None:
    token = get_token(client)
    body = {"payment_source": {"token": {"id": args.setup_token_id, "type": "SETUP_TOKEN"}}}
    resp, data = call(
        client, token, "POST", "/v3/vault/payment-tokens", body, args.request_id or str(uuid.uuid4())
    )
    if resp.is_error:
        return
    print(f"\nPAYMENT_TOKEN_ID (vault_id): {data.get('id')}")
    print(f"CUSTOMER_ID                 : {data.get('customer', {}).get('id')}")
    print("Store both server-side. Next: vault-charge <PAYMENT_TOKEN_ID>")


def vault_charge(
    client: httpx.Client,
    token: str,
    vault_id: str,
    amount: str,
    request_id: str,
    stored_credential: bool,
) -> dict:
    paypal: dict = {"vault_id": vault_id}
    if stored_credential:
        paypal["stored_credential"] = {
            "payment_initiator": "MERCHANT",
            "usage": "SUBSEQUENT",
            "usage_pattern": "UNSCHEDULED_POSTPAID",
        }
    body = order_body(amount, "agentpal-spike-vault", {"paypal": paypal})
    _, data = call(client, token, "POST", "/v2/checkout/orders", body, request_id)
    return data


def cmd_vault_charge(client: httpx.Client, args: argparse.Namespace) -> None:
    token = get_token(client)
    request_id = args.request_id or str(uuid.uuid4())
    data = vault_charge(
        client, token, args.vault_id, args.amount, request_id, not args.no_stored_credential
    )
    status = data.get("status")
    print(f"\nORDER_ID: {data.get('id')}  status={status}  request_id={request_id}")
    if status == "COMPLETED":
        for cap in captures(data):
            print(f"CAPTURE_ID: {cap.get('id')}  status={cap.get('status')}")
        print("\nRESULT: Mode 1 works. Zero-click vaulted payment completed with no redirect.")
    elif status == "APPROVED":
        print("\nOrder approved but not captured: call `capture <ORDER_ID>` (still no redirect).")
    elif status in {"PAYER_ACTION_REQUIRED", "CREATED"}:
        print(f"\nPayer action needed: {link(data, 'payer-action', 'approve')}")
        print("RESULT: vaulting alone did not give zero-click here. Plan on Mode 2.")
    else:
        print("\nUnexpected result: read the body above; try --no-stored-credential.")


def cmd_idempotency_test(client: httpx.Client, args: argparse.Namespace) -> None:
    token = get_token(client)
    request_id = str(uuid.uuid4())
    first = vault_charge(client, token, args.vault_id, args.amount, request_id, True)
    second = vault_charge(client, token, args.vault_id, args.amount, request_id, True)
    same = bool(first.get("id")) and first.get("id") == second.get("id")
    print(f"\nfirst={first.get('id')}  second={second.get('id')}  request_id={request_id}")
    print("RESULT:", "PASS (same order returned)" if same else "FAIL (inspect responses above)")
    print("Still keep your own DB unique constraint: PayPal's idempotency window is limited.")


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentPal PayPal Sandbox spike")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("token").set_defaults(fn=cmd_token)

    p = sub.add_parser("order-redirect")
    p.add_argument("--amount", default="10.00")
    p.add_argument("--request-id")
    p.set_defaults(fn=cmd_order_redirect)

    p = sub.add_parser("capture")
    p.add_argument("order_id")
    p.add_argument("--request-id")
    p.set_defaults(fn=cmd_capture)

    p = sub.add_parser("get-order")
    p.add_argument("order_id")
    p.set_defaults(fn=cmd_get_order)

    p = sub.add_parser("vault-setup")
    p.add_argument("--request-id")
    p.set_defaults(fn=cmd_vault_setup)

    p = sub.add_parser("vault-confirm")
    p.add_argument("setup_token_id")
    p.add_argument("--request-id")
    p.set_defaults(fn=cmd_vault_confirm)

    p = sub.add_parser("vault-charge")
    p.add_argument("vault_id")
    p.add_argument("--amount", default="12.00")
    p.add_argument("--request-id")
    p.add_argument("--no-stored-credential", action="store_true")
    p.set_defaults(fn=cmd_vault_charge)

    p = sub.add_parser("idempotency-test")
    p.add_argument("vault_id")
    p.add_argument("--amount", default="5.00")
    p.set_defaults(fn=cmd_idempotency_test)

    args = parser.parse_args()
    with httpx.Client(timeout=30) as client:
        args.fn(client, args)


if __name__ == "__main__":
    main()