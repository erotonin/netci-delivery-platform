# Runbook: nối netCI vào Jenkins của công ty

Làm **theo đúng thứ tự**. Mỗi bước ghi rõ ai làm, làm gì (có lệnh hoặc đoạn cấu hình copy
được), và **cách biết bước đó đã xong**. Không sang bước sau khi bước trước chưa đạt.

Giải thích lý do của từng bước và danh sách lỗi đã sửa: [HUONG_DAN_DAU_NOI_JENKINS_THUC_TE.md](HUONG_DAN_DAU_NOI_JENKINS_THUC_TE.md).
Cài chart chi tiết: [DEPLOY-KUBERNETES.md](DEPLOY-KUBERNETES.md).

Quy ước trong runbook (thay bằng giá trị thật của công ty):

| Tên | Ví dụ | Ý nghĩa |
| :--- | :--- | :--- |
| `JENKINS_URL` | `https://jenkins.congty.vn` | controller (lặp lại cho mỗi controller) |
| `FOLDER` | `platform/netci` | folder chứa job của netCI |
| `REGISTRY` | `harbor.congty.vn` | registry build push vào |
| `GIT_HOST` | `git.congty.vn` | git server của các repo ứng dụng |
| `NETCI_URL` | `https://netci.congty.vn` | địa chỉ của netCI |
| `AGENT_NS` | `netci-build` | namespace chạy pod agent |

Ký hiệu: ✅ = cách kiểm tra đã xong. ⚠ = chưa từng chạy với Jenkins công ty, cần theo dõi kỹ.

---

## Giai đoạn 0 — Chốt quyết định (platform + bảo mật, ~1 buổi họp)

Ghi lại câu trả lời; mỗi câu quyết định một giá trị cấu hình về sau.

| # | Quyết định | Nếu "có" | Nếu "không" |
| :-- | :--- | :--- | :--- |
| 0.1 | Cho container `builder` chạy `allowPrivilegeEscalation: true` (buildah rootless) | tiếp tục | **dừng**: chưa có cách build thay thế |
| 0.2 | Có Rekor (transparency log) nội bộ | `signatureRequireTlog: true`, `rekorUrl` | `signatureRequireTlog: false` |
| 0.3 | netCI được tạo và ghi đè job trong một folder riêng | `FOLDER` | tạo ở gốc (cần `Job/Create` toàn cục) |
| 0.4 | Cấp `Overall/SystemRead` (hoặc `Administer`) cho tài khoản netCI | `driftProbe: true` | `driftProbe: false` |
| 0.5 | Build farm có ra internet | bỏ qua bước mirror | làm Giai đoạn 3 |
| 0.6 | PR từ fork | `verify`: build và kiểm tra, không push | `ignore` (mặc định) |
| 0.7 | Chấp nhận một credential git và một credential registry chung cho mọi module | tiếp tục | chưa hỗ trợ credential riêng cho từng repo |

✅ Có biên bản với 7 câu trả lời.

---

## Giai đoạn 1 — Cụm Kubernetes cho agent (admin Kubernetes)

### 1.1 Namespace, service account, RBAC

```bash
# Copy từ repo: infra/kind/jenkins-agent-rbac.yaml (đổi namespace nếu cần)
kubectl apply -f infra/kind/jenkins-agent-rbac.yaml
# Pod Security: builder cần allowPrivilegeEscalation -> baseline, không phải restricted
kubectl label namespace netci-build pod-security.kubernetes.io/enforce=baseline --overwrite
```

File này tạo: namespace `netci-build`, SA `jenkins-controller` (Jenkins dùng để tạo pod), SA
`jenkins-agent` (pod build, không mount token), và Role/RoleBinding cho `pods`, `pods/log`,
`pods/exec`.

### 1.2 Token cho Jenkins cloud

Chọn một trong hai:

```bash
# (a) Token có hạn, xoay định kỳ (khuyến nghị; lab dùng 24h)
kubectl -n netci-build create token jenkins-controller --duration=720h > jenkins-controller.token

# (b) Token dài hạn gắn với Secret
kubectl -n netci-build apply -f - <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: jenkins-controller-token
  annotations: {kubernetes.io/service-account.name: jenkins-controller}
type: kubernetes.io/service-account-token
EOF
kubectl -n netci-build get secret jenkins-controller-token -o jsonpath='{.data.token}' | base64 -d > jenkins-controller.token
```

