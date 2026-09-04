# API contract

`api/openapi.yaml` is the normative HTTP contract. Client types in `frontend/src/api/netciClient.ts` must change in the same review whenever the schema changes.

## Main operations

| Method and path | Purpose | Success |
|---|---|---|
| `GET /healthz` | Liveness/version | `200` |
| `GET /stage-catalog` | Dynamic stages/templates | `200` |
| `GET /applications` | List applications | `200` |
| `POST /applications` | Declare application | `201` |
| `POST /applications/{id}/pipeline-runs` | Queue CI | `202` |
| `GET /pipeline-runs/{id}` | Read run state | `200` |
| `GET /pipeline-runs/{id}/logs` | Read normalized logs | `200` |
| `POST /pipeline-runs/{id}/ci-result` | Authenticated Jenkins/CI callback | `202` |
| `POST /pipeline-runs/{id}/security-evidence` | Authenticated CI publishes SBOM/scan/signature evidence and receives the policy verdict | `202` |
| `GET /pipeline-runs/{id}/security-evidence` | Read the stored evidence and its decision | `200` |
| `GET /applications/{id}/dora` | Four DORA metrics with the source-event count they came from | `200` |
| `GET /delivery-events` | Source events behind the DORA projection | `200` |
| `POST /deployments/{id}/approve` | Record approval | `202` |
| `POST /deployments/{id}/result` | Authenticated runtime-adapter callback | `202` |
| `POST /deployments/{id}/rollback` | Request rollback | `202` |
| `POST /systems/{systemId}/modules` | Provision a Portal module and its delivery application | `201` |
| `POST /modules/{moduleId}/pipeline-runs` | Queue a named Portal pipeline | `202` |
| `POST /modules/{moduleId}/versions` | Register an immutable release version | `201` |
| `POST /production-requests` | Create a single-module production promotion request | `201` |
| `POST /production-requests/{requestId}/approve` | Approve a pending production request | `202` |
| `POST /production-requests/{requestId}/reject` | Reject a pending production request | `202` |

## Request semantics

- Mutating operations use `Idempotency-Key`; clients reuse a key only when retrying the same logical request.
- `X-Correlation-Id` crosses Portal/Backstage, netCI, workflow, Jenkins, adapter and audit/evidence.
- Machine callbacks (`ci-result`, deployment `result` and module CI report) require `Authorization: Bearer <pipeline-api-key>`; the key comes from `NETCI_PIPELINE_API_KEY` and must be injected from a secret outside local development.
- Human actor fields are never accepted from a request body. A system's `owner`, application `startedBy`, request `requestedBy` and approval actor all come from the authenticated principal.
- Portal module creation carries one to three unique `deploymentEnvironments`. The selected pipeline template, application runtime and every environment runtime must agree. Docker/Systemd targets declare at least one DCIM server; Kubernetes targets declare a secret reference and explicit namespace, never raw kubeconfig content.
- `pipelineConfig` stores the runner, branching strategy and the branch, coverage path and ordered stage identifiers for each configured pipeline tab. Legacy `deploymentEnvironments[].tasks/taskSettings` fields remain readable for compatibility, but are never converted into commands. Deployment, health and rollback execute only reviewed playbooks checked into Git.
- Pipeline runs preserve `parameters.portalPipeline` so CI, CD Development, CD Staging, CD Production and Automation Test remain distinct in the Portal even when they target the same environment.
- Production requests currently contain exactly one module and one immutable, evidence-linked version. Approval re-checks the security evidence and, when `runAutomationTests=true`, requires that version's pipeline-reported `autoTest` result to be `passed`. The target host/namespace is rebound from that module's **production** deployment configuration; it is not inherited from the source run. The offset-aware schedule and rollback strategy are carried into Temporal. Multi-module requests are rejected until an ordered coordinator exists. Reusing an `Idempotency-Key` with a different payload is a conflict.
- Creating the approval-bound production run uses the production request ID as a second durable idempotency boundary. A retry after Portal persistence failure reuses the existing run/deployment; a concurrent loser is rolled back by the database transaction instead of creating a second promotion.
- JSON fields use camelCase at the HTTP boundary.
- Runtime/template mismatch and unknown templates are validation errors, not implicit fallback.
- Every pipeline-run and deployment record carries a `version`. A write supplies the version it read; a mismatch is `409 CONCURRENT_MODIFICATION` rather than a silent overwrite, so two callbacks racing the same transition cannot both win.
- `parameters.commitTimestamp` (ISO-8601 with an offset) is optional on a pipeline run. When present it is used as the commit event's timestamp, which is what makes Lead Time for Changes measure commit-to-production rather than button-press-to-production.

## Which engines an instance drives

`GET /healthz` reports `engines.ci` and `engines.cd`:

| Setting | Meaning |
|---|---|
| `NETCI_CI_MODE=none` (default) | netCI records the queued run and waits for an authenticated `ci-result` callback. It never reports a build it did not start. |
| `NETCI_CI_MODE=jenkins` | The router picks a healthy controller with capacity, reconciles the pipeline job from the template, triggers it and stores the controller-qualified run id on the run. If no controller accepts the build the run is failed with `CI_LAUNCH_FAILED` (502) rather than left queued forever. |
| `NETCI_CD_MODE=none` (default) | netCI tracks deployment state and waits for an authenticated `result` callback. |
| `NETCI_CD_MODE=temporal` | A successful build starts `ProvisionAndDeployWorkflow` with the deterministic id `netci-deploy-{deploymentId}`, so a retried callback re-attaches instead of duplicating. Production starts the workflow only after approval; an approval arriving for an already-running workflow is delivered as a signal. |

