# Triển khai Jenkins active-active lên production

Tài liệu này dành cho người cài đặt tại site: K8s chạy các controller, các agent chạy trên một VM
OpenStack lớn, lưu trữ chủ yếu là `local-path`. Mỗi bước ghi rõ **đã kiểm chứng ở lab** hay
**chưa**. Lý do của từng quyết định nằm trong ADR-076 đến ADR-086.

Một lệnh cài: chart `deploy/helm/netci-jenkins-ha` (ADR-085). Lab `aa-p2` đang chạy đúng từ chart
này với `ci/lab-values.yaml`. Bản mẫu cho production là `ci/production-values.yaml`.

---

## 1. Toàn cảnh

```
 người dùng / IDP ──► VIP (Octavia LB hoặc kube-vip) ──► HAProxy (DaemonSet, mỗi máy một cái)
                                                         │  cookie NETCI_REPLICA, /netci-ready/
                                             ┌───────────┴───────────┐
                                        jenkins-0                 jenkins-1     (StatefulSet,
                                        home local-path           home local-path  mỗi máy một replica)
                                             └────── netci-homesync ─┘          (2 pod)
                                                          │
                                         CloudNativePG "store" (3 instance, sync ANY 1)
                                                          │  WAL + base backup
                                                   S3 (Ceph RGW / MinIO)
 IDP ──► netci-queue (2 pod, DB netci_queue trên store) ──► haproxy-local ──► replica sở hữu job
 VM agent lớn ──(WebSocket, Node-Name)──► VIP ──► replica "nhà" của agent
 netci-supervisor ──(Nova API: os-stop)──► VM của máy đã mất ──► taint out-of-service ──► replica còn lại nhận build
```

Một máy chạy controller mất điện thì:
- UI vẫn trả lời;
- build đang chạy được replica kia nhận lại (adopt) sau khi supervisor tắt hẳn máy đó;
- store chuyển primary, không mất bản ghi đã xác nhận.

Số đo nằm trong ADR-079, ADR-080, ADR-084 và ADR-085.

## 2. Đã và chưa kiểm chứng

**Đã kiểm chứng ở lab** (3 VM KVM, k3s, `local-path`), mỗi mục có file trong `lab/evidence/`:
- **Mất điện máy chạy replica:** UI không gián đoạn; build đang chạy được nhận lại; số build không
  trùng.
- **Cắt mạng máy chạy owner** (VM vẫn chạy): build chỉ được nhận lại *sau khi* máy bị tắt nguồn
  (ADR-084). Lab tắt máy qua emulator Redfish, chưa qua Nova.
- **Mất máy chạy primary của store:** không mất bản ghi; thao tác sửa bị từ chối 28–45 s.
- **Store:** backup và restore qua Barman Cloud plugin; bản restore khớp từng dòng.
- **Cài, nâng cấp, khởi động lại:**
  - cài bằng chart lên lab đang chạy: 219/219 lần đọc UI, 220/220 lần tạo job;
  - nâng cấp controller cuốn chiếu: 358/358 lần đọc UI;
  - rolling restart với người dùng đăng nhập qua form: 0 lần tạo job bị từ chối (ADR-086);
  - rolling restart khi drain qua agent-check: 324/324 lần đọc UI, kể cả trên lab có 22.677 job
    với dashboard mất 17 s (ADR-089).
- **Đổi tên, di chuyển job và folder; view ở cấp gốc:** lan sang replica kia trong 0,1–1,1 s
  (ADR-087, ADR-088).
- **netci-queue:** mỗi run chạy đúng một lần trên hai replica, kể cả khi mất máy.
- **IDP qua queue:** adapter thật của IDP tạo job, chạy build, hủy build.

**Chưa kiểm chứng**, nên tại site phải tự làm bước kiểm tra tương ứng ở mục 10:
- fence bằng Nova thật: mới có test với Keystone/Nova giả;
- Octavia LoadBalancer;
- kube-vip trên port OpenStack có allowed-address-pairs;
- Ceph RGW hoặc MinIO thật (lab dùng SeaweedFS);
- agent chạy trên VM ngoài cụm (lab dùng agent là pod);
- realm đăng nhập của site (LDAP/SSO).

