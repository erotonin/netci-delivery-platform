# Runbook Vận hành Lab netCI Corp (`netci-corp`)

Tài liệu hướng dẫn vận hành môi trường lab công ty `netci-corp` dành cho kỹ sư vận hành khi không có tác giả trực tiếp. Mọi hướng dẫn, câu lệnh và dấu hiệu nhận biết được đúc kết từ tài liệu thiết kế và các script tự động hóa trong kho mã nguồn.

---

## 1. Tổng quan và nơi giữ secret

Lab `netci-corp` chạy trên một máy chủ độc lập mô phỏng kiến trúc môi trường doanh nghiệp: cụm Kubernetes kind gồm 3 control-plane (HA etcd) + 3 worker node, GitLab CE (:8929), Harbor (:8930 HTTPS qua CA của lab), SeaweedFS S3 (:8333 HTTPS qua TLS gateway và CA của lab), Ingress netCI (`https://netci.corp.local`), máy đích triển khai `netci-corp-app-01` (:172.17.0.61 qua SSH) và **một** Jenkins controller (StatefulSet 1 replica) có backup và failover sang node khác bằng Velero 1.18.3 + Kopia uploader (ADR-055).

### Vị trí lưu trữ và nguyên tắc an toàn secret
- **Thư mục lưu trữ:** `.netci-gate/corp` (phân quyền: `chmod 700 .netci-gate/corp`).
- **Nguyên tắc bảo mật:**
  - Mọi secret được sinh tự động một lần khi khởi tạo, **không bao giờ in ra màn hình hoặc log** (`never printed`), **không commit vào Git** (`never committed`), và thư mục `.netci-gate/corp` không bị xóa bởi `scripts/corp/down.sh`.
  - Khi truyền secret vào các tiến trình (`curl`, `docker`, `git`), luôn nạp qua stdin (`-K -`, `docker login --password-stdin`, `GIT_ASKPASS`, `docker exec -i`), tuyệt đối không đưa secret vào tham số dòng lệnh nhằm tránh lộ lọt qua lệnh `ps`.
- **Các file secret chính:**
  - GitLab: `gitlab_root_password`, `gitlab_admin_token` (bootstrap), `gitlab_jenkins_token` (group token `read_repository`, Reporter — Jenkins clone), `gitlab_netci_token` (group token `api`, Developer — pipeline designer của netCI; hai token này hết hạn sau 90 ngày: xoá file rồi chạy lại `infra/corp/gitlab/bootstrap.sh`), `payments_api_webhook_secret`.
  - Harbor: `harbor_admin_password`, `harbor_db_password`, `harbor_robot_name`, `harbor_robot_secret` (robot `netci`: pull `netci`/`mirror`, push `apps`), `harbor_ops_robot_name`, `harbor_ops_robot_secret` (robot `ops`: push/pull `netci` và `mirror`).
  - Jenkins: `jenkins_admin_password` (vận hành), `jenkins_netci_sa_password` và `jenkins_netci_sa_token` (service account `netci-sa`: quyền Overall/Read và Job Build/Cancel/Configure/Create/Discover/Read, không có Administer).
  - S3 (SeaweedFS): `s3-access-key`, `s3-secret-key`, `s3.json` (quyền tối thiểu cho identity `velero` chỉ trên bucket `netci-jenkins-backups`).
  - Velero: `velero-credentials`, `velero-repo-password`.
  - CA & chứng chỉ: `.netci-gate/corp/pki` (`ca.key`, `ca.crt`, `s3.key`, `s3.crt`, `harbor.key`, `harbor.crt`, `netci.key`, `netci.crt`).
  - SSH đích triển khai: `.netci-gate/corp/ssh` (`id_ed25519`, `known_hosts`).
  - Chữ ký số cosign: `.netci-gate/jenkins/secrets/NETCI_COSIGN_PRIVATE_KEY` (dùng chung với live lab).

> [!IMPORTANT]
> **Khóa Kopia repository (`velero-repo-password`):** Đây là chiếc chìa khóa duy nhất để giải mã và đọc lại toàn bộ dữ liệu backup volume `JENKINS_HOME` trên S3. Nếu xảy ra thảm họa mất toàn bộ máy hoặc hỏng cụm, nếu không có khóa này thì toàn bộ backup đều vô giá trị. **Bắt buộc phải lưu giữ một bản sao của file này bên ngoài máy (escrow)** trong kho quản lý bí mật tập trung.

- **Cách biết đã đúng:** Thư mục `.netci-gate/corp` có quyền `drwx------` (700), các file secret bên trong có quyền `0600`, không có file nào rỗng.

---

## 2. Khởi động / sau khi reboot máy

### Lệnh khởi động
```bash
scripts/corp/up.sh
```
Script có tính chất lũy kế an toàn (idempotent): tự động nhận diện và bỏ qua các thành phần đã tồn tại hoặc đang chạy ổn định.

### Hai thành phần KHÔNG tự hồi phục sau khi reboot máy
1. **Container haproxy load balancer của cụm HA kind (`netci-corp-external-load-balancer`):** Do kind tạo container không có restart policy tồn tại qua host reboot, sau reboot mọi lệnh `kubectl` bị từ chối kết nối (`connection refused`) và worker báo `NotReady`. Lệnh:
   ```bash
   docker update --restart unless-stopped netci-corp-external-load-balancer && docker start netci-corp-external-load-balancer
   ```
