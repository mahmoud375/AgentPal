"""Trusted backend discovery capability; no HTTP, owner approval or DB writes.

RunContext must come from the authenticated backend/runtime, never tool arguments.
An opaque reference identifies evidence only; it cannot authorize a purchase.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import re
import secrets
from threading import Lock
import time
from uuid import UUID

from agentpal.channel3 import Channel3Client, Channel3Error, Offer, compare_offer


@dataclass(frozen=True, slots=True)
class RunContext:
    owner_key: str
    agent_id: UUID
    run_id: UUID

    def __post_init__(self):
        if (not isinstance(self.owner_key, str) or not 1 <= len(self.owner_key) <= 128
                or not isinstance(self.agent_id, UUID) or not isinstance(self.run_id, UUID)):
            raise ValueError("invalid_run_context")


class InvalidOfferReference(Exception):
    def __init__(self):
        super().__init__("invalid_offer_reference")


def offer_reference_hash(reference: str) -> bytes:
    """Stable proposal origin key, available even after the registry is lost.

    This hash proves neither context ownership nor spending authority. A durable
    replay must separately verify its persisted owner/agent/run binding.
    """
    if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", reference):
        raise InvalidOfferReference()
    return hashlib.sha256(b"agentpal-offer-reference-v1\0" + reference.encode("ascii")).digest()


@dataclass(frozen=True, slots=True)
class SelectionEvidence:
    offer: Offer
    origin_ref_hash: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Entry:
    context: RunContext = field(repr=False)
    evidence: SelectionEvidence
    expires: float


class OfferRegistry:
    """Single-process, lock-protected FIFO bounds and absolute monotonic expiry.

    Only purpose-separated token hashes are retained. Reading never extends TTL.
    Eviction/restart/expiry require fresh discovery, with identical safe errors.
    """

    def __init__(self, *, capacity: int = 500, per_run: int = 100, ttl_seconds: int = 600):
        if (type(capacity) is not int or not 1 <= capacity <= 5000
                or type(per_run) is not int or not 1 <= per_run <= capacity
                or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600):
            raise ValueError("invalid_registry_bounds")
        self._capacity, self._per_run, self._ttl = capacity, per_run, ttl_seconds
        self._entries: OrderedDict[bytes, _Entry] = OrderedDict()
        self._lock = Lock()

    @staticmethod
    def _digest(reference: str) -> bytes:
        return offer_reference_hash(reference)

    def _prune(self, now: float):
        for key in [key for key, entry in self._entries.items() if entry.expires <= now]:
            del self._entries[key]

    def issue(self, context: RunContext, offer: Offer) -> str:
        if not isinstance(context, RunContext) or not isinstance(offer, Offer):
            raise ValueError("invalid_selection")
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            reference = secrets.token_urlsafe(32)
            digest = self._digest(reference)
            # Check before eviction: a collision must not overwrite or replace
            # the evidence bound to an existing reference, even at capacity.
            if digest in self._entries:
                raise RuntimeError("offer_reference_collision")
            run_keys = [key for key, entry in self._entries.items() if entry.context == context]
            if len(run_keys) >= self._per_run:
                del self._entries[run_keys[0]]
            if len(self._entries) >= self._capacity:
                self._entries.popitem(last=False)
            self._entries[digest] = _Entry(context, SelectionEvidence(offer, digest), now + self._ttl)
            return reference

    def resolve(self, context: RunContext, reference: str) -> SelectionEvidence:
        if not isinstance(context, RunContext) or not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", reference):
            raise InvalidOfferReference()
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            entry = self._entries.get(self._digest(reference))
            if entry is None or entry.context != context:
                raise InvalidOfferReference()
            return entry.evidence

    def discard_run(self, context: RunContext) -> None:
        """Trusted runtime calls this on run completion/cancellation."""
        with self._lock:
            for key in [key for key, entry in self._entries.items() if entry.context == context]:
                del self._entries[key]

    def __len__(self):
        with self._lock:
            self._prune(time.monotonic())
            return len(self._entries)

    @property
    def search_capacity(self) -> int:
        return min(50, self._per_run)


@dataclass(frozen=True, slots=True)
class Candidate:
    offer_ref: str = field(repr=False)
    offer: Offer

    def public(self) -> dict:
        return self.offer.public() | {"offer_ref": self.offer_ref}


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    candidates: tuple[Candidate, ...]
    rejected_product_count: int
    rejected_offer_count: int


@dataclass(frozen=True, slots=True)
class RefreshResult:
    status: str
    latest: Offer | None = None
    source_error: str | None = None


class DiscoveryService:
    def __init__(self, client: Channel3Client, registry: OfferRegistry):
        self._client, self._registry = client, registry

    async def search(self, context: RunContext, query: str, *, limit: int = 3) -> DiscoveryResult:
        if not isinstance(context, RunContext):
            raise ValueError("invalid_run_context")
        result = await self._client.search(query, limit=limit)
        # Bound tool output independently of the complete provider offer scan.
        # At most 50 selectable offers are displayed/issued in one search.
        offers = [offer for product in result.products for offer in product.offers][:self._registry.search_capacity]
        candidates = tuple(Candidate(self._registry.issue(context, offer), offer) for offer in offers)
        return DiscoveryResult(candidates, result.rejected_product_count,
                               sum(len(p.rejected_offers) for p in result.products))

    def resolve(self, context: RunContext, reference: str) -> SelectionEvidence:
        return self._registry.resolve(context, reference)

    async def refresh(self, context: RunContext, reference: str) -> RefreshResult:
        selected = self.resolve(context, reference).offer
        try:
            current = await self._client.retrieve(selected.product_id)
        except Channel3Error as error:
            self.resolve(context, reference)
            return RefreshResult("missing" if error.code == "not_found" else "source_error", source_error=error.code)
        # Expiry, eviction or run cancellation during I/O must not return a
        # successful refresh for a reference that no longer exists.
        self.resolve(context, reference)
        status = compare_offer(selected, current)
        latest = next((offer for offer in current.offers if offer.merchant_domain == selected.merchant_domain), None)
        # Never mutate the original evidence or resurrect old data on a failed
        # refresh. A passed result is listing evidence, still not authorization.
        return RefreshResult(status, latest)
