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
| The controller is taken over without a person | Each cell holds a Lease. When it goes unrenewed for 3 s, the Cell Supervisor asks the machine's power controller (SSH agent for libvirt, Redfish for servers). Once the machine is confirmed off, it releases the Lease and takes the node out of service, and Kubernetes starts the controller elsewhere on the replicated `JENKINS_HOME` | **Lab**: 43 unattended power-offs, every build resumed and finished SUCCESS, at most 1 log line lost. With the control plane at upstream timings (as on machines apart from the cells): fenced 2.5-3.5 s, build resumed after 37-65 s, median 41 s. With k3s's own timings, where every machine is also a control-plane node, the build resumed after 42-83 s. ADR-060 has the series and their evidence |
| Running Pipeline builds continue, each step once | Pipeline durability, bounded writeback (≤ 1 s), agents that reconnect to the new controller | **Lab**: 240 s builds finished SUCCESS after every power-off, steps not re-run, at most 1 of 240 log lines lost |
| A failure never takes down a healthy cell | Lease timings that outlast a control-plane stall (15 s / 10 s), renewals retried on fresh connections | **Lab**: the neighbouring cell kept its Lease and its Jenkins (run 6); found and fixed after run 5 broke it |
| Two controllers never write one `JENKINS_HOME` | The agent kills Jenkins within 10 s of losing the Lease; a guard kills it if the agent itself hangs; the supervisor releases a Lease only after confirming the machine off, conditionally on its version | **Lab**: agent hang 3.5 s, API partition 3.2 s (4 s deadline then); **tested**: every rule has a test that fails when the rule is removed |
| Queued builds are not lost | Runs live in PostgreSQL; the netCI plugin dispatches idempotently under Jenkins' queue lock | **Lab**: JVM killed under 6 queued items — 5 netCI runs each ran once, the direct trigger was lost (`queue-crash-*.json`) |
| Webhooks are not lost or doubled | GitLab/GitHub deliveries authenticated and keyed by their delivery id | **Lab**: real GitLab push → Jenkins in 8.9 s; a resent delivery made no second run (`webhook-gitlab-*.json`) |
| History and logs readable during a takeover | Build records in PostgreSQL, logs to OpenSearch | Not built |
| A step that started in the last second is not re-run | `netciOnce(key) { ... }` records the block's start in PostgreSQL, outside `JENKINS_HOME`; a block started before under a lost state is refused, and the guard fails closed (ADR-065) | **Lab**: 15 power-offs under a running `netciOnce` block, each resumed with one marker and never refused; a second attempt with another nonce got 409 from the live queue. **Tested**: JenkinsRule |
| A takeover is never stuck for want of room | Controllers outrank builds (priority classes); a cell no node can take does not hold its fenced machine off; the supervisor reports, before any loss, a cell no other machine could take | **Lab**: a takeover stranded for 22 min was found, and the fixed supervisor recovered it in 41 s; the headroom check runs every 30 s |
| A controller whose `JENKINS_HOME` fails is restarted | The cell agent writes to `JENKINS_HOME` every 5 s; three failures in a row are reported on the Lease and the supervisor restarts that pod, so that the volume is mounted again, on any storage (ADR-067) | **Lab**: `JENKINS_HOME` made unwritable under a running controller; reported after 12 s, pod restarted 1 s later |
| Configuration changes need no restart | The cell agent applies a changed JCasC ConfigMap to the running controller; jobs new in JCasC exist from the first start | **Lab**: applied in 81 s with the same pod and no restart; a job declared only in JCasC was there after a restart, with no reload |

## Builds on the agent fabric

| Guarantee | How | Status |
|---|---|---|
| Each build gets a fresh, isolated sandbox | One pod per build, user namespace, no API credential, limits | **Lab**: `uid_map` shows container root mapped to an unprivileged host uid (`fabric-agents-*.json`) |
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

Operating it: [the runbook](docs/RUNBOOK.md) (every alert, what to check, what to do) and
[deploy/helm](deploy/helm/README.md).

Apache-2.0. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