2. **Các container của Harbor:** Khởi động trước container ghi syslog `harbor-log`, khiến chúng thoát với mã 128 (`exit 128`) và không tự thử lại. Lệnh:
   ```bash
   cd .netci-gate/corp/harbor-installer/harbor && sudo -n docker compose up -d
   ```
*(Cả hai thao tác trên đã được tích hợp tự động trong `scripts/corp/up.sh`).*

### Điều kiện tiên quyết trên máy chủ
- `fs.inotify.max_user_instances >= 1024` (cấu hình tại `/etc/sysctl.d/99-netci-kind.conf`). Nếu để mặc định của kernel (128), pod trong cụm 6 node sẽ bị crash-loop.
- Dung lượng đĩa trống trên `/` tối thiểu 10 GB (`free_gb >= 10`). `up.sh` chủ động từ chối chạy nếu đĩa trống dưới 10 GB nhằm tránh lỗi SeaweedFS chuyển volume sang read-only.
- Người dùng cần có quyền `sudo -n` để chạy installer của Harbor và cấu hình docker certs.d.

### Cách biết đã đúng
- `scripts/corp/up.sh` hoàn tất và in: `[HH:MM:SS] up. scripts/corp/status.sh shows the state; scripts/corp/jenkins_failover.sh runs the drill.` Không có lỗi dừng giữa chừng.

---

## 3. Kiểm tra sức khoẻ hằng ngày

### Lệnh kiểm tra
```bash
scripts/corp/status.sh
```
Đây là script chỉ đọc (read-only), không làm thay đổi trạng thái của bất kỳ dịch vụ nào.

### Dấu hiệu hệ thống khỏe mạnh (Healthy) và cách biết đã đúng
- **Dung lượng đĩa (Disk):** Dưới 90% dung lượng sử dụng. Nếu `>= 90%`, script in cảnh báo `WARNING: free space before the next backup (docker builder prune)`.
- **HTTP status của các dịch vụ cốt lõi:**
  - `GitLab`: Trả về `200` tại `http://172.17.0.1:8929/users/sign_in`.
  - `Harbor`: Trả về `200` tại `https://172.17.0.1:8930/api/v2.0/ping` (TLS, `--cacert .netci-gate/corp/pki/ca.crt`).
  - `S3`: Trả về `403` tại `https://172.17.0.1:8333/` (TLS với CA lab, endpoint từ chối request ẩn danh không ký AWS).
- **Trạng thái container:** Các container `netci-corp-s3`, `netci-corp-gitlab`, `harbor-core`, `registry` đều ở trạng thái `Up` (hoặc `healthy`).
- **Trạng thái cụm Kubernetes:**
  - Cả 6 node (`control-plane`, `control-plane2`, `control-plane3`, `worker`, `worker2`, `worker3`) đều ở trạng thái `Ready`.
  - Worker node có nhãn phân bổ đúng: `worker` (`netci.io/pool=ci`), `worker2` (`pool=ci`, `jenkins-controller=eligible`), `worker3` (`pool=platform`, `jenkins-controller=eligible`).
- **Jenkins Controller Pod:** Pod `jenkins-0` trong namespace `jenkins` có trạng thái `Running`, `Ready 1/1`, chạy trên một trong hai node mang nhãn `jenkins-controller=eligible` (`worker2` hoặc `worker3`).
- **Velero Backups:** Lệnh hiển thị các bản backup gần nhất có trạng thái `Completed`, 0 errors, 0 warnings.
- **Kiểm tra truy cập Jenkins UI (khi cần):**
  `kubectl --context kind-netci-corp -n jenkins port-forward svc/jenkins 18089:8080` rồi mở `http://127.0.0.1:18089/login`, đăng nhập user `admin` với mật khẩu trong `.netci-gate/corp/jenkins_admin_password`.

---

## 4. CA của lab và chứng chỉ

Lab sử dụng một Certificate Authority riêng (`netCI corp lab CA`) để các dịch vụ cung cấp TLS có xác thực chứng chỉ hoàn chỉnh, tuyệt đối không dùng cờ bỏ qua kiểm tra TLS (`insecure-skip-verify`).

### Lệnh tạo CA và cấp chứng chỉ
- Khởi tạo CA một lần (idempotent, lưu tại `.netci-gate/corp/pki/ca.crt` và `ca.key`, hiệu lực 1825 ngày / 5 năm): `scripts/corp/lab_ca.sh ca`
- Cấp hoặc gia hạn chứng chỉ máy chủ (leaf certificate, ECDSA P-256, hiệu lực 397 ngày theo tiêu chuẩn trình duyệt): `scripts/corp/lab_ca.sh issue <name> <san>...`

### Các chứng chỉ đang tồn tại trong lab
- `s3`: SAN `IP:172.17.0.1 DNS:netci-corp-s3 DNS:localhost IP:127.0.0.1` (`.netci-gate/corp/pki/s3.{key,crt}`, `s3-chain.crt`).
- `harbor`: SAN `IP:172.17.0.1 DNS:localhost IP:127.0.0.1` (`.netci-gate/corp/pki/harbor.{key,crt}`, `harbor-chain.crt`).
- `netci`: SAN `DNS:netci.corp.local` (`.netci-gate/corp/pki/netci.{key,crt}`, `netci-chain.crt`).

