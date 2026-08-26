# ADR-004: Jenkins CI and netCI CD

Status: Accepted.

## Context

Combining CI and CD inside Jenkins would move approval, promotion and runtime policy outside the platform domain.

## Decision

Jenkins performs checkout, test, build, SBOM, vulnerability scan, sign and publish. It returns immutable artifact/evidence references. netCI/Temporal performs policy check, approval, deploy, health check and rollback.

## Consequences

Promotion never rebuilds. Jenkins pipelines cannot deploy directly to target environments. netCI needs authenticated callbacks/polling and idempotent reconciliation.

## Verification

E2E evidence links a single digest from CI through staging and production deployment.
