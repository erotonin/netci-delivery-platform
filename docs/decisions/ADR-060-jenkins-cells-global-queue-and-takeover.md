# ADR-060: Jenkins cells, a global queue outside Jenkins, and fenced takeover

Status: Proposed (2026-10-01); decisions 1 and 5 revised by the spike the same day. Supersedes the product scope of ADR-001..059: netCI no longer
deploys (the organisation deploys with Jenkins) and no longer ships a portal. Those ADRs stay
as history; the mechanisms reused from them are named below.

## Context

The organisation runs open-source Jenkins, on VM agents, and deploys with it. It wants the
guarantees CloudBees CI sells as High Availability (active/active): when a controller dies,
no build history, log, queued build or running build is lost, and people keep working.

What was verified before deciding (sources in docs/research/jenkins-ha-dr.md and the links
below):

- CloudBees HA runs several replicas of one controller on a shared file system and keeps
  their live state in Hazelcast; a surviving replica *adopts* the Pipeline builds of a dead
  one. Even CloudBees supports Pipeline only and reimplements `build` and `lock` for it.
  (docs.cloudbees.com: ha-fundamentals, pipeline-builds-ha-active-active)
- Open-source Jenkins has no equivalent. Controllers sharing one `JENKINS_HOME` corrupt it:
  state lives in memory and is serialised to XML without cross-process coordination.
- Adoption rests on a mechanism open-source Jenkins has: Pipeline durability. A `sh` step is
  a process on the agent that outlives the controller; the build resumes at the step it was
  in, provided the agent reconnects. Reconnection after a controller restart is buggy for
  WebSocket and Kubernetes agents (JENKINS-67062, kubernetes-operator#691) and must be proven
  and, where needed, fixed.
- A resuming build waits about five minutes for an ephemeral agent before giving up
  (workflow-durable-task-step). Takeover must finish well inside that.
- Jenkins' own queue is lost on a crash: `queue.xml` is written on orderly shutdown only
  (JENKINS-30909); the plugin that persisted it was withdrawn.
- Kubernetes >= 1.28 fences a dead node with the `node.kubernetes.io/out-of-service` taint:
  its pods are deleted and its volumes detached at once instead of after ~6 minutes.
- Pipeline logs can be written outside `JENKINS_HOME` and still shown in Jenkins
  (OpenTelemetry plugin, Elasticsearch-compatible backend).

True active/active on one `JENKINS_HOME` means rewriting how Jenkins holds its queue, builds,
build numbers and agent channels in memory, and it breaks plugins that keep state in memory.
It stays research (see "Rejected").

## Decision

**netCI becomes a high-availability layer around open-source Jenkins.** Jenkins is not
replaced or forked; people keep their Jenkinsfiles and the Jenkins UI.

1. **Cells.** A cell is one Jenkins controller: one JVM, its own `JENKINS_HOME` on a block
   volume replicated synchronously across nodes (Longhorn in the lab; Ceph RBD, vSAN or any
   CSI volume that survives a node in a company). Several cells run at once; each holds a set
   of teams or folders (its tenants). A fault, a bad plugin or an upgrade touches one cell.
   Velero backups remain for disaster recovery and corruption, not for failover.

   **Replication alone does not make a power loss safe** (spike, below): it replicates what
   reaches the block device, and Jenkins leaves most of its writes in the page cache. So:
   - page-cache writeback on controller nodes within ~1 s (`vm.dirty_expire_centisecs=100`,
     `vm.dirty_writeback_centisecs=100`) and the ext4 journal committed every second
     (`commit=1`) -- what a power loss can take back is bounded to about a second;
   - the WAR and the exploded plugins on the pod's local disk (`--webroot`, `--pluginroot`):
     they come from the image and are not state;
   - Pipeline durability `MAX_SURVIVABILITY` (the default) everywhere;
   - for the remaining second, a re-execution guard (decision 6) -- a step whose start was
     lost must not run twice where running twice is harmful.

2. **Every cell is built from the same sources.** Controller image and all plugins from
   `toolchain/versions.yaml` (ADR-059), configuration from JCasC, jobs from Job DSL in git,
   credentials from an external secret store scoped per cell -- never by copying
   `credentials.xml` or `secrets/master.key` between cells. Drift blocks the cell (ADR-059).

3. **One intake and one durable queue, outside Jenkins.** Webhooks, cron schedules, API calls
   and manual runs are written to a queue in PostgreSQL (HA) before anything else happens.
   Each cell is handed only what it can start now (the admission budget of ADR-050), so a
   crashed cell loses nothing that was not already running. Every build carries a global run
   id; before a build is handed to another cell the dispatcher checks, under a fenced lease,
   that the first cell never took it -- a build runs once. Cron and SCM polling run in a
   leader-elected scheduler instead of in each controller: no double runs across cells, and
   a missed slot during a takeover is caught up according to the job's policy.

4. **Cell Supervisor: detect, fence, take over.** Each cell holds a lease renewed every few
   seconds. When it lapses the supervisor fences first (out-of-service taint, or power-off
   through the hypervisor API) and only then starts the controller elsewhere on the same
   replicated volume. A fencing token accompanies everything a cell writes to netCI, so a
   controller that comes back from a partition cannot act on stale authority. Budget, each
   phase measured: detect <= 10 s, fence <= 5 s, attach <= 10 s, Jenkins start <= 30 s,
   agents reconnected <= 10 s; p95 <= 60 s.

5. **Running builds continue.** Pipeline durability plus agents that reconnect to the cell's
   stable address; the agent fabric (ADR-061) never removes an agent holding a paused build.
   An agent cannot rely on its connection breaking: a machine without power sends no FIN or
   RST, and Jenkins' channel pinger needs 5 + 4 minutes while a resumed build waits ~5. Every
   agent therefore runs under a supervisor that watches the cell's `X-Jenkins-Session` (new on
   every start) and restarts the agent JVM when it changes; the build's processes are
   durable-task's and live outside that JVM.
   Freestyle builds cannot be resumed by any product; they are re-queued and reported as
   such, never as succeeded.

