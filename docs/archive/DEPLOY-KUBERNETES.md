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
  và API cùng origin. `/metrics` không được public: nginx của portal trả 404 cho
  `/api/metrics` (nếu không, proxy `/api/` sẽ chuyển tiếp nó). Prometheus scrape thẳng
  Service `…-api:8000/metrics` trong cụm.
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
| Plugin | `workflow-job`, `workflow-cps`, `pipeline-model-definition`, `pipeline-groovy-lib` (bản cũ: `workflow-cps-global-lib`), `credentials-binding`, `plain-credentials`, `timestamper`, `git`, `pipeline-utility-steps` (`readJSON` cho custom stage), `ws-cleanup` (`cleanWs`), `kubernetes`. Tuỳ chọn: `configuration-as-code` — chỉ cho so sánh drift và reload JCasC, build không cần. |
| Tài khoản dịch vụ | `Overall/Read`, `Job/Create`, `Job/Configure`, `Job/Read`, `Job/Build`, `Job/Cancel` (netCI huỷ queue item và dừng build khi run bị huỷ hoặc bị thay thế). **Không** cần `Job/Delete`. Drift cần thêm `Overall/SystemRead`, reload cần `Overall/Administer`; thiếu thì hai tính năng đó báo controller "unreachable", build vẫn chạy. |
| Folder (tuỳ chọn) | `jenkins.folder` (vd `platform/netci`, lồng nhau bằng `/`) khi tổ chức chỉ cấp `Job/Create` trong một folder. Folder **phải có sẵn** — netCI không tạo; thiếu thì lần tạo job đầu tiên báo lỗi nêu tên folder. Các quyền `Job/*` ở trên cấp trên folder đó. |
| Shared library | Global Pipeline Library tên `netci-shared-library`, trỏ tới repo chứa `jenkins/shared-library`. Nếu trỏ thẳng vào repo netCI, đặt *Library Path* = `jenkins/shared-library/`. Phiên bản được dùng **phải có** `resources/netci/tooling/` (từ bản 0.2). |
| Credential | Secret text `netci-cosign-key` chứa private key cosign. Với workload identity, credential `netci-pipeline-api-key` **không cần** tồn tại. Git riêng, registry có xác thực và key cosign có mật khẩu: xem "Git riêng, registry có xác thực" bên dưới. |
| Agent | Kubernetes cloud có pod template gắn label `netci-ephemeral`, chứa container `builder` có python3, docker/buildah, syft, trivy, cosign (xem `jenkins/agent-toolbox/`). |
| Mạng | Agent phải tới được địa chỉ callback của netCI (mục 5, `jenkins.callbackUrl`), registry, và repo git của ứng dụng. |

**Vì sao library phải mang theo công cụ:** trước bản 0.2, pipeline chạy
`scripts/netci_callback.py` và `templates/*/scripts/ci/*.sh` từ *repo được checkout*. Việc đó
chỉ chạy được vì mọi module trong lab đều build từ chính repo netCI. Một repo ứng dụng thật
không có những file đó, nên build hỏng ngay stage đầu. Giờ `vars/netciTooling.groovy` ghi công
cụ từ resources của library ra `WORKSPACE_TMP` — ngoài workspace, nên không lọt vào build context.

### Git riêng, registry có xác thực, TLS và transparency log (ADR-054)

Mặc định build chạy **ẩn danh** như trong lab (git server và registry của lab không đòi mật
khẩu). Với Jenkins/registry của công ty, tạo credential trên controller rồi đặt ID vào values;
netCI ghi các ID này vào script của job (như `cosignCredentialsId`), không bao giờ giữ secret:

