# netCI troubleshooting runbook

## Khởi động theo profile

| Profile | Bật | Dùng cho |
|---|---|---|
| `core` | API, PostgreSQL, Registry, MinIO, Jenkins A | Phát triển domain/CI |
| `k8s` | core + kind + Jenkins ephemeral agent | Kubernetes E2E |
| `container` | core + Docker VM | Docker/Ansible E2E |
| `systemd` | core + Systemd VM | Systemd/Ansible E2E |
| `full` | core + kind + Jenkins A/B + Temporalite | Demo tổng hợp, chạy tuần tự |

## Lỗi thường gặp

### API không lên

Kiểm tra port 8000, log container và health dependency. Không xóa database ngay; trước hết lưu evidence command/log.

```bash
docker compose ps
curl -fsS http://localhost:8000/healthz
```

### Jenkins JCasC không nạp

Kiểm tra `CASC_JENKINS_CONFIG`, file YAML có mount read-only, plugin JCasC đã cài và không có plaintext secret trong Git.

```bash
docker compose logs jenkins-a
 docker compose exec jenkins-a printenv CASC_JENKINS_CONFIG
```

### Ephemeral agent không tạo được

Kiểm tra Kubernetes context, namespace `netci-build`, service account/RBAC, image pull và resource limit. Không chuyển sang shared agent để che lỗi; ghi failure evidence trước.

### Helm deploy thất bại

Kiểm tra chart lint, namespace, image digest, kube context và readiness probe. Dùng `atomic: true` và `wait: true`; không dùng `replace: true` trong production path.

```bash
helm lint deploy/helm/sample-kubernetes-app
kubectl -n staging get pods,events
helm -n staging status hello-kubernetes
```

### Ansible SSH thất bại

Kiểm tra IP inventory, SSH key, host key, Python trên target và quyền sudo. Dùng `ansible-inventory --graph` và `ansible all -m ping` trước khi deploy.

### Systemd service không healthy

Kiểm tra binary architecture, permission, unit file, symlink `current`, port 18080 và journal.

```bash
systemctl status hello-systemd
journalctl -u hello-systemd -n 100 --no-pager
curl -fsS http://127.0.0.1:18080/healthz
```

### Security gate chặn artifact

Không bypass policy. Kiểm tra digest có immutable không, SBOM có tồn tại, Trivy report, signature và identity/key reference. Chỉ cho phép exception khi có audit record và approval đúng role.

### Build chậm sau khi dùng ephemeral agent

Phân tách queue, provisioning, cache restore, build và cleanup. Không kết luận chỉ từ tổng thời gian. So sánh cùng commit, cùng agent resource, cùng image và cùng cache policy.

### Controller A down

Router phải ngừng route build mới vào A, chọn B theo health/queue/capacity và ghi detection/reroute/completion timestamps. Build đang chạy trên A không được tự ý migrate nếu chưa có checkpoint/idempotency.
