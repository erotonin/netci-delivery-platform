# netCI — toàn bộ context để vẽ sơ đồ

Tài liệu này dành cho một phiên Claude Code (cloud) được giao **vẽ sơ đồ kiến trúc netCI**.
Nó tóm tắt những gì code thực sự làm tại commit `695d08e` (tài liệu cập nhật sau đó) (nhánh `fix/prod-access-and-fail-closed-gaps`,
2026-09-29), kèm đường dẫn nguồn để kiểm chứng. **Khi tài liệu này và code khác nhau, code đúng.**

---

## 0. Quy tắc bắt buộc khi vẽ

netCI là một Internal Developer Platform mà mục đích là **nói thật về những gì nó đã và chưa kiểm chứng**.
Sơ đồ phải giữ đúng chuẩn đó:

1. **Không vẽ thứ không có trong code.** Mỗi hộp và mỗi mũi tên phải tương ứng một file, một service
   hoặc một bảng có thật. Nghi ngờ thì mở file ở cột "Nguồn".
2. **Phân biệt "đã chạy thật" và "chỉ có code".** Dùng hai kiểu nét: nét liền cho đã kiểm chứng live
   (mục 12), nét đứt hoặc nhãn *"chưa kiểm chứng live"* cho phần còn lại. Không ghi "production-ready"
   hay "live" cho thứ chưa có bằng chứng.
3. **CI và CD là hai hệ khác nhau.** Jenkins chỉ làm CI (build/test/scan/ký/publish).
   CD (deploy, health check, rollback) do **worker Temporal của netCI** làm. Không vẽ Jenkins deploy.
4. **Server quyết định danh tính.** Actor, owner, máy đích, namespace, credential và người duyệt do
   server quyết định, không lấy từ browser (ADR-015). Mũi tên từ browser không bao giờ mang những thứ này.
5. **PostgreSQL là nguồn sự thật.** Không có cache trong process quyết định trạng thái bền vững (ADR-014).
   API chạy nhiều replica, không replica nào giữ trạng thái riêng.
6. **Artifact định danh bằng digest** (`sha256:…`), không bằng tag.
7. Nhãn kỹ thuật dùng đúng tên trong code (tiếng Anh). Chú thích có thể viết tiếng Việt.

---

## 1. netCI là gì, trong một đoạn

Developer đăng ký **module** (một service có repository git) trong **system**. Khi có commit (webhook
GitLab/GitHub hoặc bấm tay), netCI tạo một **pipeline run**. Run được **admission** xếp hàng (quota,
supersede các run cũ cùng nhánh), rồi netCI gọi **Jenkins** chạy **shared library**. Jenkins
checkout → test → build image → SBOM → quét lỗ hổng → ký cosign → push lên registry, rồi **callback** về
netCI (token workload riêng cho từng run) kèm evidence: digest, SBOM, kết quả trivy, phiên bản tool.
netCI kiểm evidence bằng **policy gate**. Khi deploy, netCI khởi động **workflow Temporal**:
validate artifact (chữ ký, provenance) → chờ duyệt nếu là prod → deploy bằng adapter runtime
(Docker qua SSH, Kubernetes/Helm, systemd/Ansible) giữ **lease + fencing token** → health check →
verification theo metrics → rollback nếu hỏng → báo kết quả. Mọi chuyển trạng thái đều **atomic,
idempotent, có audit**.

---

## 2. Bản đồ thành phần (C4 mức container)

