# Quickstart

Tài liệu này tách rõ hai mục tiêu: phát triển phần portable trên Windows và nghiệm thu runtime trên Ubuntu 24.04.

## 1. Windows portable development

### Prerequisites

- Windows 10/11.
- Git 2.40+.
- Python 3.11+.
- Node.js 20.19+ và npm 10+.

Kiểm tra máy:

```powershell
py -3 scripts/doctor.py --profile windows
```

### Cài dependency

Từ repository root:

```powershell
py -3 -m pip install -r backend/requirements.txt
npm --prefix frontend ci
```

### Chạy gate portable

```powershell
py -3 scripts/validate_release.py --profile windows --execute
```

Command trả non-zero ngay khi thiếu tool/file, schema sai, test fail hoặc frontend không build. Nó không chạy Docker/kind/KVM và không tuyên bố Linux E2E đã hoàn thành.

### Chạy API và Portal

Terminal 1:

```powershell
Set-Location backend
py -3 -m uvicorn app.main:app --reload --port 8000
```

Terminal 2, từ repository root:

```powershell
npm --prefix frontend run dev
```

Mở `http://localhost:5173`. Portal gọi `/api/*`; Vite bỏ prefix và proxy tới `http://127.0.0.1:8000`, vì vậy không cần mở rộng CORS cho development.

Kết quả smoke test hiện tại:

1. Màn hình đăng nhập bảo vệ toàn bộ Portal; local preview chấp nhận tài khoản demo nhưng không lưu mật khẩu.
2. Dashboard, Systems, Servers, Production Requests, module overview, pipeline, versions, DORA và settings đều có route và trạng thái tương tác đầy đủ.
3. Wizard New Module tải DCIM candidates, lưu runner/branching/pipeline stages, deployment environment, server và health-check settings qua `POST /systems/{systemId}/modules`.
4. Pipeline trigger, version registration và production request/approve/reject gọi netCI API; loading, validation và structured API error được hiển thị trong Portal.
5. `npm --prefix frontend test` chạy component tests cho session guard, login/logout và settings interaction.

Khi không có `DATABASE_URL`, backend lưu dữ liệu trong memory và restart process sẽ mất dữ liệu tạo thêm. Khi có PostgreSQL, Portal mutation fail closed nếu persistence lỗi; nghiệm thu restart/recovery vẫn thuộc gate Ubuntu.

## 2. Backstage experiment

1. Merge `backstage/app-config.example.yaml` vào `app-config.yaml` của một Backstage instance.
2. Đảm bảo backend đã đăng ký `@backstage/plugin-proxy-backend`.
3. Register `backstage/netci-template.yaml` vào Software Catalog.
4. Chạy template và chọn cặp pipeline template/runtime tương ứng.

Backstage dùng `/api/proxy/netci`; output lấy `body.id` mà API thực sự trả về. Integration chỉ được chuyển sang `ready` sau khi chạy trên Backstage thật.

## 3. Ubuntu 24.04 handoff

Clone đúng commit thay vì copy thư mục làm việc. Sau đó:

```bash
make doctor
make frontend-install
make validate
make test
make compose-config
```

Các bước runtime dự kiến:

```bash
make up
make kind-up
make registry-connect
make jenkins-rebuild
make release-ubuntu
```

Ở trạng thái hiện tại, `make release-ubuntu` liệt kê required gate bị `blocked` và exit non-zero. Với từng hạng mục, chỉ sửa `state: blocked` thành `state: ready` sau khi target tương ứng đã có runner thật và tự assert output/evidence.

## 4. Evidence tối thiểu

Mỗi run nghiệm thu phải lưu:

- commit SHA và command;
- thời điểm bắt đầu/kết thúc và exit code;
- application, pipeline run và deployment ID;
- artifact digest bất biến;
- raw log/JSON cho policy, deploy, health và rollback;
- môi trường thực thi và kết luận.

Không dùng credential mặc định ngoài local. Không commit `.env`, private key, token, Jenkins credential hoặc kubeconfig.