## 3. Yêu cầu trước khi cài

| Thứ cần có | Vì sao |
|---|---|
| ≥ 3 node K8s, mỗi instance store trên một node | sync ANY 1 cần 2 standby trên 2 máy khác; 2 máy chết thì store từ chối ghi (không quên dữ liệu) |
| 2 node cho 2 replica Jenkins (anti-affinity bắt buộc) | hai replica chung máy thì mất máy là mất cả hai |
| StorageClass `local-path` (hoặc của site) | home mỗi replica nằm trên đĩa máy đó (ADR-076) |
| Registry nội bộ, mọi image ghim theo digest | chart từ chối image chỉ có tag |
| Bucket S3 (Ceph RGW/MinIO) + access key chỉ ghi được bucket đó | backup của store (ADR-083/085) |
| Application credential OpenStack được stop/start đúng các VM controller | fence (ADR-084) |
| Một địa chỉ VIP: Octavia, hoặc IP trống trong subnet cho kube-vip | điểm vào cho người dùng, agent, IDP |
| Quyền cài operator cấp cluster (CRD) | CloudNativePG, cert-manager, Barman Cloud plugin |

### Image (ghim digest khi mirror vào registry của site)

Phiên bản đã chạy ở lab:

| Image | Phiên bản lab | Ghi chú |
|---|---|---|
| controller netCI | `jenkins-controller:2.555.3-netci34` | build từ `jenkins/Dockerfile.controller`, sạch từ HEAD |
| netCI (homesync, queue, supervisor) | commit `53e62f9` trở lên | `make image` |
| HAProxy | `haproxy@sha256:e3229153…` | |
| kube-vip | `ghcr.io/kube-vip/kube-vip@sha256:c6d4f3a3…` | chỉ khi `vip.mode: kubevip` |
| PostgreSQL cho CNPG | `ghcr.io/cloudnative-pg/postgresql:16.15-system-trixie` | **cần cả tag lẫn digest** (CNPG đọc major version từ tag) |
| CloudNativePG operator | 1.30.1 | |
| cert-manager | v1.21.2 (controller, webhook, cainjector) | |
| Barman Cloud plugin + sidecar | v0.15.1 | sidecar được plugin chèn vào mọi pod store |

## 4. Bước 1: operator cấp cluster

Thứ tự: CloudNativePG, rồi cert-manager, rồi Barman Cloud plugin. Lab làm đúng các bước này bằng
`lab/aa/store.sh` (hàm `operator` và `plugin`). Dưới đây là phần cần mang sang site.

**CloudNativePG**: áp manifest release (kiểm checksum), rồi vá như sau:

```yaml
# kubectl -n cnpg-system patch deployment cnpg-controller-manager --type strategic -p '...'
spec:
  replicas: 2                      # failover là quyết định của operator: 1 pod chết cùng primary = chờ ~5 phút
  template:
    spec:
      containers:
        - name: manager
          env:                     # nói chuyện với API server của chính máy mình
            - {name: KUBERNETES_SERVICE_HOST, valueFrom: {fieldRef: {fieldPath: status.hostIP}}}
            - {name: KUBERNETES_SERVICE_PORT, value: "6443"}
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector: {matchLabels: {app.kubernetes.io/name: cloudnative-pg}}
              topologyKey: kubernetes.io/hostname
      tolerations:
        - {key: node.kubernetes.io/not-ready, operator: Exists, effect: NoExecute, tolerationSeconds: 10}
        - {key: node.kubernetes.io/unreachable, operator: Exists, effect: NoExecute, tolerationSeconds: 10}
```

> `KUBERNETES_SERVICE_PORT: 6443` là port API server trên node của k3s/kubeadm. Nếu site khác
> (ví dụ API server sau LB riêng), đặt đúng port của site.

