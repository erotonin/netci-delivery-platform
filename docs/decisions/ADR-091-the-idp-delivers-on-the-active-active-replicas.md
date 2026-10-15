# ADR-091: The IDP delivers on the active-active replicas, end to end

- Status: accepted; run live on the HA lab (below)
- Date: 2026-10-15
- Refines: ADR-082 (IDP through netci-queue), ADR-043 (build once, promote), ADR-085 (the chart)

## Context

The IDP's last full live delivery (rc20, September) ran on the systemd lab, against two single
Jenkins controllers. That stack is stopped, and the host has no memory to run it beside the HA lab.
ADR-082 had sent a build from the IDP to the active-active replicas, but the build failed when it
loaded the pipeline, because the replicas had no netCI shared library. Nothing had built, signed,
deployed or released an application on them.

The user asked whether the CI/CD "still works". The honest answer needed a run on what exists now.

## Decision

1. **The replicas get what the pipeline needs** (`lab/aa/casc-ci.yaml`, merged by
   `lab/aa/idp.sh casc`):
   - the shared library, from the corp GitLab;
   - credentials, read from files (`${readFile:...}`) mounted by the chart's new
     `jenkins.extraSecretMounts`, never written into the ConfigMap;
   - a Kubernetes cloud whose build pods run in `aa-build`. The chart's new `buildPods` gives the
     replicas a Role there, and the pods' own account no token.

   Each replica's pods dial back to that replica (`${POD_NAME}`): a pod is an agent of the
   controller that created it, not of the balancer.
2. **The build container runs under its own AppArmor profile** (`deploy/apparmor/netci-builder`).
   Rootless Buildah unpacks layers in a user namespace it creates. On Ubuntu 24.04 nodes the
   runtime's default profile denies mount, and `kernel.apparmor_restrict_unprivileged_userns=1`
   strips the namespace of capabilities; the first build failed so ("remount /: permission denied").
   - The profile grants `userns` and `mount`. The kernel bounds both to the build's own namespace.
   - The node's guard stays on for every other process.
   - It must be loaded on each build node.
3. **The API reads private repositories** with `NETCI_GIT_CREDENTIALS_FILE`: a git credential-store
   file holding a read token. Any helper of the API's user is reset first, and the token is never
   an argument or part of a URL. Before this, the API read repositories anonymously, so for a
   private repository the run dialog listed no branch and no commit. A file that is set but
   unreadable is refused, not ignored.
4. **The Kubernetes playbooks use the kubeconfig netCI names, with its own current context.** They
   forced `kind-netci-local`, the first lab's cluster, so they could not reach any other cluster.
   The worker reaches the cluster as `netci-deployer`: `admin` in `dev`, `staging` and `prod`,
   nothing else (`lab/aa/idp.sh deploy-prepare`).
5. **The defects the live run found are fixed where they were:**
   - **The sample chart enabled KEDA autoscaling by default**, so any cluster without KEDA refused
     the whole release. It is now off by default.
   - **The worker's cosign did not trust the registry's CA.** The TLS attempt failed and
     go-containerregistry fell back to plain HTTP on an RFC 1918 address. The worker is now given
     the CA (`SSL_CERT_DIR`), as the build pods are.
   - **netCI's own Jenkins plugin was undeclared in the toolchain**, so the controller netCI ships
     failed its own gate under `NETCI_TOOLCHAIN_ENFORCE=enforce`. It is now declared as
     `jenkins.ownPlugins` (checked, never sent to the update site) and kept equal to the plugin's
     `pom.xml` revision by a test.
   - **The module views omitted what a run was.** A build-only run read "deploys to dev", and a
     production request's release run, which builds nothing, read "manual build". All routes now
     answer with one run projection (`projections/runs.py`), which says `promotedFrom` and
     `productionRequestId`.
6. **The scan's Trivy DB is mirrored on a timer** (`netci-trivy-db-mirror`, now enabled). The lab's
   IDP runs with `NETCI_TOOLCHAIN_ENFORCE=enforce`.

## Measured on the HA lab

`lab/aa/idp_e2e.py` drives the IDP's public API as people do, signed in through Keycloak: dana is a
developer, rae a reviewer. The evidence is in `lab/evidence/aa-idp-e2e-*.json`.

| Step | Result |
|---|---|
| Build `payments-api` 545d56a (build only), through netci-queue on the replicas | checkout, unit tests, build, SBOM, scan, sign and publish all succeeded; digest `sha256:f28ea2ce…`; signature and provenance verified by the IDP (`…055522Z`) |
| Promote that digest to dev, then to staging | both healthy; no rebuild; the pods run `apps/payments-api@sha256:f28ea2ce…` |
| Register v1.0.0; dana requests production; dana approves her own request | refused, 403 |
| rae approves | production deployment healthy (`…055944Z`) |
| A push to GitLab main (b0920c6) | the GitLab webhook made a run: event `push`, rule 0, deploy to dev |
| The same build under `enforce` | refused at first: netCI's own plugin was undeclared (decision 5) |
| Retried after the fix | all 9 stages, deploy and health check included; dev healthy on `sha256:f770e5bc…`, while staging and prod stay on v1.0.0 (`…061405Z`) |

The rolling upgrades that applied the chart changes failed nothing: 329 of 329 reads,
`aa-phase6-upgrade-20261015T053255Z.json`.

## What it costs, said plainly

- **The pipeline needed Kubernetes build pods.** `netciPipeline` declared `agent { kubernetes { … } }`,
  so it could not run on inbound agents on a VM, which is the mentor's layout. ADR-092 added that.
- **A build pod is an agent of one replica.** If that replica dies mid-build, the build is not
  adopted (ADR-079 adopts builds on inbound agents, which reconnect). It fails and is retried.
- **The builder's AppArmor profile is node configuration.** A node without it cannot run a build.
- **The lab's environments are three namespaces of one cluster**, and its production approval was
  given within a minute. Real separation of clusters and real change windows are a site's
  configuration.
- **Build isolation is `none` on the lab.** Every build shares `aa-build`; per-project namespaces
  (ADR-030) were not exercised.
