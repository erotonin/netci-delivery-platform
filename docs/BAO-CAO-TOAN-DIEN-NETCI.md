# Báo cáo toàn diện dự án netCI Delivery Platform

> Mục đích của tài liệu: giúp người thực hiện hiểu, vận hành, giải thích và bảo vệ các quyết định kỹ thuật của dự án; đồng thời phân biệt trung thực giữa phần đã chạy được trên Windows và phần phải hoàn thiện, kiểm chứng trên Ubuntu 24.04.

## 1. Kết luận ngắn gọn trước khi đi vào chi tiết

netCI là một **nền tảng điều phối quy trình bàn giao phần mềm nội bộ**. Người dùng khai báo ứng dụng/module và yêu cầu chạy pipeline trên Portal; Jenkins thực hiện CI; netCI áp dụng chính sách và điều phối CD; Temporal giữ trạng thái cho các quy trình triển khai dài; Ansible hoặc Helm triển khai cùng một artifact bất biến tới Docker, Kubernetes hoặc Systemd.

Ở thời điểm lập báo cáo, dự án đã đạt mức **local reference implementation** — bản tham chiếu chạy cục bộ để chứng minh kiến trúc, hợp đồng API, giao diện và các quy tắc cốt lõi. Dự án chưa phải một nền tảng production hoàn chỉnh.

Phần Windows hiện có thể dùng để:

- chạy Portal và FastAPI;
- đăng nhập bằng cơ chế mô phỏng cục bộ;
- xem Dashboard, System, Module, Server, thông báo và tìm kiếm;
- tạo module qua wizard, lưu cấu hình CI/CD và môi trường;
- kích hoạt và theo dõi năm bề mặt pipeline theo thiết kế;
- quản lý version, bốn DORA metric, production request và module settings;
- kiểm thử hợp đồng, domain, API, workflow, frontend và build production;
- kiểm tra tĩnh cấu hình Jenkins, template, Helm, Compose và release checklist.

Phần Ubuntu còn phải chứng minh bằng chạy thật:

- Docker Compose đầy đủ, Jenkins A/B và Jenkins agent tạm thời trên kind;
- ba luồng E2E Docker, Kubernetes và Systemd;
- SBOM, quét lỗ hổng, chữ ký số và policy gate bằng artifact thật;
- Temporal worker gọi Ansible/Helm thật;
- failover giữa hai Jenkins controller;
- benchmark agent dùng chung so với agent tạm thời;
- DORA tính từ sự kiện triển khai thật;
- Backstage gọi netCI qua proxy thật.

Vì vậy, câu trình bày chính xác là:

> “Em đã hoàn thành phần thiết kế, domain, API-first backend, Portal theo artifact, cấu hình hạ tầng dưới dạng code và bộ kiểm định portable trên Windows. Bước Ubuntu là giai đoạn tích hợp và thu bằng chứng runtime, không phải viết lại kiến trúc.”

## 2. Bài toán mà dự án giải quyết

Nếu mỗi đội tự viết Jenkinsfile và tự triển khai, tổ chức thường gặp các vấn đề:

- pipeline không đồng nhất;
- quyền triển khai production khó kiểm soát;
- cùng một source nhưng mỗi môi trường lại build một artifact khác;
- thiếu SBOM, kết quả scan và chữ ký;
- khó biết ai đã phê duyệt hoặc triển khai;
- số liệu DORA bị nhập thủ công hoặc không truy ngược được;
- Jenkins controller dễ trở thành điểm nghẽn;
- build có thể làm bẩn máy dùng chung hoặc ảnh hưởng lẫn nhau.

netCI giải quyết bằng năm thuộc tính đầu ra:

1. **Reproducible — tái lập được:** cấu hình và pipeline được định nghĩa bằng code, kết quả có bằng chứng.
2. **Isolated — cô lập:** mỗi build dùng agent tạm thời, workspace bị hủy sau khi hoàn tất.
3. **Extensible — mở rộng được:** runtime và nhà cung cấp nằm sau interface/adapter, không trộn vào domain.
4. **Governed — có quản trị:** production cần approval, artifact phải có đủ bằng chứng bảo mật và audit.
5. **Measurable — đo lường được:** sự kiện CI/CD tạo ra đúng bốn DORA metric.

## 3. Dự án này là gì và không phải là gì

### 3.1 Là gì

- Một Portal self-service cho application/module delivery.
- Một FastAPI service giữ luật nghiệp vụ, trạng thái và hợp đồng HTTP.
- Một kiến trúc mẫu tách Jenkins CI khỏi netCI/Temporal CD.
- Một bộ ba template chuẩn cho Docker, Kubernetes và Systemd.
- Một môi trường lab cục bộ dùng Docker Compose, kind và KVM/libvirt.
- Một bộ acceptance gate và evidence schema để chống “demo xanh giả”.

### 3.2 Chưa phải là gì

- Chưa phải production multi-tenant.
- Chưa có SSO/RBAC doanh nghiệp thật; login Windows hiện là local preview.
- Chưa chứng minh HA production; hai Jenkins cùng host chỉ phục vụ failure drill cục bộ.
- Chưa có Kubernetes managed cloud hoặc secret manager production.
- Chưa có event store hoàn chỉnh; một phần DORA và dữ liệu UI vẫn là projection/fixture.
- Chưa có E2E runtime evidence cho ba target.

## 4. Từ điển thuật ngữ phải nắm

| Thuật ngữ | Giải nghĩa dễ hiểu | Vai trò trong dự án |
|---|---|---|
| CI | Continuous Integration — kiểm thử, build và kiểm tra source sau thay đổi | Jenkins thực hiện checkout, test, build, SBOM, scan, sign, publish |
| CD | Continuous Delivery/Deployment — đưa artifact qua các môi trường | netCI/Temporal điều phối, Ansible/Helm thực thi |
| Domain model | Mô hình hóa sự thật và luật nghiệp vụ, không phụ thuộc UI hay Jenkins | `Application`, `PipelineRun`, `Deployment` và state transition |
| Aggregate | Một nhóm dữ liệu phải thay đổi nhất quán như một đơn vị | Pipeline run và deployment phải chuyển trạng thái hợp lệ cùng nhau |
| State machine | Danh sách trạng thái và các bước chuyển được phép | Ngăn `queued` nhảy thẳng sang `succeeded`, hoặc deploy chưa approval |
| Invariant | Điều luôn phải đúng | Không deploy bằng tag `latest`; prod phải approval; digest phải dạng `sha256:` |
| Port/interface/seam | Hợp đồng mà core nhìn thấy, tạo “đường nối” thay thế được | `JenkinsAdapter`, `RuntimeAdapter`, `ArtifactStore` |
| Adapter | Cài đặt cụ thể của một interface | Jenkins HTTP adapter, Ansible runtime runner |
| Projection/read model | Dữ liệu được định hình sẵn để UI đọc nhanh | Dashboard, danh sách module, DORA cards |
| Idempotency | Gửi lại cùng một lệnh không tạo bản ghi trùng | Header `Idempotency-Key`; cùng key khác payload phải bị từ chối |
| Correlation ID | Mã theo dấu một yêu cầu xuyên nhiều dịch vụ | Header `X-Correlation-Id`, log và audit |
| Immutable digest | Định danh nội dung không đổi, ví dụ `sha256:abc...` | Build một lần rồi promote đúng artifact đó qua mọi môi trường |
| Ephemeral agent | Agent chỉ tồn tại trong thời gian một build | Jenkins tạo pod, chạy pipeline rồi hủy pod/workspace |
| JCasC | Jenkins Configuration as Code | Dựng lại Jenkins từ YAML thay vì cấu hình tay trong UI |
| SBOM | Software Bill of Materials — danh sách thành phần phần mềm | Syft sinh SBOM CycloneDX để truy vết dependency |
| Vulnerability scan | Quét lỗ hổng | Trivy chặn mức High/Critical theo policy hiện tại |
| Signing | Ký artifact để chứng minh nguồn gốc và tính toàn vẹn | Cosign ký image hoặc binary |
| Fail-closed | Khi không xác minh được thì từ chối, không giả vờ thành công | Mất persistence hoặc thiếu evidence thì operation thất bại |
| Temporal workflow | Quy trình bền vững có retry, timeout và chờ lâu | Chờ approval, deploy, health check, rollback |
| Compensation | Hành động bù khi một bước phân tán thất bại | Health check fail thì rollback revision trước |
| DORA | Bốn chỉ số đo hiệu quả delivery | Frequency, Lead Time, Change Failure Rate, MTTR |
| Evidence | Bằng chứng máy có thể đọc: lệnh, thời gian, log, ID, digest | JSON/log dùng cho release acceptance, không chỉ screenshot |
| Reference implementation | Bản chạy được để chứng minh quyết định kiến trúc | Mức hiện tại của dự án |

