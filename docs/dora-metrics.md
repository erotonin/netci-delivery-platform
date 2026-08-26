# DORA metrics

Metrics are projections from immutable delivery events, not manually entered dashboard values.

| Metric | Local definition |
|---|---|
| Deployment frequency | Count of successful production deployments per application and time window |
| Change lead time | First successful production deployment time minus commit time for the deployed SHA |
| Change fail rate | Production deployments causing failure/rollback divided by production deployments |
| Time to restore service | First matched recovery time minus failure time for the same application/deployment/environment |

The dashboard exposes exactly these four metrics, matching the Claude artifact used as the portal design contract. Deployment rework may remain an internal operational measure, but it is not presented or described as a DORA metric.

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
