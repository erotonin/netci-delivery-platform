# Runbook Vận hành Lab netCI Corp (`netci-corp`)

Tài liệu hướng dẫn vận hành môi trường lab công ty `netci-corp` dành cho kỹ sư vận hành khi không có tác giả trực tiếp. Mọi hướng dẫn, câu lệnh và dấu hiệu nhận biết được đúc kết từ tài liệu thiết kế và các script tự động hóa trong kho mã nguồn.

---

## 1. Tổng quan và nơi giữ secret

Lab `netci-corp` chạy trên một máy chủ độc lập mô phỏng kiến trúc môi trường doanh nghiệp: cụm Kubernetes kind gồm 3 control-plane (HA etcd) + 3 worker node, GitLab CE (:8929), Harbor (:8930), SeaweedFS S3 (:8333) và **một** Jenkins controller (StatefulSet 1 replica) có backup và failover sang node khác bằng Velero 1.18.3 + Kopia uploader (ADR-055).

### Vị trí lưu trữ và nguyên tắc an toàn secret
- **Thư mục lưu trữ:** `.netci-gate/corp` (phân quyền: `chmod 700 .netci-gate/corp`).
- **Nguyên tắc bảo mật:**
  - Mọi secret được sinh tự động một lần khi khởi tạo, **không bao giờ in ra màn hình hoặc log** (`never printed`), **không bao giờ commit vào Git** (`never committed`).
  - Khi truyền secret vào các tiến trình (`curl`, `docker`, `git`), luôn nạp qua stdin (`-K -`, `docker login --password-stdin`, `GIT_ASKPASS`, `docker exec -i`), tuyệt đối không đưa secret vào tham số dòng lệnh nhằm tránh lộ lọt qua lệnh `ps`.
  - Thư mục `.netci-gate/corp` không bao giờ bị xóa bởi lệnh `scripts/corp/down.sh`.
- **Các file secret chính:**
  - GitLab: `gitlab_root_password`, `gitlab_admin_token` (chỉ dùng cho bootstrap), `gitlab_jenkins_token` (group token `read_repository`, Reporter — Jenkins clone), `gitlab_netci_token` (group token `api`, Developer — pipeline designer của netCI). Hai group token hết hạn sau 90 ngày: xoá file rồi chạy lại `infra/corp/gitlab/bootstrap.sh` để cấp token mới.
  - Harbor: `harbor_admin_password`, `harbor_db_password`, `harbor_robot_name`, `harbor_robot_secret`
  - Jenkins: `jenkins_admin_password` (người vận hành), `jenkins_netci_sa_password` và `jenkins_netci_sa_token` (service account `netci-sa` của netCI: chỉ có quyền Overall/Read và Job Build/Cancel/Configure/Create/Discover/Read, không có Administer).
  - S3 (SeaweedFS): `s3-access-key`, `s3-secret-key`, `s3.json`
  - Velero: `velero-credentials`, `velero-repo-password`
  - Chữ ký số cosign: `.netci-gate/jenkins/secrets/NETCI_COSIGN_PRIVATE_KEY` (dùng chung với live lab).

> [!IMPORTANT]
> **Khóa Kopia repository (`velero-repo-password`):** Đây là chiếc chìa khóa duy nhất để giải mã và đọc lại toàn bộ dữ liệu backup volume `JENKINS_HOME` trên S3. Nếu xảy ra thảm họa mất toàn bộ máy hoặc hỏng cụm, nếu không có khóa này thì toàn bộ backup đều vô giá trị. **Bắt buộc phải lưu giữ một bản sao của file này bên ngoài máy (escrow)** trong kho quản lý bí mật tập trung.

---

## 2. Khởi động / sau khi reboot máy

### Lệnh khởi động
```bash
scripts/corp/up.sh
```
Script có tính chất lũy kế an toàn (idempotent): tự động nhận diện và bỏ qua các thành phần đã tồn tại hoặc đang chạy ổn định.

