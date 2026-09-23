# Đấu nối netCI với Jenkins đang có

Hướng dẫn đầy đủ và đã được kiểm chứng: **[DEPLOY-KUBERNETES.md](DEPLOY-KUBERNETES.md)**.

Bản trước của tài liệu này mô tả chart 1.0.0 và bộ manifest `deploy/k8s/netci-platform`.
Làm theo nó thì netCI không khởi động được (thiếu `NETCI_AUTH_MODE`, `NETCI_CD_MODE`, dùng tên
biến mà code không đọc, không có migration), và kể cả khi khởi động, netCI không build được
repo ứng dụng thật (ADR-042). Tài liệu cũ đã được thay, không sửa vá.

Tóm tắt bốn bước:

1. **Kiểm tra Jenkins** bằng `scripts/jenkins_preflight.py` — plugin, quyền của tài khoản dịch
   vụ, shared library (bản có `resources/netci/tooling`), credential `netci-cosign-key`, agent
   label. Script chỉ đọc, không thay đổi gì trên Jenkins.
2. **Build và push image**: `scripts/build_images.sh <registry>/netci <tag>`.
3. **Tạo Secret** `netci-app` và `netci-deploy-targets` với đúng tên key (DEPLOY-KUBERNETES.md mục 4).
4. **Cài**: `helm upgrade --install netci deploy/helm/netci-platform -f my-values.yaml`.
   Đổi Jenkins là đổi `jenkins.controllers[].url` và `username`, cộng token trong Secret.

Cần manifest tĩnh thay vì Helm trong change window: `helm template netci
deploy/helm/netci-platform -n netci-system -f my-values.yaml > netci.yaml`, rồi `kubectl apply
-f netci.yaml`. Kết quả đúng với chính values của bạn — không có bản sao thứ hai để lệch.
