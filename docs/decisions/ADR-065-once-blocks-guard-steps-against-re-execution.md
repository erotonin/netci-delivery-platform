# ADR-065: `netciOnce` -- a step that must not run twice is guarded outside JENKINS_HOME

- Status: accepted
- Date: 2026-10-01
- Implements: ADR-060 decisions 1 ("re-execution guard") and 6 ("deploy stages are guarded on resume")

## Context

A controller that loses power loses at most about a second of `JENKINS_HOME` (bounded writeback,
ADR-060). If a step started in that second, the record of its start is gone, and when the
Pipeline resumes on the new controller it starts the step again. The lab saw exactly this when
writeback was unbounded: an `sh` step ran a second time, in a new workspace, while the first
copy was still running.

Re-running a compile is harmless. Re-running a deployment, a database migration, a release
publication or a payment is not, and nothing inside `JENKINS_HOME` can tell, because what was
lost is precisely the record that would.

## Decision

1. **A block step, `netciOnce(key) { ... }`.** The author marks what must not run twice. Before
   the body starts, the plugin records `(build, key)` in netCI's PostgreSQL through netci-queue:
   `POST /v1/once {scope, key, nonce}`. That record lives outside `JENKINS_HOME` and does not
   roll back with it.
   - First record of this build and key: the body runs.
   - Record already there with **another nonce**: the block started before, in a state the
     controller no longer has. The step fails with that explanation, and a person decides,
     e.g. by re-running the block with `netciOnce(key, retry: true)` after checking the target.
   - Record already there with **the same nonce**: a retried call of the same attempt (its
     reply was lost). The body runs.
2. **The nonce** is generated when the step starts and kept with the step execution. After a
   normal resume the step is resumed, not started, so no record is asked for. After a rollback
   the step is started anew, with a new nonce.
3. **The scope is the build:** the controller's instance identity, the job's full name and the
   build number. It is decided by the plugin, and the server records which client (cell) asked.
4. **Fail closed.** If netci-queue cannot be reached, the body does not run. A guarded step
   that might run twice is worse than one that waits; the call is retried for up to a minute,
   then the step fails.

## Rejected

- **Guarding every step:** a re-run compile or test is harmless, and recording every step
  would put the database on every build's critical path.
- **Detecting the orphaned process on the agent** (durable-task's control directory): it shows
  that something ran, not whether running it again is safe, and the agent may be gone too.
- **Synchronous writes for `JENKINS_HOME`:** the spike measured about 4x the build time.