| Thành phần | Công nghệ | Vai trò | Nguồn |
|---|---|---|---|
| **Portal** | React 19 + Vite, TypeScript (UI tiếng Anh) | Giao diện người dùng; gọi API qua `/api`; SSO OIDC PKCE với Keycloak | `frontend/src/` |
| **netCI API** | Python FastAPI (uvicorn), ≥2 replica | Toàn bộ nghiệp vụ: modules, runs, deploy, policy, pipelines, catalog, audit | `backend/app/main.py`, `delivery.py`, `portal.py` |
| **Temporal worker** | Python, Temporal SDK, ≥2 replica | CD: workflow `ProvisionAndDeployWorkflow`, `RollbackWorkflow`, `PreviewWorkflow` | `backend/app/workflows/` |
| **PostgreSQL** | 16 | Dữ liệu netCI (45 bảng, 35 migration); Temporal cũng lưu ở PostgreSQL (ADR-036) | `backend/migrations/`, `backend/schema.sql` |
| **Temporal server** | temporalio 1.29 | Điều phối workflow CD | `docker-compose.yml` |
| **Jenkins controller** | Jenkins LTS + JCasC, **1 controller active** (ADR-055) | Chạy CI bằng shared library `netciPipeline`; agent là pod Kubernetes tạm thời | `jenkins/`, `infra/corp/jenkins/values.yaml` |
| **Shared library** | Groovy (`vars/netciPipeline.groovy`) + script bash nhúng | Các stage CI; phiên bản theo tag (`netci-0.4.2`) | `jenkins/shared-library/` |
| **CI toolbox image** | `netci/ci-toolbox:0.4.0` (buildah, syft, trivy, cosign) | Container builder trong pod agent | `jenkins/agent-toolbox/`, `toolchain/versions.yaml` |
| **Registry** | Harbor 2.15 (corp) / registry:3 (lab) | Nơi duy nhất lưu artifact (ADR-029), tag immutable | `infra/corp/harbor/` |
| **SCM** | GitLab CE (corp) / git server nhỏ (lab, ADR-037) | Repo của module và của shared library; webhook về netCI | `infra/corp/gitlab/` |
| **Identity provider** | Keycloak, realm `netci` | OIDC cho browser và API | `infra/keycloak/` |
| **DCIM** | NetBox (tuỳ chọn) | Danh mục server làm máy đích | `backend/app/adapters/netbox_dcim.py` |
| **Máy đích** | Docker host qua SSH / Kubernetes / systemd | Nơi ứng dụng được deploy | `backend/app/adapters/cd_orchestrator.py` |
| **Object storage** | SeaweedFS S3 (corp) | Backup Velero cho JENKINS_HOME | `infra/corp/seaweedfs/`, `infra/corp/velero/` |
| **Monitoring** | Prometheus / Alertmanager | Metrics cho verification sau deploy (ADR-046) | `backend/app/adapters/prometheus_metrics.py` |

Module backend đáng biết (mỗi file là một khối logic):
`admission.py` (xếp hàng và supersede run), `auth.py` (OIDC, vai trò), `workload_identity.py`
(token cho callback máy), `build_inputs.py` (allowlist tham số build), `policy/` (rules, quota,
break-glass, risk), `shared_pipelines.py` (parser script pipeline), `stage_catalog.py`, `toolchain.py`
(drift tool và plugin), `reconciler.py` (watchdog), `notifications.py` + outbox, `catalog/`,
`traffic.py` (canary, blue/green), `exposure.py` (CVE đang chạy), `retention.py`, `audit_ledger.py`.

Adapter (`backend/app/adapters/`): `jenkins_http.py`, `jenkins_router.py`, `ci_launcher.py`,
`build_isolation.py`, `cd_orchestrator.py`, `signature_verifier.py`, `trivy_rescan.py`, `oci_blob.py`,
`scm.py`, `scm_reporter.py`, `gitlab_repo.py`, `netbox_dcim.py`, `prometheus_metrics.py`,
`nginx_ingress_traffic.py`, `agent_daemon.py`.

---

## 3. Luồng chính: commit → CI → evidence → CD (vẽ thành sequence diagram)

