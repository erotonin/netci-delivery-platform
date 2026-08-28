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
| queue | 0.2s | 0.2s | +0.1s |
| provisioning | 0.0s | 0.2s | +0.2s |
| checkout | 2.1s | 11.2s | +9.1s |
| build | 3.1s | 4.2s | +1.1s |
| cleanup (archive) | 1.2s | 5.7s | +4.6s |
| **total** | **8.7s** | **30.8s** | **+22.1s** |

Provisioning the pod — the cost people expect to dominate — is the smallest term, at under
a second. The real price is the empty workspace: a build that cannot see the last build's
clone pays a full checkout every time, and that scales with repository size rather than
with anything the platform controls. Teams that find this unacceptable should attack the
checkout (a shallow clone, or a warm source cache mounted read-only), not reintroduce
shared workspaces.

Two costs in the first measurements turned out not to be inherent, and removing them took
the comparison from 505% slower (a "regression" against the configured threshold) to 253%
("acceptable"): archiving `.netci-out/**` from the workspace root made Jenkins walk the
whole workspace including `.git` over the agent channel, and `cleanWs` wiped a workspace
that is deleted with the pod moments later. Both are in
`jenkins/shared-library/vars/netciPipeline.groovy`. The numbers above are after both fixes;
the point of keeping the before-and-after is that neither cost was visible until the
benchmark attributed time to the right phase.

## Verification

ISO-01 records unique pod/workspace identities and confirms deletion on all terminal paths.
BENCH-01 (`make benchmark`) re-measures the table above and fails if the ephemeral agent
ever reports a cache hit — which would mean a previous build's workspace survived, and that
the isolation this ADR is about is not real.