6. **Deploy stages are guarded on resume.** The organisation deploys from Jenkins. A stage
   declared as a deployment does not continue blindly after a takeover: it re-checks the
   target or waits for a person, and a deployment lease with a fencing token (ADR-016) stops
   a stale controller from deploying.

7. **The read path never goes down with a cell.** Build records are written to PostgreSQL as
   they happen and logs stream to OpenSearch; history, status and logs are served from there
   while a cell is taking over.

8. **Planned work loses nothing.** Draining a cell stops dispatch to it; running builds finish
   or resume after the restart. Upgrades roll cell by cell, a small cell first.

9. **Measured, and broken on purpose.** SLOs: no trigger lost, takeover p95, share of builds
   resumed, agent wait, logs lost = 0. A chaos suite kills the JVM, powers nodes off,
   partitions the network, stalls storage and fails PostgreSQL over; every run is recorded
   as evidence.

## Spike results (lab, 2026-10-01; evidence in lab/evidence/spike-*.json)

| Failure | Configuration | Outcome | Takeover |
|---|---|---|---|
| Controller JVM killed | any | resumed, 180/180 log lines, SUCCESS | Jenkins 12.5 s, build 14 s |
| Controller node powered off | plain volume, stock agent | agent never reconnected; FAILURE | Jenkins 99 s |
| Node powered off | plain volume + agent supervisor | agent back 3.6 s after Jenkins, **but state went back ~20 s: the `sh` step ran a second time in a new workspace while the first kept running** | Jenkins 87 s |
| Node powered off | `sync,dirsync` volume | step ran once, SUCCESS | Jenkins 92-98 s |
| Node powered off, 1 s into the step | writeback 1 s + `commit=1` | step ran once, all lines, SUCCESS; < 1 s of controller-side log lost | Jenkins 76-78 s |
| Queued item (quiet period), JVM killed | any | **queue lost** (JENKINS-30909) | -- |
| Same, graceful restart | any | queue kept | -- |

Cost per build (io-probe: 200 flow-node steps, 20,000 log lines; median of 5): plain 6.5 s /
0.98 s, `sync,dirsync` 34.4 s / 14.6 s (about 4x the whole build), writeback 1 s + `commit=1`
6.6 s / 0.91 s (no measurable cost).

Takeover after a power loss is dominated by detection: ~50 s for the node to be marked
NotReady. The Cell Supervisor's own lease (decision 4) is what brings it towards the 60 s p95.
Each figure is one run; the chaos suite repeats them with random failure points.

## Cell agent and Cell Supervisor (built 2026-10-01)

