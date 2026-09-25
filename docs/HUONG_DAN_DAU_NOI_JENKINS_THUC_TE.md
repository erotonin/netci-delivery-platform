# Tích hợp netCI với Jenkins của công ty

Tài liệu này là **trình tự làm từ đầu đến cuối** để nối netCI vào một Jenkins đang chạy
của công ty, kèm lý do của từng bước và cách kiểm tra từng bước. Chi tiết cài chart (Secret,
values, image) nằm ở [DEPLOY-KUBERNETES.md](DEPLOY-KUBERNETES.md); ở đây chỉ nhắc lại phần
liên quan tới Jenkins.

> **Trạng thái thật (2026-09-25).** Mọi thứ dưới đây đã chạy với Jenkins **của lab** (2
> controller, agent là pod Kubernetes, git và registry ẩn danh qua HTTP). **Chưa lần nào chạy
> với Jenkins của một công ty.** Các mục được đánh dấu ⚠ là những thứ chỉ có test, chưa từng
> chạy thật — kiểm tra chúng ở bước 7 trước khi mở cho người dùng.

---

## 0. Kết quả rà soát trước khi tích hợp

Trước khi viết tài liệu này, toàn bộ phần netCI chạm vào Jenkins đã được rà lại từ code
(lời gọi REST, job được tạo, shared library, agent, callback, reconciler). Những lỗi sau
**đã được sửa** vì chúng sẽ hỏng hoặc không an toàn trên Jenkins công ty:

| Vấn đề | Hậu quả trên Jenkins công ty | Đã sửa trong |
| :--- | :--- | :--- |
| Pipeline không gắn credential nào cho git và registry | Repo private, Harbor có mật khẩu: build hỏng ngay | ADR-054, `7128373` |
| Registry luôn chạy `--tls-verify=false`, cosign `--allow-insecure-registry` | Bỏ qua TLS với registry thật | ADR-054 |
| Mật khẩu key cosign bị gắn cứng là rỗng | Key có mật khẩu (chuẩn của công ty) ký lỗi | ADR-054 |
| Build verify-only của PR từ fork vẫn **push image chưa ký** lên registry | Code chưa review từ bên ngoài lọt vào registry công ty | ADR-054 (đã kiểm chứng live: không push) |
| Key ký có thể bị archive khi bước Sign lỗi | Lộ private key qua artifact của build | ADR-054 |
| Build nằm trong hàng đợi Jenkins > 10 s thì không theo dõi và không huỷ được nữa | Jenkins có quiet period hoặc hết agent (chuyện thường) → run mồ côi | `6c4b18b` |
| Token callback nằm trên URL `buildWithParameters` | Lọt vào access log của Jenkins, proxy, và log của netCI | `6c4b18b` (đã kiểm chứng live) |
| Admission đếm cả hàng đợi chung của controller | Jenkins dùng chung với team khác → netCI không gửi được build nào | `6c4b18b` |
| Không hỗ trợ folder | Công ty thường chỉ cấp quyền tạo job trong một folder | `6c4b18b` |
| Reconciler đánh `failed` mọi run quá 1 giờ, kể cả run đang chờ và build còn chạy | Build dài bị đánh hỏng oan | `edcf4fd` |
| `agentLabel` cho chọn pod template bất kỳ | Lách cách ly "mỗi build một pod" | `edcf4fd` |
| Endpoint `ci-report` nhận token của build bất kỳ | Build app A ghi báo cáo cho app B | `edcf4fd` |
| Thiếu `NETCI_CALLBACK_URL` / danh sách controller thì âm thầm dùng giá trị của lab | Mọi callback đi tới `host.docker.internal` | `edcf4fd` (giờ dừng khởi động) |
| Probe drift gọi API cần quyền admin mỗi phút | Tài khoản ít quyền → báo drift giả mãi | `edcf4fd` (`jenkins.driftProbe: false`) |
| Đòi transparency log thì build upload lên **Rekor công khai** | Công bố digest mọi image nội bộ | `7128373` (phải khai `rekorUrl`) |
| Preflight thiếu 2 plugin pipeline thật sự dùng | Preflight báo đạt nhưng build hỏng | `6c4b18b` |

Gate sau các bản sửa: backend 1263 test + PostgreSQL, contract, pyflakes, `helm lint`,
`sync_shared_library --check`, frontend đều xanh.

