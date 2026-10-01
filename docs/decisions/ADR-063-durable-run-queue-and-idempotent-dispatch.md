# ADR-063: A durable run queue outside Jenkins, and dispatch that cannot start a run twice

- Status: accepted
- Date: 2026-10-01
- Refines: ADR-060 decision 3

## Context

Jenkins keeps its queue in memory and writes it to `queue.xml` only on a graceful shutdown. A
controller that crashes loses every queued item (JENKINS-30909, confirmed in the lab: a quiet-
period item vanished when the JVM was killed). A takeover of a cell is a crash, so every
trigger waiting in that cell's queue is lost. ADR-060 promises that no trigger is lost.

A run identity has to survive into Jenkins for anything outside it to tell whether a run
started. The lab showed that the REST API cannot carry one for every job:
- `buildWithParameters` drops parameters that the job does not declare (SECURITY-170);
- it refuses jobs without parameters (HTTP 400).

## Decision

1. **Accepted means durable.** A trigger (webhook, schedule, API call, person) becomes a row in
   PostgreSQL before anything acknowledges it. The row carries a run id (UUID), the job, its
   parameters, the cell that owns the job, and the client that asked (decided by the server
   from the client's credential, never taken from the request). An optional idempotency key
   (a webhook's delivery id) makes a retried delivery the same run.

2. **The netCI Jenkins plugin dispatches idempotently.** `POST /netci/dispatch` with
   `(job, runId, parameters)`:
   - holds Jenkins' queue lock;
   - looks for the run in the queue (pending items included), in the work units of executors
     that have taken an item but not yet created its build, and in the job's recent builds;
   - returns what it finds, or schedules the build with a `NetciRunAction(runId)` that is
     copied onto the build and saved with it.

   Under the queue lock no item can move between those places, so a run is never found
   nowhere while it is in fact starting. The same call repeated, by a retry, a second
   dispatcher or after a crash, returns the existing run instead of scheduling another.
   Parameters the job does not declare are refused (HTTP 400), never dropped silently. The
   caller needs `Item.BUILD` on the job, as the UI would require.

3. **The dispatcher re-sends until Jenkins has started the run.** For every run that is
   accepted or dispatched but not yet started, it calls dispatch again whenever it has not
   seen this controller session (`X-Jenkins-Session`, new on every start) acknowledge the run.
   After a crash the new controller's lookup finds nothing, and the run is scheduled again;
   after a graceful restart the restored queue item is found and nothing is duplicated. Runs
   never move to another cell: a job belongs to one cell, and the cell's lease (ADR-060)
   guarantees that only one controller of it is alive.

4. **Admission.** Each cell is given at most a configured number of dispatched-but-not-started
   runs. Jenkins' queue stays short, and the queue that matters is the durable one.

5. **What this does not cover.** A build whose start reached an agent but whose record was lost
   in the last second before a power loss (ADR-060's bounded writeback) can be scheduled again.
   The re-execution guard (ADR-060 decision 1) is what covers that window, not this ADR.

## Rejected

- **A required `NETCI_RUN_ID` job parameter:** every job would have to change, and jobs without
  parameters could not be triggered.
- **`hudson.model.ParametersAction.safeParameters`:** it keeps an undeclared parameter but
  still refuses jobs without parameters, and it changes a security default.
- **Matching builds by start time:** ambiguous whenever two runs of one job start together.
- **Persisting Jenkins' queue on every change:** that patches core behaviour of every
  controller and would still lose the item scheduled in the last second.
