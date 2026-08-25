# Local VM provisioning

Mục tiêu là tạo hai Ubuntu VM bằng KVM/QEMU + libvirt:

| VM | vCPU | RAM | Disk | Vai trò |
|---|---:|---:|---:|---|
| `netci-docker` | 2 | 3 GiB | 30 GiB | Docker/Compose Ansible target |
| `netci-systemd` | 2 | 3 GiB | 25 GiB | Native Systemd Ansible target |

Thực hiện trên Ubuntu host sau khi cài `qemu-kvm`, `libvirt-daemon-system`, `virt-manager` và cloud image Ubuntu.

## Điều kiện an toàn

- Dùng NAT network mặc định, không bridge vào mạng công ty nếu chưa được phép.
- Dùng SSH key riêng cho lab, không dùng private key production.
- VM phải có user `netci` với quyền sudo giới hạn phù hợp.
- Ghi lại IP VM vào `deploy/ansible/inventories/local.ini`.
- Có snapshot clean trước mỗi E2E test.

## Chưa chạy trên Windows

KVM/libvirt provisioning chỉ được xác thực sau khi boot Ubuntu native. File này là runbook, không phải bằng chứng VM đã được tạo.
