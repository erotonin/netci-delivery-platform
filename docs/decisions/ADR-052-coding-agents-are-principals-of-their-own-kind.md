# ADR-052: Coding agents are principals of their own kind

Status: Accepted.

## Context

Coding agents now open branches, push commits and start builds through netCI. To netCI
they looked like the developer whose token they carried, or like an anonymous SCM sender.
Nothing stopped an agent from holding a reviewer role and approving a change, including
its own, and separation of duties compared two names that might both be the agent. Nothing
put the one build a person was waiting on ahead of a storm of agent builds.

## Decision

1. **Every principal has a kind, `human` or `agent`, and the server decides it**: from the
   identity provider (membership of `NETCI_OIDC_AGENT_GROUPS`, or a token issued to an OAuth
   client in `NETCI_OIDC_AGENT_CLIENTS`, read from `azp`), or from `kind:` in the token file.
   Never from a commit's author, which anyone can write.
2. **An agent holds at most developer and viewer.** An agent the role map would make a
   reviewer or platform admin is refused: `403 AGENT_ROLE_NOT_ALLOWED` for an OIDC token,
   a configuration error for the token file. It is not quietly downgraded, because a token
   that says "reviewer" and works as "developer" hides the mapping mistake. So an agent
   approves nothing: deployments, production requests, configuration, break-glass, VEX.
3. **Three developer actions are a person's**: requesting break-glass, configuring an SCM
   integration (which repository may start builds and the secret that proves it), and
   deleting a system.
4. **People go first in the admission queue (ADR-050).** A run records `trigger.actorKind`:
   the principal's kind for a run started through the API, and `agent` for a webhook whose
   sender is in `NETCI_AGENT_SCM_LOGINS`. That sender is who the SCM says pushed, in a
   signed webhook. It orders the queue and grants nothing.

## Consequences

- Protected-branch merges are the SCM's to enforce (branch protection), not netCI's. netCI
  controls what it decides: builds, deployments and approvals.
- Audit records carry the actor's subject, not its kind; the kind is on `/me` and on runs.
- An agent may still request a production release. A person must approve it, and separation
  of duties stops that person being the requester.
