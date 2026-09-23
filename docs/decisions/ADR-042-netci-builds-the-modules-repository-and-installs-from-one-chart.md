# ADR-042: netCI builds the module's own repository, and installs from one chart

Status: Accepted. Supersedes the plain-manifest half of ADR-040.

## Context

The goal was "install netCI once, change the Jenkins endpoint, and it runs" against an
organisation's existing Jenkins. Trying to do exactly that, in the lab's kind cluster
against the lab's Jenkins A/B, surfaced three layers of defects -- none of which any test
or `helm lint` had caught.

**netCI could not build any repository but its own.** The pipeline ran
`scripts/netci_callback.py` and `templates/<t>/scripts/ci/*.sh` from the checked-out
repository. That worked only because every lab module's repository URL was the netCI
repository itself (`http://172.17.0.52/netci.git`). Neither the application directory nor
an image name reached Jenkins -- `NETCI_APP_DIR` was an allow-listed build input that
`trigger_ci_run` never sent -- so the CI scripts fell back to their defaults,
`sample-apps/hello-container` and image `hello-container`. The lab registry held
`hello-container` and no `payment-gateway` or `shop-api` image at all, after 7 and 4
"successful" builds of those modules: every container module had built and pushed the
same sample app.

**The images were not self-contained.** The API image had no migrations, so a fresh
cluster came up against an empty database. The worker image had no playbooks (compose
bind-mounts the checkout; nothing else does) and no `helm`. The API image had no `git`
(the run dialog's branch list), no `kubectl` (build isolation, traffic router) and no
`cosign` (readiness and signature verification). The portal's nginx proxied to the literal
host `netci-api`, which exists only in compose, and failed to start elsewhere.

**Chart 1.0.0 could not start netCI.** It omitted `NETCI_AUTH_MODE` and `NETCI_CD_MODE`,
which production refuses to start without; set `TEMPORAL_HOST` and `REGISTRY_PUSH_HOST`
where the code reads `TEMPORAL_ADDRESS` and `NETCI_REGISTRY_PUSH_HOST`; left
`NETCI_CALLBACK_URL` at its default `http://host.docker.internal:8000`; routed `/api`
through the Ingress without stripping the prefix, so every call 404'd; exposed `/metrics`;
and probed `/metrics`, which runs every readiness check, every five seconds. The static
manifests in `deploy/k8s` carried the same defects, which is ADR-040's accepted cost --
two descriptions of one deployment -- arriving immediately.

## Decision

1. **The shared library carries its own tooling.** `resources/netci/tooling/` is a copy of
   `templates/*/scripts/ci/` and `scripts/netci_callback.py`, kept identical by
   `scripts/sync_shared_library.py --check` (the arrangement `schema.sql` has with its
   migrations). `vars/netciTooling.groovy` writes it to `WORKSPACE_TMP` before checkout:
   outside the build context, so it cannot be copied into an image, and present even when
   checkout fails, so the failure can be reported. The tooling is pinned to the library
   version the job loads, not to whatever the application's commit contains.
2. **netCI tells the build what to build and what to call it.** `NETCI_APP_DIR` (a build
   input, validated as a relative path with no `..`) and `NETCI_IMAGE_NAME` (decided by the
   server from the application name; a caller cannot set it, so one module cannot publish
   as another) are job parameters. A build netCI dispatched without an image name fails
   rather than falling back to the sample default.
3. **Each image contains what its process runs.** Migrations and their runner in the API
   image; playbooks, the workload chart and `helm` in the worker; `git`, `kubectl` and
   `cosign` in the API. Downloaded tools are checked against published SHA-256 digests.
   The portal's API upstream is configuration (`NETCI_API_UPSTREAM`).
4. **The chart is built from the configuration the lab actually runs**
   (`infra/lab/real-local.env.template`), not from recollection. Every credential is a file;
   the chart refuses to render, naming the value, rather than install something that
   cannot work. Migrations run in an init container of each API pod, made safe by an
   advisory lock in `scripts/migrate.py`; a pre-install hook was rejected because it runs
   before the release's Secret exists. Probes are `/livez` and `/healthz`, not `/readyz`,
   so a Jenkins restart does not take every API pod out of rotation.
5. **One source of truth for deployment.** `deploy/k8s` is removed. Plain manifests come
   from `helm template -f <your values>`, which is correct for those values and needs no
   Helm in the cluster or the change window -- the audience ADR-040 kept them for.

## Consequences

- `tests/contract/test_deployment_package.py` renders the chart and fails if it sets a
  variable the code does not read, omits one production requires, renders a credential as
  a plain variable, or if the library's tooling copy has drifted. It fails on chart 1.0.0's
  `TEMPORAL_HOST` from both directions.
- `scripts/jenkins_preflight.py` checks a target controller read-only: authentication,
  crumb, plugins, permissions, library, the cosign credential, agents.
- Live evidence (2026-09-23): installed into an empty database; two API pods migrated
  concurrently (one applied 27, one waited and found none pending); `/readyz` 200 with both
  Jenkins controllers and a Temporal poller; a standalone repository `payments-api` built
  through all nine stages on the lab's existing Jenkins; `payments-api` appeared in the
  registry for the first time; `cosign verify` passed; the worker in the cluster deployed
  it over SSH and the container reports that digest.
- Removing `/metrics` from the Ingress was not enough: the portal's `/api/` proxy still
  served it as `/api/metrics`, found only by requesting it through the live Ingress. The
  portal now answers 404 there, and the contract test reads `nginx.conf` for that block.
- **Lab migration.** The lab's modules build from `netci.git` and rely on the sample
  defaults. They keep working on the library's `main` branch, which is unchanged. Moving
  `main` to this library requires giving each lab module its `NETCI_APP_DIR`
  (`sample-apps/<name>`); without it the build fails loudly rather than building the
  wrong application.
