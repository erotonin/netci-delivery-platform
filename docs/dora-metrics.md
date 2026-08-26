# DORA metrics

Metrics are projections from immutable delivery events, not manually entered dashboard values.

| Metric | Local definition |
|---|---|
| Change lead time | First successful production deployment time minus commit time for the deployed SHA |
| Deployment frequency | Count of successful production deployments per application and time window |
| Failed deployment recovery time | First matched recovery time minus failure time for the same application/deployment/environment |
| Change fail rate | Production deployments causing failure/rollback divided by production deployments |
| Deployment rework rate | Deployments superseded by corrective deployment/rollback within the agreed rework window |

## Minimum event fields

```json
{
  "eventId": "uuid",
  "eventType": "deployment.succeeded",
  "occurredAt": "RFC3339 timestamp",
  "applicationId": "uuid",
  "pipelineRunId": "uuid",
  "deploymentId": "uuid",
  "environment": "prod",
  "commitSha": "...",
  "artifactDigest": "sha256:...",
  "correlationId": "..."
}
```

Projectors deduplicate by event ID and match failures/recoveries by application, deployment lineage and environment. They expose calculation window, numerator/denominator and source-event links so a mentor can reproduce each number.

## Evidence

DORA-01 contains source JSONL, projector version/commit, query window, calculated output and a dashboard screenshot. A dashboard with synthetic or cross-application recovery matching does not pass the gate.
