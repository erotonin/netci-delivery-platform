# ADR-007: Ephemeral agent isolation

Status: Accepted.

## Context

Shared agents leak workspaces/dependencies between projects and make cleanup/security claims weak.

## Decision

Each build receives a dedicated Kubernetes pod and workspace. Controller executors are zero. Cleanup covers success, failure and cancel; caches are project-scoped and external to workspace.

## Consequences

Provisioning adds latency and Kubernetes/RBAC/image dependencies. Cache and agent timing must be measured separately. Host Docker socket mounting is excluded from the production path.

## Verification

ISO-01 records unique pod/workspace identities and confirms deletion on all terminal paths.