**Cell agent** (`cmd/cell-agent`, a native sidecar of the controller pod). It holds the cell's
Lease as `<pod>/<uid>` (15 s duration, renewed every second, 10 s renew deadline; 6 s / 4 s
was tried first, see "Unattended takeover" below). Expiry is
judged only on the observer's own monotonic clock (unchanged for the whole duration), never
from timestamps another machine wrote. The deadline counts from when a renewal *started*, so
a holder stops at least `duration - deadline` before anyone else may start, whatever the
latency of its calls. Three layers stop a controller that lost its Lease:
1. the agent SIGKILLs the JVM at the deadline (shared process namespace);
2. `cell-agent guard`, the controller container's command, kills it when the gate file's
   mtime -- set to the start of each renewal -- is older than the deadline. That covers the
   agent itself hanging or dying;
3. Jenkins starts only behind an open gate, so a restarted container is gated too.

On a graceful stop the Lease is released only after Jenkins has exited.

**Cell Supervisor** (`cmd/supervisor`, two replicas, leader-elected). It reads the API server
directly every second (never an informer cache) and forgets what it saw after a gap. The one
exception is when to ask for a machine's power state. A Lease whose version has not changed
across a gap was not renewed during it, because the reads are quorum reads, so it still
prompts the question. A machine is fenced on that basis only if it reports off. Expiry, and
anything done on a guess, still need an unbroken run of observations. When a lab machine that
was also an etcd member lost power, observations failed for a few seconds while etcd elected a
leader, and the reset added 4 s to the fencing. It acts
only on a Lease that names the current pod. Once that Lease has gone unrenewed for 3 s (three
missed renewals), it asks the machine's power controller at once. A machine that reports **off**
is fenced then, without waiting for the Lease to expire: nothing runs on a machine that is off,
and a holder alive elsewhere (a wrong mapping) would have renewed within those 3 s. Otherwise it
waits for the Lease to go unchanged for its whole duration, then asks the power controller:
- **off:** fence at once. Six seconds after a power loss the kubelet still looks alive.
- **running, kubelet silent for 20 s:** power off, unless that would cost the control plane
  its majority.
- **running, kubelet alive:** wait, then restart the pod after 30 s.
- **unknown:** alert. It never fences on a guess.

When most cells or nodes fail together, it powers nothing off. The order of a fencing is the
safety argument: confirm the machine off, then release the Lease conditionally on the version
the decision saw, then taint `out-of-service`, then delete the pod by UID. A conflict at the
release means something renewed a Lease whose machine is off, so the node-to-machine mapping
is wrong and fencing stops before anything lets a successor start. Startup refuses a mapping
that contradicts what it sees. Each of these rules has a test that turns red when the rule is
removed.

Lab evidence for the agent (cell-b, `lab/evidence/agent-*.json`, one run each):

| Probe | Result |
|---|---|
| Pod deleted | Lease released 0.07 s after "Jenkins stopped"; new pod held it at epoch+1 7.8 s after the delete; Ready at 22 s |
| Agent SIGKILLed (restarted at once) | Jenkins not interrupted; same holder and epoch |
| Agent SIGSTOPped (hung) | guard killed Jenkins 3.5 s after the stop (exit 137); liveness restarted the agent; Ready again |
| Pod cut off from the API server for 20 s (iptables in its network namespace) | Jenkins killed 3.2 s after the cut; no JVM of the pod while cut off; back after the cut healed |

## Unattended takeover (lab, 2026-10-01; lab/evidence/spike-resume-node-poweroff-supervised-*.json)

A VM was powered off under a 240 s build, with no operator action. The supervisor detected the
loss, confirmed the machine off through the power agent, released the Lease, tainted and
marked the node, deleted the pod, powered the machine back on and removed the taint. In every
run:
- the build resumed and finished SUCCESS;
- its `sh` step ran once;
- at most 1 of 240 log lines was lost.

What the runs found, and what changed:

