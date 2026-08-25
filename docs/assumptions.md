# Assumptions và điểm thay thế

| Chủ đề | Prototype local | Khi tích hợp production |
|---|---|---|
| Kubernetes | kind | Viettel-managed Kubernetes/AI Platform |
| Jenkins topology | Hai controller cùng host, router mô phỏng multi-cluster | Nhiều cluster/failure domain thật |
| Authentication | Mock role `developer`, `reviewer`, `platform-admin` | SSO/RBAC nội bộ |
| Artifact store | Local Registry + MinIO | Registry/object storage nội bộ |
| Secrets | Environment variables/secret references, không commit secret | Secret manager nội bộ |
| Scanner/signing | Syft + Trivy + Cosign | Toolchain nội bộ nếu có |
| DORA scope | Theo từng application/service | Theo chuẩn reporting của tổ chức |
| Backstage | Một Software Template gọi netCI API | Plugin/catalog integration theo nhu cầu |
| CD orchestration | Temporalite local | Temporal service production hoặc orchestration platform nội bộ |

## Không được coi là assumption

Các yêu cầu sau là bắt buộc trong bản bàn giao: pipeline-as-code, JCasC rebuild, ephemeral agent, Docker/Kubernetes/Systemd adapters, Dev/Staging/Prod, approval/audit, SBOM/scan/sign/deny, DORA dashboard, multi-controller routing/failure drill và E2E evidence.

## Quy tắc thay thế provider

Mọi provider-specific value phải nằm trong configuration hoặc adapter registration. Core domain không được import Jenkins SDK, Helm CLI, Docker SDK, libvirt API hoặc Systemd command trực tiếp.