```
Developer ─push─▶ GitLab ─webhook (HMAC/token)─▶ netCI API
netCI API: ghi scm_webhook_deliveries (chống trùng) → tạo pipeline_run QUEUED
           → admission: quota theo app/team + ngân sách hàng đợi Jenkins; run cũ cùng nhánh bị SUPERSEDED (ADR-050)
           → pin phiên bản shared pipeline vào run (tên, version, sha256) (ADR-058)
netCI API ─POST build (form params, token workload riêng cho run)─▶ Jenkins controller
Jenkins: kiểm plugin drift trước khi nhận (ADR-059, netCI đọc qua Overall/SystemRead)
Jenkins pod agent (namespace riêng mỗi project, ADR-030):
    Checkout → [block tác giả] → Unit Test → Build (buildah) → SBOM (syft) → Vulnerability Scan (trivy)
    → Sign (cosign, khoá chỉ có trong stage này) → Publish (push Harbor theo digest) → Publish Evidence
    mỗi stage ─callback stage event─▶ netCI API (pipeline_stages, pipeline_logs)
Jenkins ─callback evidence (digest, SBOM, findings, toolVersions)─▶ netCI API
netCI API: policy gate (chữ ký, SBOM, ngưỡng CVE, toolchain drift, Trivy DB ≤72h) → run SUCCEEDED/FAILED
           → security_evidence, artifact_sboms, artifact_findings; outbox → trạng thái commit trên SCM (ADR-048)
Deploy (tự động dev/staging, hoặc production request):
netCI API ─start workflow─▶ Temporal ─▶ worker
worker: validate_artifact (cosign verify + SLSA provenance, ADR-044)
        → [prod: chờ signal approve, người duyệt ≠ người yêu cầu; DB chọn người thắng, ADR-039]
        → [not_before / change freeze, ADR-047]
        → deploy (lấy deployment lease + fencing token, ADR-016/041)
        → health_check → verify_release (metrics Prometheus, ADR-046)
        → hỏng: rollback → ROLLED_BACK hoặc ROLLBACK_FAILED
        → report_deployment_result ─callback─▶ netCI API → deployments HEALTHY/FAILED
```

Chú ý khi vẽ:
- Build-only và promote là hai quyết định riêng (ADR-043): một run có thể chỉ build rồi promote sau.
- PR từ fork: chỉ verify, **không ký và không push** (ADR-054).
- Callback từ Jenkins dùng token ngắn hạn, gắn đúng một run (ADR-015), không dùng API key chung.

---

## 4. Máy trạng thái (vẽ state diagram — lấy đúng từ code)

Nguồn: `backend/app/domain/models.py` (`PIPELINE_TRANSITIONS`, `DEPLOYMENT_TRANSITIONS`).

**PipelineStatus**
```
QUEUED → RUNNING | FAILED | CANCELLED
RUNNING → WAITING_APPROVAL | SUCCEEDED | FAILED | CANCELLED
WAITING_APPROVAL → RUNNING | SUCCEEDED | FAILED | CANCELLED
FAILED → QUEUED (retry, có lineage) | ROLLED_BACK
SUCCEEDED → ROLLED_BACK
CANCELLED, ROLLED_BACK: trạng thái cuối
```
**Không có** `QUEUED → SUCCEEDED`: một run không được thành công mà không chạy. Đừng vẽ mũi tên đó.

**DeploymentStatus**
```
PENDING_APPROVAL → DEPLOYING | FAILED | CANCELLED
DEPLOYING → HEALTHY | FAILED | CANCELLED
HEALTHY → ROLLBACK_IN_PROGRESS
FAILED → DEPLOYING | ROLLBACK_IN_PROGRESS
ROLLBACK_IN_PROGRESS → ROLLED_BACK | ROLLBACK_FAILED
ROLLBACK_FAILED → ROLLBACK_IN_PROGRESS | DEPLOYING
ROLLED_BACK, CANCELLED: trạng thái cuối
```