---

## 1. Thông tin cần thu thập trước

Điền bảng này cùng admin Jenkins, team registry và team bảo mật. Mỗi dòng quyết định một
giá trị cấu hình ở các bước sau.

| # | Câu hỏi | Dùng ở |
| :-- | :--- | :--- |
| 1 | URL của từng controller (HTTPS), phiên bản Jenkins LTS | bước 2, 5 |
| 2 | Jenkins có Kubernetes cloud không? Cụm nào, namespace nào cho agent? | bước 2.5 |
| 3 | Được tạo job ở gốc hay chỉ trong một folder? Tên folder | bước 2.2 |
| 4 | Có được cấp `Overall/Administer` (hoặc `SystemRead`) cho tài khoản netCI không? | bước 2.2, 5 |
| 5 | Git server (GitLab/GitHub/Bitbucket), repo private hay public, đăng nhập bằng token HTTPS được không | bước 2.4 |
| 6 | Registry (Harbor…): hostname, TLS với CA nào, tài khoản push | bước 2.4, 2.6 |
| 7 | Chính sách ký: key cosign có mật khẩu không, ai giữ key, có Rekor nội bộ không | bước 3 |
| 8 | Chính sách Pod Security của cụm agent: có cho `allowPrivilegeEscalation: true` không | bước 2.5 |
| 9 | Build farm có ra internet không (Trivy DB, base image) | bước 2.6 |
| 10 | netCI được truy cập ở địa chỉ nào; agent pod có gọi được địa chỉ đó không | bước 2.6, 4 |

---

## 2. Phía Jenkins (admin Jenkins làm)

### 2.1 Phiên bản và plugin

Đã chạy với Jenkins **2.541.1 LTS (JDK 21)** và các plugin ở `jenkins/plugins.txt`.

| Plugin | Bắt buộc | Dùng cho |
| :--- | :--- | :--- |
| `workflow-job`, `workflow-cps`, `pipeline-model-definition`, `pipeline-groovy-lib` | có | job pipeline, cú pháp declarative, `@Library` |
| `kubernetes` | có | agent là pod, `container('builder')` |
| `git`, `git-client` | có | checkout, binding `gitUsernamePassword` |
| `credentials-binding`, `plain-credentials` | có | `withCredentials` cho key cosign, git, registry |
| `timestamper` | có | `timestamps()` |
| `pipeline-utility-steps` | có | `readJSON` cho custom stage |
| `ws-cleanup` | có | `cleanWs` khi agent không dùng một lần |
| `configuration-as-code` | không | chỉ cho probe drift và reload JCasC của netCI |

### 2.2 Tài khoản dịch vụ cho netCI

Tạo một user riêng (ví dụ `netci-sa`) và **API token** cho nó. Không dùng tài khoản admin
hay mật khẩu làm token như trong lab.

| Quyền | Vì sao |
| :--- | :--- |
| `Overall/Read` | health check, đọc hàng đợi |
| `Job/Create`, `Job/Configure` | netCI tự tạo job `netci-<application-id>` và **ghi lại cấu hình của job trước mỗi build** |
| `Job/Build`, `Job/Read` | chạy build, đọc trạng thái (reconciler) |
| `Job/Cancel` | huỷ build/queue item khi run bị huỷ hoặc bị thay thế (ADR-050) |
| **Không** `Job/Delete` | netCI không xoá job |
| `Overall/Administer` hoặc `Overall/SystemRead` — tuỳ chọn | chỉ cho probe drift và reload JCasC. Không cấp thì đặt `jenkins.driftProbe: false` |

**Folder:** nếu công ty chỉ cho tạo job trong một folder, tạo folder trước (netCI **không
tạo folder**), cấp các quyền `Job/*` ở trên *trong folder đó*, rồi đặt `jenkins.folder`
(ví dụ `platform/netci`).

**CSRF:** giữ crumb issuer bật. netCI lấy crumb và giữ cookie phiên.

Lưu ý khi vận hành: job do netCI tạo sẽ bị **ghi đè mỗi lần build**. Sửa tay trên UI sẽ mất —
mọi thay đổi đi qua netCI hoặc shared library.

### 2.3 Shared library

1. Đưa thư mục `jenkins/shared-library/` (có `vars/` và `resources/`) vào một repo git mà
   Jenkins đọc được. Có thể dùng repo riêng, hoặc chính repo netCI với *Library Path* =
   `jenkins/shared-library/`.
