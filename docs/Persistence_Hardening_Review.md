# PostgreSQL persistence targeted review

Reviewed 2026-10-21 against the 24 approved MVP decisions. Scope is persistence;
no authentication, API, source integration or HTTP payment execution was added.

## Findings and root causes

| Issue | Verdict | Revision 0001 behavior |
|---|---|---|
| A: payment transitions | Confirmed | `guard_payment` checked some destination evidence but had no transition graph. Completed/failed payments could return to preparation/execution; payer-action stop could be bypassed by changing the parent status. Unsent operations could claim HTTP outcomes, and a separate capture POST could overlap an uncertain create POST. |
| B: capture and held balance | Partially confirmed | Captures immediately contributed to spent while every held reservation contributed its full amount. A crash after committing a normal full capture could indefinitely double count that obligation until later settlement. The same treatment for unexpected partial/extra/foreign results was intentionally conservative. |
| C: limit reductions | Confirmed | `guard_period` protected immutable identity and audited changes but did not reject reductions below existing financial protection/spending. |

The broad claim that any `needs_review` payment could become completed without
capture evidence was disproved: revision 0001 already required exactly one full,
matching USD capture. However, it did not require a fresh reconciliation decision
to leave review. Revision 0002 adds that requirement and prevents reuse of an old
review after later capture facts.

Before fixing definitions, the first 25 regression cases executed against 0001:
**19 failed and 6 passed**. Failures reproduced the unsafe acceptance paths and
the full-capture accounting overlap; the conservative unexpected-result cases
already passed. Subsequent full-suite results are recorded after verification.

Final verification: `rtk proxy .venv/bin/pytest -q --tb=short` executed all original
53 cases plus 35 hardening cases: **88 passed in 17.17 seconds**, using PostgreSQL
18.6. Coverage includes both financial-writer commit orders at READ COMMITTED,
snapshot contention at REPEATABLE READ, valid uncertain recovery, create/capture
continuation, duplicate/late captures, stale review rejection, and additive
migration upgrade/downgrade/re-upgrade. Interruption tests exercise application
transaction boundaries and rollback, not server power-loss or disk durability.
Source checks also verified Python syntax, whitespace, exact downgrade restoration,
unchanged revision 0001 and unchanged unrelated application/dependency files.

## Revision 0002 changes

The migration replaces four functions and the existing balance view. It does not
introduce tables, dependencies, financial defaults, retries or payment workflows.

### Payment and operation transitions

Self-transitions remain subject to identity and destination-evidence guards.

| Payment state | Permitted different destinations |
|---|---|
| `prepared` | `dispatching` through a claim; `confirmed_failed` with no-charge evidence; `needs_review` |
| `dispatching` | `uncertain`, `payer_action_required`, `completed`, `confirmed_failed`, `needs_review` |
| `uncertain` | `dispatching` through a safe same-operation replay; `payer_action_required`, `completed`, `confirmed_failed`, `needs_review` |
| `payer_action_required`, `needs_review` | Evidence-bound `completed`, `confirmed_failed`, `reconciled`; `needs_review` |
| `completed`, `confirmed_failed`, `reconciled` | `needs_review` to preserve contradictory/late facts; no execution destinations |

Normal completion still needs exactly one full USD capture matching the known
order. Review/stopped-state closure requires a new same-payment Orders retrieval
or owner review as appropriate, no older than the known capture observations.
Unexpected USD outcomes use the existing distinct `reconciled` state with owner
resolution; partial results cannot be relabeled full success. Failure requires
no-charge evidence obtained after dispatch/preparation and no recorded captures.
Late captures can reopen a failed/reconciled/completed payment without erasing
its previous evidence or permitting another financial POST.

Operations start prepared and can enter dispatch only by incrementing their
claim counter. Only dispatched/uncertain operations can record provider outcomes;
terminal operation status changes need operation-bound provider observations.
Completed, confirmed-failed and payer-action operations remain terminal. An
uncertain operation can replay only using the original identity, payload and
documented still-valid retention window. Existing in-flight work, payer-action
stop and any recorded captures block further execution. A new capture operation
requires the create operation to be completed; preparing it does not dispatch it.
This preserves the existing two-operation structure without adding a new flow.

### Derived budget semantics