**Phiên bản shared pipeline** (`shared_pipeline_versions.status`):
luồng hiện tại tạo thẳng `active`; khi bản mới active thì bản active cũ → `superseded`.
`proposed → active | rejected` vẫn có trong code (endpoint approve/reject) nhưng portal không tạo `proposed`.
Tối đa một bản `active` cho mỗi pipeline, do partial unique index trong DB đảm bảo.

Enum khác: `Runtime` = docker | kubernetes | systemd; `Environment` = dev | staging | prod.

---

## 5. Shared pipelines (ADR-058) — sơ đồ riêng

- Pipeline là **CI dùng chung**, thuộc platform, không thuộc module. Module chọn pipeline **theo tên**
  (`applications.shared_pipeline`).
- Một pipeline là **một script**, cắt thành stage bằng dòng đánh dấu:
  ```
  # @stage unit-test "Unit Tests" builtin
  netci-builtin unit-test
  # @stage lint "Lint Dockerfile"
  hadolint Dockerfile
  ```
- **Block builtin** (unit-test, build, sbom, vulnerability-scan, sign, publish) chạy code của thư viện
  với **đúng credential stage đó cần** (khoá cosign chỉ có trong Sign). **Block tác giả** là bash, chạy
  trong container builder **không có credential nào**. Đây là lý do script bị cắt ra chứ không chạy
  nguyên một file.
- Stage builtin bắt buộc: chỉ `build` và `publish` (`REQUIRED_BUILTINS` trong
  `backend/app/shared_pipelines.py`, theo phần Amendment của ADR-058). unit-test, sbom,
  vulnerability-scan và sign là tuỳ chọn. Tính deploy được do cổng policy lúc deploy quyết định:
  artifact thiếu chữ ký, provenance hoặc SBOM bị chặn, bất kể pipeline chứa gì.
- Thứ tự builtin cố định. `checkout` ngầm định luôn chạy đầu; `deploy` và `health-check` bị từ chối
  vì đó là CD.
- **Không có bước duyệt** (Amendment 2026-09-29 của ADR-058, chủ ý của chủ dự án). developer,
  reviewer hoặc platform-admin tạo pipeline hoặc lưu phiên bản mới thì phiên bản đó **active ngay**;
  bản active cũ chuyển thành `superseded`. Mọi thao tác đều có audit. API approve/reject và trạng
  thái `proposed` vẫn còn trong code nhưng luồng của portal không tạo ra chúng.
  Rủi ro được chấp nhận: sửa pipeline dùng chung ảnh hưởng mọi module dùng nó từ lần chạy kế tiếp.
  Rủi ro này được giới hạn bởi bốn cơ chế:
  - run ghim phiên bản;
  - block tác giả không có credential;
  - audit;
  - cổng deploy.
- Mỗi run **ghim** tên, version và sha256. Lúc launch, netCI băm lại script lưu trong DB; nếu không
  khớp thì run FAILED (`PIPELINE_VERSION_MISMATCH`) và có audit.
- Jenkins nhận `NETCI_STAGES` (checkout + id các block) và `NETCI_CUSTOM_STAGES` (block tác giả,
  code base64, neo sau builtin đứng trước nó). Thư viện ghi code ra `WORKSPACE_TMP`, shell giải mã,
  rồi chạy trong builder.
- Bảng: `shared_pipelines`, `shared_pipeline_versions`. API: `GET/POST /pipelines`,
  `GET /pipelines/building-blocks`, `GET /pipelines/{name}`, `POST /pipelines/{name}/versions`,
  `.../versions/{v}/approve|reject`, `PUT /modules/{id}/shared-pipeline`.
- UI (`frontend/src/PipelinesPage.tsx`): danh sách; designer (catalog stage bên trái, **một** editor
  script bên phải; khung **Jenkinsfile (Declarative) chỉ là bản xem trước sinh ở browser**
  (`generateDeclarativeJenkinsfile`), Jenkins thật vẫn chạy shared library `netciPipeline`;
  có ribbon luồng thực thi và bảng "SLSA & Quality Gate Compliance Audit");
  chi tiết (lịch sử phiên bản, diff, duyệt/từ chối).

