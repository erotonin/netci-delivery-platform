# netCI Delivery Platform

Local reference implementation của một delivery platform API-first: developer khai báo application từ Custom Portal hoặc Backstage, Jenkins thực hiện CI trên agent ephemeral, còn netCI/Temporal điều phối CD tới Docker, Kubernetes hoặc Systemd.

> Trạng thái hiện tại: cả **11 acceptance gate** đều chạy thật trên Ubuntu 24.04 và tự ghi evidence — kind cluster, security gate (Syft/Trivy/Cosign), ba E2E runtime (Docker, Kubernetes, Systemd), DORA, CI loop qua Jenkins với agent ephemeral, rebuild controller từ JCasC, failure drill hai controller, benchmark ephemeral-vs-shared, và Backstage như một portal thứ hai. Không còn gate nào ở trạng thái `blocked`. `release-checklist.yaml` là nguồn sự thật, không phải README.

## Output cần bàn giao

Một bản hoàn chỉnh phải chứng minh được:

- Reproducible: controller, plugin và job được dựng lại từ Git/JCasC.
- Isolated: mỗi build có agent/workspace riêng và cleanup ở cả success/failure/cancel.
- Extensible: cùng domain contract deploy được qua Docker, Kubernetes và Systemd adapter.
- Governed: artifact bất biến có SBOM, scan, signature, approval, audit và rollback.
- Measurable: có source event, DORA metrics, benchmark và failure-drill evidence.

Luồng chính:

```text
Portal / Backstage -> netCI API -> Jenkins Router -> Jenkins A/B
                                            -> ephemeral CI agent
                                            -> immutable artifact digest
                                            -> policy + approval
                                            -> Docker | Kubernetes | Systemd
```

## Gate nào đã chạy thật

Mỗi gate dưới đây chạy command thật trên hạ tầng thật và ghi `evidence/<gate>.json` gồm command, timestamp, exit code, output và từng assertion kèm verdict. Gate exit non-zero khi một assertion fail.

| Gate | Chứng minh điều gì |
|---|---|
| `make kind-up` | Cluster build có namespace, RBAC controller chỉ giới hạn ở pod trong `netci-build`, không service account nào tự mount token, không có cluster-admin |
| `make security-test` | Artifact đã ký + scan sạch được cho phép; ba trường hợp bị từ chối: có CVE HIGH/CRITICAL có bản vá, thiếu chữ ký, evidence thuộc digest khác |
| `make e2e-container` | Build → registry digest → SBOM/scan/sign → policy → approval → Ansible deploy → health → promote → rollback, với digest đang chạy được đọc lại từ service |
| `make e2e-kubernetes` | Cùng digest được promote lên kind qua Helm; image của pod được đọc lại từ cluster; rollback dùng Helm revision, không build lại |
| `make e2e-systemd` | Binary Go đã ký → unit systemd → symlink `current` → restart → health → rollback, kèm so khớp digest của binary đã cài |
| `make dora-dashboard` | Bốn metric được tính lại độc lập từ `/delivery-events` và so với dashboard; recovery gắn đúng deployment đã fail |

Ngoài ra `make test-durability` chạy trên PostgreSQL thật: khôi phục sau restart, event bền vững, idempotency sống qua restart, và hai process cùng ghi một transition thì chỉ một thắng.

## Những gì chạy được trên Windows

Windows là môi trường phát triển portable, không phải môi trường nghiệm thu runtime Linux.

```powershell
py -3 scripts/doctor.py --profile windows
py -3 scripts/validate_release.py --profile windows --execute
```

Gate Windows kiểm tra Git/Python/Node/npm, syntax và schema, catalog invariant, frontend component tests/typecheck/build, backend unit/contract tests, tài liệu và tính nhất quán của release checklist. Xem [QUICKSTART.md](QUICKSTART.md) để chạy API và Portal.

## Runtime target

| Runtime | Adapter | Target local |
|---|---|---|
| Docker/Compose | `DockerRuntimeAdapter` | Ubuntu VM riêng qua SSH/Ansible |
| Kubernetes | `KubernetesRuntimeAdapter` | kind; namespaces `dev`, `staging`, `prod` |
| Systemd | `SystemdRuntimeAdapter` | Ubuntu VM thật có systemd qua SSH/Ansible |

## Các lệnh chính trên Ubuntu

```bash
make doctor          # gồm cả giới hạn fs.inotify và Python lib mà Ansible cần
make validate
make test
make lab-up          # PostgreSQL đã migrate + registry + API
make kind-up
make gates           # security-test, ba E2E và dora-dashboard

# Các gate cần Jenkins thật: hai controller dựng từ JCasC, git server và shared agent.
bash scripts/jenkins_lab.sh up
source .netci-gate/jenkins/lab.env
make jenkins-ci-loop jenkins-rebuild-gate failure-drill benchmark

# Gate cuối cần một Backstage instance thật.
bash scripts/backstage_lab.sh up
make backstage-test

make gates-all       # tất cả, khi cả hai lab đã chạy
make release-ubuntu
```

Sau khi sửa shared library hoặc `jenkins/casc/*.yaml`, dùng `bash scripts/jenkins_lab.sh publish` để controller đang chạy đọc lại config — không cần dựng lại từ đầu.

`make release-ubuntu` cố ý fail nếu một required gate vẫn có trạng thái `blocked` trong `release-checklist.yaml`. Hiện không còn gate nào blocked. Không đổi gate sang `ready` cho đến khi command chạy thật và kiểm tra evidence thật.

Xem [QUICKSTART](QUICKSTART.md) cho lab trên host không có bridge network, và [troubleshooting](docs/troubleshooting.md) cho các ràng buộc host mà thông báo lỗi không nói ra.

## Boundary

- Portal và Backstage chỉ gọi netCI API; không gọi Jenkins trực tiếp.
- Jenkins sở hữu checkout/test/build/SBOM/scan/sign/publish, không sở hữu application/deployment identity.
- netCI sở hữu policy, approval, audit, promotion, deploy, health check và rollback.
- Staging và production dùng cùng artifact digest; không rebuild khi promote.
- Core domain không import SDK/CLI của Jenkins, Temporal, Docker, Helm hoặc Systemd.
- Engine thật nằm sau seam có thể cấu hình: `NETCI_CI_MODE` (`none`|`jenkins`) và `NETCI_CD_MODE` (`none`|`temporal`). Mặc định `none` nghĩa là netCI ghi nhận trạng thái và chờ callback đã xác thực — nó không bao giờ giả vờ đã chạy một build.
- State, source event, audit và log được ghi trong **cùng một transaction**, nên không thể có trường hợp trạng thái đã đổi nhưng DORA event bị mất.

## Tài liệu

- [Architecture](docs/architecture.md)
- [Domain model](docs/domain-model.md)
- [API contract](docs/api-contract.md)
- [State machine](docs/state-machine.md)
- [Security model](docs/security-model.md)
- [DORA metrics](docs/dora-metrics.md)
- [Assumptions](docs/assumptions.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture decisions](docs/decisions/ADR-001-system-boundary.md)

## Không được coi là evidence hoàn thành

Static validator, screenshot đơn lẻ, file `sample.json`, command chỉ `echo`, hoặc file được tạo sau khi đặt biến `*_READY` không chứng minh E2E. Evidence nghiệm thu phải có command, commit SHA, application/run/deployment ID, artifact digest, timestamps, raw log/JSON và kết luận pass/fail.