2. Đánh **tag cố định** cho mỗi phiên bản (ví dụ `netci-0.3.0`). Không để job đọc một
   branch trôi: một commit mới trên branch sẽ đổi mọi build ngay lập tức mà không ai duyệt.
3. Trong *Manage Jenkins → System → Global Trusted Pipeline Libraries*: tên
   `netci-shared-library`, retrieval Modern SCM git, credential đọc repo nếu repo private,
   bật **Allow default version to be overridden**.
   - Phải là library **global (trusted)**, không phải library cấp folder: library dùng
     `libraryResource` và vài API mà sandbox chặn đối với library không tin cậy.
4. Phía netCI đặt `jenkins.sharedLibrary: netci-shared-library@netci-0.3.0`.

Phiên bản library phải có `resources/netci/tooling/` (từ 0.2) và các thay đổi của ADR-054
(từ 0.3). Library của lab ở branch `netci-0.3` trên git server của lab là bản đã chạy thật.

### 2.4 Credential trên Jenkins

| ID (đổi được qua values) | Loại | Bắt buộc | Dùng ở |
| :--- | :--- | :--- | :--- |
| `netci-cosign-key` (`jenkins.cosignCredentialsId`) | Secret text: private key cosign (PEM) | có | chỉ bước Sign |
| `jenkins.cosignPasswordCredentialsId` | Secret text: mật khẩu của key | khi key có mật khẩu | chỉ Sign/attest, dưới dạng `COSIGN_PASSWORD` |
| `jenkins.gitCredentialsId` | Username with password (token HTTPS) | khi repo private | checkout và git mirror của cache |
| `jenkins.registryCredentialsId` | Username with password cho `registry.pushHost` | khi registry có xác thực | Build, SBOM, Scan, Sign, Publish; **không bao giờ** trong build của fork |
| credential cho Kubernetes cloud | Secret text: token của service account controller | có (nếu dùng cloud) | Jenkins tạo pod agent |
| `netci-pipeline-api-key` | — | **không cần** | chỉ chế độ legacy; production dùng token riêng mỗi build (ADR-015) |

Giới hạn hiện tại:
- Một credential git cho **mọi** repo, một credential registry cho **mọi** module. Chưa hỗ
  trợ credential riêng theo repo.
- Git mirror của cache chỉ dùng HTTPS với username/password, **không dùng SSH key**.
- ID credential chỉ được chứa `A-Z a-z 0-9 _ . -`. ID khác thì netCI dừng khởi động.

### 2.5 Kubernetes cloud và pod template

Copy từ `jenkins/casc/ephemeral-agent.yaml` và `infra/kind/jenkins-agent-rbac.yaml`:

1. **Namespace cho agent** (ví dụ `netci-build`) có 2 service account: `jenkins-controller`
   (credential của cloud) và `jenkins-agent` (`automountServiceAccountToken: false`), cùng
   Role/RoleBinding cho `pods`, `pods/log`, `pods/exec`.
2. **Cloud**: API URL và CA của cụm, `jenkinsUrl` mà pod gọi ngược lại được, bật
   **WebSocket** (không cần mở TCP 50000).
3. **Pod template** tên và label `netci-ephemeral`, dùng một lần (`idleMinutes 0`), gồm:
   - container `jnlp`: `jenkins/inbound-agent`, env `NETCI_BUILD_CONTAINER=builder` và
     `NETCI_AGENT_DISPOSABLE=true`;
   - container `builder`: image toolbox của netCI, `sleep 99d`, `runAsNonRoot` uid 1000.
4. **Image toolbox**: build từ `jenkins/agent-toolbox/` (`jenkins/scripts/build-agent-toolbox.sh`)
   rồi push vào registry công ty. Gồm buildah, python3, git, syft 1.51.0, trivy 0.73.0,
   cosign 3.1.2, Go 1.27.0. Không có Docker daemon, không mount docker socket.
5. **Pod Security**: container `builder` cần `allowPrivilegeEscalation: true`, vì buildah chạy
   rootless dùng setuid `newuidmap`. Namespace phải cho phép mức PSA `baseline`. Đây là điểm
   cần **team bảo mật chấp thuận**; nếu không được, cần một cách build khác (chưa có).
