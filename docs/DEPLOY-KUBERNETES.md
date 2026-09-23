# Triển khai netCI lên Kubernetes và nối vào Jenkins đang có

Tài liệu này mô tả đúng những gì chart `deploy/helm/netci-platform` làm, và đúng những gì
đã được chạy thật trong lab. Chỗ nào chưa được chạy thật thì ghi rõ là chưa (mục 9).

Mục tiêu: **cài netCI một lần, trỏ vào Jenkins sẵn có của tổ chức, và build được repo
ứng dụng thật** — không phải repo netCI, không phải ứng dụng mẫu.

---

## 1. Kiến trúc

```
 Trình duyệt ──► Ingress (một host) ──► netci-portal (nginx)
                                          ├─ /        : giao diện
                                          └─ /api/... : proxy tới netci-api (cùng origin)

 netci-api  ──► PostgreSQL (nguồn sự thật duy nhất)
     │     ──► Jenkins có sẵn: tự tạo/cập nhật job netci-<application-id>, trigger build
     │     ──► Temporal: khởi chạy workflow triển khai
     ▲
     │ callback (token riêng cho từng build)
 Jenkins agent ── dựng công cụ CI từ shared library, checkout repo ứng dụng,
                  test → build → SBOM → scan → ký → publish → gửi bằng chứng

 netci-worker ◄── Temporal (task queue riêng của bản cài) ──► Ansible / Helm ──► host / cụm đích
```

- **Ingress chỉ có một đường `/` tới portal.** Portal proxy `/api` sang API, nên trình duyệt
  và API cùng origin. `/metrics` không được public.
- **Migration** chạy trong initContainer của mỗi pod API. `scripts/migrate.py` giữ advisory
  lock nên nhiều pod cùng khởi động vẫn an toàn: pod đầu áp dụng, các pod sau thấy "up to date".
- **Mọi secret là file** dưới `/run/secrets/...`; không cái nào nằm trong biến môi trường.
  Worker chỉ nhận `cosign.pub` và secret của đích triển khai — không có DB, không có khoá ký token.

Đã chạy thật với: Kubernetes 1.34.0 (kind), Jenkins 2.541.1, Temporal Server 1.31.1,
PostgreSQL 16, Keycloak (OIDC). Đó là phiên bản **đã thử**, không phải phiên bản tối thiểu.

---

## 2. Phía Jenkins đang có cần gì

netCI không cần job dựng sẵn: nó tự tạo và cập nhật job qua API trước mỗi lần build.
Jenkins cần:

| Thứ cần có | Chi tiết |
| :--- | :--- |
| Plugin | `workflow-job`, `workflow-cps`, `pipeline-model-definition`, `pipeline-groovy-lib` (bản cũ: `workflow-cps-global-lib`), `credentials-binding`, `plain-credentials`, `timestamper`, `git`, `kubernetes` |
| Tài khoản dịch vụ | `Overall/Read`, `Job/Create`, `Job/Configure`, `Job/Read`, `Job/Build`, `Job/Cancel` (netCI dừng build khi run bị huỷ). **Không** cần `Job/Delete`. |
| Shared library | Global Pipeline Library tên `netci-shared-library`, trỏ tới repo chứa `jenkins/shared-library`. Nếu trỏ thẳng vào repo netCI, đặt *Library Path* = `jenkins/shared-library/`. Phiên bản được dùng **phải có** `resources/netci/tooling/` (từ bản 0.2). |
| Credential | Secret text `netci-cosign-key` chứa private key cosign. Với workload identity, credential `netci-pipeline-api-key` **không cần** tồn tại. |
| Agent | Kubernetes cloud có pod template gắn label `netci-ephemeral`, chứa container `builder` có python3, docker/buildah, syft, trivy, cosign (xem `jenkins/agent-toolbox/`). |
| Mạng | Agent phải tới được địa chỉ callback của netCI (mục 5, `jenkins.callbackUrl`), registry, và repo git của ứng dụng. |

**Vì sao library phải mang theo công cụ:** trước bản 0.2, pipeline chạy
`scripts/netci_callback.py` và `templates/*/scripts/ci/*.sh` từ *repo được checkout*. Việc đó
chỉ chạy được vì mọi module trong lab đều build từ chính repo netCI. Một repo ứng dụng thật
không có những file đó, nên build hỏng ngay stage đầu. Giờ `vars/netciTooling.groovy` ghi công
cụ từ resources của library ra `WORKSPACE_TMP` — ngoài workspace, nên không lọt vào build context.

### Kiểm tra trước khi cài

```bash
export JENKINS_API_TOKEN='...'          # token của tài khoản dịch vụ; không đưa vào tham số
python3 scripts/jenkins_preflight.py \
  --url https://jenkins.example.com --user netci-sa \
  --library netci-shared-library@<phiên-bản> \
  --cosign-credential netci-cosign-key --agent-label netci-ephemeral
```

