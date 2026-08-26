# ADR-001: System boundary

Status: Accepted for the local reference implementation.

## Context

Using Jenkins jobs as the system of record would couple application identity, policy and delivery history to one execution provider.

## Decision

netCI owns applications, pipeline/deployment lifecycle, policy, approval, audit, workflow and adapter contracts. Jenkins is a CI execution engine. Portal and Backstage are API clients.

## Consequences

Provider IDs are stored only as external references. Replacing Jenkins or a UI does not change domain identity. netCI must persist and reconcile its own state.

## Verification

Domain tests run without Jenkins, and clients never call Jenkins endpoints directly.