| Values | Loại credential | Được bind ở đâu |
| :--- | :--- | :--- |
| `jenkins.gitCredentialsId` | Username with password (token HTTPS) | Checkout (GitSCM `credentialsId`) và fetch vào git mirror của cache dự án (binding `gitUsernamePassword` của plugin git, mật khẩu qua `GIT_ASKPASS`, không bao giờ nằm trong URL hay config của mirror). URL SSH không dùng được cho đường mirror. |
| `jenkins.registryCredentialsId` | Username with password cho `registry.pushHost` | Chỉ các stage nói chuyện với registry: Build (kéo base image), SBOM, Scan, Sign, Publish. `buildah login --password-stdin` tạo **một** file auth (umask 077) dưới `WORKSPACE_TMP`; buildah đọc qua `REGISTRY_AUTH_FILE`, cosign/syft/trivy đọc qua `DOCKER_CONFIG/config.json`. Xoá khi stage kết thúc, dù thành công hay không. **Không bao giờ** có trong build verify-only của fork. |
| `jenkins.cosignPasswordCredentialsId` | Secret text: mật khẩu của key cosign | Chỉ bước sign/attest, dưới dạng `COSIGN_PASSWORD`. Trống = key không mật khẩu (như lab). |

Base image và mirror Trivy DB phải nằm trong chính registry đó: build chỉ đăng nhập vào
`pushHost`. `pullHost` là nơi cụm/host kéo image; không bước build nào gọi tới nó.

Mỗi build netCI gửi thêm hai tham số, không còn để script tự đoán:

- `REGISTRY_TLS_VERIFY` = `false` **chỉ khi** `registry.allowHttp: true`, ngược lại `true`.
  `--allow-insecure-registry` của cosign đi theo nó; đặt cosign insecure trong khi TLS được
  kiểm tra sẽ bị từ chối.
- `COSIGN_TLOG_UPLOAD` = `true` khi `supplyChain.signatureRequireTlog: true` (mặc định của
  chart), kèm `COSIGN_REKOR_URL` = `supplyChain.rekorUrl`. **Phải nêu tên Rekor**: đòi tlog mà
  không có `rekorUrl` thì chart không render và netCI không khởi động, vì mặc định của cosign
  là Rekor công khai — nghĩa là công bố digest của mọi image nội bộ. Dùng Rekor riêng của công
  ty (và cho cosign trên agent lẫn trong image netCI tin public key của nó), hoặc chỉ ghi
  `https://rekor.sigstore.dev` khi thật sự chấp nhận công khai. Không có Rekor: đặt
  `signatureRequireTlog: false`.

**Build verify-only của fork (ADR-043) không đẩy gì lên registry:** SBOM đọc
`oci-archive` cục bộ, Trivy quét OCI layout giải nén từ archive đó; `push-image.sh` từ chối
chạy khi `NETCI_PUBLISH=false`. Nếu base image cần đăng nhập mới kéo được, build verify-only
sẽ **hỏng ở Build** (không có credential) — đó là chủ ý, không phải lỗi cấu hình.

### Kiểm tra trước khi cài

```bash
export JENKINS_API_TOKEN='...'          # token của tài khoản dịch vụ; không đưa vào tham số
python3 scripts/jenkins_preflight.py \
  --url https://jenkins.example.com --user netci-sa \
  --library netci-shared-library@<phiên-bản> \
  --cosign-credential netci-cosign-key --agent-label netci-ephemeral
  # chỉ khi có cấu hình tương ứng trong values:
  # --folder platform/netci --git-credential corp-git --registry-credential corp-harbor --cosign-password-credential corp-cosign-pw
```

Script chỉ đọc: không tạo, sửa hay chạy gì trên Jenkins. Mỗi mục in PASS/FAIL kèm cách sửa;
thoát 0 nghĩa là không mục nào FAIL. Mục nào tài khoản không có quyền đọc (ví dụ cấu hình toàn
cục) được báo FAIL kèm hướng dẫn kiểm tra bằng tay — không được coi là đạt.