## 5. Kiến trúc tổng thể và ranh giới trách nhiệm

```text
Người dùng / Git / Backstage
             |
             v
       Portal React
             |
        HTTP / OpenAPI
             v
      FastAPI transport
             |
     Domain + Policy + Audit
       |              |
       | CI           | CD dài hạn
       v              v
 Jenkins Router     Temporal
   |       |           |
 Jenkins A Jenkins B   +--> Ansible/Helm
       |
 ephemeral pod
       |
 test -> build -> SBOM -> scan -> sign -> publish
       |
 immutable digest + evidence
       |
 Docker VM / kind Kubernetes / Systemd VM
```

Ranh giới quan trọng nhất:

- **Portal** thu thập ý định và hiển thị trạng thái; không tự quyết luật domain.
- **FastAPI** chuyển HTTP thành command/query và trả error contract thống nhất.
- **Domain** quyết định trạng thái nào hợp lệ.
- **Policy** quyết định artifact/actor có đủ điều kiện hay không.
- **Jenkins** chịu trách nhiệm CI, không nắm quyền điều phối production dài hạn.
- **Temporal** chịu trách nhiệm workflow CD có retry, wait, timeout và rollback.
- **Ansible/Helm** là executor triển khai tới runtime.
- **PostgreSQL** là persistence; UI không được trở thành nguồn dữ liệu chuẩn.
- **MinIO/Registry** lưu evidence/artifact; digest là danh tính artifact.

Đây là một thiết kế “deep module”: phía ngoài nhìn thấy interface nhỏ, nhưng bên trong che được nhiều chi tiết. Ví dụ Portal chỉ gọi `start pipeline`; nó không cần biết Jenkins credential, queue API, pod spec hay lệnh Buildah.

## 6. Luồng chạy từ đầu đến cuối

### 6.1 Tạo System và Module

1. Người dùng mở Portal và đăng nhập local preview.
2. `App.tsx` kiểm tra session, hash route và render `PortalShell`.
3. Wizard gọi `netciClient.createModule()` tới `/api/systems/{systemId}/modules`.
4. Vite dev server bỏ prefix `/api` và proxy sang FastAPI.
5. Pydantic trong `main.py` kiểm tra hình dạng request.
6. `PortalReadModel` kiểm tra system, slot, runtime, pipeline config và deployment config.
7. Portal tạo Application tương ứng trong `DeliveryPlatform`.
8. Store ghi module/application; nếu Postgres được cấu hình nhưng ghi lỗi thì trả lỗi thay vì báo thành công giả.
9. API trả module; UI chuyển tới trang module mới.

Điểm cần nhớ: **System** là nhóm nghiệp vụ; **Module** là thành phần triển khai; **Application** là aggregate delivery phía core. Portal module ánh xạ tới delivery application.

### 6.2 Kích hoạt pipeline

1. Người dùng chọn pipeline, branch/environment rồi bấm Run.
2. Frontend gửi `Idempotency-Key` và `X-Correlation-Id`.
3. `main.py` gọi `PortalReadModel.start_module_pipeline()`.
4. Portal ánh xạ module sang application và gọi `DeliveryPlatform.start_pipeline()`.
5. Domain tạo `PipelineRun(status=queued)` sau khi kiểm tra application và idempotency.
6. Ở reference hiện tại, UI/API có thể mô phỏng callback để thể hiện vòng đời.
7. Ở bản Ubuntu hoàn chỉnh, bước kế tiếp phải là router chọn Jenkins A/B, Jenkins chạy CI và callback kết quả thật.

### 6.3 CI và chuỗi cung ứng artifact

Jenkins pipeline chuẩn gồm:

1. `checkout`: lấy source theo commit cụ thể.
2. `unit-test`: chạy test.
3. `build`: Buildah tạo OCI image, hoặc Go tạo binary.
4. `sbom`: Syft tạo danh sách thành phần.
5. `vulnerability-scan`: Trivy quét High/Critical.
6. `sign`: Cosign ký artifact.
7. `publish`: đẩy artifact và evidence.

Jenkins phải callback một digest `sha256:...`, không chỉ trả tag. Khi CI thất bại, run chuyển `failed`. Khi CI thành công, domain tạo Deployment: non-prod đi tới `deploying`, prod đi tới `pending_approval`.

### 6.4 Production request và approval

1. Người dùng chọn một hoặc nhiều module.
2. Mỗi module phải có version đã đăng ký.
3. Người dùng chọn thứ tự deploy, lịch chạy, rollback và automation test.
4. API tạo production request theo idempotency key.
5. Người có quyền approve/reject.
6. Approval phải được audit cùng actor và thời điểm.
7. Khi được approve, pipeline từ `waiting_approval` quay lại `running` và deployment từ `pending_approval` sang `deploying`.

### 6.5 CD bằng Temporal

1. Workflow nhận application, environment và immutable digest.
2. Activity kiểm tra evidence: digest, SBOM, scan, signature và decision.
3. Nếu prod, workflow chờ approval tối đa 24 giờ.
4. Activity chọn playbook theo runtime và deploy, có retry.
5. Activity health check, cũng có retry.
6. Nếu healthy, trả `healthy`.
7. Nếu không healthy, gọi rollback và trả `rolled_back`.

Temporal hữu ích vì một request HTTP không nên mở 24 giờ để chờ approval. Temporal lưu tiến độ workflow để process có thể restart mà quy trình vẫn tiếp tục.

### 6.6 DORA

Thiết kế và UI phải có **đúng bốn metric**, không phải năm:

1. **Deployment Frequency:** số lần deploy production thành công trong khoảng thời gian.
2. **Lead Time for Changes:** thời gian từ commit/change tới deployment production thành công.
3. **Change Failure Rate:** tỷ lệ deployment gây failure/rollback.
4. **Time to Restore Service (MTTR):** thời gian từ failure tới khi dịch vụ được khôi phục.

`backend/app/projections/dora.py` đã có thuật toán theo event. Tuy nhiên dashboard Portal hiện chưa hoàn toàn lấy số từ event runtime persistent. Đây là việc phải hoàn tất và chứng minh trên Ubuntu.

## 7. Domain model và state machine

### 7.1 Các thực thể chính

- `Application`: tên, repository, template, runtime và danh sách stage được phép.
- `PipelineRun`: commit, environment, parameters, trạng thái, artifact digest.
- `Deployment`: liên kết run, environment, digest, trạng thái và approval actor.
- `AuditEvent`: ai làm gì, lúc nào, với correlation ID nào.
- `DeliveryEvent`: dữ liệu đầu vào cho projection DORA.
- `PortalSystem`, `PortalModule`, `PortalProductionRequest`: read/write model phục vụ UX Portal.

### 7.2 Pipeline state machine

```text
queued -> running -> succeeded
                  -> failed
                  -> waiting_approval -> running
queued/running/waiting_approval -> cancelled
succeeded/failed -> rolled_back (theo điều kiện deployment)
```

### 7.3 Deployment state machine

```text
pending_approval -> deploying -> healthy
                              -> failed
healthy/failed -> rolled_back
```

State machine không chỉ để vẽ sơ đồ. Nó bảo vệ hệ thống khỏi callback đến sai thứ tự, retry lặp, hoặc người dùng approve một deployment đã kết thúc.

### 7.4 Các invariant quan trọng

- Application name phải theo regex quy định và không trùng.
- Template phải phù hợp runtime.
- Stage phải thuộc catalog, không trùng và giữ đúng thứ tự.
- Cùng idempotency key + cùng payload trả lại cùng kết quả; cùng key + khác payload là conflict.
- CI thành công phải có digest `sha256:` hợp lệ.
- Production phải approval.
- Deploy không dùng `latest`.
- Rollback chỉ tới revision/digest hợp lệ đã biết.
- DORA nối sự kiện bằng application/deployment/environment, không đoán theo tên hiển thị.

