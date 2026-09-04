# ADR-026: Comprehensive Production Readiness Certification & Automated Platform Verification

## Status
Accepted

## Context
As the netCI Delivery Platform achieves complete lifecycle maturity across all 13 foundational phases, engineering leadership and site reliability operations require an automated, unassailable certification harness. The platform must never rely on manual assertions, superficial health checks, or unvalidated claims of readiness.

Key challenges addressed:
1. **Multi-Phase Architectural Integrity**:
   - The platform encompasses 13 architectural milestones: canonical PostgreSQL persistence, workload identity trust boundaries, deterministic state machines, release immutability, truthful dependency health, SCM webhooks, reconcilers, DCIM lifecycle, Prometheus observability, multi-module DAG progressive delivery, enterprise governance & admission control, service catalog & self-service, and final release certification.
   - An automated audit runner must verify all 28 mission-critical invariant gates in a single unified execution.

2. **Clean-Room Enforcement & Zero Fake-Green Telemetry**:
   - Production runtimes must never fall back to mock or fixture data in default paths.
   - External dependencies (DCIM, Cosign, Vault/Secrets) must report `not_configured` or fail closed when unconfigured or unreachable, completely eliminating false positives.

3. **Continuous Reproducible Auditing**:
   - Audit runs must produce verifiable, machine-readable evidence (`evidence/production_readiness_audit.json`) containing timestamps, individual check statuses, and failure metadata.

---

## Decision

1. **Automated Audit Suite (`scripts/production_readiness_audit.py`)**:
   - Implemented an exhaustive audit runner covering all 13 platform phases:
     - **Phase 1 (Database)**: 18 canonical SQL migrations, 34 critical table definitions, and live PostgreSQL verification.
     - **Phase 2 (Workload Identity)**: Ed25519/HMAC machine token minting & validation, Jenkins scope fencing (`deployment:result` denied), build-input boundary filtering.
     - **Phase 3 (State Transitions)**: Strict forward sequencing (no jumping `QUEUED` -> `SUCCEEDED` or `PENDING_APPROVAL` -> `HEALTHY`), and mandatory two-phase rollback (`ROLLBACK_IN_PROGRESS` before `ROLLED_BACK`).
     - **Phase 4 (Release Immutability & Clean-Room)**: Verification that SCM provider table contains no mock adapters, DCIM returns unconfigured without phantom hosts, and runtime code has zero mock/fake data fallbacks.
     - **Phase 5 (Truthful Readiness)**: Unconfigured external services return `not_configured` without false greens.
     - **Phase 6 (SCM Webhooks)**: Cryptographic HMAC-SHA256 signature verification and rejection of forged webhooks.
     - **Phase 7 (Pipeline Lifecycle)**: Watchdog `Reconciler` existence for out-of-band state recovery.
     - **Phase 8 (Versioned Config & DCIM)**: Fail-closed DCIM catalog target revalidation and error handling on network disconnect.
     - **Phase 9 (Observability & Outbox)**: Prometheus `MetricsRegistry` exposition of HTTP requests, pipeline durations, and deployment outcomes.
     - **Phase 10 (DAG & Progressive Delivery)**: Kahn's topological sort for parallel release waves, cyclic dependency detection/rejection, and Canary SLO evaluation.
     - **Phase 11 (Enterprise Governance & Admission)**: Deterministic 0-100 risk scoring, dual-control break-glass approvals, and Kubernetes admission controller mutable tag rejection.
     - **Phase 12 (Catalog & Self-Service)**: Service catalog circular dependency detection, Golden Path template schema validation, preview environment TTL clamping (max 72h), and self-service fail-closed provider contract.
     - **Phase 13 (Certification Final)**: Authoritative OpenAPI 3.1.0 contract verification, release checklist compliance, and complete architectural ADR corpus (>= 25 records).

2. **Standardized Evidence Output**:
   - Audits write atomic, timestamped JSON artifacts to `evidence/production_readiness_audit.json`, suitable for ingestion by compliance monitors and deployment gating pipelines.

---

## Consequences

### Positive
- Single command (`DATABASE_URL=... python scripts/production_readiness_audit.py`) provides end-to-end platform certification.
- Enforces strict compliance with core platform invariants across CI/CD and deployment environments.
- Protects against regression of safety boundaries, fail-closed contracts, and state transition guarantees.

### Trade-offs
- The audit suite requires access to either the local/staging PostgreSQL instance or in-memory equivalents for database and table checks.
