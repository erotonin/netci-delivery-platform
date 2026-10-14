# ADR-081: A run from netci-queue runs once across active-active replicas

- Status: accepted; unit- and store-tested, measured in the lab (below)
- Date: 2026-10-14
- Refines: ADR-063 (the durable run queue), ADR-078, ADR-079

## Context

The IDP is to submit builds through netci-queue (ADR-063, ADR-073), not to a Jenkins directly.
netci-queue dispatches a run to a controller's plugin (`/netci/dispatch`), follows it
(`/netci/run`), and decides from the answers. It was built for cells, one controller per cell, and
it reads three answers as final:
- a **409** means the run is refused for good;
- **absent** for a started run means it finished (result unknown);
- a **new controller session** with the run absent means the run was lost with that controller's
  queue, so it is dispatched again.

In front of active-active replicas these calls go through the VIP and land on either replica. Read
from the code, before any lab run, each of the three goes wrong without a fault:
- A dispatch on the replica that does not own the job is refused by Jenkins' queue (only the owner
  schedules, ADR-078). The plugin answered 409, so the run was refused for good.
- A lookup on that replica does not see a build running on the owner. It answered absent, so the
  run was marked finished while it ran.
- The worst case, when the owner dies with the run's build on it:
  1. the lease moves to the other replica;
  2. netci-queue sees a new session and dispatches again;
  3. the new owner has nothing yet (the build waits for adoption), and schedules a second build;
  4. the adoption resumes the first build, so the run runs twice.

## Decision

1. **Each call goes to the job's live owner** when that is another replica. The receiving replica
   makes the same call there with the caller's API token (users are replicated; API tokens need no
   CSRF crumb), marks it routed so it is never forwarded twice, and relays the owner's answer.
2. **The store records which build number a run was given.** The reservation made before a build
   takes its number (ADR-078) carries the run id.
   - A unique index refuses a run a second number, on any replica and for any job.
   - The item that asked for it is cancelled (by id: canRun's item is not the object the queue
     holds).
   - This is the guard that holds when everything else races.
3. **A replica that does not hold a run asks the store**, outside Jenkins' queue lock:
   - a number with a running copy, or with a finished build not yet listed here, is answered
     **started, building**;
   - a number and nothing else is answered **starting**, unless its replica is gone: this replica
     itself having restarted, or the fence saying so. Then the number is let go (the number stays
     used) and the run may be scheduled once more.
   - This replica never guesses a result.
4. **With replicas, a passing refusal is a 503, never a 409**: another replica owns the job, or the
   store cannot be reached. netci-queue retries a 503.

## Tests

Store (PostgreSQL):
- a run gets one number across replicas and jobs, and is told where it went;
- 20 rounds of two replicas reserving one run at once give exactly one winner. This fails without
  the unique index mapping;
- release, lookup and authentication over HTTP.

Plugin (fake store and fake owner):
- dispatch and lookup forwarded to the owner;
- a run with a build running elsewhere is reported started and never scheduled. With the store
  lookup disabled it was queued again (`"state":"queued","created":true`);
- a number this replica gave before restarting runs once, and the next number is used;
- a second item for a numbered run is cancelled;
- with the store down, 503 for both calls.

## Lab (`lab/aa/phase7_queue_probe.py`)

netci-queue in front of the replicas (`lab/aa/queue.sh`: two instances, its database on the HA
store, Jenkins reached through the machine-local balancer).

**First run, on netci26** (`aa-phase7-queue-20261014T095524Z.json`). The owner's machine (with the
store's primary) was powered off under `long`:
- `long` was SUCCESS, every tick once;
- six runs submitted while the machine was off were all SUCCESS, with one build each on both
  replicas;
- but 5 of 10 runs submitted as the machine came back were **refused for good**. The restarted
  replica, not yet caught up with the store, answered 409 like a lone Jenkins. Fixed in netci27:
  503 until caught up, and kept out of the balancer until then.

**On netci27** (`aa-phase7-queue-20261014T103155Z.json`): **17 of 17 runs SUCCESS, each with
exactly one build on each replica**:
- 10 steady;
- `long` with its owner's machine powered off at tick 10, every tick once;
- 6 runs submitted while the machine was off;
- `long`'s key submitted again: the same run.

While the store's primary failed over, the queue answered no submission (not ready). A client
retries with the same key, as the IDP's adapter does (ADR-082).

## What it costs, said plainly

- A dispatch or lookup on the non-owner costs one more hop.
- A replica that cannot reach the owner answers 503, and netci-queue retries until the lease moves
  (15 s).
- The fence decides "its replica is gone". Without one (the lab's tests), a number reserved by a
  dead replica that never started its build waits for that replica to come back.
- netci-queue still treats a new session as a lost queue. With replicas that is safe now (a
  dispatch again finds the run, or the store refuses a second number), but it costs re-dispatches
  when calls alternate between replicas.

## Rejected

- **Pointing netci-queue at one replica.** That replica would be the single point of failure the
  VIP removes.
- **Making netci-queue replica-aware** (two URLs, one session per replica). It would put homesync's
  ownership rules in a second program, while the plugin already knows them.