| Run | Fenced after | Jenkins up after | What it showed | Change |
|---|---|---|---|---|
| 1 | 21 s | 67 s | The leader, on a healthy node, lost its lease: one HTTP/2 connection to the dead API server, every call failing. Restarted, it refused to start: the dead node still read Ready | HTTP/1.1 clients; the startup check retries and calls a mapping wrong only if the kubelet heartbeat is renewed |
| 2 | ~31 s | 95 s | The leader died with the cell's node. Longhorn moved the volume only once Kubernetes marked the node NotReady, 74 s after the power loss | The supervisor marks a fenced node NotReady itself; its pods prefer nodes without cells |
| 3 | 6.6 s | 109 s | Fast fencing, but the machine was powered on 30 s later, while the volume was still moving; Longhorn deleted the almost-started pod | Power on only once every cell is held again |
| 4 | 30 s* | 61 s | Volume attached 13 s after fencing (was 27-45 s) | — |
| 5 | 16 s* | 75 s | **A healthy cell on another node lost its Lease and restarted Jenkins:** etcd on the surviving members took 1-3 s per read, and the 4 s renew deadline did not outlast that | Cell Lease 15 s / 10 s, as Kubernetes' own controllers use; renewals retried with short attempts; the supervisor re-enters the election instead of exiting |

| 6 | 16.5 s | 44.5 s (build resumed 42.5 s) | With 15 s / 10 s: the healthy cell on another machine kept its Lease and its Jenkins; the supervisor kept its leadership. Timeline from the build's ticks and the pod's conditions: fenced 16.5 s, pod scheduled 18.5 s, volume attached and Lease held 31.5 s, build resumed 42.5 s | The probe's kubectl now fails over between API servers (pinned to the dead one, it recorded 58 s for all of these) |

\* The supervisor's leader lost its own lease in the same API stall and had to regain it.

Run 6 is the design working as intended: a power loss is taken over in ~45 s, with no person
and no lost work.

**Repeated (lab/spike/chaos_poweroff.py, random failure tick 5-60, evidence chaos-poweroff-*.json).**

| Series | Runs | Builds | Fenced | Lease held | Build resumed | Other cell |
|---|---|---|---|---|---|---|
| Before the hot standby | 4 | 4/4 SUCCESS, 1 line lost once | 27.5-37.9 s | 61.5-75.1 s | 81.2-93.9 s | Never lost its Lease when on another machine; taken over when on the same one |
| Hot standby (every replica observes) | 4 | 4/4 SUCCESS, 1 line lost once | 14.5-23.1 s | 52.4-63.1 s | 71.1-83.7 s | The same |
| Supervisor deletes every pod on the fenced node | 4 | 4/4 SUCCESS | 15.9-23.7 s | 57.6-65.9 s | 74.7-83.5 s | The same |
| One CSI attacher per node; attacher health check 2 s + 2 s | 4 | 4/4 SUCCESS, 1 line lost once | 20.5-22.9 s | 31.4-75.9 s | 49.6-93.9 s | The same |
| Longhorn managers' health check 2 s + 2 s; a machine found off is fenced 3 s after its last renewal | 2 (host shut down) | 2/2 SUCCESS | 17.4 s | 29.5-34.1 s | 47.3-55.7 s | The same |
| netCI's API clients bound each attempt (ADR-066) | 1 | 1/1 SUCCESS | 2.7 s | 19.8 s | 33.5 s | Run 2 found a deadlock: the cell had no node with room, and the supervisor would not power the fenced machine on while the cell awaited its takeover. Fixed, with priority classes |
| `netciOnce` blocks; probe evidence includes the supervisor's counters | 3 | 3/3 SUCCESS, one `netciOnce` marker each | 10.1-20.3 s | 23.8-46.4 s | 42.8-66.5 s | The leader lost observations to the dead API server (fixed: direct API server dialling), or died with the machine (fixed: leader hand-over) |
| Direct dialling, hand-over | 2 | 2/2 SUCCESS | 10.3-19.0 s | 37.5-41.8 s | 55.8-60.5 s | A gap in observations restarted the count (fixed: LeaseQuiet); the replicas had drifted beside the cells (fixed: rebalancing) |
| LeaseQuiet, rebalancing | 3 | 3/3 SUCCESS | 3.6-8.2 s | 30.2-46.9 s | 48.0-77.7 s | A renewal just before the stall was counted from its first sight (fixed: renewTime within the gap) |
| **Every fix** (`chaos-poweroff-20261001T182441Z.json`) | 6 | **6/6 SUCCESS**, one `netciOnce` marker each, no line lost | **2.9-9.3 s** | 30.2-49.3 s | **49.0-68.7 s** | In 4 of 6 runs both cells were on the lost machine; both were taken over |
| Longhorn tuning as admission policies (it had been undone mid-series) | 6 | 6/6 SUCCESS, one marker each | 2.6-9.6 s | 28.2-86.2 s | 43.3-105.0 s | One attach took 70 s (unexplained; logs lost) |
| Attacher leader lease 10 s; probe records the leaders | 6 | 6/6 SUCCESS, one marker each | 3.1-7.4 s | 25.6-64.4 s | 42.5-83.4 s | Every run lost a leader of kube-controller-manager, kube-scheduler or the attacher |
| **Control-plane timings at upstream values** (`lab/k3s-timings.sh upstream`; `chaos-poweroff-20261001T201806Z.json`) | 6 | **6/6 SUCCESS**, one marker each, 1 line lost once | **2.5-3.5 s** | 19.1-47.4 s (median 23.2) | **37.4-64.8 s (median 40.9)** | Slowest: both cells and all three leaders on the lost machine |
| Every cell on a fenced machine released at once; attacher retries ≤ 2 s | 6 | 6/6 SUCCESS, one marker each, 1 line lost once | 2.9-3.6 s | 13.4-64.8 s | 32.9-82.6 s | Longhorn deleted the replacement after it took its Lease in 2 runs (ADR-067) |
| **netCI restarts a failed JENKINS_HOME; Longhorn leaves StatefulSets alone** (`chaos-poweroff-20261001T213158Z.json`) | 6 | **6/6 SUCCESS**, one marker each, no line lost | **2.8-3.6 s** | 14.3-43.5 s (median 19.9) | **32.6-63.3 s (median 40.5)** | Jenkins started in 11-16 s every time |
| **Longhorn tuning without template annotations** (`chaos-poweroff-20261001T220424Z.json`) | 6 | **6/6 SUCCESS**, one marker each, 1 line lost once | **2.7-3.8 s** | 18.0-31.9 s (median 24.6) | **40.8-50.3 s (median 42.9)** | Every run lost two or three of the controllers' and the attacher's leaders with the machine; no run past 51 s |