**cert-manager**: manifest release, không cần vá.

**Barman Cloud plugin**: manifest release, rồi vá. **Plugin nằm trên đường failover của store**.
Ở lab, khi plugin không chạy, operator không promote standby trong 189 s (ADR-085). Vì vậy:

```yaml
# kubectl -n cnpg-system patch deployment barman-cloud --type strategic -p '...'
spec:
  replicas: 2                      # chỉ leader phục vụ; pod kia chờ lease (không bao giờ Ready khi chờ)
  template:
    spec:
      containers:
        - name: barman-cloud
          readinessProbe: {periodSeconds: 2, initialDelaySeconds: 2}   # bản phát hành: 10 s
          env:
            - {name: KUBERNETES_SERVICE_HOST, valueFrom: {fieldRef: {fieldPath: status.hostIP}}}
            - {name: KUBERNETES_SERVICE_PORT, value: "6443"}
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector: {matchLabels: {app: barman-cloud}}
              topologyKey: kubernetes.io/hostname
      tolerations:
        - {key: node.kubernetes.io/not-ready, operator: Exists, effect: NoExecute, tolerationSeconds: 10}
        - {key: node.kubernetes.io/unreachable, operator: Exists, effect: NoExecute, tolerationSeconds: 10}
```

Giữ strategy `Recreate` như bản phát hành. Rolling update sẽ treo, vì pod mới không Ready được khi
leader cũ còn giữ lease.

Nếu cụm có máy **không** chạy instance store, nên đặt plugin lên đó (thêm `nodeAffinity`). Khi
leader của plugin nằm chung máy với primary và máy đó mất, ở lab thao tác sửa bị từ chối 44,5 s.
Khi leader nằm ở máy khác thì là 38,7 s.

## 5. Bước 2: Secret (chart không tạo Secret nào)

Namespace ví dụ: `jenkins`. Tên khớp với `ci/production-values.yaml`.

| Secret | Key | Tạo bằng |
|---|---|---|
| `homesync-replicas` | `replicas` (dòng `jenkins-N:<token>`) | `scripts/jenkins_ha_secrets.py generate -n jenkins --replicas 2` |
| `homesync-tokens` | `jenkins-0`, `jenkins-1`, … | cùng lệnh trên |
| `jenkins-session-key` | `key` (≥ 32 byte) | cùng lệnh trên |
| `jenkins-admin` | `password` | site tự đặt (từ vault) |
| `jenkins-home-secrets` | các file `secrets/` + `identity.key.enc` | `scripts/jenkins_ha_secrets.py seed-home` (mục 6, bước 3) |
| `store-s3` | `ACCESS_KEY_ID`, `ACCESS_SECRET_KEY` (+ `ca.crt` nếu CA riêng) | `kubectl create secret generic --from-file` (không dùng `--from-literal`) |
| `queue-db-role` | kiểu `kubernetes.io/basic-auth`: `username=queue`, `password` | mật khẩu ngẫu nhiên |
| `queue-db` | `url`: `postgresql://queue:<pw>@store-rw.jenkins.svc:5432/netci_queue` | cùng mật khẩu trên |
| `queue-jenkins` | `user`, `token` | user Jenkins riêng cho queue + API token của user đó |
| ConfigMap `queue-config` | `config.json` | xem dưới (chỉ chứa SHA-256 của token client, không chứa secret) |

`config.json` của netci-queue (giống `lab/aa/queue.sh`):

```json
{"cells": {"aa": {"url": "http://haproxy-local.jenkins.svc.cluster.local", "credentials": "/etc/netci/cells/aa", "budget": 50}},
 "routes": [{"prefix": "", "cell": "aa"}],
 "clients": {"idp": {"tokenSha256": "<sha256 của token client IDP>", "jobs": [""]}},
 "secretParameters": ["NETCI_CALLBACK_TOKEN"]}
```

User `queue-jenkins` cần các quyền Overall/Read, Job/Read, Job/Build và Job/Cancel trên các job
của IDP. Token client của IDP đặt vào `secrets.runQueueToken` của chart IDP (mục 9).