### Hai thành phần KHÔNG tự hồi phục sau khi reboot máy
1. **Container haproxy load balancer của cụm HA kind (`netci-corp-external-load-balancer`):** Do kind khởi tạo container này không có restart policy tồn tại qua lần khởi động máy, sau reboot mọi lệnh `kubectl` đều bị từ chối kết nối (`connection refused`) và các worker node báo `NotReady`. Lệnh xử lý:
   ```bash
   docker update --restart unless-stopped netci-corp-external-load-balancer
   docker start netci-corp-external-load-balancer
   ```
2. **Các container của Harbor:** Sau reboot, các container của Harbor khởi động trước container ghi syslog `harbor-log`, khiến chúng thoát với mã 128 (`exit 128`) và không tự thử lại. Lệnh xử lý:
   ```bash
   cd .netci-gate/corp/harbor-installer/harbor && sudo -n docker compose up -d
   ```
*(Cả hai thao tác trên đã được tích hợp tự động trong `scripts/corp/up.sh`).*

### Điều kiện tiên quyết trên máy chủ
Hệ thống yêu cầu `fs.inotify.max_user_instances >= 1024` (cấu hình tại `/etc/sysctl.d/99-netci-kind.conf`). Nếu để mặc định của kernel (128), pod trong cụm 6 node sẽ bị crash-loop. Ngoài ra, người dùng cần có quyền `sudo -n` để chạy installer của Harbor.

### Dấu hiệu nhận biết thành công
- `scripts/corp/up.sh` chạy hoàn tất và in dòng thông báo:
  `[HH:MM:SS] up. scripts/corp/status.sh shows the state; scripts/corp/jenkins_failover.sh runs the drill.`
- Không có lỗi dừng đột ngột giữa chừng.

---

## 3. Kiểm tra sức khoẻ hằng ngày

### Lệnh kiểm tra
```bash
scripts/corp/status.sh
```
Đây là script chỉ đọc (read-only), không làm thay đổi trạng thái của bất kỳ dịch vụ nào.

### Dấu hiệu hệ thống khỏe mạnh (Healthy)
- **HTTP status của các dịch vụ cốt lõi:**
  - `GitLab`: Trả về `200` tại `http://172.17.0.1:8929/users/sign_in`.
  - `Harbor`: Trả về `200` tại `http://172.17.0.1:8930/api/v2.0/ping`.
  - `S3`: Trả về `403` tại `http://172.17.0.1:8333/`.
    > [!NOTE]
    > **HTTP 403 trên S3 là trạng thái hoàn toàn bình thường và khỏe mạnh**: Endpoint SeaweedFS S3 từ chối mọi request ẩn danh không có chữ ký authentication AWS hợp lệ.
- **Trạng thái container:** Các container `netci-corp-s3`, `netci-corp-gitlab`, `harbor-core`, `registry` đều ở trạng thái `Up` (hoặc `healthy`).
- **Trạng thái cụm Kubernetes:**
  - Cả 6 node (`control-plane`, `control-plane2`, `control-plane3`, `worker`, `worker2`, `worker3`) đều ở trạng thái `Ready`.
  - Worker node có nhãn phân bổ đúng: `worker` (`netci.io/pool=ci`), `worker2` (`pool=ci`, `jenkins-controller=eligible`), `worker3` (`pool=platform`, `jenkins-controller=eligible`).
- **Jenkins Controller Pod:** Pod `jenkins-0` trong namespace `jenkins` có trạng thái `Running`, `Ready 1/1`, chạy trên một trong hai node mang nhãn `jenkins-controller=eligible` (`worker2` hoặc `worker3`).
- **Velero Backups:** Lệnh hiển thị các bản backup gần nhất có trạng thái `Completed`, 0 errors, 0 warnings.
- **Kiểm tra truy cập Jenkins UI (khi cần):**
  ```bash
  kubectl --context kind-netci-corp -n jenkins port-forward svc/jenkins 18089:8080
  ```
  Truy cập `http://127.0.0.1:18089/login`, đăng nhập tài khoản `admin` với mật khẩu lưu trong `.netci-gate/corp/jenkins_admin_password`.

