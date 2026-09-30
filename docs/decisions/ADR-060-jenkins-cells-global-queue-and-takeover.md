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
