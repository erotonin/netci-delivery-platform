# ADR-074: Active-active Jenkins on one JENKINS_HOME -- what breaks, and the plan

- Status: superseded in its storage decision by ADR-076 (the mentor's answers: Pipeline only, inbound
  agents, storage mostly local-path). Phases 0 and 1 stand as evidence.
- Date: 2026-10-13
- Refines: ADR-060 (one writer per cell), ADR-071 (UI unavailable during a takeover)

## Context

The project's mentor asks that Jenkins' UI and API never go down, with as few of CloudBees'
active/active limits as possible, on Viettel's infrastructure. What is known of it:
- controllers on Kubernetes;
- agents on one large OpenStack VM with many executors and labels;
- builds never on a controller;
- 3 VMs or nodes on separate hosts may be had;
- our own load balancer, plugin and PostgreSQL are allowed.

Still open: whether jobs are Pipeline or Freestyle, whether agents connect over SSH or inbound,
and what shared file system there is. The lab is to cover each case.
(Answered 2026-10-13: Pipeline only; inbound JNLP agents; mostly local-path, Longhorn present. See ADR-076.)

CloudBees runs several replicas of a controller on one `JENKINS_HOME` on a shared file system,
keeps live state in Hazelcast, needs sticky sessions, and has a replica adopt a dead one's
builds. Its documented limits:
- non-Pipeline builds are aborted when their replica ends;
- one executor per agent;
- `build` and `lock` restricted;
- REST answers from one replica.

What open-source Jenkins does with a shared home and nothing coordinating it had to be seen
first.

## Phase 0: two controllers, one home, no coordination

`lab/aa/phase0.yaml` and `lab/aa/phase0_probe.py`: namespace `aa-lab`, a Longhorn RWX volume
(NFS), replicas r1 and r2 of the lab's controller image on different machines, JCasC-defined jobs.

| # | Experiment | Result | Evidence |
|---|---|---|---|
| F1 | Both started together, plugins unzipped into `JENKINS_HOME/plugins` (Jenkins' default) | Each deleted the other's unzipped files: r1 55 SEVERE, 13 plugins failed; r2 failed to load configuration-as-code, so JCasC never ran and neither had a job | `aa-phase0-startup-20261013T020719Z.json` |
| E1 | Both started, plugins and WAR unzipped on each pod's own disk | Both up in 51 s, both see the jobs. Nothing stops a second controller on a home | `aa-phase0-20261013T021729Z.json` |
| E2 | A build run on r1 | r2 does not know it (404) although it is on disk; r2's job shows no build | same |
| E3 | The same job triggered on both at once | Numbers 2 and 3, no collision in two runs; each replica sees only its own builds | same |
| E4 | A description set on r1, then the job disabled on r2 | Lost update: `config.xml` holds r2's save with an empty description; r1 still shows its own in memory | same |
| E5 | r1 killed in a build's 8th second | r2 does not see the build. After r2 restarted, it loaded the build from disk and resumed it ("Resuming build ... after Jenkins restart"). The step's process had run on r1's built-in node and died with it, so the build waits for it | same |

What it shows:
- **Jenkins keeps everything in one JVM's memory** (jobs, builds, numbers, configuration) and
  treats disk as its own. A shared home without coordination gives every kind of divergence:
  invisible builds, lost updates, and nothing stopping two writers.
- **Adoption exists.** A controller that loads a running Pipeline from disk resumes it. This is the
  mechanism to use. It runs only at start, it needs the build's previous owner provably stopped
  (or the build runs twice), and it needs the step's process on an agent the adopter can reach.

## Phase 1: the shared home's cost, and what it does when its machine dies

`lab/aa/phase1_storage_probe.py`: on the replica that is *not* on the share-manager's machine, 4 KiB
writes with `fsync` every 0.2 s, on the home and on the pod's own disk; then a writer loop on the home
and the replica's `/login` asked every 0.5 s while the machine running Longhorn's share-manager (the
NFS server of the RWX volume) loses power (`virsh destroy`). One run per setting.

| Setting | Write latency, home vs local (median / p99 ms) | Writes stalled after power loss | `/login` failures | Evidence |
|---|---|---|---|---|
| Longhorn default | 8.0 / 15.0 vs 4.5 / 15.1 | **133.7 s** | 0 of 496 | `aa-phase1-storage-20261013T023735Z.json` |
| `rwx-volume-fast-failover=true` | 8.9 / 17.1 vs 4.9 / 18.4 | **61.0 s** | 0 of 497 | `aa-phase1-storage-20261013T024510Z.json` |

What it shows:
- **Steady state is not the problem.** A synced write to the home costs about twice the local disk,
  a few milliseconds; Jenkins' own saves are not on a hot path where that matters.
- **The failover is.** Every writer on the home blocks until the share-manager is up on another
  machine: about a minute even with fast failover. `/login` kept answering only because it never
  touches the home. Any request that reads or saves a build, a job or a log would hang as long, on
  *every* replica at once, since they share the one NFS server. That is an outage of the UI in all
  but name. So Longhorn RWX alone does not meet "the UI never goes down".
- So point 8 below: the controllers must not block on the home. What the home holds stays there, but
  a replica must keep serving while the home is away, and the storage choice must be measured the
  same way for each option the mentor's site may have (an HA NAS over NFS, CephFS with a standby
  MDS), with the probe above.
