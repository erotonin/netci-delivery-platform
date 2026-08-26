# ADR-005: Runtime adapters

Status: Accepted.

## Context

Docker, Kubernetes and Systemd require different commands and health/rollback mechanisms.

## Decision

All targets implement `validate`, `deploy`, `getStatus`, `healthCheck` and `rollback` behind a `RuntimeAdapter` contract. Provider configuration is injected at registration time.

## Consequences

Core services branch on capability/runtime registration, not provider CLI details. Adapter tests and one E2E golden path per target are required.

## Verification

Docker, Kubernetes and Systemd adapters pass the same contract suite; changing an adapter does not modify domain entities.
