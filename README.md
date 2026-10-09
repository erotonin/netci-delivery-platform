# netCI — High availability and an agent fabric for open-source Jenkins

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

netCI turns several open-source Jenkins controllers into one system that does not lose work
when a controller dies, and runs builds in isolated sandboxes on a VM fleet. It does not replace
Jenkins: teams keep their Jenkinsfiles, plugins and the Jenkins UI. The previous product (portal,
Temporal CD, catalog) is at the tag `netci-0.3-cd-portal`.

Every claim below says how it is known. "Lab" means a run on the KVM + k3s + Longhorn lab
against real Jenkins, with the evidence file named; "tested" means automated tests only.

## When a controller's machine loses power

| Guarantee | How | Status |
|---|---|---|
| The controller is taken over without a person | Each cell holds a Lease. When it goes unrenewed for 3 s, the Cell Supervisor asks the machine's power controller (SSH agent for libvirt, Redfish for servers). Once the machine is confirmed off, it releases the Lease and takes the node out of service, and Kubernetes starts the controller elsewhere on the replicated `JENKINS_HOME` | **Lab**: 81 unattended power-offs, every build resumed and finished SUCCESS, at most 1 log line lost. With the control plane at upstream timings (as on machines apart from the cells): fenced 2.7-3.8 s, build resumed after 41-50 s, median 43 s, in the latest series of 6, where every power loss also took two or three control-plane and storage leaders. With k3s's own timings, where every machine is also a control-plane node, the build resumed after 42-83 s. **Hung machines** (running, answering nothing), powered off by the supervisor through Redfish (sushy-tools' emulator in front of the VMs): 6/6 SUCCESS, machine off 19-22 s after the hang, build resumed after 69-83 s, including 3 runs where both cells were on the hung machine. **Through a BMC that behaves like a real one** (1.5 s a request, busy once, power state 8 s late): 3/3 hung machines taken over; this showed the old 20 s off-timeout was too short (now 60 s). **At scale** (kwok, a real API server and scheduler, the real supervisor and lease code): 600 cells on 200 machines observed in 0.1 s, a lost machine fenced in 2.7-2.9 s; before a fix found there, 300 cells took 11.9 s per observation and 12-17 s to fence. ADR-060 and ADR-069 have the series and their evidence |
| Running Pipeline builds continue, each step once | Pipeline durability, bounded writeback (≤ 1 s), agents that reconnect to the new controller | **Lab**: 240 s builds finished SUCCESS after every power-off, steps not re-run, at most 1 of 240 log lines lost |
| A failure never takes down a healthy cell | Lease timings that outlast a control-plane stall (15 s / 10 s), renewals retried on fresh connections | **Lab**: the neighbouring cell kept its Lease and its Jenkins (run 6); found and fixed after run 5 broke it |
| Two controllers never write one `JENKINS_HOME` | The agent kills Jenkins within 10 s of losing the Lease; a guard kills it if the agent itself hangs; the supervisor releases a Lease only after confirming the machine off, conditionally on its version | **Lab**: agent hang 3.5 s, API partition 3.2 s (4 s deadline then); **tested**: every rule has a test that fails when the rule is removed |
| Queued builds are not lost | Runs live in PostgreSQL; the netCI plugin dispatches idempotently under Jenkins' queue lock | **Lab**: JVM killed under 6 queued items — 5 netCI runs each ran once, the direct trigger was lost (`queue-crash-*.json`) |
| Webhooks are not lost or doubled | GitLab/GitHub deliveries authenticated and keyed by their delivery id | **Lab**: real GitLab push → Jenkins in 8.9 s; a resent delivery made no second run (`webhook-gitlab-*.json`) |
| Build logs readable during a takeover | Fluent Bit beside each controller copies every build log to Loki as Jenkins writes it; read in Grafana while the cell is down. Runs submitted through netci-queue are in PostgreSQL. Nothing runs inside Jenkins and builds are not slowed (ADR-068) | **Lab**, 3 power-offs mid-build with Loki on its own failure domain: Jenkins down 50-95 s, and throughout Loki returned every line of the running build up to the crash; afterwards all 240 lines, 1-2 of them twice at the crash, none lost. Failures on the way, kept as evidence: Loki on the lost machine, and a takeover that preempted the build's own agent (fixed in the headroom check) |
| A step that started in the last second is not re-run | `netciOnce(key) { ... }` records the block's start in PostgreSQL, outside `JENKINS_HOME`; a block started before under a lost state is refused, and the guard fails closed (ADR-065) | **Lab**: 51 power-offs under a running `netciOnce` block, each resumed with one marker and never refused; a second attempt with another nonce got 409 from the live queue. **Tested**: JenkinsRule |
| A takeover is never stuck for want of room, and costs no running build when there is another way | Controllers outrank builds (priority classes); running builds carry a disruption budget, which the scheduler's preemption respects whenever other room exists; a cell no node can take does not hold its fenced machine off; the supervisor reports, before any loss, a cell whose only room is a running build's (ADR-069) | **Lab**: a takeover stranded for 22 min was found, and the fixed supervisor recovered it in 41 s; `NetciCellWithoutHeadroom` fired when the only room was a build's, and cleared when the build ended (`headroom-running-build-*.json`). **Real kube-scheduler** (kwok): without the budget a running build was preempted 5/5, with it 0/5; with builds on every machine the controller was still placed 2/2 |
| A controller whose `JENKINS_HOME` fails is restarted | The cell agent writes to `JENKINS_HOME` every 5 s; three failures in a row are reported on the Lease and the supervisor restarts that pod, so that the volume is mounted again, on any storage (ADR-067) | **Lab**: `JENKINS_HOME` made unwritable under a running controller; reported after 12 s, pod restarted 1 s later |
| Configuration changes need no restart | The cell agent applies a changed JCasC ConfigMap to the running controller; jobs new in JCasC exist from the first start | **Lab**: applied in 81 s with the same pod and no restart; a job declared only in JCasC was there after a restart, with no reload |
| A failure that needs a person is seen | 14 Prometheus alerts, each with a runbook entry, including no supervisor or queue running at all; a Grafana dashboard; every labelled series starts at 0 so the first event after a start counts | **Lab**, kube-prometheus-stack (`lab/monitoring.sh`): all 8 netCI targets up, every rule healthy; with every supervisor replica removed, `NetciSupervisorNotLeading` fired after 96 s and cleared 61 s after they returned; all 23 dashboard queries returned data through Grafana once runs and a sandbox claim had happened. **Tested**: promtool unit tests (`deploy/helm/netci/ci/test-rules.sh`) |

## Builds on the agent fabric

| Guarantee | How | Status |
|---|---|---|
| Each build gets a fresh, isolated sandbox | One pod per build, destroyed after it, with limits and a NetworkPolicy (DNS, the fabric, the controllers and addresses outside the cluster only). Isolation by trust: `standard` pools run in a user namespace on the machine's kernel; `untrusted` pools run each build in its own virtual machine (Kata Containers). A pool whose runtime the cluster lacks stops the fabric at start: no silent fallback to the shared kernel | **Lab**: `uid_map` shows container root mapped to an unprivileged host uid (`fabric-agents-*.json`); four `untrusted` builds ran in four VMs (guest kernel 6.18.35, machines 6.8.0-142), each discarded after its build, ~10 s with a warm sandbox like `standard` (`untrusted-sandboxes-20261002.json`); from both kinds of sandbox, netCI's PostgreSQL, netci-queue, the API servers, a kubelet and Prometheus were unreachable while fabric builds succeeded; a pool naming a missing RuntimeClass stopped the new replica and the running ones kept serving |
| The sandbox is bound late to whichever controller needs it | Warm pods long-poll the fabric; the claim carries the node's inbound secret, never stored | **Lab**: binding 0.01 s after the claim, agent connected 0.5 s later |
| Faster than the Kubernetes plugin | — | **Not shown.** On an idle lab with cached images it is slower (5.9 s vs 4.4 s median); see ADR-064 |
| The fleet is sized ahead of demand; hosts are recycled | — | Not built |

## Layout

```
cmd/            Go binaries: cell-agent, supervisor, netci-queue, netci-fabric, netci-sandbox
internal/       lease, cellagent, fence (SSH, Redfish), supervisor, runqueue, fabric, sandbox
jenkins/plugin/ the netCI Jenkins plugin (Java): idempotent dispatch, fabric cloud
deploy/         Helm charts (platform, cell) and the supervisor's manifest
lab/            KVM + k3s + Longhorn lab, probes and chaos runs; evidence in lab/evidence/
jenkins/        controller image; plugins.txt generated from toolchain/versions.yaml
docs/decisions/ ADRs; 060-064 define this architecture, 001-059 are history
```

## Build and test

```bash
make check                       # vet, gofmt, Go tests with -race, toolchain drift
NETCI_QUEUE_TEST_DATABASE_URL=... make check-pg   # the same, refusing to skip the PostgreSQL tests
make plugin                      # the Jenkins plugin, JenkinsRule tests and SpotBugs
make image                       # the scratch image with every Go binary (clean tree only)
```

## Decisions

- [ADR-060 — cells, global queue, fenced takeover](docs/decisions/ADR-060-jenkins-cells-global-queue-and-takeover.md) (with the lab runs)
- [ADR-061 — agent fabric](docs/decisions/ADR-061-agent-fabric-isolated-sandboxes-on-vm-hosts.md)
- [ADR-062 — repository rebuilt in Go](docs/decisions/ADR-062-repository-rebuilt-in-go-around-jenkins-ha.md)
- [ADR-063 — durable run queue, idempotent dispatch](docs/decisions/ADR-063-durable-run-queue-and-idempotent-dispatch.md)
- [ADR-064 — agent fabric v1](docs/decisions/ADR-064-agent-fabric-v1-warm-sandboxes-late-binding.md)
- [ADR-065 — netciOnce](docs/decisions/ADR-065-once-blocks-guard-steps-against-re-execution.md)
- [ADR-066 — API clients survive the death of an API server](docs/decisions/ADR-066-api-clients-survive-the-death-of-an-api-server.md)
- [ADR-067 — netCI restarts a cell whose JENKINS_HOME fails](docs/decisions/ADR-067-netci-restarts-a-cell-whose-jenkins-home-fails.md)

Installing it: [docs/INSTALL.md](docs/INSTALL.md) (prerequisites, images, both charts, the
checks before anyone depends on it). Operating it: [the runbook](docs/RUNBOOK.md) (every alert,
what to check, what to do) and [deploy/helm](deploy/helm/README.md). How it compares with
CloudBees CI HA and Medik8s, and the work it rests on: [docs/research/HA-LANDSCAPE.md](docs/research/HA-LANDSCAPE.md).

Apache-2.0. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