---

## 6. Toolchain do netCI quyết định (ADR-056, ADR-059)

- `toolchain/versions.yaml` là nguồn sự thật cho:
  - tool build: syft 1.51.0, trivy 0.73.0, cosign 3.1.2, buildah (apt), mỗi tool có URL + sha256;
  - image toolbox;
  - tuổi tối đa của Trivy DB (72h);
  - **controller Jenkins**: base `jenkins/jenkins:2.555.3-lts-jdk21@sha256:…`, image
    `netci/jenkins-controller:2.555.3-netci1`;
  - **đủ 85 plugin** kể cả phụ thuộc.
- `scripts/toolchain_sync.py` sinh `jenkins/plugins.txt` và dòng base trong `Dockerfile.controller`;
  contract test giữ chúng khớp. Image controller không build được nếu plugin cài vào lệch khai báo.
- **Drift lúc chạy:**
  - build báo `toolVersions` trong evidence; lệch thì policy từ chối (`TOOLCHAIN_DRIFT`, `TRIVY_DB_STALE`);
  - trước mỗi build netCI đọc danh sách plugin của controller; lệch (version/missing/undeclared) hoặc
    không đọc được thì controller đó không nhận build;
  - `NETCI_TOOLCHAIN_ENFORCE=warn` chỉ ghi nhận, vẫn cho qua.
- Trang Toolchain hiển thị: tool khai báo vs tool quan sát theo controller, độ tươi Trivy DB, và plugin
  lệch theo từng controller.

---

## 7. Bảo mật và niềm tin (sơ đồ trust boundary)

Vẽ ranh giới tin cậy giữa: **Browser** | **netCI API** | **Jenkins/agent** | **máy đích** | **registry**.

| Ranh giới | Cơ chế | ADR |
|---|---|---|
| Browser → API | OIDC (Keycloak), actor lấy từ token; vai trò + team ownership; browser không gửi được actor/owner/target/credential (422) | 011, 012, 015, 034 |
| Jenkins → API (callback) | Token workload ký, ngắn hạn, gắn một run, scope giới hạn, chống dùng lại (`callback_token_uses`) | 015 |
| Tham số build | Allowlist (`build_inputs.py`); khoá do server quản lý bị từ chối | 015 |
| Credential trong CI | Khoá cosign chỉ bind trong Sign; registry chỉ ở stage cần; fork PR không ký/không push | 054 |
| Artifact → máy đích | cosign verify + SLSA provenance trước deploy; SBOM lưu và rescan (exposure) | 008, 044, 045 |
| Production | Duyệt hai người, DB chọn người thắng khi duyệt đồng thời; break-glass dual-control; change freeze | 024, 038, 039, 047 |
| Shared pipeline | **Không duyệt** (Amendment ADR-058); run ghim version + sha256, block tác giả không credential, audit; cổng deploy chặn artifact thiếu chữ ký/SBOM | 058 |
| Deploy đồng thời | Lease theo app+env, fencing token tăng dần | 016, 041 |
| Coding agent | Principal loại riêng | 052 |
| Jenkins config | JCasC từ git, không sửa tay; plugin khai báo và kiểm drift | 006, 033, 059 |

---

## 8. Mô hình dữ liệu (ER diagram) — 45 bảng

Nguồn: `backend/schema.sql` (sinh từ `backend/migrations/0001…0035`). Nhóm theo miền:

- **Danh mục:** `systems`, `modules`, `applications` (có `shared_pipeline` → `shared_pipelines.name`),
  `module_config_revisions`, `scm_integrations`
- **CI:** `pipeline_runs`, `pipeline_stages`, `pipeline_logs`, `pipeline_log_sequences`,
  `security_evidence`, `version_ci_reports`, `callback_token_uses`, `scm_webhook_deliveries`,
  `stage_catalog`, `shared_pipelines`, `shared_pipeline_versions`
