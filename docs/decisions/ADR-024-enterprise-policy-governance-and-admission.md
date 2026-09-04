# ADR-024: Enterprise Policy Governance, Security Waivers, Dual-Control Break-Glass, Quotas, and Kubernetes Admission Control

## Status
Accepted

## Context
As netCI transitions to an enterprise-grade delivery platform (Phase 11 / P2.2), governance controls must be formalized, deterministic, durable, and enforceable across multiple boundaries (CI build admission, production approvals, deployment concurrency, and runtime Kubernetes cluster admission):
1. **Durable Policy Audit Trail**: Policy verdicts previously existed only in transient evaluation logs or memory. Enterprise compliance and security audits require permanent, immutable database records (`policy_decisions`) of every evaluated rule, check status, risk score, and justification.
2. **Time-Boxed Vulnerability Waivers (`security_exceptions`)**: Supply-chain security gates cannot function without an authentic exception mechanism. Teams needing to ship with known CVEs (e.g. pending upstream patch) previously risked circumventing gates entirely. Waivers must be pinned to exact CVEs and immutable sha256 digests, owned by an engineer, approved by a separate reviewer, and strictly time-boxed (`expires_at`).
3. **Two-Person Emergency Override (`break_glass_requests`)**: During P0 incidents, systems may need emergency bypass of non-critical gates. Such bypasses must require dual-control separation of duties (`requested_by != approved_by`), an active incident ticket, short-lived bounded leases (TTL <= 4h), and permanent audit tracking. Self-approval must be strictly prohibited.
4. **Multi-Scope Concurrency Quotas (`resource_quotas`)**: Uncontrolled parallel pipeline runs or deployments exhaust cluster compute and network resources, leading to noisy-neighbor outages. A hierarchical quota system (application -> team -> global) is needed to bound concurrent runs and deployment operations.
5. **Kubernetes Dynamic Admission Control (`/admission/validate`)**: Cluster-level enforcement is necessary to stop untrusted or unverified container images from running in production pods, rejecting unpinned mutable tags (e.g. `:latest`) and admitting only images with verified SBOMs, clean scans, or active break-glass waivers.

## Decision

1. **Database Schema Migration 0017 (`backend/migrations/0017_policy_engine_governance_and_admission.sql`)**:
   - `policy_decisions`: `(id, scope, target_type, target_id, allowed, reason, risk_score, checks, rules_evaluated, evaluator, evaluated_at, metadata)`.
   - `security_exceptions`: `(id, cve, artifact_digest, owner, reason, approved_by, status, created_at, expires_at, revoked_at, revoked_by)`.
   - `break_glass_requests`: `(id, target_type, target_id, requested_by, reason, incident_ticket, status, approved_by, created_at, approved_at, expires_at)`.
   - `resource_quotas`: `(id, scope, scope_id, max_concurrent_pipelines, max_concurrent_deployments, max_production_requests_per_day, created_at, updated_at)`.
   - Added btree and composite indexes on all target lookups and cursor ordering columns.

2. **Unified Policy Engine & Risk Scoring (`backend/app/policy/`)**:
   - `risk.py`: Deterministic 0-100 `RiskCalculator` based on target environment, multi-module blast radius, automation test coverage, active CVE waivers, rollback strategy, and break-glass invocation.
   - `quota.py`: Hierarchical `QuotaEnforcer` checking active pipeline and deployment concurrency limits against application, team, and global quotas.
   - `break_glass.py`: `BreakGlassService` enforcing strict dual-control authorization (refusing self-approval with 403 `SEPARATION_OF_DUTIES`), bounded TTL leases (1 to 240 minutes), and active override lookup.
   - `engine.py`: `PolicyEngine` and `BuiltinPolicyEngine` orchestrating checks across artifact admission, production requests, and deployment gates, recording all decisions to PostgreSQL.

3. **Kubernetes Dynamic Admission Webhook (`backend/app/admission.py`)**:
   - Implemented `AdmissionController.handle_admission_review` for Kubernetes `AdmissionReview` v1 webhooks.
   - Rejects unpinned mutable image tags in production namespaces with HTTP 403.
   - Verifies container image digest against pipeline runs and stored CI evidence (Syft SBOM, Trivy vulnerability scan, Cosign signature).
   - Permits emergency admission if an active break-glass request covers the artifact digest.

4. **REST API & Contract Parity (`backend/app/main.py`, `api/openapi.yaml`)**:
   - `GET /policy/decisions`: Paginated policy decisions with cursor pagination.
   - `GET /security-exceptions`, `POST /security-exceptions`, `POST /security-exceptions/{id}/revoke`: Lifecycle management of vulnerability waivers with owner-approver dual control and expiry validation.
   - `POST /break-glass/requests`, `POST /break-glass/requests/{id}/approve`, `GET /break-glass/active`: Two-person emergency break-glass procedure.
   - `GET /quotas/{scope}/{scopeId}`, `PUT /quotas/{scope}/{scopeId}`: Resource quota management.
   - `POST /admission/validate`: Kubernetes admission webhook validation endpoint.
   - Production request approval (`POST /production-requests/{requestId}/approve`) evaluates PolicyEngine and injects policy verdict & risk score.
   - Full OpenAPI 3.1.0 contract parity verified in `tests/contract/test_openapi.py`.

5. **Portal UI Integration (`frontend/src/ProductionRequestsPage.tsx`, `frontend/src/api/netciClient.ts`)**:
   - Extended `netciClient.ts` with Phase 11 types and client functions.
   - Added Enterprise Governance & Policy Verification card to Request Details modal, displaying real-time Risk Score badge (0-100), dual-control status, resource quota verification, immutable digest pinning, and break-glass indicators.

## Consequences
- Every policy decision across CI, CD, and Kubernetes admission is auditable, deterministic, and permanently recorded in PostgreSQL.
- Known vulnerabilities can be safely waived without disabling gates or creating permanent security holes.
- Production incidents can be remediated via two-person dual-control break-glass while retaining non-repudiable audit logs.
- Kubernetes clusters can point validating webhooks to netCI to prevent unverified images from being deployed.
