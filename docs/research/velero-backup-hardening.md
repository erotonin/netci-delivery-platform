# Nghiên cứu Hardening Backup Velero v1.18 và Kopia Repository cho netCI

Tài liệu nghiên cứu kỹ thuật bảo mật và vận hành cho hệ thống backup Velero v1.18.3 (Kopia file-system uploader) lưu trữ trên SeaweedFS 4.47, hỗ trợ cho [ADR-055](../decisions/ADR-055-one-jenkins-controller-at-a-time-restored-from-backup.md) và [install.sh](../../infra/corp/velero/install.sh).

---

## 1. Mật khẩu Velero Backup Repository

- **Vị trí lưu trữ:** Mật khẩu repository được lưu trong một Kubernetes Secret tên là `velero-repo-credentials` tại namespace cài đặt Velero, dưới data key `repository-password` (được mã hóa base64) ([File System Backup](https://velero.io/docs/v1.18/file-system-backup/)).
- **Phạm vi (Scope) trong v1.18:** Trong Velero v1.18, mật khẩu này là duy nhất cho toàn bộ bản cài đặt Velero (one per Velero install) ([File System Backup](https://velero.io/docs/v1.18/file-system-backup/)). Cấu trúc CRD `BackupRepository` và `BackupStorageLocation` hiện chưa hỗ trợ trường tham chiếu secret hoặc mật khẩu riêng biệt cho từng BSL/BackupRepository ([backup_repository_types.go v1.18.3](https://github.com/vmware-tanzu/velero/blob/v1.18.3/pkg/apis/velero/v1/backup_repository_types.go): không có trường password/credential nào; [keys.go v1.18.3](https://github.com/vmware-tanzu/velero/blob/v1.18.3/pkg/repository/keys/keys.go): một khoá chung `EnsureCommonRepositoryKey`, mặc định `static-passw0rd`). Tất cả các backup repository do Velero quản lý trong cụm đều dùng chung secret này.
- **Quy định đổi mật khẩu theo tài liệu chính thức:** Tài liệu của Velero chỉ định người dùng có thể cập nhật `velero-repo-credentials` với mật khẩu tự chọn trước khi bản backup đầu tiên nhắm tới repository được tạo ([File System Backup](https://velero.io/docs/v1.18/file-system-backup/)). Tài liệu cảnh báo rõ: nếu cập nhật mật khẩu sau khi backup đầu tiên đã khởi tạo repository, Velero sẽ không thể kết nối tới các bản backup cũ ("Velero will not be able to connect to the older backups") ([File System Backup](https://velero.io/docs/v1.18/file-system-backup/)).
- **Hiện tượng xảy ra với BackupRepository hiện hữu khi Secret thay đổi:** Khi thay đổi mật khẩu trong `velero-repo-credentials`, tiến trình Kopia do Velero/node-agent kích hoạt sẽ dùng mật khẩu mới để mở format blob của repository cũ. Do sai khóa giải mã format blob, Kopia báo lỗi giải mã / xác thực; tài liệu chỉ nói Velero "will not be able to connect with the older backups" ([File System Backup](https://velero.io/docs/v1.18/file-system-backup/)); trạng thái cụ thể của CR `BackupRepository` khi đó: chưa xác nhận.

---

## 2. Cơ chế thay đổi mật khẩu của Kopia (`kopia repository change-password`)

- **Bản chất của `change-password`:** Kopia áp dụng cơ chế mã hóa phong bì (envelope encryption) ([Repository Encryption](https://kopia.io/docs/advanced/encryption/)). File cấu hình gốc `kopia.repository` (format blob) chứa cấu trúc `EncryptedRepositoryConfig` bao gồm khóa mã hóa dữ liệu gốc (data encryption master key). Khi chạy lệnh `kopia repository change-password`, Kopia dùng mật khẩu cũ mở format blob, sau đó chỉ mã hóa lại cấu trúc cấu hình này bằng khóa dẫn xuất từ mật khẩu mới và ghi đè format blob ([format_change_password.go#L21](https://github.com/kopia/kopia/blob/master/repo/format/format_change_password.go#L21): `ChangePassword` chỉ dẫn xuất khoá format mới từ mật khẩu mới; [content_format.go#L20](https://github.com/kopia/kopia/blob/master/repo/format/content_format.go#L20): `MasterKey` nằm trong cấu hình được bọc lại, không đổi). Khóa mã hóa dữ liệu gốc và toàn bộ các khối dữ liệu (content/pack blobs) đã lưu trữ không hề bị thay đổi hay tái mã hóa ([Repository Encryption](https://kopia.io/docs/advanced/encryption/)).
- **Mức độ an toàn khi mật khẩu cũ bị công khai:** Trong trường hợp của netCI, mật khẩu cũ là mật khẩu mặc định công khai của Velero và backup chứa bí mật có thể giải mã (`credentials.xml` và `secrets/master.key` của Jenkins). Nếu chỉ chạy `change-password`:
  1. Bất kỳ ai từng tiếp cận bucket hoặc lưu bản sao format blob cũ đều đã có thể suy ra master key.
  2. Các bản snapshot cũ hoặc các phiên bản object cũ (nếu bucket bật versioning) vẫn giữ format blob mã hóa bằng mật khẩu công khai.
  3. Toàn bộ dữ liệu backup cũ lẫn mới trong repo vẫn dùng chung master key đã bị lộ.
- **Kết luận:** Lệnh `change-password` **không đủ an toàn**. Bắt buộc phải khởi tạo một repository Kopia hoàn toàn mới (new repository) với master key ngẫu nhiên mới sinh, sau đó thực hiện full backup lại từ đầu ([Repository Encryption](https://kopia.io/docs/advanced/encryption/)).

---

## 3. Immutable Backups: S3 Object Lock và SeaweedFS

- **Khả năng hỗ trợ của Velero:** Velero chính thức tuyên bố **không hỗ trợ** sao lưu bất biến (immutability / WORM) trên object storage ([Backup Reference - Cannot support backup data immutability](https://velero.io/docs/v1.18/backup-reference/#cannot-support-backup-data-immutability)). Velero yêu cầu quyền chỉnh sửa metadata trong suốt vòng đời của bản backup (chuyển trạng thái từ `Finalizing` sang `Completed`) và quyền xóa khi hết hạn; các rào cản immutability khiến các tác vụ này bị lỗi ([Backup Reference](https://velero.io/docs/v1.18/backup-reference/#cannot-support-backup-data-immutability), [Locking objects with Object Lock - Amazon S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)).
- **Xung đột với Kopia Maintenance & Deletion:** Kopia định kỳ chạy các job dọn dẹp kho lưu trữ (maintenance: snapshot compaction và garbage collection) để xóa các content blobs mồ côi hoặc các snapshot đã hết hạn TTL ([File System Backup](https://velero.io/docs/v1.18/file-system-backup/)). Nếu bucket bật S3 Object Lock (đặc biệt là Compliance mode), thao tác `DeleteObject` của Kopia sẽ bị từ chối (`AccessDenied`), khiến maintenance job thất bại và dung lượng storage phình to vô hạn định ([File System Backup](https://velero.io/docs/v1.18/file-system-backup/)).
- **Hỗ trợ trên SeaweedFS:** SeaweedFS 4.47 có hỗ trợ S3 Object Versioning và S3 Object Lock (cả Governance và Compliance mode) thông qua layer `s3api` ([Amazon S3 API - SeaweedFS Wiki](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API)). Tuy nhiên, do xung đột nêu trên từ phía kiến trúc của Velero và Kopia, **không nên bật Object Lock** cho bucket backup Velero trên SeaweedFS.

---

## 4. Phân quyền tối thiểu (Least-Privilege) S3 và SeaweedFS Scoping

- **Quyền IAM tối thiểu cho Velero:** Theo tài liệu chính thức của AWS plugin cho Velero, chính sách phân quyền tối thiểu bao gồm ([Velero Plugin for AWS README](https://github.com/vmware-tanzu/velero-plugin-for-aws/blob/main/README.md)):
  - Cấp độ Bucket (`arn:aws:s3:::<BUCKET>`):
    - `s3:ListBucket` (kiểm tra sự tồn tại và duyệt danh sách objects).
  - Cấp độ Objects (`arn:aws:s3:::<BUCKET>/*`):
    - `s3:GetObject`
    - `s3:DeleteObject`
    - `s3:PutObject`
    - `s3:AbortMultipartUpload`
    - `s3:ListMultipartUploadParts`
    - `s3:PutObjectTagging` (nếu dùng tính năng tagging).
- **Cơ chế giới hạn quyền theo bucket trong SeaweedFS (`s3.json`):**
  Trong SeaweedFS, quyền S3 được định nghĩa trong file cấu hình `s3.json` (truyền qua cờ `-config` của lệnh `weed s3`) ([Amazon S3 API - SeaweedFS Wiki](https://github.com/seaweedfs/seaweedfs/wiki/Amazon-S3-API)). Các thao tác cơ bản gồm: `Read`, `Write`, `List`, `Tagging`, `Admin`. Để giới hạn quyền cho một identity vào duy nhất bucket `velero`, SeaweedFS sử dụng cú pháp gắn tên bucket kèm dấu hai chấm sau action. Wiki không mô tả cú pháp này; nó được xác nhận trong source 4.47 ([auth_credentials.go#L1907](https://github.com/seaweedfs/seaweedfs/blob/4.47/weed/s3api/auth_credentials.go#L1907): `limitedByBucket := string(action) + ":" + bucket`):
  ```json
  {
    "identities": [
      {
        "name": "velero-user",
        "credentials": [
          { "accessKey": "velero-access", "secretKey": "velero-secret" }
        ],
        "actions": ["Read:velero", "Write:velero", "List:velero", "Tagging:velero"]
      }
    ]
  }
  ```
  *(Cú pháp tiền tố sâu hơn kiểu `Read:bucket/path/*` qua trường `resources`: chưa xác nhận độ ổn định trên SeaweedFS 4.47).*

---

## 5. Lưu trữ khóa Repository ngoài cụm (Escrow)

Mật khẩu Kopia repository lưu trong Kubernetes Secret (`velero-repo-credentials`) sẽ bị hủy diệt hoàn toàn nếu xảy ra thảm họa mất toàn bộ cụm (etcd hỏng, thảm họa hạ tầng, xóa nhầm namespace/cụm) ([File System Backup](https://velero.io/docs/v1.18/file-system-backup/)). Khi đó, dù dữ liệu trên S3 vẫn còn, quản trị viên không thể dựng cụm mới và giải mã backup nếu không giữ bản sao mật khẩu bên ngoài cụm ([File System Backup](https://velero.io/docs/v1.18/file-system-backup/)). Trong thực tế vận hành enterprise, khóa repository được lưu trữ ủy thác (escrow) trong các hệ thống quản lý bí mật bên ngoài như HashiCorp Vault, AWS Secrets Manager, hoặc giải pháp KMS chuyên dụng, sau đó được tự động đồng bộ vào cụm thông qua External Secrets Operator (ESO) hoặc quy trình GitOps bootstrap ([External Secrets Operator Documentation](https://external-secrets.io/)).

---

## 6. Cấu hình TLS với Private CA giữa Velero và Private S3 Endpoint

- **Cờ `--cacert` khi cài đặt:** Khi chạy `velero install`, có thể truyền đường dẫn bundle CA tự ký/nội bộ qua cờ `--cacert <path-to-ca-bundle>` để Velero tự động nạp cấu hình chứng chỉ tin cậy khi kết nối S3 endpoint ([Using Velero with a storage provider secured by a self-signed certificate](https://velero.io/docs/v1.18/self-signed-certificates/)).
- **Cấu hình trên `BackupStorageLocation` (`caCert` vs `caCertRef`):**
  - `caCert` (Deprecated): Nhúng trực tiếp chuỗi chứng chỉ CA đã mã hóa Base64 vào trường `spec.objectStorage.caCert`. Phương thức này đã bị đánh dấu deprecated và sẽ bị loại bỏ ([BackupStorageLocation](https://velero.io/docs/v1.18/api-types/backupstoragelocation/)).
  - `caCertRef` (Khuyến nghị): Tham chiếu tới một Kubernetes Secret chứa bundle chứng chỉ CA trong cùng namespace với BSL, giúp quản lý và xoay vòng chứng chỉ dễ dàng hơn:
    ```yaml
    apiVersion: velero.io/v1
    kind: BackupStorageLocation
    metadata:
      name: default
      namespace: velero
    spec:
      provider: aws
      objectStorage:
        bucket: velero
        caCertRef:
          name: private-s3-ca-cert
          key: ca.crt
      config:
        s3Url: https://s3.corp.netci.internal:8333
        s3ForcePathStyle: "true"
        region: us-east-1
    ```
  - *Lưu ý:* `caCert` và `caCertRef` loại trừ lẫn nhau (mutually exclusive), không được cấu hình cả hai cùng lúc ([BackupStorageLocation](https://velero.io/docs/v1.18/api-types/backupstoragelocation/)).

---

## 7. Khuyến nghị cho netCI

### Phân loại hạng mục
- **Bắt buộc (Required):**
  1. Khởi tạo repository Kopia mới với master key ngẫu nhiên độc lập (không dùng `change-password` vì master key cũ đã bị lộ).
  2. Lưu trữ bản sao mật khẩu mới an toàn ngoài cụm (escrow tại `.netci-gate/corp/velero-repo-password` và quản lý trong secret store).
  3. Áp dụng least-privilege trên SeaweedFS `s3.json` (`Read:velero-v2`, `Write:velero-v2`, `List:velero-v2`, `Tagging:velero-v2`).
  4. Thực hiện theo quy trình xoay khóa tuần tự, đảm bảo luôn có ít nhất một bản backup hợp lệ sẵn sàng phục hồi.
- **Tùy chọn / Đề xuất nâng cao (Optional):**
  1. Triển khai TLS 1.3 với private CA (dùng `caCertRef`): cần thiết khi đưa ra môi trường production thực tế; lab hiện tại chạy HTTP trên docker bridge nội bộ.
  2. **Không áp dụng** S3 Object Lock tại thời điểm này để tránh lỗi Kopia GC maintenance.
  3. Tích hợp External Secrets Operator (ESO) khi netCI có hạ tầng Vault/KMS tập trung.

### Quy trình xoay khoá (đã đối chiếu với lab `netci-corp`)

Vì Velero chỉ có **một** khoá repository cho toàn cụm, đổi secret làm mọi repository cũ không
đọc được nữa, kể cả bản rollback. Thứ tự dưới đây giữ cho cụm luôn có một bản backup
restore được, và biết rõ cách quay lại.

1. **Điểm rollback bằng khoá cũ.** `velero schedule pause jenkins-backup`, rồi
   `velero backup create pre-rekey-<ts> --from-schedule jenkins-backup --wait`; bản này phải
   `Completed`, 0 lỗi. (`--from-schedule` để có hook `sync` và file-system backup như lịch.)
2. **Kho mới.** Tạo bucket mới (`netci-jenkins-backups`); khoá mới sinh ngẫu nhiên, lưu **ngoài
   cụm** (`.netci-gate/corp/velero-repo-password`, quyền 600), sau này là secret manager.
3. **Chuyển.** Áp secret `velero-repo-credentials` với khoá mới; tạo BSL mới trỏ bucket mới và
   đặt làm mặc định; chuyển BSL cũ sang `ReadOnly`; restart `velero` và `node-agent`.
4. **Chứng minh, không giả định.** `velero backup create rekey-verify --from-schedule
   jenkins-backup --wait` phải `Completed`; rồi `scripts/corp/jenkins_failover.sh` phải PASS --
   restore được bằng khoá mới là bằng chứng duy nhất khoá mới dùng được. Bước này **bắt buộc**.
   Nếu thất bại: đặt lại secret về khoá cũ, BSL cũ về `ReadWrite` + mặc định, restart; điểm
   rollback ở bước 1 restore được như trước.
5. **Bật lại lịch** (`velero schedule unpause jenkins-backup`).
6. **Huỷ dữ liệu cũ ngay khi bước 4 PASS**, không giữ thêm: mọi object trong bucket cũ giải mã
   được bằng một hằng số công khai, giữ lại là giữ nguyên rủi ro. Xoá BSL cũ và bucket cũ.
7. **Least privilege sau cùng.** Giới hạn identity của Velero trong `s3.json` vào bucket mới
   (`Read/Write/List/Tagging:netci-jenkins-backups`, bỏ `Admin`), restart SeaweedFS, kiểm tra
   `velero backup-location get` còn `Available`.
