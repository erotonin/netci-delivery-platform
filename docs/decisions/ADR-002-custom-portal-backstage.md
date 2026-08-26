# ADR-002: Custom Portal and Backstage

Status: Accepted.

## Context

The project needs a focused demo UI and must also show that another developer portal can consume the same platform API.

## Decision

The React Custom Portal is the primary UX. Backstage remains a Software Template experiment through the netCI API/proxy; no domain logic is implemented in a scaffolder action.

## Consequences

Portal delivery is not blocked by Backstage setup. Both clients share OpenAPI semantics; Backstage status/log plugins are future scope unless mentor requires them.

## Verification

Portal loads the live catalog and creates/runs an application; Backstage creates the same resource shape and outputs the returned application ID.
