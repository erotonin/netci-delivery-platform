# ADR-020: Pipeline Lifecycle, Cancellation, Retry Lineage, Stage Events, and Reconciliation Watchdog

## Status
Accepted

## Context
In a production-grade Internal Developer Platform, CI/CD operations are asynchronous and distributed across external engines (Jenkins controllers, Temporal workflow clusters). In previous phases:
1. Pipelines and deployments lacked real cancellation: users could not abort an erroneous build or runaway deployment without manually logging into Jenkins or Temporal.
2. Pipeline retries were not tracked: triggering a re-run had no lineage, or risk overwriting previous run records. Run records must remain strictly immutable for compliance and auditability.
3. Callbacks were coarse-grained: Jenkins only reported start and completion, providing no stage-by-stage visibility (e.g. compile, test, SAST, docker build, publish) in the developer portal.
4. Distributed failure modes (network partitions, lost webhooks, crashed controllers) could leave pipeline runs stuck in `QUEUED` or `RUNNING` and deployments stuck in `DEPLOYING` holding exclusive deployment leases indefinitely.

## Decision

1. **Pipeline & Deployment Cancellation (`POST /pipeline-runs/{id}/cancel` & `POST /deployments/{id}/cancel`)**:
   - Cancel endpoints verify current entity state (rejecting already terminal runs with 409 `INVALID_PIPELINE_STATE` or `INVALID_DEPLOYMENT_STATE`).
   - Pipeline cancel sends HTTP POST to Jenkins abort endpoint (`/job/{job_name}/{build_number}/stop`) via `CiLauncher.abort()`.
   - Deployment cancel issues a cancel signal to Temporal workflow handle (`handle.cancel()`) via `CdOrchestrator.cancel()`.
   - Deployment cancellation immediately releases the active environment deployment lease.
   - SCM commit status is updated to `CANCELLED` when supported.
   - Audited with `pipeline.cancelled` and `deployment.cancelled` records.

2. **Pipeline Retry with Lineage and Immutability (`POST /pipeline-runs/{id}/retry`)**:
   - Only completed or cancelled runs can be retried (active runs reject with 409 `RUN_STILL_ACTIVE`).
   - The original `PipelineRun` record remains strictly immutable.
   - A new `PipelineRun` is created with `retry_of` pointing to the parent run ID.
   - Inherits commit SHA, branch, environment, and parameters from the parent.
   - Dispatches a fresh build to the configured CI launcher with a newly minted single-use workload token.
   - Emits `pipeline.retried` audit event capturing both `parentRunId` and `newRunId`.

3. **Granular Stage Events & Scoped Callbacks (`pipeline_stages`)**:
   - Created `pipeline_stages` table with schema migration `0013_pipeline_lifecycle_and_stage_events.sql` tracking:
     `pipeline_run_id`, `stage_id`, `stage_name`, `attempt`, `status`, `queued_at`, `started_at`, `completed_at`, `duration_ms`, `error_message`, `log_snippet`.
   - Included `pipeline_stages` in `scripts/netci_backup.py` critical backup manifest.
   - Created callback endpoints `POST /pipeline-runs/{id}/stages` and `POST /pipeline-runs/{id}/stages/{stageId}`.
   - Workload authorization verifies workload tokens carrying `Scope.CI_STAGE` (`ci:stage`) or `Scope.CI_RESULT` (`ci:result`). Tokens scoped only for other operations (e.g. `deploy:result`) are rejected with 403 Forbidden.
   - Persisted idempotently via PostgreSQL `ON CONFLICT (pipeline_run_id, stage_id, attempt) DO UPDATE`.
   - Query endpoint `GET /pipeline-runs/{id}/stages` returns chronologically sorted stage executions with durations.

4. **Reconciliation Watchdog Service (`backend/app/reconciler.py`)**:
   - `Reconciler` polls active pipeline runs and deployments with bounded concurrency (`ThreadPoolExecutor(max_workers=5)`).
   - Inquires external status from `CiLauncher.get_status()` and `CdOrchestrator.get_status()`.
   - Detects lost callbacks:
     - If Jenkins completed successfully, repairs run to `SUCCEEDED` (or progresses to deployment) and audits `pipeline.reconciled`.
     - If Jenkins failed or was aborted, repairs run to `FAILED` or `CANCELLED` and audits `pipeline.reconciled`.
     - If run exceeded `stale_run_timeout_seconds` without contact, transitions run to `FAILED`.
     - If Temporal workflow finished or failed, repairs deployment to `HEALTHY` or `FAILED`, releases expired deployment leases, and audits `deployment.reconciled`.
   - Exposes authenticated operational endpoint `POST /reconciler/reconcile`.

5. **Frontend Developer Experience (`netciClient.ts` & `ModulePage.tsx`)**:
   - `netciClient.ts`: Added types for `PipelineStage`, `Deployment`, and functions `cancelPipelineRun`, `retryPipelineRun`, `getPipelineStages`, `cancelDeployment`.
   - `ModulePage.tsx`:
     - Stage execution graph in `PipelineRunView` fetches live stage results and displays real stage status, duration, and error snippets.
     - "Cancel" button aborts queued/running pipeline runs.
     - "Retry" button safely triggers retry with lineage (`(retry of #...)`).

## Consequences
- No builds or deployments can hang indefinitely when callbacks are dropped.
- Historical run data is never overwritten; retries are first-class historical records with explicit lineage.
- Operators and developers have full visibility into stage progress and the ability to intervene immediately.
