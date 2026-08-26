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
| `POST /deployments/{id}/approve` | Record approval | `202` |
| `POST /deployments/{id}/rollback` | Request rollback | `202` |

## Request semantics

- Mutating operations use `Idempotency-Key`; clients reuse a key only when retrying the same logical request.
- `X-Correlation-Id` crosses Portal/Backstage, netCI, workflow, Jenkins, adapter and audit/evidence.
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

## Current limitation

The local API persistence is currently in-memory. Contract tests can run on Windows, but restart/recovery, PostgreSQL migration and Temporal worker behavior require later implementation/evidence.
