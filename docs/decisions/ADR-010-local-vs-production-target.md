# ADR-010: Local and production target separation

Status: Accepted.

## Context

kind, local Registry and same-host controllers are useful for reproducibility but do not model Viettel production topology or failure domains.

## Decision

The reference environment is Ubuntu 24.04 + Docker Compose + kind + KVM/libvirt with separate Docker/Systemd VMs. Production provider names, identity, secrets, registry and Kubernetes/AI Platform integrations remain configurable replacement points.

## Consequences

Documentation/evidence must say “local reference implementation,” not production-ready HA. Production mapping requires mentor/platform contracts and a separate security/operations review.

## Verification

Local E2E passes without hardcoded production endpoints; assumptions and gaps are explicit in `docs/assumptions.md`.
