# ADR-009: Multi-controller routing

Status: Accepted for local simulation.

## Context

Two controllers require deterministic routing and safe behavior when one becomes unhealthy.

## Decision

Two Jenkins controllers on the same Ubuntu host are accepted for the local reference demo. They run as independently addressable controller instances with separate ports, JCasC state, work queues and persistent volumes. The router selects by health, queue, capacity and capability, records its decision and reroutes new builds from an unhealthy controller. An in-flight build is not migrated without checkpoint/idempotency support.

## Consequences

Health hysteresis, stable request identity and audit/evidence are required. The failure drill must stop only one controller and prove that new work is routed to the other. Two controllers on one host demonstrate control logic and process isolation, but not host-level fault tolerance or production high availability.

## Verification

HA-01/MTTR-01 record failure, detection, reroute, completion and rejoin timestamps.