6. Chỉ các template được liệt kê trong `jenkins.allowedAgentLabels` mới có thể được một run
   chọn. Để trống thì mọi build dùng `jenkins.agentLabel`.

### 2.6 Mạng và mirror

Agent pod phải gọi ra được:

| Tới | Để làm gì |
| :--- | :--- |
| `jenkins.callbackUrl` của netCI (HTTPS) | báo stage, kết quả, evidence, SBOM |
| git server của ứng dụng | checkout |
| `registry.pushHost` | kéo base image, push, SBOM, scan, ký |
| Rekor (chỉ khi `signatureRequireTlog: true`) | upload chữ ký |

Không có internet thì cần:
- `registry.trivyDbRepository`: mirror `aquasec/trivy-db:2` vào registry công ty;
- `registry.buildBaseImage`: base image đã mirror. `Dockerfile` của ứng dụng phải có
  `ARG PYTHON_IMAGE`.

netCI (API) phải gọi được REST của từng controller. Worker của netCI phải gọi được registry
(xác minh chữ ký) và các đích deploy.

---

## 3. Chữ ký: key cosign và transparency log

1. Tạo cặp key **có mật khẩu**: `cosign generate-key-pair`.
2. Private key → credential `netci-cosign-key`; mật khẩu → credential của
   `jenkins.cosignPasswordCredentialsId`.
3. Public key (`cosign.pub`) → key `cosign.pub` trong Secret `netci-app`. netCI chỉ cần
   public key và không bao giờ giữ private key.
4. **Transparency log** — chọn một trong hai:
   - **Có Rekor nội bộ**: `supplyChain.signatureRequireTlog: true` và
     `supplyChain.rekorUrl: https://rekor.<công-ty>`. Cosign trên agent và trong image netCI
     phải tin public key của Rekor đó. ⚠ Chưa chạy thật.
   - **Không có Rekor**: `supplyChain.signatureRequireTlog: false`. Chữ ký vẫn được ký và
     kiểm tra bằng key; chỉ không có bản ghi trong log.

   Đòi tlog mà không khai `rekorUrl` thì chart không render: mặc định của cosign là **Rekor
   công khai**, nghĩa là công bố digest và danh tính ký của mọi image nội bộ. Chỉ ghi
   `https://rekor.sigstore.dev` khi thật sự chấp nhận điều đó.

---

## 4. Phía netCI (team platform làm)

Secret `netci-app` (DEPLOY-KUBERNETES.md mục 4): thêm `jenkins-<id>-api-token` cho mỗi
controller và `cosign.pub`.

Values liên quan tới Jenkins, ví dụ cho một công ty:

```yaml
global:
  environment: production
  externalUrl: https://netci.congty.vn

jenkins:
  controllers:
    - {id: A, url: "https://jenkins.congty.vn", username: netci-sa, executors: 4}
  sharedLibrary: netci-shared-library@netci-0.3.0
  folder: platform/netci                 # bỏ nếu được tạo job ở gốc
  callbackUrl: ""                        # trống = <externalUrl>/api; agent phải gọi được
  agentLabel: netci-ephemeral
  allowedAgentLabels: ""                 # trống = không run nào được tự chọn pod template
  cosignCredentialsId: netci-cosign-key
  cosignPasswordCredentialsId: netci-cosign-password
  gitCredentialsId: netci-git            # repo private
  registryCredentialsId: netci-harbor    # registry có xác thực
  admissionMaxQueue: 2                   # số build tối đa netCI để nằm chờ trong hàng đợi Jenkins
  reconcileRunTimeoutSeconds: 3600       # run đã gửi đi mà Jenkins không biết tới
  driftProbe: false                      # true chỉ khi tài khoản có Administer/SystemRead

registry:
  pushHost: harbor.congty.vn
  pullHost: harbor.congty.vn
  allowHttp: false                       # build kiểm tra TLS
  buildBaseImage: harbor.congty.vn/base/python:3.12-alpine
  trivyDbRepository: harbor.congty.vn/mirror/trivy-db:2

supplyChain:
  signatureRequireTlog: false            # hoặc true + rekorUrl (bước 3)
  rekorUrl: ""
  requireProvenance: true

buildIsolation:
  mode: none                             # kubernetes: namespace riêng cho mỗi dự án (ADR-030), ⚠ chưa chạy thật
```