---

## 4. Backup

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
- **Kiểm tra trạng thái backup:**
  ```bash
  .netci-gate/corp/bin/velero --kubecontext kind-netci-corp backup get <tên-backup>
  ```
  Hoặc kiểm tra trường phase qua JSON:
  ```bash
  .netci-gate/corp/bin/velero --kubecontext kind-netci-corp backup get <tên-backup> -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"]["phase"])'
  ```
- **Kiểm tra BackupStorageLocation đích:**
  ```bash
  .netci-gate/corp/bin/velero --kubecontext kind-netci-corp backup describe <tên-backup> | grep "Backup Storage Location"
  ```
  Backup phải được ghi vào BSL `jenkins-s3` (bucket `netci-jenkins-backups`, repository mã hoá bằng khoá riêng). BSL `default`, trỏ vào bucket `velero` cũ (khoá mặc định công khai), chỉ còn ở chế độ `ReadOnly` cho tới khi bị huỷ. Kiểm tra danh sách BSL:
  ```bash
  .netci-gate/corp/bin/velero --kubecontext kind-netci-corp backup-location get
  ```
- **Dấu hiệu nhận biết thành công:** Cột `STATUS` hiển thị `Completed`, `ERRORS: 0`, `WARNINGS: 0`, và BSL ở trạng thái `Available`.

---

## 5. Failover Jenkins (planned and unplanned)

Quy trình chuyển đổi dự phòng controller Jenkins được tự động hóa qua script:
```bash
scripts/corp/jenkins_failover.sh [--planned] [--no-marker]
```

### Các kịch bản thực hiện
1. **Diễn tập phục hồi (Drill):**
   ```bash
   scripts/corp/jenkins_failover.sh
   ```
   Script tạo một job đánh dấu (`dr-marker`) trên Jenkins, chạy build #N, tạo backup tức thì, cô lập node cũ (`cordon`), xóa namespace `jenkins`, khôi phục từ backup sang node eligible còn lại, và xác minh build #N vẫn tồn tại nguyên vẹn.
2. **Chuyển có kế hoạch (Planned Failover):**
   ```bash
   scripts/corp/jenkins_failover.sh --planned
   ```
   Tự động chụp một bản backup mới nhất trước khi chuyển (`planned-<timestamp>`), xóa namespace và restore sang node kia.
3. **Mất node đột xuất (Unplanned Failover - khi node chết thật):**
   ```bash
   scripts/corp/jenkins_failover.sh --no-marker
   ```
   Script tự động tìm bản backup `Completed` mới nhất trên hệ thống và tiến hành khôi phục.

### Thứ tự các bước bắt buộc khi failover
1. Cordon worker node cũ: `kubectl --context kind-netci-corp cordon <node-hỏng>`.
2. Xóa toàn bộ namespace `jenkins`: `kubectl --context kind-netci-corp delete namespace jenkins --timeout=180s`.
   > [!WARNING]
   > Bắt buộc phải xóa sạch namespace bao gồm StatefulSet và PVC. Cơ chế restore của Kopia/Velero chỉ giải nén dữ liệu volume vào pod do chính Velero tái tạo; nếu để StatefulSet tự tạo pod thì pod sẽ khởi động trên một volume rỗng.
3. Khôi phục từ bản backup `Completed`:
   ```bash
   .netci-gate/corp/bin/velero --kubecontext kind-netci-corp restore create <tên-restore> --from-backup <tên-backup> --wait
   ```
4. Đợi pod khởi động trên node eligible còn lại:
   ```bash
   kubectl --context kind-netci-corp -n jenkins wait --for=condition=ready pod/jenkins-0 --timeout=600s
   ```
