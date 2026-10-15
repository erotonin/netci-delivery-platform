# ADR-093: Adoption tested with the IDP's own pipeline -- what holds and what does not

- Status: accepted (the fence fix); the copy problem is solved by ADR-094 (4 of 4 adopted IDP builds succeeded)
- Date: 2026-10-15
- Refines: ADR-079 (adoption), ADR-084 (strict fence), ADR-092 (static agents)

## Context

ADR-079 measured adoption with `long`, a pipeline of 90 one-second `sh` ticks. When its replica's
pod was killed, the surviving replica took the build over in 4.3 s. The mentor's requirement is
"no lost builds", and the build that matters is the IDP's: about 9 stages, each wrapped in
callbacks that bind a credential and run a short `sh`. `lab/aa/idp_adoption_probe.py` starts a
real build of payments-api through the IDP on the lab's inbound agents and kills the replica that
owns it.

## Decision: the fence no longer misses a renewal written in its successor's first second

Kubernetes reports a container's `startedAt` in whole seconds. After a force-delete, the old
process lived on for a second or two and wrote one last copy of its running build. When that copy
fell in the second its successor's container started, "started after the renewal" could never be
true. No survivor adopted the build; it waited 50-60 s for the restarted replica to boot and take
it back itself. This happened in 2 of 4 kills before the fix.

In that second, the renewal can only be the old process's: a new JVM has written nothing yet. The
fence now counts the old process as gone once it has been silent for two copy periods plus a second
(`SAME_SECOND_SILENCE_MS`, 7 s). The rule applies in both modes, and only while the node is Ready
and reporting. `FenceTest` covers it; it ships in controller `2.555.3-netci35`.

## Measured on the lab (controller netci35, pod of the owner killed, no grace)

| Build | Adopted by the survivor | End |
|---|---|---|
| `long` #67 | after 9.6 s | SUCCESS, all 90 ticks once |
| `long` #68 | after 4.6 s | SUCCESS, all 90 ticks once |
| `long` #69 | after 4.2 s | **ABORTED**: 30 s after adoption the agent's channel stopped (the replica's ping failed). The agent's writer stayed blocked, so it noticed only at TCP's broken pipe, 5 min later, which is the build's wait for its agent |
| IDP payments-api #9 (before the fix) | no: the restarted owner took it back after 61 s | succeeded, signed, policy allow |
| IDP #12, killed during Build | after 17.9 s | **hung**: resumed from a copy taken before the old process's last steps, in an `sh` whose control directory that process had already removed. Cancelled through the IDP after 15 min |
| IDP #13, killed during the scan | after 6.3 s | **failed**: `SynchronousResumeNotSupportedException: withCredentials cannot be resumed after a controller restart`. The IDP run ended failed, honestly |

Evidence: `lab/evidence/aa-phase5-20261015T07*.json` and `aa-idp-adoption-20261015T0*.json`.

## What this establishes, said plainly

- **A build made of long steps survives its replica's death and is adopted in seconds.**
- **The IDP's pipeline does not, reliably.** In 2 of 3 kills it was adopted and then failed or hung.
  It is never reported as a success it did not have: the run ends failed, or stays running until
  cancelled, and a retry rebuilds it. But "no lost builds" is not established for it.

There are two causes.

1. **A copy is not taken at a point the pipeline can resume from.** Copies are taken every 3 s, and
   the old process runs on for a second or two after it is "dead". So the adopted state can be:
   - inside a step that cannot resume (`withCredentials` while it binds), or
   - behind steps the old process already finished and cleaned up (an `sh` whose control
     directory is gone).

   The IDP's pipeline runs dozens of such short steps per build, so it is far more exposed than
   `long`.
2. **An inbound agent can take minutes to notice its channel is dead**, when its writer blocks.

## Directions, not done

- Take a copy at step boundaries: a graph listener on each new flow node, alongside the timer.
  Never take one while a step that cannot resume is starting, so that the copy is always resumable.
- Bind credentials to fewer, longer blocks. The callback token, though, stays bound only around
  the callback: user code (unit tests) runs on the same agent and could forge results with it.
- Wrap the node in `retry(conditions: [nonresumable(), agent()])`. Its first attempt still reports
  "failed" to the IDP before the retry, so the IDP's run model has to learn "retrying" first.
- On the agent, a socket write timeout, so a blocked writer is found in seconds, not at TCP's
  give-up.