Những gì netCI **tự quyết**, không cần cấu hình trên Jenkins: tên job, tham số build, token
callback riêng cho mỗi build (hạn 24 giờ), stage nào chạy, và build của fork có được publish
hay không.

Ngoài môi trường local, netCI **dừng khởi động** nếu thiếu `NETCI_JENKINS_CONTROLLERS`,
`NETCI_CALLBACK_URL`, token của một controller, hoặc `rekorUrl` khi đòi tlog. Chart luôn đặt
hai giá trị đầu.

---

## 5. Kiểm tra Jenkins bằng preflight (chỉ đọc)

Chạy từ một máy gọi được Jenkins, cho **từng** controller:

```bash
export JENKINS_API_TOKEN='...'          # token của netci-sa; không đưa vào dòng lệnh
python3 scripts/jenkins_preflight.py \
  --url https://jenkins.congty.vn --user netci-sa \
  --library netci-shared-library@netci-0.3.0 \
  --cosign-credential netci-cosign-key --agent-label netci-ephemeral \
  --folder platform/netci \
  --git-credential netci-git --registry-credential netci-harbor \
  --cosign-password-credential netci-cosign-password
```

- `PASS`/`FAIL`: mỗi dòng FAIL kèm cách sửa; thoát 0 nghĩa là không có FAIL.
- `INFO`: thông tin, không chặn (ví dụ tài khoản không đọc được JCasC → đặt `driftProbe: false`).
- `NOT CHECKED`: preflight **không chứng minh được** mục này, phải kiểm tra ở bước 7:
  - phiên bản library có tồn tại không;
  - `Job/Build`, `Job/Cancel`.

Preflight **không** kiểm tra các mục sau; tự kiểm tra bằng tay:

| Cần kiểm tra | Cách kiểm |
| :--- | :--- |
| Pod template có `builder` và env `NETCI_BUILD_CONTAINER` | xem cấu hình cloud; build thử ở bước 7 |
| Agent gọi được netCI | từ một pod trong namespace agent: `curl -sS <callbackUrl>/livez` |
| Agent gọi được git, registry (và TLS tin được CA) | `git ls-remote`, `buildah login` từ pod `builder` |
| Base image và Trivy DB mirror có trong registry | kéo thử bằng `buildah pull` |
| PSA cho phép `allowPrivilegeEscalation` | tạo pod thử bằng đúng template |

---

## 6. Cài netCI

Theo DEPLOY-KUBERNETES.md mục 3–6 (image, Secret, values, `helm upgrade --install`). Sau khi
cài, `/readyz` phải báo `ci.healthyControllers` bằng số controller.

---

## 7. Chạy thử theo bậc

Làm tuần tự; bậc sau chỉ bắt đầu khi bậc trước đạt. Mỗi bậc ghi lại bằng chứng: run id,
build number trên Jenkins.

1. **Build thủ công, chỉ build.** Tạo một module thử trỏ vào repo thật của công ty, bấm
   *Run Pipeline* và bỏ chọn deploy. Kỳ vọng:
   - job `netci-<id>` xuất hiện đúng folder;
   - build đi hết checkout → publish;
   - run có digest;
   - tab evidence có SBOM, kết quả scan, chữ ký và provenance.

   Nếu build hỏng ở "Loading library" thì phiên bản library sai. Hỏng ở Build do không kéo
   được base image thì kiểm tra credential registry hoặc mirror.
2. **Deploy dev** từ run đó (Promote). Kỳ vọng: worker xác minh chữ ký và provenance, deploy
   đạt healthy.
3. **Webhook từ SCM của công ty.** Cấu hình webhook tới `<externalUrl>/api/webhooks/scm/<github|gitlab>`
   với secret đã đặt trong module. Sau đó:
   - push lên `main` → build và deploy dev;
   - push nhánh khác → chỉ build;
   - mở PR trong cùng repo → build, không deploy;
   - ⚠ PR từ fork: mặc định bị bỏ qua. Nếu module bật `forkPullRequests: verify` thì chỉ build
     và kiểm tra, **không** push gì lên registry. Xác nhận bằng cách so danh sách tag trước và
     sau (đã kiểm chứng trên lab).
