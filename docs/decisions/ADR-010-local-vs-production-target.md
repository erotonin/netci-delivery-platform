# ADR-010: Local and production target separation

Status: Accepted.

## Context

kind, local Registry and same-host controllers are useful for reproducibility but do not model Viettel production topology or failure domains.

## Decision

The reference environment is Ubuntu 24.04 + Docker Compose + kind + KVM/libvirt with separate Docker/Systemd VMs. Local Kubernetes uses the existing `dev`, `staging` and `prod` namespaces. The production Kubernetes boundary is distribution-neutral: a conformant Kubernetes API plus one least-privilege service account/kubeconfig and an explicit target namespace per environment. Core services never guess a production namespace. Cluster distribution, credential issuer and namespace value remain deployment configuration rather than domain rules. Production identity, secrets, registry and Kubernetes/AI Platform integrations remain configurable replacement points.

## Consequences

Documentation/evidence must say “local reference implementation,” not production-ready HA. Unknown Viettel-specific values do not block the local handover; integration replaces the configured provider values and still requires a separate security/operations review.

## Verification

Local E2E passes without hardcoded production endpoints. Kubernetes tests prove environment-scoped kubeconfig use, least-privilege access and use of the explicitly configured target namespace; assumptions and gaps are explicit in `docs/assumptions.md`.