### Quy trình gia hạn và khởi động lại dịch vụ tương ứng
Khi gia hạn (`scripts/corp/lab_ca.sh issue <name> ...`), cặp khóa và chứng chỉ mới được sinh trong thư mục tạm, chỉ khi xác thực thành công qua CA mới ghi đè vào `.netci-gate/corp/pki/`. Sau khi gia hạn, nạp lại chứng chỉ cho dịch vụ tương ứng:
- **S3 gateway container (`netci-corp-s3-tls`):**
  `scripts/corp/lab_ca.sh issue s3 IP:172.17.0.1 DNS:netci-corp-s3 DNS:localhost IP:127.0.0.1 && docker restart netci-corp-s3-tls`
- **Harbor:**
  `scripts/corp/lab_ca.sh issue harbor IP:172.17.0.1 DNS:localhost IP:127.0.0.1 && bash infra/corp/harbor/install.sh`
- **Ingress netCI (Secret `netci-tls` trong namespace `netci-system`):**
  `scripts/corp/lab_ca.sh issue netci DNS:netci.corp.local`
  `kubectl --context kind-netci-corp -n netci-system create secret tls netci-tls --cert=.netci-gate/corp/pki/netci.crt --key=.netci-gate/corp/pki/netci.key --dry-run=client -o yaml | kubectl --context kind-netci-corp apply -f -`

### Các client lưu trữ và tin tưởng CA của lab
1. **Các node kind:** File `.netci-gate/corp/containerd-certs.d/172.17.0.1:8930/ca.crt` mount vào `/etc/containerd/certs.d` trên cả 6 node kind; containerd `hosts.toml` cấu hình kéo image qua TLS xác thực.
2. **Build pods (agent Jenkins):** ConfigMap `lab-ca` trong namespace `netci-build`, mount vào container `builder` tại `/etc/containers/certs.d/172.17.0.1:8930/ca.crt` (buildah) và `/etc/netci/registry-ca`, biến môi trường `SSL_CERT_DIR="/etc/ssl/certs:/etc/netci/registry-ca"` (cosign, trivy).
3. **netCI (API & worker):** ConfigMap `lab-ca` trong namespace `netci-system` (`registry.caConfigMap: lab-ca` trong `values-lab-corp.yaml`).
4. **Velero:** Secret `lab-ca` trong namespace `velero` (tạo tự động qua `velero install --cacert`), dùng cho BSL kết nối S3 qua TLS.
5. **GitLab:** Bản sao `.netci-gate/corp/gitlab-trusted-certs/netci-lab-ca.crt` mount writable vào `/etc/gitlab/trusted-certs` để xác thực webhook gửi tới netCI Ingress.
6. **Máy đích triển khai (`netci-corp-app-01`):** Lưu tại `/etc/docker/certs.d/172.17.0.1:8930/ca.crt`.
7. **Máy host:** Lưu tại `/etc/docker/certs.d/172.17.0.1:8930/ca.crt` và `/etc/docker/certs.d/localhost:8930/ca.crt`.

### Cách biết đã đúng
- Kiểm tra chứng chỉ đã cấp: `openssl verify -CAfile .netci-gate/corp/pki/ca.crt .netci-gate/corp/pki/<name>.crt` trả về `.netci-gate/corp/pki/<name>.crt: OK`.
- Kiểm tra kết nối S3 qua TLS: `curl -s -o /dev/null -w '%{http_code}' --cacert .netci-gate/corp/pki/ca.crt https://172.17.0.1:8333/` trả về `403`.
- Kiểm tra Harbor qua TLS: `curl -fsS -o /dev/null --cacert .netci-gate/corp/pki/ca.crt https://172.17.0.1:8930/api/v2.0/ping` thoát 0.

---

## 5. Đẩy image lên Harbor

### Lệnh thực thi
```bash
scripts/corp/push_image.sh <local image> <harbor reference>
# Ví dụ: scripts/corp/push_image.sh nginx:1.29 172.17.0.1:8930/mirror/nginx:1.29
```

### Tại sao không dùng lệnh `docker push`?
Theo ghi chú thiết kế trong script: Docker 29 sử dụng containerd image store, khi lấy registry token nó không nạp cấu hình chứng chỉ từ `/etc/docker/certs.d`, dẫn đến lỗi xác thực private CA (`x509: certificate signed by unknown authority`). Nếu cấu hình tin tưởng CA của lab trên toàn bộ máy chủ (`machine-wide trust`), mọi tiến trình trên host đều sẽ tin cậy CA này, vi phạm nguyên tắc cô lập. Thay vào đó, script sử dụng công cụ `buildah` (từ toolbox `netci/ci-toolbox:0.4.0`) để đẩy image với tham số `--cert-dir` chỉ chứa duy nhất CA của lab, xác thực bằng tài khoản robot của người vận hành (`harbor_ops_robot_name` / `secret`). Image được xuất qua `docker save` và giữ nguyên vẹn nội dung.

### Cách biết đã đúng
Lệnh chạy thành công, thoát mã 0 và in ra digest của image lưu trên Harbor (dạng `sha256:...`) để ghim cố định phiên bản.

---

## 6. netCI trên netci-corp

### Cài đặt và nâng cấp
```bash
helm upgrade --install netci deploy/helm/netci-platform -n netci-system \
  -f deploy/helm/netci-platform/examples/values-lab-corp.yaml
```