With the lab's control plane at upstream timings, standing in for a control plane on machines
without cells, a power loss is fenced in about 3 s. The build continues after 41-50 s even when
the machine also took the leaders of kube-controller-manager, kube-scheduler and the CSI
attacher (the latest series), and after 32.6 s at best. With k3s's own timings, fencing takes ~3 s, or 8-9 s when the lost machine held etcd's
leadership. The
lab's k3s sets etcd's election timeout to 5 s, and until a new leader is elected no API server
answers anyone, so the supervisor has nothing to act through. A control plane on machines that
run no cells, or etcd with its 1 s default, removes that difference.

What remains is the storage moving the volume (21-40 s from fencing to the Lease held, longer
when two volumes move at once) and Jenkins starting (~10 s, longer when two start on one
machine). The traces (`lab/spike/trace_takeover.sh`) found and removed two blind spots on the
storage's side (ADR-066). The volume move itself is the next thing to measure.

## What this reuses from netCI

The Jenkins REST client and controller router (ADR-009), admission budget and supersession
(ADR-050), plugin and toolchain drift gates (ADR-056/059), leases and fencing tokens
(ADR-016/041), the reconciler (ADR-020), webhook signature checks and de-duplication
(ADR-019), workload identity for callbacks (ADR-015). They are rewritten in Go (ADR-062).

## Rejected

- **Active/active replicas on one `JENKINS_HOME`.** The only complete implementation is
  proprietary and constrained even there; building one means maintaining a distributed
  rewrite of Jenkins internals against every plugin release. Research after this ADR is in
  production, not before.
- **Restore from backup as the failover path** (ADR-055). RPO of a backup interval and a
  restore on every failure; replication makes RPO 0 and backups stay for DR.
- **One large controller.** A single queue lock and a single blast radius for the whole
  organisation; the Jenkins project's own guidance is to scale by adding controllers.

## Consequences

- Jobs must be defined as code (Job DSL) for a cell to be rebuilt or a job to spill to a
  sibling cell; hand-made jobs are migrated once.
- Webhooks point at netCI's intake, not at a controller.
- Builds on a cell that fails pause for up to the takeover time; CloudBees adopts them almost
  immediately. Everything else in the table of guarantees holds.
- Nothing in this ADR is verified yet. The first work is a spike on the lab (3 KVM VMs, k3s,
  Longhorn) that proves or disproves resume, agent reconnection, queue loss on crash,
  controller start time and replicated storage.
