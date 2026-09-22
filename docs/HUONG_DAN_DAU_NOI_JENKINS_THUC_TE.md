# Hướng Dẫn Đấu Nối netCI Với Cụm Jenkins Hiện Có (VTNet)

Tài liệu này hướng dẫn chi tiết quy trình 4 bước để triển khai nền tảng **netCI Delivery Platform** lên cụm Kubernetes và đấu nối trực tiếp vào hệ thống **Jenkins hiện có** của VTNet.

---

## Kiến Trúc Tổng Thể

```
+-----------------------------------------------------------------------------------+
| VTNet Infrastructure                                                              |
|                                                                                   |
|  [Trình duyệt Dev]                                                                |
|         │                                                                         |
|         ▼                                                                         |
|  [Ingress NGINX]                                                                  |
|         │                                                                         |
|   ┌─────┴──────────────────┐                                                      |
|   ▼                        ▼                                                      |
| [netCI Frontend]     [netCI Backend] ─────────┐                                   |
| (React Web Portal)   (FastAPI Core)           │ 2. REST API Trigger               |
|                            │                  ▼ (Auto-creates Job & injects params)|
|                            │          [Jenkins Controller VTNet]                  |
|                            │          (Nạp Shared Library: netciPipeline)         |
|                            │                  │                                   |
|                            │                  │ 3. Gọi K8s API sinh Pod           |
|                            │                  ▼                                   |
|                            │          [Ephemeral Pod Agent]                       |
|                            │          (Namespace: netci-build)                    |
|                            │                  │                                   |
|                            │ 4. Callback      ▼                                   |
|                            └────────── [Test -> SBOM -> Scan -> Sign -> Push]     |
|                                               │                                   |
|                                               ▼                                   |
|                                       [Harbor Registry VTNet]                     |
+-----------------------------------------------------------------------------------+
```

---

## BƯỚC 1: Khởi Tạo Quyền Hạn Trên Cụm Kubernetes (RBAC)

Để Jenkins có quyền tự động tạo và xóa các Ephemeral Pod khi chạy build, cần tạo Namespace và ServiceAccount cho Jenkins trên cụm K8s.

1. **Áp dụng file manifest RBAC có sẵn:**
   ```bash
   kubectl apply -f deploy/k8s/netci-platform/01-rbac.yaml
   ```
   *File này tạo ra:*
   * Namespace: `netci-build` (cô lập toàn bộ tiến trình build, không ảnh hưởng ứng dụng khác).
   * ServiceAccount: `jenkins-controller` và `jenkins-agent`.
   * Role & RoleBinding: Cấp đúng quyền tối thiểu (`create`, `get`, `list`, `watch`, `delete`) trên `pods` và `persistentvolumeclaims`.

2. **Lấy Token xác thực của `jenkins-controller`:**
   ```bash
   # Nếu K8s >= 1.24 (Tạo ServiceAccount token):
   kubectl create token jenkins-controller -n netci-build --duration=87600h
   ```
   *(Lưu lại chuỗi Token này để dùng ở Bước 2).*

---

## BƯỚC 2: Cấu Hình Trên Jenkins Của VTNet

Truy cập vào giao diện web Jenkins của VTNet (ví dụ `http://jenkins.vtnet.viettel.vn:8080`) bằng tài khoản Quản trị.

### 2.1. Cấu hình Kubernetes Cloud (để Jenkins sinh Pod trên K8s)
1. Vào **Manage Jenkins** → **Clouds** (hoặc **Nodes & Clouds**) → Chọn **Add a new cloud** → **Kubernetes**.
2. Điền các thông số:
   * **Name**: `netci-k8s` (hoặc tên bất kỳ).
   * **Kubernetes URL**: `https://<ip-k8s-api>:6443` (Địa chỉ API Server nội bộ của K8s).
   * **Kubernetes server certificate key**: (Copy nội dung CA certificate nếu dùng HTTPS nội bộ, hoặc tích chọn *Disable https certificate check* nếu trong mạng cô lập).
   * **Kubernetes Namespace**: `netci-build`
   * **Credentials**: Bấm *Add* → Chọn loại *Secret text* → Dán chuỗi **Token** đã lấy ở Bước 1 vào.
   * **Jenkins URL**: `http://<ip-hoac-domain-jenkins>:8080`
   * **WebSocket**: Tích chọn `true` (khuyên dùng để kết nối Agent mượt qua Ingress/Firewall mà không cần mở port 50000).
3. Bấm **Test Connection** → Nhìn thấy thông báo **"Connection test successful"** màu xanh là hoàn tất.

### 2.2. Nạp Shared Library của netCI
1. Vào **Manage Jenkins** → **System** → Tìm mục **Global Pipeline Libraries** → Bấm **Add**.
2. Điền thông tin:
   * **Name**: `netci-shared-library` (bắt buộc đúng tên này).
   * **Default version**: `main` (hoặc commit/tag mong muốn).
   * **Retrieval method**: Chọn *Modern SCM* → *Git*.
   * **Project Repository**: URL Git repo chứa Shared Library của netCI (thư mục `jenkins/shared-library`).
   * **Credentials**: Tài khoản Git đọc repo (nếu repo private).
3. Bấm **Save**.

