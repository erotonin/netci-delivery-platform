# ADR-016: Deployment leases, monotonic fencing tokens, and atomic log sequences

Status: Accepted.

## Context

Three concurrency and consistency hazards were present in deployment execution and log capture:

1. **Unconstrained concurrent deployments to the same target.**
   Nothing prevented multiple concurrent deployment requests or approvals against the same application, environment, and logical target (e.g., host set, namespace). If two approvals occurred close together, two workflows executed concurrently, racing to deploy conflicting configurations or code. Whichever workflow finished last silently overwrote the other, leaving the system in an unpredictable state.

2. **Stale callbacks and zombie workflows.**
   A workflow instance experiencing network delays, long pauses, or timeouts could be superseded by a newer deployment workflow. When the delayed workflow finally completed and reported its callback, it could overwrite a newer, successful deployment's status with stale results.

3. **Log sequence race condition.**
   `INSERT INTO pipeline_logs ... (SELECT max(sequence) + 1 ...)` without table or row locking is vulnerable under concurrent execution. Two concurrent append operations against the same pipeline run could calculate the same sequence number, causing a unique constraint violation and aborting one of the transactions.

## Decision

### 1. Database-enforced deployment lease with partial unique index

We enforce deployment exclusivity directly in PostgreSQL using a `deployment_leases` table and a partial unique index:
```sql
CREATE UNIQUE INDEX deployment_leases_one_active_per_target
    ON deployment_leases (application_id, environment, target)
    WHERE released_at IS NULL;
```
- **Why database-level?** API replicas and workers are distributed. In-memory locking or check-then-act logic in application code cannot prevent races between different replicas. The database partial unique index acts as the single source of truth and enforces mutual exclusion atomically.
- **Lease fields**: Each lease records `id`, `application_id`, `environment`, `target`, `deployment_id`, `owner` (workflow ID or API request identifier), monotonic `fencing_token`, `acquired_at`, `heartbeat_at`, `expires_at`, and optional `released_at`/`release_reason`.
- **Conflict handling**: Attempting to acquire a lease when an unreleased lease already exists raises a collision error (`409 TARGET_LOCKED`), which is recorded in the audit log.

### 2. Monotonic fencing tokens per target

To defend against stale callbacks:
- A dedicated counter table (`deployment_fencing_counters`) tracks `next_token` per `(application_id, environment, target)`.
- When a lease is acquired, `next_token` is incremented atomically using `UPDATE ... RETURNING`.
- The acquired `fencing_token` is stored on the `deployments` record and must be supplied by the workflow on callback.
- On any callback or status update (`/deployments/{id}/result`), the system verifies that the supplied `fencingToken` is greater than or equal to the deployment's recorded `fencing_token` and that no newer lease generation has superseded it.
- Callbacks bearing a stale or missing fencing token are rejected with `409 STALE_FENCING_TOKEN` and audited.

### 3. Clear terminal states and two-phase rollback lifecycle

The deployment status constraint is expanded to cover all real operational lifecycle states:
- `pending_approval`
- `deploying`
- `healthy`
- `failed`
- `rollback_in_progress`
- `rolled_back`
- `rollback_failed`
- `cancelled`

Rollback operations transition through `rollback_in_progress` before reaching `rolled_back` or `rollback_failed`. A DORA recovery event is emitted only when a rollback or subsequent deployment genuinely succeeds in restoring service.

### 4. Atomic log sequence allocation

We replaced the vulnerable `SELECT max(sequence) + 1` with a dedicated counter table (`pipeline_log_sequences`):
- Each pipeline run has an atomic counter row.
- Appending logs atomically increments `next_sequence` via `UPDATE pipeline_log_sequences SET next_sequence = next_sequence + :count WHERE pipeline_run_id = :id RETURNING next_sequence`.
- Concurrent log appenders receive disjoint sequence ranges, guaranteeing monotonic ordering and eliminating primary key collisions.

## Consequences

- Deployments to the same application, environment, and target are serialized.
- Delayed or replayed workflow callbacks cannot corrupt newer deployment states.
- Log appending is concurrency-safe under multi-worker loads.
- Full auditability for all lease acquisitions, conflicts, expirations, and stale callbacks.