Both default to `none` so the local reference implementation is honest about what it is doing. Neither mode changes any domain rule.

## Delivery events and DORA

Every state change writes its state, its outbox events, its audit record and its log lines in one database transaction. That is what stops "the deployment succeeded but the metric event was lost" from being possible.

Three event kinds are recorded, and only these are projected:

| Event | Written when | Used for |
|---|---|---|
| `commit` | a pipeline run is queued | the start of Lead Time for Changes |
| `deployment` | a **production** deployment reaches a terminal state | Deployment Frequency and Change Failure Rate |
| `recovery` | a later production release restores service after a failure, or a rollback completes | Time to Restore Service |

A `recovery` carries the `deploymentId` of the failure it restored, so restore time is never inferred from event ordering. Only one recovery is emitted per failure. `GET /applications/{id}/dora` reports `sourceEvents` alongside the metrics; a number without events behind it reads as zero rather than as a plausible baseline.

Error response:

```json
{
  "code": "RUNTIME_TEMPLATE_MISMATCH",
  "message": "runtime does not match pipeline template",
  "correlationId": "..."
}
```

## Browser clients

Vite serves Portal requests through same-origin `/api` and rewrites that prefix to FastAPI during development. The Compose `portal` service builds the same bundle and Nginx proxies `/api` to `netci-api`; no development server is present in that image. `VITE_NETCI_API_URL` is an explicit deployment override for a separately hosted UI. Backstage uses `/api/proxy/netci` configured by `backstage/app-config.example.yaml`; it does not call Jenkins.

For a separately hosted browser client, `NETCI_ALLOWED_ORIGINS` is a comma-separated list of exact `http` or `https` origins. Wildcards, credentials in URLs, paths, queries and fragments are rejected at startup. Only `GET`, `POST` and the headers required by this contract are allowed through CORS; `X-Correlation-Id` is exposed to the browser.

## Persistence

PostgreSQL is canonical at request time. Every command reads the rows it is about to change inside its own transaction and writes them back with the version it read; every query reads the database with a filter. Nothing is cached between requests and nothing is loaded at start-up, which is what makes multiple API replicas and multiple workers correct: they share state because they share the database, not because they were started together. See [ADR-014](decisions/ADR-014-postgresql-canonical-state.md).

When `DATABASE_URL` is absent in `NETCI_ENVIRONMENT=local`, the API uses an in-memory store, which keeps unit tests fast and a portable preview usable. It is a test adapter, not a fallback: outside local mode a missing `DATABASE_URL`/`DATABASE_URL_FILE` is a startup error rather than a degraded mode. With PostgreSQL configured:

- an unreachable database returns `503 PERSISTENCE_UNAVAILABLE` on reads as well as writes, instead of answering from a snapshot that may be stale;
- a run or deployment update that loses a race returns `409 CONCURRENT_MODIFICATION`. The losing replica is correct again on its next request, because it re-reads;
- a duplicate-key race also returns `409 CONCURRENT_MODIFICATION` rather than a `503` that would send an operator to look at the database for a name clash;
- idempotency records are written in the same transaction as the resource they describe, so a retry after a network timeout returns the original resource on any replica and after any restart;
- pipeline logs, delivery events and audit events survive a restart.

### Failure modes

| Situation | Answer | Why |
| --- | --- | --- |
| Database unreachable | `503 PERSISTENCE_UNAVAILABLE`, `/healthz` `503 degraded` | Answering a read from memory would be answering from a snapshot |
| Two replicas race the same transition | one `2xx`, one `409 CONCURRENT_MODIFICATION` | Compare-and-set on the `version` column |
| Two replicas insert the same key | one `2xx`, one `409 CONCURRENT_MODIFICATION` | The unique index decides; the refusal is translated, not leaked |
| Onboarding retried with the same `Idempotency-Key` and body | `201` with the original module | The idempotency row and the resources share one transaction |
| Same key, different body | `409 IDEMPOTENCY_KEY_REUSED` | The stored request hash does not match |
| Crash mid-onboarding | nothing is written | Application, module, audit and idempotency row commit together |

### Runbook

- **`/healthz` reports `degraded` with `portalPersistence.status = degraded`.** The API cannot reach PostgreSQL. Check `DATABASE_URL`/`DATABASE_URL_FILE`, the database's own health, and connection limits — every request now takes a connection, so exhaustion presents the same way. The health response never contains the connection string.
- **Clients report sporadic `409 CONCURRENT_MODIFICATION`.** Two writers are touching one aggregate. This is the control working; the client should re-read and retry. Persistent conflicts on one run usually mean a duplicated callback — check for two CI controllers or a retrying webhook.
- **A client says onboarding returned `MODULE_EXISTS` after a timeout.** It retried without an `Idempotency-Key`. With the header the retry returns the original module; without it the server cannot tell a retry from a second request.
- **Adding a replica.** Point it at the same `DATABASE_URL` and start it. No warm-up, no cache priming, and no restart of the existing replicas is required.

Apply schema changes with `python scripts/migrate.py`. Each file in `backend/migrations` runs once, in filename order, in its own transaction, and is recorded in `schema_migrations` with its checksum; editing an already-applied migration is refused rather than skipped. `backend/schema.sql` is generated from those files (`--emit-schema`) for the compose initdb mount, and `--check-schema` fails if the two have drifted.

`backend/tests/test_persistence_postgres.py` asserts restart recovery, event durability, idempotency replay across a restart, that two replicas racing the same transition do not both win, that a second replica sees the first one's writes without restarting, and that a fault injected between the application insert and the module insert rolls both back. It skips unless `NETCI_TEST_DATABASE_URL` points at a migrated database.
