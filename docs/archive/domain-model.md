# Domain model

## Ubiquitous language

| Term | Meaning |
|---|---|
| Application | Stable netCI identity plus repository, template, runtime and default environment |
| Stage catalog | Supported stage definitions and ordered golden-path templates |
| PipelineRun | One CI request for an application, commit and environment |
| Artifact | Published immutable image/binary identified by digest |
| Deployment | Attempt to place one artifact into one environment/runtime target |
| Approval | Actor/comment decision permitting a governed transition |
| AuditEvent | Append-only record of actor, action, subject, time and correlation ID |
| DeliveryEvent | Normalized source event used by the DORA projector |

## Aggregate relationships

```text
Application 1 --- * PipelineRun 1 --- 0..1 Artifact
Application 1 --- * Deployment  * --- 1 Artifact
Deployment  1 --- * Approval
Any aggregate --- * AuditEvent / DeliveryEvent
```

## Required invariants

- Application name is unique and matches `^[a-z0-9][a-z0-9-]{2,62}$`.
- Template runtime equals application runtime.
- A repeated idempotency key with the same request replays the first result; a different request conflicts.
- A deployment accepts `repository@sha256:...` or a signed binary digest, never mutable `latest`.
- A production deployment cannot skip policy or approval.
- Rollback targets a previously healthy revision and is itself audited.
- DORA recovery is matched by application, deployment and environment—not timestamp alone.

## Ports

Core/application services depend on contracts such as `JenkinsPort`, `WorkflowPort`, `ArtifactStorePort`, `SecurityVerifier` and `RuntimeAdapter`. A runtime adapter exposes `validate`, `deploy`, `get_status`, `health_check` and `rollback`. Infrastructure implementations may change without changing these domain concepts.
