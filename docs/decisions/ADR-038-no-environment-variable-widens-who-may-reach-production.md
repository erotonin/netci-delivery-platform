# ADR-038: No environment variable widens who may reach production

Status: Accepted.

## Context

`require_environment_permission` restricts production deployment to `reviewer` and
`platform-admin`. That check has its own history: it was written once and never
invoked, which made it documentation rather than a control, and
`backend/tests/test_authorization.py::test_a_developer_cannot_run_a_production_pipeline`
exists because of it.

A change in the working tree reintroduced the same class of problem from the other
direction. It added `NETCI_ALLOW_DEVELOPER_PROD_CD`, which -- when set -- admitted
`developer` to production deployment. The flag appeared twice, in
`backend/app/policy/rules.py` and again in the `_reviewer_access` dependency in
`backend/app/main.py`, with no test, no documentation and no ADR. Two copies of an
authorization rule can drift apart, and only one of them needs to be widened.

## Decision

**Production access is stated once, in `policy.rules.PROD_CD_ROLES`, and no environment
variable widens it.** The API dependency reads that same frozenset, so the transport
layer cannot become more permissive than the rule it applies.

An environment variable is the wrong instrument for this. Granting it produces a
production deployment with no approver identity, no incident ticket, no expiry and no
audit record -- the two-person rule reduced to a formality that nothing records. An
operator reading the audit log afterwards cannot distinguish "a reviewer approved this"
from "the flag was on that day", which is the exact ambiguity this project exists to
refuse.

## What was rejected

**Keeping the flag but confining it to `NETCI_ENVIRONMENT=local`.** This is safe and was
considered. It was rejected because it buys very little: the lab already has reviewer
and platform-admin identities (`rae`, `pat`), so nothing in local development needs a
developer to deploy production. A control with a disabled-in-theory bypass still has to
be read, tested and reasoned about by everyone who touches it.

**Keeping the flag with a test and this ADR.** Rejected for the same reason: the
supported bypass already exists and is better in every respect.

## The supported bypass

`backend/app/policy/break_glass.py`. `BreakGlassService` requires an authenticated
requester, an explicit justification, an incident ticket and a second approver
(dual control), and the lease it grants is bounded (`DEFAULT_TTL_MINUTES = 60`,
`MAX_TTL_MINUTES = 240`) and written to the audit log. An emergency that genuinely
needs a developer in production goes through it, and leaves a record saying so.

## Consequences

- `NETCI_ALLOW_DEVELOPER_PROD_CD` is not read anywhere. Setting it does nothing.
- Two regression tests pin this:
  `test_no_environment_variable_widens_who_may_reach_production` (the env var is set and
  the request is still refused 403) and `test_the_prod_role_set_is_stated_once` (the API
  and the policy layer share one definition). Both were verified to fail when the flag
  was temporarily reintroduced, so neither passes vacuously.