- Kept: `rwx-volume-fast-failover=true` on the lab's Longhorn. It halves the stall and costs nothing
  measured.

## Decision: the design the next phases build

1. **Nothing that is not state on the shared home.** WAR, unzipped plugins and caches are on each
   replica's own disk (F1).
2. **PostgreSQL coordinates, as Hazelcast does for CloudBees** (netCI already runs one):
   - replica membership with leases and epochs (as cells hold theirs);
   - the owner of each running build;
   - build numbers allocated atomically per job;
   - a per-item lock for configuration writes;
   - change notifications (`LISTEN`/`NOTIFY`), so replicas reload an item another replica changed.
3. **One owner per build.** netci-queue dispatches each run to one replica. Requests for a build's
   pages go to its owner (the load balancer asks), so its live state is never read from a replica
   that does not hold it.
4. **Adoption after fencing.** When a replica stops renewing, its machine or pod is fenced first
   (the supervisor and fence code, ADR-060 and ADR-069). Only then does a surviving replica take
   its builds and resume them from the home, without restarting itself.
5. **Agents outside the replicas.** The large VM's executors become single-executor agents with the
   same labels: same capacity, one connection per build. Each agent reconnects to whichever replica
   owns its build; SSH and inbound agents are both to be covered.
6. **Our own load balancer** (HAProxy): sticky sessions, health checks, and build URLs routed to
   their owner.
7. **Every CloudBees limit is to be removed or measured**:
   - one executor per agent: point 5, with no loss of capacity;
   - `build` and `lock`: tracked in PostgreSQL;
   - REST answers from one replica: merged by the proxy;
   - Freestyle: cannot resume in Jenkins at all, so it is re-run from the start and that is said
     plainly.

8. **Requests do not wait on the shared home** (phase 1). Reads a replica can answer from memory
   are answered from memory; a request that needs the home while it is away gets a bounded wait and
   an honest error, never a hang; the load balancer's health check tells "this replica is stuck on
   storage" from "this replica is down", so a storage stall does not look like every replica dying.
   Which store the mentor's site has decides how long the home is away; the probe measures it.

## Phases

| Phase | What | Pass |
|---|---|---|
| 0 | Uncoordinated replicas | Done: the table above |
| 1 | The shared file system's latency, and its failover when the machine holding it dies (Longhorn RWX now; NFS and CephFS options) | Done for Longhorn RWX: it does not, alone (61 s stall); NFS and CephFS still to measure |
| 2 | Load balancer + replicas serving the UI; a machine loses power | UI and API probed every second, none failing (or within the mentor's bound) |
| 3 | PostgreSQL coordination: ownership, numbers, configuration; adoption without restart | A running build on a dead replica carries on, every step once |
| 4 | Agents (single-executor, SSH and inbound), cron and polling once per cluster, `build`, `lock` | Each correct with a replica lost |
| 5 | Chaos and soak, as for the cells | Evidence in `lab/evidence/` |

## Rejected

- **Two `JENKINS_HOME` copies synchronised file by file** (rsync, lsyncd, DRBD dual-primary). Two
  JVMs writing the same XML files diverge, as E4 shows on one home. Copying the files does not order
  the writes.
- **Restarting the surviving replica to adopt** (what E5 did). Its users and its own builds would see
  it go down, which is what active-active is for.