## 6. Bước 3: cài chart

```bash
cp deploy/helm/netci-jenkins-ha/ci/production-values.yaml site-values.yaml   # sửa image, VIP, S3, agent
helm template t deploy/helm/netci-jenkins-ha -n jenkins -f site-values.yaml >/dev/null   # chart tự từ chối giá trị sai
```

1. **Cài với một replica** (`--set replicas=1`), rồi chờ `jenkins-0` Ready.
2. **Tạo khóa agent:** mở trang của một inbound agent trên jenkins-0 một lần. Jenkins chỉ tạo khóa
   agent ở lần đầu nó tính secret cho một agent.
3. **Seed:** `python3 scripts/jenkins_ha_secrets.py seed-home -n jenkins`. Lệnh này từ chối nếu đã có
   hai replica chạy, vì khi đó mỗi replica đã tự sinh khóa riêng.
4. **Nâng lên hai replica:** `helm upgrade … --set replicas=2`. Cả hai replica giờ dùng chung
   `secrets/`: credential đọc được ở cả hai, và secret của agent giống nhau trên cả hai.

Chart từ chối (CI kiểm từng trường hợp trong `ci/charts.sh`):
- image không có digest;
- image store không có tag;
- agent có "nhà" là replica không tồn tại;
- thiếu tên một Secret;
- `fence` không phải `kubernetes` hay `kubernetes-power`;
- kube-vip mà không có địa chỉ.

## 7. Bước 4: VIP, chọn một trong hai

**A. Octavia** (`balancer.vip.mode: loadBalancer`): Service `ui` kiểu LoadBalancer, đặt annotation
của cloud-provider-openstack trong `balancer.vip.annotations`. Chart đặt
`externalTrafficPolicy: Local`, nên Octavia phải có health monitor để không gửi request tới node
không có HAProxy. Xem tài liệu bản cloud-provider-openstack của site để biết annotation tương ứng
(ví dụ `loadbalancer.openstack.org/enable-health-monitor`). *Chưa kiểm chứng ở lab.*

**B. kube-vip** (`balancer.vip.mode: kubevip`, `address`, `interface`): VIP ARP. Trên OpenStack,
port của **mỗi** node chạy HAProxy phải cho phép địa chỉ VIP, nếu không Neutron sẽ chặn gói:

```bash
openstack port set --allowed-address ip-address=<VIP> <port-id của node>
```

Lab chạy mode này trên mạng KVM. *Bước allowed-address-pairs chưa kiểm chứng.*

## 8. Bước 5: fence bằng Nova (supervisor)

Supervisor nằm trong chart `deploy/helm/netci` (`supervisor.*`). Các bước:

1. **Application credential** chỉ được làm việc trên các VM controller: `GET /servers/{id}`,
   `POST /servers/{id}/action` (`os-stop`, `os-start`). Dùng access rules, hoặc một project chỉ chứa
   các VM đó.
2. **Tắt ngay khi được lệnh:** đặt property `os_shutdown_timeout=0` cho image (hoặc flavor) của các
   VM controller. Mặc định Nova chờ guest tắt êm, nên fence sẽ chậm hơn nhiều.
3. **Secret cấu hình fence**, gồm `fence.json` và một thư mục credential. Trỏ tới nó bằng
   `supervisor.fence.configSecretName` và `supervisor.fence.items`:
   ```json
   {"nodes": {
     "ctl-1": {"nova": {"authURL": "https://keystone.site:5000/v3", "region": "RegionOne",
                        "server": "<uuid VM ctl-1>", "credentials": "/etc/netci/fence/openstack"}},
     "ctl-2": {"nova": {"authURL": "https://keystone.site:5000/v3", "region": "RegionOne",
                        "server": "<uuid VM ctl-2>", "credentials": "/etc/netci/fence/openstack"}}}}
   ```
   Thư mục credential chứa `application_credential_id`, `application_credential_secret`, và
   `ca.crt` nếu Keystone dùng CA riêng. Nếu hai node trỏ cùng một server, cấu hình bị từ chối.