`reserved_amount` now means outstanding financial protection rather than the raw
sum of rows whose lifecycle status is held. For one linked, exact full USD capture
on a dispatched/uncertain/completed/reconciled payment, recorded spending replaces
the hold in the derived balance. Both changes are visible with the same capture
commit. A rollback leaves the original hold and no captured spending. Explicit
settlement remains a later, idempotently guarded lifecycle transition.

| Facts for a $40 proposal with its reservation still held | Reserved | Spent |
|---|---:|---:|
| No capture / uncertain outcome | 40 | 0 |
| Exactly one matching $40 USD capture | 0 | 40 |
| Unexpected $10 USD partial capture | 40 | 10 |
| Matching $40 plus unexpected $5 extra capture | 40 | 45 |
| Unexpected EUR capture | 40 | 0 USD; EUR fact retained |

Review/stopped/conflicting states retain conservative protection. Once a
reservation is settled or reliably released, that history stays resolved; late
unexpected captures remain actual spending and reopen payment review. The schema
does not invent FX conversions or automatically re-hold a resolved reservation.
Services must surface such late anomalies and any remaining exposure for review.

Actual USD captures can exceed the limit and are never rejected to hide a
provider outcome. New reservations and first dispatches are blocked when the
derived period balance would exceed its limit. Captures and unresolved work stay
attributed to the original reserved month; UTC renewal and no rollover are unchanged.

### Budget adjustments and concurrency

A reduction must leave the effective limit at least equal to confirmed USD spend
plus outstanding protection. Increases are allowed within the money domain,
including incremental increases when unexpected facts already exceed the old
limit. Initial limits/policy identity stay immutable; successful adjustments are
audited. Updating the active policy still does not silently modify an existing
period. The product decision about coupling those two owner actions remains open.

Payment and limit writers now use the existing owner revision-update lock, like
reservations, dispatch and captures. All services must acquire that lock before
other financial row locks. Direct row updates can acquire locks in another order
and receive a safe deadlock abort; PostgreSQL transaction aborts require retry of
the whole transaction, never an automatic repeat of an HTTP payment. Snapshot and
locking behavior follows PostgreSQL's [transaction isolation documentation](https://www.postgresql.org/docs/current/transaction-iso.html)
and [volatile-function visibility rules](https://www.postgresql.org/docs/current/xfunc-volatility.html).

## Migration compatibility

The repository contained one initial Alembic revision and documented only
disposable test databases. No persistent database was contacted to discover or
change externally provisioned development instances. Both original 0001 migration
files remain unchanged. New databases apply 0001 then 0002. Existing 0001 databases
can explicitly run `alembic upgrade head` using their privately supplied URL;
neither reset nor manual schema editing is needed for the definitions.

The migration updates definitions without changing financial rows. An existing
underfunded period is not silently raised; subsequent unsafe reductions/dispatches
are blocked. Previously inconsistent lifecycle rows require evidence-based review
before services use them. The migration cannot reconstruct missing provider facts
or prove that an old status is truthful. Preserve the data and reconcile it.
Downgrading to 0001 restores the known weaknesses and old accounting semantics;
the downgrade exists for reproducibility tests and is unsuitable for operation.

All integration tests use private disposable clusters and random databases.
Revision round trips, upgrade with committed capture facts, no-op upgrade,
offline SQL and the separate runtime-role permissions are exercised. The original
underfunded-dispatch regression is retained by constructing that state under 0001,
upgrading it, and asserting dispatch remains blocked; current-schema tests reject
the reduction itself. No original assertion was removed or relaxed.

## Service-layer limits and next task

The backend must verify and sanitize provider evidence, distinguish an actual
no-charge resolution from a failed HTTP response, establish that an old worker
cannot still execute before marking it uncertain, and authenticate human review.
Stored observation timestamps and outcome labels are trusted backend input, not
independent proof of provider truth. Durable exact payload encryption, endpoint
retention verification, and TTL/policy handling between create/capture still need
a bounded service design before payment integration. No PayPal/Channel3 call,
credential access, automatic retry or persistent-database migration was performed.

The next smallest backend task remains single-owner bootstrap and secure
login/session persistence, without public registration, followed by authenticated
policy configuration. The complete purchasing system remains unimplemented.