Script chỉ đọc: không tạo, sửa hay chạy gì trên Jenkins. Mỗi mục in PASS/FAIL kèm cách sửa;
thoát 0 nghĩa là mọi mục đều đạt. Mục nào tài khoản không có quyền đọc (ví dụ cấu hình toàn
cục) được báo FAIL kèm hướng dẫn kiểm tra bằng tay — không được coi là đạt.

---

## 3. Image

```bash
DOCKER_BUILD_ARGS="--network host" scripts/build_images.sh harbor.example.com/netci 0.2.0
```

Ba image, đều build từ gốc repo:

| Image | Chứa thêm | Vì sao |
| :--- | :--- | :--- |
| `backend` | `scripts/migrate.py` + `backend/migrations`, `git`, `kubectl` 1.34, `cosign` 3.1.2 | migration khi cài; `git ls-remote` cho danh sách branch; build isolation và traffic router; verify chữ ký |
| `worker` | `deploy/ansible`, `deploy/helm/sample-kubernetes-app`, `helm` 3.21, `cosign` | playbook triển khai; chart workload chung cho module Kubernetes |
| `frontend` | template upstream nginx | địa chỉ API là cấu hình (`NETCI_API_UPSTREAM`), không đóng cứng |

`kubectl` và `helm` được tải kèm kiểm tra SHA-256 đã công bố.

---

## 4. Secret

Tạo trước, rồi đặt `existingSecret: netci-app`. Tên key phải **đúng như bảng** — chart trỏ
biến `*_FILE` tới từng file này.

| Key trong `netci-app` | Bắt buộc | Nội dung |
| :--- | :--- | :--- |
| `database-url` | có | `postgresql://netci:<mật-khẩu>@host:5432/netci?sslmode=require` |
| `workload-token-keys` | có | `k1:<chuỗi ngẫu nhiên ≥32 ký tự>` — khoá ký token cho build và deployment |
| `cosign.pub` | có | **public** key tương ứng với `netci-cosign-key` trên Jenkins. netCI không bao giờ cần private key. |
| `jenkins-<id>-api-token` | có, mỗi controller một key | ví dụ `jenkins-a-api-token` cho controller `id: A` |
| `dcim-api-token` | khi `dcim.provider` được đặt | token NetBox/DCIM |
| `build-cluster-kubeconfig` | khi `buildIsolation.mode: kubernetes` | kubeconfig của cụm build |
| `traffic-kubeconfig` | khi `traffic.router` được đặt | kubeconfig để điều phối ingress cho canary/blue-green |

```bash
kubectl -n netci-system create secret generic netci-app \
  --from-file=database-url=./database-url \
  --from-file=workload-token-keys=./workload-token-keys \
  --from-file=cosign.pub=./cosign.pub \
  --from-file=jenkins-a-api-token=./jenkins-a-api-token
```

Đích triển khai (worker), trong `netci-deploy-targets`: `id_ed25519`, `known_hosts`, và các
kubeconfig mà module khai báo bằng `kubeconfigRef`.

```bash
kubectl -n netci-system create secret generic netci-deploy-targets \
  --from-file=id_ed25519=./ansible_key --from-file=known_hosts=./known_hosts
```

Không có `existingSecret`, chart tự render Secret từ `secrets.*` — mọi giá trị đều `required`,
không có mặc định.

---

## 5. Values

Ví dụ đã chạy thật: `deploy/helm/netci-platform/examples/values-lab-kind.yaml`. Bản tối thiểu
cho một tổ chức:

```yaml
global:
  externalUrl: https://netci.example.com

image:
  registry: harbor.example.com/netci
  tag: 0.2.0

existingSecret: netci-app

jenkins:
  controllers:
    - {id: A, url: "https://jenkins.example.com", username: netci-sa, executors: 4}
  sharedLibrary: netci-shared-library@<phiên-bản có resources/netci/tooling>
  # Mặc định là <externalUrl>/api. Khi agent chạy trong cùng cụm với netCI, dùng Service:
  # callbackUrl: http://netci-netci-platform-api.netci-system.svc:8000

temporal:
  address: temporal-frontend.temporal.svc:7233
  taskQueue: netci-delivery        # mỗi bản cài một queue riêng

auth:
  mode: oidc
  oidc:
    issuer: https://sso.example.com/realms/netci
    roleMap: netci-admins=platform-admin,release-managers=reviewer,engineers=developer

registry:
  pushHost: harbor.example.com

dcim:                               # bắt buộc chọn một trong hai
  provider: netbox
  baseUrl: https://netbox.example.com/api
  # hoặc: provider: "" và requireRevalidation: false (không có inventory để đối chiếu)

deployTargets:
  existingSecret: netci-deploy-targets
  sshPrivateKeyFile: id_ed25519
  knownHostsFile: known_hosts
  inventory: |
    [docker_targets]
    app-prod-01 ansible_host=10.0.20.10 ansible_user=netci netci_become=true
    [all:vars]
    ansible_python_interpreter=/usr/bin/python3
```

