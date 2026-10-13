# ADR-079: Adopting a running build when its replica dies

- Status: accepted (built; unit-tested across a real Jenkins restart; the lab run is ADR-078 phase 5)
- Date: 2026-10-13
- Refines: ADR-078

## Context

By hand (ADR-078 phase 5): a Pipeline running on r0, copied into r1's home, r0 killed: loading the
run on r1 resumed it, the agent reconnected to r1 through the VIP, and the build finished with every
line once. Three things were done by hand that a replica must do itself:
1. the copy of the running build;
2. the decision that r0 is dead, so that continuing its build cannot run it twice;
3. on r0's return, not resuming its stale copy of a build r1 has taken.

## Decision

1. **A running build is replicated while it runs**: its directory (program, flow nodes, log) is
   published as `@running/<job>/<n>` every 3 s when it changed, and deleted when the build is
   finalised (the finished build is then `@builds/...`). The other replicas only **record** that it
   exists; they never write it into their home, because loading it there would resume it.
2. **Adoption needs the lease and a fence.** A replica adopts a running build only when the job's
   lease has expired (its owner stopped renewing) **and** the fence confirms the owner is not
   running. Lab fence: the Kubernetes API says the owner's pod is gone, not running, being deleted,
   or on a node that is not Ready. Then it claims the job's lease (one winner, ADR-078), writes the
   latest copy into its home, makes the number known and loads the run, which resumes itself.
2b. **A build follows its agent.** It is adopted only where the agent it is using (the last
   `Running on X` of its log, if that node block has not closed) is online. The first lab run had
   the dead replica come back in 9 s and take its own build back while its agent had reconnected to
   the other replica; the build waited for that agent for good.
3. **A replica that starts finds its own running builds suspect**: before Jenkins loads its jobs,
   each build directory whose build.xml says `<completed>false</completed>` is moved aside. Before
   loading, not after: a job lists its numbers as it loads, and a number listed while its directory
   was away stayed unloadable (`recognizeNumber` keeps an entry it already has). And not "no
   `<result>`": a running Pipeline's build.xml already says `SUCCESS`, its initial value. Those builds come back only through
   adoption (2) -- by this replica or another, whichever takes the lease -- so a build is resumed in
   one place, from the latest replicated copy.

## What it costs, said plainly

- **The fence decides correctness.** A pod that Kubernetes reports gone is gone on a live node; a
  machine that is partitioned but alive is not proven dead by the API. A site fences the machine
  (Redfish power-off, as the supervisor does for cells, ADR-060/069); the lab's Kubernetes check
  covers a killed pod and a dead machine, not a partition.
- An agent is connected to one replica at a time. A build adopted while its agent is still held by
  an old owner that is alive waits for the agent ("Waiting for reconnection"), it is not run twice
  on the agent.
- The build is paused from the owner's death to the adoption: the lease (15 s) plus the fence.
- A copy is at most 3 s old (a replica stopping on purpose publishes a last one); it is published
  only once the build has a build.xml -- the first copy, a bare log, could not be loaded by anyone.
  The `sh` step reads its output on from the offset the program holds.

## Rejected

- **Resuming on every replica and letting one win**: the losers would have run controller-side
  steps already.
- **Calling `onResume()` after loading the run**: a second resumption; it failed the build by hand.