## 8. Giải thích cấu trúc và logic từng file

Phần này nhóm file theo trách nhiệm. Những file sinh tự động như lockfile không cần học từng dòng, nhưng phải hiểu vai trò của chúng.

### 8.1 File gốc

| File | Logic/vai trò |
|---|---|
| `README.md` | Tuyên ngôn kiến trúc, phạm vi local reference, mục tiêu và đường dẫn tài liệu chính. Đây là điểm bắt đầu cho reviewer. |
| `QUICKSTART.md` | Hướng dẫn chạy nhanh Windows/Ubuntu và các lệnh kiểm tra. Không thay thế acceptance evidence. |
| `Makefile` | Giao diện lệnh thống nhất cho doctor, validate, test, up, kind, E2E, benchmark và release gate. Các target Ubuntu chưa sẵn sàng chủ động trả `BLOCKED`, tránh xanh giả. |
| `docker-compose.yml` | Mô tả Postgres, Registry, MinIO, Temporalite, API, Temporal worker và Jenkins A/B; gắn volume, port, healthcheck và dependency. |
| `.env.example` | Danh sách biến môi trường mẫu. Credential chỉ dùng local, phải đổi trước môi trường chia sẻ. |
| `release-checklist.yaml` | Manifest 21 gate: 11 `ready`, 10 `blocked`. Đây là nguồn sự thật về trạng thái release, không phải README cảm tính. |
| `pytest.ini` | Cấu hình pytest/marker và đường dẫn test. |
| `.gitattributes` | Chuẩn hóa line ending giữa Windows/Linux, rất quan trọng cho shell script khi chuyển Ubuntu. |

### 8.2 Tài liệu kiến trúc và quyết định

| File | Nội dung cần nắm |
|---|---|
| `docs/architecture.md` | Sơ đồ thành phần, ownership, topology Compose/kind/KVM và các nguyên tắc correlation/idempotency. |
| `docs/domain-model.md` | Ubiquitous language — bộ từ chung giữa code và người trình bày; quan hệ và invariant. |
| `docs/api-contract.md` | Quy ước request/response/error, idempotency, correlation và callback. |
| `docs/state-machine.md` | Trạng thái hợp lệ của pipeline/deployment và rollback. |
| `docs/security-model.md` | Build-once, digest, SBOM, Trivy, Cosign, verify-before-deploy và deny mặc định. |
| `docs/dora-metrics.md` | Định nghĩa đúng bốn DORA metric và cách ghép sự kiện. |
| `docs/portal-api.md` | Endpoint phục vụ dashboard/system/module/version/request. |
| `docs/troubleshooting.md` | Cách khoanh vùng lỗi theo lớp và lệnh chẩn đoán. |
| `docs/assumptions.md` | Các quyết định mặc định đã chốt để không treo dự án vì câu hỏi mở. |
| `docs/decisions/ADR-001-system-boundary.md` | Chốt ranh giới netCI so với Jenkins/runtime. |
| `ADR-002-custom-portal-backstage.md` | Portal tùy biến là UI chính; Backstage là thử nghiệm tích hợp. |
| `ADR-003-temporal-boundary.md` | Chỉ dùng Temporal cho quy trình CD dài, không biến mọi thao tác CRUD thành workflow. |
| `ADR-004-jenkins-ci-netci-cd.md` | Jenkins sở hữu CI; netCI/Temporal sở hữu CD/policy. |
| `ADR-005-runtime-adapters.md` | Docker/Kubernetes/Systemd nằm sau adapter để core không phụ thuộc provider SDK. |
| `ADR-006-jcasc-config-source-of-truth.md` | Jenkins phải tái tạo từ JCasC, không dựa vào click tay. |
| `ADR-007-ephemeral-agent-isolation.md` | Build chạy trên pod tạm thời, controller không chạy build. |
| `ADR-008-artifact-security.md` | Chốt Syft + Trivy + Cosign và evidence bắt buộc. |
| `ADR-009-multi-controller-routing.md` | Cách chọn Jenkins khỏe/có capability/ít queue và ý nghĩa hai controller local. |
| `ADR-010-local-vs-production-target.md` | Phân biệt demo local và topology production, tránh claim HA quá mức. |
| `docs/BAO-CAO-TOAN-DIEN-NETCI.md` | Tài liệu đang đọc: nối kiến trúc, code, trạng thái và bộ vấn đáp thành một câu chuyện. |

### 8.3 API contract và schema

| File | Logic/vai trò |
|---|---|
| `api/openapi.yaml` | Hợp đồng công khai của API. Contract test bảo đảm route/model trong code không lệch tài liệu. |
| `api/dora-dashboard.schema.json` | Schema dashboard bắt buộc đúng 4 metric và có số lượng source event. |
| `api/benchmark-report.schema.json` | Chuẩn JSON cho báo cáo baseline/ephemeral, phase timing và kết luận regression. |
| `evidence/security-evidence.schema.json` | Chuẩn evidence gồm digest, SBOM, vulnerability scan, signature và policy decision. |
| `evidence/windows-static-validation.json` | Bằng chứng đã chạy kiểm định tĩnh Windows; không đại diện cho E2E Ubuntu. |

### 8.4 Backend core

| File | Logic chạy |
|---|---|
| `backend/app/main.py` | Composition/transport layer FastAPI: tạo app, CORS, middleware correlation, Pydantic request model, error mapping, callback auth và khai báo route. Route gọi domain/portal thay vì chứa toàn bộ luật nghiệp vụ. |
| `backend/app/domain/models.py` | Enum runtime/environment/status, frozen dataclass và bảng transition. Đây là từ vựng cốt lõi và “hàng rào” state machine. |
| `backend/app/delivery.py` | `DeliveryPlatform`: tạo application, start pipeline, nhận CI result, approval, deployment result và rollback; kiểm tra catalog, digest, idempotency, transition và audit. Có in-memory store và optional Postgres persistence. |
| `backend/app/portal.py` | `PortalReadModel`: dữ liệu system/module/version/production request cho UI; seed demo; tạo module; ánh xạ module sang application; trigger pipeline; xử lý request và các projection dashboard/DCIM/server/audit. |
| `backend/app/persistence.py` | Hai repository Postgres cho delivery và Portal; chuyển dataclass ↔ row/JSON; seed dữ liệu; load/save; health. Khi persistence được cấu hình nhưng lỗi, Portal fail-closed. |
| `backend/schema.sql` | DDL cho application, run, deployment, audit, idempotency, system, module, version, production request, join table, constraint, index và trigger `updated_at`. Hiện là schema thô, chưa có migration versioned. |
| `backend/app/policy/rules.py` | `ArtifactEvidence` và policy yêu cầu digest/SBOM/scan/signature; production kiểm tra role. Chính sách hiện đơn giản và cần nối chặt vào luồng thật. |
| `backend/app/projections/dora.py` | Sắp xếp delivery event, tính frequency/lead time/failure rate/restore time theo đúng khóa liên kết. Chưa thay hoàn toàn số mô phỏng trên Portal. |
| `backend/app/adapters/interfaces.py` | Các Protocol: Jenkins, runtime, artifact store, image builder, policy. Đây là seam giúp test và thay implementation. |
| `backend/app/adapters/jenkins_router.py` | Lọc controller healthy, đúng capability, còn capacity; chọn queue nhỏ nhất, capacity lớn nhất và ID ổn định. Hiện mới là thuật toán in-memory. |
| `backend/app/adapters/jenkins_http.py` | REST client Jenkins: health, tạo/cập nhật freestyle job XML, trigger, xem status/log và abort bằng basic auth. Đây là PoC adapter, chưa nối vào API production flow. |
| `backend/requirements.txt` | Dependency runtime được pin cho FastAPI, Postgres, Temporal, HTTP/YAML. |
| `backend/requirements-dev.txt` | Dependency kiểm thử/quality dùng trong development. |
| `backend/Dockerfile` | Image chạy API/worker từ source và requirements; cần build/test thực trên Ubuntu. |

### 8.5 Temporal workflow

