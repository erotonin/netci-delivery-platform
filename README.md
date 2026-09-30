# netCI — High availability and an agent fabric for open-source Jenkins

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

netCI turns several open-source Jenkins controllers into one system that does not lose work
when a controller dies, and runs builds in isolated sandboxes on a VM fleet that is sized
ahead of demand. It does not replace Jenkins: teams keep their Jenkinsfiles and the Jenkins UI.

> **Status: re-architecture in progress (2026-10).** Nothing below is verified yet. The first
> milestone is a spike on the lab that proves or disproves the assumptions the design rests
> on. The previous product (portal, Temporal CD, catalog) is at the tag `netci-0.3-cd-portal`.

## Guarantees it is built to give

| When a controller dies | Mechanism | ADR |
|---|---|---|
| Build history is not lost | `JENKINS_HOME` on a synchronously replicated volume (RPO 0) | 060 |
| Logs are not lost and stay readable | Pipeline logs stream to OpenSearch as they are written | 060 |
| Queued builds are not lost | The queue lives in PostgreSQL outside Jenkins; Jenkins' own queue is lost on a crash | 060 |
| Running Pipeline builds continue | Fenced takeover on the same volume in ≤ 60 s p95, agents reconnect, Pipeline resumes | 060 |
| New builds are not blocked | Several cells built from the same sources; dispatch goes to healthy ones | 060 |
| A deployment is not run twice | Deploy stages are guarded after a takeover; deployment leases carry fencing tokens | 060 |

| For builds | Mechanism | ADR |
|---|---|---|
| Each build is clean and isolated | One sandbox per build on a VM host; isolation class by trust (user-namespaced runc, Sysbox, Kata/gVisor) | 061 |
| Builds start in about a second | Warm sandboxes bound to a controller only when claimed | 061 |
| The fleet fits the demand | Hosts added from the global queue's projection, drained and replaced after N builds or M hours | 061 |

## Layout

```
cmd/            one Go binary per service (intake, supervisor, fabric, readpath)
internal/       shared Go packages
jenkins-plugin/ the Jenkins plugin (Java): fabric cloud, dispatch bridge, deploy guard
deploy/         Helm charts for the services and a cell
lab/            KVM + k3s + Longhorn lab, spike and chaos suite
jenkins/        controller image; plugins.txt generated from toolchain/versions.yaml
toolchain/      the one declaration of tool and plugin versions
docs/decisions/ ADRs; 060-062 define this architecture, 001-059 are history
```

## Build

```bash
make check      # go vet, gofmt, tests with -race, toolchain drift
make toolchain  # regenerate jenkins/plugins.txt after editing toolchain/versions.yaml
```

## Decisions

- [ADR-060 — cells, global queue, fenced takeover](docs/decisions/ADR-060-jenkins-cells-global-queue-and-takeover.md)
- [ADR-061 — agent fabric](docs/decisions/ADR-061-agent-fabric-isolated-sandboxes-on-vm-hosts.md)
- [ADR-062 — repository rebuilt in Go](docs/decisions/ADR-062-repository-rebuilt-in-go-around-jenkins-ha.md)
- [Research: Jenkins HA and DR](docs/research/jenkins-ha-dr.md)

Apache-2.0. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