### Các Secret tồn tại trong namespace `netci-system`
1. **`netci-app`:** Chứa cấu hình bảo mật chính của netCI:
   - `database-url`: Chuỗi kết nối PostgreSQL tới cơ sở dữ liệu `netci_corp` (`postgresql://netci:...@172.17.0.1:5432/netci_corp`).
   - `workload-token-keys`: Khóa ký token phân phối cho build và deployment (`k1:<chuỗi ngẫu nhiên >= 32 ký tự>`).
   - `cosign.pub`: Khóa công khai cosign tương ứng với private key `.netci-gate/jenkins/secrets/NETCI_COSIGN_PRIVATE_KEY`.
   - `jenkins-corp-api-token`: API token của tài khoản dịch vụ `netci-sa` lấy từ `.netci-gate/corp/jenkins_netci_sa_token` (hoặc mật khẩu từ `jenkins_netci_sa_password`).
   - `scm-gitlab-token`: Token nhóm GitLab với quyền `api` (Developer) lấy từ `.netci-gate/corp/gitlab_netci_token`.
2. **`netci-deploy-targets`:** Secret chứa thông tin xác thực SSH tới máy đích triển khai:
   - `id_ed25519`: Private key SSH sinh tại `.netci-gate/corp/ssh/id_ed25519`.
   - `known_hosts`: Khóa máy chủ đã ghim tại `.netci-gate/corp/ssh/known_hosts`.
   *(Tạo qua: `kubectl --context kind-netci-corp -n netci-system create secret generic netci-deploy-targets --from-file=id_ed25519=.netci-gate/corp/ssh/id_ed25519 --from-file=known_hosts=.netci-gate/corp/ssh/known_hosts --dry-run=client -o yaml | kubectl --context kind-netci-corp apply -f -`)*
3. **`netci-tls`:** Chứng chỉ TLS cho Ingress: `tls.crt` (từ `.netci-gate/corp/pki/netci.crt`), `tls.key` (từ `.netci-gate/corp/pki/netci.key`).
4. **ConfigMap `lab-ca`:** Lưu root CA từ `.netci-gate/corp/pki/ca.crt` (được trỏ bởi `registry.caConfigMap: lab-ca`).

### Địa chỉ truy cập (URLs) và máy đích triển khai (Deploy target)
- Giao diện và API: `https://netci.corp.local` thông qua Ingress-nginx trên worker node `netci-corp-worker`. Máy host cần định tuyến tên miền `netci.corp.local` về IP của node này trong `/etc/hosts`.
- Khởi tạo hoặc dừng máy đích giả lập: `scripts/corp/app_host.sh up` (hoặc `down`). Máy đích chạy container `netci-corp-app-01` (IP `172.17.0.61`, image `netci/lab-prod-host:0.2.0`). netCI worker kết nối qua Ansible bằng SSH với private key riêng và host key đã ghim (`StrictHostKeyChecking=yes`). Docker trên máy đích tin cậy Harbor qua CA của lab.

### Cách biết đã đúng
- Kiểm tra trạng thái tích hợp của netCI:
  ```bash
  kubectl --context kind-netci-corp -n netci-system exec deploy/netci-netci-platform-api -c api -- python -c \
    'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8000/readyz").read().decode())'
  ```
  Phản hồi HTTP 200 và `"ready": true` cho tất cả các thành phần (`database`, `ci`, `cd`, `cosign`).
- Kiểm tra máy đích: `scripts/corp/app_host.sh up` in ra `ssh-ok`, `become-ok` và phiên bản Docker.

---

## 7. Kiểm chứng đầu-cuối (E2E Verification)

### 1. Kiểm chứng quy trình build và triển khai (`e2e_build.py`)
```bash
set -a; source .netci-gate/real-local.env; set +a
NETCI_API_URL=http://127.0.0.1:18100 .venv/bin/python scripts/corp/e2e_build.py
```
*(Nếu gọi qua Ingress, đặt `NETCI_API_URL=https://netci.corp.local`).*
1. Lấy token OIDC cho tài khoản admin và developer qua Keycloak.
2. Tạo hệ thống `corp-payments` và module `payments-api` trỏ tới repo GitLab `http://172.17.0.1:8929/platform/payments-api.git`.
3. Đăng ký tích hợp SCM và webhook trên GitLab trỏ về `https://netci.corp.local/api/webhooks/scm/gitlab` (xác thực TLS qua CA lab).
4. Kích hoạt pipeline run trên commit nhánh `main`. Jenkins controller build image, quét lỗ hổng, tạo SBOM và ký số cosign.
5. Kiểm tra security evidence: digest `sha256:...`, chữ ký xác thực thành công, danh sách `toolVersions` có đủ `syft`, `trivy`, `cosign`, quyết định chính sách là `allow`.
6. Kiểm tra `GET /toolchain`: controller `corp` / `jenkins-corp` báo cáo đúng các phiên bản công cụ đã khai báo trong `toolchain/versions.yaml`, không bị drift (`drift none`), Trivy DB không quá hạn (`trivyDb stale: False`).
7. Worker triển khai ứng dụng qua SSH tới `netci-corp-app-01`, xác nhận trạng thái deployment kết thúc ở `healthy`.

### 2. Kiểm chứng Pipeline Designer (`e2e_designer.py`)
Theo ADR-057, người dùng chỉnh sửa pipeline trên portal nhưng việc phê duyệt thực hiện qua Git:
1. **Bước 1 - Đề xuất (Propose):**
   `NETCI_API_URL=http://127.0.0.1:18100 .venv/bin/python scripts/corp/e2e_designer.py propose`
   Script gọi netCI mở một Merge Request (MR) trên GitLab tại nhánh `netci/pipeline-<id>`, chỉ thay đổi đúng 2 file: `.netci/pipeline.yaml` và `.netci/stages/lint-dockerfile.sh`.
