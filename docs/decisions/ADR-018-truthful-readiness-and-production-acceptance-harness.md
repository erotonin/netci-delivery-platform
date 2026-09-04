# ADR-018: Truthful Readiness Probing and Production Acceptance Harness

## Context

In production environments, standard liveness checks (`/livez` or legacy `/healthz`) often obscure whether an Internal Developer Platform is genuinely capable of accepting and processing deployment requests. A false-green status reported when external orchestrators (Jenkins, Temporal), databases (PostgreSQL), catalogs (DCIM), or security verifiers (Cosign) are degraded leads to silent failures and bad routing decisions by ingress controllers or load balancers.

Phase P0.5 requires:
1. Architectural separation between liveness (`/livez`), readiness (`/readyz`), and authenticated operator diagnostics (`/operator/health`).
2. Readiness must fail closed (HTTP 503) when mandatory dependencies are down.
3. Unconfigured optional dependencies must be reported truthfully as `not_configured` or `optional` rather than disguised as healthy green.
4. An automated production acceptance harness (`scripts/production_acceptance_harness.py`) that executes real infrastructure checks across 9 critical gates and persists immutable evidence JSON and JUnit XML reports without substituting runtime mocks.

## Decisions

### 1. Distinct Health Endpoint Topology
- **`/livez`**: Minimal endpoint verifying that the FastAPI process is running and accepting TCP connections (`HTTP 200`).
- **`/readyz`**: Evaluates active dependencies with timeouts and circuit breakers. If PostgreSQL or mandatory configured engines are unreachable, fails closed with `HTTP 503 Service Unavailable`.
- **`/operator/health`**: Requires `Role.PLATFORM_ADMIN` authentication. Yields comprehensive diagnostic telemetry while strictly redacting all secrets, credentials, and sensitive internal paths.

### 2. Truthful Dependency Status
- Unconfigured integrations (e.g. `NETCI_DCIM_BASE_URL` unset, or `NETCI_SIGNATURE_VERIFY_MODE=none`) are reported with status `not_configured` (optional: true). They are never masked as fake operational "ok".

### 3. Production Acceptance Harness
- Implemented `scripts/production_acceptance_harness.py` to evaluate the 9 mandatory P0 gates:
  1. `persistence_across_api_restart`
  2. `oidc_role_and_team`
  3. `dcim_inventory_lookup`
  4. `jenkins_checkout_build_push`
  5. `sbom_trivy_cosign_verification`
  6. `temporal_workflow_restart_resume`
  7. `ansible_host_and_target_namespace`
  8. `deployment_failure_and_rollback`
  9. `backup_and_restore_verification`
- Generates JSON (`evidence/production_acceptance_<timestamp>.json`) and JUnit XML (`evidence/acceptance.xml`) containing:
  - Commit SHA
  - Image digest
  - Timestamp
  - Per-gate results (PASS, FAIL, BLOCKED)
- Missing external credentials/infrastructure are reported as `BLOCKED` with specific prerequisites, never marked with a false `PASS`.

## Status

Phase P0 is fully code-ready, with all unit, integration, concurrency, backup drill, and contract tests passing. Live verification against external clusters (Jenkins/Temporal/DCIM) correctly reports `BLOCKED` in local test environments until configured.
