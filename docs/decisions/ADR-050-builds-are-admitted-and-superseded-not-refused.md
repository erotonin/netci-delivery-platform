# ADR-050: Builds are admitted and superseded, not refused

Status: Accepted. Replaces the 429 of the pipeline quota for SCM-triggered runs.

## Context

Coding agents push faster than people. An agent iterating on one pull request can push
twenty commits in two minutes. netCI dispatched every one to Jenkins the moment it was
written, so each commit held an executor until it finished, including the nineteen that
were out of date before they started. The quota from migration 0017 capped concurrency
by refusing the run with `429 PIPELINE_QUOTA_EXCEEDED`. That protects Jenkins but loses
the event: a webhook answered with 429 is not redelivered by GitHub, and the commit that
mattered, the last one, could be the one refused.

## Decision

1. **A run is written, then admitted.** Every run is still written `queued`, in the
   transaction that accepted it. Dispatching it to CI is a separate step, *admission*,
   which happens only while its quota scope has room (`max_concurrent_pipelines`, counted
   over runs that hold CI capacity: admitted and not finished). A run over the limit
   waits in PostgreSQL instead of being refused. Admission runs after a run is accepted,
   after a run finishes, and on a timer, under one advisory lock, and each admission is a
   compare-and-set on the run's version, so two replicas never dispatch one run twice.
   Oldest first within a scope.
2. **The queue is bounded.** `max_queued_pipelines` (per quota scope, default 50) is the
   one case still refused, `429 PIPELINE_QUEUE_FULL`: an unbounded queue only moves the
   overload from Jenkins to the database.
3. **SCM runs of one ref form a concurrency group** (`<application>:<ref>`, e.g. a branch
   or `pr/12`). When a new run joins a group:
   - a run of the group still **waiting for admission** is cancelled at once, superseded
     by the new one. It never started, so this costs nothing and loses nothing: the new
     commit contains it;
   - a run that is **already building** is cancelled too for pull requests, and left to
     finish for branches (`cancelInProgress`, default true for pull requests and false
     for pushes, configurable per module in `pipelineConfig.delivery`);
   - a run is **never** cancelled once it has started signing or publishing, is waiting
     for approval, or has a deployment. Stopping those would leave a half-published
     artifact or a half-deployed environment, which costs more than the build saves.
   The superseded run ends `cancelled` with `supersededBy` naming the run that replaced
   it, its commit status says so, and the cancellation is audited.
4. **Some runs never join a group.** Tag builds (a release is never skipped), manual runs
   (a person asked for that exact commit) and retries are admitted like any other run but
   supersede nothing and are superseded by nothing.

## Consequences

- At most one run of a pull request waits and at most one builds. Twenty pushes cost
  about two builds instead of twenty. That is a property of the rule, not a measurement;
  the saving on a real agent storm is still to be measured in the lab.
- A run may now sit in `queued` for a while. The API reports whether it is waiting for
  admission (`admittedAt` null) so a queued run is not mistaken for a stuck one.
- An API caller that exceeded the concurrency quota used to get 429; it now gets 202 and
  a waiting run. Only a full queue is refused.
- Capacity is the configured quota, not Jenkins' free executors. Reading executor
  availability from the controllers is the next step and is not done here.

## Live evidence (2026-09-25, Kubernetes install 0.2.0-rc15, the lab's Jenkins A)

Twelve commits on `agent/storm2` of the lab's `payments-api` repository, one signed GitHub
push webhook each, sent in 0.9 s, with the application's quota at one concurrent build.
All twelve were accepted (201). Two were dispatched to Jenkins: the first commit, admitted
at once, and the twelfth, admitted 44.9 s after it was written, when the first finished. Both builds
succeeded. The other ten ended `cancelled` with `supersededBy` set, before admission, so
Jenkins never saw them. Elapsed: 101 s. `cancelInProgress` (stopping a build already
running) was not exercised live: pushes default to false.

A first attempt built an empty tree (the storm's own clone came back empty over HTTP), and
netCI reported both builds failed at `Build` for a missing Dockerfile. That was the test
set-up, not netCI; the branch was deleted and the storm repeated from the bare repository.
