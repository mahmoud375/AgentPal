# AgentPal

AgentPal is a policy-controlled AI procurement/payment simulation for one owner.
Channel3 discovers retailer listings; PayPal Sandbox payments go to AgentPal's
single demo Business account. The application does not order or fulfill goods
from those retailers. The FastAPI health endpoint, PostgreSQL persistence and
single-owner authentication and owner policy/budget APIs are implemented;
purchasing is not yet integrated.

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
existing schema at revision `0002`; this task adds no migration.

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
capture accounting and limit updates. SQLAlchemy Core
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
runtime implementation is a later task. Once submitted, the reference hash and
necessary selected-offer evidence become durable with the purchase. A unique
origin hash prevents competing inserts for the same selection. Future services
must return the existing proposal on a matching retry, reject changed quantity,
and never use a new selection to recover an uncertain payment.

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
  the user-facing policy evaluation service is not implemented here.
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
