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
| `POST /production-requests` | Create an ordered, multi-module production request | `201` |
| `POST /production-requests/{requestId}/approve` | Approve a pending production request | `202` |
| `POST /production-requests/{requestId}/reject` | Reject a pending production request | `202` |

## Request semantics

- Mutating operations use `Idempotency-Key`; clients reuse a key only when retrying the same logical request.
- `X-Correlation-Id` crosses Portal/Backstage, netCI, workflow, Jenkins, adapter and audit/evidence.
- Machine callbacks (`ci-result`, deployment `result` and module CI report) require `Authorization: Bearer <pipeline-api-key>`; the key comes from `NETCI_PIPELINE_API_KEY` and must be injected from a secret outside local development.
- Portal module creation carries one to three unique `deploymentEnvironments`. The selected pipeline template, application runtime and every environment runtime must agree. Docker/Systemd targets declare at least one DCIM server; Kubernetes targets declare a secret reference and explicit namespace, never raw kubeconfig content.
- `pipelineConfig` stores the runner, branching strategy and the branch, coverage path and ordered stage identifiers for each configured pipeline tab. Health-check task details are stored under `deploymentEnvironments[].taskSettings.healthCheck`; delay values use an explicit unit such as `500ms`, `10s` or `1m`.
- Pipeline runs preserve `parameters.portalPipeline` so CI, CD Development, CD Staging, CD Production and Automation Test remain distinct in the Portal even when they target the same environment.
- Production requests contain one to twenty unique modules, an immutable registered version and deployment order for each module, an offset-aware schedule, rollback strategy and automation-test policy. Reusing an `Idempotency-Key` with a different payload is a conflict.
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

Vite serves Portal requests through same-origin `/api` and rewrites that prefix to FastAPI during development. `VITE_NETCI_API_URL` is an explicit deployment override. Backstage uses `/api/proxy/netci` configured by `backstage/app-config.example.yaml`; it does not call Jenkins.

For a separately hosted browser client, `NETCI_ALLOWED_ORIGINS` is a comma-separated list of exact `http` or `https` origins. Wildcards, credentials in URLs, paths, queries and fragments are rejected at startup. Only `GET`, `POST` and the headers required by this contract are allowed through CORS; `X-Correlation-Id` is exposed to the browser.

## Persistence

When `DATABASE_URL` is absent the API uses deterministic in-memory state, which keeps unit tests fast and the Windows preview usable. With PostgreSQL configured:

- state is written before the in-memory projection is updated, so a storage failure returns `503 PERSISTENCE_UNAVAILABLE` instead of reporting a state the database does not hold;
- idempotency records are persisted, so a restart cannot double-create a resource a client already got a response for;
- pipeline logs and audit events survive a restart.

Apply schema changes with `python scripts/migrate.py`. Each file in `backend/migrations` runs once, in filename order, in its own transaction, and is recorded in `schema_migrations` with its checksum; editing an already-applied migration is refused rather than skipped. `backend/schema.sql` is generated from those files (`--emit-schema`) for the compose initdb mount, and `--check-schema` fails if the two have drifted.

`backend/tests/test_persistence_postgres.py` asserts restart recovery, event durability, idempotency replay across a restart, and that two processes racing the same transition do not both win. It skips unless `NETCI_TEST_DATABASE_URL` points at a migrated database.
