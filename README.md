# AgentPal

AgentPal is a policy-controlled AI procurement/payment simulation for one owner.
Channel3 discovers retailer listings; PayPal Sandbox payments go to AgentPal's
single demo Business account. The application does not order or fulfill goods
from those retailers. The FastAPI health endpoint, PostgreSQL persistence and
single-owner authentication, owner policy/budget APIs and a reusable Channel3
discovery/transient-selection foundation and durable initial purchase proposals
and secure owner purchase approval are implemented. Reservation and payment
execution services are pending.

The [authoritative context](docs/AgentPal_Full_Context_Handoff.md) records approved
decisions, completed experiments and remaining integration work.

## Development and migrations

Python 3.12+ and PostgreSQL are required. Integration tests were exercised on
PostgreSQL 18.6. Run commands from this repository checkout; migration assets
are maintained alongside the source rather than generated from ORM metadata.

```sh
rtk proxy uv sync --locked
```

Provision a **fresh, empty** database and a migration role through your local
PostgreSQL administration. For an existing local peer-authenticated role/server:

```sh
rtk proxy createdb agentpal_dev
rtk proxy env DATABASE_URL='postgresql+psycopg:///agentpal_dev' uv run alembic upgrade head
rtk proxy env DATABASE_URL='postgresql+psycopg:///agentpal_dev' uv run alembic current
```

For password authentication, set `DATABASE_URL` privately using the placeholder
format in `.env.example`. Neither configuration nor Alembic automatically opens
`.env` or `.env.local`. Do not paste connection strings into reports. Migrations
run only when explicitly invoked; importing FastAPI or the database package
does not connect or migrate. No owner, password, policy amount or payment data
is seeded. `alembic downgrade` destroys persisted state and is for intentionally
disposable databases only.

```sh
rtk proxy uv run uvicorn agentpal.main:app --reload
rtk proxy uv run pytest -q
```

Tests require local `initdb` and `pg_ctl` and a non-root user. They start a private
temporary PostgreSQL cluster with no TCP listener, create/drop only their own
randomly named test databases, and stop/remove the cluster afterward. They do
not use `DATABASE_URL`, real credentials, password files or existing databases.
Their explicit empty password file is created inside the disposable directory.
Missing PostgreSQL binaries cause integration tests to skip, not to simulate
PostgreSQL with SQLite. Test money, hashes, ciphertext and provider identifiers
are fictional fixtures; no PayPal or Channel3 calls occur.

## Channel3 discovery and transient offer selection

`agentpal.channel3` and `agentpal.discovery` provide reusable asynchronous backend
functions. They add no HTTP endpoints, so there is no unauthenticated discovery
surface or new owner-session bypass. The future trusted agent runtime must bind
these functions to its authenticated owner and verified agent/run context. No
PydanticAI agent or payment is implemented. The proposal service below consumes
this foundation for independent retrieval and deterministic initial evaluation.

Privately export `CHANNEL3_API_KEY` before constructing `Channel3Settings()`.
The variable already appears as an empty placeholder in `.env.example`; no dotenv
file is opened. Imports and `/health` need no Channel3 configuration. Settings
hide the key in representations/validation messages. Do not log settings dumps,
request headers, source bodies, registry contents or opaque references.

| Operation | Fixed Channel3 contract |
|---|---|
| `Channel3Client.search(query, limit=3)` | `POST https://api.trychannel3.com/v1/search`, JSON `{"query": "…", "limit": 3}`; root `products` array |
| `Channel3Client.retrieve(product_id)` | `GET https://api.trychannel3.com/v1/products/{product_id}`; root product object with exactly the requested `id` |

