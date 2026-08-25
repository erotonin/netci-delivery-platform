# netCI Delivery Platform

Prototype Internal Developer Platform cho pipeline-as-code, ephemeral agent isolation và delivery đa runtime.

## Mục tiêu

Developer tạo application và pipeline từ Custom Portal hoặc API, không cần tự viết Jenkinsfile. Jenkins thực hiện CI trên agent container ephemeral riêng cho build; netCI/Temporal điều phối CD qua Docker, Kubernetes hoặc Systemd bằng Ansible/Helm.

## Runtime target

| Runtime | Adapter | Target local |
|---|---|---|
| Docker/Compose | `DockerRuntimeAdapter` | Linux VM riêng qua SSH/Ansible |
| Kubernetes | `KubernetesRuntimeAdapter` | kind, namespaces `dev`, `staging`, `prod` |
| Systemd | `SystemdRuntimeAdapter` | Linux VM thật có systemd qua SSH/Ansible |

## Stack đã chốt

- Backend: Python + FastAPI.
- Frontend: React + TypeScript + Vite.
- Workflow: Temporalite cho workflow nhiều bước; direct adapter call cho thao tác ngắn.
- CI: Jenkins Shared Library + JCasC.
- CD: netCI/Temporal điều phối; Ansible là runner đa runtime; Kubernetes dùng `kubernetes.core.helm`.
- Artifact/security: local Registry, MinIO, Syft, Trivy và Cosign.
- Local orchestration: Docker Compose cho service nền tảng, kind cho Kubernetes workload.

## Trạng thái môi trường

Giai đoạn hiện tại tạo contract và source skeleton trên Windows. Các runtime Linux chưa được coi là đã xác thực cho đến khi chạy trên Ubuntu 24.04 native.

## Khi chuyển sang Ubuntu

```bash
make doctor
make compose-up
make kind-up
make jenkins-up
make e2e-container
make e2e-kubernetes
make e2e-systemd
make security-test
make benchmark
make failure-drill
```

## Nguyên tắc boundary

Portal và Backstage chỉ gọi netCI API. Core domain không phụ thuộc Jenkins, Temporal, Docker, Helm hay Systemd. Mọi integration phải đi qua interface/adapter để thay provider mà không sửa domain.

## Chưa tuyên bố hoàn thành

Các phần cần runtime Ubuntu để xác thực gồm Docker/kind/Jenkins/Temporalite, ephemeral agent thật, KVM/libvirt, hai Linux VM, Ansible SSH, security scan/signing, benchmark và multi-controller failure drill.
