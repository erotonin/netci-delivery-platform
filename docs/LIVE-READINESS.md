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
| Temporal 1.8.2 | CD orchestration, task queue `netci-delivery`; one worker process on the host | 127.0.0.1:7233 |
| NetBox 4.1 | DCIM: tenants = systems, device roles = modules, devices = servers, sites = environments; 13 lab devices incl. `netci-retired-01` (decommissioning) | http://127.0.0.1:8080 |
| Ansible + Docker | deploy target = this host, `deploy/ansible/inventories/localhost.ini`, six logical hosts named as in NetBox | local |
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
| Backend + contract (PostgreSQL-backed) | `NETCI_TEST_DATABASE_URL=…/netci .venv/bin/python -m pytest backend/tests tests/contract -o addopts="" -q` (run in two halves; see CLAUDE.md) | 556 passed, 4 skipped (opt-in Temporal tests, run separately below) + 41 passed |
| Temporal workflow tests against the time-skipping test server | `NETCI_RUN_TEMPORAL_TEST=1 .venv/bin/python -m pytest backend/tests/test_temporal_workflow.py` | 4 passed |
| Frontend | `cd frontend && npm test && npm run build` | 26 passed, build OK |
| Static | `pyflakes backend/app/`, `scripts/migrate.py --check-schema` | clean; schema matches 20 migrations |

---

## 6. What is NOT established

Say these plainly rather than let the table above imply them.

| Gap | Owner / next step |
|---|---|
| The registry is plaintext HTTP; `imagePullHost` is how the host reaches it. A real deployment needs TLS and one name. | Infra. |
| Rekor / transparency log is off (`--tlog-upload=false`, `--insecure-ignore-tlog`). Signatures are key-based only. | Security: decide on a Rekor instance; set `NETCI_SIGNATURE_REQUIRE_TLOG=true`. |
| The edge-agent *socket* registry (`_ACTIVE_RUNNERS`) is process-local by nature; command dispatch to an agent connected to another replica is not supported. Maintenance mode and telemetry are now in PostgreSQL (ADR-029). | Platform: route agent commands through the database (a command table the owning replica polls) if multi-replica dispatch is needed. |
| Password-grant OIDC in the harness is a lab convenience; the browser login flow was not exercised by automation today. | Platform: Playwright login test against Keycloak. |
| Single host for every environment. Nothing was proven about network reachability, SSH, or privilege escalation to a separate target. | Infra: a second VM in `local.ini`. |
| `production_readiness_audit.py` is a code self-check; its verdict is now `SELF_CHECK_PASSED` / `SELF_CHECK_FAILED`, never "certified". | Done. |
| The per-build pod's remaining cost is the fresh-workspace checkout (≈ 17 s for a 4 MB, 446-file repository on kind's emptyDir) plus provisioning; the project cache does not address it. A warm-workspace strategy that keeps isolation (e.g. a per-project workspace PVC with `git clean -fdx` on entry) would, and would need measuring. | Platform. |
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