Requests use `x-api-key` and JSON Accept headers, no environment proxies,
redirects or automatic retries. No retailer or source-supplied URL is fetched;
the separate credit-consuming URL lookup API is not used. Queries are 1–200
characters, limits 1–5, and product IDs are 1–200 ASCII alphanumeric/underscore/
hyphen characters. Each operation has a 45-second total network deadline,
5-second connect and 10-second I/O timeouts, a 2 MB response bound and at most
1,000 offers per product. Oversized results fail closed. Compressed responses
are rejected; the adapter requests `Accept-Encoding: identity` to avoid
unbounded decompression. Duplicate JSON keys and nonfinite JSON numbers fail.
There is no pagination implementation. Every returned offer is scanned before
display is capped at 50 candidates (or the registry's smaller per-run capacity).

The spike establishes the field-mapping basis, rather than a checked-in raw
provider response. Supported products require `id`, bounded `title` and an
`offers` array. Every offer needs a usable `domain`, exact USD `price`, and HTTPS
`url`; optional `condition` is bounded text or null. ASCII domains are lowercased
and strictly validated, without merging `www` or subdomains; URLs, ports, IP
literals, trailing dots and whitespace are not merchant domains. The entire
exact URL, including attribution/query, becomes a SHA-256 fingerprint and is
discarded in clear. Affiliate hosts may differ from the merchant domain; neither
the URL nor domain proves legal seller identity. URL attribution changes therefore
block comparison conservatively, even if the underlying retailer item is unchanged.

Prices use `price.price`, with an isolated compatibility mapping for `price.amount`
noted in the spike's reference/guide discrepancy. If both exist, both must be valid
and equal. `price.currency` must be exactly `USD`. Strings use ordinary nonnegative
decimal notation; JSON numeric values are decoded directly to Decimal. Floats,
fractional cents, nonfinite/negative amounts, malformed or inconsistent aliases
are rejected without rounding. Values have a computational bound of 30 integer
digits; this is not a configured financial limit. `compare_at_price` never sets
the purchase price. No tax, shipping, FX or checkout calculation is performed.

Only exact `InStock` reports `availability="available"`; `OutOfStock` reports
`"unavailable"`; all other/missing/malformed values report `"unknown"`. Valid
unavailable/unknown listings can have references but are not purchase candidates.
`purchase_candidate` describes source availability on a structurally valid listing,
not policy eligibility or spending authorization. Structurally valid prohibited
merchants are retained so the future policy engine can issue an actual
`MERCHANT_NOT_ALLOWED` decision. Malformed offers are rejected. Duplicate canonical
domains within one product are ambiguous even when a duplicate is malformed or
out of stock; none of that merchant's offers is selectable. A missing/invalid
domain makes the whole product unsupported because hidden duplication cannot be
ruled out. Duplicate product IDs in one search are also rejected. Other valid
products/merchants can still be returned, with rejection counts; detail retrieval
fails safely if the required product structure is missing.

Trusted backend usage (illustrative code; executing `search` makes a live request):

```python
from uuid import uuid4
from agentpal.channel3 import Channel3Client, Channel3Settings
from agentpal.discovery import DiscoveryService, OfferRegistry, RunContext

# Once per backend process; never expose these objects/credentials to the model.
registry = OfferRegistry()  # global 500 entries, per-run 100, absolute TTL 600s
discovery = DiscoveryService(Channel3Client(Channel3Settings()), registry)

# The authenticated runtime supplies these identities, never model/tool arguments.
context = RunContext(owner_key=verified_owner_login, agent_id=verified_agent_id, run_id=uuid4())
result = await discovery.search(context, query="wireless keyboard", limit=3)
tool_result = [candidate.public() for candidate in result.candidates]
# A future tool receives only query; the proposal service accepts ref + quantity.
# It closes over context and never accepts owner/run/merchant/price from the AI.
evidence = discovery.resolve(context, selected_offer_ref)
refresh = await discovery.refresh(context, selected_offer_ref)
# No purchase is created here. Even refresh.status == "passed" is not authorization.
registry.discard_run(context)  # run completion/cancellation
```

`Candidate.public()` returns only reference, product ID, canonical merchant, an
untrusted title, exact decimal-string price, USD, availability, bounded source
availability, observation time and the structural candidate flag. Descriptions,
raw URLs, fingerprints and provider blobs are omitted. Titles and any source text
must remain data when rendered or placed in model context; they cannot change
instructions, identity, price, policy or payee. Provider errors use fixed codes
(`timeout`, `network_error`, `rate_limited`, `not_found`, `provider_failure`,
`invalid_response`, `response_too_large`, `unsupported_encoding`) with no response
bodies, exception details or Retry-After echo. Known literal key echoes are
redacted from titles or rejected in identity fields. Failure is distinct from an
empty successful search. Source strings remain untrusted, not generally certified
secret-free content.

References contain 32 random bytes, encoded as opaque 43-character URL-safe tokens.
Only purpose-separated SHA-256 hashes are kept, bound to the full owner/agent/run
context and frozen evidence. The lock-protected registry enforces global/per-run
FIFO eviction and monotonic absolute expiry; reads do not extend TTL. Defaults
can be configured in code within caps of 5,000 entries/3,600 seconds. Forged,
cross-context, expired, evicted, canceled and restart-lost references all fail with
`invalid_offer_reference`; repeat discovery. Run **one backend worker**. No discovery
table, background worker, durable reference recovery or cross-process registry
exists. A reference can be resolved repeatedly during its lifetime; durable proposal
origin uniqueness/payment idempotency are separate future responsibilities.

`resolve()` returns frozen `SelectionEvidence`, including the origin-reference
hash and product/domain, URL fingerprint, nullable condition, matching-version
`channel3-product-domain-url-condition-v1`, unique-offer count, exact USD price,
source availability and local UTC retrieval time. This aligns with existing immutable
purchase evidence fields but performs no database writes. `refresh()` always
retrieves that product independently and compares only the original canonical
merchant. It returns `passed`, `identity_changed`, `price_changed`, `unavailable`,
`ambiguous`, `unsupported`, `missing` or `source_error`. It never substitutes a
merchant, changes the original evidence or falls back to stored availability on
failure. Both price increases and decreases block the old selection, as do URL/
condition changes. New discovery is needed after a changed listing. Title changes
alone are not identity evidence. Reference expiry/eviction during I/O is rechecked.

The proposal service below independently refreshes before building its durable
snapshot, evaluates actual policy/exact totals and enforces durable origin
uniqueness. The future dispatch service must refresh/compare the **persisted
snapshot** again; a transient reference need not survive proposal creation. It
must handle policy/TTL/budget/dispatch races and cannot treat a prior passed refresh
as ongoing permission. Discovery itself grants no approval or financial authority.

**Evidence limits:** all new tests use explicitly synthetic responses and mocked
HTTPX transports; no live call was made. Before live integration, verify actual
`price.price` versus `price.amount` representation, optional condition/null behavior,
all offers returned by detail/search, domain case/format, URL/attribution stability,
response sizes/limits, identity encoding, compression support, and provider data-use/
retention/display permissions. These conservative local restrictions are not claims
about a guaranteed provider schema. Search/refresh timestamps are local observation
times, not provider freshness guarantees. Latest successful Channel3 listing data
is the approved Sandbox demonstration pricing basis, not guaranteed retailer
checkout price or physical stock. All eventual payments still go to AgentPal's
single PayPal Sandbox Business account; catalog retailers receive no demo payment
and no actual order is placed or fulfilled.

## Purchase proposals and deterministic initial evaluation

`agentpal.proposals` connects the discovery service to the existing purchase and
audit schema, with no new routes, tables, migrations, dependencies or environment
variables. There is no public purchase API. The future trusted tool runtime closes
over an authenticated run context; the model submits only a reference and quantity.
It cannot provide price, merchant, policy, owner/session/agent IDs, approval or payee.

| Internal contract | Behavior |
|---|---|
| `ProposalService(engine, discovery)` | Reuses the configured SQLAlchemy engine and `DiscoveryService`; no import-time DB or provider calls |
| `start_run(owner, agent_id)` | Synchronous trusted launcher helper; accepts an authenticated `OwnerSession` from `OwnerRead`, rechecks active session/login/CSRF hash and enabled owned agent, returns a backend-generated `RunContext` |
| `await create(context, offer_ref, quantity)` | Resolves durable replay first; otherwise independently refreshes and atomically creates/evaluates a proposal; returns frozen `ProposalResult` |
| `ProposalResult.public()` | Allowlisted proposal ID, policy, product/merchant/title, decimal-string prices, quantity, initial decision/reasons, UTC creation/expiry, replay flag and observed expired/current-policy flags |
| `exact_total(unit_price, quantity)` / `evaluate_policy(...)` | Exact multiplication and deterministic pure evaluation over trusted source/configuration/accounting inputs; not tools or spending capabilities |

Trusted integration example (constructing services is inert; executing search or
new-proposal creation calls Channel3):

```python
from agentpal.proposals import ProposalService

proposals = ProposalService(engine, discovery)
# Trusted launcher only: owner came from existing cookie authentication/OwnerRead.
# Do not pass owner credentials/session objects or this launcher to the model.
context = proposals.start_run(owner, verified_agent_id)

async def propose_purchase(offer_ref: str, quantity: int) -> dict:
    return (await proposals.create(context, offer_ref, quantity)).public()
```

Starting a run does not grant human approval authority. The internal Python
`RunContext` constructor is not an authentication mechanism: never deserialize
owner/agent/run identities from model arguments. `create()` rechecks the context's
owner login against the single DB owner and the agent's enabled ownership. The
registry verifies unused-reference ownership; committed replay verifies an immutable
owner/agent/run context hash. No cookie or owner session is passed to agent tools.
Owner logout does not itself cancel an already launched agent run; the future
runtime must dispose/cancel its references when ending a run. Disabled agents are
blocked from proposal creation/replay. Run restoration after restart must use
verified owner/agent identity and the original run ID from trusted audit storage,
never an AI-supplied claim. Replay cannot silently adopt a reference from another run.

New proposals require a successful independent product-detail refresh with the
same canonical merchant, full URL fingerprint, nullable condition and exact USD
price—including rejection of decreases—and explicitly `InStock`. No merchant
substitution, cached fallback or raw attributed URL is used. Source failures,
missing/ambiguous/unavailable/changed offers and invalid references are validation
errors, not fabricated `REJECTED` policy decisions; no purchase is inserted for
them. An absent active policy raises `POLICY_SETUP_REQUIRED` without inventing
financial defaults. Source network calls happen outside database transactions;
sync database operations run off the async event loop.

Quantity must be a strict positive Python integer within PostgreSQL BIGINT; bools,
floats and strings are rejected. `unit_price × quantity` uses sufficient isolated
Decimal precision without rounding, and PostgreSQL independently generates the
stored total. The final transaction acquires the existing owner financial revision
lock, rechecks agent/context and reference lifetime, reads the latest active policy,
ensures the UTC month and reads `budget_balances`. Available funds use the same
PostgreSQL NUMERIC expression as `/budget/current`, including conservative
capture/protection accounting. No Python financial aggregation or invented hold
is introduced. If the UTC month changes between initialization and observation,
`BUDGET_PERIOD_CHANGED` safely rolls back; retry the same request.

All rejection rules run in stable order, with an immutable rule trace:

| Initial decision | Reasons |
|---|---|
| `REJECTED` | All failing rules among `MERCHANT_NOT_ALLOWED`, `HARD_PURCHASE_LIMIT_EXCEEDED`, `INSUFFICIENT_BUDGET` |
| `AWAITING_HUMAN_APPROVAL` | `HUMAN_APPROVAL_REQUIRED`, when all rejection rules pass and total exceeds the automatic threshold |
| `AUTO_AUTHORIZED` | `AUTO_APPROVAL_THRESHOLD_MET`, when all rejection rules pass and total is at/below the threshold |

The hard limit is inclusive. Human approval cannot override a rejection. Internal
database labels remain `rejected`, `awaiting_approval`, `auto_authorized`; public
contracts use the uppercase decision names above. These are **initial decisions,
not payment or current execution statuses**. Concurrent proposals can both appear
affordable because neither reserves money. Final source/policy/TTL checks and
atomic budget reservation remain mandatory in future dispatch, even for an
initially `AUTO_AUTHORIZED` proposal.

One successful transaction writes an immutable `purchases` snapshot, the existing
trigger's `proposal_recorded` audit, and one `proposal_evaluated` agent audit. The
latter records evaluation version, context hash/original run ID, budget period,
observation time, exact limit/spending/protection/availability, total and boolean
rule trace. Necessary source observations are stored in the purchase; no raw
provider bodies, source descriptions, URL attribution, secrets or model rationale
are stored in audits. PostgreSQL computes the snapshot hash and expiry from the
evaluated immutable policy TTL. A UTC budget period may be initialized on demand.
No approval, offer-revalidation dispatch record, reservation, payment or payment
operation is created. Existing database guards remain unchanged.

Idempotency uses the same purpose-separated reference hash as discovery and the
existing unique purchase origin. A committed proposal is checked **before** the
volatile registry or provider is required, including after registry loss/restart,
expiry or policy change. Matching owner/agent/run and quantity return the original
proposal and TTL; changed quantity raises `REFERENCE_QUANTITY_CONFLICT`.
Cross-context replay is rejected before exposing quantity conflicts. Old externally
inserted purchases without a unique `proposal_evaluated` context binding cannot
be adopted. No new audit is emitted on replay and no financial operation restarts.
Expired/stale-policy flags are observations at read time; they do not rewrite or
re-authorize the original decision. Preserve them when showing replayed results.

The owner lock serializes compliant duplicate submissions and financial writers;
the unique origin remains the DB backstop. Serialization/deadlock/unique-origin
conflicts roll back and check the durable winner in a new scope. If no winner has
committed, return `TRANSACTION_CONFLICT` and retry the same request; there is no
automatic source/payment retry. Deferred commit failures cannot return success.
Storage failures expose only `PROPOSAL_STORAGE_UNAVAILABLE`; source errors use
`OFFER_PRICE_CHANGED`, `OFFER_IDENTITY_CHANGED`, `OFFER_MISSING`, `OFFER_AMBIGUOUS`,
`OFFER_UNAVAILABLE`, `OFFER_UNSUPPORTED` or `OFFER_SOURCE_ERROR`. Malformed/lost
references use the existing `InvalidOfferReference`; errors never echo input,
SQL or provider exception bodies. If a caller times out/cancels while a DB worker
is already committing, it must retry the same reference/quantity to discover the
durable outcome. A fresh reference creates a new proposal identity and proves
nothing about recovery or replay of an uncertain PayPal payment.

Remaining work includes pre-dispatch source refresh,
final atomic financial eligibility/reservation, Vault setup and durable payment/
reconciliation orchestration. No PayPal API is called here. Eventual payments go
to **AgentPal's single PayPal Sandbox Business account**, not the catalog retailer;
there is no retailer checkout, guaranteed stock or goods fulfillment. Before
end-to-end judging, the previously documented live Channel3 price aliases, optional
condition, URL stability, supported shapes/encoding and data-use permissions still
need authorized verification; offline synthetic tests do not establish them.

## Human purchase approval and owner inspection

`agentpal.approvals` exposes the minimum owner-only workflow for immutable purchase
proposals. All routes require the existing active owner cookie. Writes also require
the trusted exact Origin (or same-origin Referer) and session-bound `X-CSRF-Token`.
Agent bearer tokens, claimed roles and owner/agent/session headers provide no
authority. Owner credentials and HTTP clients must never become AI tool inputs.
Authentication proves credential possession; it cannot establish physical human
presence after a browser/credential compromise.

| Endpoint | Contract |
|---|---|
| `GET /purchases/approval-queue?limit=20&offset=0` | Current-policy, unexpired, undispatched proposals initially awaiting approval with no recorded human decision. Limit 1–100, offset 0–10,000, newest creation/UUID first; returns `items`, `limit`, `offset`, `has_more` |
| `GET /purchases/{id}` | Safe immutable proposal details, evaluated policy, original expiry, snapshot binding, human decision and current observed eligibility/payment state; 404 for an unknown ID |
| `PUT /purchases/{id}/approval` | JSON `decision` (`APPROVE` or `DENY`), required 64-character lowercase hexadecimal `snapshot_hash` and integer `snapshot_hash_version: 1`; returns `purchase` and `replayed` |

Use the snapshot binding returned by GET after inspecting the exact product,
merchant, condition, unit price, quantity, total, evaluated policy and expiry.
Unknown fields (including owner/session IDs or replacement prices), duplicate JSON
keys and malformed decisions are rejected. Bodies are limited to 2 KiB and fully
parsed/authenticated before financial locks; session/origin/CSRF are checked again
by `OwnerWrite` before mutation. Responses are `no-store`/`nosniff`. Titles and
source condition remain untrusted display text; a dashboard must escape them as
text and never execute merchant instructions or markup.

The response deliberately separates `initial_decision`, nullable `human_decision`,
`eligibility` and `payment_status`. Monetary values are exact decimal strings;
timestamps are UTC. `human_decision` contains only decision and time, with no session
ID. Raw attributed URLs, URL fingerprints, origin-reference hashes, run IDs,
provider order/Vault references, credentials and arbitrary audit/provider JSON are
omitted. The snapshot hash is an owner-visible concurrency binding, not a secret
or an authorization credential. The evaluated policy is the immutable original
version with its current active flag, never a replacement of that snapshot.

Only proposals initially `AWAITING_HUMAN_APPROVAL` can receive a new decision.
Both approval and denial require the unchanged snapshot, original unexpired TTL,
current policy and no dispatch. Rejected and auto-authorized proposals cannot
receive human decisions. Approval additionally refuses disallowed merchants,
hard-limit violations, known invalidated offers and currently insufficient budget.
Denial can still record refusal when those financial/source conditions block
approval, but cannot change an expired, policy-invalidated or dispatched proposal.
Expired/stale proposals leave the queue and remain inspectable by ID. A new
proposal is required to obtain fresh consent for a changed or expired snapshot.

Current budget uses the same `budget_balances` NUMERIC accounting as the budget API.
If the current month has no period row, its observation uses the configured active
monthly base with no current-month spend/protection; these read/decision endpoints
do not initialize budget periods. A reservation already attached to a purchase is
not charged against available funds twice in the inspection projection. Approval
does not reserve money or guarantee future availability. The future dispatch gate
must independently refresh Channel3, verify current policy/TTL/approval and atomically
reserve/recheck finances before preparing a payment.

`eligibility.can_approve`/`can_deny` describe new human decisions at observation time.
Reason codes identify known blockers, including expiry, stale policy, rejection,
denial, dispatch, merchant/hard-limit/budget violations and invalidated source evidence.
`APPROVED_PENDING_DISPATCH_CHECKS` means consent is recorded; `execution_ready` remains
false. `payment_status` comes only from the separate payment record, or `NOT_STARTED`.
An approved unpaid proposal is never labeled purchased/completed. This slice never
creates a reservation, payment, payment operation or provider request, and never
releases or changes existing financial protection.

One immutable approval/denial row binds the actual authenticated owner session to
the stored snapshot. The existing database trigger emits one owner-attributed
`human_decision_recorded` audit. Matching repeated requests return the first decision
with `replayed: true`, including after policy changes/expiry; that is historical
replay, not fresh authorization. The first deciding session/time remain unchanged.
An opposite decision or stale hash returns 409 instead of overwriting evidence.
There is no approval withdrawal/reversal flow. Replaying a dispatched/uncertain
purchase does not restart it, release protection or permit replacement payment.

Writes reuse owner → session locking and the financial revision update, serializing
with logout, policy changes and other compliant financial writers. Concurrent
identical requests produce one decision/audit; conflicting requests have one winner.
Success is sent only after `OwnerWrite` commits. Serialization/deadlock aborts return
409 `transaction_conflict`; uniqueness conflicts return 409 `decision_conflict`;
insert/commit-time eligibility failures return 409 `approval_ineligible`. Reload
details and repeat the same explicit decision only when appropriate. No automatic
decision retry occurs. Invalid sessions return 401; origin/CSRF failures 403;
invalid payloads 422; stale snapshots 409 `snapshot_conflict`; generic storage failures
503 without SQL or secret details. Offset pagination can shift as the queue changes;
refresh the first page after a decision.

Migration `0003_approval_commit_guard.py` adds a deferred constraint trigger that
reuses `validate_approval()` at commit, alongside the existing insert-time check.
A regression test reproduced that revision `0002` could insert while valid, wait
past expiry and still commit. The additive guard rejects proposal/session expiry
and invalid snapshot/policy/dispatch bindings during that transaction. It modifies
no historical rows; revisions `0001` and `0002` are unchanged. Run an explicit
`alembic upgrade head` only against an authorized database before using this workflow.
No persistent database was migrated during implementation. Downgrading to `0002`
preserves history but removes the extra commit-time protection.

Safe usage after the interactive login example below, before logout:

```python
queue = client.get('/purchases/approval-queue', params={'limit': 20})
queue.raise_for_status()
purchase_id = input('Proposal UUID to inspect: ')
inspected = client.get(f'/purchases/{purchase_id}')
inspected.raise_for_status()
proposal = inspected.json()  # Safe details; inspect/render untrusted text as text.
decision = input('Explicit decision, APPROVE or DENY: ')
request_body = {
    'decision': decision,
    'snapshot_hash': proposal['snapshot_hash'],
    'snapshot_hash_version': proposal['snapshot_hash_version'],
}
csrf_name = '__Host-agentpal_csrf'  # agentpal_csrf_dev for explicit HTTP loopback mode.
result = client.put(f'/purchases/{purchase_id}/approval', json=request_body,
                    headers={'X-CSRF-Token': client.cookies[csrf_name]})
result.raise_for_status()
result.json()  # Consent and payment state are separate.
```

PayPal Vault onboarding, dispatch/recovery and reconciliation services remain pending.
Every eventual Sandbox payment still goes to AgentPal's single Business account,
not the catalog retailer. Approval places no real retailer order and guarantees no
physical stock or fulfillment. No live Channel3 or PayPal API is called by these routes.

## Single-owner authentication

No public registration exists. After initializing a fresh database as above,
export its `DATABASE_URL` privately and initialize the owner in an interactive
terminal. The command prompts for a login name and a hidden password twice;
passwords are never command arguments. Use a unique 15–128 character passphrase.
Duplicate initialization exits with status 1 and leaves the existing owner
unchanged. Invalid input/configuration or noninteractive input exits with status 2.

```sh
rtk proxy uv run agentpal-bootstrap
```

The equivalent module entry point is `rtk proxy uv run python -m agentpal.auth.bootstrap`.
Only the requested owner and a safe audit event are inserted; there are no seeded
policies, limits, PayPal references or default credentials. Bootstrap needs the
database URL but does not need browser-origin settings. Authentication reuses the
existing schema; apply the current migration head (`0003`) for approval commit checks.

All configuration is read from exported environment variables, never automatically
from `.env` or `.env.local`:

| Variable | Purpose / default |
|---|---|
| `DATABASE_URL` | Private PostgreSQL psycopg connection URL; required for authentication |
| `AUTH_ORIGIN` | Required single external browser origin, e.g. `https://agentpal.example`; no path/wildcards |
| `AUTH_ALLOW_INSECURE_LOCALHOST` | `false`; explicit `true` permits HTTP only for localhost/127.0.0.1/::1 |
| `AUTH_SESSION_TTL_SECONDS` | Absolute lifetime: `28800` (8 hours), configurable from 60 to 86400; no sliding extension |
| `AUTH_LOGIN_MAX_ATTEMPTS` | `5` attempts, including successes, across all usernames and IPs in one process |
| `AUTH_LOGIN_WINDOW_SECONDS` | `60` seconds for that bounded sliding window |

For local HTTP development, choose one exact loopback host and use it consistently:

```sh
rtk proxy uv run python -m agentpal.auth.bootstrap
rtk proxy env AUTH_ORIGIN=http://127.0.0.1:8000 AUTH_ALLOW_INSECURE_LOCALHOST=true uv run uvicorn agentpal.main:app --host 127.0.0.1 --port 8000
```

The first command is needed only once. On HTTPS deployments, set the external
HTTPS `AUTH_ORIGIN` and keep development mode disabled. Session cookies are
Secure, HttpOnly, SameSite=Strict, host-only, Path=/ and use the `__Host-` prefix.
Development uses distinct cookie names and omits Secure only in explicit loopback
mode. A TLS reverse proxy must preserve Host and report HTTPS through Uvicorn's
proxy handling restricted to the actual trusted proxy IPs. Never trust forwarded
headers from arbitrary clients. No frontend or CORS allowlist is implemented;
future frontend/API traffic must share the configured origin, using a same-origin
development proxy if necessary.

| Endpoint | Behavior |
|---|---|
| `POST /auth/login` | JSON `login_name` and `password`; trusted Origin or Referer required; returns only `login_name` and sets fresh cookies |
| `GET /auth/me` | Requires an active database session; returns only `login_name` |
| `POST /auth/logout` | Requires trusted origin and, for an active session, `X-CSRF-Token`; durably revokes it and clears cookies |

Passwords use Argon2id with the RFC 9106 low-memory profile (64 MiB, three passes).
Valid older parameters are upgraded on successful login. Unknown names use the
same owner verification path; an uninitialized account uses a dummy Argon2id hash.
Wrong credentials return the same generic 401. Login bodies are bounded to 4096
bytes and malformed inputs never echo passwords. Authentication responses use
`Cache-Control: no-store`; database failures return a generic 503. Health remains
available without authentication configuration; auth fails closed if it is absent.

Session and CSRF tokens each contain 32 random bytes. Only purpose-separated
SHA-256 hashes are persisted. A successful re-login rotates/revokes the current
browser session; separate browser sessions can coexist. Absolute expiration and
revocation are checked in PostgreSQL on every protected request. Session creation
and revocation are audited without raw tokens. A missing/expired/revoked session
has no authority: repeated logout returns 204 and clears stale cookies without an
authenticated database mutation. Protected writes still reject expired sessions.

The separate CSRF cookie is deliberately readable by same-origin frontend code;
the session cookie is HttpOnly. Read the CSRF cookie into `X-CSRF-Token` for
state-changing requests. The server compares its hash against the active session
row, so another session's token cannot authorize a request. Cookies alone cannot
authorize writes. Exact Origin/Referer and Fetch Metadata checks provide additional
protection, including against login CSRF. Origin must match when supplied; if
absent on a write, a same-origin Referer is required. Cross-site/same-site Fetch
Metadata is rejected; a missing metadata header is supported with origin checks.

To exercise the local flow without credentials in shell history, open an
interactive Python session with `rtk proxy uv run python`, then enter:

```python
import getpass
import httpx
origin = "http://127.0.0.1:8000"
client = httpx.Client(base_url=origin, headers={"Origin": origin})
credentials = {"login_name": input("Owner login: "), "password": getpass.getpass("Password: ")}
response = client.post("/auth/login", json=credentials)
del credentials
response.raise_for_status()
client.get("/auth/me").json()  # Safe login-name response only.
csrf = client.cookies["agentpal_csrf_dev"]
client.post("/auth/logout", headers={"X-CSRF-Token": csrf}).status_code  # 204
client.close()
```

Do not print cookie jars, request bodies, hashes or token variables, and do not
configure request-body/Cookie/Authorization logging at the application or proxy.
Run verification with `rtk proxy uv run pytest -q`; tests use fictional credentials
and isolated PostgreSQL, not the owner's configured database.

Future read endpoints should depend on `OwnerRead` from `agentpal.auth.api`.
Future sensitive writes should accept `OwnerWrite`, use `context.connection` for
the entire short transaction, and obtain approval/audit actor IDs from
`context.owner.session_id`. This dependency validates session and CSRF, acquires
owner then session locks to serialize with logout, rolls back failures, and commits
before the response is sent. Financial triggers remain in force. Do not accept a
caller-supplied owner/approver ID or do network I/O inside that transaction.

The owner cookie is a sensitive bearer credential: an AI agent cannot authenticate
by claiming an owner role, sending an agent ID, or using its own bearer token.
Never provide owner passwords, session/CSRF cookies or owner HTTP clients to agent
tools or model context. Authentication proves credential possession, not physical
human presence. Keep future agent routes/tool capabilities separate from owner
dependencies. XSS or a compromised browser can still perform owner actions;
frontend hardening remains necessary. There is no MFA, password reset or recovery
flow in this slice. The bounded login limiter and two concurrent Argon2 slots are
process-local, reset on restart and can cause temporary owner login denial under
attack; run one worker as approved. Multi-worker deployment needs shared admission
control. Durable sessions themselves survive backend restarts.

Security references: [argon2-cffi parameters](https://argon2-cffi.readthedocs.io/en/stable/parameters.html),
[OWASP CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html),
and [OWASP authentication guidance](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html).

## Owner policy management and monthly budget

All endpoints below require the existing authenticated owner cookie. Policy
writes require `X-CSRF-Token` and the trusted Origin/Referer checks described above.
The agent cannot supply an owner/session identity or modify configuration through
an agent bearer token. There are no new environment variables or dependencies.

| Endpoint | Contract |
|---|---|
| `GET /policy` | Current immutable policy version and its UUID; 409 `policy_setup_required` before initial setup |
| `PUT /policy` | Full replacement of all five settings, with required nullable `expected_policy_id`; returns `policy`, `budget`, `changed` |
| `GET /policy/history?limit=20&offset=0` | Versions newest first, `active` flag and `has_more`; limit 1–100; no session identifiers |
| `GET /budget/current` | Current UTC period, effective/original limits, captured spending, outstanding protection, available amount, active policy UUID and next reset |

There are no financial defaults. On the first PUT, supply `expected_policy_id:
null`; subsequently use the UUID returned by GET `/policy`. Send monthly budget,
auto-approval threshold and hard limit as nonnegative decimal **strings** with at
most two fractional digits. JSON numbers, exponents, NaN and fractional cents are
rejected before any rounding. TTL is a positive JSON integer number of seconds
within BIGINT and PostgreSQL/Python expiry arithmetic bounds. Auto-approval must not
exceed the hard limit; the hard limit can exceed the shared monthly budget.
Zero amounts and an empty allowlist are valid configurations, not default values.

Merchant allowlists must contain unique, canonical lowercase domains, with no
scheme, path, wildcard or trailing dot, using the database's existing validation.
Allowlist order does not change policy meaning. All fields are required; unknown
fields are rejected. Request uploads are bounded to 16 KiB and are fully parsed
before acquiring financial row locks. Malformed requests never echo their input.
Responses serialize money as decimal strings and timestamps as UTC. Unexpected
provider spending can retain more than two fractional digits and is not rounded.

After logging in with the interactive Python example above, **before logout**,
configure the initial policy or replace the current version using your own values:

```python
current = client.get("/policy")
if current.status_code == 409 and current.json()["detail"]["code"] == "policy_setup_required":
    expected = None
else:
    current.raise_for_status()
    expected = current.json()["id"]
domains = input("Canonical merchant domains, comma-separated (blank denies all): ")
body = {
    "expected_policy_id": expected,
    "monthly_limit": input("Monthly USD budget: "),
    "auto_approval_limit": input("Auto-approval USD threshold: "),
    "hard_purchase_limit": input("Hard USD purchase limit: "),
    "purchase_ttl_seconds": int(input("Proposal TTL seconds: ")),
    "merchant_allowlist": [item.strip() for item in domains.split(",") if item.strip()],
}
headers = {"X-CSRF-Token": client.cookies["agentpal_csrf_dev"]}
saved = client.put("/policy", json=body, headers=headers)
saved.raise_for_status()
saved.json()
client.get("/budget/current").json()
client.get("/policy/history").json()
```

For HTTPS, use the `__Host-agentpal_csrf` cookie instead. The existing client
already supplies Origin and authenticated cookies; do not pass it to agent tools.

**Approved Decision 25:** configured monthly-budget changes apply immediately to
the current UTC month's effective limit and establish the base for future months.
A decrease cannot undercut confirmed USD spending plus outstanding protection.
The new immutable policy, active pointer, current-period limit and owner-attributed
audit event commit together. A rejected decrease changes none of them. Period
initial policy/limit, historical months, purchase snapshots and reservation/payment
period bindings remain unchanged. New months use the latest active policy with
no rollover; on-demand period creation remains protected by PostgreSQL locks.
The accounting month is selected by the database initializer under the owner
financial lock, providing one period for the operation even at a UTC boundary.

A policy replacement invalidates unexecuted proposals under older versions.
Already-dispatched/uncertain payments retain their original financial protection
and recovery identity. This API does not cancel, settle or release purchases.
It does not implement proposal evaluation, payment execution or reconciliation.

When configuration and effective limit already match, PUT is a no-op: it preserves policy identity, approvals
and audit counts. Repeating an already successful PUT with its old expected UUID
is accepted only when the requested configuration and effective limit already
match; it does not create another policy version. Concurrent different updates
with the same expected UUID cannot overwrite each other; one receives a conflict.

| Response | Meaning / next action |
|---|---|
| 401 | Missing, forged, expired or revoked owner session |
| 403 | CSRF or browser-origin validation failed |
| 409 `policy_setup_required` | Configure an initial policy; no budget was invented |
| 409 `policy_conflict` | Another version is active; refresh before replacing it |
| 409 `budget_conflict` | Proposed decrease is below current spending/protection; keep or raise the limit |
| 409 `transaction_conflict` | PostgreSQL aborted a serialization/deadlock conflict; refresh and retry the same request |
| 422 `invalid_policy` / `invalid_allowlist` | Correct the supplied fields; nothing was committed |

Serialization aborts, including commit-time failures, roll back the complete
transaction before a response is sent. There is no automatic financial HTTP retry.
Owner/session/financial lock ordering also serializes writes with logout,
reservations, dispatch claims and captured-spending transitions. All runtime
financial writers must continue to follow that lock order and use the guards.
The database role remains trusted; future configuration writers must reuse this
atomic policy/budget path rather than changing the active pointer independently.

The budget uses `agentpal.budget_balances`; available is calculated in PostgreSQL
as effective limit minus confirmed USD spending and outstanding protection.
It can be negative. An exact full USD capture replaces the hold immediately;
unexpected partial/extra/foreign results retain the existing conservative rules.
`unresolved_payment_count`, `review_required_payment_count`,
`non_usd_capture_count` and `overcommitted` expose summary warnings without
inventing FX conversions. Detailed payment/audit review remains a future UI/API.
Unexpected provider facts can exceed a limit after an otherwise valid adjustment;
those facts remain recorded and further commitments are blocked. The API never
hides such overspending to make the budget appear within its cap.

## Persistence architecture

Alembic is the migration coordinator. `migrations/versions/0001_persistence.sql`
defines the initial PostgreSQL schema; additive revision `0002` hardens lifecycle,
capture accounting and limit updates, and `0003` adds approval commit checks. SQLAlchemy Core
and psycopg provide connections; there is no duplicate, incomplete ORM schema
or autogeneration workflow. Create subsequent reviewed Alembic revisions for
schema changes. Application connections use READ COMMITTED isolation and hide
SQL parameter values; connection strings are represented as secrets.

| Tables | Responsibility |
|---|---|
| `owners`, `owner_sessions`, `agents` | One owner; Argon2id hash storage; hashed session/CSRF tokens; agent attribution |
| `policies` | Immutable configuration versions, exact-domain allowlist, owner-session provenance |
| `budget_periods`, `budget_reservations` | UTC monthly limits and permanent reservation attribution/state |
| `purchases`, `approvals`, `offer_revalidations` | Immutable selected-offer/financial snapshots, bound human decisions, source-check evidence |
| `vault_setups`, `payment_methods` | Onboarding correlation/expiry, verified completion, encrypted provider references |
| `payments`, `payment_operations` | One payment per purchase; distinct immutable logical POST/request identity and encrypted exact payload |
| `provider_observations`, `payment_captures` | Append-only reconciliation evidence and canonical confirmed captures, including unexpected outcomes |
| `audit_events` | Append-only decision, activation, reservation, dispatch and capture history |

There are no permanent discovery, product-catalog, webhook or organization tables.
Decision 22 permits bounded run-local references and one backend process; their
runtime implementation now exists in `agentpal.discovery`. Once submitted, the reference hash and
necessary selected-offer evidence become durable with the purchase. A unique
origin hash prevents competing inserts for the same selection. `agentpal.proposals`
returns the existing proposal on a matching trusted-context/quantity retry and
rejects changed quantity. A new selection never proves safe recovery of an uncertain payment.

### Financial and authorization invariants

- Planned amounts use an exact, finite, nonnegative NUMERIC domain that rejects
  fractional cents **before rounding**. Quantity is positive; total is generated
  as unit price times quantity. Currency is USD. No thresholds or TTL default
  values are embedded in the schema. Zero-value storage is supported; whether
  such a proposal can be sent to PayPal remains an integration decision.
- Monthly identity is `(owner, month_start)` with explicit generated UTC start/end
  timestamps. `ensure_current_budget_period()` uses the active policy and
  concurrency-safe insertion. No rollover or scheduler. A reservation binds to
  the UTC period at reservation time; a cross-month unexecuted proposal can use
  that current period only while its original policy and TTL remain valid.
- Financial writers serialize on the owner row, then the original period and
  payment rows. A real owner-row revision update prevents obsolete aggregate
  reads under REPEATABLE READ; serialization failures require transaction retry.
  Future services must acquire the owner lock before other financial row locks
  and handle PostgreSQL deadlock/serialization aborts safely.
- `budget_balances` derives outstanding protection and actual confirmed USD captures.
  One exact, same-order full USD capture outside a stopped/review state replaces
  its held amount immediately,
  including after a committed capture followed by a process interruption before
  status/settlement. The durable reservation can remain `held` until settlement;
  the view counts that obligation once as spent, without rewriting history.
  New reservations cannot exceed the available period limit. Capture evidence
  may expose overspending; it is never rejected to preserve an artificial cap.
  Unexpected partial captures conservatively retain the full reservation while
  also recording actual spend until manual reconciliation. Foreign-currency
  evidence is retained for review without conversion into USD spending.
- Period initial identity/limit are immutable. Explicit current-period limit
  adjustments are possible and automatically audited. A reduction cannot go
  below confirmed USD spending plus outstanding protection; increases remain
  possible even when unexpected captures have already exceeded the old limit.
  Limit updates serialize with reservations and captures. Changing a policy does
  not silently rewrite period history. Under Decision 25, the authenticated
  configuration API atomically updates the current effective limit as well as
  the configured monthly amount for future periods.
- Purchase contents, policy versions, approvals, source observations, confirmed
  captures and audit events are immutable. The database computes a versioned
  snapshot binding from typed purchase fields with canonical epoch timestamps. Expiry derives from the policy
  TTL at creation; approval does not extend it.
- Merchant-rejected proposals remain durable and automatically generate audit
  events. Database reservation/dispatch guards independently reject disallowed
  merchants, hard-limit violations, rejected proposals, missing human approval,
  expired proposals and stale policy versions. This is a final integrity guard;
  the initial deterministic service is implemented in `agentpal.proposals`;
  human approval is implemented in `agentpal.approvals`; final dispatch remains pending.
- A passed source check must match product, domain, URL fingerprint, condition,
  unique-offer count, original price, USD and exactly `InStock`. Definitive
  identity/price/availability failures invalidate execution; a later passing
  observation cannot erase that history. A source error blocks until a fresh
  successful check. Each first POST claim requires a check performed after
  operation preparation and bound to that operation. The backend must still
  perform the actual independent retrieval immediately before claiming dispatch.
- A committed dispatch claim requires a still-held reservation within the
  current period limit and marks the local **possibly dispatched** boundary.
  It does not prove a provider response. Policy changes serialize against that
  claim. Database preparation and HTTP transmission cannot be atomic. Network
  calls must occur outside transactions. After a possible dispatch, TTL/policy
  changes do not release the reservation or justify a replacement payment.
- Operation endpoints, request IDs, payload ciphertext and plaintext-body hashes
  are immutable. Create and capture are separate logical operations. There is
  one operation of each kind per payment; retries increment that same operation.
  Replay is blocked unless endpoint-specific documented retention was recorded
  and its original window remains valid. There is **no assumed retention default**.
  A currently dispatched operation cannot be claimed again; only an explicitly
  uncertain operation/payment can enter a safe replay. Recovery services must
  establish that the original worker is no longer executing before marking it
  uncertain. Source and provider APIs are never called by these database guards.
  An existing safe replay is distinct from starting a new POST operation; new
  operations still require current authorization/revalidation. Multi-step Orders
  continuation/recovery semantics need service review before payment integration.
- Payment states cannot return to preparation or execution after completion,
  confirmed failure, reconciliation or payer-action stop. Review can close with
  fresh same-payment reconciliation evidence and matching capture/no-charge facts.
  Operation outcomes require a dispatched operation and operation-bound provider
  evidence; an uncertain outcome remains recoverable with the original identity.
  Capture dispatch requires a completed create operation, no other in-flight or
  uncertain operation, and no recorded captures. Confirmed unexpected facts can
  reopen a terminal payment for review without permitting further execution.
- `PAYER_ACTION_REQUIRED` stops dispatch claims. Unknown outcomes retain their
  reservations. Confirmed failure needs same-payment no-charge evidence and no
  recorded captures. Full settlement requires exactly one matching full USD
  capture; extra/partial/foreign or conflicting captures require review. Capture
  IDs are globally unique, preventing duplicate accounting. Late evidence stays
  associated with the original reserved period, regardless of capture date.
  Unexpected USD captures can close through a separate `reconciled` outcome,
  backed by an active owner-session review certifying the resolved exposure;
  this releases the hold without labeling partial capture as full success.
  Foreign-currency evidence cannot authorize settling a held reservation because
  the MVP has no FX accounting rule. The future review service must verify that all capture facts
  are recorded and no further exposure remains before certifying resolution.

The [targeted persistence review](docs/Persistence_Hardening_Review.md) records
the reproduced failures, transition rules, accounting examples and upgrade
compatibility. Existing revision `0001` databases can explicitly run the same
`alembic upgrade head` command; no reset is required. No persistent database was
contacted during this work. Revision `0002` changes definitions, not historical
rows; pre-existing inconsistent states require evidence-based review. Downgrading
to `0001` restores known guard weaknesses and the previous accounting semantics.

### Trust boundaries and remaining work

The database does not authenticate an HTTP cookie, establish that a request is
human, verify a PayPal response, call Channel3 or prove that ciphertext is
encrypted. The authentication layer now validates sessions/CSRF and uses Argon2id;
future financial services must use its verified owner context, independently
obtain source/provider evidence, encrypt sensitive
references and the exact replayable request bytes, and sanitize observation/audit
JSON. Never expose stored Vault/session material or arbitrary JSON through an API.
Onboarding callback handling, encryption/key provisioning, password recovery,
deployment-specific session lifetime selection, source URL/condition matching fixtures, Channel3 retention
permissions, replay-window verification and all financial configuration values
remain explicit integration work. Description text is not needed in financial
snapshots; a URL fingerprint is a conservative comparison guard, not a provider
offer ID or an exemption from data-use terms. Source observations do not prove
live checkout price, legal seller identity, physical stock or fulfillment.

Use a separate non-owner runtime database role. The migration owner can alter
its own triggers and must not be the application account. Provision roles using
your normal administration, then grant only the required rights, for example:

```sql
GRANT USAGE ON SCHEMA agentpal TO agentpal_app;
GRANT USAGE ON TYPE agentpal.usd_amount TO agentpal_app;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA agentpal TO agentpal_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA agentpal TO agentpal_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA agentpal TO agentpal_app;
GRANT UPDATE ON agentpal.owners, agentpal.owner_sessions, agentpal.agents,
  agentpal.budget_periods, agentpal.budget_reservations, agentpal.vault_setups,
  agentpal.payment_methods, agentpal.payments, agentpal.payment_operations
  TO agentpal_app;
```

Do not grant DELETE, TRUNCATE, schema ownership or DDL rights to the runtime role.
PUBLIC receives no access to the application schema/tables/functions. This is a
trusted backend role, not an AI/database tool capability. Sensitive owner actions
must add verified human attribution; automatically generated system audit events
alone are not evidence of human authorization.

Design references: PostgreSQL [constraints](https://www.postgresql.org/docs/current/ddl-constraints.html)
and [locking](https://www.postgresql.org/docs/current/explicit-locking.html),
the [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html),
and the [SQLAlchemy psycopg dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg).