**Đổi Jenkins = đổi `jenkins.controllers[].url` và `username`, cộng token trong secret.**
Mọi thứ khác về Jenkins là điều kiện ở mục 2, đã được preflight kiểm tra.

Chart **từ chối render** thay vì cài một netCI không làm được việc: thiếu `externalUrl`,
`image.registry`, địa chỉ Temporal, issuer OIDC, registry push; hoặc không có DCIM mà vẫn bật
`requireRevalidation` (khi đó mọi deployment đều bị chặn). Thông báo lỗi nêu đúng giá trị thiếu.

---

## 6. Cài và kiểm tra

```bash
helm upgrade --install netci deploy/helm/netci-platform -n netci-system --create-namespace -f my-values.yaml
kubectl -n netci-system rollout status deploy/netci-netci-platform-api
```

Hỏi netCI xem từng integration có trả lời không (image không có curl, nên dùng python):

```bash
kubectl -n netci-system exec deploy/netci-netci-platform-api -c api -- python -c \
  'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8000/readyz").read().decode())'
```

`/readyz` báo từng mục: `ci.healthyControllers`, `cd.pollers` (worker đang nghe đúng queue),
`cosign.version`, DB, khoá workload. `"ready": false` ở mục nào thì mục đó chưa nối được —
không phải lỗi cài. Probe của Kubernetes dùng `/livez` và `/healthz` (chỉ DB), **không** dùng
`/readyz`: nếu dùng, Jenkins khởi động lại sẽ kéo mọi pod API ra khỏi rotation.

---

## 7. Repo ứng dụng cần gì

Template `container-ci-cd-v1` đòi ở repo ứng dụng:

- `Dockerfile` ở gốc repo, hoặc ở thư mục con được truyền bằng build input `NETCI_APP_DIR`
  (đường dẫn tương đối, không có `..`).
- Tuỳ chọn: test `test*.py` (unittest). Không có thì kết quả là `skipped` — không phải `passed`.

Không cần bất kỳ file nào của netCI trong repo. Tên image do **server** quyết định từ tên
application; người gọi không đặt được, để module này không thể publish dưới tên module khác.

---

## 8. Lỗi thường gặp (đều đã gặp thật)

| Hiện tượng | Nguyên nhân |
| :--- | :--- |
| `NETCI_IMAGE_NAME was not supplied` | API cũ hơn library: image backend chưa có bản sửa 0.2. Build lại image. |
| `No such file: scripts/netci_callback.py` | Jenkins đang dùng library **trước** 0.2 (không có tooling). Trỏ `sharedLibrary` tới phiên bản mới. |
| `One or more variables have some issues with their values` | Biểu thức `?.` trong khối `environment {}` của declarative pipeline. Đã tránh trong library 0.2. |
| Pod portal không lên, log `host not found in upstream` | Image frontend cũ đóng cứng `netci-api:8000`. Dùng image 0.2 — chart truyền `NETCI_API_UPSTREAM`. |
| Mọi deployment bị từ chối vì DCIM | `requireRevalidation: true` mà không cấu hình DCIM. Chart 1.1 chặn trường hợp này ngay khi render. |

---

## 9. Đã và chưa kiểm chứng thật

**Đã chạy thật trong lab** (2026-09-23, cụm kind, Jenkins A/B của lab):
cài bằng Helm vào cơ sở dữ liệu trống; hai pod API migrate đồng thời (một áp dụng 27
migration, một chờ khoá); `/readyz` 200 với 2/2 controller Jenkins và poller Temporal;
đăng nhập OIDC qua API; tạo system và module cho repo `payments-api` **tách riêng**; netCI tự
tạo job trên Jenkins và cân tải giữa hai controller; build đủ 9 stage; image `payments-api`
xuất hiện trong registry với đúng digest; `cosign verify` đạt; worker trong cụm triển khai
qua SSH tới host đích và container trả về chính digest đó.

**Chưa chạy thật qua bản cài Kubernetes:** đăng nhập SSO trên trình duyệt qua portal của bản
kind (Keycloak lab gắn issuer `127.0.0.1`, pod không tới được địa chỉ đó — giới hạn mạng lab
một máy); runtime Kubernetes và systemd; `buildIsolation: kubernetes`; DCIM/NetBox; traffic
router (canary, blue/green); Ingress có TLS. Những mục này có code và test, nhưng chưa có một
lần chạy qua chính bản cài này.