### 2.3. Tạo API Token cho netCI kết nối Jenkins
1. Đăng nhập tài khoản dùng cho netCI (ví dụ tài khoản `netci-sa`).
2. Vào **Manage Jenkins** → **Users** → Chọn tài khoản `netci-sa` → Bấm **Configure**.
3. Tại mục **API Token**, bấm **Add new Token** → Đặt tên `netci-platform` → Bấm **Generate**.
4. Copy chuỗi API Token (ví dụ: `11a1b2c3d4e5f60718293a4b5c6d7e8f90`).

---

## BƯỚC 3: Cấu Hình & Triển Khai netCI Platform

Có 2 cách triển khai: bằng **Helm Chart** (khuyên dùng cho VTNet) hoặc bằng **Kustomize**.

### Cách 1: Triển khai bằng Helm Chart (Khuyên dùng)

1. Mở file [deploy/helm/netci-platform/values.yaml](file:///home/deployer/netci-delivery-platform/deploy/helm/netci-platform/values.yaml), chỉnh sửa thông tin hạ tầng thực tế:
   ```yaml
   jenkins:
     controllerUrl: "http://jenkins.vtnet.viettel.vn:8080"  # URL Jenkins VTNet
     username: "netci-sa"                                   # User Jenkins
     apiToken: "11a1b2c3d4e5f60718293a4b5c6d7e8f90"        # Token vừa tạo ở Bước 2.3
     agentLabel: "netci-ephemeral"
     buildNamespace: "netci-build"

   registry:
     pushHost: "harbor.vtnet.viettel.vn"                    # Harbor nội bộ
     pullHost: "harbor.vtnet.viettel.vn"
   ```

2. Chạy lệnh cài đặt Helm lên cụm:
   ```bash
   helm upgrade --install netci deploy/helm/netci-platform \
     --namespace netci \
     --create-namespace \
     -f deploy/helm/netci-platform/values.yaml
   ```

### Cách 2: Triển khai bằng Kustomize Manifests

1. Cập nhật URL Jenkins trong file [deploy/k8s/netci-platform/02-configmap.yaml](file:///home/deployer/netci-delivery-platform/deploy/k8s/netci-platform/02-configmap.yaml):
   ```yaml
   JENKINS_A_URL: "http://jenkins.vtnet.viettel.vn:8080"
   ```
2. Cập nhật mật khẩu/token trong file [deploy/k8s/netci-platform/03-secrets.example.yaml](file:///home/deployer/netci-delivery-platform/deploy/k8s/netci-platform/03-secrets.example.yaml) và đổi tên thành `03-secrets.yaml`.
3. Áp dụng toàn bộ manifests bằng 1 lệnh duy nhất:
   ```bash
   kubectl apply -k deploy/k8s/netci-platform/
   ```

---

## BƯỚC 4: Kiểm Tra Thông Suốt & Chạy Thử Pipeline Đầu Tiên

1. **Kiểm tra trạng thái các Pods của netCI:**
   ```bash
   kubectl get pods -n netci
   ```
   *Kết quả mong đợi:*
   * `netci-backend-...` (Running 2/2)
   * `netci-frontend-...` (Running 2/2)
   * `netci-worker-...` (Running 2/2)

2. **Truy cập Web Portal của netCI:**
   * Mở trình duyệt vào domain: `http://netci.vtnet.viettel.vn` (hoặc IP NodePort / Ingress).
3. **Chạy thử Pipeline cho 1 ứng dụng:**
   * Vào giao diện netCI → Chọn ứng dụng (ví dụ `payment-gateway`).
   * Bấm **Run Pipeline**.
4. **Quan sát luồng chạy thực tế:**
   * Trên giao diện netCI: Các stage lần lượt chuyển từ `running` sang `succeeded` (xanh lá).
   * Trên Jenkins VTNet: Job `netci-payment-gateway` tự động xuất hiện và chạy bản build mới.
   * Trên cụm Kubernetes: Kiểm tra lệnh `kubectl get pods -n netci-build` sẽ thấy Pod Ephemeral sinh ra, chạy test/build/scan rồi tự động biến mất khi hoàn thành!

---

## PHỤ LỤC: Các Câu Hỏi Thường Gặp (Troubleshooting)

### Q1: Nếu Jenkins của VTNet dùng chứng chỉ SSL tự ký (Self-signed Certificate)?
* **Giải pháp:** Trong file `02-configmap.yaml` hoặc `values.yaml`, thêm biến môi trường:
  ```yaml
  PYTHONHTTPSVERIFY: "0"  # Hoặc mount file Root CA của Viettel vào /etc/ssl/certs
  ```

### Q2: Nếu Jenkins của VTNet chạy Agent trên máy ảo Linux tĩnh (VM), không có Kubernetes?
* **Giải pháp:** Trong `values.yaml`, chỉ cần đặt `agentLabel: "linux-agent"` (nhãn của máy ảo Jenkins). Thư viện `netciPipeline.groovy` sẽ tự động chuyển sang chế độ chạy trực tiếp trên máy ảo mà không gọi tới Kubernetes.

### Q3: Nếu Harbor Registry của VTNet là HTTP (Insecure Registry)?
* **Giải pháp:** Trên các node worker của Kubernetes, thêm cấu hình vào `/etc/docker/daemon.json` hoặc `/etc/containerd/config.toml`:
  ```json
  {
    "insecure-registries": ["harbor.vtnet.viettel.vn"]
  }
  ```