| File | Logic chạy |
|---|---|
| `backend/app/workflows/provision_and_deploy.py` | Workflow bền vững: validate artifact một lần, chờ approval nếu cần, deploy retry 3, health retry 3, rollback nếu unhealthy. |
| `backend/app/workflows/activities.py` | Side-effect thật: đọc evidence JSON an toàn, chạy Syft/Trivy/Cosign, dựng command Ansible theo runtime, gọi health URL. Workflow chỉ điều phối; activity mới được phép đụng I/O. |
| `backend/app/workflows/worker.py` | Kết nối Temporal server, đăng ký workflow/activity và chạy worker. Wiring hiện dùng filesystem evidence + Ansible runner. |

### 8.6 Backend và contract tests

| File | Điều được bảo vệ |
|---|---|
| `backend/tests/test_api.py` | Route, validation, error contract, idempotency, correlation, callback auth và state flow. |
| `backend/tests/test_portal.py` | System/module/version/request, seed, pipeline config, persistence fail-closed và các edge case Portal. |
| `backend/tests/test_policy.py` | Cho phép/từ chối theo evidence và production role. |
| `backend/tests/test_dora.py` | Công thức DORA, thứ tự event và khóa liên kết. |
| `backend/tests/test_temporal_workflow.py` | Workflow logic; có thể skip khi môi trường test Temporal không đủ — đây là skip có chủ đích hiện tại. |
| `backend/tests/test_workflow_activities.py` | Evidence path, tool result, Ansible command và health behavior. |
| `tests/contract/test_openapi.py` | OpenAPI parse được và route/schema quan trọng tồn tại. |
| `tests/contract/test_state_machine.py` | State machine không lệch tài liệu/hợp đồng. |
| `tests/failure/test_failure_drill.py` | Harness luôn rejoin controller và ghi evidence cả thành công/thất bại. Không chứng minh controller thật đã failover. |
| `tests/performance/test_benchmark.py` | Parse phase timing, tính delta và đánh dấu inconclusive/regression. Không phải benchmark runtime thật. |
| `tests/acceptance.md` | Danh sách scenario E2E và evidence phải thu cho nghiệm thu. |

### 8.7 Frontend

| File | Logic chạy |
|---|---|
| `frontend/src/main.tsx` | Entry point React, gắn `App` vào DOM và nạp CSS. |
| `frontend/src/App.tsx` | Hash router, session guard, route map, login/logout, layout và điều hướng sau khi tạo module. Dùng hash giúp local static hosting không cần server rewrite route. |
| `frontend/src/LoginPage.tsx` | UI login bám artifact; SSO/password chỉ là local preview; kiểm tra trường bắt buộc và không lưu password. |
| `frontend/src/PortalShell.tsx` | Sidebar, topbar, breadcrumb, search, notification, settings và logout; nạp systems; modal có Escape/focus handling. |
| `frontend/src/PortalFeedback.tsx` | Context/toast chung để operation báo success/error nhất quán. |
| `frontend/src/GeneralPages.tsx` | Dashboard, systems, system detail và servers. Ưu tiên API, fallback fixture khi cần; server CRUD hiện có local session overlay. |
| `frontend/src/NewModuleWizard.tsx` | Wizard 3 bước: thông tin chung, CI/CD, deployment; validate runtime target, health check và gửi cấu hình đầy đủ về API. |
| `frontend/src/ModulePage.tsx` | Tabs overview/pipelines/versions/DORA; trigger run, history, run detail, stage graph, log; hiển thị đúng branch/stage đã lưu và đúng 4 DORA metric. Một số chart/log vẫn mang tính minh họa. |
| `frontend/src/ModuleSettings.tsx` | General, pipelines, access, team, activity; phần đã có API dùng API, phần backend chưa có dùng session preview hoặc disable kèm giải thích. |
| `frontend/src/ProductionRequestsPage.tsx` | List/create/approve/reject request; chọn nhiều module, registered version, thứ tự, schedule/timezone, rollback và automation. Không cho chọn module chưa có version. |
| `frontend/src/portalData.ts` | Fixture/fallback theo artifact để UI không trắng khi API chưa sẵn sàng. Không được xem là production source of truth. |
| `frontend/src/api/netciClient.ts` | Typed HTTP client; base URL mặc định `/api`; parse error JSON; tự thêm idempotency/correlation cho mutation; gom endpoint tại một chỗ. |
| `frontend/src/styles.css` | Design tokens, typography, color, spacing, component state, modal/chart/table và responsive rules. |
| `frontend/src/vite-env.d.ts` | Type declaration cho Vite environment. |
| `frontend/src/test/setup.ts` | Chuẩn bị jsdom/testing-library và cleanup cho test. |
| `frontend/src/App.test.tsx` | Auth guard và navigation cơ bản. |
| `LoginPage.test.tsx` | Login validation và bảo đảm password không bị lưu. |
| `GeneralPages.test.tsx` | Dashboard thay dữ liệu fallback bằng response API. |
| `ModulePage.test.tsx` | Pipeline config/stage/version/DORA hiển thị theo dữ liệu. |
| `ModuleSettings.test.tsx` | Hành vi setting, disabled action và preview persistence. |
| `frontend/vite.config.ts` | React plugin, test config và proxy `/api` sang FastAPI có rewrite prefix. |
| `frontend/package.json` | Script dev/build/test và dependency pin chính xác. |
| `frontend/package-lock.json` | Khóa toàn bộ dependency tree để cài đặt tái lập; không sửa tay. |
| `frontend/tsconfig.json` | TypeScript strict/build configuration. |
| `frontend/index.html` | HTML shell chứa root element và entry module. |

### 8.8 Jenkins

| File | Logic/vai trò |
|---|---|
| `jenkins/Dockerfile.controller` | Dựng Jenkins LTS JDK21, cài tool/plugin pin, đưa JCasC và snapshot shared library thành local Git repository. |
| `jenkins/plugins.txt` | Plugin Jenkins có version cụ thể, tránh rebuild hôm sau nhận plugin khác. |
| `jenkins/casc/base.yaml` | Cấu hình chung: `numExecutors: 0`, local admin, shared library và seed job. Controller không chạy build. |
| `jenkins/casc/controller-a.yaml` | Identity/overlay riêng của Jenkins A. |
| `jenkins/casc/controller-b.yaml` | Identity/overlay riêng của Jenkins B. |
| `jenkins/casc/ephemeral-agent.yaml` | Kubernetes cloud/pod template: namespace `netci-build`, emptyDir workspace, non-root, resource limit, không mount host socket, không auto-mount service account token. |
| `jenkins/shared-library/vars/netciPipeline.groovy` | Pipeline dùng chung thực hiện checkout → test → build → SBOM → scan → sign → publish; archive evidence và luôn cleanup. Chưa được sinh động hoàn toàn từ Portal config. |
| `jenkins/agent-toolbox/Dockerfile` | Image build agent chứa Buildah, Syft, Trivy, Cosign, Go với version/checksum pin; chạy UID 1000. |
| `jenkins/scripts/build-agent-toolbox.sh` | Lệnh build/tag toolbox image trước khi Jenkins tạo agent. |

### 8.9 Template CI/CD

| Nhóm file | Logic/vai trò |
|---|---|
| `templates/catalog.yaml` | Catalog ba template, runtime, artifact kind, adapter, stage và environment. Đây là source of truth cho lựa chọn Portal. |
| `templates/container-ci-cd-v1/netci-template.yaml` | Contract template container/Docker; prod yêu cầu approval. |
| `templates/container-ci-cd-v1/scripts/ci/common.sh` | Validate biến, tag và registry reference dùng chung. |
| `test.sh` | Python compile/unit test. |
| `build.sh` | Rootless Buildah tạo OCI archive. |
| `sbom.sh` | Syft tạo CycloneDX SBOM. |
| `scan.sh` | Trivy chặn High/Critical. |
| `push-image.sh` | Push image và lấy immutable digest. |
| `sign.sh` | Cosign ký bằng key được cấp. |
| `publish.sh` | Chỉ publish khi đủ artifact/evidence. |
| `templates/kubernetes-ci-cd-v1/netci-template.yaml` | Contract image triển khai Kubernetes. |
| `templates/kubernetes-ci-cd-v1/scripts/ci/*.sh` | Wrapper/tái sử dụng chuỗi container CI cho sample Kubernetes, tránh copy logic không cần thiết. |
| `templates/systemd-ansible-ci-cd-v1/netci-template.yaml` | Contract binary triển khai Systemd qua Ansible. |
| `templates/systemd-ansible-ci-cd-v1/scripts/ci/common.sh` | Validate input/path/version cho binary. |
| `test.sh`, `build.sh` | Go test và build binary. |
| `sbom.sh`, `scan.sh` | SBOM cho file và Trivy filesystem/rootfs scan. |
| `sign.sh` | Cosign `sign-blob` cho binary. |
| `publish.sh` | Upload binary và evidence với digest. |

