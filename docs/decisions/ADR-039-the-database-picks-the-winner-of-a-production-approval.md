# ADR-039: The database picks the winner of a production approval

Status: Accepted.

## Context

`PortalService.approve_request` read a production request, checked its status was
`waiting_approval`, ran the separation-of-duties and artifact gates, and then handed the
request to `ReleasePlanCoordinator.start_release`, which marked wave 1 in progress and
dispatched its modules.

The status check and the status write were separate statements in separate transactions.
Between them a second reviewer could read the same `waiting_approval` and pass the same
check. Nothing in `store/postgres.py` took a row lock -- a search for `FOR UPDATE`
returned nothing -- so two reviewers pressing approve at the same moment could both
proceed and one release could be dispatched into production twice.

`start_release` made this worse in two further ways. It wrote `status="approved"`
unconditionally, so calling it on a rejected or cancelled request dragged that request
back to approved. And it wrote no audit record at all: the whole multi-module saga
produced zero `AuditRecord`s, so a wave start left no trace of who started it.

## Decision

**A status transition that gates real work is a conditional UPDATE, not a read followed
by a write.** `PlatformSession.claim_portal_request(request_id, from_status, to_status)`
issues `UPDATE ... WHERE id = %s AND status = %s` and reports whether it changed a row.
Exactly one caller sees `True`; the loser is told the same thing a late approver is told.
`approve_request` claims the request inside the same transaction as its gates, before
anything is dispatched.

`start_release` keeps the request's current status instead of forcing `approved`, returns
early when wave 1 is already `in_progress`/`succeeded`/`failed` (so a retried approval or
a replayed callback cannot dispatch twice), and writes a `release_plan.wave_started`
audit record naming the actor, the wave and its modules.

## What was rejected

**`SELECT ... FOR UPDATE` on the request row.** It would also be correct, but it holds a
lock for the duration of the gates -- the artifact and automation checks read other
tables -- and it leaves the "who won" decision implicit in lock ordering. A conditional
UPDATE states the precondition in the statement that depends on it.

**An application-level lock or a uniqueness table.** More moving parts for a guarantee
one WHERE clause already gives.

## Consequences

- `claim_portal_request` is on the session protocol and implemented by both the
  PostgreSQL and in-memory stores, so the in-memory store used by tests refuses the same
  second claim.
- `test_persistence_postgres.py::test_only_one_of_two_simultaneous_approvals_claims_a_production_request`
  runs two real connections against a barrier and asserts exactly one wins. It was
  verified to fail when the conditional UPDATE is replaced with the old read-then-write.
- Three coordinator tests cover idempotency, the audit record and the status no longer
  being forced back to `approved`.
