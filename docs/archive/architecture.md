# Architecture

Status: accepted target architecture; runtime verification pending on Ubuntu 24.04.

## Context and flow

```text
Portal ---------\
Backstage -------+--> netCI API --> Domain / Policy / Audit
Git trigger -----/          |              |
                            |              +--> Temporal workflow (long-running)
                            +--> direct ports (short status/log operations)
                                           |
                                    Jenkins Router
                                      /         \
                              Jenkins A       Jenkins B
                                    \          /
                                     ephemeral agent
                                            |
                              test/build/SBOM/scan/sign/publish
                                            |
                                  immutable artifact digest
                                   /          |          \
                           Docker VM       kind/Helm    Systemd VM
```

## Component ownership

| Component | Owns | Does not own |
|---|---|---|
| Portal/Backstage | input, presentation, client-side validation | domain decisions, Jenkins calls |
| netCI API | application identity, run/deployment lifecycle, policy, approval, audit | build execution |
| Temporal | durable orchestration, retry/timeout/wait/compensation | synchronous reads |
| Jenkins | CI execution and build logs | promotion/deployment identity |
| Runtime adapter | target-specific validate/deploy/status/health/rollback | cross-runtime policy |
| Registry/MinIO | artifact and evidence bytes | release decision |
| DORA projector | event-to-metric projection | mutation of delivery state |

## Deployment topology

Local reference topology uses Docker Compose for platform services, kind for Kubernetes workloads/agents, and two KVM/libvirt Ubuntu VMs for Docker and native Systemd targets. Jenkins A/B on one host demonstrate routing behavior only; this is not a production failure domain.

## Architectural invariants

- Git/JCasC is the controller configuration source of truth.
- Every application has a netCI ID independent from Jenkins job/run IDs.
- CI emits an immutable digest; CD never rebuilds during promotion.
- Production deployment requires policy success and approval.
- Provider-specific code stays behind ports/adapters.
- Every asynchronous action carries correlation and idempotency data.

See ADR-001 through ADR-010 in `docs/decisions/` for the decisions behind these boundaries.
