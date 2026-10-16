# ADR-094: A running build's copy is one it can be resumed from

- Status: accepted; measured on the lab (below)
- Date: 2026-10-15
- Refines: ADR-079 (adoption), ADR-093 (which found the IDP's pipeline not surviving)

## Context

ADR-093 killed the replica owning a real IDP build. The build was adopted, then it either failed
with `SynchronousResumeNotSupportedException` (`withCredentials`) or hung for good. Jenkins'
own sources show both causes:

- **`GeneralNonBlockingStepExecution` keeps its background thread's name in the saved program**
  (`threadName`), and `onResume` fails the step while it is set. `withCredentials` binds and unbinds
  in such a thread, and the IDP's pipeline does that around every callback. A copy taken in that
  moment cannot be resumed. A restart could not resume there either: Jenkins does not restart then
  (`StepExecution.blocksRestart`).
- **durable-task's watcher on the agent removes a step's control directory** right after it has
  given the result to its controller (`handler.exited(...)`, then `controller.cleanup(...)`). A
  watcher started on a directory that is gone gives up without a word ("giving up on watching
  nonexistent"). The copy was taken every 3 s, and an IDP build runs a dozen short `sh` steps in
  that time. Adopted from a copy taken before a step ended, the build waited for that step forever.

## Decision

1. **No copy while the run blocks a restart.** This is Jenkins' own test:
   `Executor.getAsynchronousExecution().blocksRestart()`, which is true while the CPS VM is busy or
   a step is running background code. It is asked before and after the directory is read. The
   previous copy stands, and the next comes at the next step boundary.
2. **A copy at every step boundary.** A `GraphListener` asks for a copy on each new flow node. The
   copy is made 100 ms later on a timer thread, never on the CPS VM thread, and once per burst of
   nodes; the 3 s timer stays. A copy is at most a fraction of a second behind the build, not up to
   3 s. Copies of one run are made one at a time.
3. **A step that ended after the last copy fails with its reason; it is not waited for and not
   guessed.** When the copy's step has no control directory on its agent any more, the adopter
   writes the directory back before the run is loaded:
   - a log line saying what happened;
   - exit code 75 (EX_TEMPFAIL).

   The step fails now, and the build can be run again. Its real result is lost with the replica
   that had it, and nothing invents one.

## Tests, written with it (`CopyPointsTest`)

- **A build stopped while a step runs background code is adopted from the copy before it and
  succeeds.** With the guard switched off, the same test fails with the lab's
  `SynchronousResumeNotSupportedException`.
- **Each of four step boundaries reaches the store's copy within a second.** With the listener off,
  the first is 2.6 s late.
- **A step whose control directory is gone gets exit 75 and its reason.**

## Measured on the lab (controller `2.555.3-netci36`)

`lab/aa/idp_adoption_probe.py --kill pod` was run four times, each killing the replica that owned
a real build of payments-api on the lab's inbound agents at a different stage, with no grace.

| Killed during | Adopted by the survivor | Run in the IDP | Artifact |
|---|---|---|---|
| unit-test (#14) | after 24.2 s | succeeded | signed, policy allow |
| build (#15) | after 15.1 s | succeeded | signed, policy allow |
| vulnerability-scan (#16) | after 11.0 s | succeeded | signed, policy allow |
| sign (#17) | after 5.2 s | succeeded | signed, policy allow |

Then the machine itself: `--kill machine` cut the power of `netci-lab-1`, which ran the owning
replica, during Build (#18). The strict fence waited for the power supervisor's out-of-service
taint. The survivor adopted the build after 59.4 s, and it succeeded after 88.4 s, every stage
once, signed, policy allow. The machine was powered on again and the lab recovered by itself.

Each of the four pod-kill builds was resumed once, and `build.sh` and `scan.sh` each ran once. In #15, decision 3
applied, to the storage-setup `sh` inside a stage callback. The callback is best effort, so it
logged "stage report skipped: exit code 75" and the build went on; before this ADR that step was
waited for forever. Evidence: `lab/evidence/aa-idp-adoption-20261015T095*.json`. ADR-093's three
kills had given one hang and one failure.

## What it does not solve

- **The window is narrowed, not closed.** A step can still end after the last boundary copy, in the
  fraction of a second before its replica dies. That case now fails at once with its reason
  (decision 3). It no longer hangs.
- **An inbound agent whose channel writer is blocked** still takes minutes to notice (ADR-093,
  `long` #69). It has been seen once in about 13 kills. Four more pod kills of `long` on netci36
  (#70–#73, `aa-phase5-20261015T12*.json`) were all SUCCESS with every tick once: three were adopted
  by the survivor in 4.1–13.3 s, and one was taken back by its restarted owner. Its cause, in the
  network path or remoting, is not found.
- **A step adopted from a copy before it ended runs again**, from its start or from its agent's
  mark. An `sh` with side effects must be safe to repeat; netCI's callbacks and build scripts are.