Lấy CA của API server:

```bash
kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > cluster-ca.crt
```

✅ `kubectl --token="$(cat jenkins-controller.token)" -n netci-build auth can-i create pods` trả
`yes`, và `... auth can-i create pods -n default` trả `no`.

### 1.3 Mạng ra từ `netci-build`

Pod trong `netci-build` phải gọi được `NETCI_URL`, `GIT_HOST`, `REGISTRY` (và Rekor nếu 0.2 = có).
Nếu cụm có NetworkPolicy mặc định chặn egress, mở đúng các đích này.

✅ Kiểm tra ở bước 5.3.

---

## Giai đoạn 2 — Image cho agent (platform)

```bash
# Toolbox: buildah, python3, git, syft 1.51.0, trivy 0.73.0, cosign 3.1.2, Go 1.27.0
JENKINS_TOOLBOX_IMAGE=$REGISTRY/netci/ci-toolbox:0.4.0 jenkins/scripts/build-agent-toolbox.sh
docker push $REGISTRY/netci/ci-toolbox:0.4.0

# Inbound agent (nếu cụm không kéo được từ Docker Hub)
docker pull jenkins/inbound-agent:3386.v353e57a_1b_ea_0-1-jdk21
docker tag  jenkins/inbound-agent:3386.v353e57a_1b_ea_0-1-jdk21 $REGISTRY/mirror/inbound-agent:3386.v353e57a_1b_ea_0-1-jdk21
docker push $REGISTRY/mirror/inbound-agent:3386.v353e57a_1b_ea_0-1-jdk21
```

Nếu registry dùng CA nội bộ: thêm CA vào image toolbox (sửa `jenkins/agent-toolbox/Dockerfile`,
copy CA vào `/usr/local/share/ca-certificates/` rồi `update-ca-certificates`), nếu không buildah,
syft, trivy, cosign đều từ chối TLS.

✅ `docker run --rm $REGISTRY/netci/ci-toolbox:0.4.0 sh -c 'buildah --version; syft version; trivy --version; cosign version'`
in đủ bốn phiên bản.

---

## Giai đoạn 3 — Mirror khi không có internet (platform, chỉ khi 0.5 = không)

```bash
# Trivy vulnerability DB (build dùng qua registry.trivyDbRepository)
docker pull aquasec/trivy-db:2 && docker tag aquasec/trivy-db:2 $REGISTRY/mirror/trivy-db:2 && docker push $REGISTRY/mirror/trivy-db:2
# hoặc: oras copy ghcr.io/aquasecurity/trivy-db:2 $REGISTRY/mirror/trivy-db:2

# Base image của ứng dụng (build dùng qua registry.buildBaseImage)
docker pull python:3.12-alpine && docker tag python:3.12-alpine $REGISTRY/base/python:3.12-alpine && docker push $REGISTRY/base/python:3.12-alpine
```

Trivy DB cập nhật hằng ngày: đặt job mirror chạy định kỳ, nếu không, kết quả scan sẽ cũ dần.

`Dockerfile` của mỗi ứng dụng phải có `ARG PYTHON_IMAGE` và `FROM ${PYTHON_IMAGE}` (build truyền
`--build-arg PYTHON_IMAGE=<registry.buildBaseImage>`).

✅ Cả hai tag đều có trong registry.

---

## Giai đoạn 4 — Jenkins (admin Jenkins)

### 4.1 Plugin

```text
workflow-job workflow-cps pipeline-model-definition pipeline-groovy-lib
kubernetes git git-client credentials-binding plain-credentials
timestamper pipeline-utility-steps ws-cleanup
(tuỳ chọn) configuration-as-code
```

Cài qua *Manage Jenkins → Plugins* hoặc `jenkins-plugin-cli --plugins <danh sách>`. Phiên bản
đã chạy thật: `jenkins/plugins.txt`, Jenkins 2.541.1 LTS.

