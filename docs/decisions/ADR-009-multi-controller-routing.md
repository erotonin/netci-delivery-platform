# ADR-009: Multi-controller routing

Status: Accepted for local simulation.

## Context

Two controllers require deterministic routing and safe behavior when one becomes unhealthy.

## Decision

The router selects by health, queue, capacity and capability, records its decision and reroutes new builds from an unhealthy controller. An in-flight build is not migrated without checkpoint/idempotency support.

## Consequences

Health hysteresis, stable request identity and audit/evidence are required. Two controllers on one host demonstrate control logic but not production high availability.

## Verification

HA-01/MTTR-01 record failure, detection, reroute, completion and rejoin timestamps.
