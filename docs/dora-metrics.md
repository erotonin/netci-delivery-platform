# DORA metrics

Metrics are projections from immutable delivery events, not manually entered dashboard values.

| Metric | Local definition |
|---|---|
| Deployment frequency | Count of successful production deployments per application and time window |
| Change lead time | First successful production deployment time minus commit time for the deployed SHA |
| Change fail rate | Production deployments causing failure/rollback divided by production deployments |
| Time to restore service | First matched recovery time minus failure time for the same application/deployment/environment |

The dashboard exposes exactly these four metrics, matching the Claude artifact used as the portal design contract. Deployment rework may remain an internal operational measure, but it is not presented or described as a DORA metric.

## Where the events come from

Source events are rows in `delivery_events`, written in the **same database transaction**
as the state change that caused them. There is no separate publisher that can fall behind
or drop a message, which is what makes a metric reproducible from its inputs:

| Event | Written by | Carries |
|---|---|---|
| `commit` | `start_pipeline` | commit SHA, pipeline run, and `parameters.commitTimestamp` when the caller supplies the real authoring time |
| `deployment` | `record_deployment_result` and `rollback_deployment`, for production only | deployment id, success, and whether it required intervention |
| `recovery` | the next healthy production deployment, or a rollback of a failed one | the deployment id of the **specific failure** it restored |

A recovery is emitted once per failure and is bound to that failure's deployment id, so
Time to Restore Service is never inferred from event ordering. `GET /delivery-events`
returns the raw rows; `GET /applications/{id}/dora` returns the four metrics plus
`sourceEvents`, the count they were projected from. The Portal prints that count beside
the cards, so a figure with nothing behind it reads as "no delivery events recorded yet"
rather than as a healthy-looking baseline.

The reporting window is a rolling 30 days and is returned with the payload; a rate is
meaningless without the period it was measured over.

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

`make dora-dashboard` runs `scripts/gate_dora.py`, which drives a known delivery history
through the API (a good release, a failed release, the recovery that followed), reads the
dashboard back, validates it against `api/dora-dashboard.schema.json`, and then
**recomputes all four metrics independently** from `GET /delivery-events` and compares.
A metric that cannot be reproduced from its source events fails the gate.

The evidence it writes (`evidence/dora-dashboard.json`) contains the commands, the raw
events, the dashboard payload, the independently recomputed values and every assertion
with its verdict. A dashboard with synthetic values or cross-application recovery matching
cannot pass.
