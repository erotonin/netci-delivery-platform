# netCI — Live readiness record

This file records what has actually been established against real infrastructure, for
one commit, with the commands that established it and where the evidence lives. It
replaces an earlier version of this document that reported a `CERTIFIED` verdict from
`scripts/production_readiness_audit.py`. That script checks the code's own invariants
(state tables, token scopes, migration counts) in-process; it never contacts Jenkins,
Temporal, an IdP, a DCIM or a registry, and its verdict must not be read as one. Its
output stays useful as a self-check and is kept; the word *certified* is not.

**Rule (CLAUDE.md #10):** nothing here may be described as live-verified without a run
against real external infrastructure, and every claim below names that run.

---

## 1. Commit and environment under test

| | |
|---|---|
| Code under test | `592855faeddd309e15e8723dc14aef949f6b3da0` plus the working tree committed immediately after this document (see `git log -1` on the commit that adds this file; the harness evidence records `commitSha`). |
| Date | 2026-09-15 (UTC) |
| Where | One Linux host (this repository's development machine). Everything below runs on it; no cloud resources. |
| Profile | `NETCI_ENVIRONMENT=production` (fail-closed mode), `.netci-gate/real-local.env` (local, not committed: contains lab secrets) |

### Real components the stack talked to

| Component | What | Address (lab) |
|---|---|---|
| PostgreSQL 16 | canonical store, database `netci_live` (the test suite uses `netci`, which its fixtures truncate) | 127.0.0.1:55432 |
| Keycloak 26 | OIDC issuer, realm `netci`; groups `netci-admins`, `release-managers`, `engineers` mapped to roles `platform-admin`, `reviewer`, `developer`; users `pat`, `rae`, `dana`, `sam` | http://127.0.0.1:8180/realms/netci |
| Jenkins ×2 | controllers `jenkins-a`, `jenkins-b`, building on ephemeral pods in a `kind` cluster with the pinned toolbox image (syft 1.51.0, trivy 0.73.0, cosign 3.1.2) | 172.17.0.50:8080, 172.17.0.51:8080 |
| Git server | the module's repository, cloned by Jenkins | http://172.17.0.52/netci.git |
| OCI registry | `registry:2`; reachable as `172.17.0.1:55000` from the build cluster and `localhost:55000` from the host | :55000 |
| Temporal 1.8.2 | CD orchestration, task queue `netci-delivery`; one worker process on the host (two since §4g) | 127.0.0.1:7233 |
| NetBox 4.1 | DCIM: tenants = systems, device roles = modules, devices = servers, sites = environments; 13 lab devices incl. `netci-retired-01` (decommissioning) | http://127.0.0.1:8080 |
| Ansible + Docker | deploy target = this host, `deploy/ansible/inventories/localhost.ini`, six logical hosts named as in NetBox | local |
| Since 2026-09-16 | ingress-nginx v1.12.1 on the kind worker (canary), a separate SSH production host (`netci-prod-01`, 172.17.0.60), two API replicas (:8100 `api-a`, :8101 `api-b`), Prometheus 3.5 + Alertmanager 0.28, user-systemd timers (backup, DR drill, Jenkins token rotation); `scripts/bootstrap.sh --check` lists all of it | see §4f–§4j |
| cosign 3.1.2 | on the worker host (`NETCI_COSIGN_EXECUTABLE`), same binary as the toolbox; readiness reports the version | — |

No adapter in the stack above was a fake. `NETCI_DEMO_DATA` is unset. The only
in-repo sample is the application being deployed (`sample-apps/hello-container`).

---

## 2. Acceptance harness — nine gates, all exercised

Command (run from the repository root with the live profile loaded):

```bash
set -a; source .netci-gate/real-local.env; set +a
NETCI_ACCEPTANCE_API_URL=http://127.0.0.1:8100 \
NETCI_ACCEPTANCE_MODULE=hello-container \
NETCI_ACCEPTANCE_COMMIT=31fee125730cb69e55d3da4238725a098d67391f \
NETCI_ACCEPTANCE_OIDC_TOKEN_URL=http://127.0.0.1:8180/realms/netci/protocol/openid-connect/token \
NETCI_ACCEPTANCE_OIDC_CLIENT_ID=netci NETCI_ACCEPTANCE_OIDC_CLIENT_SECRET=… \
NETCI_ACCEPTANCE_USER_ADMIN=pat:… NETCI_ACCEPTANCE_USER_REVIEWER=rae:… NETCI_ACCEPTANCE_USER_DEVELOPER=dana:… \
NETCI_ACCEPTANCE_RETIRED_SERVER=netci-retired-01 \
NETCI_ACCEPTANCE_WORKER_RESTART=<script that stops/starts the worker> \
PYTHONPATH=backend .venv/bin/python scripts/production_acceptance_harness.py
```

Each gate performs the operation it is named after through the public API and passes
only when the real thing happened. Evidence: `evidence/production_acceptance_<stamp>.json`
and `evidence/acceptance.xml` (the JSON carries `commitSha`, per-gate durations and the
identifiers below).

| # | Gate | What actually happened | Result |
|---|---|---|---|
| 1 | persistence_across_api_restart | A row inserted directly into PostgreSQL was served by the running API process and returned 404 after deletion. | PASS |
| 2 | oidc_role_and_team | Three Keycloak identities obtained via the realm; `/me` reported method `oidc`, roles `platform-admin` / `reviewer` / `developer`, non-empty teams. A garbage token → 401. The developer starting a `prod` run → 403 `ENVIRONMENT_FORBIDDEN`. | PASS |
| 3 | dcim_inventory_lookup | `/readyz` reports DCIM `netbox` ready; `/dcim/servers` for the module lists the NetBox devices; `netci-retired-01` is reported `decommissioning`. | PASS |
| 4 | jenkins_checkout_build_push | A real run: Jenkins checked out `31fee125…`, built on an ephemeral pod, pushed to the registry; netCI recorded `jenkinsRunId`, the digest and the security evidence. | PASS |
| 5 | sbom_trivy_cosign_verification | Evidence carries a syft SBOM, a passed trivy scan and a cosign signature; the harness then ran `cosign verify` itself (v3.1.2) against the digest — success — and against an unsigned digest — refused. | PASS |
| 6 | temporal_workflow_restart_resume | Worker stopped; `/readyz` reported `cd: no_workers` (a stale poller aged past 75 s is not counted); a build completed and its deployment sat in `deploying`; worker restarted; the deployment finished `healthy`. | PASS |
| 7 | ansible_host_and_target_namespace | `parameters.target_hosts` / `target_namespace` on a run → 422 naming the key (`DEPLOYMENT_PARAMETER_NOT_ACCEPTED`), never silently dropped. A revision pointing dev at `netci-retired-01` was activated and dispatch was refused 422 `DCIM_TARGET_UNAVAILABLE`; the original target was restored. | PASS (the first run of the day FAILED here on the harness's own expectation of the error code — the server's refusal was right — and was corrected; `evidence/production_acceptance_20260915T082156Z.json` keeps that run) |
| 8 | deployment_failure_and_rollback | `POST /deployments/{id}/rollback` to the previous digest; `RollbackWorkflow` ran on the worker (verify → playbook → health) and reported; the platform recorded `rolled_back` with the older digest, and the host served it. | PASS |
| 9 | backup_and_restore_verification | `scripts/netci_backup.py drill`: dump, restore into a scratch database, 34 tables / row counts / checksums / migrations compared, injected failure detected. | PASS |
| 10 | multi_controller_failover_mttr | A controller is stopped (`docker stop`); readiness must report it; a build must be routed to the survivor and complete; the controller is restarted and must rejoin. Detection time and MTTR are recorded. | PASS (11.1 s / 88.4 s) |

Runs on 2026-09-15, each 9 PASS / 0 FAIL / 0 BLOCKED, ≈ 6 minutes each (three real
Jenkins builds, one worker restart drill, one rollback, one backup drill):

- `evidence/production_acceptance_20260915T082746Z.json` — code at `592855f` + the
  working tree that became `9c69aa6`.
- `evidence/production_acceptance_20260915T094415Z.json` — code at `d16a6c7` (all three
  runtimes, durable server state, redeploy fix, periodic reconciliation), `commitSha`
  stamped from `NETCI_BUILD_COMMIT`.
- `evidence/production_acceptance_20260915T154813Z.json` — code at `2f18952`, **ten**
  gates (the failover/MTTR gate added), 10 PASS; builds ran in the project's isolated
  namespace with the `lint` custom stage.

- `evidence/production_acceptance_20260916T094139Z.json` — code at `3ac3aa2` (stamped `-dirty`: the only uncommitted change was this document), lab commit
  `139eeb67…` (the working tree published to the lab git server): **10 PASS**, failover
  detected in 11.3 s, MTTR 83.2 s. The previous run of the day stamped a two-day-old
  `commitSha` from a pin in the lab profile; the pin is gone, the harness now stamps the
  repository HEAD (with `-dirty` when the tree is not clean), and that run's file was
  discarded rather than kept with a wrong commit.

The evidence file is the authority for the verdict; this table describes what each gate
does.

---

## 3. Manual end-to-end proofs on the same day (not part of the harness)

All through the API with Keycloak identities; identifiers are in the `netci_live`
database and its audit log (`GET /audit-events?moduleId=hello-container`).

| Proof | Result |
|---|---|
| Onboard system + module via `/systems`, `/systems/{id}/modules` with NetBox-backed targets | done |
| dev release: trigger → Jenkins A → evidence `allow` → Temporal → cosign re-verify on worker → Ansible docker deploy (read-only rootfs, cap-drop ALL, host networking) → health probe answered with the deployed digest → `healthy` | deployment `9cd28eef…`, digest `sha256:da0648d0…` |
| Same on Jenkins B (the router spreads builds across both controllers) | run `464cd88c…` dispatched to `jenkins-b` |
| staging release on the same host under a distinct container name | deployment `feeafe92…` healthy |
| prod release: stops at `pending_approval`; the requester's own approval → 403 `SEPARATION_OF_DUTIES`; a developer → 403; reviewer `rae` approves → `deploying` → `healthy` | deployment `e8ec6d6d…`, fencing token 1 |
| second prod release, approved, healthy; then rollback by the reviewer to the previous digest: developer → 403; unknown digest → 409 `ROLLBACK_ARTIFACT_UNKNOWN`; real target → `rollback_in_progress` → worker ran it → `rolled_back` in 17 s; host serves the previous digest | deployment `32dd4eb3…`, fencing token 3 |
| Configuration revision touching production → `pending_approval`; author's approval → 403 `SEPARATION_OF_DUTIES`; developer → 403 `FORBIDDEN`; reviewer → active | revision 5 |
| Temporal durability: a workflow started with no worker resumed 12 minutes later when a worker appeared | earlier in the day, run `7c6f7c89…` |
| **Kubernetes runtime**: `hello-kubernetes` built on Jenkins B, Helm release in namespace `dev` on the kind cluster, pod running the digest-pinned image `172.17.0.1:55000/hello-kubernetes@sha256:dd398520…` | deployment `0bb63e63…` healthy |
| **systemd runtime**: `hello-systemd-go` Go binary built on Jenkins A, pushed to the registry as a one-layer OCI artifact, signed, re-verified on the worker, fetched and installed as a user-scope systemd unit answering on :18191 | deployment `51440ee8…` healthy (ADR-029) |
| Maintenance mode set through the API, API restarted, dispatch to that host refused 422 `DCIM_TARGET_UNAVAILABLE (maintenance)` — the state is in PostgreSQL, not the process | observed 09:11–09:12 UTC |
| `POST /modules/hello-container/config/apply` (redeploy the in-service digest under the active revision, no rebuild): first attempt refused `NO_DEPLOYABLE_ARTIFACT` after a rollback, second hit a foreign-key error on PostgreSQL — both fixed (ADR-029 §5) — third: `deploying` → worker re-verified → `healthy` in ~20 s | deployment `b41b093f…`, fencing token 11 |
| Readiness truthfulness: `/readyz` → 503 with `cd.status = no_workers` while the worker was down; `cosign.version = v3.1.2`; 2/2 Jenkins controllers; NetBox `ready` | observed repeatedly |

---

## 4. Defects found only by running for real

Nine, all fixed the same day, all with regression tests. They are recorded in
`docs/decisions/ADR-028-what-the-first-real-deployment-taught.md`. The short list:

1. cosign 3 signatures unreadable by the cosign 2 the worker had → every artifact refused.
2. Failure reason recorded as `ActivityError` and nothing else.
3. The per-deployment callback token was written into `ansible-playbook --extra-vars`.
4. No server-owned place for the playbook's install root / port / network mode.
5. Ansible inventory names ≠ DCIM device names → `--limit` matched nothing.
6. A no-op resubmission of the production target demanded an approver.
7. A configuration revision's targets were not schema-validated.
8. The runtime runner reported stderr *or* stdout, hiding the real Ansible failure.
9. The registry has a different name inside the build cluster and on the host.

And one more, found by the rollback proof: `POST /deployments/{id}/rollback` wrote
`rollback_in_progress` and started nothing — no worker ever executed a manual rollback.
`RollbackWorkflow` now runs it (ADR-028, decision section).

Running the other two runtimes found seven more (ADR-029): the systemd path had no
artifact store at all; Kubernetes targets were passed as Ansible `--limit` hosts and the
worker used the wrong Ansible collections; maintenance mode and telemetry were
process-local dicts (forgotten on restart, invisible to a second replica, and the
heartbeat's objects would have crashed the gate); the telemetry endpoint invented
"normal" numbers; an unconfigured DCIM called every target healthy; redeploy-with-configuration
acquired its lease before the deployment row existed and picked the wrong source run after a
rollback; nothing ever invoked the reconciler, so a run whose Jenkins build failed at checkout
stayed `queued` (now reconciled on a schedule).

---

## 4b. Second day of live work (2026-09-15, later): isolation, catalog, failover

| Proof | Result |
|---|---|
| Per-project build isolation (ADR-030): a build of `hello-container` created namespace `netci-build-hello-container-a29c7b` (SA, Role/RoleBinding for the controller identity, ingress-deny NetworkPolicy, `netci-cache` PVC) on first launch; the pod ran there, on both controllers, with the cache bound | Jenkins B #8 (cold, `NETCI_CACHE=miss`, git mirror created), Jenkins A #12 (warm, `NETCI_CACHE=hit`) |
| `/readyz` reports `ci.buildIsolation` (mode, controller identity, cache size) and would be not-ready if netCI could not create namespaces | observed |
| Benchmark, three modes × 5 runs on the lab (`evidence/benchmarks/report.json`, `samples.csv`): reusable-pod baseline 48.2 s, plain ephemeral pod 50.0 s, isolated pod with project cache 51.9 s (warm 52.2 s). Isolation costs ≈ +3.6 s pod/PVC provisioning and ≈ +2 s layered image storage per build (+8 % on this 50 s workload); the cache hits on every warm run (`cacheHitRate 1.0`) but this sample application has nothing expensive to cache, so the measurement establishes **no regression beyond the provisioning cost**, not a gain. A workload with dependencies to download would show the cache's benefit; this one does not, and the number is reported as measured. | `conclusion: acceptable` |
| Lab git server switched from dumb to smart HTTP because a dumb fetch of an up-to-date repository cost ~6 s per fetch and would have been read as agent cost (`scripts/lab/git_smart_http.py`) | clone 0.57 s, up-to-date fetch 0.04 s |
| Stage catalog from the database: developer refused to register (403), a shell string refused (`INVALID_STAGE_SCRIPT`), `lint` registered by the admin after `unit-test`, a module dropping `sbom` refused (`REQUIRED_STAGE_REMOVED`), a valid selection stored | live API calls, 2026-09-15 |
| Controller drift endpoint `GET /api/v1/ci/controllers/drift`: after both controllers were recreated from the repository's JCasC → `drift: false`; before normalisation was tuned it reported the expected per-controller differences (system message, transient pod label atoms) | observed |
| **Harness, 10 gates** at commit `2f18952` (`evidence/production_acceptance_20260915T154813Z.json`): 10 PASS. Gate 10 `multi_controller_failover_mttr`: `jenkins-a` stopped, `/readyz` reported it down after **11.1 s**, the next build was routed to `jenkins-b` and published a digest at **MTTR 88.4 s**, `jenkins-a` rejoined at 103.6 s | PASS |
| Custom stage `lint` (registered in the catalog, selected by the module in the portal) ran on a real build after `unit-test`, inside the project namespace; the deployment that followed was `healthy` | Jenkins A `netci-8c7dc688…#1`, 2026-09-15 15:33 UTC |

## 4d. 2026-09-16: every runtime through every environment, and a truthful baseline

| Proof | Result |
|---|---|
| `hello-kubernetes` staging (Helm, namespace `staging`) → prod: `pending_approval`, requester's approval → 403 `SEPARATION_OF_DUTIES`, reviewer approves → `healthy`; second release; rollback to the previous digest → `rolled_back`, cluster serving `c37d3617…` again | deployments `bf65f440…`, `df1ce5a8…` |
| `hello-systemd-go` dev + staging + prod as **three separate units** (`hello-systemd-go-{dev,staging,prod}`, ports 18191–18193); prod approval gated the same way; the Go build is reproducible (same commit → same digest, so the second release had nothing to roll back to — recorded as such), a third release from a new commit → rollback → the unit serves the previous commit's binary, `current` → `rel-2704a1c2…` | deployments `5b57c1f4…`, `47d18372…`, and the third |
| Defect found: the systemd playbook named the unit after the application only, so the staging release **restarted dev's service on staging's port** and dev stopped answering — the same class of fault the docker container name had. Fixed: the unit carries the environment. | ADR-030 consequences |
| Benchmark with a *real* long-lived baseline (the reusable pod now keeps `label netci-shared` + `idleMinutes 120`, so consecutive builds land on the same pod: warm checkout 1.5 s): baseline 20.1 s (warm ≈ 13 s), ephemeral 40.0 s, isolated+cache 47.2 s (warm 45.0 s); the OCI tarball is no longer archived, which cut post-build time from ≈ 12 s to ≈ 1.3 s in every mode. **Honest reading:** a fresh pod costs ≈ +26 s on this 13-second workload (fresh-workspace checkout ≈ 17 s, provisioning ≈ 4 s); the project cache hits on every warm run but recovers none of it, because what a warm agent really has is a warm *workspace*, and a per-build pod by design does not. Isolation is a security decision paid for in wall time; the number is reported as measured. | `evidence/benchmarks/report.json` (5 × 3, threshold 400 %, `acceptable`) |

## 4e. 2026-09-16 (later): a separate production host, catalog governance, fail-closed canary

| Proof | Result |
|---|---|
| `scripts/lab/prod_host.sh`: a systemd container on the lab network as a stand-in for a production VM (offline like the build farm: the image is built with `--network=host`); the worker reaches it over SSH with a generated key and a **pinned host key** (`StrictHostKeyChecking=yes`), escalates with `become`, installs a **system-scope** unit under `/opt/hello-systemd` | `netci-prod-01` in NetBox (tenant `hello-systemd-go`, site `prod`) and in the inventory |
| `hello-systemd-go` prod re-targeted to that host through a config revision (prod change → `pending_approval` → reviewer approved); release → `healthy`; second release; rollback → host serves the previous binary | deployments `5694f8bf…`, and the second |
| Defect found: the worker probed the health URL (`127.0.0.1:<port>`) on **itself**, so a release that was healthy on the remote host was reported failed and **rolled back**. Fixed: the probe is aimed at the inventory's `ansible_host` | first attempt `f5f5f985…` (failed, wrongly), regression test |
| Custom stages now need a second administrator (`proposed` → `approve` by someone else → `active`); a module cannot select a proposed stage; parameters declared per stage, valued per module, validated, passed as environment | unit + API tests; live registration path unchanged for the admin UI |
| `POST /deployments/{id}/traffic` and canary advance/abort answer **501 `TRAFFIC_ROUTER_NOT_CONFIGURED`** unless a real router is configured; the in-memory router is refused outside local mode | `traffic.build_traffic_router` tests |

## 4f. 2026-09-16 (afternoon): a real canary, and the benchmark re-measured on a cache-sensitive workload

| Proof | Result |
|---|---|
| ingress-nginx v1.12.1 installed on the kind worker from the **lab registry** (`scripts/lab/ingress_nginx.sh`, digests re-pinned to the mirror) | `/readyz` → `traffic: {mode: nginx-ingress, status: ready}` |
| Stable `hello-kubernetes` prod re-released with the canary-capable chart (per-environment host, zero-trust policy admitting the controller); the **first attempt failed** on Helm's schema (`/canary/weight: got string, want integer` — Ansible templated each value to a string); fixed by rendering the values as one dict expression | deployment `759085dd…` healthy; stable answers `e8bedb33…` through nginx |
| Canary production request (developer `dana`, reviewer `rae`, steps 20/50/100): canary release `hello-kubernetes-canary` installed beside stable with the canary ingress at 20 %; **200 requests through the ingress at each step: 20.5 % / 52.0 % / 100 % answered with the canary digest**; the last step created a `promote` deployment (`0077f842…`, healthy) — stable now runs `e5fc8787…`, the canary release and ingress are gone, request `succeeded` | `evidence/canary-nginx.json` (`passed`) |
| Abort path: a second canary at 30 % (observed 32.5 %), `POST …/canary/abort` → weight 0 within 2 s (200/200 answers from stable), the canary deployment `rolled_back` (its release removed), stable image unchanged | `evidence/canary-nginx-abort.json` (`passed`) |
| Canary for a docker/systemd module is refused at approval (`canary delivery needs the kubernetes runtime`); the docker and systemd playbooks assert `release_track == stable` | API test + playbook pre_tasks |
| Benchmark, **Go template** (cold `GOCACHE` ≈ 58 s, warm ≈ 2 s; 5 × 3, checkout by `git archive` from the mirror): baseline 24.0 s, ephemeral 87.9 s, **isolated + cache 24.3 s (+1.4 % total, −4.8 % warm; build 1.8 s vs 13.2 s)**. On a workload whose cost is the compiler cache, the per-project cache recovers all of it. | `evidence/benchmarks/report-go.json` |
| Benchmark, **container template** (same run): baseline 15.4 s, ephemeral 42.5 s, isolated 33.0 s (+114 %). The 5-second buildah build gains little from a cache; provisioning (3.7 s), the mirror checkout (8.2 vs 4.0 s) and cleanup (2.5 s) are the cost of a fresh pod and are reported as such. | `evidence/benchmarks/report.json` |

## 4g. 2026-09-16 (afternoon): two API replicas, two workers

| Proof | Result |
|---|---|
| API `api-a` (:8100) and `api-b` (:8101) on the same `netci_live`; an edge agent connected to `api-a` (token minted for `netci-prod-01`); `GET /api/v1/agents/status` on **`api-b`** lists it with `replicaId: api-a`, `stale: false`, real telemetry, and no invented `ip`/`os`/`version` | migration 0023, `agent_fleet.py` |
| `POST /api/v1/agents/execute` on **`api-b`** for that agent → `uptime` output in **270 ms**, `requestedOn: api-b`, `claimedBy: api-a` (the command is a row the holding replica claimed) | command `842ff40e…` |
| Reconciler and outbox passes take a PostgreSQL advisory lock per pass; the second replica skips a pass the first is inside | `test_agent_fleet.py` (PostgreSQL) |
| Two Temporal workers; `worker_failover_drill.py` **SIGKILLed the worker running the playbook** of a dev deployment; Temporal (heartbeat timeout 45 s, activities now heartbeat every 10 s) started attempt 2 on the survivor 46 s later; deployment `healthy` **48.2 s after the kill** | `evidence/worker-failover.json`, workflow `netci-deploy-656915d7…` |

## 4h. 2026-09-16 (afternoon): scheduled backups, alerting

| Proof | Result |
|---|---|
| `infra/systemd/netci-backup.{service,timer}` (nightly 02:00, create → verify → prune) and `netci-dr-drill.{service,timer}` (weekly restore drill); installed in the lab's user systemd by `scripts/lab/backup_timer.sh`; **both services run once through systemd**: `Result=success`, backup of `netci_live` encrypted (AES-256-GCM), restored into a scratch database, 34 tables / row counts / checksums / FKs / 23 migrations compared | `evidence/dr_drill_20260916T074042Z.json` (`PASSED`), `.netci-gate/backups/` (local) |
| `/metrics` publishes the readiness verdict (`netci_ready`, `netci_dependency_ready{dependency}`), `netci_cd_pollers`, `netci_ci_controllers_healthy`, `netci_agents{state}`, `netci_reconciler_corrections_total`, `netci_replica_info{replica}` | test in `test_observability_and_notifications.py` |
| Prometheus v3.5.0 + Alertmanager v0.28.1 (`infra/monitoring/`, `scripts/lab/monitoring.sh`) scraping both replicas; rules for replica down, not ready, dependency lost, no/one worker, controller lost, stale agent, reconciler correcting, outbox backlog, 5xx rate. **Drill: `docker stop jenkins-b`** → `NetciJenkinsControllerLost` active 2 m 47 s later (scrape + `for: 2m`), delivered to the webhook receiver, **resolved** 60 s after `docker start` | `evidence/alerting-drill.json` (`passed`; the first delivery attempt failed on a port collision in the lab receiver, noted there) |

## 4i. 2026-09-16 (evening): secrets as files, token rotation with revocation, reload from git, bootstrap inventory

| Proof | Result |
|---|---|
| Both controllers recreated with `/run/secrets` mounted (token, pipeline key, cosign key as files; no secret in the container environment); JCasC resolved them | `docker exec jenkins-a ls /run/secrets`; `/readyz` `healthyControllers: 2` |
| `POST /api/v1/ci/controllers/reload` with the HMAC push-webhook signature → both controllers reloaded in 4.4 s, drift `false`, audit row `ci.controllers.reloaded` by `webhook:casc`; a wrong signature → 401 | `evidence/casc-reload-and-rotation.json` |
| `jenkins_lab.sh rotate-token`: new 24 h token bound to a new anchor Secret, file rewritten atomically, reload through netCI, previous anchor deleted → **previous token refused 10 s later**, new token accepted; a dev build then **succeeded** on the rotated credential (run `c78ecc13…`, deployment healthy); the same rotation ran once through `netci-jenkins-token-rotate.service` (timer twice daily) | same file; first two checks were wrong (client cert in the kubeconfig authenticated instead of the token) and are recorded as such |
| `netci_ci_controllers_drift` metric + `NetciControllerDrift` rule loaded in Prometheus | `curl :8100/metrics`, `:9090/api/v1/rules` |
| `scripts/bootstrap.sh --check` inventories 30 components and reports **everything present** on this host; `--up` is composed of the same ensure-steps. **Not run on a fresh machine.** | ADR-033 consequences |

## 4j. 2026-09-16 (evening): a person signs in through Keycloak in a real browser

| Proof | Result |
|---|---|
| `GET /auth/config` names the public PKCE client and the provider's discovered endpoints; the Portal offers **Đăng nhập bằng SSO**, sends Chromium to Keycloak's login form, the password is typed **there**, the code is exchanged with the PKCE verifier, and `GET /me` accepts the id_token (`method: oidc`, roles from groups) | `frontend/e2e/oidc-login.spec.ts`: 2 passed against the live lab (`evidence/oidc-browser-login.json`) |
| A forged callback (`?code=stolen&state=not-ours`) → "state mismatch", no token-endpoint call | same |

## 4k. 2026-09-16 (night): review follow-ups — balancer, single sign-out, steering rules, scheduled retention

| Proof | Result |
|---|---|
| HAProxy 3.0 (`infra/lb/haproxy.cfg`, `scripts/lab/lb.sh`) on :8000 in front of `api-a`/`api-b`: round robin, `httpchk /healthz`, `retries 3` + `redispatch`, websocket tunnel. Portal, workers and Jenkins callbacks now go through it (`NETCI_CALLBACK_URL=…:8000`). Agent connected through the LB; `execute` via the LB answered by the other replica (268 ms). **`kill -9 api-a`** (held the agent): 37/40 requests in the next 10 s ok (before redispatch), agent reconnected via the LB 2 s later; **`kill -9 api-b`** with redispatch: **40/40 ok**. A build+deploy ran with every callback through the LB | `evidence/lb-failover.json` |
| Sign-out ends the provider session: Playwright control — a second tab signed in **without a password** while the session lived; after the Portal's logout (browser sent to `end_session_endpoint` with `id_token_hint`), the next SSO click shows Keycloak's password form again | `e2e/oidc-login.spec.ts` 3 passed |
| Header/cookie steering: `X-Canary: always` → **200/200** canary; `Cookie: canary=always` → 200/200; `X-Canary: other` → split by weight (45.5 % at 50 %); no header → 51.5 % | `evidence/canary-nginx-rules.json` |
| Retention on a schedule (daily, first pass 5 min after start, advisory lock): spent callback tokens, delivered notifications, delivery events, console lines of runs finished > 90 days ago. Audit events are deliberately never thinned. `POST /admin/retention/purge` runs the same pass (409 if another replica holds it); `netci_retention_purged_total{kind}` | tests |

## 4l. 2026-09-16 (night): the docker runtime on the separate host, agents on the host

| Proof | Result |
|---|---|
| `netci-prod-host` rebuilt with Docker 29 + compose; NetBox device `netci-prod-02` (tenant/role `hello-container`) for the same machine; revision approved by `rae`; **two releases failed** (inner Docker on the outer overlay — whiteout files; then the containerd snapshotter's store still on it) and were rolled back by the playbook's rescue; with overlay2 on a volume: **healthy**, `docker ps` on the host shows `hello-container-prod`, health answers the release's digest; second release healthy; **rollback** → host serves the first digest again | `evidence/docker-prod-host.json` |
| Edge agents now run **on the host** (`netci-agent@netci-prod-01/02`, systemd, DynamicUser + docker group) and report the host's own figures. The daemon no longer invents 25/15/35 % on a failed reading; the server records nothing for an unknown one. `docker ps` through `execute` via the LB: requested on `api-a`, claimed by `api-b`, exit 0 | same |
| The pre-flight gate then **refused** a prod release (`resource_exhausted`: disk 92.3 % > 90 %) — the lab disk is genuinely that full. Recreatable caches were cleared (89.8 %) and `hello-systemd-go` prod re-released healthy. | same; the 48 GB of reclaimable Docker volumes on this host are OpenStack data, not netCI's, and were not touched |

## 4m. 2026-09-16 (night): real blue/green

| Proof | Result |
|---|---|
| First blue/green after a rolling stable: `green` release installed with no Ingress; **0 %** of 200 requests reached it until the worker reported healthy; then the stable Ingress was patched to green: **200/200** new digest; switch-back refused (no `blue` release yet) | `evidence/bluegreen-nginx-1.json` |
| Second blue/green: `blue` installed, switched on healthy (200/200); `POST …/traffic/switch {blue→green}` → **200/200 old digest** in one call; forward again → 200/200 | `evidence/bluegreen-nginx-2.json`, ADR-035 |
| Rolling restart of both API replicas through the balancer while requests flowed: **353/353** ok | `scripts/lab/lb.sh` |

## 4c. Host reboot (2026-09-16 08:06 UTC)

The lab host rebooted overnight. Every container without a restart policy stopped
(both Jenkins controllers, Keycloak, NetBox, Temporal, PostgreSQL, the git server, the
lab registry); the kind cluster and the deployed `hello-container-*` containers came
back on their own. After `docker start` of the stopped containers and relaunching the
API and worker (`scripts/lab/api.sh`, `scripts/lab/worker.sh`), `/readyz` reported every
section ready with no data lost: `netci_live` still held every run, deployment,
revision and the stage catalog; the Jenkins controllers' cluster token (24 h) was still
valid; the per-project namespaces and cache claims were intact on the cluster. Nothing
had to be recreated from git this time -- state volumes survived -- but the controllers
*can* be, and were on 2026-09-15 (`scripts/jenkins_lab.sh recreate a|b`). What this does
not prove: recovery on a fresh machine (see §6).

## 5. Test suites at this commit

| Suite | Command | Result |
|---|---|---|
| Backend + contract (PostgreSQL-backed) | `NETCI_TEST_DATABASE_URL=…/netci .venv/bin/python -m pytest backend/tests tests/contract -o addopts="" -q` (run in two halves; see CLAUDE.md) | 634 passed, 4 skipped (opt-in Temporal tests, run separately below) + 41 passed |
| Temporal workflow tests against the time-skipping test server | `NETCI_RUN_TEMPORAL_TEST=1 .venv/bin/python -m pytest backend/tests/test_temporal_workflow.py` | 4 passed |
| Frontend | `cd frontend && npm test && npm run build` | 29 passed, build OK; Playwright OIDC spec 2 passed against the lab |
| Static | `pyflakes backend/app/`, `scripts/migrate.py --check-schema` | clean; schema matches 23 migrations |

---

## 6. What is NOT established

Say these plainly rather than let the table above imply them.

| Gap | Owner / next step |
|---|---|
| The registry is plaintext HTTP; `imagePullHost` is how the host reaches it. A real deployment needs TLS and one name. | Infra. |
| Rekor / transparency log is off (`--tlog-upload=false`, `--insecure-ignore-tlog`). Signatures are key-based only. | Security: decide on a Rekor instance; set `NETCI_SIGNATURE_REQUIRE_TLOG=true`. |
| `scripts/bootstrap.sh --up` has not been exercised on a clean host (the lab machine has ~10 GB free; the reclaimable space is OpenStack data, not netCI's, and the owner chose not to tear the lab down for it). `--check` reports every component present. | Platform: a throwaway VM run when one is available. |
| The Portal's static files are still served by one Vite/preview process; the API is balanced, the UI is not. | Infra: serve the build from the balancer or a CDN. |
| `production_readiness_audit.py` is a code self-check; its verdict is now `SELF_CHECK_PASSED` / `SELF_CHECK_FAILED`, never "certified". | Done. |
| The per-build pod's remaining cost, on a workload the compiler cache does not dominate, is provisioning + the mirror checkout + cleanup (≈ +17 s on the 5-second container build, §4f). The Go workload shows the cache recovers the build; nothing recovers the pod. | Platform: measured, accepted (ADR-030). |
| Canary and blue/green are Kubernetes + ingress-nginx only. Retiring an idle colour is a manual `helm uninstall`. | Platform. |
| A ReadWriteOnce cache claim serialises a project's concurrent builds on one node; multi-node needs RWX or a registry layer cache. | Infra. |

---

## 7. How to reproduce

1. Start the lab: PostgreSQL, Keycloak (`.netci-gate/keycloak/netci-realm.json`),
   NetBox + `scripts/netbox_seed_lab_inventory.py`, Temporal, registry, kind + Jenkins A/B
   (`scripts/jenkins_lab.sh`), git server. Copy the toolbox's cosign to the host and set
   `NETCI_COSIGN_EXECUTABLE`.
2. `python scripts/migrate.py` against `netci_live`; start the API with the production
   profile and the worker (`python -m app.workflows.worker`).
3. `curl /readyz` — every section must be `ready` before anything else is attempted.
4. Onboard the module with `runtimeSettings` for each environment (§1 of ADR-028 explains
   why these are reviewed configuration and not run parameters).
5. Run the harness (§2): `scripts/lab/harness.sh` wraps it for the lab, reading the
   identities and OIDC client from `.netci-gate/real-local.env`. `scripts/lab/benchmark.sh N`
   runs the three-mode benchmark. Read the evidence file, not this document, for the
   verdict.