5. Sau khi node cũ hồi phục, bỏ cô lập: `kubectl --context kind-netci-corp uncordon <node-cũ>`.

### Ý nghĩa RTO và RPO
- **RTO (Recovery Time Objective):** Thời gian từ lúc sự cố xảy ra/xóa namespace đến khi Jenkins controller mới khởi động xong và tiếp nhận HTTP request. Trong bài kiểm thử thực tế lab ghi nhận **RTO = 84 giây**.
- **RPO (Recovery Point Objective):** Độ lệch dữ liệu tính bằng thời gian từ bản backup được phục hồi đến thời điểm sự cố. Với diễn tập hoặc planned failover, RPO ~ 1 giây (do backup ngay trước khi chuyển). Với sự cố mất node đột xuất, RPO tối đa là 15 phút (chu kỳ schedule).
- **Số phận của các build đang chạy (in-flight builds):** Do kiến trúc luồng JVM của Jenkins, các build đang chạy dở khi controller chết sẽ bị mất. netCI Reconciler sẽ phát hiện và cập nhật trạng thái các build này là `FAILED` (tuyệt đối không báo `SUCCEEDED` - chống false green). Người dùng có thể an toàn kích hoạt chạy lại (retry).

### Xử lý khi bản backup rơi vào `PartiallyFailed`
Nếu backup gần nhất có trạng thái `PartiallyFailed`, script `jenkins_failover.sh` sẽ **từ chối khôi phục** (`backup ... is PartiallyFailed, not Completed`).
> [!CAUTION]
> **TUYỆT ĐỐI KHÔNG ÉP RESTORE (`do not force it`) từ bản PartiallyFailed.** Trạng thái này thường báo hiệu quá trình upload volume bị ngắt quãng giữa chừng (ví dụ SeaweedFS bị OOM). Cố tình khôi phục từ bản này sẽ khiến `JENKINS_HOME` bị thiếu thư mục `secrets/` hoặc hỏng cấu trúc XML, gây mất dữ liệu không thể cứu vãn. Người vận hành phải tìm bản backup `Completed` trước đó để phục hồi.

---

## 6. Xoay khoá repository (Key rotation)

Quy trình xoay khóa Kopia repository tuân thủ nghiêm ngặt 7 bước tuần tự từ tài liệu nghiên cứu kỹ thuật (`docs/research/velero-backup-hardening.md`, mục 7). Không sử dụng lệnh `kopia repository change-password` vì master key cũ đã bị lộ theo mật khẩu mặc định công khai của Velero.

Vì Velero chỉ có **một** khóa repository chung cho toàn bộ cụm, việc đổi secret sẽ làm mọi repository cũ không đọc được nữa (kể cả bản rollback). Thứ tự dưới đây đảm bảo cụm luôn có bản backup khôi phục được và biết rõ cách quay lại:

1. **Điểm rollback bằng khoá cũ:** `velero schedule pause jenkins-backup`, rồi `velero backup create pre-rekey-<ts> --from-schedule jenkins-backup --wait`; bản này phải `Completed`, 0 lỗi. (`--from-schedule` để có hook `sync` và file-system backup như lịch.)
2. **Kho mới:** Tạo bucket mới (`netci-jenkins-backups`); khoá mới sinh ngẫu nhiên, lưu **ngoài cụm** (`.netci-gate/corp/velero-repo-password`, quyền 600), sau này là secret manager.
3. **Chuyển:** Áp secret `velero-repo-credentials` với khoá mới; tạo BSL mới trỏ bucket mới và đặt làm mặc định; chuyển BSL cũ sang `ReadOnly`; restart `velero` và `node-agent`.
4. **Chứng minh, không giả định:** `velero backup create rekey-verify --from-schedule jenkins-backup --wait` phải `Completed`; rồi `scripts/corp/jenkins_failover.sh` phải PASS -- restore được bằng khoá mới là bằng chứng duy nhất khoá mới dùng được. Bước này **bắt buộc**. Nếu thất bại: đặt lại secret về khoá cũ, BSL cũ về `ReadWrite` + mặc định, restart; điểm rollback ở bước 1 restore được như trước.
5. **Bật lại lịch:** (`velero schedule unpause jenkins-backup`).
6. **Huỷ dữ liệu cũ ngay khi bước 4 PASS**, không giữ thêm: mọi object trong bucket cũ giải mã được bằng một hằng số công khai, giữ lại là giữ nguyên rủi ro. Xoá BSL cũ và bucket cũ.
7. **Least privilege sau cùng:** Giới hạn identity của Velero trong `s3.json` vào bucket mới (`Read/Write/List/Tagging:netci-jenkins-backups`, bỏ `Admin`), restart SeaweedFS, kiểm tra `velero backup-location get` còn `Available`.