- **Artifact và bảo mật:** `release_versions`, `artifact_sboms`, `artifact_findings`,
  `artifact_rescans`, `security_waivers`, `security_exceptions`
- **CD:** `deployments`, `deployment_leases`, `deployment_fencing_counters`, `production_requests`,
  `production_request_modules`, `change_freezes`, `preview_environments`
- **Governance:** `policy_decisions`, `break_glass_requests`, `resource_quotas`, `audit_events`,
  `idempotency_records`
- **Sự kiện và thông báo:** `delivery_events`, `notifications` (outbox)
- **Service catalog và self-service:** `catalog_services`, `catalog_service_dependencies`,
  `catalog_templates`, `resource_requests`
- **Hạ tầng và agent:** `server_health_records`, `server_maintenance_states`, `server_telemetry`,
  `agent_connections`, `agent_commands`

Quan hệ chính:
- system 1–n module; module 1–1 application;
- application 1–n pipeline_run; pipeline_run 1–n pipeline_stages và logs;
- pipeline_run → release_version (digest);
- release_version → deployment (theo env);
- production_request n–n module qua `production_request_modules`;
- shared_pipeline 1–n shared_pipeline_version; application n–1 shared_pipeline.

---

## 9. Ba cách triển khai netCI (deployment diagram)

**a) Lab "live" (systemd + Docker, một máy)** — database `netci_live`:
- API: user-systemd `netci-api-a` (:8100) và `netci-api-b` (:8101);
- worker: `netci-worker-1`, `netci-worker-2`;
- portal: vite :5173;
- phụ trợ: PostgreSQL :55432, Temporal :7233, Keycloak :8180, NetBox :8080, registry :55000,
  Prometheus :9090 / Alertmanager :9093;
- Jenkins A/B: 172.17.0.50 và .51; git server 172.17.0.52; máy prod `netci-prod-host` 172.17.0.60 (SSH).

**b) Kubernetes lab (`netci-local`, kind)** — Helm release `netci`, namespace `netci-system`,
database `netci_k8s`, task queue `netci-k8s-delivery`. Chart `deploy/helm/netci-platform`
(template: api, worker, portal, ingress, configmap, secret, prometheusrule).

**c) Lab mô phỏng công ty (`netci-corp`)** — xem `infra/corp/README.md`:
- **Cụm kind HA:** 3 control plane (etcd 3 thành viên) + 3 worker, sau một haproxy.
  Node được ghim IP tĩnh (`scripts/corp/pin_node_ips.sh`).
  - `worker`, `worker2`: pool `ci` (pod build);
  - `worker2`, `worker3`: đủ điều kiện chạy controller Jenkins; `worker3` thuộc pool `platform`.
- **Ingress:** MetalLB VIP `172.17.255.200` → 2 replica ingress-nginx trên 2 node → `https://netci.corp.local`
  (TLS bằng CA của lab).
- **netCI:** namespace `netci-system`, Pod Security `restricted`, database `netci_corp`, image `0.3.0-corp4`.
- **Jenkins:** một controller (StatefulSet 1 replica, JCasC, matrix-auth, tài khoản `netci-sa` không có
  Administer); JENKINS_HOME trên PV.
  - **Failover:** Velero + Kopia backup 15 phút/lần lên SeaweedFS S3; khi mất node thì restore sang node
    kia (ADR-055, RTO đo được 63–84 s).
- **Harbor 2.15:**
  - project `netci`, `mirror`, `apps`; tag immutable;
  - robot `netci` chỉ được push vào `apps`, robot `ops` dùng cho vận hành.
- **GitLab CE:** group `platform` chứa shared library (tag `netci-0.4.2`) và `payments-api`;
  webhook HTTPS về netCI.
