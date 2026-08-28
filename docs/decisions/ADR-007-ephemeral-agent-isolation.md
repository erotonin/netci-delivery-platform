# ADR-007: Ephemeral agent isolation

Status: Accepted.

## Context

Shared agents leak workspaces/dependencies between projects and make cleanup/security claims weak.

## Decision

Each build receives a dedicated Kubernetes pod and workspace. Controller executors are zero. Cleanup covers success, failure and cancel; caches are project-scoped and external to workspace.

## Consequences

Provisioning adds latency and Kubernetes/RBAC/image dependencies. Cache and agent timing must be measured separately. Host Docker socket mounting is excluded from the production path.

### What it costs, measured

`scripts/gate_benchmark.py` runs the same pipeline on both agent types. The numbers below
are from `evidence/benchmark.json` on the local kind lab, three runs each, with the
`unit-test,build` stages and a small application — they are not a claim about production
hardware, but the shape of the result is what the decision turns on:

| Phase | Shared agent | Ephemeral pod | Extra |
|---|---:|---:|---:|
| queue | 0.3s | 1.5s | +1.2s |
| provisioning | 0.0s | 1.4s | +1.4s |
| checkout | 0.9s | 18.9s | +18.0s |
| build | 3.3s | 6.5s | +3.2s |
| cleanup (archive) | 1.0s | 10.0s | +9.0s |
| **total** | **7.7s** | **46.8s** | **+39.1s** |

Provisioning the pod — the cost people expect to dominate — is the smallest term at ~1.4s.
The real price is the empty workspace: a build that cannot see the last build's clone pays
a full checkout every time, and that scales with repository size rather than with anything
the platform controls. Teams that find this unacceptable should attack the checkout (a
shallow clone, or a warm source cache mounted read-only), not reintroduce shared workspaces.

Two costs in the earlier measurements turned out not to be inherent and were removed:
archiving `.netci-out/**` from the workspace root made Jenkins walk the whole workspace
including `.git` over the agent channel, and `cleanWs` wiped a workspace that is deleted
with the pod moments later. Both are in `jenkins/shared-library/vars/netciPipeline.groovy`.

## Verification

ISO-01 records unique pod/workspace identities and confirms deletion on all terminal paths.
BENCH-01 (`make benchmark`) re-measures the table above and fails if the ephemeral agent
ever reports a cache hit — which would mean a previous build's workspace survived, and that
the isolation this ADR is about is not real.
