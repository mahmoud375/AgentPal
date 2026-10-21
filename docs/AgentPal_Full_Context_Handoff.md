# AgentPal — Full Project Context & Progress Handoff

**Last updated:** 2026-10-21  
**Status:** PayPal/Channel3 feasibility and the FastAPI foundation passed. PostgreSQL persistence, owner authentication/policy/budget APIs, Channel3 discovery, idempotent proposals and secure human purchase approval/read APIs are implemented. Provider behavior is tested offline with synthetic fixtures; live adapter compatibility remains unverified. Reservation/payment orchestration, agent runtime and end-to-end execution remain unimplemented.

**Purpose:** The single authoritative project context for the existing AgentPal repository.

> **Authority and scope:** Sections 1–14 describe the current requirements, evidence, open questions, and roadmap. Section 3, **Approved MVP Decisions**, records all 25 owner-approved decisions and supersedes earlier proposals. Appendix A preserves historical evidence only; its commands, temporary code, old plans, and statuses are not current implementation instructions. Spike-script comments can also predate completed experiments and approved scope. This context does not authorize implementation of every planned feature.

## 1. Operating instructions for assistants / Codex

- Communicate and write repository documentation, reports, and submission materials in **English**.
- Work in the existing repository at `/home/mahmoud/MY_SDD/Projects/agentpal`. Python source lives under `src/agentpal/`; a separate `backend/` application does not exist.
- Complete one authorized task at a time. Respect explicit owner implementation authority; report actual changes and verification. Earlier review-only tasks do not override a later authorized implementation request.
- Follow the referenced `/home/mahmoud/.codex/RTK.md`: prefix shell commands with `rtk`. Environment: Fedora Linux, Zsh, Python/uv, Node.js/npm.
- Never open or expose `.env`, `.env.local`, or other credential-containing files during context review. Do not put secrets, access tokens, Vault token references, or customer credentials into public documentation, frontend output, AI context, screenshots, or logs. `.env.example` is for placeholders.
- Do not commit, push, deploy, install packages, run API requests, or perform Sandbox payments without task authorization. Keep each task within its authorized scope.
- Distinguish **approved requirements**, **observed experiments**, **implemented features**, and **OPEN design questions**. HTTP 200 alone does not establish success when the body or SSE stream contains an error.
- Inspect repository reality before making implementation claims. Report contradictions instead of silently choosing requirements. Preserve financial evidence and avoid unnecessary enterprise complexity.

## 2. Product, demonstration, and disclosure

**AgentPal is a policy-controlled AI procurement/payment simulation for one human owner.** A planned PydanticAI agent discovers and compares external product listings through Channel3 and proposes purchases. FastAPI independently retrieves the selected listing, applies owner policies, obtains human approval when required, and controls PayPal Sandbox execution. PostgreSQL will enforce shared-budget correctness and preserve the audit trail. React/TypeScript and AG Grid are planned for the operational interface; AG Studio analytics remain optional pending licensing and integration review.

> **Prominent demo disclosure:** Channel3 discovers external retailer listings, while every demonstration payment goes to **AgentPal's single PayPal Sandbox Business account**. The simulation does **not** place or fulfill an actual order with Lenovo, Best Buy, or any other catalog retailer. Catalog merchants are listing sources; they are not necessarily PayPal payees. This disclosure must remain clear in the application, demo, and submission materials.

The five product demonstration goals below are AgentPal goals, not five separate official contest mandates:

1. **Zero-click authorization:** a real AI proposal passes deterministic policy and completes a vaulted Sandbox payment without a new purchase-time payer redirect.
2. **Human approval:** a proposal above the auto-approval threshold and within the hard purchase limit waits for an authorized human session.
3. **Policy rejection:** a disallowed catalog merchant, hard-limit violation, or insufficient budget is rejected before payment dispatch, with a clear rule trace.
4. **Duplicate prevention:** duplicate clicks, concurrent requests, and safe retries do not duplicate purchases or charges.
5. **Prompt injection defense:** merchant-controlled content cannot alter policies, financial inputs, or approval authority.

A functional, reproducible end-to-end application and a public English YouTube demonstration under three minutes remain delivery goals; isolated spikes are not the finished application.

## 3. Approved MVP Decisions

All 25 decisions below are **confirmed requirements**, not proposals. Decisions 22–24 were approved in the persistence implementation request; Decision 25 was approved in the authenticated policy/budget API request and supersedes earlier open budget-adjustment questions. Remaining integration/configuration choices are collected in Section 13.

### Decision 1 — Single PayPal Sandbox Business Account

All demonstration payments go to one PayPal Sandbox Business account. External/catalog merchants are product listing sources, not necessarily payment recipients. The backend controls the actual PayPal payee. Do not claim multi-merchant payment routing, real retailer checkout, or fulfillment.

### Decision 2 — Single Human Owner

The MVP has one human owner who manages policies, budgets, payment connections, and human approvals. The AI cannot impersonate the owner or approve its own purchases. Organizations and complex role-based access control are outside the MVP.

### Decision 3 — Shared Budget

All AI agents share the same owner-level budget. Concurrent purchases must not overspend it. PostgreSQL transactions and constraints must enforce correctness; AI reasoning cannot enforce the budget.

### Decision 4 — Automatically Renewing Monthly Budget

The shared budget renews every calendar month, with a distinct accounting period for each month. Historical budgets, spending, and unresolved reservations remain associated with their original periods.

### Decision 5 — USD Only

All catalog prices, budgets, purchase amounts, and PayPal Sandbox payments use USD. No foreign exchange or multi-currency conversion is in scope. Use Python `Decimal` and PostgreSQL `NUMERIC` for financial amounts; a non-USD external listing cannot be silently converted.

### Decision 6 — Fixed All-Inclusive Catalog Pricing

The application's proposed purchase amount is `total_amount = unit_price * quantity`. AgentPal calculates no additional shipping, tax, or checkout fees. AI-generated prices are never authoritative. If the selected offer's authoritative **application pricing** changes before dispatch, the old proposal cannot execute; a new proposal is required.

External Channel3 listing prices are not guaranteed retailer checkout quotes and must not be represented as independently verified final retailer prices.

### Decision 7 — Auto-Approval Threshold and Hard Purchase Limit

The owner configures both limits; do not hardcode arbitrary financial thresholds.

- At or below the auto-approval threshold: eligible for automatic authorization if all other policies pass.
- Above that threshold but within the hard purchase limit: requires human approval.
- Above the hard purchase limit: rejected.
- Insufficient monthly budget: rejected regardless of human approval.

### Decision 8 — Merchant Allowlist

The owner configures permitted **catalog merchants**. Deterministic backend policy evaluation rejects any catalog merchant outside that allowlist. Merchant identity cannot be accepted blindly from AI-generated text. The allowlist applies to listing merchants, not the single Sandbox receiving account; domain-to-merchant representation needs design review.

### Decision 9 — No Monthly Budget Rollover

Unused funds do not carry over. Each month starts with its configured base monthly limit. Pending or uncertain prior-month payments remain accounted for in their original month.

### Decision 10 — UTC Accounting Boundaries

Monthly periods begin at **00:00 UTC on the first day of each month**. Use PostgreSQL `TIMESTAMPTZ`. The frontend may show dates/reset times in the viewer's local timezone while clearly identifying UTC as the accounting boundary.

### Decision 11 — On-Demand Automatic Monthly Renewal

Transactionally create or retrieve the new monthly period on the first relevant API request in that month, with protection against duplicate creation under concurrency. No background renewal scheduler is required. The period's financial boundaries remain correct even when its record is created later.

### Decision 12 — Single Configurable Purchase TTL

The owner configures one expiration duration starting when the purchase proposal is created. It governs both proposal validity and human approval; approval does not start a fresh validity period. An expired, unexecuted proposal requires a new proposal. Expiry must not mark a possibly dispatched payment as failed or release its reserved budget.

### Decision 13 — Invalidate Old Proposals on Policy Changes

Changing the active policy invalidates every older proposal that has not yet entered PayPal dispatch. A new proposal must be evaluated under the new policy; do not silently alter an approved snapshot. Already-dispatched operations are not automatically canceled by a policy change.

### Decision 14 — In-App PayPal Vault Onboarding

Provide an in-app **Connect PayPal** experience for the single owner:

1. The owner initiates connection.
2. The backend creates the appropriate Sandbox Vault setup operation.
3. The owner authorizes on PayPal-hosted pages.
4. The backend verifies/finishes Vault token creation.
5. The backend securely stores the payment-method reference.
6. Subsequent authorized purchases can use zero-click vaulted payments.

A browser redirect alone does not prove Vault completion. Vault token references and credentials remain server-side and must not be unnecessarily exposed to the frontend or public repository.

### Decision 15 — Stop and Manual Review on PAYER_ACTION_REQUIRED

If a zero-click payment requires additional payer action, stop further automatic execution and surface the operation for manual review. Preserve payment state and relevant budget reservations until the outcome can be safely resolved. Do not implement interactive checkout fallback or automatically create a replacement payment. This restriction is separate from the PayPal-hosted authorization required for Vault onboarding.

### Decision 16 — API Reconciliation and Manual Review

Use PayPal Orders API retrieval when a known PayPal order ID exists. Preserve durable state and reservations for uncertain outcomes; require manual review when the provider outcome cannot be safely determined. A timeout or lost response is not proof of failure. **No PayPal webhook integration is in the MVP.**

### Decision 17 — Controlled Safe Retries

Retry only the same logical PayPal payment operation, with the same `PayPal-Request-Id`, the exact same financial request payload, and a verified, documented idempotency retention window for that specific endpoint. If the window is unknown or expired, do not automatically repeat the POST; use reconciliation/manual review. Never generate a fresh request ID to retry a payment whose result is uncertain.

