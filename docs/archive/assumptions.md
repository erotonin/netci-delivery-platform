# Assumptions và điểm thay thế

| Chủ đề | Reference local | Khi tích hợp production |
|---|---|---|
| Kubernetes | kind với namespace `dev`, `staging`, `prod` | Kubernetes API tương thích; kubeconfig/service account và target namespace được cấu hình riêng từng môi trường |
| Jenkins topology | Hai controller cùng host chỉ chứng minh routing/failure drill | Nhiều host/failure domain thật |
| Authentication | `none` chỉ dành cho loopback; token file dùng được cho pilot | OIDC issuer/audience/group mapping thật |
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
| Production workflow | Temporal điều phối schedule, approval, deploy, health, rollback và callback bền vững | Tích hợp SR/CR/alarm ngoài chỉ được tuyên bố khi có adapter và evidence thật |
| Portal/Backstage | Custom Portal là UI chính; Backstage chỉ cần Software Template gọi cùng netCI API | Artifact chỉ mô tả Release Portal, không có Backstage status/log surface |
| Docker target | Deploy tới target server qua Ansible | Add environment ghi `Executed via Ansible` |
| Systemd target | Deploy tới target server qua Ansible | Add environment ghi `Executed via Ansible` |
| Kubernetes target | Adapter nhận kubeconfig; Ansible không bắt buộc bọc Helm/Kubernetes | Add environment ghi `Executed via kubeconfig` |
| CI execution | Pipeline chạy trên Jenkins, được GitLab webhook kích hoạt; module chọn system/custom runner | Bước CI/CD Configuration của New Module |
| Security presentation | Portal hiển thị kết quả coverage, automation test, SAST/SCA và vulnerabilities; không khóa UI vào tên vendor | Module Overview và Version history |

## Ba quyết định mặc định không còn chờ mentor

1. Hai Jenkins controller cùng một Ubuntu host **được chấp nhận cho local reference demo** nếu tách controller/JCasC/port/queue/volume và failure drill chứng minh router chuyển request mới khi một controller dừng. Kết quả này không được gọi là production HA.
2. Kubernetes production dùng contract trung lập distribution: Kubernetes API tương thích, kubeconfig gắn service account least-privilege và target namespace được khai báo rõ cho từng environment. Local dùng `dev`, `staging`, `prod`; core không tự đoán namespace production. Distribution, credential issuer và namespace là cấu hình có thể thay thế khi tích hợp thực tế.
3. Registry + MinIO + Syft + Trivy + Cosign là toolchain của local reference implementation. UI, policy và evidence dùng contract trung lập vendor để có thể thay từng provider bằng công cụ nội bộ mà không sửa core domain.

Ba quyết định này đủ để chạy và đánh giá reference local, không tự chứng minh một môi trường production đã live. Production chỉ đạt khi credential, endpoint và runtime target thật được nối và bộ acceptance gate chạy lại trên đúng commit triển khai.