4. **Build dài và hàng đợi Jenkins.**
   - ⚠ Để một build nằm trong hàng đợi của Jenkins (ví dụ khi hết agent), rồi huỷ run trên
     netCI: queue item phải bị huỷ. Khi build đã bắt đầu, trạng thái phải được theo dõi tiếp.
   - Một build chạy quá `reconcileRunTimeoutSeconds` mà Jenkins vẫn báo đang chạy thì
     **không** bị đánh `failed`.
5. **Tải.** Với quota nhỏ, push liên tục vào một nhánh: chỉ build đầu và build cuối lên
   Jenkins, còn lại bị thay thế (ADR-050, đã kiểm chứng trên lab: 12 push → 2 build).
6. **Sự cố.**
   - Tắt một controller: build mới đi controller còn lại.
   - Chặn callback: reconciler phải đóng run sau timeout và dừng build trên Jenkins.

---

## 8. Vận hành

- **Nâng library**: đánh tag mới, chạy preflight với `--library ...@<tag-mới>`, đổi
  `jenkins.sharedLibrary`, `helm upgrade`. Quay lại bằng tag cũ; job được ghi lại ở build kế tiếp.
- **Xoay token**: tạo token mới cho `netci-sa`, cập nhật `jenkins-<id>-api-token` trong
  Secret, restart API.
- **Cảnh báo**: `NetciNoCiController` (không controller nào trả lời), `NetciOutboxDeadLetters`,
  `NetciReconcilerCorrecting` (callback bị mất — kiểm tra `callbackUrl` từ agent). Xem docs/SLO.md.
- **Metric**: `netci_ci_controllers_healthy`, `netci_ci_controllers_drift` (chỉ khi
  `driftProbe: true`).
- **Build có token 24 giờ**: build (tính cả thời gian chờ trong hàng đợi Jenkins) dài hơn
  thế sẽ không báo về được, và bị reconciler dừng.

---

## 9. Quyết định công ty cần chốt

1. Cho phép `allowPrivilegeEscalation: true` cho container `builder` (buildah rootless) hay không.
2. Có Rekor nội bộ không; nếu không thì `signatureRequireTlog: false`.
3. netCI được toàn quyền trên các job của nó (tạo, ghi đè mỗi build) trong một folder riêng.
4. Có cấp `Administer`/`SystemRead` cho probe drift không.
5. Một credential git và một credential registry dùng chung cho mọi module có chấp nhận
   được không (chưa có credential riêng cho từng repo).
6. PR từ fork: `ignore` (mặc định) hay `verify`.
7. Quota build mỗi team (`/quotas`) và `admissionMaxQueue` phù hợp với sức chứa của controller.

---

## 10. Lỗi thường gặp

| Triệu chứng | Nguyên nhân thường gặp | Sửa |
| :--- | :--- | :--- |
| netCI không khởi động: `requires NETCI_JENKINS_CONTROLLERS, NETCI_CALLBACK_URL` | cài bằng env, không qua chart | đặt đủ hai biến |
| `/readyz` báo 0 controller healthy | URL/token sai, TLS của Jenkins không được tin | chạy preflight; thêm CA vào image |
| Build hỏng ở "Loading library" | phiên bản library không có, hoặc library không phải global | kiểm tra tag và cấu hình Global Trusted Library |
| Build đứng ở stage đầu, không callback | agent không gọi được `callbackUrl` | `curl` từ pod agent; sửa `jenkins.callbackUrl` hoặc network policy |
| Run `queued` mãi, `admittedAt` rỗng | quota của team đầy, hoặc hàng đợi Jenkins ≥ `admissionMaxQueue` | xem quota; tăng agent hoặc `admissionMaxQueue` |
| Sign hỏng "decrypt" | key có mật khẩu mà chưa đặt `cosignPasswordCredentialsId` | tạo credential mật khẩu |
| Push hỏng "authentication required" | chưa đặt `registryCredentialsId` | tạo credential registry |
| Deploy bị từ chối vì chữ ký không có trong transparency log | bật `signatureRequireTlog` nhưng build không upload | kiểm tra `rekorUrl` và đường tới Rekor từ agent |
| `AGENT_LABEL_NOT_ALLOWED` | run chọn một pod template không nằm trong danh sách | thêm vào `allowedAgentLabels`, hoặc bỏ lựa chọn |
| `netci_ci_controllers_drift=1` mãi | tài khoản không có quyền đọc JCasC | `driftProbe: false` hoặc cấp `SystemRead` |
