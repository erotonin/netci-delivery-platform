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

## 3. Ubuntu 24.04: chạy các gate thật

Clone đúng commit thay vì copy thư mục làm việc.

### 3.1 Kiểm tra host và chạy gate portable

```bash
make doctor
make frontend-install
make validate
make test
```

`make doctor` kiểm tra cả hai thứ hay bị bỏ sót và rất khó chẩn đoán từ thông báo lỗi: giới hạn `fs.inotify` mà kind cần, và các Python library mà Ansible collection cần. Xem [troubleshooting](docs/troubleshooting.md) khi một trong hai fail.

### 3.2 Dựng lab tối thiểu

```bash
make lab-up
```

Lab gồm PostgreSQL (đã migrate), một OCI registry và netCI API. Trên host mà Docker daemon không có bridge/NAT — published port không hoạt động — dùng chế độ host network:

```bash
make lab-up NETCI_LAB_MODE=hostnet
```

Lệnh in ra đúng các biến môi trường cần export cho các gate.

### 3.3 Dựng build cluster

```bash
make kind-up
bash infra/kind/local-registry.sh          # REGISTRY_MODE=gateway nếu dùng hostnet
```

`make kind-up` không chỉ tạo cluster: nó assert namespace, phạm vi RBAC của controller, và việc không service account nào tự mount token — tức là các thuộc tính mà [ADR-007](docs/decisions/ADR-007-ephemeral-agent-isolation.md) tuyên bố.

### 3.4 Chạy các acceptance gate

```bash
make security-test      # Syft + Trivy + Cosign thật: 1 allow, 3 deny
make e2e-container      # source -> registry digest -> approval -> Docker -> health -> rollback
make e2e-kubernetes     # cùng digest promote lên kind qua Helm, rollback theo revision
make e2e-systemd        # binary đã ký -> systemd unit -> symlink flip -> rollback
make dora-dashboard     # 4 metric tính lại độc lập từ source event
make gates              # chạy tất cả những gate trên
```

Mỗi gate ghi `evidence/<gate>.json` gồm command, timestamp, exit code, output và **từng assertion kèm verdict**. Gate exit non-zero khi một assertion fail, nên nó không thể báo xanh mà không thực sự đạt.

Kiểm tra durability cần database thật:

```bash
export NETCI_TEST_DATABASE_URL='postgresql://netci:netci-local-only@127.0.0.1:55432/netci'
make test-durability
```

### 3.5 Release gate

```bash
make release-ubuntu
```

Lệnh này vẫn exit non-zero khi còn required gate ở trạng thái `blocked`. Hiện **không còn gate nào blocked** — cả 11 gate đều có runner thật. Năm gate cuối cần lab Jenkins (`bash scripts/jenkins_lab.sh up`) hoặc lab Backstage (`bash scripts/backstage_lab.sh up`) đang chạy, nếu không chúng sẽ fail vì không kết nối được chứ không âm thầm pass. Chỉ chuyển `blocked` sang `ready` sau khi có runner thật tự assert output.

## 4. Evidence tối thiểu

Mỗi run nghiệm thu phải lưu:

- commit SHA và command;
- thời điểm bắt đầu/kết thúc và exit code;
- application, pipeline run và deployment ID;
- artifact digest bất biến;
- raw log/JSON cho policy, deploy, health và rollback;
- môi trường thực thi và kết luận.

Các gate ở mục 3.4 đã sinh ra đúng bộ này tự động; không cần chụp màn hình để thay thế.

Không dùng credential mặc định ngoài local. Không commit `.env`, private key, token, Jenkins credential hoặc kubeconfig.