### 8.10 Hạ tầng và deployment

| File | Logic/vai trò |
|---|---|
| `infra/kind/kind-config.yaml` | kind cluster gồm control-plane/worker và port mapping cần thiết. |
| `infra/kind/namespaces.yaml` | Tạo `netci-build`, `dev`, `staging`, `prod`. |
| `infra/kind/jenkins-agent-rbac.yaml` | Quyền tối thiểu để Jenkins tạo/đọc/xóa pod agent trong namespace build. |
| `infra/kind/local-registry.sh` | Kết nối local Registry vào network của kind và cấu hình endpoint. |
| `infra/libvirt/README.md` | Runbook hai VM Docker/Systemd và IP lab. Hiện chưa phải provisioning automation hoàn chỉnh. |
| `deploy/ansible/requirements.yml` | Collection Ansible cần cài, gồm Kubernetes/Docker dependencies. |
| `deploy/ansible/inventories/local.ini` | Inventory lab với host/group Docker, Kubernetes/Systemd. IP phải khớp VM Ubuntu thật. |
| `deploy/ansible/playbooks/deploy-docker.yml` | Từ chối tag/digest không hợp lệ, render Compose, pull/up, health check; rescue khôi phục compose trước hoặc stop. |
| `hello-container.compose.yml.j2` | Template Compose inject image bằng `repository@digest`. |
| `deploy-kubernetes.yml` | Helm atomic/wait rồi kiểm tra image đang chạy đúng digest. |
| `deploy-systemd.yml` | Verify SHA, đưa binary vào releases, đổi symlink `current`, restart/health; rescue symlink cũ và lưu journal. |
| `hello-systemd.service.j2` | Unit file chạy binary từ symlink current với restart policy. |
| `deploy/helm/sample-kubernetes-app/Chart.yaml` | Metadata chart mẫu. |
| `values.yaml` | Giá trị mặc định; digest sentinel chỉ để lint, runtime phải inject digest thật. |
| `values.schema.json` | Validate kiểu/required value đầu vào chart. |
| `templates/_helpers.tpl` | Helper tên/label chuẩn. |
| `templates/deployment.yaml` | Kubernetes Deployment dùng `repository@digest`, probe/resource/security context. |
| `templates/service.yaml` | Kubernetes Service cho sample app. |

### 8.11 Backstage

| File | Logic/vai trò |
|---|---|
| `backstage/netci-template.yaml` | Software Template hỏi application/runtime/repository rồi gọi cùng netCI API qua proxy. Không tạo business logic thứ hai. |
| `backstage/app-config.example.yaml` | Cấu hình proxy Backstage → netCI. Cần instance thật để xác minh auth/network/output. |

### 8.12 Sample applications

| File/nhóm | Logic/vai trò |
|---|---|
| `sample-apps/hello-container/app.py`, `test_app.py`, `Dockerfile` | Ứng dụng Python nhỏ có health/test, làm input E2E Docker. |
| `sample-apps/hello-kubernetes/app.py`, `test_app.py`, `Dockerfile` | Ứng dụng tương tự dùng cho Helm/kind E2E. |
| `sample-apps/hello-systemd-go/main.go`, `main_test.go`, `go.mod`, `build.sh` | HTTP service Go nhỏ và build script dùng cho binary/Systemd E2E. |

### 8.13 Script kiểm định và bằng chứng

| File | Logic/vai trò |
|---|---|
| `scripts/doctor.py` | Kiểm OS và version công cụ. Windows kiểm Python/Git/Node/npm; Ubuntu kiểm thêm Docker, Compose, kubectl, kind, Helm, Ansible, Go, Syft, Trivy, Cosign, virsh và daemon/connection. |
| `scripts/validate_windows.py` | Kiểm 31 file bắt buộc, YAML, compile Python, dependency frontend pin, Vite proxy, API wiring, Backstage và tham chiếu Makefile. |
| `scripts/validate_catalog.py` | Bảo vệ đúng ba template, runtime/adapter, stage duy nhất/đúng thứ tự, security stage và ba environment. |
| `scripts/validate_platform.py` | Kiểm tĩnh Compose services, JCasC A/B, plugin pin, Helm files/digest và security evidence schema. |
| `scripts/validate_release.py` | Validate manifest, phát hiện false-green marker, lọc profile và chỉ execute khi không còn required blocked gate. Exit code 2 nghĩa là bị chặn có chủ đích. |
| `scripts/trigger_pipeline.py` | CLI/webhook adapter nhỏ gửi start pipeline với commit, environment, idempotency và correlation ID. |
| `scripts/collect_evidence.py` | Chạy một command, ghi command/time/duration/exit/stdout/stderr/environment thành JSON; trả lại exit code gốc. |
| `scripts/benchmark.py` | Chạy baseline và ephemeral nhiều lần; parse `NETCI_TIMING_JSON`, xuất CSV/JSON, tính success, phase average, cache hit và regression threshold. Thiếu phase marker thì `inconclusive`, không xanh giả. |
| `scripts/failure_drill.py` | Thực hiện stop → poll detect → reroute → complete → rejoin trong `finally`; ghi output tail, timestamp và MTTR. Cần command thật trên Ubuntu. |

## 9. Những gì đã hoàn thành đến hiện tại

### 9.1 Hoàn thành và đã kiểm chứng trên Windows

- Kiến trúc, domain model, state machine, security model, DORA và 10 ADR.
- FastAPI route và error contract cho core delivery và Portal.
- Frontend bám artifact với login, shell, dashboard, system/module/server, wizard, pipeline, version, DORA, production request và settings.
- Đúng bốn DORA metric theo artifact.
- Idempotency/correlation ở HTTP mutation.
- Domain transition, approval, callback, rollback và audit cơ bản.
- Optional PostgreSQL repositories và schema.
- Temporal workflow/activity code.
- Jenkins JCasC A/B, shared library và ephemeral pod spec.
- Ba template, ba sample app và ba deployment adapter/playbook.
- Release manifest chống false-green.
- Kiểm thử mới nhất ngày 2026-08-27:
  - 77 Python tests được thu thập: 76 pass, 1 skip có chủ đích;
  - 5 frontend test files, 7 tests pass;
  - TypeScript build và Vite production build pass;
  - `validate_windows.py` pass 31 required files;
  - release checklist hợp lệ: 21 checks, 11 ready, 10 blocked.

### 9.2 Đã có code nhưng chưa được phép coi là hoàn tất runtime

- Jenkins HTTP adapter và router chưa được nối khép kín vào start-pipeline flow.
- Temporal workflow chưa được start từ production API flow thật.
- Postgres code có nhưng cần migration/restart/concurrency test thật.
- Security scripts có nhưng chưa chạy artifact thật xuyên suốt Registry/MinIO.
- Ansible/Helm có rollback logic nhưng chưa thu evidence E2E trên ba target.
- DORA projector có thuật toán nhưng dashboard vẫn có dữ liệu tổng hợp/minh họa.
- Backstage template có contract nhưng chưa test với Backstage server thật.
- Settings/server có phần local overlay vì backend endpoint chưa đủ.

### 9.3 Chưa hoàn thành

Mười gate Ubuntu đang `blocked`:

1. kind cluster bootstrap + namespace evidence.
2. Jenkins JCasC destructive rebuild rehearsal.
3. Container E2E.
4. Kubernetes E2E.
5. Systemd E2E.
6. Security deny/allow test với evidence thật.
7. Baseline vs ephemeral benchmark.
8. Jenkins controller failure drill thật.
9. DORA dashboard từ source event thật.
10. Backstage integration test.

