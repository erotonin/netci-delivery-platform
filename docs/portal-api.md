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

`POST /systems` tạo system. `POST /systems/{systemId}/modules` tạo module đồng thời tạo application delivery record theo pipeline template/runtime đã chọn. `POST /modules/{moduleId}/pipeline-runs` là command Portal-level để trigger pipeline; backend resolve `moduleId → applicationId` rồi gọi delivery application layer.

`POST /production-requests/{requestId}/approve` lưu actor/comment, thay đổi approval state và phải tạo audit event. Khi deployment thật đã tồn tại, command này phải tiếp tục gọi deployment approval của delivery domain thay vì chỉ đổi trạng thái read model.

## Persistence rule

Trong local source-only mode, read model có fallback in-memory để test deterministic. Khi `DATABASE_URL` tồn tại, service bootstrap các bảng `systems`, `modules`, `release_versions`, `production_requests`, load projection từ PostgreSQL và persist các create/approval command. Đây là compatibility path; trước khi mentor nghiệm thu runtime, cần kiểm tra migration/schema trên container Postgres và thay phần swallow lỗi persistence bằng health/error telemetry rõ ràng.

## UI states

Mọi query cần có loading, empty, error và stale-data state. Mọi command cần gửi `Idempotency-Key`, `X-Correlation-Id`, hiển thị kết quả thành công/thất bại và giữ correlation id trong audit/log. Dữ liệu hiển thị trong dashboard phải có `asOf` hoặc thời gian truy vấn khi chuyển sang production read model.
