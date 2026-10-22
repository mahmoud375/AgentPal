"""Explicit offline rehearsal fixtures. Never loaded by the live application.

Synthetic listings travel through the real Channel3 parser and independent
retrieval. PydanticAI's TestModel is scripted, not a live AI reasoning claim.
"""

from decimal import Decimal
import json
import re

import httpx
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from agentpal.channel3 import Channel3Client, Channel3Settings
from agentpal.discovery import DiscoveryService, OfferRegistry
from agentpal.shopping import OfferChoice, SearchPlan, ShoppingRuntime


PRODUCTS = (
    ("demo_keyboard", "Synthetic wireless keyboard", "65.00"),
    ("demo_mouse", "Synthetic ergonomic mouse", "24.00"),
    ("demo_monitor", "Synthetic 27-inch monitor", "349.00"),
)


def product(identifier, title, price):
    return {"id": identifier, "title": title, "offers": [{"domain": "demo-shop.test",
        "price": {"price": price, "currency": "USD"}, "availability": "InStock", "condition": "New",
        "url": "https://demo-shop.test/" + identifier}]}


def synthetic_catalog(request: httpx.Request):
    if request.method == "POST":
        query = json.loads(request.content)["query"].lower()
        category = next((name for name in ("keyboard", "mouse", "monitor") if name in query), "keyboard")
        return httpx.Response(200, json={"products": [product(*p) for p in PRODUCTS if category in p[0]]})
    match = next((p for p in PRODUCTS if request.url.path.endswith("/" + p[0])), None)
    return httpx.Response(200, json=product(*match)) if match else httpx.Response(404)


class RehearsalPlanner:
    mode = "offline_rehearsal"

    async def plan(self, request):
        match = re.search(r"\b([1-9][0-9]?)\b", request)
        quantity = int(match[1]) if match else 1
        result = await Agent(TestModel(custom_output_args={"query": request[:200], "quantity": quantity}),
                             output_type=SearchPlan, retries=0).run(request)
        return result.output

    async def choose(self, request, candidates):
        selected = min(candidates, key=lambda c: Decimal(c["unit_price"]))
        result = await Agent(TestModel(custom_output_args={"offer_ref": selected["offer_ref"]}),
                             output_type=OfferChoice, retries=0).run(request)
        return result.output


class RehearsalVault:
    # The disposable database has no Vault enrollments or payment methods.
    def status(self):
        return {"connected": False, "onboarding": None, "agreement_state": "unverified"}


def rehearsal_runtime():
    client = Channel3Client(Channel3Settings(CHANNEL3_API_KEY="synthetic-rehearsal-not-a-credential"),
                            transport=httpx.MockTransport(synthetic_catalog))
    return ShoppingRuntime(DiscoveryService(client, OfferRegistry()), RehearsalPlanner())