### 4.2 Folder và tài khoản dịch vụ

1. Tạo folder `FOLDER` (*New Item → Folder*). netCI **không tự tạo** folder.
2. Tạo user `netci-sa`. Đăng nhập bằng user đó: *user → Security → API Token → Add new token*.
   Lưu token vào file `jenkins-a-api-token` (không dán vào chat hay ticket).
3. Cấp quyền. Với *Matrix Authorization* (folder-based) hoặc *Role-based Strategy*:
   - toàn cục: `Overall/Read` (+ `Overall/SystemRead` nếu 0.4 = có);
   - trong `FOLDER`: `Job/Create`, `Job/Configure`, `Job/Build`, `Job/Read`, `Job/Cancel`;
   - **không** cấp `Job/Delete`.
4. Giữ CSRF (crumb issuer) bật.

✅ `curl -su netci-sa:$(cat jenkins-a-api-token) $JENKINS_URL/me/api/json` trả `"id":"netci-sa"`.

### 4.3 Credential

Tạo ở *Manage Jenkins → Credentials → System → Global* (hoặc bằng JCasC, đoạn dưới):

| ID | Kind | Nội dung | Khi nào |
| :--- | :--- | :--- | :--- |
| `netci-cosign-key` | Secret text | nội dung `cosign.key` (Giai đoạn 5) | luôn |
| `netci-cosign-password` | Secret text | mật khẩu của key | key có mật khẩu |
| `netci-git` | Username with password | user + **token** HTTPS đọc repo | repo private |
| `netci-harbor` | Username with password | robot account có quyền pull/push các project | registry có xác thực |
| `netci-k8s-token` | Secret text | `jenkins-controller.token` (1.2) | Kubernetes cloud |

```yaml
# JCasC tương đương (giá trị lấy từ file secret, không ghi thẳng vào YAML)
credentials:
  system:
    domainCredentials:
      - credentials:
          - string: {id: netci-cosign-key, scope: GLOBAL, secret: "${NETCI_COSIGN_PRIVATE_KEY}"}
          - string: {id: netci-cosign-password, scope: GLOBAL, secret: "${NETCI_COSIGN_PASSWORD}"}
          - usernamePassword: {id: netci-git, scope: GLOBAL, username: "netci-bot", password: "${NETCI_GIT_TOKEN}"}
          - usernamePassword: {id: netci-harbor, scope: GLOBAL, username: "robot$netci", password: "${NETCI_HARBOR_TOKEN}"}
          - string: {id: netci-k8s-token, scope: SYSTEM, secret: "${KUBERNETES_SERVICE_ACCOUNT_TOKEN}"}
```

ID chỉ dùng `A-Z a-z 0-9 _ . -`.

### 4.4 Kubernetes cloud và pod template

Đoạn JCasC dưới đây là bản đã chạy thật trên lab (`jenkins/casc/ephemeral-agent.yaml`), đã
thay giá trị cho công ty. Nếu làm bằng UI (*Manage Jenkins → Clouds → New cloud → Kubernetes*),
nhập đúng các trường này.

```yaml
jenkins:
  clouds:
    - kubernetes:
        name: netci-agents
        serverUrl: https://<api-server>:6443
        serverCertificate: "<nội dung cluster-ca.crt>"
        namespace: netci-build
        skipTlsVerify: false
        credentialsId: netci-k8s-token
        jenkinsUrl: https://jenkins.congty.vn      # địa chỉ POD gọi ngược về controller
        webSocket: true
        containerCapStr: "10"
        podRetention: never
        templates:
          - name: netci-ephemeral
            label: netci-ephemeral
            namespace: netci-build
            serviceAccount: jenkins-agent
            nodeUsageMode: EXCLUSIVE
            workspaceVolume: {emptyDirWorkspaceVolume: {memory: false}}
            podRetention: never
            idleMinutes: 0
            yaml: |
              apiVersion: v1
              kind: Pod
              spec:
                automountServiceAccountToken: false
                serviceAccountName: jenkins-agent
                restartPolicy: Never
                securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000}
                containers:
                  - name: jnlp
                    image: harbor.congty.vn/mirror/inbound-agent:3386.v353e57a_1b_ea_0-1-jdk21
                    env:
                      - {name: NETCI_BUILD_CONTAINER, value: builder}
                      - {name: NETCI_AGENT_DISPOSABLE, value: "true"}
                    securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
                    resources: {requests: {cpu: 100m, memory: 256Mi}, limits: {cpu: 500m, memory: 1Gi}}
                  - name: builder
                    image: harbor.congty.vn/netci/ci-toolbox:0.4.0
                    command: ["sleep"]
                    args: ["99d"]
                    tty: true
                    env:
                      - {name: BUILDAH_ISOLATION, value: chroot}
                      - {name: STORAGE_DRIVER, value: vfs}
                    securityContext: {allowPrivilegeEscalation: true}
                    resources: {requests: {cpu: 500m, memory: 1Gi}, limits: {cpu: "2", memory: 4Gi}}
```

