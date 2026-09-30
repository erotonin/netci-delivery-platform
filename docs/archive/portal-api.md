# netCI Release Portal API

Custom Portal dùng netCI API làm lớp duy nhất để đọc dữ liệu và thực hiện command. Frontend không truy cập Jenkins, Temporal, database hoặc runtime target trực tiếp.

## Resource hierarchy

```text
System
└── Module
    ├── Application / delivery contract
    ├── Pipeline runs
    ├── Releases / artifact evidence
    ├── Deployments: dev, staging, prod
    ├── DORA metrics
    └── Production approval requests
```

`System` là product/service grouping dùng cho navigation và dashboard. `Module` là deployable component; module giữ `applicationId` để nối sang delivery domain hiện tại. Vì vậy có thể giữ các state machine `PipelineRun` và `Deployment` hiện có, trong khi UI dùng hierarchy phù hợp với Release Portal.

## Read models

`GET /portal/dashboard` trả KPI tổng hợp, series pipeline theo 7 ngày và system activity. `GET /systems` trả danh sách system; `GET /systems/{systemId}` trả system detail kèm module cards. `GET /modules/{moduleId}/overview` trả merge request checks, CD deployments, releases và trends. `GET /modules/{moduleId}/pipeline-runs`, `/versions` và `/dora` phục vụ các tab tương ứng.

Các response read model có thể materialize từ PostgreSQL query hoặc projection worker. API không nên bắt frontend tự join nhiều nguồn Jenkins/Temporal/registry vì điều đó làm lộ integration boundary và tạo trạng thái không nhất quán.

## Commands

`POST /systems` tạo system. `POST /systems/{systemId}/modules` tạo module đồng thời tạo application delivery record theo pipeline template/runtime đã chọn và lưu `deploymentEnvironments` (environment, runtime, server/task hoặc kubeconfig reference/namespace). Mọi environment của một application dùng chung runtime; Docker/Systemd bắt buộc có server, Kubernetes bắt buộc có kubeconfig reference và namespace. `POST /modules/{moduleId}/pipeline-runs` là command Portal-level để trigger pipeline; backend resolve `moduleId → applicationId` rồi gọi delivery application layer.

`POST /production-requests/{requestId}/approve` tạo một production promotion run và deployment thật từ pipeline run/digest/evidence của version, ghi liên kết `deploymentId`, rồi gọi delivery approval. Temporal hoặc callback mode hoàn tất deployment và đồng bộ request sang `succeeded`/`blocked`. Hiện API chỉ nhận đúng một module; multi-module bị từ chối rõ cho tới khi có coordinator đảm bảo deployment order.

## Persistence rule

In-memory mode chỉ là seam phục vụ unit test/local source-only; nó không sinh dữ liệu mẫu nếu `NETCI_DEMO_DATA` không được bật rõ ràng. Khi `DATABASE_URL` tồn tại, PostgreSQL là canonical tại thời điểm request: `PortalService` (trước đây `PortalReadModel`) đọc `systems`, `modules`, `release_versions`, `production_requests` trực tiếp từ database trong transaction của chính request, không load projection vào RAM lúc khởi động. Nhờ vậy nhiều API replica thấy cùng một state ngay lập tức mà không cần restart. Mutation Portal fail closed nếu PostgreSQL lỗi (`503 PERSISTENCE_UNAVAILABLE`, kể cả trên đường đọc); `/healthz` công bố trạng thái persistence. Onboarding module ghi application + module + idempotency record trong **một** transaction, nên retry sau network timeout trả về đúng resource cũ thay vì `MODULE_EXISTS`. Xem [ADR-014](decisions/ADR-014-postgresql-canonical-state.md). Production phải cấu hình PostgreSQL và chạy migration/restart/recovery gate trên database thật.

Module lưu `pipelineConfig` gồm runner routing label, branching strategy và cấu hình từng pipeline. Target host/namespace/kubeconfig reference do server bind vào run; các field `tasks/taskSettings` cũ chỉ được đọc để tương thích và không bao giờ được thực thi. Deploy, health và rollback chỉ chạy playbook đã review trong Git. Production request hiện hỗ trợ đúng một module; version bất biến, lịch có timezone, rollback strategy và automation-test gate đều được backend cưỡng chế. Pipeline run lưu `parameters.portalPipeline` để không trộn lịch sử của các pipeline dùng chung environment.

## UI states

Mọi query cần có loading, empty, error và stale-data state. Mọi command cần gửi `Idempotency-Key`, `X-Correlation-Id`, hiển thị kết quả thành công/thất bại và giữ correlation id trong audit/log. Dữ liệu hiển thị trong dashboard phải có `asOf` hoặc thời gian truy vấn khi chuyển sang production read model.

## UI reference contracts

Các màn hình mới dùng ba contract bổ sung để giữ đúng luồng của Release Portal:

- `GET /dcim/services?query={nameOrCode}`: tra cứu service khi tạo System.
- `GET /dcim/modules?systemId={systemId}`: lấy module ứng viên cho wizard New Module.
- `GET /dcim/servers?systemId={systemId}&moduleId={moduleId}`: lấy target server thật cho wizard từ DCIM.
- `GET /servers`: read-only projection của các runtime target đã được lưu trong cấu hình module; endpoint này không tự nhận là inventory/health source.
- `POST /modules/{moduleId}/versions`: đăng ký version; `pipelineRunId` và `artifactDigest` phải đi cùng nhau để version đủ điều kiện promote production. API kiểm tra run đã `succeeded`, digest khớp và security evidence là `allow`.
- `POST /modules/{moduleId}/versions/{tag}/ci-report`: pipeline đẩy coverage, automation test, SAST, vulnerability counts và commit SHA vào version đã đăng ký.

Endpoint CI report, callback `POST /pipeline-runs/{id}/ci-result` và callback `POST /deployments/{id}/result` đều yêu cầu `Authorization: Bearer <pipeline-api-key>`. Key đọc từ `NETCI_PIPELINE_API_KEY`; giá trị mặc định chỉ phục vụ local development và phải thay bằng secret manager khi triển khai thật.

DCIM dùng HTTP adapter khi có `NETCI_DCIM_BASE_URL`. Contract upstream là `GET /services?query=...`, `GET /systems/{id}/modules` và `GET /systems/{id}/servers?moduleId=...`; mỗi response chứa mảng `items`. Khi chưa cấu hình, API trả `status: not_configured` cùng mảng rỗng; không sinh service/server thay thế. `/servers` chỉ chiếu các runtime target đã lưu trong module và dùng trạng thái `unknown` khi chưa có health provider.
