# netCI acceptance scenarios

## E2E-01 — Container

1. Push commit lên GitHub hoặc bare Git.
2. Tạo `container-ci-cd-v1` application từ Portal.
3. Router chọn Jenkins A hoặc B.
4. Jenkins tạo ephemeral agent riêng.
5. Chạy test, build, SBOM, Trivy, Cosign và publish digest.
6. netCI/Temporal yêu cầu approval nếu target là staging/prod.
7. Ansible deploy Docker/Compose lên Docker VM.
8. Health check thành công và audit event được ghi.

## E2E-02 — Kubernetes

1. Dùng cùng artifact digest đã qua security gate.
2. Chọn environment `dev` hoặc `staging`.
3. Ansible gọi `kubernetes.core.helm` với chart version và values đã pin.
4. Helm release được wait đến ready.
5. Readiness/liveness health check thành công.
6. Tạo production request và chứng minh trạng thái chờ approval.
7. Sau approval, promote cùng digest; không rebuild image.

## E2E-03 — Systemd

1. Build Go binary `linux/amd64`.
2. Lưu artifact version vào store.
3. Ansible SSH vào Systemd VM.
4. Copy binary vào release directory.
5. Cập nhật symlink `current`, install unit, daemon-reload và restart.
6. Kiểm tra `/healthz` và `journalctl`.
7. Deploy version lỗi có chủ ý, health check fail.
8. Rollback symlink về version trước và chứng minh service healthy.

## ISO-01 — Ephemeral agent

- Mỗi build có agent/pod/workspace riêng.
- Hai project chạy đồng thời không đọc workspace của nhau.
- Workspace và agent bị cleanup sau success/failure/cancel.
- Cache nằm ngoài workspace và không chứa secret.

## SEC-01 — Supply chain deny

- Image có CVE HIGH/CRITICAL vượt policy phải bị chặn.
- Artifact không có SBOM phải bị chặn.
- Artifact chưa verify Cosign signature phải bị chặn.
- Deploy chỉ nhận immutable digest, không nhận tag mutable làm identity.

## REBUILD-01 — Jenkins rebuild

- Xóa controller A.
- Dựng lại image/volume từ Git config và JCasC.
- Plugin version, shared library và seed job xuất hiện lại.
- Không click cấu hình thủ công trong Jenkins UI.

## HA-01 — Controller failure drill

- Đăng ký A/B với health, queue và capacity.
- Dừng A.
- Build mới không được route vào A.
- Router chuyển build mới sang B.
- Ghi detection time, routing time, completion time và MTTR.
- Khi A quay lại, verify config drift trước khi nhận build.

## DORA-01 — Five metrics

Event model phải đủ cho Change Lead Time, Deployment Frequency, Failed Deployment Recovery Time, Change Fail Rate và Deployment Rework Rate. Mỗi event gắn `applicationId`, `commitSha`, `artifactDigest`, `environment`, `deploymentId`, `actor` và timestamp.
