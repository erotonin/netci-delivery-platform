# ADR-030: Per-project build isolation with a warm cache; pipelines configured from a catalog

Status: Accepted.

## Context

ADR-007 made every build an ephemeral pod. Measured on 2026-09-12, that cost 3× the
wall time of a long-lived agent (`evidence/benchmarks/report.json`, 1 run each,
`cacheHitRate 0.0`): a fresh pod clones the whole repository and rebuilds every image
layer. And the isolation was per *build*, not per *project*: every pod ran in the one
`netci-build` namespace, as the one `jenkins-agent` service account, with no network
policy between projects.

Separately, "configure the pipeline from the portal" meant ticking nine hard-coded
stage names; the "Add custom stage" button was disabled with a tooltip pointing at a
catalog that did not exist. And nothing compared what two Jenkins controllers actually
loaded, so a controller changed by hand would route builds that behaved differently.

## Decision

**A namespace per project, provisioned by netCI before every launch.**
`backend/app/adapters/build_isolation.py` applies, idempotently, a Namespace
(`netci-build-<name>-<id6>`, PSA `baseline`), a ServiceAccount without an API token, a
Role+RoleBinding that lets the Jenkins controllers' identity manage pods *in that
namespace only*, an ingress-deny NetworkPolicy, and a `netci-cache` PVC. A build whose
isolation cannot be established is not dispatched (`CiLaunchError`), and outside local
mode `NETCI_BUILD_ISOLATION` must be chosen explicitly (`kubernetes` or `none`).
Readiness reports the provisioner under `ci.buildIsolation`.

**The pod is declared by the pipeline, inheriting the cloud template.** The shared
library's `agent { kubernetes { inheritFrom …; namespace …; yaml … } }` takes the
namespace, service account and claim from the build parameters netCI passes
(`NETCI_BUILD_NAMESPACE`, `NETCI_BUILD_SERVICE_ACCOUNT`, `NETCI_BUILD_CACHE_CLAIM`).
Wrapping a declarative `pipeline {}` in a scripted `podTemplate {}` was tried first and
does not work: the declarative block stops being parsed as one.

**The cache is the project's, mounted at `/netci-cache` in both containers.** The
checkout keeps a bare mirror there and clones with `--reference`; the builder keeps
image layers (`XDG_DATA_HOME`, `--layers`), Go and pip caches there. Two projects never
share a cache; the same project's builds do, across both controllers.

**The benchmark measures three modes**, each N runs: the long-lived baseline
(`netci-shared`, a reusable pod with `idleMinutes`, defined from the same pod spec by a
YAML anchor), the plain ephemeral pod, and the isolated pod with its cache. The first
run of each mode is its cold start and is reported separately from the warm figures.
On 2026-09-16, 5 runs each, with the baseline pod actually reused between builds
(`label netci-shared` + `idleMinutes 120`): baseline 20.1 s (warm ≈ 13 s), ephemeral
40.0 s, isolated 47.2 s. A per-build pod costs ≈ +26 s on this workload -- almost all of
it a fresh-workspace checkout (≈ 17 s) and pod provisioning (≈ 4 s); the project cache
hits on every warm run and recovers none of it, because a warm agent's advantage is a
warm *workspace*, which a per-build pod does not have by design. The first measurement
that day (baseline not reused, all modes ≈ 50 s) was wrong about the baseline and is
superseded (`evidence/benchmarks/report.json`). Two lab facts had to be fixed before the number
meant anything: the lab git server spoke the dumb protocol (~6 s per fetch of an
up-to-date repository) and the checkout ran through `container()`, whose per-command
exec round trip made 30 git invocations cost 18 s.

**The stage catalog is data** (`stage_catalog` table, migration 0021). Built-in stages
are the ones the shared pipeline implements; `checkout`, `build`, `sbom`,
`vulnerability-scan`, `sign` and `publish` are *required* because an artifact without
them is not deployable under netCI's policy. A platform administrator registers a
**custom stage**: a repository-relative `*.sh` path anchored after a built-in stage.
The portal never accepts a command. A module chooses its stages in Settings → Pipeline
stages; netCI resolves the order (`stage_catalog.resolve_pipeline_stages`) and hands
Jenkins `NETCI_STAGES` plus `NETCI_CUSTOM_STAGES` (JSON); the pipeline runs custom
stages right after their anchor, and fails the build if the script is not in the commit.

**Controller drift is reported, not assumed away.** `GET /api/v1/ci/controllers/drift`
compares each controller's normalized JCasC export, plugin set and non-netCI job list.

**Failover is a harness gate.** Gate 10 stops one controller, waits for readiness to
say so, starts a build, requires it to land on the survivor and finish, restarts the
controller and waits for it to rejoin; detection time and MTTR are recorded.

## Consequences

- Per-project namespaces need the build cluster's RBAC to allow netCI to create
  namespaces and bind roles; the lab's kubeconfig is admin, a deployment scopes it.
- A ReadWriteOnce cache claim serialises concurrent builds of one project on one node;
  the lab has one node. Multi-node clusters need RWX storage or a per-node cache.
- `scripts/jenkins_lab.sh` no longer starts a JNLP shared agent; the baseline is a pod.
- The systemd unit, like the docker container, is named per environment
  (`<app>-<environment>`): one host serving dev and staging otherwise shares one unit.
- The nine built-ins are seeded by the migration; the in-memory store seeds them from
  `stage_catalog.BUILTIN_STAGES`. Both must change together.

## Rejected

- Per-project *controllers*: isolation at that level multiplies operational surface for
  no security the namespace boundary does not already give.
- Letting a module type shell into a custom stage: the portal would become a remote
  shell on every build agent. A path in the reviewed repository is the boundary.
- Registry-backed layer cache (`--cache-to`): worth adding for multi-node clusters, but
  it does not help the checkout, which was the largest cold-start cost measured.