4. **Gắn nhãn** cho các node chạy replica:
   `kubectl label node ctl-1 ctl-2 netci.io/fence-on-loss=true`. Một node có nhãn mà kubelet im
   lặng quá `NETCI_NODE_FENCE_AFTER` (mặc định 45 s) thì sẽ bị tắt nguồn, máy được xác nhận đã tắt,
   rồi node nhận taint out-of-service.
5. **Fence chặt** cho replica: `jenkins.fence: kubernetes-power` (mặc định của chart). Một replica
   chỉ được coi là đã chết khi có taint, hoặc khi kubelet của một node Ready báo tiến trình đã dừng.

*Rủi ro đã nêu trong ADR-084:*
- Hypervisor chết thì Nova không tắt được VM. Máy đó không được fence và build không được nhận lại,
  cho tới khi người vận hành đánh dấu compute service down. Hệ thống đứng chờ, không chạy build
  hai lần.
- Credential này tắt được VM controller, nên hãy giới hạn quyền của nó.

## 9. Bước 6: agent và IDP

**Agent trên VM lớn** (không phải pod):
- `JENKINS_URL=http://<VIP>/` và `JENKINS_WEB_SOCKET=true`;
- secret lấy ở trang agent trên bất kỳ replica nào (sau khi seed, hai bên giống nhau);
- khai báo trong `agents.homes` (ví dụ `agent-0: jenkins-0`): HAProxy đưa agent về replica "nhà",
  và sang replica kia khi nhà chết.

Agent ngoài cụm dùng VIP: máy nhận VIP trả RST cho kết nối chết. Agent là pod thì dùng
`haproxy-local` (ADR-080). Nên đặt `-Dsun.net.client.defaultConnectTimeout=3000`.

**IDP** (chart `deploy/helm/netci-platform`):

```yaml
jenkins:
  controllers:
    - {id: A, url: "http://<VIP>", username: netci, executors: 2}   # đúng MỘT controller: VIP của các replica
  runQueueUrl: http://queue.jenkins.svc.cluster.local:8080
secrets:
  runQueueToken: "<token client; netci-queue giữ SHA-256 của nó>"    # hoặc key run-queue-token trong existingSecret
```

Chart IDP từ chối nếu `runQueueUrl` đi cùng nhiều hơn một controller, hoặc thiếu token.

## 10. Kiểm tra sau khi cài (làm tại site, ghi kết quả)

1. **Mỗi replica sẵn sàng.** Chạy
   `kubectl -n jenkins exec jenkins-0 -- curl -s localhost:8080/netci-ready/` và kết quả phải là
   `ready`; làm tương tự với jenkins-1.
2. **Store khỏe.** `kubectl -n jenkins get cluster store` phải báo `Cluster in healthy state`, với
   `ContinuousArchiving=True`.
3. **Backup và restore thật.**
   - Tạo một `Backup` với `method: plugin` và chờ `completed`.
   - Dựng một Cluster mới từ `externalClusters` có `plugin: {name: barman-cloud.cloudnative-pg.io,
     parameters: {barmanObjectName: store-backups, serverName: store}}`.
   - So số dòng `home_items` và `home_build_numbers` với store. Lab làm đúng như vậy bằng
     `store.sh restore-test`.
4. **Đăng nhập qua VIP**, tạo một job, rồi xóa pod của replica đang phục vụ bạn. Trang kế tiếp phải
   vẫn đăng nhập, và form gửi đi không bị 403 (ADR-077, ADR-086).
5. **Agent:** chạy một build dài trên agent của VM, rồi tắt máy chạy replica sở hữu build. Build
   phải được replica kia nhận lại sau khi supervisor fence.
6. **Fence bằng Nova:** cắt mạng của một node controller (không tắt máy). Supervisor phải tắt VM
   qua Nova; trong lúc chờ không có build nào chạy hai lần.