### Decision 18 — Full Capture Only

Intend one complete capture per authorized purchase. No intentional partial captures, multiple-capture workflows, or refund workflow. If unexpected partial or extra captures occur, record the actual provider facts and move the purchase into reconciliation; never discard or falsify financial evidence.

### Decision 19 — Single Owner Authentication

Implement one owner login with **no public registration**. Requirements: secure password hashing, preferably Argon2id; secure HttpOnly cookie-based authenticated sessions; CSRF and session-abuse protection; authentication and authorization on sensitive endpoints. Human approval requires a verified human session and cannot be authorized merely by an AI tool request. No complex multi-user or organizational RBAC. Precise authentication implementation and schema details remain subject to design review.

### Decision 20 — External Product Search API

Discovery and comparison must use an external product data API; an internal static catalog cannot be the sole discovery source. The backend independently retrieves selected product/offer data, validates the merchant listing, price, currency, and available status information, and builds an immutable purchase snapshot. External descriptions and merchant data are untrusted. Discovery is not retailer checkout or order fulfillment.

### Decision 21 — Channel3 as the Initial Product Provider

Channel3 is the initial external provider, approved for feasibility testing. The initial search and product-retrieval test completed successfully. It is suitable for discovery and backend product-data refresh, but has not been proven suitable for authoritative retailer checkout or fulfillment. Do not assume unobserved offer identifiers, freshness guarantees, or checkout capabilities.

### Decision 22 — Transient Offer References

Use backend-issued opaque references in bounded, server-controlled agent-run memory; one backend process/worker is explicitly permitted. Unused selections may be lost on restart and discovery repeated. Once submitted, necessary proposal identity/business/financial evidence becomes durable in PostgreSQL. No permanent discovery-only registry is required. Initially support only safely distinguishable offers: ambiguous multiple offers within a product and canonical merchant domain must be rejected, with no merchant substitution. Proposal uniqueness and payment idempotency remain separate. The registry/tool runtime itself is not implemented by the database task.

### Decision 23 — Latest Channel3 Listing Price

The latest successfully retrieved Channel3 listing price is the approved application pricing basis for the Sandbox demonstration. Independently revalidate before proposal creation and immediately before dispatch. Relevant identity, price (increase or decrease), currency or required-detail changes block the original purchase. AI never sets the amount. Listing observations remain distinct from guaranteed live retailer quotations; payment still goes to AgentPal's single Sandbox Business account.

### Decision 24 — Source-Reported Availability

Execution requires an independently observed, explicitly recognized available status; the initial spike observed `InStock`. Unknown, missing, unavailable and unsupported statuses cannot count as available. A failed refresh cannot authorize payment using stored price/availability. Explain failures and permit fresh discovery without substituting an offer or transferring its approval. Source-reported availability does not guarantee physical retailer stock.

### Decision 25 — Immediate Monthly Budget Updates

Changes to the configured monthly spending budget take effect immediately for the
current UTC accounting month and establish the configured base for future months.
An increase updates the effective current-month limit immediately. A decrease
must not undercut confirmed spending plus outstanding financial protection.
Rejected decreases must not partially alter the active policy or budget. Policy
and budget changes must be attributable to the authenticated human owner and
audited. Preserve immutable policy/purchase evidence, historical periods, initial
limits, confirmed captures, reservations and original payment/reservation periods.
Future months initialize from the latest active policy; previous-period unresolved
work stays in its original period. UTC calendar boundaries and no rollover remain
unchanged. For example, with $400 in spending/protection, a $1,000 limit may rise
to $1,500 or fall to $500, but cannot fall to $300. These numbers illustrate the
approved behavior and are not application defaults.

## 4. Trust boundaries, purchase intent, and selected-offer identity

| Layer | Responsibility | Boundary |
|---|---|---|
| AI / planned PydanticAI agent | External product discovery, comparison, explanations, structured proposals | Cannot supply authoritative prices/merchant identity, change policy or budgets, approve itself, or call PayPal directly |
| FastAPI backend | Independent listing retrieval/validation, policy evaluation, owner approval, durable orchestration, controlled PayPal payee | Must not turn unverified model output or merchant content into financial authorization |
| PostgreSQL | Transactions, constraints, durable snapshots, reservations, accounting and audit | Must enforce shared-budget and duplicate-prevention invariants despite concurrent requests or crashes |
| Human owner session | Policy/payment configuration and human approval | AI tool calls cannot confer human authority |
| Channel3 | External product/listing data | Domain is not verified legal merchant identity; listing data is not a guaranteed live checkout quote |
| PayPal Sandbox | Vault operations and actual order/capture facts | Does not replace application authorization or the budget ledger; no MVP webhook dependency |

### Critical unresolved issue: a product can have multiple merchant offers

The earlier intent example `product_id + quantity` is **insufficient**: product `PO7Dccd` appeared with offers from more than one merchant domain. Product identity alone cannot bind a proposal to the owner's intended merchant offer.

Decision 22 selects a backend-issued transient reference as the purchase selection mechanism. **No independent stable Channel3 offer ID was observed; do not invent one.** The implemented adapter rejects multiple offers within the same product/canonical domain and conservatively compares the complete URL fingerprint and nullable condition. Explicitly synthetic fixtures exercise these rules; real URL/condition stability still requires authorized verification. A unique response representation does not prove stable retailer offer identity.

Search selections stay in bounded process memory, bound to trusted owner/agent/run context. The proposal service now persists submitted product/domain identity, URL fingerprint, condition, source observations, exact money, origin-reference hash, evaluated policy/decision/reasons and immutable snapshot binding. The schema stores no permanent discovery registry or retailer catalog. Structurally valid prohibited merchants reach genuine deterministic evaluation and are durably rejected with audit evidence. The AI tool/API runtime is not implemented here.

The backend must independently retrieve/validate the selected listing and build an immutable snapshot. AI-supplied prices or merchant names cannot be authoritative financial inputs, even if they resemble external data. A backend re-fetch is independent of AI text, but still does not guarantee the retailer's live checkout price or stock.

Approval must bind to the unchanged purchase snapshot, verified human identity, and original proposal expiry. Policy changes, expiry, and changed application pricing follow Decisions 6, 12, and 13; unresolved refresh/identity behavior cannot be silently bypassed to dispatch a payment.

## 5. Architecture and inspected repository state

| Concern | Current decision / implementation status |
|---|---|
| Backend | FastAPI in existing `src/agentpal/` package |
| Bootstrap | `src/agentpal/main.py`: `FastAPI(title="AgentPal API", version="0.1.0")`; `GET /health` returns `{"status":"ok"}` |
| Packaging | `pyproject.toml`: project version `0.1.0`, Python `>=3.12`, uv build backend; `uv.lock` present |
| Existing dependencies | FastAPI/HTTPX/Pydantic stack plus SQLAlchemy 2.0, psycopg 3, Alembic and argon2-cffi; pytest is a development dependency, with exact resolution in `uv.lock` |
| Package entry point | `src/agentpal/__init__.py` still contains the starter CLI greeting; it is not completed application orchestration |
| Agent | PydanticAI planned; no live integration implemented |
| Product discovery | `channel3.py` fixed-origin search/detail adapter and `discovery.py` bounded transient references/independent comparison implemented offline; no HTTP routes or agent runtime |
| Payments | `scripts/spike_paypal.py` Vault/Orders spike passed; not integrated into FastAPI |
| Database | PostgreSQL-native SQL schema, Alembic revisions `0001`–`0003`, SQLAlchemy Core/psycopg connections and database guards implemented; no duplicate ORM schema |
| Local database setup | Tests start isolated local PostgreSQL clusters; PostgreSQL 18.6 is available and was used. No persistent database or Compose service was provisioned |
| Owner access | Interactive owner bootstrap and `/auth/login`, `/auth/me`, `/auth/logout` implemented; no public registration |
| Policy / budget services | Owner-only configuration/history/current budget and human approval/read APIs plus reusable proposal evaluation implemented; no reservation/payment-dispatch service |
| Frontend | React/TypeScript planned; exact framework OPEN; no integrated AgentPal frontend |
| Operational UI / analytics | AG Grid planned; AG Studio optional; both require appropriate licensing review |
| Hosting | Render + Neon previously tentative; hosting is not a binding approved choice and deployment is unverified |

Repository layout after persistence and authentication implementation (credential files omitted):

```text
agentpal/
├── .env.example
├── .gitignore
├── .python-version
├── LICENSE
├── README.md
├── pyproject.toml
├── uv.lock
├── alembic.ini
├── migrations/            # env, revision template and native SQL initial revision
├── tests/                 # disposable PostgreSQL fixtures and invariant tests
├── docs/
│   └── AgentPal_Full_Context_Handoff.md
├── scripts/
│   ├── spike_paypal.py
│   └── spike_channel3.py
└── src/
    └── agentpal/
        ├── __init__.py
        ├── main.py
        ├── policy.py      # owner policy versions and current UTC budget API
        ├── channel3.py    # bounded provider adapter, exact parsing and comparison
        ├── discovery.py   # trusted run context, transient registry and service
        ├── proposals.py   # idempotent immutable proposals and initial rule evaluation
        ├── approvals.py   # owner queue/details and immutable human decisions
        ├── db/            # explicit PostgreSQL settings and lazy engine factory
        └── auth/          # bootstrap, Argon2id, cookie sessions, CSRF and dependencies
```

The context review read `pyproject.toml`, both package files, both spike scripts, `.env.example`, and `.gitignore`. The environment example contains PayPal placeholders and `CHANNEL3_API_KEY`; the ignore file excludes `.env`, `.venv/`, `__pycache__/`, `node_modules/`, and `dist/`, among other entries. No credential file was read and no spike was executed. At review start, `git status --short` showed the project files/directories as untracked; no commit or push was performed.