*(Hiện trạng lab ngày 2026-09-26: bước 1–5 đã xong. `rekey-verify-1605` Completed vào `jenkins-s3`; diễn tập failover restore từ repository mới PASS (build đánh dấu #4, RTO 63 s). Bước 6 — xoá BSL `default` và bucket `velero` — và bước 7 đang chờ người vận hành cho phép, vì đây là thao tác xoá dữ liệu.)*

---

## 7. Phát hành phiên bản shared library mới

### Lệnh phát hành
```bash
LIB_TAG=netci-0.x.y bash infra/corp/gitlab/bootstrap.sh
```

### Các nguyên tắc quản trị phiên bản
- **Tags không bao giờ được dịch chuyển (`tags are never moved`):** Một khi tag đã phát hành lên GitLab, tuyệt đối không dịch chuyển tag sang commit khác. Dịch chuyển tag sẽ âm thầm làm thay đổi mã nguồn thư viện mà tất cả các job đang ghim phiên bản đó thực thi. Nếu có thay đổi, phải nâng giá trị `LIB_TAG` lên phiên bản mới.
- **Main không bao giờ được force push (`main is never force-pushed`):** Nhánh `main` của thư viện được bảo vệ (protected). Mỗi phiên bản mới là một commit mới nằm trên `main`, gắn tag có chú thích (`git tag -a`).

### Cập nhật cấu hình sau khi phát hành
Sau khi phát hành tag mới trên GitLab:
1. Cập nhật phiên bản mặc định trong cấu hình JCasC của Jenkins (`infra/corp/jenkins/values.yaml` tại `unclassified.globalLibraries.libraries[0].defaultVersion: netci-0.x.y`).
2. Cập nhật cấu hình netCI platform (`deploy/helm/netci-platform/examples/values-lab-corp.yaml` tại `jenkins.sharedLibrary: netci-shared-library@netci-0.x.y`).

### Dấu hiệu nhận biết thành công
- Script xuất thông báo:
  `gitlab: group platform, netci-shared-library@netci-0.x.y, payments-api`
- Nếu tag đã tồn tại từ trước, script dừng và từ chối ghi đè:
  `gitlab: netci-0.x.y already exists; a released tag is not moved (bump LIB_TAG)`

---

## 8. Sự cố thường gặp

Chỉ tổng hợp các sự cố đã được ghi nhận trong tài liệu nguồn và thực nghiệm lab:

### 1. SeaweedFS bị tràn bộ nhớ (SeaweedFS OOM)
- **Hiện tượng:** Bản backup Velero bị dừng đột ngột, Kopia báo lỗi `connection reset`, Velero đánh dấu volume backup là `Canceled` hoặc backup rơi vào trạng thái `PartiallyFailed`.
- **Nguyên nhân:** Mức giới hạn RAM 512 MiB trước đây bị vượt qua khi Kopia upload song song nhiều khối dữ liệu, tiến trình all-in-one của SeaweedFS đệm dữ liệu dẫn đến bị Linux cgroup OOM-kill.
- **Cách xử lý:** Đảm bảo `mem_limit: 1g` và đặt biến môi trường `GOMEMLIMIT: 800MiB` trong file `infra/corp/seaweedfs/docker-compose.yml`, sau đó khởi động lại:
  ```bash
  docker compose -f infra/corp/seaweedfs/docker-compose.yml up -d
  ```

### 2. Harbor container thoát với mã 128 sau reboot (Harbor exit 128 after reboot)
- **Hiện tượng:** Sau khi reboot máy, gọi `curl http://172.17.0.1:8930/api/v2.0/ping` bị lỗi, kiểm tra container Harbor thấy dừng với exit code 128.
- **Nguyên nhân:** Khi khởi động máy, các container dịch vụ của Harbor khởi động trước container syslog `harbor-log`, khiến chúng không thể gửi log và thoát với mã 128 mà docker compose không tự thử lại.
- **Cách xử lý:** Khởi động lại toàn bộ stack Harbor với quyền root:
  ```bash
  cd .netci-gate/corp/harbor-installer/harbor && sudo -n docker compose up -d
  ```

### 3. Mất kết nối API cụm kind sau reboot (kind load balancer after reboot)
- **Hiện tượng:** Sau khi khởi động lại máy, mọi lệnh `kubectl` báo lỗi kết nối từ chối (`connection refused`) và các worker node hiển thị trạng thái `NotReady`.
- **Nguyên nhân:** Container haproxy `netci-corp-external-load-balancer` (đứng trước 3 control-plane nodes) do kind tạo không có restart policy tồn tại qua host reboot.
- **Cách xử lý:** Cập nhật restart policy và khởi động lại container:
  ```bash
  docker update --restart unless-stopped netci-corp-external-load-balancer
  docker start netci-corp-external-load-balancer
  ```

### 4. Lỗi giới hạn inotify của hệ điều hành (inotify limit)
- **Hiện tượng:** Các pod chạy trên cụm Kubernetes liên tục rơi vào trạng thái `CrashLoopBackOff`.
- **Nguyên nhân:** Giá trị mặc định `fs.inotify.max_user_instances` của Linux kernel là 128, không đủ cho số lượng inotify watcher của cụm kind 6 node.
- **Cách xử lý:** Cấu hình tham số kernel tối thiểu 1024 trong file `/etc/sysctl.d/99-netci-kind.conf`:
  ```bash
  sudo sysctl -w fs.inotify.max_user_instances=1024
  # Kiểm tra lại:
  sysctl -n fs.inotify.max_user_instances
  ```

### 5. Lỗi Hook "container not found" ngay sau khi restart (hook container not found -- retry)
- **Hiện tượng:** Lệnh backup thực hiện ngay sau khi pod Jenkins khởi động lại bị báo lỗi: hook `jenkins-quiesce` thất bại với thông báo container `jenkins` không tìm thấy (`container not found`), làm bản backup bị `PartiallyFailed`.
- **Nguyên nhân:** Pod `jenkins-0` mới được tái tạo hoặc đang trong quá trình chuyển trạng thái, container `jenkins` chưa kịp sẵn sàng để kubelet thực thi lệnh exec hook `sync`.
- **Cách xử lý:** Chờ pod `jenkins-0` chuyển hẳn sang trạng thái `Running` và `1/1 Ready`:
  ```bash
  kubectl --context kind-netci-corp -n jenkins wait --for=condition=ready pod/jenkins-0 --timeout=600s
  ```
  Sau khi pod đã Ready, tiến hành thử lại (retry) lệnh tạo backup:
  ```bash
  .netci-gate/corp/bin/velero --kubecontext kind-netci-corp backup create <tên-backup> --from-schedule jenkins-backup --wait
  ```
