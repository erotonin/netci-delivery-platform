# ADR-014: PostgreSQL is canonical at request time

Status: Accepted.

## Context

`DeliveryPlatform` and `PortalReadModel` were process-lifetime caches. Both loaded every
application, run, deployment, delivery event, log line, audit record, system, module,
version and production request into dictionaries at import time and answered every
subsequent request from RAM. Writes went to PostgreSQL first and were then mirrored into
those dictionaries.

That design has three consequences that no amount of care inside a single process can fix:

* **Replicas diverge.** A second API replica never learns about a system created on the
  first one. Its dictionaries are a snapshot of the moment it started, and nothing
  invalidates them. Authorization is computed from `platform.list_applications()`, so the
  divergence is not cosmetic: replica B refuses access to an application replica A owns.
* **Startup cost grows with history.** The load is unbounded — every log line and every
  audit row ever written is materialized before the process serves its first request.
* **Onboarding was not atomic.** `POST /systems/{systemId}/modules` created a Delivery
  Application in one transaction and attached the Portal Module in another. A crash, a
  storage error or a network timeout between the two left an application that no module
  points at, and the retry that a client would naturally send hit
  `MODULE_EXISTS` instead of returning the resource the first attempt created.

## Decision

**One seam, one transaction per request.** `app/store` owns a `PlatformDatabase` whose
entire interface is `transaction() -> PlatformSession`. A session is one PostgreSQL
transaction that exposes targeted reads and writes for both the delivery aggregate and the
Portal hierarchy. Connection handling, SQL, row mapping, version compare-and-set and
rollback live inside the implementation; callers see a context manager.

* **Canonical reads.** Commands read the rows they are about to change inside their own
  transaction. Queries read the database with a filter rather than scanning a dictionary.
  No process-local cache decides anything durable, so there is nothing to invalidate.
* **No startup load.** Composition builds the seam. It does not read state.
* **Compare-and-set.** Every mutating write of a run or a deployment carries the version it
  was read at and updates `WHERE id = ... AND version = ...`. Zero rows updated is a lost
  race, surfaced as `409 CONCURRENT_MODIFICATION`. Because the next request re-reads from
  the database, a replica that loses a race is correct again immediately — the previous
  design could stay stale until it was restarted.
* **One transaction for onboarding.** The endpoint opens a session and hands it to both the
  delivery command and the Portal command, so the application row, the module row, the
  audit record and the idempotency record commit or roll back together.
* **Idempotency is a database row, not a memory entry.** The `idempotency_records` insert is
  part of the same transaction as the resource it describes, and replay is answered by
  reading that table. A retry after a network timeout therefore returns the original
  application *and* the original module, on any replica, after any restart.

**Naming.** `PortalReadModel` issued commands — it created systems, attached modules,
registered versions and approved production requests. It is now `PortalService`. A name that
says "read model" while writing state hides exactly the thing a reader needs to see.

**In-memory adapter.** `InMemoryDatabase` implements the same `PlatformDatabase` interface
with staged, rolled-back-on-exception state. It is selected only when `DATABASE_URL` is
absent *and* `NETCI_ENVIRONMENT=local`. Any other environment raises at startup rather than
serving from a store that does not survive the process.

## Alternatives considered

* **Cache with invalidation (LISTEN/NOTIFY).** Rejected for this phase. It adds a second
  source of truth and a failure mode (a missed notification) whose symptom is silent
  authorization divergence — the exact bug being removed. A cache can be reintroduced later
  behind the same seam, where it is provably not load-bearing for correctness.
* **Advisory-lock the aggregate for the whole request.** Rejected: it serializes unrelated
  work and still needs a version column for callbacks that arrive without a lock.
  Compare-and-set is cheaper and detects the case that actually matters.
* **A single "onboarding" stored procedure.** Rejected: it moves domain rules into SQL,
  where the stage/template validation and the ownership policy cannot be unit-tested.

## Consequences

Every request now costs at least one database round trip, and the database must be
reachable for reads as well as writes — a `PERSISTENCE_UNAVAILABLE` 503 is now possible on
read paths that previously answered from RAM. That is the intended trade: an API that
cannot reach its source of truth must say so rather than answer from a snapshot.

Multiple API replicas and multiple workers are now supported: they share state because they
share the database, not because they were started at the same time.

`PostgresDeliveryStore.load()` and `PostgresPortalStore.load()` are gone, along with
`_rehydrate_idempotency`. Demo seeding, which previously wrote into the dictionaries during
construction, is now an explicit `seed_demo_data(database)` call that writes rows and is
still gated on `NETCI_DEMO_DATA`.

## Verification

`backend/tests/test_persistence_postgres.py` runs against a real PostgreSQL: two
independently constructed API instances share one database and see each other's writes
without a restart; two sessions racing the same aggregate produce exactly one winner;
a fault injected between the application insert and the module insert rolls back both;
an onboarding retry with the same `Idempotency-Key` returns the same application and the
same module; the same key with a different payload is `409`; no orphan application is left
behind; and an authorization query on the second instance uses the newest state.