## 10. Các điểm kỹ thuật cần cải thiện để hoàn thiện đúng best practice

### P0 — bắt buộc trước khi tuyên bố demo E2E hoàn chỉnh

1. **Nối start pipeline tới Jenkins thật.** API phải gọi router, chọn controller, provision/update job, trigger, lưu external run ID và callback có xác thực.
2. **Nối CI success tới Temporal thật.** Không dừng ở callback mô phỏng; start workflow với deterministic workflow ID/idempotency.
3. **Dùng evidence thật.** Jenkins phải upload SBOM/scan/signature; Temporal phải lấy đúng evidence theo digest và policy phải deny nếu thiếu/sai.
4. **Chạy ba runtime target.** Docker VM, kind Kubernetes và Systemd VM đều phải deploy đúng cùng digest, health check và rollback.
5. **Hoàn thành persistence.** Dùng Postgres trong E2E; kiểm tra restart không mất state/log/idempotency.
6. **Hoàn thành DORA event pipeline.** Mọi transition phát event bền vững; projector đọc event; dashboard kèm `sourceEventCount`.
7. **Thu evidence cho từng gate.** Command, timestamp, raw log, controller/run/workflow ID, digest và kết luận.

### P1 — cần cho một bản demo chắc chắn và dễ bảo vệ

1. Thêm versioned migration như Alembic; không chỉ chạy `schema.sql` thủ công.
2. Persist idempotency record; hiện schema có bảng nhưng core còn giữ map trong memory.
3. Thêm optimistic concurrency/version hoặc compare-and-set cho transition tránh hai callback đồng thời.
4. Persist pipeline log/audit đầy đủ, tránh mất khi restart.
5. Dùng transactional outbox cho state + event, tránh lưu state thành công nhưng phát DORA event thất bại.
6. Chuẩn hóa Jenkins job API/pipeline parameter; bỏ giả định freestyle path nếu dùng pipeline job.
7. Dùng secret file/credential store; không đưa key/password vào command line hoặc source.
8. Thêm Playwright E2E cho login, wizard, pipeline, request, logout; thêm accessibility scan.
9. Thêm loading skeleton, retry/error boundary và empty state thống nhất ở mọi page.
10. Tách fixture rõ bằng `demoMode`; không để fallback che lỗi API trong acceptance profile.

### P2 — hướng production, không bắt buộc cho local reference

1. SSO OIDC/SAML thật và RBAC theo system/module/environment.
2. Secret manager, TLS, network policy, backup/restore, retention và log aggregation.
3. Managed PostgreSQL/Temporal/Jenkins topology, không claim HA từ hai container cùng host.
4. Observability với metrics, traces, structured logs và alert.
5. Rate limiting, quota, tenant isolation và audit export.
6. Policy engine mạnh hơn như OPA nếu phạm vi thực tế yêu cầu policy thay đổi độc lập.
7. DR drill và supply-chain provenance/SLSA ở mức phù hợp.

## 11. Kế hoạch chuyển sang Ubuntu theo thứ tự an toàn

### Giai đoạn 1 — chuẩn bị host

1. Clone đúng commit và xác nhận working tree sạch.
2. Chạy `python3 scripts/doctor.py --profile ubuntu`.
3. Sửa line ending/permission script nếu cần; không chỉnh logic chỉ để vượt gate.
4. Copy `.env.example` thành file local, thay credential và giữ ngoài Git.
5. Chạy các gate portable: source structure, catalog, platform static, backend/frontend tests.

Điều kiện qua: doctor xanh và không có dependency trôi phiên bản.

### Giai đoạn 2 — dựng nền tảng dữ liệu/dịch vụ

1. `docker compose config` và `helm lint`.
2. Dựng Postgres, Registry, MinIO, Temporalite và API trước.
3. Xác nhận health, volume persistence và restart recovery.
4. Dựng Temporal worker, kiểm tra registered workflow/activity.
5. Dựng Jenkins A/B từ JCasC, không cấu hình tay.

Điều kiện qua: restart container không làm mất dữ liệu nghiệp vụ; API/Temporal/Jenkins health xanh.

### Giai đoạn 3 — kind và ephemeral agent

1. Tạo kind cluster.
2. Apply namespace và RBAC.
3. Kết nối local Registry.
4. Build/push agent toolbox.
5. Jenkins tạo một pod agent và chạy smoke pipeline.
6. Chứng minh pod biến mất và workspace không còn sau build.

Điều kiện qua: không build trên controller, không Docker socket/hostPath, pod tạm thời cleanup.

### Giai đoạn 4 — ba E2E

Mỗi E2E phải đi qua Portal/API → Jenkins → artifact/evidence → Temporal → runtime → health.

- Container: deploy tới Docker VM bằng digest.
- Kubernetes: Helm atomic lên kind và verify image digest.
- Systemd: binary release directory + symlink + unit restart trên VM.

Mỗi loại chạy thêm một lần failure để chứng minh rollback.

### Giai đoạn 5 — governance và đo lường

1. Security allow case đủ evidence.
2. Security deny case thiếu/sai signature hoặc High/Critical.
3. Production approval và unauthorized callback/actor denial.
4. DORA từ source event thật.
5. Benchmark tối thiểu ba cặp baseline/ephemeral.
6. Stop Jenkins A, detect, reroute B, hoàn tất run, rejoin A.
7. Backstage tạo application qua proxy.

### Giai đoạn 6 — chốt release

Chỉ chuyển từng check từ `blocked` sang `ready` sau khi command thật và assertion thật tồn tại. Sau đó chạy release gate Ubuntu và lưu evidence theo release/commit/digest.

## 12. Demo script đề xuất cho mentor

### Bản 10 phút

1. **1 phút — bài toán:** pipeline phân mảnh, thiếu policy, build lại artifact, thiếu đo lường.
2. **1 phút — kiến trúc:** Portal → API/domain → Jenkins CI → digest/evidence → Temporal CD → ba runtime.
3. **2 phút — Portal:** login, dashboard, system/module, wizard và saved pipeline config.
4. **2 phút — pipeline:** trigger, state graph, idempotency/correlation, artifact digest.
5. **1 phút — production request:** version, module order, schedule, approval.
6. **1 phút — security:** SBOM/Trivy/Cosign và fail-closed.
7. **1 phút — DORA:** đúng bốn metric, tính từ event.
8. **1 phút — trung thực phạm vi:** Windows complete gì, Ubuntu còn 10 gate gì.

### Câu mở đầu mẫu

> “netCI không thay Jenkins. Nó đặt một lớp domain, policy và orchestration phía trên Jenkins để chuẩn hóa cách khai báo ứng dụng, build artifact một lần, kiểm chứng artifact rồi promote chính digest đó tới Docker, Kubernetes hoặc Systemd. Em chọn Jenkins cho CI vì hệ sinh thái build mạnh; Temporal cho CD vì approval, retry, timeout và rollback là workflow dài hạn.”

### Câu kết mẫu

> “Giá trị của bản hiện tại không chỉ nằm ở màn hình. Em đã chốt contract, state machine, interface, cấu hình hạ tầng bằng code và release gate chống xanh giả. Em không gọi các phần chưa chạy trên Ubuntu là đã hoàn tất; chúng được ghi rõ thành 10 gate blocked với bằng chứng cần thu.”

## 13. Bảy điều cần thống nhất với mentor

Đây không phải bảy câu bạn “không biết”. Chúng là bảy quyết định phạm vi cần nói rõ để mentor xác nhận rằng tiêu chí chấm không khác giả định của dự án.

1. **Mức bàn giao:** local reference implementation, không phải production platform.
2. **Nguồn UI:** artifact Claude là source of truth về thành phần/luồng; DORA có đúng 4 metric.
3. **Cổng self-service:** custom Portal là chính; Backstage chỉ chứng minh khả năng tích hợp qua cùng API.
4. **Phân quyền engine:** Jenkins sở hữu CI; netCI/Temporal sở hữu policy và CD.
5. **Topology demo:** Ubuntu native + Compose + kind + hai VM KVM/libvirt; hai Jenkins cùng host không được gọi là production HA.
6. **Security/production policy:** build once/promote digest; prod approval; SBOM + High/Critical scan + Cosign signature là gate bắt buộc.
7. **Definition of Done:** hoàn tất khi ba E2E, rollback, security deny, DORA event, benchmark, failure drill và evidence đều chạy thật; không chỉ có code/config.