Hai tên phải giữ đúng: container **`builder`** và env **`NETCI_BUILD_CONTAINER=builder`**.
Thiếu env này thì các bước build chạy trong `jnlp`, nơi không có công cụ.

✅ Tạo một Pipeline job thử (xoá sau) với nội dung dưới, chạy phải thấy phiên bản buildah:

```groovy
podTemplate(inheritFrom: 'netci-ephemeral') { node(POD_LABEL) { container('builder') { sh 'buildah --version && id' } } }
```

### 4.5 Shared library

1. Tạo repo `netci-shared-library` trên `GIT_HOST`, chép nội dung `jenkins/shared-library/`
   (hai thư mục `vars/` và `resources/`) vào gốc repo, commit, **đánh tag** `netci-0.3.0`.
   (Hoặc trỏ thẳng vào repo netCI với *Library Path* `jenkins/shared-library/`.)
2. *Manage Jenkins → System → Global Trusted Pipeline Libraries → Add*:
   - Name `netci-shared-library`, Default version `netci-0.3.0`;
   - bật **Allow default version to be overridden**, tắt *Load implicitly*;
   - Retrieval: Modern SCM → Git → repo trên, credential `netci-git` nếu repo private.

```yaml
unclassified:
  globalLibraries:
    libraries:
      - name: netci-shared-library
        defaultVersion: netci-0.3.0
        implicit: false
        allowVersionOverride: true
        retriever:
          modernSCM:
            scm:
              git: {remote: "https://git.congty.vn/platform/netci-shared-library.git", credentialsId: netci-git}
```

Phải là library **global (trusted)**, không đặt ở cấp folder.

✅ Thêm dòng `@Library('netci-shared-library@netci-0.3.0') _` vào đầu job thử ở 4.4 rồi chạy:
log có `Loading library netci-shared-library@netci-0.3.0`.

---

## Giai đoạn 5 — Key ký (người giữ key, theo quy trình của bảo mật)

```bash
COSIGN_PASSWORD='<mật khẩu mạnh>' cosign generate-key-pair      # tạo cosign.key + cosign.pub
```

- `cosign.key` → credential `netci-cosign-key`; mật khẩu → `netci-cosign-password` (4.3). Sau
  đó xoá bản trên máy.
- `cosign.pub` → Secret của netCI (6.1). netCI chỉ cần public key.

Nếu 0.2 = có Rekor: agent và image netCI phải tin public key của Rekor đó ⚠.

✅ `cosign public-key --key cosign.key` (nhập mật khẩu) in ra đúng nội dung `cosign.pub`.

---

## Giai đoạn 6 — netCI (platform)

### 6.1 Secret

```bash
kubectl -n netci-system create secret generic netci-app \
  --from-file=database-url=./database-url \
  --from-file=workload-token-keys=./workload-token-keys \
  --from-file=cosign.pub=./cosign.pub \
  --from-file=jenkins-a-api-token=./jenkins-a-api-token
# Thêm jenkins-b-api-token... cho mỗi controller. Xem DEPLOY-KUBERNETES.md mục 4 cho các key khác.
```

### 6.2 Values (phần Jenkins)