2. **Bước 2 - Phê duyệt (Merge):**
   > [!IMPORTANT]
   > **Merge bắt buộc phải do một con người thực hiện (review và bấm Merge trên giao diện GitLab).** Người đề xuất tuyệt đối không bao giờ được tự merge (chống self-approval theo ADR-057). Token của netCI (`api`/Developer) cũng không có quyền bypass phê duyệt trên nhánh được bảo vệ.
3. **Bước 3 - Xác minh (Verify):**
   Sau khi MR được merge trên GitLab, webhook kích hoạt netCI cập nhật revision:
   `NETCI_API_URL=http://127.0.0.1:18100 .venv/bin/python scripts/corp/e2e_designer.py verify`
   Script xác minh pipeline của module đã mang stage mới, mã script đọc lại từ GitLab khớp chính xác với nội dung đề xuất, và sự kiện audit `pipeline.proposal_opened` được ghi nhận.

### Cách biết đã đúng
- `e2e_build.py` in `[ok]` cho từng bước và in dòng `Deployment ... healthy on ['netci-corp-app-01']`.
- `e2e_designer.py propose` in `[ok] merge request !<iid> opened on netci/pipeline-...`.
- `e2e_designer.py verify` in `[ok] stage code read back from GitLab matches what was proposed`.

---

## 8. Backup

### Lịch sao lưu tự động (Schedule)
- Khai báo tại `infra/corp/velero/schedule.yaml`.
- Schedule tên `jenkins-backup`, định kỳ mỗi 15 phút một lần (`*/15 * * * *`), lưu trữ 24 giờ (`ttl: 24h`).
- Áp dụng `defaultVolumesToFsBackup: true` cho namespace `jenkins`.
- Tích hợp hook đóng băng dữ liệu (`jenkins-quiesce`): Trước khi backup, Velero gọi lệnh `/bin/sh -c sync` trực tiếp trong container `jenkins` của pod controller để xả sạch bộ đệm tệp xuống volume.

### Chụp backup theo yêu cầu (On-demand backup)
Khi chuẩn bị bảo trì hoặc cần tạo điểm phục hồi tức thời, luôn thực thi:
```bash
.netci-gate/corp/bin/velero --kubecontext kind-netci-corp backup create <tên-backup> --from-schedule jenkins-backup --wait
```
**Tại sao LUÔN LUÔN dùng `--from-schedule jenkins-backup`?**
Tham số `--from-schedule` bắt buộc Velero kế thừa toàn bộ cấu hình mẫu của schedule: bao gồm hook `sync` (tránh rách file XML) và cơ chế file-system backup (`defaultVolumesToFsBackup: true`). Nếu chỉ chạy `velero backup create` thuần túy, lệnh sẽ bỏ qua hook và bỏ qua volume PV của Jenkins, tạo ra bản backup rỗng không thể dùng để khôi phục.

### Kiểm tra backup Completed và BackupStorageLocation (BSL)
- Trạng thái backup: `.netci-gate/corp/bin/velero --kubecontext kind-netci-corp backup get <tên-backup>`
- BackupStorageLocation đích: `.netci-gate/corp/bin/velero --kubecontext kind-netci-corp backup describe <tên-backup> | grep "Backup Storage Location"`
  Backup phải được ghi vào BSL `jenkins-s3` (bucket `netci-jenkins-backups`, repository mã hoá bằng khoá riêng). BSL `default`, trỏ vào bucket `velero` cũ (khoá mặc định công khai), chỉ còn ở chế độ `ReadOnly` cho tới khi bị huỷ. Kiểm tra danh sách BSL:
  `.netci-gate/corp/bin/velero --kubecontext kind-netci-corp backup-location get`

### Cách biết đã đúng
Cột `STATUS` hiển thị `Completed`, `ERRORS: 0`, `WARNINGS: 0`, và BSL ở trạng thái `Available`.

---

## 9. Failover Jenkins (planned, unplanned và diễn tập)

Quy trình chuyển đổi dự phòng controller Jenkins được tự động hóa qua script:
```bash
scripts/corp/jenkins_failover.sh [--planned] [--no-marker] [--unplanned]
```

### Các kịch bản thực hiện
1. **Diễn tập phục hồi tiêu chuẩn (Drill):** `scripts/corp/jenkins_failover.sh`
   Script tạo job đánh dấu (`dr-marker`) trên Jenkins, chạy build #N, tạo backup tức thì, cô lập node cũ (`cordon`), xóa namespace `jenkins`, khôi phục từ backup sang node eligible còn lại, và xác minh build #N vẫn tồn tại nguyên vẹn.
2. **Chuyển có kế hoạch (Planned Failover):** `scripts/corp/jenkins_failover.sh --planned`
   Tự động chụp một bản backup mới nhất trước khi chuyển (`planned-<timestamp>`), xóa namespace và restore sang node kia.
3. **Mất node đột xuất thực tế (Unplanned Failover):** `scripts/corp/jenkins_failover.sh --unplanned`
   Mô phỏng sự cố mất nguồn máy chủ đột ngột bằng `docker kill` trên worker node đang chứa controller.