Nếu mentor đồng ý bảy điểm này, hướng dự án hiện tại là chính xác. Nếu mentor đổi một điểm, phải cập nhật ADR, acceptance criteria và release checklist trước khi code tiếp.

## 14. Bộ câu hỏi vấn đáp kỹ thuật giả định và trả lời mẫu

### 14.1 Nhóm tổng quan

**Hỏi: netCI khác Jenkins ở đâu?**

Đáp: Jenkins là execution engine cho CI. netCI là control plane giữ application model, policy, audit, approval, deployment orchestration và DORA. netCI gọi Jenkins chứ không viết lại Jenkins.

**Hỏi: Tại sao không để Jenkins làm luôn CD?**

Đáp: Jenkins có thể deploy, nhưng approval dài, retry nhiều giờ, rollback và state đa hệ thống sẽ làm pipeline khó phục hồi và khó quản trị. Temporal phù hợp hơn cho durable workflow; Jenkins tập trung vào build ngắn hạn.

**Hỏi: Vì sao cần Portal khi đã có Jenkins UI?**

Đáp: Jenkins UI nói ngôn ngữ job/build. Portal nói ngôn ngữ application/module/version/environment/production request, che chi tiết Jenkins và chuẩn hóa policy cho người dùng.

**Hỏi: Backstage có bị trùng Portal không?**

Đáp: Không có hai business core. Backstage Software Template chỉ là client khác của cùng API. Portal là giao diện chính theo artifact; Backstage chứng minh extensibility.

**Hỏi: Sản phẩm đầu ra cụ thể là gì?**

Đáp: Một repository có Portal, API/domain, persistence, workflow, Jenkins-as-code, template CI/CD, deployment adapter, sample apps, lab topology, test và machine-readable evidence/release gates.

### 14.2 Nhóm domain và API

**Hỏi: Vì sao cần state machine thay vì chỉ cập nhật status?**

Đáp: Callback có thể trễ, lặp hoặc sai thứ tự. State machine từ chối transition vô nghĩa, bảo vệ invariant và giúp audit đáng tin.

**Hỏi: Idempotency giải quyết việc gì?**

Đáp: Người dùng double-click hoặc mạng retry không được tạo hai pipeline/deployment. Cùng key và cùng payload trả kết quả cũ; cùng key nhưng payload khác báo conflict.

**Hỏi: Idempotency đã hoàn chỉnh chưa?**

Đáp: HTTP/domain behavior và schema đã có, nhưng record core hiện còn phần in-memory. Trước E2E/production-like phải persist và test qua restart/concurrency.

**Hỏi: Correlation ID khác pipeline ID thế nào?**

Đáp: Pipeline ID định danh một entity. Correlation ID theo dấu toàn bộ chuỗi request/log/callback có thể chạm nhiều entity và dịch vụ.

**Hỏi: Tại sao transport không nên chứa business rule?**

Đáp: Nếu rule nằm trong route, CLI/Backstage/worker dễ có hành vi khác nhau và khó test. Route chỉ parse/auth/map error; domain là một nguồn luật duy nhất.

**Hỏi: Fail-closed là gì?**

Đáp: Nếu hệ thống không lưu được hoặc không xác minh được evidence, operation thất bại. Không trả success rồi chỉ giữ dữ liệu tạm, vì như vậy audit và governance sai.

**Hỏi: API-first đem lại gì?**

Đáp: Portal và Backstage dùng chung contract; có contract test; UI có thể thay mà domain không đổi; tích hợp Git/webhook cũng dùng cùng command.

### 14.3 Nhóm artifact và security

**Hỏi: Tại sao digest tốt hơn tag?**

Đáp: Tag có thể bị trỏ lại sang nội dung khác; digest là hash của nội dung. `repo@sha256:...` giúp staging và prod chắc chắn chạy đúng artifact đã test.

**Hỏi: “Build once, promote many” nghĩa là gì?**

Đáp: CI build một artifact một lần. Các môi trường chỉ promote cùng digest, không rebuild theo environment, nên giảm drift và tăng khả năng audit.

**Hỏi: SBOM, scan và sign khác nhau thế nào?**

Đáp: SBOM liệt kê thành phần; scan đối chiếu chúng với lỗ hổng; signature chứng minh artifact được pipeline tin cậy ký và không bị đổi. Ba bằng chứng bổ sung cho nhau.

**Hỏi: Tại sao security gate phải chạy trước deploy?**

Đáp: Phát hiện sau deploy là quá muộn. Temporal activity xác minh evidence theo đúng digest và fail-closed trước khi gọi runtime adapter.

**Hỏi: Cosign key đặt ở đâu?**

Đáp: Bản local dùng credential injection; không commit key. Hướng production phải dùng Jenkins credential/secret manager hoặc keyless flow phù hợp với identity provider.

### 14.4 Nhóm Jenkins và agent

**Hỏi: Tại sao controller có `numExecutors: 0`?**

Đáp: Để controller chỉ điều phối. Build không chạy trên máy quản trị, giảm blast radius và tránh tài nguyên build làm Jenkins mất ổn định.

**Hỏi: Ephemeral agent an toàn hơn ở đâu?**

Đáp: Mỗi build có pod/workspace riêng và bị hủy sau đó; không lưu rác/credential giữa hai project. Pod không mount Docker socket hoặc hostPath và dùng resource limit.

**Hỏi: Build container thế nào nếu không mount Docker socket?**

Đáp: Dùng rootless Buildah trong toolbox image. Nhờ vậy không trao quyền điều khiển Docker daemon của host cho build.

**Hỏi: Router chọn Jenkins controller thế nào?**

Đáp: Loại controller unhealthy, thiếu capability hoặc hết capacity; sau đó ưu tiên queue nhỏ, capacity lớn và ID ổn định. Bước tiếp theo là nối nó với health/queue thật.

**Hỏi: Hai Jenkins có phải HA không?**

Đáp: Trong lab, chúng chứng minh routing/failover logic. Vì cùng host và còn phụ thuộc chung, không được gọi là production HA.

**Hỏi: JCasC đem lại lợi ích gì?**

Đáp: Jenkins có thể rebuild từ Git, review thay đổi, tái lập A/B và giảm configuration drift từ thao tác tay.

### 14.5 Nhóm Temporal và deployment

**Hỏi: Workflow khác activity thế nào?**

Đáp: Workflow chứa logic điều phối deterministic; activity thực hiện I/O không deterministic như shell, HTTP, Ansible, scan. Temporal retry activity và lưu trạng thái workflow.

**Hỏi: Vì sao approval ở Temporal?**

Đáp: Approval có thể chờ nhiều giờ. Temporal có durable timer/signal nên process restart không làm mất tiến độ; HTTP request hoặc Jenkins executor không phải chờ mở.

**Hỏi: Retry có gây deploy lặp không?**

Đáp: Activity phải được thiết kế idempotent và deploy theo desired state/digest. Ngoài retry của Temporal, hệ thống cần external operation ID và kiểm tra trạng thái đích.

**Hỏi: Rollback ba runtime khác nhau thế nào?**

Đáp: Docker khôi phục Compose/image trước; Kubernetes dùng Helm atomic/revision; Systemd đổi symlink về release trước rồi restart. Domain chỉ gọi `rollback`, adapter che khác biệt.

**Hỏi: Vì sao vẫn dùng Ansible cho Kubernetes khi đã có Helm?**

Đáp: Helm là cơ chế render/release Kubernetes; Ansible có thể là lớp automation chung gọi module Helm và kiểm tra sau deploy. Core không phụ thuộc chi tiết đó.

### 14.6 Nhóm DORA

**Hỏi: DORA có mấy metric?**

Đáp: Đúng bốn: Deployment Frequency, Lead Time for Changes, Change Failure Rate và Time to Restore Service.

**Hỏi: Tại sao không nhập DORA thủ công?**

Đáp: Số thủ công không audit được và dễ thiên lệch. Metric phải là projection từ commit/deployment/failure/recovery event có timestamp và identity.