- **Máy đích:** container `netci-corp-app-01` (SSH có pin host key).
- **Dịch vụ ngoài cụm** (container trên docker bridge `172.17.0.1`): GitLab :8929, Harbor :8930,
  SeaweedFS :8333 (qua gateway TLS nginx).

---

## 10. Portal — các trang (sơ đồ điều hướng)

Sidebar:
- **Core Delivery:** Dashboard, Systems & Pipelines, Pipelines, Production Requests, Service Catalog.
- **Platform & Governance:** Release Calendar, Servers, Toolchain.
- **Systems:** danh sách system, dẫn tới module.

Trang khác theo route: `system` (tổng quan), `module` (tab overview, pipeline, version, config, DORA),
`new-module` (wizard 3 bước: General → Pipeline CI chọn theo tên → Deployment CD), `scorecards`,
`release-plan` (DAG nhiều module, thuật toán Kahn, phát hiện vòng), `stage-catalog`,
`ci-cost` (FinOps), `architecture`.

Service Catalog còn các tab: **Services** (owner, tier, lifecycle, đồ thị phụ thuộc), **Previews**
(môi trường tạm theo MR, TTL) và **Resources** (yêu cầu tài nguyên có duyệt; provider chưa cấu hình thì
"fail-closed"). Tab Templates và trang Vulnerabilities đã bị bỏ.

Route dùng hash (`#/pipelines`). Nguồn: `frontend/src/App.tsx`, `PortalShell.tsx`.

---

## 11. Danh sách ADR (quyết định kiến trúc) — dùng làm chú thích

001 ranh giới hệ thống · 002 portal tự viết thay Backstage · 003 ranh giới Temporal · 004 Jenkins CI,
netCI CD · 005 adapter runtime · 006 JCasC là nguồn cấu hình · 007 agent tạm thời cô lập · 008 bảo mật
artifact · 009 định tuyến nhiều controller · 010 tách máy đích local/prod · 011 xác thực là seam, actor
lấy từ credential · 012 app thuộc team · 013 projection trung thực · 014 PostgreSQL là nguồn sự thật ·
015 workload identity cho callback, build input đóng · 016 lease, fencing token, log sequence · 017
release immutable · 018 readiness trung thực · 019 webhook SCM, checkout repo private, trạng thái commit ·
020 vòng đời pipeline, cancel, retry lineage, watchdog · 021 cấu hình môi trường có phiên bản, DCIM ·
022 observability, outbox, pool, DR · 023 release plan DAG, SAGA, progressive delivery · 024 policy,
waiver, break-glass hai người, quota, admission K8s · 025 service catalog, golden path, preview, self-service ·
026 chứng nhận production · 027 edge-agent, bỏ mọi đường thành công giả · 028 bài học deploy thật đầu
tiên · 029 registry là kho artifact duy nhất · 030 cô lập build theo project, cache ấm, pipeline từ
catalog · 031 canary sau ingress-nginx · 032 nhiều replica mọi thứ · 033 Jenkins dựng lại từ git, secret
là file · 034 browser đăng nhập tại IdP · 035 blue/green · 036 Temporal lưu PostgreSQL · 037 SCM là của
tổ chức · 038 không biến môi trường nào mở rộng quyền prod · 039 DB chọn người thắng khi duyệt prod ·
040 hai cách tự deploy netCI · 041 lease theo app+env · 042 build repo của chính module, một chart ·
043 CI và CD là hai quyết định · 044 SLSA provenance · 045 SBOM và rescan · 046 verification theo metrics ·
047 change freeze · 048 trạng thái SCM qua outbox · 049 preview được deploy thật · 050 admission và
supersede · 051 path filter · 052 coding agent là principal riêng · 053 chi phí CI · 054 build xác thực
git/registry, fork không push · 055 một controller Jenkins, standby được restore · 056 netCI quyết định
phiên bản tool · 057 designer theo module (đã bị 058 thay) · 058 shared pipeline (amended 2026-09-29: không duyệt, chỉ
bắt buộc build + publish) · 059 netCI quyết định
controller và plugin Jenkins.