| Mục | Mức | Kiểm tra gì |
| :--- | :--- | :--- |
| `reachable`, `crumb` | PASS/FAIL | controller trả lời, token xác thực được, cấp được CSRF crumb |
| `plugins`, `kubernetes plugin` | PASS/FAIL | đủ plugin bắt buộc ở bảng trên |
| `folder` | PASS/FAIL | chỉ khi có `--folder`: folder tồn tại và tài khoản thấy được |
| `permissions` | PASS/FAIL | liệt kê job và mở được trang *New Item* ở nơi netCI tạo job (folder, hoặc gốc) |
| `library`, `cosign credential`, `agents` | PASS/FAIL | library được cấu hình, credential ký tồn tại, có agent/cloud cho label |
| `jcasc plugin`, `drift/reload` | INFO | `configuration-as-code` có không; tài khoản đọc được JCasC không. Không bao giờ làm FAIL |
| `library version` | NOT CHECKED | ref trong `--library tên@ref` có tồn tại không — Jenkins chỉ resolve khi build nạp library, và kiểm tra bằng form validation cần quyền admin; ref sai làm build đầu tiên hỏng ở "Loading library" |
| `build/cancel` | NOT CHECKED | `Job/Build`, `Job/Cancel` — muốn chứng minh thì phải chạy và dừng một build thật |

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
| `jenkins-<id>-api-token` | có, mỗi controller một key | ví dụ `jenkins-a-api-token` cho controller `id: A`. Token của một **service account** chỉ có quyền Job (Build/Cancel/Configure/Create/Discover/Read) và Overall/Read, không phải admin. |
| `scm-gitlab-token` / `scm-github-token` | khi dùng designer hoặc SCM status | token nhóm/bot tối thiểu (GitLab: `api`, vai trò Developer). Cần kèm `scm.gitlabTokenInSecret: true`. |
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
  # folder: platform/netci            # khi job phải nằm trong một folder có sẵn
  # Mặc định là <externalUrl>/api. Khi agent chạy trong cùng cụm với netCI, dùng Service:
  # callbackUrl: http://netci-netci-platform-api.netci-system.svc:8000
  # Trống = ẩn danh như lab (mục 2, "Git riêng, registry có xác thực"):
  # gitCredentialsId: corp-git
  # registryCredentialsId: corp-harbor
  # cosignPasswordCredentialsId: corp-cosign-password

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
  # Project mà mọi artifact được đẩy vào: <pushHost>/apps/<image>. Harbor giữ repository
  # theo project có quyền riêng; tài khoản build chỉ nên được push vào đúng một project.
  # Do server quyết định (NETCI_REGISTRY_NAMESPACE); đường dẫn sai làm netCI dừng khởi động.
  namespace: apps
  # Registry dùng CA riêng của công ty: ConfigMap (key ca.crt) trong namespace của netCI.
  # API và worker tin CA đó bên cạnh CA hệ thống (SSL_CERT_DIR); allowHttp để false.
  caConfigMap: corp-ca

scm:
  gitlabUrl: https://gitlab.example.com
  # Khi dùng existingSecret: đặt true nếu Secret có key scm-gitlab-token (tương tự
  # githubTokenInSecret cho scm-github-token). Thiếu key thì netCI coi là "chưa cấu hình".
  gitlabTokenInSecret: true

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

**Nối vào Jenkins của công ty không chỉ là đổi URL.** Credential git/registry, key cosign có
mật khẩu, folder, pod template, mạng từ agent và transparency log đều phải được chuẩn bị;
preflight không kiểm tra hết. Làm theo [HUONG_DAN_DAU_NOI_JENKINS_THUC_TE.md](HUONG_DAN_DAU_NOI_JENKINS_THUC_TE.md).

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

**Chưa chạy thật trên Jenkins nào (ADR-054):** git riêng qua `gitCredentialsId`, registry có
xác thực qua `registryCredentialsId`, key cosign có mật khẩu, registry TLS với
`REGISTRY_TLS_VERIFY=true`, và upload lên Rekor. Đã chạy thật **ngoài Jenkins**, bằng chính các
script CI và đoạn `buildah login` của library, với buildah/syft/trivy/cosign thật và một
registry đòi mật khẩu: build verify-only quét được từ archive cục bộ và registry vẫn trống;
push ẩn danh bị từ chối; sau khi đăng nhập thì push, SBOM, scan theo digest và ký bằng key có
mật khẩu đều qua.