```yaml
global:
  environment: production
  externalUrl: https://netci.congty.vn
existingSecret: netci-app

jenkins:
  controllers:
    - {id: A, url: "https://jenkins.congty.vn", username: netci-sa, executors: 4}
  folder: platform/netci
  sharedLibrary: netci-shared-library@netci-0.3.0
  agentLabel: netci-ephemeral
  allowedAgentLabels: ""
  cosignCredentialsId: netci-cosign-key
  cosignPasswordCredentialsId: netci-cosign-password
  gitCredentialsId: netci-git
  registryCredentialsId: netci-harbor
  callbackUrl: ""                    # trống = https://netci.congty.vn/api
  admissionMaxQueue: 2
  reconcileRunTimeoutSeconds: 3600
  driftProbe: false                  # true nếu 0.4 = có

registry:
  pushHost: harbor.congty.vn
  pullHost: harbor.congty.vn
  allowHttp: false
  buildBaseImage: harbor.congty.vn/base/python:3.12-alpine
  trivyDbRepository: harbor.congty.vn/mirror/trivy-db:2

supplyChain:
  signatureRequireTlog: false        # 0.2
  rekorUrl: ""                       # bắt buộc khi signatureRequireTlog: true
  requireProvenance: true
```

Các phần khác (Temporal, OIDC, DCIM, đích deploy): DEPLOY-KUBERNETES.md mục 5.

### 6.3 Cài

```bash
helm upgrade --install netci deploy/helm/netci-platform -n netci-system --create-namespace -f values-congty.yaml
kubectl -n netci-system rollout status deploy/netci-netci-platform-api
kubectl -n netci-system exec deploy/netci-netci-platform-api -c api -- python -c \
  'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8000/readyz").read().decode())'
```

✅ `/readyz` có `"ready": true` và `ci.healthyControllers` bằng số controller. Nếu chart từ chối
render hoặc pod không khởi động, lỗi nêu đúng giá trị còn thiếu (ví dụ `rekorUrl`,
`NETCI_CALLBACK_URL`).

---

## Giai đoạn 7 — Kiểm tra trước khi chạy build thật (platform)

### 7.1 Preflight (chỉ đọc, chạy cho từng controller)

```bash
export JENKINS_API_TOKEN="$(cat jenkins-a-api-token)"
python3 scripts/jenkins_preflight.py --url https://jenkins.congty.vn --user netci-sa \
  --library netci-shared-library@netci-0.3.0 --folder platform/netci \
  --cosign-credential netci-cosign-key --cosign-password-credential netci-cosign-password \
  --git-credential netci-git --registry-credential netci-harbor --agent-label netci-ephemeral
```

✅ Không có dòng `FAIL`, lệnh thoát 0. Dòng `NOT CHECKED` (phiên bản library, `Job/Build`,
`Job/Cancel`) sẽ được chứng minh ở Giai đoạn 8.

### 7.2 Từ pod agent: có gọi được mọi thứ không

```bash
kubectl -n netci-build run netci-probe --rm -it --restart=Never \
  --image=harbor.congty.vn/netci/ci-toolbox:0.4.0 \
  --overrides='{"spec":{"serviceAccountName":"jenkins-agent","automountServiceAccountToken":false}}' -- bash -c '
    curl -sS -o /dev/null -w "netci %{http_code}\n" https://netci.congty.vn/api/livez
    git ls-remote https://git.congty.vn/<nhóm>/<repo-thử>.git HEAD | head -1
    curl -sS -o /dev/null -w "registry %{http_code}\n" https://harbor.congty.vn/v2/'
```

✅ `netci 200`; `git ls-remote` in một commit (repo private thì trả 401 là đúng, credential
`netci-git` sẽ được dùng khi build); `registry 401` hoặc `200` (tức là TLS và đường mạng đều ổn).

---

## Giai đoạn 8 — Chạy thử theo bậc (platform + một team thử)

Chọn **một repo thật, nhỏ** của một team. Ghi lại run id và build number ở mỗi bậc.