Nguồn: `docs/decisions/ADR-0NN-*.md`. Mỗi ADR có mục Context / Decision / Consequences / Evidence.

---

## 12. Cái gì đã chạy thật, cái gì chưa (bắt buộc thể hiện trên sơ đồ)

**Đã kiểm chứng live:**
- *Qua bản cài Kubernetes (Jenkins A/B, Temporal, registry và Keycloak thật):*
  - cài vào DB trống; OIDC; onboarding và build `payments-api` đủ 9 stage; push registry; `cosign verify`;
  - deploy qua SSH;
  - build-only + promote; provenance; SBOM + rescan;
  - verification sau deploy và rollback;
  - đăng nhập SSO qua portal trên browser;
  - admission + supersede: 12 webhook trong 0,9 s → 2 build Jenkins, 10 bị supersede;
  - release plan simulate; trang Stage Catalog, CI Cost, Release Plan; `ciSeconds` từ build thật;
  - PR fork verify-only không push.
- *Trên lab corp:*
  - build `payments-api` từ GitLab bởi Jenkins, push Harbor, cosign verify, toolchain không drift;
  - deploy healthy lên `netci-corp-app-01`;
  - drill failover Jenkins PASS (RTO 84 s, và 63 s sau khi đổi khoá repository Kopia).

**Chưa kiểm chứng live** (vẽ nét đứt):
- shared pipeline với block tác giả chạy thật (ADR-058);
- cổng plugin drift trên controller 2.555.3 (ADR-059), vì cụm corp đang chờ khôi phục sau lần khởi
  động lại máy ngày 2026-09-29;
- webhook từ GitHub/GitLab.com thật; cancel-in-progress; path filter; agent principal;
- ngân sách hàng đợi Jenkins thực sự điều tiết; preview; trạng thái SCM; canary analysis; SLO alert;
- TLS end-to-end ngoài lab; runtime systemd; DCIM; traffic router.

---

## 13. Gợi ý bộ sơ đồ (theo thứ tự ưu tiên)

1. **Context (C4 L1):** Developer, Reviewer/Approver, Platform admin ↔ netCI ↔ GitLab, Jenkins,
   Harbor, Keycloak, Temporal, máy đích, Prometheus.
2. **Container (C4 L2):** mục 2, tách rõ khối CI (Jenkins) và CD (Temporal worker).
3. **Sequence commit → deploy:** mục 3.
4. **State diagram** pipeline và deployment: mục 4.
5. **Shared pipeline:** script → parser → blocks → Jenkins stages, với credential theo stage: mục 5.
6. **Trust boundaries:** mục 7.
7. **ER diagram** theo nhóm: mục 8.
8. **Deployment lab corp:** mục 9c (node, VIP, ingress, Harbor, GitLab, Velero/S3).
9. **Toolchain governance:** versions.yaml → plugins.txt → image → drift check → gate: mục 6.
10. **Portal navigation:** mục 10.

Định dạng gợi ý: Mermaid (`flowchart`, `sequenceDiagram`, `stateDiagram-v2`, `erDiagram`) để nằm được
trong repo và review được; hoặc SVG/PNG từ đó. Mỗi sơ đồ ghi commit nó phản ánh (`695d08e`).

---

## 14. Tài liệu gốc nên đọc thêm (trong repo)

- `docs/HUONG-DAN-HIEU-TOAN-BO-NETCI.md`: giải thích toàn hệ thống bằng tiếng Việt, có bảng thuật ngữ.
- `docs/architecture.md`, `docs/domain-model.md`, `docs/state-machine.md`, `docs/security-model.md`.
- `api/openapi.yaml`: hợp đồng API đầy đủ.
- `infra/corp/README.md`, `docs/RUNBOOK-CORP-LAB.md`: lab corp.
- `docs/decisions/`: 59 ADR.
