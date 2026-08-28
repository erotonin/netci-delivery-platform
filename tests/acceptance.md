# netCI acceptance scenarios

Every scenario below has a runner. Each writes `evidence/<gate>.json` with the commands
it ran, their timings and exit codes, and one line per assertion — so a green result can be
re-read rather than trusted.

| Scenario | Runner | Evidence |
|---|---|---|
| E2E-01 Container | `make e2e-container` | `evidence/e2e-container.json` |
| E2E-02 Kubernetes | `make e2e-kubernetes` | `evidence/e2e-kubernetes.json` |
| E2E-03 Systemd | `make e2e-systemd` | `evidence/e2e-systemd.json` |
| ISO-01 Ephemeral agent | `make kind-up` (the guarantees) + `make jenkins-ci-loop` (a real build in a throwaway pod) | `evidence/kind-cluster.json`, `evidence/jenkins-ci-loop.json` |
| SEC-01 Supply-chain deny | `make security-test` | `evidence/security-gate.json` |
| REBUILD-01 Jenkins rebuild | `make jenkins-rebuild-gate` | `evidence/jenkins-rebuild.json` |
| HA-01 Controller failure drill | `make failure-drill` | `evidence/failure-drill.json` |
| BENCH-01 Isolation cost | `make benchmark` | `evidence/benchmark.json`, `evidence/benchmarks/report.json` |
| BACKSTAGE-01 Second portal | `make backstage-test` | `evidence/backstage-integration.json` |
| DORA-01 Four metrics | `make dora-dashboard` | `evidence/dora-dashboard.json` |

The first five need `make lab-up` and `make kind-up`. REBUILD-01, HA-01, BENCH-01 and the
CI half of ISO-01 additionally need `bash scripts/jenkins_lab.sh up`; BACKSTAGE-01 needs
`bash scripts/backstage_lab.sh up`. `make gates-all` runs the lot.

Each runner exits non-zero on the first failed assertion and writes its evidence either
way, so a failed gate is as readable as a passing one.

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

- Image có CVE HIGH/CRITICAL **có bản vá** vượt policy phải bị chặn. Finding chưa có bản vá vẫn được ghi vào evidence nhưng không chặn — lý do trong [security model](../docs/security-model.md).
- Artifact không có SBOM phải bị chặn.
- Artifact chưa verify Cosign signature phải bị chặn.
- Evidence thuộc digest khác không được phép cấp phép cho artifact đang deploy.
- Deploy chỉ nhận immutable digest, không nhận tag mutable làm identity.
- Khi `NETCI_REQUIRE_SECURITY_EVIDENCE=true`, artifact hoàn toàn không có evidence cũng bị chặn.

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

## DORA-01 - Four metrics

Event model phải đủ cho Deployment Frequency, Lead Time for Changes, Change Failure Rate và Time to Restore Service. Mỗi event gắn `applicationId`, `commitSha`, `artifactDigest`, `environment`, `deploymentId`, `actor` và timestamp.

Gate không chỉ đọc dashboard: nó tính lại cả bốn metric một cách độc lập từ `GET /delivery-events` rồi so sánh. Metric nào không tái lập được từ source event thì gate fail. Nó cũng assert recovery gắn đúng `deploymentId` của lần fail mà nó khôi phục, chứ không suy ra theo thứ tự thời gian.
