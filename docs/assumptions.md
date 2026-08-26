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

## Quyết định đã chốt theo Claude artifact

Các quyết định dưới đây không còn là câu hỏi mở:

| Chủ đề | Quyết định chốt | Bằng chứng trong artifact |
|---|---|---|
| DORA | Hiển thị đúng 4 metric: Deployment Frequency, Lead Time for Changes, Change Failure Rate, Time to Restore Service | System Overview và tab DORA Metrics đều có 4 card/4 biểu đồ |
| Production workflow | Là luồng demo chính; Temporalite điều phối các bước dài, chờ và resume | Timeline gồm SR/CR, GNOC approval, NOCPro5 alarm check, CD Production, deploy modules và close SR/CR |
| Portal/Backstage | Custom Portal là UI chính; Backstage chỉ cần Software Template gọi cùng netCI API | Artifact chỉ mô tả Release Portal, không có Backstage status/log surface |
| Docker target | Deploy tới target server qua Ansible | Add environment ghi `Executed via Ansible` |
| Systemd target | Deploy tới target server qua Ansible | Add environment ghi `Executed via Ansible` |
| Kubernetes target | Adapter nhận kubeconfig; Ansible không bắt buộc bọc Helm/Kubernetes | Add environment ghi `Executed via kubeconfig` |
| CI execution | Pipeline chạy trên Jenkins, được GitLab webhook kích hoạt; module chọn system/custom runner | Bước CI/CD Configuration của New Module |
| Security presentation | Portal hiển thị kết quả coverage, automation test, SAST/SCA và vulnerabilities; không khóa UI vào tên vendor | Module Overview và Version history |

## Chỉ còn cần mentor xác nhận

1. Hai Jenkins controller cùng một Ubuntu host có được chấp nhận là mô phỏng multi-controller local cho bài demo không?
2. Viettel production dùng Kubernetes distribution, credential flow và namespace convention nào ngoài contract kubeconfig đã chốt?
3. Có bắt buộc mô phỏng tên/cách tích hợp registry, secret manager, scanner hoặc signing tool nội bộ hay chấp nhận Registry + MinIO + Syft + Trivy + Cosign cho local reference implementation?
