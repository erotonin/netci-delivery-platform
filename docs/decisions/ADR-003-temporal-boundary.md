# ADR-003: Temporal boundary

Status: Accepted.

## Context

Making every request a workflow adds latency/complexity, while long delivery flows need durable wait, retry and compensation.

## Decision

Use direct ports/adapters for short reads and commands such as status/log/abort. Use Temporal for multi-step workflows requiring retry, timeout, approval wait, resume or rollback compensation.

## Consequences

Workflow inputs/results must be serializable and activities idempotent. A Temporal worker is required for workflow E2E, but API health does not depend on wrapping simple reads in workflows.

## Verification

Workflow tests cover replay/retry/approval; direct operations have adapter contract tests.
