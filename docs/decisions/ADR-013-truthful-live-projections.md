# ADR-013: Truthful live projections and workflow completion

Status: Accepted.

## Context

The Portal previously filled missing integrations and empty history with reference records. Temporal could finish a runtime deployment without updating the API, and supply-chain evidence disappeared when the API restarted. These behaviors made a healthy-looking screen diverge from operational truth.

## Decision

Production composition starts with no demo records. Reference data is available only when `NETCI_DEMO_DATA=true`, which tests opt into explicitly. Unconfigured external catalogs return `not_configured` plus an empty collection. UI views render API data, loading, empty or error states; they do not fall back to operational-looking fixtures.

Security evidence, its policy decision, audit record and log entry are committed durably. A Temporal workflow reports its terminal health result back to the authenticated deployment callback, and repeating the same terminal callback is idempotent.

## Consequences

A fresh installation looks empty until an operator registers real systems and integrations. That is intentional. DCIM installations must implement the documented REST adapter contract, including system/module server inventory, and configure `NETCI_DCIM_BASE_URL`. Temporal workers require the API URL and pipeline key, because a worker unable to report completion is not considered configured. Runtime targets without a health provider remain `unknown`.

## Verification

PostgreSQL durability tests restart the platform and compare evidence plus audit identity. Temporal tests assert that the core deployment id and terminal result reach the reporter. Portal tests assert explicit unconfigured DCIM and unknown target health instead of fixture status.