**Previously completed FastAPI verification (2026-10-21):** import returned `AgentPal API`; in-process TestClient checks passed for title, version, and `/health` HTTP 200 with exact JSON. The earlier run reported a nonblocking HTTPX deprecation warning and a temporary uv cache. These are recorded prior results, not new tests or evidence of domain-feature completion.

## 6. Purchase lifecycle and financial correctness requirements

The following sequence is an implementation brief under Section 3, **not an approved API, state enum, or schema**:

1. The agent proposes a quantity and a safe selected-offer reference once that design is approved.
2. The backend independently retrieves product/offer information, validates listing/merchant, USD price and available status data, and builds an immutable purchase/policy snapshot with one proposal expiry.
3. Deterministic evaluation applies allowlist, threshold, hard limit, and shared monthly budget. Outcomes are rejection, eligibility for automatic authorization, or a request for human approval.
4. Required human approval must come from the verified owner session for the unchanged, unexpired snapshot. Approval cannot override budget or hard-limit rejection.
5. Before dispatch, enforce current-policy validity, TTL and unchanged authoritative application pricing. Policy/pricing invalidation or expiry requires a new proposal. Review must define the transaction boundary that makes these checks safe against concurrent policy updates and competing purchases.
6. Transactionally protect the shared budget and persist the reservation, operation identity, exact request payload, and relevant provider references before network I/O. Commit durable state before dispatch; do not hold a database transaction open across the PayPal call. Database reservation/release guards are implemented; trusted services must orchestrate them using independently verified provider evidence.
7. Execute against the backend-controlled Sandbox payee using the stored payment-method reference. Full capture and safe-retry behavior follow Decisions 17–18.
8. In a new transaction, record actual provider facts and apply each reservation/spending transition at most once. A denied or merely pending purchase is not captured spending.
9. For timeout, lost response, process interruption, unexpected capture behavior, or `PAYER_ACTION_REQUIRED`, preserve state/reservations and reconcile or require manual review. Retrieve a known order through the Orders API. Unknown/expired replay windows block automatic POST retries. No replacement payment, purchase-time redirect fallback, or webhook integration.

Monthly renewal is on demand and uses UTC calendar boundaries. Unused funds do not roll over; previous-period obligations remain in their original period even if resolved later. The exact period-binding rule for an unexecuted proposal crossing a month boundary remains OPEN; a new month must not silently erase or move existing reservations.

Vault onboarding is a separate owner-initiated flow (Decision 14). Its PayPal-hosted authorization does not authorize an interactive purchase fallback. Final states, allowed transitions, recovery behavior, and race protection need explicit design review before implementation.

## 7. Implemented PostgreSQL persistence