### Thứ tự các bước bắt buộc khi xử lý mất node đột xuất (`--unplanned`)
1. **Mất nguồn đột ngột:** Node chứa controller bị tắt nguồn đột ngột (`docker kill <node>`).
2. **Phát hiện:** Cụm Kubernetes phát hiện node chuyển sang trạng thái `NotReady` (khoảng 49 giây).
3. **Fencing (Cô lập dứt điểm):** Xác nhận container node đã dừng hẳn (`Running == false`), kubelet cũ không thể tiếp tục chạy `jenkins-0`.
4. **Cordon:** Cordon node hỏng (`kubectl cordon <node>`).
5. **Cưỡng chế xóa pod và namespace:** Kubernetes mặc định giữ pod StatefulSet trên node chết vì không biết node chỉ bị phân vùng mạng hay đã tắt thật. Người vận hành bắt buộc phải xóa cưỡng bức:
   `kubectl --context kind-netci-corp -n jenkins delete pod --all --grace-period=0 --force`
   `kubectl --context kind-netci-corp delete namespace jenkins --wait=false`
6. **Gỡ finalizer của PVC:** Do kubelet trên node chết không thể xác nhận nhả volume detach, patch gỡ bỏ finalizer trên các PVC trong namespace `jenkins` để namespace bị xóa hoàn toàn.
7. **Khôi phục từ bản backup `Completed`:**
   `.netci-gate/corp/bin/velero --kubecontext kind-netci-corp restore create <tên-restore> --from-backup <tên-backup> --wait`
8. **Đợi controller sẵn sàng:** Chờ pod `jenkins-0` khởi động xong và Ready trên node eligible còn lại (`worker2` hoặc `worker3`).
9. **Khôi phục node cũ (Node recovery):** Bật lại container node (`docker start <node>`), chờ node chuyển về `Ready`, sau đó bỏ cô lập (`kubectl uncordon <node>`). Kubelet trên node phục hồi sẽ không tìm thấy pod nào của Jenkins do API server đã xóa trước đó.

### Kết quả đo lường độ sẵn sàng API (từ ADR-055)
- Trong bài kiểm thử unplanned failover thực tế ghi nhận **RTO = 107 giây**, **RPO = 0 giây** (backup ngay trước sự cố), build đánh dấu #8 tồn tại nguyên vẹn.
- **Độ sẵn sàng API của netCI:** Khi liên tục gửi request thăm dò mỗi giây một lần qua một node còn sống trong suốt quá trình failover: chỉ có **12 trên tổng số 114 request bị thất bại**, và tất cả đều nằm trọn trong **cửa sổ phát hiện 49 giây** (do Kubernetes Service vẫn chuyển tiếp request tới pod replica trên node vừa chết trước khi kube-controller-manager kịp loại bỏ endpoint; Ingress có cơ chế retry sẽ che giấu được các lỗi này). Sau 49 giây, không có thêm bất kỳ request nào bị lỗi.
- **Bài học từ đợt kiểm thử trước:** Từng có lần toàn bộ API bị mất trong 58 giây do init container chạy `scripts/migrate.py` của replica còn lại bị timeout kết nối database sau khi node của nó khởi động lại, và kubelet back-off giữ pod 2.5 phút. Hiện nay `scripts/migrate.py` đã có cơ chế tự động thử lại kết nối, replica chuyển sang Ready chỉ sau 12 giây khi node phục hồi.

### Xử lý khi bản backup rơi vào `PartiallyFailed`
Nếu backup gần nhất có trạng thái `PartiallyFailed`, script `jenkins_failover.sh` sẽ **từ chối khôi phục** (`backup ... is PartiallyFailed, not Completed`).
> [!CAUTION]
> **TUYỆT ĐỐI KHÔNG ÉP RESTORE (`do not force it`) từ bản PartiallyFailed.** Trạng thái này thường báo hiệu quá trình upload volume bị ngắt quãng giữa chừng (ví dụ SeaweedFS bị OOM). Cố tình khôi phục từ bản này sẽ khiến `JENKINS_HOME` bị thiếu thư mục `secrets/` hoặc hỏng cấu trúc XML, gây mất dữ liệu không thể cứu vãn. Người vận hành phải tìm bản backup `Completed` trước đó để phục hồi.

### Cách biết đã đúng
Lệnh failover in: `PASS: marker build #<N> survived the failover` cùng giá trị RTO và RPO đo được.

---

## 10. Xoay khoá repository (Key rotation)

Quy trình xoay khóa Kopia repository tuân thủ nghiêm ngặt 7 bước tuần tự từ tài liệu nghiên cứu kỹ thuật (`docs/research/velero-backup-hardening.md`, mục 7). Không sử dụng lệnh `kopia repository change-password` vì master key cũ đã bị lộ theo mật khẩu mặc định công khai của Velero.

1. **Điểm rollback bằng khoá cũ:** `velero schedule pause jenkins-backup`, rồi `velero backup create pre-rekey-<ts> --from-schedule jenkins-backup --wait`; bản này phải `Completed`, 0 lỗi.
2. **Kho mới:** Tạo bucket mới (`netci-jenkins-backups`); khoá mới sinh ngẫu nhiên, lưu **ngoài cụm** (`.netci-gate/corp/velero-repo-password`, quyền 600).
3. **Chuyển:** Áp secret `velero-repo-credentials` với khoá mới; tạo BSL mới trỏ bucket mới và đặt làm mặc định; chuyển BSL cũ sang `ReadOnly`; restart `velero` và `node-agent`.
4. **Chứng minh, không giả định:** `velero backup create rekey-verify --from-schedule jenkins-backup --wait` phải `Completed`; rồi `scripts/corp/jenkins_failover.sh` phải PASS -- restore được bằng khoá mới là bằng chứng duy nhất khoá mới dùng được. Bước này **bắt buộc**. Nếu thất bại: rollback về cấu hình cũ.
5. **Bật lại lịch:** (`velero schedule unpause jenkins-backup`).
6. **Huỷ dữ liệu cũ ngay khi bước 4 PASS**, không giữ thêm: Xoá BSL cũ và bucket cũ.
7. **Least privilege sau cùng:** Giới hạn identity của Velero trong `s3.json` vào bucket mới (`Read/Write/List/Tagging:netci-jenkins-backups`, bỏ `Admin`), restart SeaweedFS, kiểm tra `velero backup-location get` còn `Available`.

