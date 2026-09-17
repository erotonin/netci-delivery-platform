# ADR-036: Temporal persists to PostgreSQL, beside netCI's own database

Status: Accepted.

## Context

Every deployment is a Temporal workflow: validate the artifact, take the lease, run the
playbook, health-check, roll back on failure, report. The lab and the compose file ran
`temporal server start-dev`, whose persistence is in-memory. A restart of that process
-- a host reboot did exactly this on 2026-09-17 -- forgot every in-flight workflow.
The deployment row in netCI then stayed `deploying` with a `workflowId` nothing
answered for, and the lease it held had to be reaped by the reconciler as a crash.

netCI's own rule is that PostgreSQL is canonical at request time and nothing durable
lives in process memory (ADR-014). The orchestrator holding the most important
in-flight state was the one exception.

## Decision

**Temporal persists to the same PostgreSQL server as netCI, in its own databases.**
`temporal` and `temporal_visibility`, created next to `netci` by the bootstrap step
(lab) or the init script (compose). The server is `temporalio/auto-setup`, which
applies Temporal's schema on first start and is idempotent afterwards; the UI is a
separate container. The dev-server image is no longer used anywhere.

**One server, two owners, no shared tables.** Temporal's schema is Temporal's; netCI
never reads it. Sharing the server is an operational choice (one thing to back up,
patch and monitor in the lab); the databases could move to another server by changing
`POSTGRES_SEEDS` and nothing in netCI would notice.

**Retention is 30 days** (`DEFAULT_NAMESPACE_RETENTION=720h`): closed workflow histories
stay readable for as long as netCI's own pipeline logs, so a deployment's audit can be
followed into the orchestrator that ran it.

**What this does not do.** `scripts/netci_backup.py` backs up and restore-verifies
`netci` only. Temporal's databases are not in that backup: a restore of netCI without
them brings back deployments whose workflows are gone, which the reconciler reports as
crashed and the operator re-runs. That gap is stated in the backup script's docstring
rather than papered over with an unverified dump.

## Proof

With the server on PostgreSQL, a staging rollout of `shop-api` was started from the
browser and `docker restart netci-temporal` was issued while its workflow was running.
The workflow resumed after the restart and the run finished; the outcome is recorded in
`docs/LIVE-READINESS.md`.

## Rejected

- *Keep start-dev and reap on restart.* The reconciler already turns an orphaned
  deployment into an honest `failed`; but losing the rollout was never acceptable, only
  visible.
- *SQLite file persistence of the dev server.* Survives a restart, not a second
  replica; the platform runs two of everything (ADR-032).