The initial schema is `migrations/versions/0001_persistence.sql`; additive revision `0002` hardens lifecycle/accounting/budget guards, and `0003` rechecks new human decisions at commit. Neither rewrites prior migration history or historical rows. Connection configuration lives in `src/agentpal/db/`; no credential files are loaded automatically and importing the application does not migrate/connect. See [README](../README.md#persistence-architecture) for entities, migration commands, transaction rules, access grants and limitations, and [the targeted review](Persistence_Hardening_Review.md) for findings and upgrade compatibility.

Implemented responsibilities: one owner and hashed session/CSRF persistence; agents; immutable policy versions; UTC monthly periods; immutable purchases and human approvals; offer revalidation evidence; Vault onboarding/encrypted method-reference storage; reservations; payments and separate immutable POST operations; provider observations/confirmed captures; append-only audit. There are no permanent discovery/product/merchant catalog, organization or webhook tables.

Key database guarantees: exact USD amounts without silent fractional-cent rounding; generated purchase totals; unique proposal origin; policy/TTL/snapshot/session bindings; concurrency-safe monthly creation and reservation accounting; operation-bound source checks requiring `InStock`; exclusive in-flight dispatch claims; immutable request identity/payload and conditional replay windows; evidence-bound failure/release; full-capture settlement and preservation of unexpected provider facts. Unexpected USD captures can resolve through a distinct, authenticated owner-reviewed reconciliation outcome without claiming full success. Actual captured spending may expose overspending and is never concealed to satisfy an authorization limit.

Reservations bind to the current UTC period when created, and original-period attribution remains unchanged during late reconciliation. A single matching full USD capture replaces its held amount in the derived balance immediately, even if status/settlement commits later; partial, extra or conflicting captures retain conservative protection while unresolved. Period initial limits are preserved; explicit limit adjustments are separately auditable and reductions cannot undercut confirmed spending plus outstanding protection. Decision 25 and the owner API now atomically couple configured monthly-budget changes to the current period's effective limit. A possibly dispatched reservation cannot be released on proposal expiry. Closed/stopped payments cannot re-enter execution; review closure needs fresh reconciliation evidence, and unresolved operations/capture facts block additional POST execution.

Database guards are necessary integrity checks, not complete business integrations. Future trusted services must authenticate human sessions/CSRF, independently retrieve/interpret Channel3 and PayPal evidence, encrypt private references/payloads, sanitize JSON, supply rule explanations, acquire consistent locks and recover interrupted operations. The database cannot establish source freshness, cookie authenticity or cryptographic correctness of supplied ciphertext. Financial policy values, encryption/key provisioning, password recovery and endpoint-specific PayPal replay evidence are not invented or seeded.

### Implemented single-owner authentication foundation

`src/agentpal/auth/` implements an interactive `agentpal-bootstrap` command with
hidden confirmed password entry and no password arguments/defaults. Argon2id uses
the RFC 9106 low-memory profile; duplicate/racing bootstrap attempts preserve the
existing owner. No financial configuration is seeded and migrations remain at
`0002`. No persistent database was accessed or owner provisioned during this task.

FastAPI exposes `POST /auth/login`, `GET /auth/me` and `POST /auth/logout`. Random
32-byte session/CSRF tokens are stored only as purpose-separated SHA-256 hashes
in the existing `owner_sessions` table. Session cookies are HttpOnly, host-only,
SameSite=Strict and Secure with `__Host-` names by default; explicit HTTP loopback
development uses distinct names. Absolute expiry defaults to eight hours and is
configurable from one minute to one day, without sliding renewal. Revocation and
expiry are checked from PostgreSQL; sessions survive backend restarts. Session
creation/revocation are audited without raw tokens, and re-login rotates the
current browser session. Login errors are generic and malformed requests do not
echo credentials. Owner info returns only the login name.

The CSRF cookie is intentionally readable by same-origin frontend code and must
be supplied as `X-CSRF-Token`; its stored hash binds it to the active session.
Logout and future owner writes also require the configured exact Origin or a
same-origin Referer. Cross-site/same-site Fetch Metadata is rejected; no CORS is
enabled. Inactive-session logout clears stale cookies with 204 without granting
owner authority; protected writes reject inactive sessions. The bounded login
limiter defaults to five attempts per minute globally across names/IPs, with two
concurrent Argon2 slots; admission counters reset on restart and require one
backend worker. They do not replace durable session storage.

Future financial reads should use `OwnerRead`; sensitive writes should use
`OwnerWrite` from `agentpal.auth.api`, its transaction connection and its verified
session ID for approval/audit attribution. The write dependency locks owner then
session, validates CSRF, rolls back errors and commits before returning a response.
Do not pass owner cookies, credentials or HTTP clients to agent tools. Agent IDs
and claimed roles cannot confer owner authority; stolen owner credentials still
represent a browser/credential compromise, not proof of physical human presence.
Password recovery, MFA, frontend/XSS defenses and deployment hardening remain
outside this slice. See [README](../README.md#single-owner-authentication) for
configuration, secure bootstrap and interactive login/logout verification.

Authentication verification on PostgreSQL 18.6: **132 tests passed**, including
44 authentication/HTTP/Argon2id cases and all 88 existing persistence cases.
Package source/wheel builds, locked dependency validation and the installed
bootstrap help entry point passed. The existing Starlette HTTPX TestClient
deprecation warning remains nonblocking. No credential files, external product
or payment APIs, or persistent user databases were accessed; no commit/push occurred.

### Implemented owner policy and budget APIs

`src/agentpal/policy.py` adds owner-only `GET /policy`, full-replacement
`PUT /policy`, paginated `GET /policy/history`, and `GET /budget/current`.
Writes reuse `OwnerWrite` and its verified session/CSRF/transaction connection;
reads reuse `OwnerRead`. The budget read rechecks the session under owner/session
locks before on-demand period initialization. Uploads are bounded and parsed before
financial row locks. No new tables, migrations, dependencies or configuration were
needed; existing authentication work and financial guards remain in place.

All five settings are explicitly supplied, with no seeded financial defaults.
Money inputs are exact nonnegative decimal strings with at most two fractional
digits; JSON money numbers are rejected. TTL is a positive integer within the
database/Python timestamp representation, auto-approval cannot exceed the hard
limit, and the canonical PostgreSQL allowlist validator rejects malformed,
uppercase, duplicate or ambiguous domain forms. The allowlist can be empty.
Fresh owners receive 409 `policy_setup_required` until their initial PUT.

Each actual configuration change inserts an immutable policy version and updates
the active pointer, the current effective monthly limit and a human-attributed
audit event in one short transaction. The old month base remains in
`initial_limit`/`initial_policy_id`; existing period bindings and purchase snapshots
are not rewritten. Budget decreases below spend/protection return 409
`budget_conflict` without partial state. Required nullable `expected_policy_id`
prevents lost updates: use null for initial setup and the active UUID thereafter.
Identical configuration/effective-limit requests are no-ops, including an exact
retry after success, so retries do not create duplicate versions/audits.
Different stale replacements receive 409 `policy_conflict`. PostgreSQL deadlock
or serialization aborts, including commit-time aborts, return 409
`transaction_conflict` after full rollback; the client can refresh and retry the
same request. No automatic financial HTTP retry is implemented.

Current-budget amounts come from `agentpal.budget_balances`; available is computed
with PostgreSQL NUMERIC and can be negative. UTC boundaries, reset time, effective
and initial limit, active policy ID, spending/protection and summary review/foreign
capture counts are returned without session secrets. Full captures are not counted
twice; unexpected provider precision/results remain visible. Policy changes
invalidate old unexecuted proposals through existing guards, while dispatched and
uncertain reservations retain their original protection and recovery identity.
This API does not evaluate new purchases, execute payments or reconcile them.

Final policy/budget verification on isolated PostgreSQL 18.6:
`rtk proxy .venv/bin/pytest -q --tb=line` reported **194 passed in 50.61 seconds**,
including all 132 previous cases plus 62 policy/budget cases. Tests exercise real
database concurrency, deferred commit-time serialization rollback and safe retry,
UTC period initialization/renewal, original-period exposure, large exact amounts,
policy invalidation and policy/reservation/dispatch/capture/revocation races.
The renewal test advances only the initializer's month clock in a disposable test
database; production has no clock override. Package builds, lockfile, syntax and
whitespace checks passed; the existing TestClient deprecation warning remains.
Decisions 1–24 remain verbatim. No financial migration, dependency, environment
configuration or existing test was changed in this task, and prior uncommitted
authentication work was preserved. No persistent database or external product/
payment API was accessed; no commit or push occurred.

### Implemented purchase proposals and deterministic initial evaluation

`src/agentpal/proposals.py` now connects Channel3/discovery to the existing
immutable purchase and audit persistence. No HTTP routes, migrations, dependencies,
financial defaults or new configuration were added. `ProposalService(engine,
discovery)` provides synchronous trusted `start_run(owner, agent_id)` and async
`create(context, offer_ref, quantity)`. `exact_total` and `evaluate_policy` are pure
helpers over trusted inputs; public output uses frozen `Proposal`/`ProposalResult`
and an allowlisted `.public()` projection, not raw database/provider records.

The trusted launcher receives an authenticated `OwnerSession` from `OwnerRead`.
`start_run` rechecks session activity, login and CSRF hash plus agent ownership/
enabled status, then generates a run UUID. AI tools receive only reference and
quantity arguments and close over the backend context; no owner/session/agent
identity is accepted as an AI authorization claim. Direct `RunContext` construction
is an internal trust boundary, not authentication. Creation/replay rechecks the
single owner's login and enabled owned agent, and verifies the reference's full
owner/agent/run binding. No human approval power is granted. Logout does not
automatically cancel an existing run; the future runtime must dispose its references
when ending/canceling a run, and must securely restore original run IDs after restart.

Creation checks durable origin identity before needing the volatile registry.
For a new proposal it resolves the reference and independently retrieves through
`DiscoveryService.refresh`, requiring unchanged product/domain/URL/condition,
unique supported USD price (including no decreases) and explicit `InStock`.
Source failures/changes/ambiguity/unavailability and invalid references are safe
validation errors, not fake business-policy rejections; they insert no purchase.
All source I/O occurs outside database transactions. Sync database scopes are
offloaded from the async event loop.

The short write transaction acquires the existing financial revision/owner lock,
rechecks agent/context, durable replay and reference validity, reads the latest
active policy and initializes/reads the current UTC budget period. No active
policy raises `POLICY_SETUP_REQUIRED`, without a proposal or seeded defaults.
Available money comes directly from `budget_balances` using PostgreSQL NUMERIC,
with existing full/partial/extra/foreign capture protection rules unchanged.
An observed UTC period rollover rolls back with `BUDGET_PERIOD_CHANGED` for safe
retry. Quantity is a strict positive BIGINT; isolated Decimal precision computes
the exact total, independently generated again by PostgreSQL.

Deterministic rejection codes are evaluated in stable order: `MERCHANT_NOT_ALLOWED`,
`HARD_PURCHASE_LIMIT_EXCEEDED`, `INSUFFICIENT_BUDGET`. All failing rejection reasons
are recorded. Otherwise at/below the automatic threshold returns `AUTO_AUTHORIZED`
with `AUTO_APPROVAL_THRESHOLD_MET`; above it returns `AWAITING_HUMAN_APPROVAL` with
`HUMAN_APPROVAL_REQUIRED`. The hard maximum is inclusive, and human approval cannot
override rejection. Database labels remain unchanged (`rejected`, `auto_authorized`,
`awaiting_approval`). These are initial decisions only, never payment status or
guaranteed current execution authority.

Each successful transaction inserts one immutable purchase with verified source
evidence, exact amounts, evaluated policy, initial decision/reasons and DB-computed
snapshot hash/expiry. The existing trigger writes `proposal_recorded`; the service
writes one `proposal_evaluated` agent audit, with evaluation version, context hash,
original run ID, budget period/observation, exact accounting/total and boolean rule
trace. Audits contain no raw URLs, provider blobs, descriptions, credentials or
model explanations. Context binding fits the existing append-only audit table,
so no schema change is necessary. No human approval, dispatch revalidation,
reservation, payment or payment operation is inserted. An active UTC period may
be initialized on demand. Concurrent affordable proposals may both be initially
eligible; future dispatch must independently refresh and atomically recheck/
reserve budget. No holds are invented for awaiting or rejected proposals.

The existing unique origin hash and owner lock prevent duplicate proposals. Matching
reference, owner/agent/run and quantity return the original committed proposal
before source/registry access, even after registry loss/restart, expiry or policy
changes. Different quantity conflicts; cross-context replay is rejected first.
Replays do not extend TTL, duplicate audit events, rewrite snapshots or restart
financial operations. Expired/stale-policy observation flags accompany the original
initial decision. Existing rows lacking the service's unique immutable context
audit cannot be adopted. Original run IDs can be restored from trusted audit data;
AI-supplied run claims are not a recovery mechanism. Serialization/deadlock/unique-
origin aborts roll back and check a committed winner; otherwise the caller receives
`TRANSACTION_CONFLICT` and retries the same request. Deferred failures cannot return
success. A timed-out/canceled caller must resolve the same reference/quantity before
assuming an in-progress DB worker failed. New references are distinct proposal
identities, not proof of safe retry for an uncertain PayPal payment.

After this proposal slice, human approval was implemented below. Dispatch must still compare independent
source retrieval against the durable purchase, enforce current policy/original TTL,
atomically reserve current-period capacity and prepare durable payment recovery
identity. No dispatch service, PayPal calls, agent or frontend were implemented here.
Catalog retailers still receive no demo payment: the sole eventual recipient is
AgentPal's PayPal Sandbox Business account, with no actual retailer fulfillment.
Channel3 price alias/types, condition/URL stability, shapes/encoding and data-use
permissions still require separately authorized live verification before judging.

Proposal verification on isolated PostgreSQL 18.6:
`rtk proxy .venv/bin/pytest -q --tb=line` reported **377 passed, 1 warning in
61.73 seconds**, preserving all 322 prior tests and adding 55 proposal cases.
Tests cover actual duplicate/policy/financial-lock concurrency, deferred commit
serialization/deadlock/storage failures with full rollback and safe retry, registry-
loss replay, changed-quantity/cross-context rejection, source changes/outages,
exact large totals, immutable snapshot/audit evidence, expiration, authoritative
accounting and replay without restarting an uncertain payment. Channel3 responses
are explicitly synthetic mocked fixtures; no external requests were made. The
existing Starlette/HTTPX TestClient deprecation warning remains nonblocking.
Offline wheel/source builds, lockfile validation, syntax and whitespace checks
passed. All 25 decisions remain verbatim; existing tests, auth/policy/database code,
migrations, dependencies and config placeholders were unchanged. Previous
uncommitted discovery work was preserved, with compatible extensions for stable
reference hashing and restoring immutable offer metadata. No credentials or
persistent databases were accessed; no commit/push occurred.

### Implemented secure human purchase approval workflow

`src/agentpal/approvals.py` adds owner-only `GET /purchases/approval-queue`,
`GET /purchases/{id}` and `PUT /purchases/{id}/approval`. Reads reuse `OwnerRead`;
writes reuse the existing exact-origin, session-bound CSRF and `OwnerWrite`
transaction/session recheck. Bodies are bounded to 2 KiB, reject unknown fields/
duplicate JSON keys and are parsed before financial locks. All responses receive
no-store/nosniff headers. No agent bearer/identity header confers owner authority;
owner credentials/HTTP clients must stay outside AI tools. Credential possession
still does not prove physical human presence after browser compromise.

The queue contains current-policy, unexpired, undispatched proposals initially
awaiting approval with no human decision. It is bounded/paginated (limit 1–100,
offset 0–10,000; stable creation/UUID ordering, has_more). Offset results can shift
as the queue changes. Expired or stale proposals remain readable by ID. Safe
details expose original product/merchant/condition/title, exact quantity and
decimal-string amounts, immutable policy context, UTC creation/source/expiry,
snapshot binding, human decision/time and current observed status. They omit raw
URLs/fingerprints, origin hashes/run IDs, owner-session IDs, provider references,
secrets and arbitrary audit/provider JSON. Source text remains untrusted display
data and must be escaped by a future dashboard.

Explicit `APPROVE`/`DENY` writes require the inspected 64-character lowercase
snapshot hash and strict integer version 1. The service compares with the stored
binding and inserts the backend hash/current verified session, never caller
identity or financial fields. Both new decisions require initially awaiting status,
current policy, original unexpired TTL and no dispatch. Approval additionally
blocks disallowed merchant, hard-limit/budget violations and known source
invalidation; denial can record refusal when those execution conditions block
approval. Both obey the authoritative existing snapshot/policy/session/expiry/
dispatch trigger. Rejected/auto-authorized, expired, stale-policy and dispatched
proposals cannot receive new human decisions.

The existing immutable `approvals` row and automatic owner-attributed
`human_decision_recorded` audit are the only durable records written. No purchase
snapshot/TTL, budget period, reservation or payment is created or rewritten by
these routes. Budget observations reuse PostgreSQL `budget_balances`; a missing
current period uses the active configured base without initializing the period.
An already attached reservation is not counted as a new affordability requirement
in the read projection. Consent does not reserve funds or guarantee execution.
Dispatch must independently refresh source and atomically recheck policy/TTL/
approval and budget, preserving all original reservation/recovery obligations.

Responses separate initial proposal decision, nullable human decision, observed
eligibility and actual payment status (`NOT_STARTED` if absent). Can-approve/
can-deny flags describe new human decisions only; reason codes expose known
blockers. `APPROVED_PENDING_DISPATCH_CHECKS` is unpaid consent, not completion;
execution_ready remains false. Recorded payment state is never inferred from
human approval. Replaying a dispatched/uncertain purchase changes no operation,
hold or recovery identity.

Writes serialize on owner/session and financial revision locks. Concurrent
identical requests write one decision/audit; opposite decisions conflict. Matching
snapshot/decision replays preserve the original session/time, even after policy
changes or proposal expiry, and do not grant fresh authority. An immutable denial
cannot be replaced with approval; reversal/withdrawal is outside this slice.
Success is reported only after commit. Serialization/deadlock errors return 409
transaction_conflict, uniqueness 409 decision_conflict, stale hashes 409
snapshot_conflict and trigger eligibility failures 409 approval_ineligible.
Errors expose no SQL or secrets; there is no automatic decision retry.

Forward Alembic revision `0003_approval_commit_guard.py` adds a deferred INSERT
constraint trigger reusing `validate_approval()` at commit. A real regression
reproduced that revision `0002` allowed an approval inserted while valid to commit
after the proposal expired. The added check also catches session expiry and
invalid snapshot/policy/dispatch bindings during the transaction. Existing decisions
and audits are preserved; revisions `0001`/`0002` are unchanged. Downgrade removes
the additional guard without rewriting rows. No persistent database was migrated;
the single old migration-head assertion now expects `0003`, retaining its other
history/accounting checks.

No external API, Vault onboarding, payment dispatch/recovery, agent orchestration
or frontend was added. All eventual payments still go to AgentPal's single PayPal
Sandbox Business account, not catalog retailers; no actual retailer order,
guaranteed stock or fulfillment is implied. See [README](../README.md#human-purchase-approval-and-owner-inspection)
for endpoint contracts and explicit owner usage.

Approval verification on isolated PostgreSQL 18.6:
`rtk proxy .venv/bin/pytest -q --tb=line` reported **433 passed, 1 warning in
86.83 seconds**, including all 377 prior cases and 56 new approval cases. Tests
cover authenticated queue/details, strict snapshot/CSRF/origin/session handling,
actual discovery-to-proposal-to-approval integration with mocked source data,
concurrent identical/opposite decisions, both policy-race orders, logout during
upload and after transaction admission, purchase/session expiry after insertion,
deferred serialization/deadlock/uniqueness aborts, safe retry, immutable audit/
session attribution, no new financial holds/payments, and forward migration
preservation. The initial expiry regression failed against `0002` and passes with
`0003`. One slow-upload test needed independent TestClient portals to avoid
blocking its own event loop; no application workaround was added. The existing
Starlette/HTTPX TestClient deprecation warning remains nonblocking. Offline source/
wheel builds, lockfile, syntax and whitespace checks passed. All 25 decisions and
old migrations remain unchanged; the sole prior test edit updates the expected
head revision from `0002` to `0003`. No real credential files, live APIs or persistent
databases were accessed; no commit or push occurred.

## 8. Channel3 feasibility — completed search and retrieval spike

Evidence is the controlled experiment using `scripts/spike_channel3.py`, as reported by the project owner. The context update inspected the script without running it or contacting Channel3.

| Test | Observed result |
|---|---|
| Search, `POST /v1/search` | HTTP 200; **three products and four offers** |
| Search fields | Product IDs, titles, merchant domains, prices, currency, and availability were present |
| Product identity | Example observed product ID: `PO7Dccd`; the same product appeared under more than one merchant offer |
| Retrieval, `GET /v1/products/PO7Dccd` | HTTP 200; returned product ID matched search; retrieved product contained the observed merchant offers and price data |

The script's CLI `lookup` command retrieves by product ID; it does not call the separate `POST /v1/lookup` URL API. Its local `observed_at_utc` is request-time metadata, not a provider freshness guarantee.

### Observed limitations and compliance questions

- No independent stable offer ID was observed.
- Merchant domain is not verified legal merchant identity.
- No PayPal merchant/payee identity was returned.
- No guaranteed real-time retailer checkout quote was established.
- No guaranteed freshness timestamp was observed.
- Shipping, taxes, final retailer checkout price, stock certainty, and retailer fulfillment were not verified.
- Looking up the product again through Channel3 is independent of AI-generated text, but does not guarantee the retailer's live checkout price.
- Channel3 terms, API access, caching rules, public redistribution restrictions, and use of benchmark/evaluation results require compliance review.

**Verified:** Channel3 product discovery and product-detail retrieval work with the configured API.

**Not verified:** Offer-specific stable IDs, retailer checkout prices, multi-merchant PayPal payouts, or real retailer fulfillment.

Channel3 is suitable for discovery and backend product-data refresh within these limits. The following implementation addresses conservative selected-offer mapping and parsing; its live compatibility is not established merely by the earlier successful spike. The simulation disclosure in Section 2 remains mandatory for future demonstrations.

### Implemented discovery and transient-selection foundation

`src/agentpal/channel3.py` implements asynchronous fixed-origin search and product
retrieval, based on the existing spike's `POST /v1/search` (`products` array) and
`GET /v1/products/{id}` (root product) mappings and `x-api-key`. Settings read only
explicit exported `CHANNEL3_API_KEY`, with no dotenv access. There are no new
HTTP routes, migrations or dependencies. Requests have no retries/redirects/proxies,
a 45-second total deadline, connect/I/O bounds, 2 MB response cap, at most five
products and 1,000 offers per product. Identity encoding is requested and compressed
responses rejected to bound decompression. Bodies, headers and exception details
never become public errors; only fixed error codes leave the adapter.

Required product fields are `id`, bounded untrusted `title`, and `offers`.
Each supported offer has a canonical ASCII domain, HTTPS URL fingerprint, optional
bounded/null condition and valid exact USD price. Prices decode JSON directly
to Decimal, never through float, and reject fractional cents/invalid/nonfinite/
negative values. `price.price` and the spike-noted `price.amount` compatibility
branch are isolated; both must agree if present. Amounts have a computational
30-integer-digit bound, not a financial policy default. `compare_at_price` is
ignored. Only exact `InStock` is available; `OutOfStock` is unavailable, all other
or missing availability unknown. Unavailable/unknown listings can remain visible
with references, but cannot pass refresh for execution.

All offers are scanned before limiting display to 50 candidates. Same canonical
domain duplicates are ambiguous even when a duplicate has invalid price or is
out of stock. Missing/unusable domains reject the entire product, since a hidden
duplicate cannot be excluded. Duplicate product IDs reject those search products.
Other malformed offers yield fixed rejection reasons; valid prohibited merchants
remain available for actual future policy evaluation. No allowlist filter or fake
rejection exists. Raw URL/description/provider blobs are not tool output. Source
text is untrusted, and no arbitrary retailer URL is requested.

`src/agentpal/discovery.py` implements frozen trusted `RunContext`, `OfferRegistry`,
`SelectionEvidence` and `DiscoveryService.search/resolve/refresh`. Trusted callers
must supply authenticated owner/verified agent identity and backend-created run
IDs, never deserialize these from AI arguments. No runtime authentication or
PydanticAI integration is added by this internal Python boundary. References are
32 random bytes in opaque 43-character URL-safe encoding, stored only as
purpose-separated SHA-256 hashes bound to owner/agent/run and frozen source evidence.
Default bounds are 500 global/100 per run with 600-second absolute monotonic TTL;
lock-protected FIFO eviction, run disposal, restart loss and expired/forged/cross-
context references fail safely. Reads do not extend TTL. Run one worker; no
discovery-only persistence or distributed registry exists.

`refresh()` independently retrieves the selected product and compares only its
original merchant, URL fingerprint, nullable condition, exact USD amount and
recognized availability. Both increases and decreases block the old selection.
Changed attribution URLs fail conservatively; title changes alone are not
identity. Results match existing revalidation vocabulary: passed, identity_changed,
price_changed, unavailable, ambiguous, unsupported, missing, source_error. A failed
refresh supplies no fallback evidence. Expiry/eviction during I/O is rechecked.
The original selection is never rewritten, and no merchant is substituted.

These observations/ref hashes align with existing immutable purchase fields but
grant no spending authority. The proposal service now independently refreshes,
evaluates policy/quantity/budget, persists snapshot/origin evidence and enforces
durable uniqueness. Future dispatch must compare a new retrieval with that durable
snapshot, independently of transient-registry survival; dispatch remains unimplemented.

Verification uses explicitly synthetic mocked HTTPX fixtures, never real retailer
data or live calls. The earlier spike does not establish exact price alias/type,
null condition behavior, URL attribution stability, response-size/encoding behavior
or provider retention/display permissions. Those remain authorized integration
checks. Listing price is the approved Sandbox demo basis; source stock and local
observation timestamps are not retailer guarantees. Catalog retailers still receive
no PayPal Sandbox demo payment and no actual goods are ordered or fulfilled.

See [README](../README.md#channel3-discovery-and-transient-offer-selection) for the
call contracts, safe tool output, registry lifecycle, limitations and usage example.

Verification on isolated PostgreSQL 18.6: the complete
`rtk proxy .venv/bin/pytest -q --tb=line` run reported **322 passed, 1 warning in
51.53 seconds**, including all 194 prior cases and 128 new discovery/reference
cases. The existing Starlette/HTTPX TestClient deprecation warning remains.
Offline source/wheel builds, locked dependency validation, Python syntax and
whitespace checks passed. All 25 decisions remain verbatim; existing code/tests,
dependencies, migrations and configuration placeholders were unchanged. No real
credential files, persistent databases or external APIs were accessed. No commit
or push occurred.

## 9. PayPal Sandbox feasibility and credential hygiene

### Completed experiments — CLI spike, not application integration

| Test | Observed result |
|---|---|
| Sandbox OAuth | **PASSED:** `POST /v1/oauth2/token` returned HTTP 200 |
| Vault setup | **PASSED:** `POST /v3/vault/setup-tokens` returned 201; payer authorization required |
| Payer authorization | Separate Personal Sandbox payer approved the saved method; the Business merchant account could not approve itself |
| Vault confirmation | **PASSED:** `POST /v3/vault/payment-tokens` returned 201 and created payment-method/customer references |
| Merchant-initiated zero-click payment | **PASSED:** `POST /v2/checkout/orders` returned 201, order and capture `COMPLETED`, **$12.00 USD**, no purchase-time redirect |
| PayPal request idempotency | **PASSED:** same request ID returned first 201, then 200, with the same order and capture for **$5.00 USD** |

This proves feasibility in the tested Sandbox configuration. It does not establish production permissions, universal zero-click success, application-level duplicate/concurrency safety, or an endpoint's precise idempotency retention window. Do not repeat charges just to re-prove the spike.

### Exposed client secret — rotation completed

The exposed Sandbox client secret was successfully rotated, the old secret revoked/deleted, and OAuth retested successfully **after revocation**: HTTP 200, `Token OK`, reported `expires_in=32400s`. The existing REST application and Client ID were retained; the rotation did not recreate the vaulted payment method. Dashboard renumbering of the surviving secret label did not indicate another secret change.

No actual secret or access token belongs in this document. Historical setup/payment-token and customer references have been replaced with placeholders in Appendix A; existing test references are not hardcoded application configuration or fresh authorization. A misleading old script comment saying calls were not yet tested, and its suggestion of redirect fallback, do not supersede these completed results or Decision 15.

## 10. AG Grid / AG Studio experiments — outcomes and limits

The experiment ran **outside AgentPal** in `/home/mahmoud/MY_SDD/tests/hackathon-paypal-ag-grid-boilerplate`, cloned from `paypaldev/hackathon-paypal-ag-grid-boilerplate`.

| Experiment | Status / evidence |
|---|---|
| Installation and rendering | **PASSED:** isolated boilerplate installed and displayed AG Studio |
| Application-provided demo data | **PASSED:** three explicitly simulated transaction rows were ingested |
| Grid, sorting and aggregation | **PASSED:** Ledger rendered and summed; the temporary accounting convention was misleading |
| Edit Mode / chart creation | **PASSED:** grouped column chart showed one each of Success, Pending, Denied |
| Saving/restoring edited report | **PASSED:** saved chart survived browser refresh |
| AI Assistant | **ATTEMPTED, NOT CONFIRMED:** `api_error: undefined`; cause not conclusively established |
| Production/judging license suitability | **UNRESOLVED:** trial behavior does not prove deployment/judging rights |

Additional historical evidence:

- Tested versions: Next.js 16.3.6, React 19.2.8, AG Grid 36.2.0, AG Studio 3.0.0; Node v22.23.1 / npm 10.9.8. These are spike versions, not adopted AgentPal frontend dependencies.
- After npm timeouts, a restricted-socket/extended-timeout retry installed 382 packages. The isolated spike reported eight high-severity audit findings, unresolved there.
- Sandbox Invoicing and Reporting requests returned `403 NOT_AUTHORIZED`. Those unrelated entitlements are not MVP dependencies; application analytics should use AgentPal's own records.
- Boilerplate-only catch-and-return-empty fallbacks must not become silent “zero revenue/zero balance” claims in AgentPal.
- The three simulated rows produced a misleading `-870.00` aggregate by including pending/denied amounts as spending. AgentPal must separate requested amounts, reservations and actual captured spending.
- The AI proxy's `POST /api/ai/responses 200` did not prove a successful model response or tool call. API credits were a possible issue, not an established cause.
- Verify both **AG Grid edition/feature licensing** and **AG Studio production/judging licensing**. An AG Grid-only interface is a candidate fallback subject to applicable licensing, not an assumed license exemption. Optional Studio AI integration remains OPEN.

These experiments are not integrated AgentPal features. The old “click Edit” and “try saving a report” next steps are complete and superseded.

## 11. Official PayPal AI Hackathon requirements

Source: user-supplied **PayPal AI Hackathon (Devpost) Official Rules**, version supplied 2026-10-20. This documentation task made no network requests or independent rules recheck; check official amendments before submission.

- **Submission deadline:** **November 12, 2026, at 12:00 PM Pacific Time**. A November 10 internal target is only a buffer.
- A **functional application** must centrally use the PayPal developer platform in Sandbox and an **AI tool**.
- Public source repository with an **open-source license**, necessary code/assets, and clear setup/execution instructions.
- A working demo accessible to judges, **hosted or reproducibly runnable**, with safe instructions for required test access. Static mockups alone are insufficient.
- A **public YouTube demonstration video under three minutes**.
- **English submission materials**, including description, instructions and demo.
- Keep judging access available through the judging period ending **December 15, 2026**. Supplied judging window: December 1 at 2:00 PM PT through December 15 at 8:00 AM PT.
- Judging includes five **equally weighted** criteria: technological implementation, design, potential impact, innovation/idea, and presentation; the supplied rules also describe a viability/theme pass-fail stage.
- Respect third-party SDK, asset and code licenses; public setup must not expose secrets. Award eligibility restrictions still apply.

Optional sponsor prizes and sponsor-specific eligibility are separate from overall submission requirements. Prior supplied categories included Best Use of PayPal + AI, Agentic Commerce, and AG Grid; AG Studio is **not** a mandatory overall requirement. Sponsor categories and trials do not establish a software license grant. Verify AG Grid/AG Studio licensing rather than assuming hackathon use is automatically covered.

Historical schedule context: submissions opened October 1, 2026, at 9:15 AM PT; winners were expected around December 21 at 2:00 PM PT. These dates are supplied context, not newly verified observations.

## 12. Current progress and MVP scope

| Area | Current status |
|---|---|
| All 25 architectural decisions | **APPROVED:** Section 3 |
| PayPal OAuth / Vault / zero-click / repeated request ID | **PASSED, separate spike:** Section 9 |
| PayPal secret rotation / revocation / OAuth retest | **COMPLETED:** Section 9 |
| Channel3 discovery and product-detail retrieval | **PASSED, separate spike:** Section 8 |
| Channel3 safe selected-offer mapping and usable-price policy | **IMPLEMENTED offline:** bounded adapter, exact prices, conservative comparison and transient references; live compatibility/permissions still unverified |
| AG Studio install, custom data, grid, Edit Mode, charts, persistence | **PASSED, separate spike:** Section 10 |
| AG Studio AI Assistant / judging license | **UNCONFIRMED / UNRESOLVED** |
| FastAPI app metadata and `GET /health` | **IMPLEMENTED; prior import/TestClient verification passed** |
| PostgreSQL persistence / migrations / invariant tests | **IMPLEMENTED:** native SQL/Alembic, disposable PostgreSQL integration/concurrency tests; no persistent database provisioned |
| Single-owner authentication | **IMPLEMENTED:** secure bootstrap, Argon2id login, durable sessions/revocation, CSRF and reusable owner dependencies |
| Vault onboarding in app | **NOT IMPLEMENTED** |
| Owner policy configuration/history and current-month budget API | **IMPLEMENTED:** authenticated, CSRF-protected writes, immutable versions and atomic immediate limits under Decision 25 |
| Purchase proposals and initial deterministic policy evaluation | **IMPLEMENTED:** independent refresh, immutable decisions/rule traces, exact totals and context-bound durable idempotency; no HTTP routes |
| Human purchase approval and safe inspection | **IMPLEMENTED:** owner queue/details, explicit snapshot-bound approval/denial, immutable audit, idempotency and commit-time expiry/session guard |
| Reservation/payment dispatch services | **NOT IMPLEMENTED**; persistence and final financial integrity guards are implemented |
| Database idempotency, accounting and retry guards | **IMPLEMENTED**; HTTP/provider orchestration and reconciliation services are **NOT IMPLEMENTED** |
| Live PydanticAI / Channel3 / PayPal application integration | Reusable Channel3 adapter implemented but not called live in this task; agent/HTTP orchestration and PayPal integration **NOT IMPLEMENTED** |
| Integrated frontend / end-to-end AgentPal execution / judge-ready demo | **NOT COMPLETED** |
| PayPal webhooks | **EXCLUDED from MVP**, not a pending integration milestone |

The required vertical slice is external discovery → safe selected offer → immutable proposal → deterministic policy/owner approval → shared-budget reservation → vaulted Sandbox payment → accurate persisted result/audit and useful UI. It includes the simulation disclosure, secure judging access, README, public open-source repository and concise English demo.

Outside MVP: organizations/complex RBAC, multi-currency, multi-merchant payouts, real retailer checkout/fulfillment, intentional partial/multiple captures, refund workflow, interactive purchase fallback, PayPal webhooks, unrelated Invoicing/Reporting APIs, production live payments, and elaborate distributed infrastructure. Optional AG Studio/AI analytics must not block the functional core.

## 13. Remaining integration and owner-configuration questions

These are unresolved integration/configuration choices; approved requirements in Section 3 must not be reopened as if undecided. The implemented schema is described in Section 7. No financial configuration defaults have been invented.

| OPEN question | What must be resolved |
|---|---|
| Selected-offer adapter evidence | Conservative product/domain/URL/condition matching is implemented with synthetic fixtures; verify actual attribution stability, null condition behavior, price aliases and supported shapes before live integration |
| Source data retention | Necessary purchase evidence is implemented; Channel3 retention/display permissions still require review before ingestion |
| Source adapter integration | Parsing/retrieval/comparison and independent proposal refresh implemented offline; live adapter verification, runtime identity integration and pre-dispatch refresh remain pending |
| Channel3 access and data-use restrictions | API availability for the demo/judging, terms, caching, public redistribution, benchmark/evaluation use |
| Owner deployment and recovery | Authentication and configurable lifetime are implemented; select deployment settings/safe judge access and define owner recovery separately; no public registration |
| Vault onboarding persistence | Setup/callback correlation and validation, verified completion, retries/cancellation, secure payment-method storage |
| Owner-selected financial values | The configuration API and validation are implemented; the owner must supply actual deployment budget/threshold/hard-limit values, with no seeded defaults |
| Proposal TTL | The owner supplies a valid duration through the API; one TTL begins at proposal creation and existing snapshots cannot be extended |
| Transactional services | Idempotent proposals and human decisions, owner/session locks and safe conflict handling implemented; dispatch and verified reconciliation remain pending |
| Monthly configuration actions | RESOLVED by Decision 25 and the API: immediate atomic current-month limits; reservations bind at reservation time and never move |
| Policy/pricing/dispatch race | Policy activation versus database dispatch claims is serialized; independent source refresh and actual HTTP dispatch/recovery still require service implementation |
| PayPal idempotency | Endpoint-specific documented retention, replay eligibility and exact payload persistence; safe reconciliation/manual review when replay is unavailable, including no known order ID |
| Payment state and evidence | Final transitions, reconciliation procedure, full-capture verification and unexpected partial/extra capture accounting |
| Financial edge behavior | Exact cent storage and schema/migrations are implemented. Decide zero-value provider execution behavior; preserve unexpected higher-precision provider facts for review |
| AG Grid / AG Studio | Applicable feature/edition and judging/deployment licenses; optional Studio AI feasibility and unresolved assistant error |

## 14. Roadmap after persistence, authentication, policy and discovery foundations

Context consolidation, all 25 decisions, PostgreSQL persistence/hardening, owner authentication/configuration, the offline-tested Channel3 foundation, idempotent proposals and secure human approval/read APIs are complete. Do not repeat those implementations, completed feasibility spikes or blank schema/adapter planning. Review existing guards/dependencies and their integration limitations before the next bounded task.

1. Verify real Channel3 adapter compatibility and data-use permissions in a separately authorized bounded check; offline fixtures do not establish live compatibility.
2. Implement Vault onboarding, including callback validation and external encryption-key provisioning.
3. Build pre-dispatch source/financial gates and durable payment/reconciliation services around existing database guards. Verify endpoint replay retention and exact payload recovery before any authorized payment test.
4. Integrate PydanticAI and the frontend; verify end-to-end execution and prepare the public licensed source, setup/judging access and concise English demonstration.

The implemented foundations include reusable source retrieval, durable initial proposals and owner purchase-read/human-approval endpoints, but no discovery HTTP/AI runtime, checkout, payment execution, frontend or end-to-end purchasing system.

---

# Appendix A — Historical PayPal & AG Studio Test Log (archived)

This appendix preserves earlier observations, historical command examples, and temporary code for audit/debugging. **It is historical evidence only, not a current progress report, implementation plan, or authorization to execute commands.** Section 3 overrides earlier scope assumptions; Sections 8–12 record the latest experiment and implementation status. Secret rotation, later AG Studio chart/save success and the unsuccessful AI attempt supersede earlier observations. Sensitive setup/payment-token and customer references are replaced with placeholders; retained order/capture IDs are historical evidence only. Temporary prices, users, datasets, fallback code and tooling choices below are not approved application defaults.

## A.1. PayPal Sandbox Setup

The original project directory is:

```text
agentpal/
```

The PayPal test script is:

```text
scripts/spike_paypal.py
```

The script was originally provided by Claude to test whether merchant-initiated zero-click payments were actually possible with our PayPal Sandbox account.

### Sandbox Accounts

Two separate PayPal Sandbox accounts were used:

- Business Sandbox: Merchant account associated with the REST App and API Credentials.
- Personal Sandbox: Customer account named John Doe.

An initial approval attempt failed with:

```text
This PayPal account is associated with the merchant
you're trying to pay.
```

This happened because the wrong PayPal account was being used for the approval flow.

Switching to the separate Sandbox Personal account resolved it.

## A.2. OAuth Authentication — PASSED

Command:

```bash
uv run scripts/spike_paypal.py vault-setup
```

OAuth endpoint:

```http
POST https://api-m.sandbox.paypal.com/v1/oauth2/token
```

Result:

```text
HTTP 200 OK
```

The application successfully retrieved a Sandbox OAuth access token.

The token was masked in terminal output.

## A.3. Vault Setup — PASSED

Endpoint:

```http
POST /v3/vault/setup-tokens
```

Result:

```text
HTTP 201 Created
STATUS: PAYER_ACTION_REQUIRED
```

Setup Token:

```text
<HISTORICAL_SETUP_TOKEN_ID>
```

PayPal returned an approval URL.

We opened it using the separate Sandbox Personal account.

After clicking the approval button, PayPal redirected to:

```text
http://localhost:8000/paypal/return
```

Chrome displayed:

```text
ERR_CONNECTION_REFUSED
```

This happened because there was no local server listening on port 8000.

However, the approval itself had succeeded, as confirmed by the next API request.

The unavailable return page did not prevent completion of the CLI-based Vault test.

## A.4. Payment Token Creation — PASSED

Command:

```bash
uv run scripts/spike_paypal.py vault-confirm <HISTORICAL_SETUP_TOKEN_ID>
```

Endpoint:

```http
POST /v3/vault/payment-tokens
```

Result:

```text
HTTP 201 Created
```

Created identifiers:

```text
PAYMENT_TOKEN_ID: <HISTORICAL_PAYMENT_TOKEN_ID>
CUSTOMER_ID:      <HISTORICAL_CUSTOMER_ID>
```

This confirmed that the customer's PayPal account was successfully vaulted in Sandbox.

Actual Vault/customer identifiers have been omitted from this public-facing history. Application payment-method references must be stored securely server-side and associated with the single authorized owner.

## A.5. Zero-Click Payment — PASSED

Command:

```bash
uv run scripts/spike_paypal.py vault-charge <HISTORICAL_PAYMENT_TOKEN_ID> --amount 12.00
```

Endpoint:

```http
POST https://api-m.sandbox.paypal.com/v2/checkout/orders
```

Result:

```text
HTTP 201 Created

ORDER_ID: 05V17948E8513403H
ORDER_STATUS: COMPLETED

CAPTURE_ID: 7R897397G7212142V
CAPTURE_STATUS: COMPLETED

AMOUNT: 12.00 USD
```

Stored credential details:

```json
{
  "payment_initiator": "MERCHANT",
  "usage": "SUBSEQUENT",
  "usage_pattern": "UNSCHEDULED_POSTPAID"
}
```

The payment completed without an additional customer redirect or approval screen.

Script output:

```text
RESULT: Mode 1 works.
Zero-click vaulted payment completed with no redirect.
```

### Historical feasibility conclusion — fallback superseded

**PayPal Mode 1 (Vaulted Zero-Click Payments) was confirmed feasible in the tested Sandbox environment.**

The earlier optional redirect-fallback idea is superseded by Decision 15: the MVP stops for manual review on purchase-time payer action. Vault onboarding authorization remains a separate approved flow.

Important: This does not prove that the production account has the same permission or that every future merchant-initiated payment is guaranteed to complete.

## A.6. PayPal Idempotency Test — PASSED

Command:

```bash
uv run scripts/spike_paypal.py idempotency-test <HISTORICAL_PAYMENT_TOKEN_ID>
```

The script:

1. Generated one UUID for `PayPal-Request-Id`.
2. Sent a $5.00 vaulted payment.
3. Repeated the same request using exactly the same ID.
4. Compared the returned Order IDs.

Results:

```text
FIRST REQUEST:
HTTP 201 Created

RETRY:
HTTP 200 OK

ORDER_ID (both):
8L980057LP2083344

CAPTURE_ID (both):
0R045126CT9057317

AMOUNT:
5.00 USD

ORDER STATUS:
COMPLETED

CAPTURE STATUS:
COMPLETED
```

Final output:

```text
RESULT: PASS (same order returned)
```

### What This Proves

Both requests returned the same completed order and same capture in the tested Sandbox scenario.

### What This Does Not Prove

We still need our own PostgreSQL idempotency records and unique constraints, plus tests for:

- Concurrent requests.
- Timeouts.
- Lost responses.
- Process crashes.
- Budget reservation races.
- Replay after PayPal's idempotency retention window.

Do not rerun the `vault-charge` command with a fresh request ID unnecessarily: it can create another Sandbox payment.

---

**Historical experiment B — AG Studio spike**

## A.7. Why We're Testing AG Studio

The earlier experiment explored AG Studio as a major analytics component. Current scope keeps AG Studio optional pending licensing/integration review (Section 10).

We want to use:

- AG Grid for operational purchase monitoring.
- AG Studio for analytics and reporting.
- The AG Studio AI integration so users can ask questions about purchase decisions and policy results.

Example future question:

> Which spending policy blocked the most purchases this week?

We must verify feasibility before integrating AG Studio into AgentPal.

## A.8. Official Boilerplate Clone — COMPLETED

We cloned the PayPal + AG Grid + AG Studio boilerplate into a completely separate test directory.

Commands:

```bash
cd ~/MY_SDD/tests

git clone https://github.com/paypaldev/hackathon-paypal-ag-grid-boilerplate.git

cd hackathon-paypal-ag-grid-boilerplate
```

Working directory:

```text
~/MY_SDD/tests/hackathon-paypal-ag-grid-boilerplate
```

**Important:** This is separate from the actual AgentPal repository.

### Installed Technologies

The boilerplate's `package.json` includes:

| Technology | Version |
|---|---|
| Next.js | 16.3.6 |
| React | 19.2.8 |
| AG Grid | 36.2.0 |
| AG Studio | 3.0.0 |
| TypeScript | 5.x |

Environment:

```text
Node.js: v22.23.1
npm:     10.9.8
OS:      Fedora Linux
Shell:   Zsh
```

## A.9. npm Installation Problems — RESOLVED

The initial installation:

```bash
npm ci
```

failed with:

```text
ETIMEDOUT
```

A retry with extended timeouts then failed with:

```text
EIDLETIMEOUT
Idle timeout reached for host registry.npmjs.org:443
```

The npm Registry itself responded normally:

```text
npm PONG 551ms
```

The download logs showed slow large packages such as Next.js and Sharp binaries.

The successful installation command was:

```bash
npm ci \
  --maxsockets=3 \
  --fetch-retries=5 \
  --fetch-retry-mintimeout=10000 \
  --fetch-retry-maxtimeout=120000 \
  --fetch-timeout=300000
```

Result:

```text
added 382 packages, and audited 383 packages in 13s
```

npm reported eight high-severity audit findings.

We did not run `npm audit fix`, because the objective was a controlled feasibility test without dependency changes.

Those findings still need reviewing before deployment.

## A.10. Boilerplate Startup — PASSED

Command:

```bash
npm run dev
```

The Next.js application launched successfully.

Homepage:

```text
http://localhost:3000
```

Dashboard:

```text
http://localhost:3000/dashboard
```

The homepage showed:

- Dashboard.
- Catalog Products.
- Billing Plans.
- Invoices.
- Transactions.
- Balances.

Clicking Dashboard initially produced an error because PayPal credentials were missing.

## A.11. PayPal Credentials for the Boilerplate

The `env.example` file contains:

```dotenv
PAYPAL_CLIENT_ID=
PAYPAL_SECRET=

AG_STUDIO=

AG_STUDIO_OPENAI_API_KEY=

OPENAI_MODELS=gpt-5.6-terra,gpt-5.6-luna,gpt-5.6-sol
```

A `.env.local` file was created in the boilerplate directory.

We used the same working PayPal Sandbox Business REST App credentials from AgentPal.

Variable mapping:

```text
AgentPal:
PAYPAL_CLIENT_ID
PAYPAL_CLIENT_SECRET

Boilerplate:
PAYPAL_CLIENT_ID
PAYPAL_SECRET
```

No secrets should be pasted into chat or committed.

`AG_STUDIO` and `AG_STUDIO_OPENAI_API_KEY` were left unconfigured during the initial tests.

## A.12. PayPal API 403 Errors

After configuring credentials, the dashboard failed because the boilerplate calls several PayPal APIs that our Sandbox REST App cannot currently access.

The errors were:

```text
GET /v2/invoicing/invoices
403 NOT_AUTHORIZED
```

```text
GET /v1/reporting/transactions
403 NOT_AUTHORIZED
```

```text
GET /v1/reporting/balances
403 NOT_AUTHORIZED
```

The errors contained:

```text
Authorization failed due to insufficient permissions.
```

These permissions are separate from the working Vault and Orders v2 APIs.

We decided **not to spend time obtaining Invoicing or Reporting permissions**, since AgentPal's analytics should primarily come from its own PostgreSQL database.

## A.13. Temporary Dashboard Data-Loading Workarounds

File modified:

```text
lib/dashboard-data.ts
```

Original code:

```typescript
const [products, plans, invoices, transactions, balances] =
  await Promise.all([
    listProducts(),
    listPlans(),
    listInvoices(),
    searchTransactions(
      new Date(asOf.getTime() - TRANSACTION_WINDOW_MS),
      asOf
    ),
    getBalances(),
  ]);
```

Because `Promise.all()` fails if any request rejects, one PayPal `403` prevented the entire dashboard from loading.

We changed three calls inside the separate boilerplate.

### Invoices Fallback

```typescript
listInvoices().catch((error) => {
  console.warn("Invoicing unavailable during AG Studio spike:", error);
  return [];
}),
```

### Transactions Fallback

```typescript
searchTransactions(
  new Date(asOf.getTime() - TRANSACTION_WINDOW_MS),
  asOf
).catch((error) => {
  console.warn("Transactions unavailable during AG Studio spike:", error);
  return [];
}),
```

### Balances Fallback

```typescript
getBalances().catch((error) => {
  console.warn("Balances unavailable during AG Studio spike:", error);
  return { balances: [] };
}),
```

These are **temporary experiment-only workarounds**.

Do not blindly copy them into production AgentPal, because production code needs to distinguish unavailable data from truly empty datasets.

After these changes, AG Studio loaded successfully but displayed:

```text
No PayPal data yet
```

That was expected because the returned datasets were empty.

## A.14. AG Studio Data Integration Analysis

We inspected:

```text
app/dashboard/studio/data.ts
```

Important functions:

```typescript
export const hasData = (data: DashboardData) =>
  [
    data.invoices,
    data.transactions,
    data.products,
    data.plans,
    data.balances,
  ].some((rows) => rows.length > 0);
```

```typescript
export function buildStudioData(
  data: DashboardData
): AgDataSourcesDefinition
```

AG Studio supports a structured definition containing:

- Sources.
- Fields.
- Relationships.
- Calendars.
- Calculated measures.

This means the dashboard can consume application-generated data rather than relying on PayPal's reporting APIs.

We also inspected:

```text
app/dashboard/page.tsx
```

Its original flow is:

```typescript
await connection();
const data = await getDashboardData();

return (
  <Dashboard
    data={data}
    licenseKey={process.env.AG_STUDIO}
    aiModels={aiEnabled() ? aiModels() : undefined}
  />
);
```

So we decided to inject a small temporary dataset immediately after `getDashboardData()` for the feasibility test.

## A.15. Temporary Demo Dataset — ADDED SUCCESSFULLY

File modified:

```text
app/dashboard/page.tsx
```

We inserted:

```typescript
const data = await getDashboardData();

// Temporary AG Studio feasibility test data.
data.transactions = [
  {
    id: "demo-001",
    date: "2026-10-19T10:00:00Z",
    eventType: "Purchase",
    status: "Success",
    direction: "Debit",
    currency: "USD",
    amount: -120,
    fee: 0,
    net: -120,
    endingBalance: null,
    counterparty: "Office Supplies",
    note: "Auto-approved purchase",
  },
  {
    id: "demo-002",
    date: "2026-10-20T11:00:00Z",
    eventType: "Purchase",
    status: "Pending",
    direction: "Debit",
    currency: "USD",
    amount: -250,
    fee: 0,
    net: -250,
    endingBalance: null,
    counterparty: "Tech Store",
    note: "Awaiting approval",
  },
  {
    id: "demo-003",
    date: "2026-10-21T12:00:00Z",
    eventType: "Purchase",
    status: "Denied",
    direction: "Debit",
    currency: "USD",
    amount: -500,
    fee: 0,
    net: -500,
    endingBalance: null,
    counterparty: "Restricted Merchant",
    note: "Blocked by spending policy",
  },
];
```

These are not real PayPal payments. They are temporary records demonstrating AG Studio data ingestion.

Important domain note: The temporary dataset represents pending and denied requests using negative amounts, causing a misleading financial total. In production AgentPal, separate requested, reserved, and captured amounts so rejected transactions are not counted as completed spending.

## A.16. AG Studio Data Rendering — PASSED

After refreshing:

```text
http://localhost:3000/dashboard
```

AG Studio rendered its normal reports.

The Revenue page still showed empty invoice widgets because we added transaction records only.

We switched to:

```text
Cash & payouts
```

The **Ledger** widget displayed all three transactions correctly.

Observed data:

| Date | Merchant | Status | Amount |
|---|---|---|---:|
| Oct 19 | Office Supplies | Success | -$120 |
| Oct 20 | Tech Store | Pending | -$250 |
| Oct 21 | Restricted Merchant | Denied | -$500 |

The Ledger displayed a total of:

```text
-870.00
```

### Confirmed Working

- AG Studio UI rendering.
- Custom data ingestion.
- Ledger display.
- Column rendering.
- Dates and field formatting.
- Sorting.
- Sum aggregation.
- Integration between application data and AG Studio's Grid widget.

### Historical status reconciled with later evidence

- Creating charts in Edit Mode and saving/restoring edited reports **subsequently passed** (Section 10).
- AI Assistant was subsequently attempted and returned `api_error: undefined`; cause remains unconfirmed. Agent/LLM tool integration was not proven by that attempt.
- Production/judging license suitability remains **OPEN**.
- Resolution of the separate boilerplate's Next.js development overlay reporting nine issues was not established.

Also, the Payouts widgets were empty because the added rows were transactions, not payouts.

---


---

**End of handoff.**
