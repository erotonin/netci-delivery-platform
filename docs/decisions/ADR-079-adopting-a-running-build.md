# ADR-079: Adopting a running build when its replica dies

- Status: accepted; measured in the lab (below)
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
2. **Adoption needs the runner gone and a claim.** The runner is the replica that last wrote the
   build's copy. A replica adopts it when the runner is itself (it restarted: the old process is
   gone) or the fence says the runner is gone since that copy; it claims the build by writing the
   copy again under its own name, compare-and-set (of two replicas trying, one wins), writes it into
   its home, makes the number known and loads the run, which resumes itself. Lab fence: the
   Kubernetes API says the runner's pod is gone, being deleted, not running, its container started
   after the copy (a new process), or its node not Ready.
   **Not the job's lease.** The first design required it. The lease decides who starts builds and
   reserves numbers, and it moved to the other replica for a new build (#8) while #7 waited for
   adoption with its agent on the first replica: #7 was stuck between the two for good.
2b. **A build follows its agent.** It is adopted only where the agent it is using (the last
   `Running on X` of its log, if that node block has not closed) is online. The name is read with
   the console notes removed: in the log file it is a hyperlink note, and read raw it named no agent
   any replica had -- nothing was adopted in the second lab run.
2c. **An idle agent visiting another replica goes home** when its home answers, so the next build of
   a job owned by the home has its agents (both agents had ended on one replica once). The first lab run had
   the dead replica come back in 9 s and take its own build back while its agent had reconnected to
   the other replica; the build waited for that agent for good.
3. **A replica that starts finds its own running builds suspect**: before Jenkins loads its jobs,
   each build directory whose build.xml says `<completed>false</completed>` is moved aside. Before
   loading, not after: a job lists its numbers as it loads, and a number listed while its directory
   was away stayed unloadable (`recognizeNumber` keeps an entry it already has). And not "no
   `<result>`": a running Pipeline's build.xml already says `SUCCESS`, its initial value. Those builds come back only through
   adoption (2) -- by this replica or another, whichever takes the lease -- so a build is resumed in
   one place, from the latest replicated copy.

## Measured (the lab, netci22, `lab/aa/phase5_probe.py`, `aa-phase5-20261013T192242Z.json`)

`long`, 90 one-second ticks in an `sh` step on agent-0 (inbound, WebSocket through the VIP), owned by
r0. Nothing done by hand after the death; the adoption time is the adopting replica's own log line.

| Death of the owner | Adopted by | Adopted after | Result | Ticks | Returning replica |
|---|---|---|---|---|---|
| its pod killed (no grace) | jenkins-1 | **4.3 s** | SUCCESS, 74.5 s after the death | 90, none missing, none twice | lists it SUCCESS, did not resume it |
| its machine's power cut | jenkins-1 | **49.5 s** | SUCCESS, 76.2 s after the death | 90, none missing, none twice | lists it SUCCESS, did not resume it |

After a machine's death the build still finished when it would have: the `sh` process ran on the
agent throughout, and the adopter read its output on. The 49.5 s are Kubernetes taking the dead
node to NotReady, which is what the lab's fence waits for; a power fence (Redfish) is faster.

Found on the way, each kept as evidence and fixed: the agent's name read from the raw log (a
console note) matched nothing; adoption waited on the job's lease, which had moved for a new build
(#7 stuck while #8 ran); a fast-returning replica took its build back while its agent was on the
other; HAProxy kept an agent's tunnel to a dead machine open for an hour; the fence's pooled
connection kept landing on the dead API server; an agent was sent home from a build just adopted
for it. Once, the lab's PostgreSQL was on the lost machine: the store answered 503 for ~80 s and
adoption waited for it -- the store must be HA.

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
