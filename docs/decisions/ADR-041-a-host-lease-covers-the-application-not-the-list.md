# ADR-041: A host lease covers the application in that environment, not the list of hosts

Status: Accepted. Supersedes the host-set half of the lease key introduced with
migration 0010.

## Context

Migration 0010 exists because "two approvals seconds apart produced two workflows writing
to the same hosts, and the surviving state was whichever finished last". The lock it
added is a partial unique index on `(application_id, environment, target)`, and
`DeliveryPlatform.lease_target` computed `target` for a host deployment as the sorted,
comma-joined host list.

That key catches two releases naming the **identical** set. It does not catch two whose
sets **overlap**, because two different lists are two different keys. A module whose
registered configuration changed between two releases -- one host added, one retired --
produces exactly that: run A targets `[h1, h2]`, run B targets `[h1, h3]`, both are
granted a lease, and both write to `h1`. The failure migration 0010 was written to stop,
reachable through the supported configuration-revision flow.

(The key was also truncated to 200 characters, so two long host lists sharing a prefix
collapsed onto one key -- over-locking, harmless, and a sign the key was carrying more
than a key should.)

## Decision

**A host-based deployment locks every host-based deployment of the same application in
the same environment.** `lease_target` returns `hosts:<application_id>` rather than the
host list. The namespace key is unchanged, so Kubernetes releases that deliberately run
beside each other are unaffected. The host list moves into the
`deployment.lease_acquired` audit record, where it is information rather than identity.

## Why this is not a loss of capability

`target_hosts` is the set of servers registered for one module in one environment
(`portal.py`: `configured_servers = list(target.get("servers") or [])`). One module plus
one environment yields one list, so two concurrent runs of that module in that
environment always carry the same list -- they already collided. The only way to get two
*different* lists is a configuration change between them, which is the overlap this
refuses on purpose.

The strategies that genuinely run a second release beside a live one are canary
(ADR-031) and blue/green (ADR-035). Both are Kubernetes-only -- `coordinator.py` refuses
a canary on any other runtime, and a colour is a Helm release with `netci.track` -- and
both key on `namespace:`, which this does not touch.

A test asserted the opposite: "one service with a blue and a green host set can deploy to
both at once". It was rewritten rather than preserved, because it described an
arrangement the product cannot produce and the key that allowed it is the defect.

## What was rejected

**One lease row per host.** Strictly finer, and it would have kept per-host concurrency
if anything could produce it. It moves the fencing token, which a deployment carries
exactly one of, onto an ambiguous "primary" row among N acquired in the same transaction
with the same `acquired_at`; release, heartbeat and expiry recovery each become
all-of-N operations. That is a restructuring of the platform's most safety-critical code
to preserve an arrangement no caller can reach.

**An advisory lock plus an overlap check.** Correct, but it puts a check-then-act back
into the path the unique index was introduced to own, and the host list would have to
survive the 200-character column to be checked at all.

## Consequences

- Two host releases of one application in one environment serialise; the second is
  refused `DEPLOYMENT_TARGET_BUSY` rather than both proceeding.
- A lease held under the old key is not recognised under the new one. There is no
  deployment in flight on this host; on a system where there were, drain before deploying
  this change.
- Pinned by `test_two_releases_whose_host_sets_overlap_do_not_both_get_to_deploy`, which
  was written failing against the old key, and by
  `test_two_namespaces_of_one_application_still_do_not_collide`.