| Bậc | Làm | ✅ Kỳ vọng | Nếu hỏng |
| :--- | :--- | :--- | :--- |
| 8.1 | Portal: tạo system + module trỏ vào repo; *Run Pipeline*, bỏ chọn deploy | job `netci-<id>` xuất hiện trong `FOLDER`; build chạy checkout → publish; run có digest; evidence có SBOM, scan, chữ ký, provenance | "Loading library": sai tag hoặc library không global. Hỏng ở Build: base image hoặc credential registry. Hỏng ở Sign: mật khẩu key |
| 8.2 | *Promote* run đó lên dev | worker xác minh chữ ký và provenance; deployment `healthy` | chữ ký không xác minh được: `cosign.pub` không khớp key |
| 8.3 | Huỷ một run đang build | Jenkins báo build `ABORTED` | thiếu `Job/Cancel` |
| 8.4 | ⚠ Chiếm hết agent (hoặc đặt quiet period), chạy 1 run rồi huỷ khi nó còn trong hàng đợi Jenkins | queue item bị huỷ, run `cancelled` | ghi lại log của API và báo lại |
| 8.5 | Cấu hình webhook (bảng dưới), push lên `main` | build và deploy dev tự động | xem *Recent Deliveries* của webhook: 401 = secret sai |
| 8.6 | Push nhánh khác; mở PR cùng repo | chỉ build, không deploy | — |
| 8.7 | Push 2 commit liên tiếp vào PR đang build | build cũ `ABORTED`, build mới chạy (đã kiểm chứng trên lab) | — |
| 8.8 | Nếu 0.6 = verify: mở PR từ fork | build chạy tới scan, không có digest; **registry không có tag mới** | nếu có tag mới: dừng lại, báo ngay |

**Cấu hình webhook cho module** (người có quyền developer của team, không phải agent):

```bash
TOKEN=<access token OIDC của bạn>
curl -sS -X POST https://netci.congty.vn/api/applications/<applicationId>/scm \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"provider":"github","repositoryIdentity":"<org>/<repo>","secretToken":"<chuỗi ngẫu nhiên ≥ 24 ký tự>"}'
```

Rồi trên SCM:
- **GitHub**: *Settings → Webhooks → Add*. URL `https://netci.congty.vn/api/webhooks/scm/github`,
  content type `application/json`, secret giống `secretToken`, chọn event *Pushes* và *Pull requests*.
- **GitLab**: *Settings → Webhooks*. URL `.../api/webhooks/scm/gitlab`, *Secret token* giống
  `secretToken`, chọn *Push events* và *Merge request events*.

`applicationId` lấy ở trang module, hoặc từ `GET /api/modules/<moduleId>`.

---

## Giai đoạn 9 — Mở cho các team (platform)

1. Đặt quota cho từng team: `PUT /api/quotas/team/<team>` với `maxConcurrentPipelines`,
   `maxQueuedPipelines`.
2. Bật cảnh báo (chart `monitoring.prometheusRule.enabled: true`): `NetciNoCiController`,
   `NetciReconcilerCorrecting`, `NetciOutboxDeadLetters`.
3. Đưa cho các team hai yêu cầu: `Dockerfile` có `ARG PYTHON_IMAGE`; không sửa job trên UI
   Jenkins (netCI ghi đè job mỗi lần build).
4. Lịch xoay vòng: token `netci-sa`, token của cloud, key cosign, và job mirror Trivy DB.

✅ Hai team đầu tiên chạy ổn một tuần mà không có run nào `failed` do hạ tầng.

---

## Quay lại (rollback)

- **Library**: đổi `jenkins.sharedLibrary` về tag cũ rồi `helm upgrade`. Job được ghi lại ở build
  kế tiếp.
- **netCI**: `helm rollback netci <revision>`. Migration của database chỉ tiến, không có bản
  lùi. Việc bản cũ chạy trên schema mới **chưa được kiểm chứng**, nên backup database trước mỗi
  lần nâng (`scripts/netci_backup.py`) để quay lại được thật.
- **Ngắt hẳn Jenkins khỏi netCI**: bỏ controller khỏi `jenkins.controllers`. Các job
  `netci-*` trong `FOLDER` vẫn còn và có thể xoá tay.
