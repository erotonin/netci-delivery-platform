# netCI — Jenkins HA: high availability and an agent fabric for open-source Jenkins

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](../LICENSE)

netCI is an Internal Developer Platform (the [README](../README.md) describes it). Its builds run on
Jenkins, and this part of netCI turns several open-source Jenkins controllers into one system that
does not lose work when a controller dies, and runs builds in isolated sandboxes on a VM fleet. It
does not replace Jenkins: teams keep their Jenkinsfiles, plugins and the Jenkins UI.

Every claim below says how it is known. "Lab" means a run on the KVM + k3s + Longhorn lab
against real Jenkins, with the evidence file named; "tested" means automated tests only.

## When a controller's machine loses power

| Guarantee | How | Status |
|---|---|---|
| The controller is taken over without a person | Each cell holds a Lease. When it goes unrenewed for 3 s, the Cell Supervisor asks the machine's power controller (SSH agent for libvirt, Redfish for servers). Once the machine is confirmed off, it releases the Lease and takes the node out of service, and Kubernetes starts the controller elsewhere on the replicated `JENKINS_HOME` | **Lab**: 81 unattended power-offs, every build resumed and finished SUCCESS, at most 1 log line lost. With the control plane at upstream timings (as on machines apart from the cells): fenced 2.7-3.8 s, build resumed after 41-50 s, median 43 s, in the latest series of 6, where every power loss also took two or three control-plane and storage leaders. With k3s's own timings, where every machine is also a control-plane node, the build resumed after 42-83 s. **Hung machines** (running, answering nothing), powered off by the supervisor through Redfish (sushy-tools' emulator in front of the VMs): 6/6 SUCCESS, machine off 19-22 s after the hang, build resumed after 69-83 s, including 3 runs where both cells were on the hung machine. **Through a BMC that behaves like a real one** (1.5 s a request, busy once, power state 8 s late): 3/3 hung machines taken over; this showed the old 20 s off-timeout was too short (now 60 s). **At scale** (kwok, a real API server and scheduler, the real supervisor and lease code): 600 cells on 200 machines observed in 0.1 s, a lost machine fenced in 2.7-2.9 s; before a fix found there, 300 cells took 11.9 s per observation and 12-17 s to fence. **Many volumes at once** (8 cells on one machine, real Longhorn volumes): moved as fast as one, every fsynced write present afterwards; the pod deleted first used to wait ~10 s for Longhorn (5 of 5), now its pods go once the storage has settled (ADR-070). ADR-060, ADR-069 and ADR-070 have the series and their evidence |
| Running Pipeline builds continue, each step once | Pipeline durability, bounded writeback (≤ 1 s), agents that reconnect to the new controller | **Lab**: 240 s builds finished SUCCESS after every power-off, steps not re-run, at most 1 of 240 log lines lost |
| A failure never takes down a healthy cell | Lease timings that outlast a control-plane stall (15 s / 10 s), renewals retried on fresh connections | **Lab**: the neighbouring cell kept its Lease and its Jenkins (run 6); found and fixed after run 5 broke it |
| Two controllers never write one `JENKINS_HOME` | The agent kills Jenkins within 10 s of losing the Lease; a guard kills it if the agent itself hangs; the supervisor releases a Lease only after confirming the machine off, conditionally on its version, and never deletes the pod of a cell that still renews (a wrong node-to-machine mapping; found and fixed in ADR-070) | **Lab**: agent hang 3.5 s, API partition 3.2 s (4 s deadline then); **tested**: every rule has a test that fails when the rule is removed |
| Queued builds are not lost | Runs live in PostgreSQL; the netCI plugin dispatches idempotently under Jenkins' queue lock | **Lab**: JVM killed under 6 queued items — 5 netCI runs each ran once, the direct trigger was lost (`queue-crash-*.json`) |
| Webhooks are not lost or doubled | GitLab/GitHub deliveries authenticated and keyed by their delivery id | **Lab**: real GitLab push → Jenkins in 8.9 s; a resent delivery made no second run (`webhook-gitlab-*.json`) |
| Build logs readable during a takeover | Fluent Bit beside each controller copies every build log to Loki as Jenkins writes it; read in Grafana while the cell is down. Runs submitted through netci-queue are in PostgreSQL. Nothing runs inside Jenkins and builds are not slowed (ADR-068) | **Lab**, 3 power-offs mid-build with Loki on its own failure domain: Jenkins down 50-95 s, and throughout Loki returned every line of the running build up to the crash; afterwards all 240 lines, 1-2 of them twice at the crash, none lost. Failures on the way, kept as evidence: Loki on the lost machine, and a takeover that preempted the build's own agent (fixed in the headroom check) |
| A step that started in the last second is not re-run | `netciOnce(key) { ... }` records the block's start in PostgreSQL, outside `JENKINS_HOME`; a block started before under a lost state is refused, and the guard fails closed (ADR-065) | **Lab**: 51 power-offs under a running `netciOnce` block, each resumed with one marker and never refused; a second attempt with another nonce got 409 from the live queue. **Tested**: JenkinsRule |
| A takeover is never stuck for want of room, and costs no running build when there is another way | Controllers outrank builds (priority classes); running builds carry a disruption budget, which the scheduler's preemption respects whenever other room exists; a cell no node can take does not hold its fenced machine off; the supervisor reports, before any loss, a cell whose only room is a running build's (ADR-069) | **Lab**: a takeover stranded for 22 min was found, and the fixed supervisor recovered it in 41 s; `NetciCellWithoutHeadroom` fired when the only room was a build's, and cleared when the build ended (`headroom-running-build-*.json`). **Real kube-scheduler** (kwok): without the budget a running build was preempted 5/5, with it 0/5; with builds on every machine the controller was still placed 2/2 |
| A controller whose `JENKINS_HOME` fails is restarted | The cell agent writes to `JENKINS_HOME` every 5 s; three failures in a row are reported on the Lease and the supervisor restarts that pod, so that the volume is mounted again, on any storage (ADR-067) | **Lab**: `JENKINS_HOME` made unwritable under a running controller; reported after 12 s, pod restarted 1 s later |
| Upgrades lose nothing | A planned restart is not a failure: the agent keeps the Lease until Jenkins exits, then releases it | **Lab**: rollback and upgrade of every component during a 240 s netciOnce build: SUCCESS, every queued run once, no supervisor action, UIs down 19-42 s (`upgrade-*.json`) |
| A failing dependency is refused, not pretended | Readiness follows the database; netciOnce fails closed; nothing is fenced on an unknown power state | **Lab**: PostgreSQL restarted and down 90 s, a BMC unreachable when a machine died (`dependency-*.json`) |
| Faults one after another do not wear it down | Each fault leaves the cluster as it found it; nothing accumulates | **Lab**: 3 h soak, 38 random faults back to back (power loss, hung machine, the supervisor's leader killed with a machine, JVM killed, PostgreSQL restarted, pod deleted): 38/38 builds SUCCESS, every invariant held between rounds (`soak-20261011T125323Z.json`) |
| Configuration changes need no restart | The cell agent applies a changed JCasC ConfigMap to the running controller; jobs new in JCasC exist from the first start | **Lab**: applied in 81 s with the same pod and no restart; a job declared only in JCasC was there after a restart, with no reload |
| A failure that needs a person is seen | 16 Prometheus alerts, each with a runbook entry, including no supervisor or queue running at all; a Grafana dashboard; every labelled series starts at 0 so the first event after a start counts | **Lab**, kube-prometheus-stack (`lab/monitoring.sh`): all 8 netCI targets up, every rule healthy; with every supervisor replica removed, `NetciSupervisorNotLeading` fired after 96 s and cleared 61 s after they returned; all 23 dashboard queries returned data through Grafana once runs and a sandbox claim had happened. **Tested**: promtool unit tests (`deploy/helm/netci/ci/test-rules.sh`) |

## Active-active: the UI and API never go down (ADR-076 to ADR-079)

The cells above keep builds when a controller's machine dies, but that controller's UI is down
until it is taken over. Active-active keeps the UI up: several replicas serve one Jenkins at once,
each with **its own `JENKINS_HOME` on its own node's disk** (`local-path`, as the site mostly runs),
kept the same through PostgreSQL by netCI's own plugin and `netci-homesync` -- not one shared home:
on Longhorn RWX that stalled every replica 61 s when the NFS server's machine died (ADR-074).
Pipeline only, inbound agents (the mentor's site). Lab: 2 replicas, an HAProxy per machine, a
kube-vip VIP, 2 inbound agents with 2 executors each.

| Guarantee | How | Status |
|---|---|---|
| The UI and API answer through a machine's power loss | HAProxy on every machine behind a kube-vip VIP (ARP, leader election on each machine's own API server); health checks every second, a failed request sent again to the other replica | **Lab**: a replica's machine 0-2 s, the VIP's machine 6 s, both on one machine 2-4 s; one sample each (`aa-phase2-*.json`) |
| A signed-in person stays signed in when their replica dies | Users replicated; a cookie signed with a key every replica has, bound to the user's seed, restored with the realm's current authorities (ADR-077) | **Lab**: machine lost, still `admin` on the other replica (`aa-phase2-20261013T151003Z.json`); pod killed, 59 of 60 polls signed in (`aa-phase3-20261013T150720Z.json`) |
| Both homes stay the same, no edit lost | Every item and user change ordered by PostgreSQL: a per-item lock, compare-and-set on its version, a feed every replica follows (ADR-076) | **Lab**: a job on r0 on r1 in 231 ms (median); ADR-074's lost update replayed, nothing lost; 40 concurrent edits on both replicas, 25 accepted, 15 refused with the holder named, both converged; a replica down while the other changed comes back with every change; store down: edits refused (503), reads served |
| An API token works on every replica | Users replicated | **Lab**: minted on r0, accepted by r1 in 0.05 s, revoked there in 0.8 s |
| Build numbers used once; every build listed everywhere | One owner per job (a lease with an epoch); each number reserved in PostgreSQL before the build takes it; finished builds published and listed by `recognizeNumber` (ADR-078) | **Lab**: listed on the other replica in 104-229 ms; a cron job on both replicas built once a minute; owner killed, numbers continued (`aa-phase4-20261013T161858Z.json`) |
| "Build now" on any replica | Made on the job's owner by the replica that got it, for the same person | **Lab**: 201, built once, on the owner |
| A running build survives its replica's death | Copied every 3 s while it runs; adopted where its agent is, once the fence says its runner is gone, claimed by compare-and-set; loading it resumes it; a returning replica sets its own unfinished builds aside (ADR-079) | **Lab**: pod killed, adopted in 4.3 s; machine lost, in 49.5 s; both SUCCESS, 90 of 90 ticks, none twice, not resumed again by the replica that came back (`aa-phase5-20261013T192242Z.json`) |
| Edits survive the store's machine | netci-homesync's PostgreSQL as three CloudNativePG instances on `local-path`, synchronous to one standby, the feed in commit order (ADR-080) | **Lab**: the primary's machine powered off under a create a second on each replica: 268 acknowledged, none missing on either, numbers kept; edits refused (503) for 28 s; a standby's machine lost: no edit refused (`aa-phase6-store-20261014T045629Z.json`, `...043239Z.json`) |
| Faults one after another under load | A soak: jenkins pod and machine, store pod and machine, homesync pod, the VIP's HAProxy, each in the middle of a 90 s build (ADR-080) | **Lab**: 11 of 11 builds SUCCESS, every tick once, each done 83-85 s after its fault; 3235 acknowledged creates on both replicas; UI 1742 of 1750 polls (`aa-phase6-soak-20261014T073506Z.json` and two more) |
| A rolling upgrade without downtime | One replica at a time; each drains first (its readiness answers 503, the balancer moves everyone) | **Lab**: UI 306 of 306 polls, 303 creates on both; one 403 per restarted replica for a client holding a session crumb, none with an API token (`aa-phase6-upgrade-20261014T083045Z.json`) |
| A queued run runs once across replicas | netci-queue in front of the replicas: the run id reserved with the build number (unique), dispatch forwarded to the job's live owner, 503 while a replica catches up (ADR-081) | **Lab**: 17 of 17 queued runs SUCCESS, each once on both replicas, through a machine's loss (`aa-phase7-queue-*.json`) |
| The IDP builds through the queue | The IDP's adapter submits to netci-queue with one entry point, retries a submit with the same key, never shows the callback token (ADR-082) | **Lab**: the generated job on both replicas in 0.16 s, the build submitted, read and cancelled by run id (`aa-idp-queue-20261014T105149Z.json`) |
| The store is backed up and restored; its feed stays small | WAL archived and a base backup daily to S3 through the Barman Cloud plugin; superseded changes compacted (ADR-083, ADR-085) | **Lab**: restored from the backups in 211 s, every item and build number identical; a lagging follower re-bootstraps after compaction |
| A partitioned machine never runs a build twice | The supervisor powers off a labelled lost node (Nova at the site, Redfish in the lab) before the out-of-service taint; replicas adopt only on that taint (ADR-084) | **Lab**: a cable pulled under a build: adopted 57.8 s after, 6 s after the machine was confirmed off; SUCCESS, every tick once (`aa-phase8-partition-20261014T171635Z.json`) |
| One chart installs it | `deploy/helm/netci-jenkins-ha`: replicas, homesync, balancer, VIP (kube-vip or Octavia), store, queue; refuses a tag without digest (ADR-085) | **Lab**: the running lab adopted and upgraded by the chart under load, UI 219 of 219, creates 220 of 220 (`aa-phase6-upgrade-20261014T175148Z.json`) |
| A form works on every replica | The CSRF crumb bound to the shared sign-in, not to one JVM's session (ADR-086) | **Lab**: a rolling restart, two people signed in through the form: 239 creates, none refused, one crumb answered by both replicas (`aa-phase6-upgrade-20261014T193437Z.json`) |
| No CloudBees limit kept | Agents keep several executors; Pipeline resumes; REST answers from any replica | Agents ran with 2 executors each. Freestyle is not covered (the site has none) |
| A rename or move goes everywhere | One change of the store moves the item, its children, its builds, lease and numbers; the other replica renames its own copy; refused while a build under it runs on any replica (ADR-087) | **Lab**: renamed on r1, on both 1.1 s later with its builds, next build #3; refused (409, "running on jenkins-0") while building, renamed once finished; moved into a folder, the folder renamed (`aa-phase9-rename-20261015T014229Z.json`) |
| Views go everywhere | Each root view is an entry of the store, published after a view request under its lock; a job's pages through `/view/<v>/` ordered and routed like its own (ADR-088) | **Lab**: a view made, changed, deleted on one replica, on the other 0.09-0.12 s later (`aa-phase10-views-20261015T024853Z.json`) |
| A drain cuts nothing in flight | HAProxy's agent check: "maint" while draining (nothing new sent, in-flight requests finish), the preStop waits until none is in flight; a changed haproxy.cfg reloaded in place, the VIP staying (ADR-089) | **Lab**: rolling restart 324/324 reads, 630 creates none missing; a config change reloaded by all 3 balancers, 242/242 reads (`aa-phase6-upgrade-20261015T024350Z.json`, `…024008Z.json`) |
| The IDP delivers on the replicas | Build pods from the replicas' Kubernetes cloud, the shared library and credentials by JCasC, the builder's AppArmor profile, private repositories read with a token file (ADR-091) | **Lab**: payments-api built through netci-queue, signed and verified, promoted dev → staging, released to prod by a second person; a real GitLab push webhook built and deployed to dev under `enforce` (`aa-idp-e2e-20261015T061405Z.json`) |
| Not done, said plainly | Edits are refused for 28-45 s while the store's primary fails over (longer when the Barman Cloud plugin's leader died with it, ADR-085); a new HAProxy image still replaces the balancers, and kube-vip can then move the VIP (ADR-089); a script that signs in with a password keeps Jenkins' own session-bound crumb (an API token needs none, ADR-086); Nova fencing, Octavia and kube-vip with allowed-address-pairs are tested against fakes or not at all, never against a real OpenStack | -- |

## Builds on the agent fabric

| Guarantee | How | Status |
|---|---|---|
| Each build gets a fresh, isolated sandbox | One pod per build, destroyed after it, with limits and a NetworkPolicy (DNS, the fabric, the controllers and addresses outside the cluster only). Isolation by trust: `standard` pools run in a user namespace on the machine's kernel; `untrusted` pools run each build in its own virtual machine (Kata Containers). A pool whose runtime the cluster lacks stops the fabric at start: no silent fallback to the shared kernel | **Lab**: `uid_map` shows container root mapped to an unprivileged host uid (`fabric-agents-*.json`); four `untrusted` builds ran in four VMs (guest kernel 6.18.35, machines 6.8.0-142), each discarded after its build, ~10 s with a warm sandbox like `standard` (`untrusted-sandboxes-20261002.json`); from both kinds of sandbox, netCI's PostgreSQL, netci-queue, the API servers, a kubelet and Prometheus were unreachable while fabric builds succeeded; a pool naming a missing RuntimeClass stopped the new replica and the running ones kept serving |
| The sandbox is bound late to whichever controller needs it | Warm pods long-poll the fabric; the claim carries the node's inbound secret, never stored | **Lab**: binding 0.01 s after the claim, agent connected 0.5 s later |
| A broken warm sandbox does not cost a build its wait | A warm sandbox that is not ready is never handed out; a warm claim not bound in 15 s fails its sandbox; the controller asks what became of its claim and stops waiting when it was given up, or after 20 s bound without an agent; claims are timed on the database's clock (ADR-071) | **Lab**: a build past two frozen warm sandboxes finished in 52.8 s; before, one build waited 353 s (`fabric-broken-sandbox-*.json`) |
| Faster than the Kubernetes plugin | Sandboxes start with the controller's jars in their agent's cache (a pool's `jarCache`, ADR-072) | **Lab, in bursts**: 4.82 s median, none over 6 s of 18, against 6.17 s. **One at a time not shown**: 3.88 s against 3.17 s (`fabric-timeline-20261012T034419Z.json`) |
| The fleet is sized ahead of demand; hosts are recycled | — | Not built |

## Layout

```
cmd/            Go binaries: cell-agent, supervisor, netci-queue, netci-fabric, netci-sandbox
internal/       lease, cellagent, fence (SSH, Redfish), supervisor, runqueue, fabric, sandbox
jenkins/plugin/ the netCI Jenkins plugin (Java): idempotent dispatch, fabric cloud
deploy/         Helm charts (platform, cell) and the supervisor's manifest
lab/            KVM + k3s + Longhorn lab, probes and chaos runs; evidence in lab/evidence/
jenkins/        controller image; plugins.txt generated from toolchain/versions.yaml
docs/decisions/ ADRs; 060-071 define this architecture, 001-059 are history
docs/TRINH-BAY-TOAN-DIEN-NETCI.md  the whole project explained, in Vietnamese
```

## Build and test

```bash
make check                       # vet, gofmt, Go tests with -race, toolchain drift
NETCI_QUEUE_TEST_DATABASE_URL=... make check-pg   # the same, refusing to skip the PostgreSQL tests
make plugin                      # the Jenkins plugin, JenkinsRule tests and SpotBugs
make image                       # the scratch image with every Go binary (clean tree only)
ci/charts.sh                     # both Helm charts lint, render and refuse what they must
deploy/helm/netci/ci/test-rules.sh   # promtool checks and unit tests of the alerts
```

## Decisions

- [ADR-060 — cells, global queue, fenced takeover](decisions/ADR-060-jenkins-cells-global-queue-and-takeover.md) (with the lab runs)
- [ADR-061 — agent fabric](decisions/ADR-061-agent-fabric-isolated-sandboxes-on-vm-hosts.md)
- [ADR-062 — repository rebuilt in Go](decisions/ADR-062-repository-rebuilt-in-go-around-jenkins-ha.md)
- [ADR-063 — durable run queue, idempotent dispatch](decisions/ADR-063-durable-run-queue-and-idempotent-dispatch.md)
- [ADR-064 — agent fabric v1](decisions/ADR-064-agent-fabric-v1-warm-sandboxes-late-binding.md)
- [ADR-065 — netciOnce](decisions/ADR-065-once-blocks-guard-steps-against-re-execution.md)
- [ADR-066 — API clients survive the death of an API server](decisions/ADR-066-api-clients-survive-the-death-of-an-api-server.md)
- [ADR-067 — netCI restarts a cell whose JENKINS_HOME fails](decisions/ADR-067-netci-restarts-a-cell-whose-jenkins-home-fails.md)
- [ADR-068 — build logs readable during a takeover](decisions/ADR-068-build-logs-readable-during-a-takeover.md)
- [ADR-069 — fencing with real BMCs, at scale, without costing a running build](decisions/ADR-069-fencing-with-real-bmcs-at-scale.md)
- [ADR-070 — cell pods go after the storage settles](decisions/ADR-070-cell-pods-go-after-the-storage-settles.md)
- [ADR-071 — upgrades, failing dependencies, cluster DNS, start time, a fabric that heals](decisions/ADR-071-what-production-asks-beyond-a-power-loss.md)
- [ADR-074 — active-active on one shared home: what breaks](decisions/ADR-074-active-active-controllers-on-one-jenkins-home.md)
- [ADR-076 — active-active on replicated homes](decisions/ADR-076-replicated-homes-not-a-shared-home.md)
- [ADR-077 — a sign-in that survives its replica](decisions/ADR-077-a-sign-in-that-survives-its-replica.md)
- [ADR-078 — one owner per job, numbers reserved in order](decisions/ADR-078-one-owner-per-job-numbers-reserved-in-order.md)
- [ADR-079 — adopting a running build](decisions/ADR-079-adopting-a-running-build.md)
- [ADR-080 — the store made HA; a rolling upgrade; a deadlock](decisions/ADR-080-the-store-made-ha.md)
- [ADR-081 — a queued run runs once across replicas](decisions/ADR-081-a-queued-run-runs-once-across-replicas.md)
- [ADR-082 — the IDP submits builds through netci-queue](decisions/ADR-082-the-idp-submits-builds-through-netci-queue.md)
- [ADR-083 — the store in production: backups and compaction](decisions/ADR-083-the-store-in-production-backups-and-compaction.md)
- [ADR-084 — fencing active-active replicas by power](decisions/ADR-084-fencing-active-active-replicas-by-power.md)
- [ADR-085 — installed by one chart; backups through the Barman Cloud plugin](decisions/ADR-085-active-active-jenkins-installed-by-one-chart.md)
- [ADR-086 — a CSRF crumb every replica accepts](decisions/ADR-086-a-crumb-every-replica-accepts.md)
- [ADR-087 — renames and moves are one change of the store](decisions/ADR-087-renames-and-moves-are-one-change-of-the-store.md)
- [ADR-088 — root views are replicated](decisions/ADR-088-root-views-are-replicated.md)
- [ADR-089 — a drain lets what is in flight finish](decisions/ADR-089-a-drain-lets-what-is-in-flight-finish.md)
- [ADR-091 — the IDP delivers on the active-active replicas](decisions/ADR-091-the-idp-delivers-on-the-active-active-replicas.md)

Deploying it at a site: [docs/JENKINS-HA-PRODUCTION.md](JENKINS-HA-PRODUCTION.md) (Vietnamese: prerequisites,
operators, Secrets, the chart, VIP, Nova fencing, agents, the IDP, checks, operations).

Installing it: [docs/INSTALL.md](INSTALL.md) (prerequisites, images, both charts, the
checks before anyone depends on it). Operating it: [the runbook](RUNBOOK.md) (every alert,
what to check, what to do) and [deploy/helm](../deploy/helm/README.md). How it compares with
CloudBees CI HA and Medik8s, and the work it rests on: [docs/research/HA-LANDSCAPE.md](research/HA-LANDSCAPE.md).

Apache-2.0. See [CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md).
