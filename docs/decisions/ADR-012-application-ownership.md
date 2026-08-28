# ADR-012: Applications are owned by a team, and roles alone are not authorization

Status: Accepted.

## Context

[ADR-011](ADR-011-authentication-seam.md) gave netCI identity and roles. Roles answer *what
kind of thing* someone may do: read, create, run a pipeline, approve production. They do
not answer *which applications* they may do it to.

For a single team that distinction does not exist. For an organisation it is most of the
question. With global roles:

- any `developer` can start any team's pipeline, including one they have never seen;
- any `reviewer` can approve any team's production release, including one they have no
  context to judge;
- any `reviewer` can roll back any team's deployment.

Separation of duties still held — the requester could not approve their own release — but
it was satisfied by *any* second reviewer in the company, which is not what an approval is
supposed to mean.

## Decision

An application carries an `owner_team`. A principal carries the teams it belongs to:
`teams:` in the token file, or the identity-provider group claim named by
`NETCI_OIDC_TEAMS_CLAIM` (default `groups`). `require_team_access` is checked wherever
someone **acts** on an application — starting a pipeline, approving a deployment, rolling
one back.

Four choices worth stating, because each could reasonably have gone the other way:

**Reads are not team-scoped.** A delivery platform is more useful when everyone can see the
state of the estate, and visibility carries far less risk than action. Scoping reads would
also make the dashboard, DORA metrics and audit trail per-team, which is a different product.

**A platform-admin is not team-scoped.** Administering the platform must not require joining
every team in the organisation. This is the role that can act anywhere, and it is the role
to grant sparingly — `scripts/netci_token.py` says so when issuing one.

**An unowned application is unrestricted.** Applications created before ownership existed
have no owner, and assigning them to a team nobody chose would be worse than leaving them
alone. Role checks still apply; team checks do not. `NETCI_REQUIRE_APPLICATION_OWNER=true`
closes the door once every application has an owner: an unowned one becomes platform-admin
only, and new ones must name a team. The door is open only for as long as the migration
needs it.

**You may only hand an application to a team you belong to.** Otherwise `ownerTeam` is a
field anyone can set to anything, which is a label rather than a control.

## Consequences

Team membership is not managed by netCI. It comes from the token file or the directory, and
netCI only compares strings — so a team rename is a directory change plus an `owner_team`
update, and netCI has no opinion about which is the source of truth. That is deliberate:
netCI holding its own team model would immediately be a stale copy of the real one.

Ownership is flat. There is no hierarchy, no group nesting beyond what the IdP flattens
into the claim, and no per-environment delegation — "team A may deploy to staging but not
production" is not expressible. Roles cover the environment axis; teams cover the
application axis; the two do not compose further.

`applications.owner_team` is nullable (migration `0004`), so the upgrade changes no existing
behaviour until an operator opts in.

## Alternatives considered

**Per-application ACLs.** More flexible and much worse to operate: every application grows a
list that drifts from the org chart, and offboarding means finding every list someone is on.
Team membership already lives in the directory and is already maintained.

**Scoping reads as well.** Rejected above. It is also the change most likely to be regretted
quietly, because the cost — people cannot see why a shared dependency is failing — shows up
as friction rather than as an incident.

**Making platform-admin team-scoped.** Rejected: it produces an administrator who cannot
administer, and the workaround is adding them to every team, which is the same permission
with more steps and less visibility.

## Verification

`backend/tests/test_authorization.py` runs the API with two teams and a team-less
platform-admin, and asserts: an application can only be handed to a team you belong to;
another team's pipeline cannot be started; a reviewer from another team cannot approve your
production release; another team's deployment cannot be rolled back; unowned applications
keep working; and `NETCI_REQUIRE_APPLICATION_OWNER` closes both doors — existing unowned
applications become platform-admin only, and new ones must name a team.

`backend/tests/test_api.py::test_adding_owner_team_did_not_rehash_existing_idempotency_keys`
covers the upgrade hazard this change introduced: the idempotency record stores a hash of
the request payload, so folding in a new field unconditionally turns every existing key's
next replay into `IDEMPOTENCY_KEY_REUSED`. It is folded in only when set.
