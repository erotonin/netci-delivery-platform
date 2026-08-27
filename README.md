# netCI Delivery Platform

Local reference implementation của một delivery platform API-first: developer khai báo application từ Custom Portal hoặc Backstage, Jenkins thực hiện CI trên agent ephemeral, còn netCI/Temporal điều phối CD tới Docker, Kubernetes hoặc Systemd.

> Trạng thái hiện tại: phần contract, domain skeleton, Portal và static gate có thể phát triển trên Windows. Các tuyên bố về Docker/kind/KVM/Jenkins/Temporal/Ansible/Syft/Trivy/Cosign chỉ được công nhận sau khi có evidence chạy thật trên Ubuntu 24.04.

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
make doctor
make validate
make test
make compose-config
make release-ubuntu
```

`make release-ubuntu` cố ý fail nếu một required gate vẫn có trạng thái `blocked` trong `release-checklist.yaml`. Không đổi gate sang `ready` cho đến khi command chạy thật và kiểm tra evidence thật.

## Boundary

- Portal và Backstage chỉ gọi netCI API; không gọi Jenkins trực tiếp.
- Jenkins sở hữu checkout/test/build/SBOM/scan/sign/publish, không sở hữu application/deployment identity.
- netCI sở hữu policy, approval, audit, promotion, deploy, health check và rollback.
- Staging và production dùng cùng artifact digest; không rebuild khi promote.
- Core domain không import SDK/CLI của Jenkins, Temporal, Docker, Helm hoặc Systemd.

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
