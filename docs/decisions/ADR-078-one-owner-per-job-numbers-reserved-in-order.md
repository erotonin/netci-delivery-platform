# ADR-078: One owner per job; build numbers reserved before a build starts

- Status: accepted for the numbering (store and plugin built and tested); finished-build replication,
  routing and adoption are still design
- Date: 2026-10-13
- Refines: ADR-076

## Context

After ADR-076 phase 3 the replicas hold the same jobs and users, but each still builds on its own:
- each home keeps its own `nextBuildNumber`, so r0 and r1 would both run build #5 of a job;
- a finished build exists only in the home that ran it, so the other replica's job page lacks it;
- a running build's live state (its execution, its console) is in one JVM.

The mentor's constraints that matter here: Pipeline only (durable), inbound agents, no builds on a
controller.

## Decision

1. **One owner per job at a time**, holding a lease in netci-homesync (PostgreSQL), with an epoch
   that grows on every change of hands (the fencing token). Only the owner starts builds of the job.
   A job is claimed by the replica that is asked to build it and has no live owner; the owner
   renews while it is alive. Numbers then stay what people expect: 1, 2, 3 per job.
2. **A number is reserved before the build takes it.** Jenkins assigns a build its number when the
   executor creates the run, after any plugin could intervene, so the reservation happens one step
   earlier, when the queue decides the item may run (`QueueTaskDispatcher.canRun`): the owner
   records `(job, next number)` in the store under its lease epoch, and only then lets the item
   start. Not `canTake`: a Pipeline run is a flyweight task, started without any dispatcher's
   `canTake` being asked -- the unit test showed no number reserved through it. While a reservation is not yet consumed by a started run, other items of that job
   wait ("waiting for a build number"), so the run takes exactly the reserved number. A replica
   that lost its lease cannot reserve, so it cannot start a build -- a partitioned old owner stops
   by itself. **Every number is used once**, including across a change of owner, which starts from
   the store's highest reservation.
3. **A finished build is published**: its directory (`build.xml`, the log, the flow nodes) to the
   store; the other replicas write it into their home and call
   `LazyBuildMixIn._getRuns().recognizeNumber(n)` (public in 2.555), so every replica lists every
   build without reloading the job.
4. **A running build's pages go to its owner**: a replica that does not have the build answers with
   a redirect that sets the balancer's cookie to the owner (the sign-in follows, ADR-077; HAProxy's
   cookie needs `preserve`, or it removes the cookie the replica sets). **A "Build now" on a
   non-owner is made on the owner by that replica**, for the same person: redirecting the browser's
   POST failed in the lab, because its CSRF crumb is bound to the first replica's session and the
   owner answered 403. The first replica has checked the crumb; it calls the owner with the same
   credential (an API token is valid everywhere, users being replicated) or a shared session it
   signs for that person, takes the owner's own crumb, and relays the answer, with the owner's
   Location rewritten to a path on the entry point.
5. Logs of finished builds are kept in PostgreSQL in the lab, with a size limit that is enforced and
   said; a site would put them in an object store (the corp lab has one).

## Phase 5, built on this

When a job's owner stops renewing, its machine or pod is fenced first (supervisor and fence,
ADR-060/069); then a survivor takes the lease with a new epoch, writes the running build's
replicated state into its home and resumes it. The agent running the step reconnects through the
VIP and is routed by `Node-Name` to the new owner. That is where "a build is not lost when a
machine dies" is decided, and it is measured there, not claimed here.

## Phase 5 by hand: a running build continued by the surviving replica (done)

`lab/aa/phase5_manual.py`, before any code automates it. `long` runs 90 one-second ticks in an `sh`
step on agent-0 (an inbound agent, two executors, WebSocket through the VIP), started on r0. At
tick 15 r0's copy of the running build is copied into r1's home; r0 is killed and its machine
cordoned (dead, fenced). Evidence `aa-phase5-manual-20261013T164325Z.json`:

- agent-0 reconnected through the VIP; r0 being down, the balancer gave it to r1 **after 4.5 s**;
- on r1, the build's number made known (`recognizeNumber`) and the run loaded: it resumed itself
  ("Resuming build ... Waiting for reconnection of agent-0 ... Ready to run") -- **r1 was not
  restarted**;
- it **finished SUCCESS** on r1: **90 ticks, none missing, none twice**, all on agent-0. The `sh`
  process had kept running on the agent; r1 read its output on from the offset the copy held.

What the first try taught (`aa-phase5-manual-20261013T164147Z.json`): loading the run is the whole
adoption. Calling `onResume()` on its steps as well resumed them a second time ("Already
resumed"), the `sh` step interrupted its process, and the build failed.

What automation must add, from this: the running build replicated while it runs (here: one copy by
hand); adoption only once the old owner is fenced (here: killed and cordoned by hand); and a
returning replica must not resume, from its stale copy, a build that was adopted elsewhere (here:
its copy was replaced by hand before it came back).

## Rejected

- **Numbers striped by replica** (r0 odd, r1 even): no coordination needed, but numbers that jump
  and change meaning when a replica is added.
- **Reserving the number after the build has it** (`RunListener.onInitialize`): too late -- if the
  record failed, a new owner could take the same number.
- **Any replica builds any job, numbers from a database sequence**: Jenkins gives a plugin no hook
  between "this item starts" and "this run has number N" other than the queue's dispatch step,
  which this design uses; without an owner, two replicas' dispatches would still race for one
  job's next number.
