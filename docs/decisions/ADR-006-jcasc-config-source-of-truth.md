# ADR-006: JCasC configuration source of truth

Status: Accepted.

## Context

Manual controller configuration is not reviewable or reproducible and makes A/B drift likely.

## Decision

Jenkins image, exactly pinned plugins, jobs, shared library, security/cloud config and controller overlay live in Git. JCasC documents are supplementary and may not redefine the same key. Secrets are credential references.

## Consequences

Controller changes require review/static validation and a destructive rebuild rehearsal. UI-only changes are drift and must be removed or codified.

## Verification

REBUILD-01 deletes/recreates a controller from a commit and proves expected config/job/plugin inventory.
