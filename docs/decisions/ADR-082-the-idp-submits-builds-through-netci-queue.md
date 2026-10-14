# ADR-082: The IDP submits its builds through netci-queue

- Status: accepted; unit-tested, lab smoke below
- Date: 2026-10-14
- Refines: ADR-063, ADR-073, ADR-081

## Context

The IDP triggered builds with `buildWithParameters` on a Jenkins it chose, and followed them by
Jenkins queue item and build number. Every Jenkins HA guarantee measured in the lab (ADR-060..081)
is for runs that come through netci-queue. A build the IDP triggered directly could be lost, because
Jenkins' queue is in memory: the lab lost exactly that kind of trigger.

Three behaviours of the launcher do not survive replicas or a durable queue:
- **Falling through to the next controller.** When a trigger failed, the launcher tried the next
  controller. If the first one had accepted the build and only its answer was lost, the run was
  built twice.
- **Queue item ids.** They belong to one replica, and so does `/stop` on a build. Behind the VIP,
  both land on either replica.
- **A new callback token on every launch.** A retried launch looked like a different run to an
  idempotent intake.

## Decision

1. With `NETCI_RUN_QUEUE_URL` (and its client token) set, the Jenkins adapter is
   `JenkinsQueueAdapter`.
   - A build is a `POST /v1/runs` keyed by the IDP's pipeline run id, carrying the same parameters
     as before (`_ci_parameters`, shared with the direct adapter).
   - It is read (`GET /v1/runs/{id}`) and cancelled (`POST /v1/runs/{id}/cancel`) by that run id.
   - The job is still created through Jenkins at the one entry point, where it is replicated
     (ADR-076). The log is read there too.
2. **One entry point only.** With a queue, `NETCI_JENKINS_CONTROLLERS` must name exactly one
   controller: the queue routes builds, not netCI. Startup is refused otherwise.
3. **A submission is retried with the same key while the queue is unavailable** (503 or no answer;
   its database failing over took ~30 s in the lab), for up to `NETCI_RUN_QUEUE_SUBMIT_RETRY_SECONDS`
   (120 s). Any other refusal fails the launch at once. Accepted means durable.
4. **The callback token is a secret parameter of the queue** (`secretParameters`).
   - The queue keeps it, because the run is dispatched from what was stored.
   - It never shows it back.
   - It is left out of what makes a retry the same run: a retry carries a newly minted token, and
     the run keeps its first one.
5. **Statuses map to the IDP's existing vocabulary.**
   - accepted and dispatched: queued; started: running;
   - finished by result: SUCCESS succeeded, FAILURE or UNSTABLE failed, ABORTED cancelled;
   - cancelled: cancelled; refused: failed;
   - a result the queue could not learn: unknown, which the IDP does not act on.
   - Runs with ids from before the queue are still read from Jenkins.

## Tests

`backend/tests/test_jenkins_queue_adapter.py`, against a real HTTP fake of the intake:
- one run per pipeline run, retried, with a newly minted token;
- the queue unavailable for three submissions, waited for with the same key;
- a 403 not retried, a queue down past the bound failing the launch, no answer at all;
- every state and result mapped;
- the log read through Jenkins under a folder, empty before the build exists;
- cancel by run id;
- an old run id read from Jenkins;
- the token required, one entry point required;
- no error carries the callback token.

The full gate: `pytest backend/tests tests/contract` 62 + 1507 passed, `pyflakes` clean,
`schema.sql` matches 35 migrations.

## Lab smoke (`lab/aa/idp_queue_smoke.py`, `aa-idp-queue-20261014T105149Z.json`)

The IDP's own adapter, against the replicas behind the VIP and netci-queue on the HA store
(controller netci28):
- the job the IDP generates was on both replicas 0.16 s after its creation;
- the build was submitted with every IDP parameter and read by run id (queued, then failed), and
  submitted again with a new token it was the same run;
- its 23 log lines were read through the VIP;
- the queue's answers never held the token;
- a second build was cancelled through the queue.

The build failed where it had to: `Could not find any definition of libraries
[netci-shared-library]`. The lab's replicas have no netCI shared library, so this proves the path,
not the pipeline. The rolling upgrade to netci28 under load answered 358 of 358 UI polls.

## What it costs, said plainly

- The IDP's admission budget (ADR-050) still counts the queue of the one Jenkins it reaches. With
  replicas that is one replica's queue. The queue's own budget per cell (ADR-063) is what bounds
  Jenkins now.
- The log of a build is read through the entry point. A replica that does not have the build sends
  the request to the one that does (owner routing, ADR-078). During a cell's takeover there is no
  log until it is back, except in Loki (ADR-068).
