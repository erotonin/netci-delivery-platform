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

## Current limitation

When `DATABASE_URL` is absent, the local API intentionally uses deterministic in-memory state. With PostgreSQL configured, Portal mutations fail closed if persistence is unavailable and `/healthz` exposes persistence status; migration, restart/recovery and Temporal worker behavior still require runtime evidence on Ubuntu before they can be marked ready.
