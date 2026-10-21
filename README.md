# AgentPal

AgentPal is a policy-controlled AI procurement/payment simulation for one owner.
Channel3 discovers retailer listings; PayPal Sandbox payments go to AgentPal's
single demo Business account. The application does not order or fulfill goods
from those retailers. The FastAPI health endpoint and PostgreSQL persistence
layer are implemented; the purchasing application is not yet integrated.

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
  not silently rewrite period history. Whether an owner configuration action
  also requests such an adjustment remains a future product/API decision.
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
encrypted. A trusted backend must validate sessions/CSRF, use a real Argon2id
implementation, independently obtain source/provider evidence, encrypt sensitive
references and the exact replayable request bytes, and sanitize observation/audit
JSON. Never expose stored Vault/session material or arbitrary JSON through an API.
Onboarding callback handling, encryption/key provisioning, password recovery,
session lifetimes, source URL/condition matching fixtures, Channel3 retention
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