7. **VIP:** tắt máy đang giữ VIP. UI phải trả lời lại trong vài giây.

## 11. Vận hành

- **Nâng cấp controller.** Mỗi thay đổi plugin là một tag và digest mới. Đổi `images.controller` rồi
  chạy `helm upgrade`.
  - StatefulSet cuốn chiếu từng replica, chờ `minReadySeconds` (30 s) giữa hai replica.
  - Replica sắp dừng vào drain (ADR-089):
    - agent-check trên cổng 8098 báo `maint`, nên HAProxy không gửi request mới tới nó;
    - request đang chạy được làm nốt; preStop đợi tới khi không còn request nào, tối đa
      `drainSeconds` (30 s);
    - nếu site có NetworkPolicy, phải mở cổng 8098 từ HAProxy tới các pod Jenkins.
  - Lab đo được 324/324 lần đọc UI.
- **Đổi `haproxy.cfg`** (qua chart): balancer tự kiểm tra config mới bằng `haproxy -c` rồi reload tại
  chỗ trong khoảng một phút, không thay pod. Config bị `haproxy -c` từ chối thì không được áp dụng,
  và log balancer ghi `netci: the changed haproxy.cfg is refused`.
- **Nâng cấp Barman Cloud plugin** (Recreate): trong vài giây giữa hai pod, store không failover
  được. Đừng nâng cấp lúc đang có máy bảo trì.
- **Nâng cấp CNPG operator:** lên 1.31 trở đi thì phải dùng `backup.mode: plugin`, vì 1.31 bỏ
  in-tree.
- **Mất một máy:** không cần làm gì. Supervisor fence, store tự chuyển primary, build được nhận lại.
  Khi máy về, supervisor gỡ taint.
- **Xoay khóa.** Chạy `jenkins_ha_secrets.py generate --rotate`, rồi restart homesync và các replica.
  - Đổi session key thì mọi người phải đăng nhập lại.
  - Đổi token homesync thì replica cũ không ghi được cho tới khi restart.
- **Thêm replica:**
  1. tăng `replicas`;
  2. chạy `generate --rotate --replicas N`;
  3. thêm `agents.homes` nếu cần;
  4. bảo đảm đủ máy (anti-affinity bắt buộc).
- **Khôi phục sau sự cố lớn.** Dựng store từ backup theo mục 10.3, rồi trỏ chart vào nó. Home của
  các replica tự đồng bộ lại từ store khi khởi động (ADR-083: replica tụt hậu thì bootstrap lại).

## 12. Giới hạn đã biết (nói thẳng)

- **Script đăng nhập bằng mật khẩu và giữ crumb** vẫn bị 403 sau khi replica của nó restart.
  Script nên dùng API token (ADR-086).
- **View ở cấp gốc** được đồng bộ (ADR-088). View nào là view chính thì do từng replica tự giữ,
  theo JCasC.
- **Đổi image HAProxy** vẫn phải thay pod balancer. Khi dùng kube-vip, VIP có thể chuyển máy một
  lần trên mỗi máy (khoảng 2 s kết nối bị từ chối, ADR-089). Hãy làm việc này ngoài giờ cao điểm.
  Riêng đổi `haproxy.cfg` thì được reload tại chỗ, không làm rớt kết nối nào.
- **Đổi tên và di chuyển item** được đồng bộ (ADR-087). Thao tác bị từ chối (409, kèm lý do) khi
  một build của item đang chạy trên bất kỳ replica nào. Một run của netci-queue xếp cho tên cũ sẽ
  nhận 404 sau khi đổi tên, nên đừng đổi tên các job mà IDP tạo.
- **Ngân sách hàng đợi của IDP (ADR-050)** chỉ đếm hàng đợi của một replica. Giới hạn thực tế là
  budget của netci-queue (ADR-063).
- **Build bị nhận lại** dựa trên bản sao chạy mỗi 3 s: vài giây log cuối có thể chỉ còn trong Loki
  (ADR-068/079).