*(Hiện trạng lab: bước 1–5 và bước 7 đã xong. Identity S3 của Velero chỉ còn quyền trên `netci-jenkins-backups`. Bước 6 xoá BSL `default` và bucket `velero` cũ đang chờ quyết định của người vận hành).*

### Cách biết đã đúng
Bản backup kiểm tra `rekey-verify` đạt `Completed` và diễn tập failover từ repository mới PASS.

---

## 11. Phát hành phiên bản shared library mới

### Lệnh phát hành
```bash
LIB_TAG=netci-0.x.y bash infra/corp/gitlab/bootstrap.sh
```

### Các nguyên tắc quản trị phiên bản
- **Tags không bao giờ được dịch chuyển (`tags are never moved`):** Một khi tag đã phát hành lên GitLab, tuyệt đối không dịch chuyển tag sang commit khác. Dịch chuyển tag sẽ âm thầm làm thay đổi mã nguồn thư viện mà tất cả các job đang ghim phiên bản đó thực thi. Nếu có thay đổi, phải nâng giá trị `LIB_TAG` lên phiên bản mới.
- **Main không bao giờ được force push (`main is never force-pushed`):** Nhánh `main` của thư viện được bảo vệ (protected). Mỗi phiên bản mới là một commit mới nằm trên `main`, gắn tag có chú thích (`git tag -a`).

### Cập nhật cấu hình sau khi phát hành
Theo ADR-056, thư viện từ bản `netci-0.4` trở lên bắt buộc phải báo cáo `toolVersions` (syft, trivy, cosign), các bản cũ hơn sẽ bị cổng chính sách từ chối (`ARTIFACT_POLICY_DENIED`). Sau khi phát hành:
1. Cập nhật phiên bản trong cấu hình JCasC của Jenkins (`infra/corp/jenkins/values.yaml` tại `unclassified.globalLibraries.libraries[0].defaultVersion: netci-0.4.1`).
2. Cập nhật cấu hình netCI platform (`deploy/helm/netci-platform/examples/values-lab-corp.yaml` tại `jenkins.sharedLibrary: netci-shared-library@netci-0.4.1`).

### Cách biết đã đúng
- Script xuất thông báo: `gitlab: group platform, netci-shared-library@netci-0.x.y, payments-api`
- Nếu tag đã tồn tại từ trước, script dừng và từ chối ghi đè: `gitlab: netci-0.x.y already exists; a released tag is not moved (bump LIB_TAG)`

---

## 12. Sự cố thường gặp

### 1. SeaweedFS bị đầy đĩa (Full-disk incident)
- **Hiện tượng:** Bản backup Velero thất bại liên tục với lỗi HTTP 500 (`Internal Server Error`), SeaweedFS báo lỗi volume chuyển sang chế độ chỉ đọc (`read-only volumes`).
- **Nguyên nhân:** Dung lượng đĩa trên phân vùng `/` của máy host bị đầy (từng ghi nhận ở mức 100% ngày 2026-09-26). SeaweedFS tự động khóa ghi volume để bảo vệ dữ liệu.
- **Cơ chế phòng ngừa:**
  - `scripts/corp/status.sh` cảnh báo khi dung lượng sử dụng `>= 90%`: `WARNING: free space before the next backup (docker builder prune)`.
  - `scripts/corp/up.sh` kiểm tra dung lượng trống trên `/`, từ chối khởi động nếu còn dưới 10 GB: `only <N> GB free on /: free space first (docker builder prune)`.
- **Cách xử lý:** Giải phóng dung lượng đĩa của Docker (`docker builder prune -a -f && docker system prune -f`). Sau khi đĩa trống >= 10 GB, khởi động lại SeaweedFS: `docker compose -f infra/corp/seaweedfs/docker-compose.yml restart`.

### 2. SeaweedFS bị tràn bộ nhớ (SeaweedFS OOM)
- **Hiện tượng:** Bản backup Velero bị dừng đột ngột, Kopia báo lỗi `connection reset`, Velero đánh dấu volume backup là `Canceled` hoặc backup rơi vào trạng thái `PartiallyFailed`.
- **Nguyên nhân:** Giới hạn RAM 512 MiB trước đây bị vượt qua khi Kopia upload song song nhiều khối dữ liệu, tiến trình all-in-one của SeaweedFS đệm dữ liệu dẫn đến bị Linux cgroup OOM-kill.
- **Cách xử lý:** Đảm bảo `mem_limit: 1g` và đặt biến môi trường `GOMEMLIMIT: 800MiB` trong file `infra/corp/seaweedfs/docker-compose.yml`, sau đó khởi động lại:
  `docker compose -f infra/corp/seaweedfs/docker-compose.yml up -d`