**Hỏi: Change Failure Rate tính thế nào?**

Đáp: Số deployment production gây failure hoặc rollback chia tổng deployment production trong kỳ. Cần định nghĩa event window nhất quán.

**Hỏi: MTTR ghép failure và recovery ra sao?**

Đáp: Theo cùng application, deployment và environment, rồi lấy thời gian từ failure đến recovery. Không ghép chỉ dựa trên thứ tự toàn cục.

**Hỏi: DORA hiện đã dùng dữ liệu thật chưa?**

Đáp: Thuật toán projection đã có và được test, UI có đúng contract bốn metric, nhưng integration với event persistent/runtime còn là Ubuntu gate.

### 14.7 Nhóm frontend/UX

**Hỏi: Vì sao dùng hash router?**

Đáp: Local static server không cần rewrite mọi route về `index.html`; phần sau `#` do browser xử lý. Production có thể chuyển router chuẩn khi hosting hỗ trợ.

**Hỏi: Login hiện có an toàn production không?**

Đáp: Không. Đây là local preview lưu session marker và không lưu password. Production cần OIDC/SAML, token lifecycle, CSRF/CORS strategy và RBAC.

**Hỏi: Frontend có hoàn toàn dùng API chưa?**

Đáp: Các luồng chính đã gọi API. Một số chart/log, server/settings vẫn dùng fixture hoặc session overlay vì backend chưa đủ; chúng phải được gắn nhãn demo và không dùng làm acceptance source.

**Hỏi: UI đã giống artifact 100% chưa?**

Đáp: Thành phần và luồng chính đã bám artifact. Không nên tuyên bố pixel-perfect 100% nếu chưa có visual regression baseline/screenshot diff theo viewport. Câu chính xác là functional parity cao, còn cần visual regression/accessibility audit để chứng minh tuyệt đối.

**Hỏi: Đã tối ưu UX chưa?**

Đáp: Đã có validation, disabled reason, toast, modal Escape/focus, empty/error states và responsive CSS. Còn cần user test, keyboard/a11y audit, Playwright và đo performance để kết luận tối ưu khách quan.

### 14.8 Nhóm database và độ tin cậy

**Hỏi: Vì sao cần Postgres nếu memory model chạy được?**

Đáp: Memory phù hợp unit test/demo nhanh nhưng mất khi restart và không chia sẻ giữa replicas. Postgres cần cho audit, idempotency và workflow correlation bền vững.

**Hỏi: Nguy cơ hai callback đồng thời là gì?**

Đáp: Cả hai có thể đọc cùng status và ghi hai transition. Cần transaction với row lock hoặc optimistic version/compare-and-set.

**Hỏi: Transactional outbox giải quyết gì?**

Đáp: State và event được commit cùng transaction. Worker phát event sau, tránh trạng thái đã đổi nhưng DORA/audit event bị mất.

**Hỏi: Schema SQL hiện đủ chưa?**

Đáp: Đủ cho reference data model nhưng chưa có migration version, rollback migration, backup/restore và concurrency proof.

### 14.9 Nhóm kiểm thử và acceptance

**Hỏi: Test pass có nghĩa hệ thống hoàn thiện chưa?**

Đáp: Không. Unit/contract/static test chứng minh logic cục bộ. E2E Ubuntu mới chứng minh network, credential, container, cluster, VM và toolchain hoạt động cùng nhau.

**Hỏi: Tại sao release gate có trạng thái blocked?**

Đáp: Để không dùng script placeholder trả exit 0. Blocked nêu rõ thiếu evidence gì; chỉ chuyển ready khi có command và assertion thật.

**Hỏi: Một screenshot Jenkins xanh có đủ evidence không?**

Đáp: Không. Cần command, timestamp, raw log/JSON, application/run/workflow/controller ID, artifact digest, kết quả assertion và commit/version.

**Hỏi: Benchmark đo gì?**

Đáp: So sánh baseline shared agent với ephemeral agent theo queue, provisioning, cache restore, build, cleanup, total, success và cache hit. Thiếu phase timing thì kết luận inconclusive.

**Hỏi: Failure drill pass khi nào?**

Đáp: Controller bị stop thật; monitor phát hiện; run mới/đang chờ được reroute; pipeline hoàn tất; controller rejoin; evidence có timestamp/output và MTTR.

### 14.10 Câu hỏi phản biện mạnh

**Hỏi: Bạn vibe-code, làm sao chứng minh bạn hiểu code?**

Đáp mẫu:

> “Em có dùng AI để tăng tốc tạo mã, nhưng em quản lý chất lượng bằng domain model, ADR, OpenAPI, state machine, pinned dependency, automated test và release gate chống false-green. Em có thể đi từ một thao tác UI qua HTTP, domain transition, persistence, Jenkins/Temporal đến runtime và chỉ rõ phần nào chưa nối thật. Em không coi code do AI sinh là bằng chứng; test và runtime evidence mới là bằng chứng.”

**Hỏi: Điểm yếu lớn nhất hiện tại là gì?**

Đáp: Integration seam chưa khép kín trên Ubuntu: API → router/Jenkins → evidence → Temporal → runtime → event/DORA. Code từng phần đã có, nhưng acceptance phụ thuộc chứng minh toàn chuỗi và persistence/concurrency.

**Hỏi: Nếu chỉ có một tuần, ưu tiên gì?**

Đáp: Chọn một vertical slice container chạy thật trước, gồm Postgres, Jenkins ephemeral, digest/evidence, Temporal, Docker deploy/health/rollback. Sau khi pattern ổn mới nhân sang Kubernetes/Systemd; song song giữ release gate trung thực.

**Hỏi: Quyết định nào dễ thay đổi nhất?**

Đáp: Portal có thể thay bằng Backstage client, runtime adapter có thể thay implementation, registry/object store có thể đổi. Domain/state/policy contract phải ổn định hơn. Đó là lợi ích của seam và adapter.

**Hỏi: Nếu bỏ Temporal thì sao?**

Đáp: Demo ngắn vẫn có thể gọi deploy trực tiếp, nhưng approval lâu, retry, restart recovery và compensation sẽ phải tự viết. ADR-003 cố ý chỉ dùng Temporal ở ranh giới có lợi ích rõ.

## 15. Checklist kiến thức cá nhân trước khi trình bày

Bạn nên tự trả lời trôi chảy, không nhìn tài liệu, các điểm sau:

- Nói được bài toán và pitch 60 giây.
- Vẽ được sơ đồ Portal → API/domain → Jenkins → artifact → Temporal → runtime.
- Giải thích vì sao Jenkins CI, Temporal CD.
- Kể đúng ba aggregate và hai state machine.
- Giải thích idempotency, correlation và immutable digest bằng ví dụ.
- Nói đúng bốn DORA metric và nguồn event.
- Giải thích SBOM/scan/sign khác nhau.
- Giải thích ephemeral agent và vì sao không mount Docker socket.
- Mô tả rollback Docker/Kubernetes/Systemd.
- Nói rõ 11 ready/10 blocked, không đánh đồng static test với E2E.
- Chỉ ra ba khoảng trống lớn: integration, persistence/concurrency, runtime evidence.
- Chạy được Windows test/build và đọc một error response.
- Mở `release-checklist.yaml` và giải thích vì sao gate blocked là điều tốt.

## 16. Tóm tắt mức độ sở hữu dự án

Muốn tự tin bảo vệ một dự án được tạo với sự hỗ trợ AI, bạn không cần thuộc lòng mọi dòng CSS hay lockfile. Bạn cần sở hữu năm tầng hiểu biết:

1. **Why:** vấn đề nào đang giải quyết và tiêu chí thành công.
2. **What:** các domain object, state, policy và output.
3. **How:** request đi qua file/lớp nào, side effect xảy ra ở đâu.
4. **Proof:** test/evidence nào chứng minh claim.
5. **Limits:** phần nào chưa làm, rủi ro gì và kế hoạch khép kín ra sao.

Với trạng thái hiện tại, bạn có thể trình bày tự tin rằng phần thiết kế và Windows reference đã được hoàn thiện có kiểm định. Bạn chưa nên nói “nền tảng đã hoàn tất” cho tới khi 10 gate Ubuntu được chuyển sang ready bằng evidence chạy thật.