### 3. Harbor container thoát với mã 128 sau reboot (Harbor exit 128 after reboot)
- **Hiện tượng:** Sau khi reboot máy, gọi `curl --cacert .netci-gate/corp/pki/ca.crt https://172.17.0.1:8930/api/v2.0/ping` bị lỗi, kiểm tra container Harbor thấy dừng với exit code 128.
- **Nguyên nhân:** Khi khởi động máy, các container dịch vụ của Harbor khởi động trước container syslog `harbor-log`, khiến chúng không thể gửi log và thoát với mã 128 mà docker compose không tự thử lại.
- **Cách xử lý:** Khởi động lại toàn bộ stack Harbor với quyền root:
  `cd .netci-gate/corp/harbor-installer/harbor && sudo -n docker compose up -d`

### 4. Mất kết nối API cụm kind sau reboot (kind load balancer after reboot)
- **Hiện tượng:** Sau khi khởi động lại máy, mọi lệnh `kubectl` báo lỗi kết nối từ chối (`connection refused`) và các worker node hiển thị trạng thái `NotReady`.
- **Nguyên nhân:** Container haproxy `netci-corp-external-load-balancer` (đứng trước 3 control-plane nodes) do kind tạo không có restart policy tồn tại qua host reboot.
- **Cách xử lý:** Cập nhật restart policy và khởi động lại container:
  `docker update --restart unless-stopped netci-corp-external-load-balancer && docker start netci-corp-external-load-balancer`

### 5. Lỗi giới hạn inotify của hệ điều hành (inotify limit)
- **Hiện tượng:** Các pod chạy trên cụm Kubernetes liên tục rơi vào trạng thái `CrashLoopBackOff`.
- **Nguyên nhân:** Giá trị mặc định `fs.inotify.max_user_instances` của Linux kernel là 128, không đủ cho số lượng inotify watcher của cụm kind 6 node.
- **Cách xử lý:** Cấu hình tham số kernel tối thiểu 1024 trong file `/etc/sysctl.d/99-netci-kind.conf`:
  `sudo sysctl -w fs.inotify.max_user_instances=1024 && sysctl -n fs.inotify.max_user_instances`

### 6. Lỗi Hook "container not found" ngay sau khi restart (hook container not found -- retry)
- **Hiện tượng:** Lệnh backup thực hiện ngay sau khi pod Jenkins khởi động lại bị báo lỗi: hook `jenkins-quiesce` thất bại với thông báo container `jenkins` không tìm thấy (`container not found`), làm bản backup bị `PartiallyFailed`.
- **Nguyên nhân:** Pod `jenkins-0` mới được tái tạo hoặc đang trong quá trình chuyển trạng thái, container `jenkins` chưa kịp sẵn sàng để kubelet thực thi lệnh exec hook `sync`.
- **Cách xử lý:** Chờ pod `jenkins-0` chuyển hẳn sang trạng thái `Running` và `1/1 Ready` (`kubectl --context kind-netci-corp -n jenkins wait --for=condition=ready pod/jenkins-0 --timeout=600s`), sau đó thử lại lệnh tạo backup:
  `.netci-gate/corp/bin/velero --kubecontext kind-netci-corp backup create <tên-backup> --from-schedule jenkins-backup --wait`

### 7. Init container migrate bị ConnectionTimeout sau khi reboot node
- **Hiện tượng:** Pod API rơi vào `CrashLoopBackOff`, init container migrate báo lỗi không thể kết nối tới cơ sở dữ liệu PostgreSQL, kubelet back-off khiến replica bị treo tới 2.5 phút và gây gián đoạn API.
- **Nguyên nhân:** Sau khi một node khởi động lại, mạng pod (CNI) cần một khoảng thời gian ngắn để thiết lập định tuyến tới PostgreSQL trên host (`172.17.0.1:5432`). Trước đây `scripts/migrate.py` chỉ thử kết nối 1 lần duy nhất rồi thoát.
- **Cách xử lý & Hiện trạng:** `scripts/migrate.py` đã được bổ sung hàm `connect()` tự động thử lại kết nối với deadline qua biến môi trường `NETCI_MIGRATE_CONNECT_DEADLINE_SECONDS` (mặc định 90 giây, thử lại mỗi 2 giây). Replica API sẽ tự động chuyển sang `Ready` sau khoảng 12 giây khi mạng sẵn sàng.

### 8. Thư mục `trusted-certs` của GitLab cần quyền ghi (GitLab trusted-certs needing a writable mount)
- **Hiện tượng:** Container GitLab khởi động lỗi hoặc không nạp được CA của lab, khiến các webhook gửi tới Ingress của netCI thất bại do không xác thực được SSL.
- **Nguyên nhân:** Khi khởi động và reconfigure, GitLab Omnibus thực thi lệnh `c_rehash` trên thư mục `/etc/gitlab/trusted-certs` để tạo các symlink băm cho chứng chỉ. Nếu mount thư mục này ở chế độ chỉ đọc (`:ro`), `c_rehash` sẽ gặp lỗi `Read-only file system` và dừng lại.
- **Cách xử lý:** Tạo thư mục riêng `.netci-gate/corp/gitlab-trusted-certs` chứa bản sao của `ca.crt` và mount vào container ở chế độ có quyền ghi (không dùng cờ `:ro`) trong `infra/corp/gitlab/docker-compose.yml`:
  ```bash
  mkdir -p .netci-gate/corp/gitlab-trusted-certs
  cp .netci-gate/corp/pki/ca.crt .netci-gate/corp/gitlab-trusted-certs/netci-lab-ca.crt
  ```
