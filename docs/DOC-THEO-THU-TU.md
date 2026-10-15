# netCI — Tài liệu đọc theo thứ tự

> **File này được sinh tự động** bởi `scripts/docs/reading_order.py` từ: `docs/TRINH-BAY-TOAN-DIEN-NETCI.md`,
> các ADR 060–072 trong `docs/decisions/`, `docs/BAO-VE-DE-TAI-NETCI.md` và `docs/HUONG-DAN-DEMO-THU-CONG.md`.
> Đừng sửa trực tiếp file này: sửa các file gốc rồi chạy lại script.
> Số phần ("Phần 3", "mục 3.7", "TB 3.7") giữ nguyên như trong tài liệu trình bày, nên các tham chiếu chéo
> vẫn đúng. "TB" là tài liệu trình bày toàn diện.

## Lộ trình

| Bước | Đọc | Khi nào |
|---|---|---|
| 1 | Nền tảng | Tuần 1 |
| 2 | Hiểu netCI | Tuần 2 |
| 3 | Kiểm chứng, bài học, giới hạn | Tuần 3 |
| 4 | Các quyết định kiến trúc (ADR, tiếng Anh) | Tuần 3 |
| 5 | Đọc code (mở trong trình soạn thảo, đọc kèm test) | Tuần 3 |
| 6 | Chuẩn bị bảo vệ | Tuần 4 |
| 7 | Demo thủ công | Tuần 4 |
| 8 | Tự kiểm tra | Tuần 4 |
| — | dựng lại, vận hành, lộ trình kỹ năng, thuật ngữ, câu lệnh, bản đồ mã nguồn | Khi cần |

Nếu ít thời gian: Bước 1, Bước 2 (phần 8 và 9), Bước 6, Bước 7.

---

# Bước 1 — Nền tảng

> **Tuần 1.** Đọc câu chuyện để hiểu vì sao có đề tài, rồi học Jenkins, container, Kubernetes, storage, hệ phân tán, BMC. Vừa đọc vừa làm bài tập 17.3 số 1–3 (ở phần Tham khảo cuối file). Gặp từ lạ: Phụ lục A ở cuối file.

## 1. Câu chuyện: một đêm Jenkins sập

### 1.1 Bối cảnh (use case thực tế)

Một công ty phần mềm cỡ vừa: vài chục đội phát triển, vài trăm repository. Mọi thứ được build và
deploy bằng **Jenkins mã nguồn mở**:
- mỗi repo có một `Jenkinsfile` mô tả cách build, test, deploy;
- plugin do công ty tự chọn;
- agent (máy chạy build) là các **VM sống lâu**, dùng đi dùng lại.

Jenkins ở đây không chỉ build. Nó còn **deploy** lên production, chạy migration cơ sở dữ liệu, phát
hành bản release. Nó là "dây chuyền sản xuất" của cả công ty.

Một đêm, máy vật lý chạy Jenkins controller mất điện. Nguyên nhân không quan trọng: nguồn hỏng, kernel
treo, card mạng chết. Sáng hôm sau, đội vận hành thấy:

| Hiện tượng | Vì sao (bản chất) |
|---|---|
| Không ai vào được Jenkins suốt lúc máy chết và cả lúc khởi động lại | Chỉ có **một** controller: nó là điểm chết duy nhất (single point of failure) |
| Các build đang chạy dở bị đánh dấu thất bại, hoặc treo | Pipeline chỉ chạy tiếp được nếu controller quay lại kịp và agent nối lại được |
| Các build đang **xếp hàng** biến mất, không dấu vết | Jenkins giữ hàng đợi trong RAM. `queue.xml` chỉ được ghi khi tắt **có trật tự** (lỗi JENKINS-30909). Mất điện thì không có lúc tắt có trật tự |
| Webhook từ GitLab gửi trong lúc sập: mất, hoặc gửi lại thì build hai lần | Không có chỗ bền vững nào nhận và khử trùng lặp webhook |
| Một bước deploy **chạy hai lần** sau khi khôi phục | Bản ghi "bước đã bắt đầu" còn nằm trong page cache, chưa xuống đĩa, nên mất cùng máy. Khi chạy tiếp, Jenkins tưởng bước chưa chạy |
| Khôi phục bằng backup mất hàng giờ, thiếu dữ liệu của những giờ gần nhất | Backup là khôi phục thảm hoạ, không phải chuyển đổi dự phòng (failover) |
| Build hôm sau lỗi lạ vì file rác của build hôm trước trên agent | Agent sống lâu tích tụ trạng thái từ build này sang build khác |
| Giờ cao điểm build xếp hàng dài, giờ thấp điểm VM agent nằm không | Năng lực cố định, không co giãn theo nhu cầu |
| Một PR từ người ngoài (fork) chạy code lạ chung kernel với build deploy production | Không có mức cô lập theo độ tin cậy |

### 1.2 Vì sao không "mua CloudBees là xong"

CloudBees CI là bản thương mại của Jenkins. Nó có tính năng **High Availability active/active**:
- nhiều bản sao (replica) của một controller cùng chạy trên một file system dùng chung (NFS);
- trạng thái sống được giữ trong Hazelcast (một bộ nhớ phân tán);
- khi một replica chết, replica khác "nhận nuôi" các Pipeline đang chạy.

Nhưng:
- Tốn tiền bản quyền theo người dùng, và buộc vào một nhà cung cấp.
- Chính tài liệu CloudBees ghi nhiều plugin bị giới hạn hoặc không hỗ trợ trong chế độ HA (Docker,
  EC2, một số thiết lập của Kubernetes plugin, Blue Ocean, file parameter).
- Công ty muốn giữ Jenkins mã nguồn mở, giữ Jenkinsfile và UI quen thuộc.

**Đề bài của netCI**: xây một lớp HA (high availability, sẵn sàng cao) và một "agent fabric" (lớp cung
cấp máy chạy build) bao quanh Jenkins mã nguồn mở. Nó phải ngang CloudBees ở những gì quan trọng
(không mất build, không mất hàng đợi, không chạy trùng), đủ chất lượng production, có thể bán được.
Phần lõi viết bằng Go, plugin Jenkins viết bằng Java.

### 1.3 Đề tài gồm những gì

netCI là một **Internal Developer Platform (IDP)**: bài toán lớn. Developer dùng portal để đưa module
lên, build, ký ảnh, kiểm SBOM, deploy có phê duyệt (Phần 11). Mọi build của IDP chạy trên Jenkins,
nên Jenkins sập là cả IDP đứng.

**HA cho Jenkins** là bài toán con, và là phần đang được đi sâu nhất (Phần 2–10, 12–16): chữa đúng nỗi
đau ở 1.1. Hai phần nằm chung một repo (ADR-073). Việc nối IDP vào lớp HA, tức là build của IDP đi qua
hàng đợi bền tới các cell sống sót khi mất máy, đang làm.

## 2. Nền tảng 1 — Jenkins từ con số 0

### 2.1 CI/CD là gì

- **CI (Continuous Integration)**: mỗi lần ai đó đẩy code lên, một hệ thống tự động lấy code về,
  build, chạy test, báo kết quả. Mục đích: phát hiện lỗi sớm.
- **CD (Continuous Delivery/Deployment)**: tiếp theo CI, tự động đóng gói và triển khai bản đã qua
  test lên môi trường (staging, production).
- **Jenkins** là một trong những công cụ CI/CD lâu đời và phổ biến nhất. Nó là mã nguồn mở, viết bằng
  Java, và có khoảng 2 000 plugin.

### 2.2 Các khái niệm cơ bản của Jenkins

Hình dung Jenkins như một **nhà máy**: có một quản đốc giao việc và nhiều công nhân làm việc.

| Khái niệm | Hình dung | Thực chất |
|---|---|---|
| **Controller** (trước gọi là master) | Quản đốc | Một tiến trình Java (JVM) chạy `jenkins.war`. Giữ cấu hình, danh sách job, lịch sử build, hàng đợi, giao diện web (UI) |
| **Agent** (trước gọi là slave/node) | Công nhân | Nơi build thực sự chạy. Là một tiến trình Java (`agent.jar`) chạy trên máy khác, nối về controller |
| **Executor** | Một bàn làm việc của công nhân | Một "khe" chạy được một build. Agent có N executor thì chạy song song được N build |
| **Job** (hay project, item) | Một loại sản phẩm | Định nghĩa một việc: lấy code ở đâu, chạy gì |
| **Build** (hay run) | Một lần sản xuất | Một lần chạy job, có số thứ tự (#1, #2…), có log (console), có kết quả: `SUCCESS`, `FAILURE`, `UNSTABLE`, `ABORTED` |
| **Pipeline** | Quy trình nhiều công đoạn | Kiểu job mô tả bằng code (Groovy), gồm nhiều stage và step |
| **Jenkinsfile** | Bản vẽ quy trình | File chứa Pipeline, nằm trong repo |
| **Plugin** | Đồ nghề bổ sung | Phần mở rộng (file `.jpi`/`.hpi`) thêm tính năng: git, Kubernetes, credentials… |
| **Label** | Tay nghề của công nhân | Nhãn trên agent (ví dụ `linux`, `netci-standard`); build xin "một agent có nhãn X" |
| **Queue** (hàng đợi) | Bảng việc chờ | Danh sách build đang chờ executor. **Nằm trong RAM** |
| **`JENKINS_HOME`** | Kho hồ sơ của nhà máy | Thư mục trên đĩa chứa *toàn bộ* trạng thái: `config.xml`, job, build, log, plugin, credential đã mã hoá. **Đây là thứ phải sống sót khi mất máy** |
| **Credential** | Chìa khoá kho | Mật khẩu, token, SSH key lưu trong Jenkins (mã hoá bằng `secrets/master.key`) |

### 2.3 Một Jenkinsfile và điều gì xảy ra khi nó chạy

```groovy
pipeline {
  agent { label 'netci-standard' }          // xin một agent có nhãn netci-standard
  stages {
    stage('Build') {
      steps { sh 'make build' }              // chạy lệnh shell trên agent
    }
    stage('Deploy') {
      steps {
        netciOnce('deploy-prod') {           // bước của netCI: khối này chạy không quá một lần
          sh './deploy.sh'
        }
      }
    }
  }
}
```

Điều xảy ra, từng bước:
1. Có người bấm "Build", hoặc một webhook từ GitLab báo có commit mới.
2. Jenkins tạo một **mục hàng đợi** (queue item) cho job.
3. Pipeline bắt đầu chạy *trên controller* (phần Groovy được thông dịch ở controller, gọi là CPS).
4. Gặp `agent { label ... }`, Jenkins cần một executor có nhãn đó. Nếu chưa có agent nào phù hợp,
   một **Cloud** (2.5) được nhờ tạo agent mới.
5. Gặp `sh 'make build'`: controller không tự chạy lệnh. Nó nhờ plugin **durable-task** làm:
   - ghi một file script vào thư mục làm việc (workspace) trên agent;
   - khởi động một tiến trình "bọc" (wrapper) trên agent. Wrapper chạy script, ghi output vào một file
     log, và khi xong thì ghi mã thoát (exit code) vào một file khác;
   - controller **định kỳ hỏi** agent: "log có thêm gì không? đã có exit code chưa?". Mặc định bắt đầu
     sau 250 ms, giãn dần ×1,2 tới tối đa 15 s khi không có gì mới.
6. Mọi tiến trình của Pipeline được lưu xuống đĩa (`JENKINS_HOME`) thường xuyên.

**Điểm then chốt cho HA**: tiến trình `sh` là của *agent*, và nó **sống lâu hơn controller**. Nếu
controller chết rồi sống lại (hoặc một controller khác mở cùng `JENKINS_HOME`), nó đọc lại trạng thái
Pipeline từ đĩa. Agent nối lại, controller lại hỏi file log và exit code, và build **chạy tiếp** đúng
chỗ đang dở. Đó là **durability** (độ bền) của Pipeline. Mức `MAX_SURVIVABILITY` (mặc định) là bền nhất.
Điều kiện: agent phải nối lại được trong khoảng 5 phút (sau đó build bỏ cuộc), và đĩa phải còn đúng
trạng thái.

### 2.4 Agent nối về controller như thế nào (remoting)

- Agent chạy `java -jar agent.jar -url http://controller:8080/ -name <tên> -secret <bí mật> -webSocket`.
- **Inbound agent**: agent chủ động gọi về controller (ngược với SSH agent, nơi controller SSH vào máy).
- **WebSocket**: kênh nối đi qua cổng HTTP của controller, không cần mở cổng TCP riêng (50000).
- **Secret** của agent: một chuỗi 64 hex do controller sinh cho từng node, chứng minh "tôi đúng là agent
  này". Lộ secret là người khác giả làm agent được, nên netCI **không bao giờ ghi nó xuống đĩa hay CSDL**.
- **Remoting** là thư viện Java cho kênh nối đó. Controller gửi lệnh và cả **class Java** sang agent
  qua kênh này. Agent cần class nào thì tải cả file jar chứa nó từ controller và cất vào **jar cache**
  (`<workDir>/remoting/jarCache`). Một agent mới tinh có cache trống, nên tải khoảng 19 MB (Phần 10.9,
  ADR-072).
- **`X-Jenkins-Session`**: header HTTP mà controller trả ở mọi trang, có giá trị **mới mỗi lần JVM khởi
  động**. netCI dùng nó để nhận ra "controller đã là một phiên khác" (đã khởi động lại hoặc đã được
  thay).

### 2.5 Cloud và việc tự tạo agent

- **Cloud** (trong Jenkins) là một API để Jenkins *tự tạo agent khi cần*: Kubernetes plugin (mỗi build
  một pod), EC2 plugin (mỗi build một VM)… netCI có cloud riêng tên `netci`.
- **NodeProvisioner** là bộ quyết định "có cần tạo thêm agent không". Mặc định nó chờ 100 s sau khởi
  động, rồi xem xét 10 s một lần, và "làm mượt" (chờ xem tải có kéo dài không). netCI chỉnh để tạo agent
  ngay (`NoDelayProvisioning`, `ProvisionOnEnqueue`, và chu kỳ 2 s).
- **RetentionStrategy**: khi nào gỡ agent. netCI dùng `OnceRetentionStrategy`: chạy đúng một build rồi
  gỡ.

### 2.6 Cấu hình bằng code

- **JCasC** (Jenkins Configuration as Code): cấu hình Jenkins bằng một file YAML (`jenkins.yaml`): bảo
  mật, cloud, công cụ… Đổi file là đổi cấu hình; netCI reload nóng mà không cần khởi động lại.
- **Job DSL**: tạo job bằng code (Groovy) trong JCasC hoặc qua "seed job".
- **Plugin manager CLI** (`jenkins-plugin-cli`): cài plugin theo `plugins.txt` khi build image.

### 2.7 Những điểm yếu của Jenkins mã nguồn mở mà netCI phải lách

| Điểm yếu | Hệ quả | netCI xử lý |
|---|---|---|
| Hàng đợi chỉ trong RAM | Mất điện là mất mọi build đang chờ | Hàng đợi bền netci-queue (10.6) |
| Một JVM giữ mọi thứ, ghi XML không phối hợp | Không thể chạy hai controller trên một `JENKINS_HOME` | "Một người ghi, chuyển nhanh" (7.1) |
| Ghi đĩa qua page cache | Mất điện mất vài giây đến vài chục giây trạng thái | Giới hạn writeback, ext4 commit 1 s, `netciOnce` (10.1, 10.7) |
| Agent tĩnh, sống lâu | Build bẩn, không co giãn | Fabric: mỗi build một sandbox mới (10.8) |
| Không biết "lần này" và "lần trước" có phải cùng controller | Gửi trùng, chờ nhầm | `X-Jenkins-Session` |

### 2.8 Thực hành tối thiểu với Jenkins (trên máy bất kỳ có Docker)

```bash
docker run -d --name j -p 8080:8080 jenkins/jenkins:lts-jdk21
docker logs j 2>&1 | grep -A2 "initialAdminPassword"   # mật khẩu lần đầu
# Mở http://localhost:8080, cài plugin gợi ý, tạo job Pipeline với script ở 2.3 (bỏ netciOnce),
# đổi agent thành "agent any". Chạy, xem Console Output.
# Thử durability: tạo job với  sh 'for i in $(seq 1 120); do echo $i; sleep 1; done'
# chạy, rồi giữa chừng:  docker restart j  — sau khi Jenkins lên lại, build chạy tiếp.
# Thử mất hàng đợi: bấm Build nhiều lần cho job có quiet period, rồi  docker kill j  và  docker start j.
```

## 3. Nền tảng 2 — Container và Kubernetes từ con số 0

### 3.1 Máy ảo (VM) và lab

- **KVM**: tính năng ảo hoá trong kernel Linux. **QEMU**: giả lập phần cứng cho VM.
- **libvirt** (lệnh `virsh`) quản lý VM:
  - `virsh list`: xem VM;
  - `virsh destroy <vm>`: **rút điện ngay**, như mất điện;
  - `virsh suspend <vm>`: đóng băng, như **máy treo**;
  - `virsh start <vm>`, `virsh resume <vm>`.
- **cloud-init**: cấu hình VM ở lần khởi động đầu (user, SSH key, sysctl). `lab/vms.sh` dùng nó.
- **Nested virtualisation**: VM chạy được VM bên trong. Kata trong lab cần tính năng này.
- **qcow2 thin disk**: đĩa ảo chỉ chiếm chỗ thật khi được ghi. Nếu đĩa máy chủ đầy, QEMU **tạm dừng
  (pause)** VM. Lab đã gặp chuyện này hai lần.

### 3.2 Container

- **Container** là một tiến trình bình thường bị cô lập:
  - *namespace*: nó chỉ thấy tiến trình, file, mạng, user của riêng nó;
  - *cgroup*: bị giới hạn CPU, RAM.
- **Image** (ảnh): gói chứa hệ file của container, gồm nhiều lớp (layer). Ví dụ `jenkins/jenkins:lts`.
- **Tag** (`:1.0`) có thể bị ghi đè sang ảnh khác. **Digest** (`@sha256:…`) là mã băm nội dung, **bất
  biến**. netCI chỉ chạy ảnh theo digest.
- **Registry**: kho ảnh (Docker Hub, Harbor).
- **runc**: chương trình tạo container theo chuẩn OCI. **containerd**: daemon quản lý container (k3s
  dùng nó). **crictl**: lệnh xem container ở mức containerd (`sudo k3s crictl ps`).
- **User namespace** (`hostUsers: false`): root trong container được ánh xạ thành một uid không có quyền
  trên máy thật.
- **seccomp**: lọc system call. **capabilities**: chia nhỏ quyền root; netCI bỏ hết (`drop: [ALL]`).
- **Kata Containers**: chạy mỗi pod trong một VM nhẹ có **kernel riêng**, cô lập mạnh hơn nhiều.

### 3.3 Kubernetes là gì và vì sao cần

Khi có nhiều máy và nhiều container, cần một hệ thống:
- chọn máy nào chạy container nào (lập lịch);
- tự chạy lại container khi nó chết;
- cấp đĩa, mạng, địa chỉ ổn định;
- cho phép mô tả "trạng thái mong muốn" và tự đưa hệ thống về trạng thái đó.

**Kubernetes** (k8s) làm đúng những việc đó. Ý tưởng cốt lõi là **vòng điều khiển (control loop)**: bạn
khai báo "tôi muốn 1 pod Jenkins với đĩa này", và các controller của Kubernetes liên tục so sánh *thực
tế* với *mong muốn* rồi sửa cho khớp. netCI cũng viết theo đúng kiểu này (supervisor, fabric reconciler).

**k3s** là một bản Kubernetes gọn (một binary), đủ chuẩn, dễ dựng trên vài VM. Lab dùng k3s v1.36.4.

### 3.4 Kiến trúc một cụm Kubernetes

```
           ┌──────────────── control plane ────────────────┐
 kubectl ─►│ kube-apiserver  ◄──►  etcd (CSDL, cần quorum)  │
           │   ▲      ▲                                    │
           │   │      └── kube-scheduler (chọn node cho pod)│
           │   └───────── kube-controller-manager           │
           │              (các vòng điều khiển: node,       │
           │               StatefulSet, PodGC, attach…)     │
           └───────────────────────────────────────────────┘
                     ▲                    ▲
          ┌──────────┴───────┐  ┌─────────┴────────┐
          │ node 1           │  │ node 2           │
          │ kubelet          │  │ kubelet          │  ...
          │ containerd       │  │ containerd       │
          │ pod, pod, …      │  │ pod, …           │
          └──────────────────┘  └──────────────────┘
```

- **kube-apiserver**: cổng vào **duy nhất**. Mọi thứ (kubectl, kubelet, controller, netCI) đều đọc ghi
  qua nó, bằng HTTP/REST.
- **etcd**: CSDL key-value lưu mọi đối tượng. Cần **đa số** thành viên (quorum) còn sống mới ghi được:
  3 thành viên thì cần 2.
- **kube-scheduler**: chọn node cho pod mới.
- **kube-controller-manager**: chạy nhiều vòng điều khiển.
- **kubelet**: tác nhân trên mỗi node, chạy pod và báo trạng thái. Nó gia hạn **node lease** khoảng 10 s
  một lần để báo "tôi còn sống".
- **Leader election**: controller-manager, scheduler (và netCI supervisor, fabric) chạy nhiều bản sao
  nhưng chỉ một bản làm việc, nhờ một **Lease** (3.6). Leader chết thì bản khác lên thay sau vài chục
  giây.
- **[Lab]** Cả 3 VM đều vừa là control plane vừa chạy pod. **[Prod]** Tách control plane ra máy riêng.

### 3.5 Các đối tượng cần biết (kèm ví dụ)

Mọi đối tượng là một tài liệu YAML/JSON có `apiVersion`, `kind`, `metadata` (tên, namespace, nhãn) và
`spec` (mong muốn), thường kèm `status` (thực tế).

| Đối tượng | Là gì | netCI dùng để |
|---|---|---|
| **Namespace** | "Thư mục" nhóm đối tượng, có quyền riêng | Mỗi cell một namespace (`cell-a`, `cell-b`); `netci-system`; `netci-agents` |
| **Node** | Một máy trong cụm | Mỗi VM của lab |
| **Pod** | Đơn vị chạy nhỏ nhất: một hoặc nhiều container dùng chung mạng và volume | Controller Jenkins là một pod; mỗi sandbox build là một pod |
| **Init container** | Container chạy xong trước container chính | `install-guard`, `install` của sandbox |
| **Native sidecar** | Init container có `restartPolicy: Always`: chạy suốt cùng pod, khởi động trước | `cell-agent` |
| **Deployment** | N pod giống nhau, thay thế tự do | supervisor, queue, fabric |
| **StatefulSet** | Pod có **tên cố định** (`jenkins-0`) và **đĩa riêng** theo `volumeClaimTemplates` | Mỗi cell là một StatefulSet 1 bản sao |
| **DaemonSet** | Mỗi node một pod | longhorn-manager, kata-deploy |
| **Service** | Địa chỉ ổn định (DNS + IP ảo) cho một nhóm pod | `jenkins.cell-b.svc.cluster.local` |
| **NodePort** | Mở một cổng trên mọi node tới Service | Lab: cell-a 30080, cell-b 30081, queue 30090 |
| **ConfigMap / Secret** | Cấu hình / bí mật, gắn vào pod dạng file hay biến môi trường | JCasC, cấu hình fabric, mật khẩu BMC |
| **PV / PVC / StorageClass** | PVC là "yêu cầu đĩa", PV là "đĩa thật", StorageClass là "loại đĩa" | `JENKINS_HOME` là một PVC Longhorn |
| **Lease** | Đối tượng nhỏ thể hiện "ai đang giữ quyền, tới bao giờ" | Mỗi cell có Lease `jenkins` |
| **ServiceAccount, Role, RoleBinding (RBAC)** | Danh tính của pod và quyền của nó | Cell chỉ được sửa đúng Lease của nó |
| **Taint / toleration** | Taint trên node đẩy pod đi; toleration cho pod chịu được | Taint `out-of-service` |
| **PriorityClass** | Mức ưu tiên; pod cao có thể đẩy (preempt) pod thấp | platform > cell > sandbox |
| **PodDisruptionBudget (PDB)** | Giới hạn số pod được làm gián đoạn tự nguyện | Không cho preempt build đang chạy |
| **NetworkPolicy** | Tường lửa giữa các pod | Sandbox chỉ ra được nơi cần |
| **RuntimeClass** | Chọn runtime (runc hay kata) | Pool `untrusted` dùng Kata |
| **VolumeAttachment** | Ghi "volume X đang gắn vào node Y" | Supervisor đếm attachment trên node đã fence |
| **Event** | Nhật ký sự kiện của đối tượng | `kubectl get events` |

Một StatefulSet tối thiểu cho Jenkins (rút gọn từ chart `netci-cell`):

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: jenkins, namespace: cell-b}
spec:
  replicas: 1
  serviceName: jenkins
  selector: {matchLabels: {app: jenkins}}
  template:
    metadata: {labels: {app: jenkins}}
    spec:
      containers:
        - name: jenkins
          image: 172.17.0.1:8930/netci/jenkins-controller@sha256:...   # luôn theo digest
          volumeMounts: [{name: home, mountPath: /var/jenkins_home}]
  volumeClaimTemplates:
    - metadata: {name: home}
      spec:
        accessModes: [ReadWriteOnce]          # chỉ gắn vào một node mỗi lúc
        storageClassName: longhorn-sync       # đĩa nhân bản 3 bản sao
        resources: {requests: {storage: 10Gi}}
```

Một Lease (đúng như trong cụm):

```yaml
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata: {name: jenkins, namespace: cell-b, resourceVersion: "123456"}
spec:
  holderIdentity: jenkins-0/2f6c…           # <tên pod>/<uid pod>
  leaseDurationSeconds: 15
  renewTime: "2026-10-12T03:44:10.123456Z"
  leaseTransitions: 7                        # số lần đổi chủ: dùng như "epoch"
```

### 3.6 Ba cơ chế Kubernetes mà netCI dựa vào

1. **Cập nhật có điều kiện (optimistic concurrency)**. Mỗi đối tượng có `resourceVersion`. Gửi cập nhật
   kèm `resourceVersion` cũ mà ai đó đã sửa trước thì API server từ chối (409 Conflict). Nhờ vậy:
   - hai cell agent không cùng giữ một Lease;
   - supervisor nhả Lease "chỉ khi nó vẫn đúng như lúc mình nhìn thấy".
2. **Taint `node.kubernetes.io/out-of-service`** (tính năng *non-graceful node shutdown*, GA từ 1.28).
   Gắn taint này lên một node nghĩa là khẳng định "node đã tắt hẳn". Kubernetes liền xoá pod trên đó và
   gỡ volume **mà không chờ kubelet xác nhận**. Tài liệu yêu cầu node phải *thật sự* tắt, nên netCI xác
   nhận Off với bộ điều khiển nguồn trước.
3. **Leader election bằng Lease**. Bản giữ Lease làm việc; mất Lease thì dừng.

### 3.7 Điều gì xảy ra khi một node chết, nếu chỉ có Kubernetes (đây là lý do có netCI)

Giả sử pod `jenkins-0` của một StatefulSet đang chạy trên node 2, và node 2 mất điện:

| Sau | Kubernetes làm gì | Hệ quả với Jenkins |
|---|---|---|
| ~40–50 s | Node lease không được gia hạn, node bị đánh dấu `NotReady`/`Unknown`, gắn taint `unreachable` | Jenkins đã chết từ giây 0, chưa có gì thay |
| ~5 phút | Pod bị "đuổi" (toleration mặc định 300 s), chuyển sang `Terminating` | Pod **kẹt Terminating mãi**: kubelet đã chết nên không ai xác nhận container đã dừng |
| mãi mãi | StatefulSet **không tạo `jenkins-0` mới** khi `jenkins-0` cũ còn tồn tại trong API (tên cố định, đảm bảo "nhiều nhất một") | Volume RWO vẫn ghi là gắn vào node 2 |

Cách "chữa" thường thấy là người trực **force-delete** pod. Nhưng nếu node 2 thật ra chỉ bị *mất mạng*,
chưa chết, thì Jenkins cũ vẫn đang ghi đĩa, và pod mới sẽ ghi cùng: **split-brain**, hỏng `JENKINS_HOME`.

netCI tự động làm việc mà người trực sợ làm, nhưng làm **an toàn**: biết chắc máy đã tắt (hỏi bộ điều
khiển nguồn, hoặc tự tắt nó), rồi mới dùng taint out-of-service, trong khoảng 3 s thay vì vài phút.

### 3.8 Những lệnh kubectl cần thuộc

```bash
kubectl get nodes -o wide                       # máy, trạng thái, IP, kernel
kubectl get pods -A -o wide                     # mọi pod, ở node nào
kubectl -n cell-b describe pod jenkins-0        # chi tiết + Events (vì sao Pending/lỗi)
kubectl -n cell-b logs jenkins-0 -c jenkins --since=10m   # log một container
kubectl -n cell-b exec -it jenkins-0 -c jenkins -- bash   # vào trong container
kubectl -n cell-b get lease jenkins -o yaml     # ai giữ Lease
kubectl get events -A --sort-by=.lastTimestamp | tail
kubectl apply -f x.yaml ; kubectl delete -f x.yaml
kubectl -n cell-b rollout status sts/jenkins    # chờ rollout xong
kubectl top nodes ; kubectl top pods -A         # CPU/RAM thực dùng
```

### 3.9 Helm

- **Helm** là trình quản lý gói của Kubernetes. Một **chart** là bộ template YAML; **values** là tham số.
  `helm upgrade --install <release> <chart> -f values.yaml` sinh manifest và áp vào cụm.
- netCI có 2 chart: `deploy/helm/netci` (nền tảng: supervisor, queue, fabric, giám sát) và
  `deploy/helm/netci-cell` (mỗi cell một release).

### 3.10 Thực hành tối thiểu với Kubernetes

```bash
# Một node k3s trên một VM Linux bất kỳ:
curl -sfL https://get.k3s.io | sh -
sudo k3s kubectl get nodes
# Tạo StatefulSet ở 3.5 (đổi image thành jenkins/jenkins:lts-jdk21, storageClassName: local-path).
# Xem pod, PVC, Service. Xoá pod và xem StatefulSet tạo lại đúng tên jenkins-0, giữ nguyên PVC.
# Với 3 node: tắt hẳn node đang chạy jenkins-0 và quan sát bảng 3.7 trong 10 phút.
```

## 4. Nền tảng 3 — Storage, hệ phân tán, phần cứng, quan sát

### 4.1 Storage: vì sao "đĩa" là khó nhất

- **Page cache**: khi chương trình ghi file, Linux thường chỉ ghi vào RAM (page cache) rồi báo "xong".
  Dữ liệu xuống đĩa thật sau đó vài giây, gọi là **writeback**. Mất điện là mất phần chưa xuống đĩa.
  - `vm.dirty_expire_centisecs`: dữ liệu "bẩn" bao lâu thì phải ghi (mặc định 3000 = 30 s).
  - `vm.dirty_writeback_centisecs`: bao lâu thì luồng writeback chạy một lần (mặc định 500 = 5 s).
- **fsync**: lời gọi hệ thống "đẩy file này xuống đĩa ngay, chờ tới khi xong". Chương trình cẩn thận
  (PostgreSQL) gọi fsync; Jenkins phần lớn thì không.
- **ext4 journal, `commit=N`**: ext4 ghi metadata qua journal, mặc định mỗi 5 s. `commit=1` là mỗi 1 s.
  Mount option `sync,dirsync`: mọi lần ghi đều đồng bộ (an toàn hơn, chậm hơn).
- **Block storage và file storage**: block là "một ổ đĩa ảo" (một máy gắn, tự format ext4); file storage
  là "thư mục dùng chung qua mạng" (NFS, nhiều máy gắn).
- **RWO (ReadWriteOnce)**: volume chỉ gắn vào **một node** mỗi lúc. Với netCI đó là một lớp chống hai
  controller cùng ghi.
- **Nhân bản đồng bộ**: mỗi lần ghi phải được N bản sao trên N máy xác nhận rồi mới báo xong. Máy chết
  thì không mất gì *đã xuống thiết bị khối*. Lưu ý: phần còn nằm trong page cache thì vẫn mất.
- **CSI** (Container Storage Interface): chuẩn plugin storage của Kubernetes.
  - `ControllerPublish`/`Unpublish`: gắn/gỡ volume vào một node (attach/detach).
  - `NodeStage`/`NodePublish`: mount vào máy và vào pod.

### 4.2 Longhorn (storage của lab)

- Mỗi volume có một **engine** (chạy trên node đang dùng volume) và N **replica** (bản sao trên các
  node). Engine ghi đồng bộ vào mọi replica. Lab: 3 replica.
- **instance-manager**: pod trên mỗi node, chạy engine và replica.
- **longhorn-manager**: DaemonSet điều khiển; mỗi volume có một manager "chủ sở hữu".
- **CSI plugin** của Longhorn: `ControllerPublishVolume` chờ volume "published" (attached, engine có
  endpoint), hỏi lại mỗi 2 s, tối đa 90 s. Engine monitor chỉ cập nhật endpoint **mỗi 5 s**, nên một lần
  attach mất khoảng 4 s hoặc khoảng 10 s (hai mức).
- Tham số quan trọng:
  - `numberOfReplicas`, `staleReplicaTimeout`;
  - `storage-over-provisioning-percentage` (lab 100%) và `storage-minimal-available-percentage` (25%):
    quyết định còn bao nhiêu chỗ để tạo volume mới;
  - `disable-scheduling-on-cordoned-node` (true).
- **[Prod]** Có thể thay bằng Ceph RBD, vSAN, hay SAN của tổ chức, miễn là block RWO nhân bản qua nhiều
  máy và có CSI.

### 4.3 Hệ phân tán: những ý tưởng nền

- **Split-brain**: hai bên cùng tin mình là "chủ" và cùng ghi, dữ liệu hỏng. **Kẻ thù số một.**
- **Lease** (Gray & Cheriton, 1989): quyền có thời hạn. Người giữ phải gia hạn; **không gia hạn được thì
  phải tự dừng *trước* khi lease hết hạn**, để người khác nhận lease mà không chồng lấn.
- **Fencing, STONITH** ("Shoot The Other Node In The Head", từ Pacemaker): đừng tin bên kia "chắc đã
  dừng". Phải *làm cho nó dừng*, ví dụ cắt điện, trước khi giao tài nguyên cho người khác. Kleppmann
  (2016) chỉ ra rằng khoá thôi là không đủ: một tiến trình bị dừng tạm (GC pause, VM bị đóng băng) có thể
  tỉnh dậy và vẫn ghi.
- **Fencing token, epoch**: số tăng dần mỗi lần đổi chủ (ở đây là `leaseTransitions`). Người ghi cũ mang
  số cũ thì bị từ chối.
- **Failure detector**: phát hiện càng nhanh thì càng hay nghi nhầm. netCI tách hai việc:
  - *nghi* thì nhanh và rẻ (3 s, chỉ để đi hỏi);
  - *quyết* thì dựa vào sự thật (bộ điều khiển nguồn báo máy Off).
- **Quorum**: đa số. etcd 3 thành viên cần 2; tắt một máy sao cho chỉ còn 1 thành viên là cả cụm đứng.
- **Panic guard**: khi quá nhiều thứ hỏng cùng lúc, nguyên nhân thường là chung (mạng, API server), đừng
  hành động. Phải đếm theo **máy**, không theo cell (bài học 15.5).
- **Idempotent**: làm lại nhiều lần cho cùng kết quả như làm một lần. Dispatch, nhả Lease, xoá pod đều
  phải idempotent, vì mọi thứ trong hệ phân tán đều có thể bị gửi lại.
- **Exactly-once**: "đúng một lần" thực chất là *at-least-once* (gửi lại tới khi chắc chắn) cộng *khử
  trùng lặp* (idempotency key).
- **Đồng hồ**: mỗi máy một đồng hồ, có thể lệch nhau hàng phút (VM bị pause). **Không bao giờ trừ hai
  mốc thời gian lấy từ hai máy khác nhau** (bài học 15.17).
- **Fail closed**: khi không chắc thì từ chối, không đoán.

### 4.4 Phần cứng và điều khiển nguồn

- **BMC** (Baseboard Management Controller): máy tính nhỏ trên bo mạch server, chạy cả khi server tắt,
  cho phép bật/tắt nguồn từ xa qua mạng quản trị. Tên theo hãng: **iDRAC** (Dell), **iLO** (HPE),
  **XClarity** (Lenovo), **OpenBMC**.
- **IPMI**: giao thức cũ. **Redfish** (chuẩn DMTF): REST/JSON qua HTTPS.
  - `GET /redfish/v1/Systems/<id>` → `PowerState` (`On`/`Off`).
  - `POST /redfish/v1/Systems/<id>/Actions/ComputerSystem.Reset` với `{"ResetType":"ForceOff"}` để tắt cứng.
- **Hành vi thật của BMC** (ADR-069): trả lời chậm (0,5–3 s), có lúc bận (503), `PowerState` vẫn báo On
  vài giây sau ForceOff, iDRAC trả 409 "already powered OFF" nếu máy vừa tự mất điện, 409 khi đang
  "settle" sau một lần đổi nguồn.
- **sushy-tools**: trình giả lập Redfish đặt trước libvirt. Lab chạy nó ở `192.168.122.1:8000` (TLS +
  basic auth); `lab/redfish-quirks.py` ở cổng 8001 mô phỏng BMC "khó tính".

### 4.5 Quan sát (observability)

- **Metric**: con số theo thời gian (counter, gauge, histogram). **Prometheus** kéo (scrape) metric từ
  endpoint `/metrics` của mỗi dịch vụ. **PromQL** là ngôn ngữ truy vấn.
- **Histogram, p99**: phân bố thời gian; p99 là mức mà 99% số lần nhanh hơn.
- **PrometheusRule**: luật cảnh báo (`expr` + `for`: điều kiện phải đúng liên tục bao lâu).
  **PodMonitor**: báo Prometheus nên scrape pod nào. **promtool**: kiểm luật và chạy unit test cho luật.
- **Grafana**: dashboard. **Loki**: lưu log, truy vấn bằng LogQL. **Fluent Bit**: tác nhân đọc file log
  và đẩy đi.
- **Runbook**: tài liệu "cảnh báo này nghĩa là gì, kiểm gì, làm gì" (`docs/RUNBOOK.md`).


---

# Bước 2 — Hiểu netCI

> **Tuần 2.** Yêu cầu, tiêu chí, lựa chọn công nghệ, kiến trúc, các luồng từng giây (phần 8 và 9 là trọng tâm), từng thành phần, phần IDP (bài toán lớn mà HA là một phần), và active-active: UI/API không bao giờ tắt.

## 5. Từ nỗi đau thành yêu cầu

Mỗi nỗi đau ở 1.1 được chuyển thành một yêu cầu **kiểm chứng được**:

| # | Nỗi đau | Yêu cầu |
|---|---|---|
| R1 | Controller là điểm chết duy nhất | Máy của controller chết thì controller được **tự động** chạy lại trên máy khác, không cần người |
| R2 | Build đang chạy bị mất | Pipeline đang chạy **chạy tiếp** trên controller mới; mỗi bước chạy **đúng một lần** |
| R3 | Hai controller có thể cùng ghi một `JENKINS_HOME` | **Không bao giờ** có hai controller cùng ghi, kể cả khi mạng chập chờn hoặc máy chỉ treo |
| R4 | Hàng đợi mất | Mọi trigger đã được nhận thì **không mất**: nhận nghĩa là đã ghi bền |
| R5 | Webhook mất hoặc build trùng | Webhook được xác thực và khử trùng lặp theo delivery id |
| R6 | Bước nguy hiểm chạy hai lần | Có cách đánh dấu một khối "chỉ được chạy một lần"; nghi ngờ thì **từ chối** thay vì chạy lại |
| R7 | Lỗi một chỗ lan ra cả hệ thống | Sự cố của một controller không làm sập controller khoẻ khác |
| R8 | Không ai biết hệ thống đang hỏng | Mọi lỗi cần người xử lý đều thành cảnh báo, kèm runbook |
| R9 | Build bẩn vì agent sống lâu | Mỗi build có một sandbox mới, cô lập, bị huỷ sau build |
| R10 | Code không tin cậy chạy chung kernel | Mức cô lập theo độ tin cậy: code ngoài chạy trong VM riêng |
| R11 | Không xem được log lúc controller đang chuyển | Log build đọc được ngay cả khi controller đang được tiếp quản |
| R12 | Phải đổi Jenkinsfile, plugin, thói quen | Không đổi Jenkinsfile, không fork Jenkins; vẫn dùng UI Jenkins |
| R13 | Nâng cấp, DB chết, BMC mất mạng | Nâng cấp không mất gì; phụ thuộc hỏng thì **từ chối rõ ràng**, không giả vờ |

Các yêu cầu phi chức năng (cách làm, không phải tính năng):
- **Fail closed**: thiếu một thứ bắt buộc thì dừng, không lặng lẽ chạy kiểu "giả lập".
- **Không bao giờ nói "đã chạy thật" khi chưa có bằng chứng** chạy trên hạ tầng thật.
- Mọi chuyển trạng thái quan trọng phải nguyên tử, idempotent, có audit, an toàn khi chạy đồng thời.
- Ảnh container định danh bằng **digest**, không bằng tag có thể đổi.
- Không bao giờ log token, secret, private key.

## 6. Tiêu chí: thế nào là "làm được"

| Tiêu chí | Ngưỡng | Vì sao chọn ngưỡng này |
|---|---|---|
| Thời gian tiếp quản (mất điện → build chạy tiếp) | Phải **dưới 5 phút**; mục tiêu khoảng 1 phút | Build đang chạy chờ agent khoảng 5 phút rồi bỏ cuộc. Quá 5 phút là mất build |
| Fence (cô lập máy chết) | Vài giây sau khi mất điện | Mọi thứ phía sau chờ bước này |
| Split-brain | **0** lần trong mọi kịch bản, kể cả máy treo và ánh xạ node–máy sai | Một lần là hỏng `JENKINS_HOME` |
| Dữ liệu mất khi mất điện | Tối đa khoảng 1 s ghi | Giới hạn writeback (10.1) |
| Trigger mất | 0 | R4 |
| Khối `netciOnce` chạy hai lần | 0; nghi ngờ thì từ chối | R6 |
| Cell khoẻ bị ảnh hưởng | 0 lần restart, 0 lần mất Lease | R7 |
| Quy mô | Hàng trăm cell, mỗi lần quan sát vẫn dưới 1 s | Nhịp quan sát của supervisor là 1 s |
| Lỗi liên tiếp nhiều giờ | Không bất biến nào bị phá giữa các lần | Lỗi hiếm chỉ lộ ra khi chạy lâu |
| Bằng chứng | Mỗi khẳng định gắn với một file trong `lab/evidence/` | Không có bằng chứng thì không được nói "đã chạy" |

Kết quả hiện tại (chi tiết ở Phần 13):
- **hơn 80 lần mất điện không người can thiệp**, cộng **38/38 lượt soak 3 giờ** gồm mất điện, treo máy,
  giết JVM, restart PostgreSQL…: mọi build chạy tiếp và kết thúc SUCCESS;
- fence 2,3–3,9 s khi mất điện; build chạy tiếp sau 34–67 s (trung vị 43 s);
- chưa có lần split-brain nào; mỗi khối `netciOnce` có đúng một marker.

## 7. Chọn giải pháp và công nghệ

### 7.1 Quyết định gốc: active/active hay "một người ghi, chuyển nhanh"?

| Phương án | Ưu | Nhược | Kết luận |
|---|---|---|---|
| **Active/active trên một `JENKINS_HOME`** (như CloudBees) | UI không bao giờ tắt | Jenkins mã nguồn mở giữ queue, số build, kênh agent **trong RAM** và ghi XML không phối hợp giữa các tiến trình. Hai JVM cùng ghi sẽ hỏng dữ liệu. Muốn làm phải viết lại lõi Jenkins và làm hỏng plugin | **Loại** |
| **Nhiều Jenkins độc lập + chia việc** | Đơn giản | Không giải quyết việc một controller chết | Dùng làm *một phần*: mỗi "cell" giữ một nhóm đội |
| **Backup/restore** (Velero) | Có sẵn, đã làm ở bản 0.3 (ADR-055) | Tính bằng phút đến giờ, mất dữ liệu từ bản backup gần nhất | Chỉ để khôi phục thảm hoạ |
| **Một người ghi tại một thời điểm, volume nhân bản, chuyển nhanh và an toàn** | Jenkins vẫn là Jenkins, plugin không đổi | UI tắt khoảng 1 phút khi chuyển | **Chọn** (ADR-060) |

**Bản chất của lựa chọn**: Jenkins không chia sẻ được trạng thái, vậy đừng bắt nó chia sẻ. Thay vào đó:
1. chỉ cho đúng một controller chạy tại một thời điểm;
2. giữ đĩa của nó an toàn qua việc mất máy;
3. chuyển nó đi thật nhanh;
4. chứng minh không bao giờ có hai cái cùng chạy.

### 7.2 Từng lựa chọn công nghệ

| Vấn đề | Đã xét | Chọn | Lý do |
|---|---|---|---|
| Nền chạy controller và sandbox | VM thuần + script; Nomad; Kubernetes | **Kubernetes** (lab: **k3s** v1.36.4, 3 server, etcd nhúng) | Có sẵn lập lịch, volume (CSI), `Lease`, taint out-of-service, PriorityClass, PDB. k3s nhẹ, dựng nhanh trên 3 VM |
| `JENKINS_HOME` sống qua việc mất máy | NFS dùng chung; Ceph RBD; Longhorn; vSAN | **Block RWO nhân bản đồng bộ** (lab: **Longhorn** v1.13.0, 3 bản sao) | RWO là thêm một lớp chống split-brain; nhân bản đồng bộ không mất gì đã xuống đĩa; Longhorn cài bằng một manifest |
| Ai được làm controller | ZooKeeper/etcd riêng; khoá trong PostgreSQL; **Kubernetes Lease** | **Lease** | Có sẵn, cập nhật có điều kiện theo `resourceVersion`, có `leaseTransitions` làm epoch |
| Phát hiện máy chết | Chỉ dựa vào node NotReady (40–50 s); NHC (Medik8s) | **Lease của chính cell** (nghi sau 3 s) **+ hỏi trạng thái điện** | Nhanh mà không đoán |
| Fencing | Chỉ xoá pod; reboot qua agent; **tắt nguồn qua BMC** | **Tắt nguồn (STONITH), xác nhận Off** qua **Redfish** (server thật) hoặc SSH tới libvirt (VM lab) | Máy "hình như chết" có thể vẫn đang ghi; chỉ máy đã tắt hẳn mới chắc không ghi |
| Ngôn ngữ dịch vụ | Python (bản 0.3); **Go** | **Go** 1.27 | Controller hạ tầng (client-go, Cluster API) đều bằng Go; binary tĩnh, ảnh `FROM scratch` |
| Plugin trong Jenkins | Groovy script; shared library; **plugin Java** | **Plugin Java** (`jenkins/plugin`) | Dispatch idempotent cần giữ khoá hàng đợi của Jenkins, chỉ làm được từ bên trong |
| Hàng đợi bền | Jenkins queue; Kafka; **PostgreSQL** | **PostgreSQL** (netci-queue) | Giao dịch ACID, đã có sẵn, không thêm hạ tầng |
| Sandbox build | Kubernetes plugin; VM mỗi build; **pod ấm + bind muộn** | **netci-fabric** | Pod ấm đã kéo ảnh, khởi động sẵn, và đã nạp sẵn jar của controller (ADR-072) |
| Cô lập code không tin cậy | runc chung kernel; gVisor; Sysbox; **Kata** | **Kata** (`kata-qemu-runtime-rs`) cho pool `untrusted`; **runc + user namespace** cho `standard` | Kata cho mỗi build một VM có kernel riêng |
| Giám sát | Tự viết; **Prometheus + Grafana + Loki** | kube-prometheus-stack 87.10.1, Loki 3, Fluent Bit 5.1.3 | Chuẩn ngành; luật kiểm được bằng promtool |
| Đóng gói, cài đặt | YAML thuần; Kustomize; **Helm** | 2 chart: `netci`, `netci-cell` | Tổ chức cài như cài một sản phẩm |
| Registry ảnh | Docker Hub; **Harbor** | Harbor (lab `172.17.0.1:8930`, TLS bằng CA riêng, tag bất biến) | Registry riêng, có robot account |
| Thử quy mô | Dựng 300 VM; **kwok** | **kwok** 0.8.0 + control plane thật | Node giả nhưng API server, etcd, scheduler thật; supervisor chạy binary thật |
| BMC để thử | Server thật; **sushy-tools** + proxy "khó tính" | sushy-tools (8000), `redfish-quirks.py` (8001) | Không có phần cứng nên mô phỏng đúng hành vi hãng công bố |

## 8. Kiến trúc netCI

### 8.1 Hình tổng thể

```
                          ┌──────────── namespace netci-system ────────────┐
 GitLab/GitHub webhook ──►│ netci-queue (2 bản sao) ──► PostgreSQL           │
 API / người dùng ───────►│   nhận bền, khử trùng lặp, dispatch idempotent    │
                          │ netci-supervisor (2 bản sao, 1 leader)           │
                          │   quan sát Lease mỗi 1 s, hỏi BMC, fence, dọn     │──► BMC (Redfish) / SSH agent
                          │ netci-fabric (2 bản sao, 1 leader)               │
                          │   pool pod ấm, claim, reconcile                  │
                          └──────────────────────────────────────────────────┘
        ┌──── namespace cell-a ────┐     ┌──── namespace cell-b ────┐
        │ StatefulSet jenkins (1)  │     │ ...                      │
        │  ├ install-guard (init)  │     │                          │
        │  ├ cell-agent (sidecar): │     │                          │
        │  │   giữ Lease "jenkins" │     │                          │
        │  ├ jenkins (guard → JVM) │     │                          │
        │  └ log-shipper (Fluent)  │──► Loki (ngoài miền lỗi)       │
        │ PVC JENKINS_HOME (RWO,   │     │                          │
        │   Longhorn, 3 bản sao)   │     │                          │
        └──────────────────────────┘     └──────────────────────────┘
        ┌──── namespace netci-agents ── sandbox (pod ấm) ──────────┐
        │ install (init) → netci-sandbox → agent.jar -webSocket     │
        │ jar cache chép từ image jenkins-agent-jars (chỉ đọc)       │
        │ RuntimeClass: runc+userns (standard) | kata (untrusted)   │
        └───────────────────────────────────────────────────────────┘
        Prometheus + Grafana (namespace monitoring): 16 cảnh báo, 1 dashboard
```

Năm dịch vụ Go nằm trong **một ảnh** `netci/netci` (`FROM scratch`): `cell-agent`, `supervisor`,
`netci-queue`, `netci-fabric`, `netci-sandbox`. Cộng với ảnh controller Jenkins (Jenkins 2.555.3 LTS +
plugin + plugin netCI) và ảnh jar cache.

### 8.2 Khái niệm "cell"

Một **cell** là một Jenkins controller: một JVM, một `JENKINS_HOME` riêng trên volume nhân bản, phục vụ
một nhóm đội (tenant).
- Lỗi, plugin hỏng hay một lần nâng cấp chỉ chạm tới một cell (R7).
- Mọi cell dựng từ cùng nguồn: ảnh controller và plugin từ `toolchain/versions.yaml`, cấu hình từ JCasC,
  job từ Job DSL, credential từ kho secret theo cell.
- Không bao giờ chép `credentials.xml` hay `secrets/master.key` giữa các cell.
- **[Lab]** 2 cell: `cell-a` (StorageClass `longhorn-sync`, NodePort 30080), `cell-b`
  (`longhorn-commit1`, NodePort 30081).

### 8.3 Pod của một cell: bốn container và lý do của từng cái

| Container | Loại | Việc | Vì sao |
|---|---|---|---|
| `install-guard` | init | Chép binary `cell-agent` vào một volume chung | Ảnh Jenkins không có binary của netCI |
| `cell-agent` | native sidecar | Giữ Lease `jenkins`, ghi gate file, probe `JENKINS_HOME`, reload JCasC, dọn plugin, liên kết plugin đã giải nén | Quyền làm controller phải do một thứ nhỏ, đáng tin, chạy trước Jenkins giữ |
| `jenkins` | chính | `cell-agent guard -- jenkins.sh`: guard chỉ khởi động JVM khi gate mở, giết JVM khi gate cũ | Nếu chính cell-agent treo thì vẫn có thứ giết Jenkins |
| `log-shipper` | sidecar | Fluent Bit đẩy log build sang Loki | Log đọc được khi cell đang bị tiếp quản (R11) |

### 8.4 Ba lớp chống split-brain (R3)

1. **Cell agent và guard**: không gia hạn được Lease trong 10 s (`renewDeadline`) thì agent giết Jenkins,
   *trước khi* Lease 15 s hết hạn. Nếu chính agent treo, guard thấy gate file cũ quá hạn và tự giết JVM.
2. **Supervisor**: chỉ nhả Lease thay một holder sau khi **bộ điều khiển nguồn báo máy Off**. Lệnh nhả là
   **có điều kiện** theo đúng `resourceVersion` đã quan sát; nếu Lease vừa được gia hạn thì dừng ngay
   (`FenceAborted`) và cảnh báo `NetciFenceLeaseMoved`.
3. **Storage**: volume RWO chỉ gắn vào một node mỗi lúc.

Ba lớp độc lập: muốn split-brain thì cả ba phải cùng sai.

## 9. Các luồng chính, từng giây một

### 9.1 Một build bình thường, qua hàng đợi bền và fabric

1. GitLab gửi webhook `POST /v1/hooks/gitlab` tới **netci-queue**. Queue kiểm chữ ký (token/HMAC), lấy
   delivery id làm khoá idempotency, **ghi một dòng vào PostgreSQL**, rồi mới trả 202. Webhook gửi lại thì
   tìm thấy dòng cũ, không tạo run thứ hai.
2. **Dispatcher** của queue (vòng lặp) lấy run ở trạng thái `accepted`, gọi plugin trên controller của
   cell: `POST /netci/dispatch {job, runId, params}`.
3. Plugin (`NetciEndpoint`) giữ **khoá hàng đợi** của Jenkins, tìm run trong hàng đợi, trong executor và
   trong build gần đây (`RunLookup`). Không thấy thì xếp lịch build kèm `NetciRunAction(runId)`. Trả lời
   kèm `session` (= `X-Jenkins-Session`).
4. Build cần agent nhãn `netci-standard`. `ProvisionOnEnqueue` đánh thức NodeProvisioner ngay;
   `NetciCloud` tạo một node `NetciAgent` (có inbound secret).
5. `NetciLauncher` gửi `POST /v1/claims {pool, agent, secret, controller}` tới **netci-fabric**. Fabric lấy
   **nguyên tử** một sandbox `warm` trong PostgreSQL (compare-and-set), giữ binding (kèm secret) **chỉ
   trong RAM**.
6. Trong pod sandbox, `netci-sandbox` đang long-poll `GET /v1/binding`, nhận binding và chạy
   `agent.jar -webSocket`. Jar cache đã được chép sẵn lúc pod còn ấm, nên agent không phải tải ~19 MB jar.
7. Agent online, build chạy. Queue thấy build bắt đầu thì ghi `started`, xong thì ghi `finished`.
8. Build xong, `OnceRetentionStrategy` gỡ node, plugin gửi `DELETE /v1/claims/{id}`, fabric xoá pod và tạo
   pod ấm mới.

Thời gian đo trên lab (ADR-072): từ lúc trigger tới lúc xong một build `sh` một dòng, trung vị **3,9 s**
khi chạy lần lượt và **4,8 s** khi 4 build cùng lúc.

### 9.2 Mất điện (máy `netci-lab-2` chạy controller của `cell-b` và một build 240 s)

| t (s) | Sự kiện | Ai làm |
|---|---|---|
| 0 | `virsh destroy netci-lab-2`: máy mất điện | (lỗi) |
| 0–1 | Cell agent không gia hạn được nữa | — |
| ~3 | Supervisor thấy Lease không đổi đủ `SuspectAfter` = 3 s, **hỏi BMC**: `PowerState` = Off | supervisor |
| ~3,3 | **Fence**: (1) xác nhận Off; (2) nhả Lease có điều kiện, ghi annotation `netci.io/fenced`; (3) thêm taint `out-of-service`; (4) ghi node NotReady (storage phản ứng ngay, không chờ 40–50 s); (5) nhả Lease của các cell khác trên cùng máy (sau `QuietCheck` 2 s); (6) xoá mọi pod **không phải cell** trên node | supervisor |
| ~6,5 | `StorageSettle` 3 s sau taint, recovery **force-delete pod cell** (chỉ pod không còn gia hạn) | supervisor |
| ~7 | StatefulSet tạo `jenkins-0` mới, scheduler đặt sang máy khác | Kubernetes |
| 7–20 | Volume gỡ khỏi node chết, gắn sang node mới, mount | CSI, Longhorn |
| ~22 | Cell agent mới giữ Lease (epoch +1); guard mở cổng cho Jenkins | cell-agent |
| 22–40 | Jenkins khởi động, đọc `JENKINS_HOME`, nạp lại Pipeline | Jenkins |
| ~42 | Agent của build nối lại (khoảng 2 s sau khi Jenkins sẵn sàng) | agent |
| ~43 | Bước `sh` đang dở **chạy tiếp**; sau đó build kết thúc SUCCESS | — |

Soak 3 giờ (15 lần mất điện): fence 2,3–3,9 s, build chạy tiếp trung vị 43 s. Phần của netCI là khoảng
3 s; còn lại là storage và Jenkins khởi động.

### 9.3 Máy treo (không tắt, chỉ im lặng)

- Supervisor chờ kubelet im lặng đủ 20 s (`NodeStale`), chỉ khi việc tắt máy **không làm mất đa số etcd**.
- Rồi **ForceOff qua Redfish** và chờ tới khi BMC báo Off (tối đa `offTimeout` 60 s; gửi lại ForceOff mỗi
  2 s), sau đó đi tiếp như 9.2.
- Soak: fence 16,5–20,5 s sau khi treo (chờ Lease 15 s hết hạn), build chạy tiếp như các lần khác.

### 9.4 Supervisor leader chết cùng máy

Supervisor có 2 bản sao, một leader. Nếu leader nằm trên đúng máy mất điện thì bản còn lại phải chờ Lease
của supervisor hết hạn rồi mới làm leader: fence khoảng 25 s thay vì 3 s (soak: 7 lần, đều PASS).

### 9.5 Một khối `netciOnce` bị mất điện cắt ngang

1. Trước khi chạy thân khối, plugin ghi `(build, key, nonce)` vào PostgreSQL qua queue (`POST /v1/once`).
2. Máy chết giữa khối. `JENKINS_HOME` có thể lùi lại tới 1 s.
3. Controller mới chạy tiếp Pipeline từ trạng thái trên đĩa:
   - nếu trạng thái đã ghi rằng khối bắt đầu (cùng nonce), khối tiếp tục bình thường;
   - nếu trạng thái đã lùi về *trước* khi khối bắt đầu, Pipeline định chạy khối lần nữa với **nonce mới**:
     queue thấy khối đã bắt đầu với nonce khác, nên **từ chối** ("cannot tell whether this block ran
     before … not running it"). Người quyết định chạy lại bằng `retry: true`.
4. Queue không trả lời được (DB chết) thì khối **không chạy** (fail closed).

### 9.6 Nâng cấp netCI trong lúc có build

`helm upgrade` lần lượt thay supervisor, queue, fabric và từng cell. Với cell, đó là **restart có kế
hoạch**: cell agent giữ Lease cho tới khi Jenkins thoát hẳn rồi mới nhả, nên supervisor không coi đó là
sự cố. Lab: build `netciOnce` 240 s vẫn SUCCESS, mọi run gửi trong lúc nâng cấp chạy đúng một lần, UI tắt
19–42 s (ADR-071).

### 9.7 Một phụ thuộc hỏng

- **PostgreSQL restart 20 s**: queue trả 503 trong lúc đó (client gửi lại), mọi run đã nhận vẫn chạy
  đúng một lần, khối `netciOnce` chờ rồi chạy.
- **PostgreSQL chết 90 s**: khối `netciOnce` **fail closed**, không có marker.
- **BMC mất mạng đúng lúc một máy chết**: supervisor không làm gì với cell (không biết máy có thật tắt
  không) và cảnh báo; BMC trả lời lại thì fence sau 0,1 s.

## 10. Các thành phần, đi sâu

### 10.1 Dữ liệu sống sót qua mất điện (`JENKINS_HOME`)

Nhân bản không tự làm cho mất điện trở nên an toàn: Longhorn chỉ nhân bản thứ đã xuống thiết bị khối,
trong khi Jenkins để phần lớn dữ liệu ghi nằm trong page cache. Spike đầu tiên cho thấy trạng thái
Pipeline lùi khoảng 20 s và một bước chạy hai lần. Cách giải:
- **Giới hạn writeback** trên node: `vm.dirty_expire_centisecs=100`, `vm.dirty_writeback_centisecs=100`
  (`lab/vms.sh` ghi vào `/etc/sysctl.d/91-netci-writeback.conf`).
- **ext4 commit 1 s**: StorageClass `longhorn-commit1` với `mountOptions: [commit=1]`; hoặc
  `longhorn-sync` với `[sync, dirsync]` (an toàn hơn, chậm hơn).
- WAR và plugin giải nén nằm trên đĩa cục bộ của pod (`--webroot`, `--pluginroot`), vì chúng không phải
  trạng thái. Ảnh controller **giải nén sẵn** plugin lúc build, nên mỗi lần khởi động không phải giải nén
  lại 86 plugin (khởi động lại 9,3 → 7,0 s).
- Phần một giây còn lại do `netciOnce` lo.

### 10.2 Cell agent (`cmd/cell-agent`, `internal/cellagent`)

- Giữ Lease `jenkins`: thời hạn 15 s, `renewDeadline` 10 s, gia hạn mỗi 1 s, mỗi lần thử tối đa 0,3 s
  trên một kết nối mới (ADR-066).
- **Gate file** (`/run/netci/gate`, emptyDir trong RAM): "tôi đang giữ Lease". **Guard** chỉ khởi động
  Jenkins khi gate mở và giết JVM khi gate cũ quá `renewDeadline`.
- **Probe `JENKINS_HOME`** (ADR-067): 5 s một lần ghi một file, fsync, rename, fsync thư mục. Ba lần lỗi
  liên tiếp thì báo lên Lease (`netci.io/volume-failed`), supervisor restart pod đó.
- **JCasC reload nóng**: ConfigMap đổi thì gọi reload; lỗi cấu hình thì báo (`NetciCellConfigurationRefused`).
- **Dọn plugin**: xoá khỏi `JENKINS_HOME` các plugin mà ảnh không mang theo.
- **Liên kết plugin đã giải nén** (`LinkPluginRoot`, `AlignPluginTimes`).

### 10.3 Supervisor (`cmd/supervisor`, `internal/supervisor`)

Ba phần tách bạch:
- **Collector** (`collect.go`): mỗi giây đọc trực tiếp (không cache): StatefulSet có nhãn `netci.io/cell`,
  Node, node lease, VolumeAttachment, Lease của mọi cell (1 list), pod của mọi cell (1 list), trạng thái
  điện khi cần. Khoảng trống giữa hai lần quan sát thành công vượt `MaxGap` (3 s) thì nó **quên** hết và
  quan sát lại từ đầu.
- **Decide** (`decide.go`): hàm thuần, đầu vào là bức tranh, đầu ra là danh sách hành động (`FenceNode`,
  `DeletePod`, `ForceDeletePods`, `Unfence`, `PowerOn`, `Alert`). Toàn bộ chính sách nằm ở đây, nên test
  được hết.
- **Executor** (`execute.go`): thực hiện theo thứ tự an toàn; mỗi máy chỉ fence một lần.

| Tham số | Giá trị | Ý nghĩa |
|---|---|---|
| `SuspectAfter` | 3 s | Lease không đổi chừng này thì hỏi BMC |
| `NodeStale` | 20 s | kubelet im lặng chừng này thì coi là không quản lý được pod |
| `StuckPodAfter` | 30 s | Lease không đổi mà kubelet sống: restart pod |
| `Cooldown` | 2 phút | Sau một hành động, không hành động lại với cell đó |
| `PanicFraction` | 0,5 | Hơn nửa số **máy** có cell hỏng cùng lúc: không tắt máy nào, chỉ cảnh báo |
| `PowerOnAfter` | 30 s | (lab) bật lại máy đã fence |
| `StorageSettle` | 3 s | Chờ storage "thấm" việc node chết rồi mới xoá pod cell |
| `QuietCheck` | 2 s | Lease của các cell khác trên máy phải im lặng chừng này mới được nhả |
| `offTimeout` / `queryTimeout` | 60 s / 3 s | BMC phải báo Off trong 60 s; mỗi lần hỏi tối đa 3 s |

Chốt an toàn khác:
- **Kiểm ánh xạ node–máy lúc khởi động** (`validate.go`); sai thì không hành động.
- **Đổi cấu hình fence** (xoay mật khẩu BMC): supervisor tự khởi động lại và kiểm ánh xạ lại.
- **Không bao giờ xoá pod của cell còn gia hạn Lease** (ADR-070).
- Cell pod chịu taint out-of-service 30 s (`outOfServiceTolerationSeconds`), để taint-eviction và PodGC
  của Kubernetes không "chạy đua" với supervisor.
- Kiểm **headroom**: báo trước nếu một cell mất máy thì không còn chỗ (`NetciCellWithoutHeadroom`).
- Kiểm **DNS cụm**: báo nếu CoreDNS chỉ còn trên một máy (`NetciClusterDNSOnOneMachine`).

### 10.4 Fence (`internal/fence`)

- Giao diện `Fencer`: `State(machine)`, `PowerOff`, `PowerOn`.
- **Redfish** (`redfish.go`): HTTPS với TLS ghim SHA-256 (hoặc CA), basic auth, đọc thông điệp lỗi kiểu
  iDRAC.
- **`EnsureOff`**: gửi ForceOff, hỏi trạng thái tới hạn chót, gửi lại ForceOff mỗi 2 s. Chỉ dừng sớm khi
  bị **từ chối** (400/401/403/404/405); 409 hay 503 không phải từ chối. Chỉ BMC báo Off mới tính là Off.
- **SSH agent** (lab, libvirt): một lệnh bắt buộc (forced command) chỉ đọc, tắt, bật được VM tên
  `netci-lab-<n>`.

```json
{"nodes": {"netci-lab-1": {"redfish": {
   "endpoint": "https://192.168.122.1:8000",
   "system": "/redfish/v1/Systems/<uuid>",
   "credentials": "/etc/netci/fence/bmc",
   "requestTimeout": "5s"}}}}
```

`credentials` là thư mục chứa `username`, `password`, `tls-sha256` (mount từ Secret).

### 10.5 Kết nối tới API server sống sót khi nó chết (`internal/kubeclient`, ADR-066)

Một kết nối TCP tới máy đã mất điện **không đóng, cũng không trả lời**. client-go mặc định dùng HTTP/2 với
một kết nối duy nhất và chỉ phát hiện sau 30 + 15 s. netCI dùng HTTP/1.1, giới hạn thời gian mỗi lần thử,
đóng kết nối khi lỗi, và dial thẳng endpoint của API server còn sống.

### 10.6 netci-queue và plugin Java (ADR-063)

- **Nhận nghĩa là bền**: `POST /v1/runs` (API) hoặc `POST /v1/hooks/{name}` (webhook) được ghi thành một
  dòng PostgreSQL *trước khi* trả lời. Client xác định từ credential, không lấy từ request. Trường nào do
  server quyết định mà request gửi lên thì bị từ chối (422), không lặng lẽ bỏ qua.
- **Dispatch idempotent** dưới khoá hàng đợi (9.1).
- **Gửi lại đến khi chạy**: chưa thấy phiên controller hiện tại xác nhận run thì gửi lại. Controller mới
  sau mất điện không có run trong hàng đợi, nên run được xếp lại (`netci_runs_redispatched_total`).
- **Admission**: giới hạn số run đã dispatch mà chưa chạy trên mỗi cell.
- `LoadItemsWrittenAtStartup`: sửa một lỗi của Jenkins làm job mới khai báo trong JCasC biến mất sau lần
  khởi động khai báo nó.
- Lab: giết JVM khi có 6 mục trong hàng đợi, 5 run qua netci-queue đều chạy đúng một lần; trigger đi thẳng
  vào Jenkins thì mất (đúng như dự đoán).

### 10.7 `netciOnce` (ADR-065)

```groovy
netciOnce('deploy-prod') { sh './deploy.sh' }
netciOnce(key: 'deploy-prod', retry: true) { sh './deploy.sh' }   // người đã quyết định chạy lại
```

Bản ghi nằm *ngoài* `JENKINS_HOME`, nên không bị cuốn theo khi đĩa lùi lại. Lab: 51 lần mất điện giữa
khối `netciOnce`, mỗi lần đúng một marker, không lần nào bị từ chối nhầm.

### 10.8 Agent fabric (ADR-061, ADR-064, ADR-071)

- **Pool**: ảnh, CPU/RAM/đĩa, RuntimeClass, nhãn, số pod ấm (`warm`), tối đa (`max`), jar cache.
- **Trạng thái** trong PostgreSQL: `creating → warm → claimed → bound → released → deleted` (+ `failed`).
  Mọi chuyển trạng thái là compare-and-set, có audit. Reconciler đối chiếu mỗi giây.
- Claim khi hết pod ấm thì tạo pod "lạnh" ngay (cold claim); đã tới `max` thì trả 429, Jenkins hỏi lại.
- Pod chứng minh danh tính bằng **projected ServiceAccount token** (audience `netci-fabric`, 10 phút) qua
  TokenReview; pod không có quyền gì với API Kubernetes.
- Pod bận mang nhãn `netci.io/busy`, có PDB, nên preemption tránh build đang chạy.
- NetworkPolicy: sandbox chỉ ra được DNS, fabric, controller và địa chỉ ngoài cụm.
- **Sandbox hỏng không làm build phải chờ** (ADR-071):
  - sandbox ấm mà pod không còn Ready thì bị đánh dấu `failed` và thay mới;
  - claim vào sandbox ấm mà 15 s chưa nhận binding thì sandbox bị coi là hỏng;
  - `GET /v1/claims/{id}` cho cell biết claim của mình ra sao; plugin hỏi mỗi 2 s và dừng ngay khi fabric
    bỏ claim, hoặc khi claim đã `bound` 20 s mà agent chưa tới;
  - fabric đo thời gian bằng **đồng hồ của PostgreSQL**, không bằng đồng hồ của chính nó.

### 10.9 Jar cache của agent (ADR-072)

- **Vấn đề**: agent mới tinh tải khoảng **19 MB** jar của controller qua kênh remoting (15 jar; riêng
  jenkins-core 11,9 MB), lúc controller dựng kênh và lúc bước `sh` đầu tiên. Khi nhiều agent cùng tải,
  cửa sổ truyền của kênh đầy và agent phải chờ; có build mất 15 s. Bằng chứng: thread dump của controller
  chụp đúng lúc một agent bị kẹt (`JarLoaderImpl.writeJarTo` chặn ở `PipeWindow.get`).
- **Giải pháp**:
  - `jenkins/Dockerfile.agent-jars` sinh, từ một ảnh controller, một ảnh nhỏ (khoảng 17 MB nén) chứa jar
    cache. Tên file do chính `Checksum` của remoting đặt (`jenkins/agent-jars/hudson/remoting/NetciAgentJarCache.java`),
    danh sách jar ở `jenkins/agent-jars/agent-jars.txt`.
  - Pool có `jarCache: <ảnh>@sha256:…`. Fabric mount ảnh đó **chỉ đọc** (image volume của Kubernetes);
    `netci-sandbox` chép vào cache riêng của agent **trước khi pod báo ấm** (9 ms).
- **Vì sao phải theo digest**: remoting dùng jar trong cache theo *tên* mà không kiểm nội dung; một tag bị
  dời sang ảnh khác là agent nạp jar sai.
- **Kết quả**: A/B 16 build mỗi bên: 3,38 s so với 4,66 s trung vị, 0 so với 2 lần kẹt 15 s.

### 10.10 Ưu tiên và chỗ trống

- PriorityClass: platform 1 000 000 > cell 900 000 > sandbox -10 (`preemptionPolicy: Never`).
- Build đang chạy có PDB, nên scheduler preempt pod rảnh trước. Thử trên scheduler thật (kwok): không có
  PDB thì 5/5 lần build bị giết, có PDB thì 0/5.
- PDB cho pod không có controller **phải** dùng `minAvailable` dạng số nguyên.
- Agent của Kubernetes plugin không được nằm cùng máy với controller (anti-affinity), vì agent chết cùng
  controller thì không chạy tiếp được.

### 10.11 Quan sát và cảnh báo

16 cảnh báo (`deploy/helm/netci/templates/monitoring.yaml`), mỗi cái có mục trong `docs/RUNBOOK.md`:

| Cảnh báo | Nghĩa ngắn gọn |
|---|---|
| `NetciCellDown` | Một cell không có controller sống |
| `NetciCellWithoutHeadroom` | Nếu máy của cell này chết, không còn chỗ để chuyển |
| `NetciCellsShareAMachine` | Hai cell nằm chung một máy, sẽ chết cùng nhau |
| `NetciCellConfigurationRefused` | JCasC mới bị Jenkins từ chối |
| `NetciClusterDNSOnOneMachine` | DNS cụm chỉ còn trên một máy |
| `NetciComponentDown` | Không còn bản nào của supervisor/queue/fabric |
| `NetciControllerUnreachable` | Queue không gọi được controller |
| `NetciFabricNoWarmSandbox` | Pool không còn pod ấm |
| `NetciFabricSandboxesFailing` | Sandbox hỏng liên tục |
| `NetciFenceLeaseMoved` | Máy báo Off mà Lease lại được gia hạn: ánh xạ node–máy sai |
| `NetciRunsLostWithAController` | Run phải gửi lại vì controller mất hàng đợi |
| `NetciSupervisorBlind` | Supervisor không quan sát được |
| `NetciSupervisorNeedsAPerson` | Supervisor dừng tay, cần người (panic guard, BMC không trả lời…) |
| `NetciSupervisorNotLeading` | Không có supervisor leader |
| `NetciSupervisorSlowObservations` | Mỗi lần quan sát chậm |
| `NetciTakeoverSlow` | Một lần tiếp quản quá lâu |

Log build sang Loki qua Fluent Bit (ADR-068): xem trong Grafana bằng
`{cell="cell-b", job="<job>"} | build="<n>"`, kể cả lúc cell đang bị tiếp quản.

## 11. Phần IDP của netCI (bài toán lớn)

netCI là một **Internal Developer Platform**. Mã nằm ở `backend/`, `frontend/`, `api/`, `templates/`,
`sample-apps/`; cách chạy ở `README.md` và `QUICKSTART.md`:
- **Backend** Python (FastAPI, psycopg 3), PostgreSQL là nguồn sự thật lúc xử lý request.
- **Portal** React 19 + Vite, SSO qua **Keycloak** (OIDC).
- **CD** bằng **Temporal** (workflow bền): deploy qua SSH, canary, blue/green, rollback.
- **Chuỗi cung ứng**: build trong Jenkins, đẩy lên Harbor, ký bằng **cosign**, SLSA provenance, SBOM bằng
  **syft**, quét bằng **trivy**.
- **Quản trị**: policy, change freeze, phê duyệt hai người, tính chi phí CI.

Đã chạy thật trên lab (onboarding, build 9 stage, đẩy registry, `cosign verify`, deploy qua SSH, SSO).

**Quan hệ với Jenkins HA.** Mọi build của IDP chạy trên Jenkins, nên Jenkins là điểm chết của cả IDP.
Có một giai đoạn, repo được dựng lại chỉ quanh bài toán HA và phần IDP bị gỡ ra (ADR-060, ADR-062). Đó là
một bước đi sai, đã được sửa (ADR-073): IDP là bài toán lớn, HA là một phần của nó, và cả hai nằm chung
một repo. Việc còn lại là nối hai phần: build của IDP đi qua netci-queue (nhận bền, gửi không trùng,
`netciOnce`) tới các cell, thay vì gọi thẳng một Jenkins.

Cổng kiểm của IDP (ADR-073): migration và `schema.sql`, `pyflakes backend/app/`, `docker compose config`,
toàn bộ pytest, test và build của portal. Test chạy trên một PostgreSQL riêng; không bao giờ trên DB của
stack đang chạy.

## 11b. Active-active: UI/API Jenkins không bao giờ tắt

Cell (mục 8–10) giữ được build khi máy của controller chết, nhưng **UI của controller đó tắt** cho
tới khi takeover xong (40–80 s). Mentor yêu cầu UI/API không bao giờ tắt, kiểu CloudBees nhưng không
chịu các giới hạn của CloudBees. Hạ tầng của mentor: Pipeline only, agent JNLP (inbound) trên một VM
lớn, storage chủ yếu `local-path`, có Longhorn, được phép dùng LB riêng, plugin riêng, PostgreSQL.

**Vì sao không dùng một JENKINS_HOME dùng chung (cách của CloudBees).** Lab đã thử (ADR-074):
- không có gì điều phối thì hai Jenkins trên một home cho ra build "tàng hình" và cập nhật bị ghi đè
  lặng lẽ;
- trên Longhorn RWX, máy chạy NFS server chết thì **mọi** replica bị treo ghi 61 s cùng lúc.

Vì vậy chọn **mỗi replica một home riêng** trên đĩa của chính máy nó, giữ cho giống nhau qua PostgreSQL
(ADR-076). Máy nào chết thì chỉ mất replica đó, đĩa của replica kia không bị ảnh hưởng.

**Các mảnh ghép, mỗi mảnh giải một nỗi đau:**

| Mảnh | Làm gì | Vì sao cần |
|---|---|---|
| HAProxy trên mọi máy + VIP (kube-vip) | Một địa chỉ cho người dùng; health check mỗi giây; request hỏng được gửi lại sang replica kia | Bản thân LB cũng không được là điểm chết |
| netci-homesync (Go, trên PostgreSQL) | Mỗi job/user một dòng có version; ghi bằng compare-and-set; một luồng thay đổi mà mọi replica theo | Database xếp thứ tự các lần ghi; copy file thì không |
| Plugin: khoá trước khi sửa | Một lần sửa trên UI giữ khoá của item, bắt kịp mọi thay đổi trước, rồi mới lưu | Không bao giờ sửa từ nội dung cũ (lỗi E4 ở phase 0) |
| User đồng bộ + cookie phiên ký chung (ADR-077) | Mật khẩu, API token, seed giống nhau; cookie HMAC cho phép replica kia dựng lại phiên với quyền từ realm | Replica chết thì người dùng vẫn đăng nhập; token tạo ở r0 dùng được ở r1 |
| Một chủ cho mỗi job + đặt trước số build (ADR-078) | Lease có epoch; số build ghi vào PostgreSQL trước khi build nhận số | Số build 1, 2, 3 không trùng; cron chạy trên mọi replica nhưng chỉ build một lần |
| Nhận nuôi build đang chạy (ADR-079) | Build được sao chép mỗi 3 s; replica có agent của build nhận nuôi khi fence xác nhận chủ cũ đã chết; nạp lại run là nó tự resume | Build không mất khi replica chết |

**Đo trên lab** (KVM + k3s, 3 máy; các file nằm trong `lab/evidence/`):

| Câu hỏi | Kết quả |
|---|---|
| UI/API khi một máy mất điện | Máy replica: 0–2 s. Máy giữ VIP: 6 s. Cả hai trên một máy: 2–4 s |
| Người đã đăng nhập | Vẫn đăng nhập ở replica kia (máy mất điện, pod bị giết) |
| Job tạo ở r0 có ở r1 sau | Trung vị 231 ms |
| 40 lần sửa đồng thời trên 2 replica | 25 nhận, 15 bị từ chối có lý do, không mất lần nào, hai bên hội tụ |
| Build đang chạy, giết pod chủ | Nhận nuôi sau 4,3 s, SUCCESS, đủ 90/90 dòng, không dòng nào hai lần |
| Build đang chạy, mất điện máy chủ | Nhận nuôi sau 49,5 s, SUCCESS, đủ 90/90 dòng, build vẫn xong đúng giờ |
| Mất điện máy chạy primary của kho (PostgreSQL HA, ADR-080) | 268 lần ghi đã trả 200, không mất lần nào; số build giữ nguyên; sửa bị từ chối (503) 28 s; mất máy chứa standby thì không lần sửa nào bị từ chối |
| Soak: 11 sự cố nối tiếp (pod, máy, kho, homesync, HAProxy), mỗi lần giữa một build 90 s | 11/11 build SUCCESS, mỗi dòng đúng 1 lần, xong 83–85 s sau sự cố; 3235 lần ghi có trên cả hai replica; UI 1742/1750 |
| Khởi động lại cuốn chiếu, có drain | UI 306/306; 303 lần tạo job có trên cả hai replica |

**Giới hạn, nói thẳng:**
- Kho đã HA (3 instance CloudNativePG trên `local-path`, đồng bộ tới 1 standby, ADR-080), nhưng trong lúc nó
  failover thì việc sửa bị từ chối khoảng 25 s. Đọc, UI và build đang chạy vẫn tiếp tục.
- Chưa đồng bộ view, đổi tên và di chuyển item.
- Một POST gửi từ trang do replica đã chết phục vụ sẽ hỏng crumb CSRF một lần; tải lại trang là được.
- Fence trong lab là góc nhìn của Kubernetes. Một máy bị cô lập mạng mà vẫn sống thì phải có fence
  bằng nguồn điện (Redfish) như cell đang làm.
- Mỗi agent thuộc một replica tại một thời điểm, nên năng lực build được chia theo agent của từng replica.

**Những thứ chỉ test mới thấy, và đã được sửa** (ADR-076–079 có đủ):
- HAProxy xoá cookie mà plugin đặt (cần `preserve`).
- POST bị chuyển hướng sang replica khác thì hỏng crumb CSRF (nên phải chuyển tiếp phía server).
- Pipeline là flyweight task, nên `canTake` không được gọi (phải dùng `canRun`).
- `build.xml` của Pipeline đang chạy vẫn ghi `SUCCESS` (dấu hiệu đúng là `completed=false`).
- Tên agent trong log là một ConsoleNote.
- HAProxy giữ tunnel của agent tới máy chết suốt một giờ.
- Kết nối có sẵn trong pool cứ rơi vào API server của máy chết.
- Feed của kho đánh số trước khi commit, nên một replica có thể bỏ qua một thay đổi commit muộn. Đã sửa bằng
  một khoá để thứ tự feed trùng thứ tự commit.
- Build được nhận nuôi bị thiếu 2 dòng log (log được chép trước `program.dat` và còn nằm trong bộ đệm). Đã
  sửa: chép log sau cùng, flush trước khi chép.
- Agent tưởng vẫn kết nối với máy đã chết suốt 9 phút (ping mặc định 5 phút). Đã sửa: ping 5 s, và agent
  dùng HAProxy trên chính máy của nó.
- Deadlock: một job tạo trên r1 giữ khoá của Jenkins rồi chờ khoá của homesync, trong khi follower giữ khoá
  homesync và chờ khoá của Jenkins. Sửa bằng khoá theo từng item (ADR-080); lần chạy failover của kho phát
  hiện ra, trước cả lúc cắt điện.


---

# Bước 3 — Kiểm chứng, bài học, giới hạn

> **Tuần 3.** Hệ thống đã được chứng minh tới đâu, những lỗi đã tìm ra, và những gì chưa làm.

## 13. Kiểm chứng: test, chaos, soak, quy mô

### 13.1 Các tầng kiểm chứng

| Tầng | Lệnh | Kiểm gì |
|---|---|---|
| Unit Go | `make check` (`go test -race ./...`) | Mọi quy tắc của Decide; fence với BMC "khó tính"; số request không tăng theo số cell |
| Tích hợp PostgreSQL | `NETCI_QUEUE_TEST_DATABASE_URL=... go test ./internal/runqueue ./internal/fabric` | Queue và fabric với CSDL thật (kể cả lệch đồng hồ) |
| Plugin | `make plugin` (JenkinsRule, SpotBugs) | Dispatch, `netciOnce`, launcher |
| Luật cảnh báo | `deploy/helm/netci/ci/test-rules.sh` | promtool |
| Chart | `ci/charts.sh` | `helm lint`, `helm template` |
| Chaos trên lab | `lab/spike/chaos_poweroff.py` | Mất điện hay máy treo thật, Jenkins thật, build 240 s |
| Soak | `lab/spike/soak.py --hours 3 --seed N` | Lỗi ngẫu nhiên liên tiếp nhiều giờ, kiểm bất biến sau mỗi lượt |
| Probe chuyên đề | `agent_probe.py`, `queue_crash_probe.py`, `webhook_probe.py`, `headroom_probe.py`, `logs_during_takeover.py`, `many_volumes_probe.py`, `upgrade_probe.py`, `dependency_probe.py`, `dns_alert_probe.py`, `fabric_timeline.py`, `fabric_broken_sandbox_probe.py`, `agent_delay_probe.py` | Từng cơ chế |
| Quy mô | `KWOK_DIR=... lab/scale/run.sh 100 300 5` | Supervisor thật với 100–600 cell |
| Preemption | `lab/scale/preemption.py --rounds 5` | Scheduler thật |

**Quy tắc viết test**: test viết *cùng* code và phải **thất bại trên code cũ**. Một test viết từ cùng một
hiểu lầm với code thì sẽ pass và chẳng chứng minh gì. Mọi bản sửa trong đề tài đều được kiểm như vậy (bỏ
phần code mới đi, test phải đỏ).

### 13.2 Kết quả chính (có bằng chứng)

| Kịch bản | Kết quả | Bằng chứng |
|---|---|---|
| Mất điện, không người can thiệp | Hơn 80 lần, mọi build SUCCESS, mất tối đa 1/240 dòng log | ADR-060, `chaos-poweroff-*.json` |
| **Soak 3 giờ, 38 lỗi ngẫu nhiên liên tiếp** | **38/38 PASS** (mất điện 15, + leader supervisor 7, treo 6, giết JVM 4, restart PostgreSQL 4, xoá pod 2): mọi build SUCCESS, không mất tick, đúng 1 marker; giữa các lượt mọi Lease đúng pod, không còn taint, mọi node Ready, mọi volume healthy | `soak-20261011T125323Z.json` |
| Máy treo, ForceOff qua Redfish | 6/6, máy tắt sau 19–22 s | `chaos-poweroff-20261002T070357Z.json` |
| BMC "khó tính" (chậm 1,5 s, 503, trễ 8 s) | 3/3; Off sau 22 s, nên timeout cũ 20 s quá ngắn | `chaos-poweroff-20261009T020225Z.json` |
| Đổi mật khẩu BMC | Hai replica tự khởi động lại, kiểm lại ánh xạ trong 66 s | `chaos-poweroff-20261009T042237Z.json` |
| Quy mô 600 cell / 200 máy | Mỗi lần quan sát 0,1 s, fence 2,7–2,9 s, 0,16 core, 53 MiB | `scale-*.json` |
| 8 volume cùng chuyển | Nhanh như 1 volume; mọi lần ghi có fsync còn nguyên | `many-volumes-*.json` |
| Preemption | 5/5 → 0/5 build bị giết khi có PDB | `preemption-budget-*.json` |
| Nâng cấp và rollback toàn bộ khi build chạy | Build SUCCESS, mọi run đúng một lần, UI tắt 19–42 s | `upgrade-*.json` |
| PostgreSQL chết, BMC mất mạng | Từ chối rõ ràng, `netciOnce` fail closed, không fence nhầm | `dependency-*.json` |
| Sandbox hỏng | Build vượt 2 sandbox hỏng trong 52,8 s (trước đây có build chờ 353 s) | `fabric-broken-sandbox-*.json` |
| Fabric vs Kubernetes plugin, có jar cache | Lần lượt: 3,88 s vs 3,17 s; 4 build cùng lúc: 4,82 s vs 6,17 s; fabric không build nào quá 6 s | `fabric-timeline-20261012T034419Z.json` |
| Jar cache A/B | 3,38 s vs 4,66 s trung vị; 0 vs 2 lần kẹt 15 s | `agent-jar-cache-ab-20261012.json` |

### 13.3 Đọc một file bằng chứng chaos

`timings_s`: `fenced` (taint được thêm), `leaseTaken` (pod mới giữ Lease), `jenkinsUp`, `resumed` (build chạy
tiếp), tính từ lúc gây lỗi. `leadersBefore`: leader của controller-manager, scheduler, csi-attacher ở máy
nào (leader chết cùng máy thì chậm hơn, không phải lỗi của netCI). `once`: số marker `netciOnce` (phải là 1).
`neighbour*`: cell láng giềng có bị ảnh hưởng không (nếu nó ở chung máy thì nó cũng phải chuyển, đó là
đúng).

## 15. Những bài học đắt giá

Mỗi bài học theo cùng một khuôn: hệ thống đúng trong điều kiện lab nhưng sai khi điều kiện thật khác đi.
Cách tìm cũng giống nhau: dựng lại đúng điều kiện thật, đo, và **nghi ngờ cả công cụ đo**.

1. **Nhân bản ≠ an toàn khi mất điện.** Page cache làm mất khoảng 20 s trạng thái và một bước chạy hai lần.
   Sửa bằng writeback 1 s, commit 1 s và `netciOnce`.
2. **Kết nối tới API server đã chết không đóng, cũng không trả lời** (ADR-066). Sửa bằng HTTP/1.1, giới hạn
   thời gian mỗi lần thử, đóng kết nối khi lỗi, dial thẳng endpoint.
3. **Patch Deployment của Longhorn bị driver-deployer ghi đè**, nên phải dùng admission policy; một JSON
   patch có list typed object còn làm API server của k3s 1.36.4 panic.
4. **Longhorn xoá nhầm controller mới** (auto-delete khi volume bị gỡ bất ngờ). Sửa: loại StatefulSet khỏi
   auto-delete; cell agent tự probe `JENKINS_HOME`.
5. **Đếm theo cell, không theo máy**: hai cell trên một máy treo bị đọc thành "2/2 cell hỏng", panic guard
   chặn, cả hai nằm chết. Sửa: đếm theo máy.
6. **Renew deadline 4 s ngắn hơn một lần bầu leader etcd**, nên cell khoẻ bị restart. Sửa thành 15 s / 10 s.
7. **BMC thật** (ADR-069): một lỗi của ForceOff không có nghĩa máy chưa tắt. Gửi lại tới hạn chót; chỉ tin
   `PowerState`; timeout 20 s → 60 s.
8. **O(N) trong vòng lặp có hạn chót**: 2N GET mỗi vòng làm 4 s/vòng ở 100 cell. Sửa: 2 LIST.
9. **Công cụ đo cũng có lỗi**: API server giả trong test bỏ qua namespace và field selector; kwok tự gia hạn
   node lease; bộ mô phỏng gia hạn Lease sau khi đã "mất điện".
10. **Scheduler không phân biệt pod rảnh với pod đang chạy build**: phải biểu diễn bằng thứ scheduler đọc
    được (PDB, `minAvailable` nguyên).
11. **Cấu hình đổi trong lúc chạy** (xoay mật khẩu BMC): supervisor vẫn dùng mật khẩu cũ và sẽ im lặng mất
    khả năng fence. Sửa: theo dõi Secret, khởi động lại, kiểm ánh xạ lại.
12. **Xoá pod quá sớm làm storage chậm** (ADR-070); trên đường sửa tìm ra **một lỗi an toàn**: recovery xoá
    pod của cell vẫn đang gia hạn Lease.
13. **CoreDNS chỉ một bản** là một điểm chết ngoài netCI: volume chuyển mất 60 s thay vì khoảng 15 s. Sửa:
    2 bản trên 2 máy và cảnh báo `NetciClusterDNSOnOneMachine`.
14. **Endpoint chờ Ready làm agent chậm 6 s**. Sửa: `publishNotReadyAddresses` + startupProbe 1 s; còn ~2 s.
15. **Mỗi lần khởi động Jenkins giải nén lại 86 plugin**. Sửa: giải nén sẵn trong ảnh; 9,3 → 7,0 s.
16. **Sandbox ấm "chết" vẫn được giao**, có build chờ 353 s. Sửa ở fabric và plugin.
17. **Hai đồng hồ trong một phép trừ**: một VM chậm 197 s sau khi bị pause làm fabric huỷ mọi claim. Sửa:
    chỉ dùng giờ của DB (cả metric của queue). **Đừng bao giờ trừ hai mốc thời gian lấy từ hai máy khác nhau.**
18. **Lần soak đầu tiên hỏng vì công cụ đo**, không phải vì netCI: bật timestamper làm mỗi dòng console có
    tiền tố giờ, probe không đọc được "tick". Sửa probe, chạy lại, và ghi rõ lần hỏng là của công cụ.
19. **Đo burst sai**: 4 trigger đồng thời cùng đọc `nextBuildNumber` nên cả 4 luồng đo cùng một build. Sửa:
    lấy số build từ queue item (header `Location`). Bằng chứng đo sai bị loại.
20. **Số đo bị méo khi hai controller dồn một máy** (84% RAM): mọi build chậm gấp đôi. Phải đo lại khi cell
    nằm riêng; số đo cũ không được dùng.
21. **15 s "bí ẩn" của Jenkins** (ADR-072): không phải DNS, không phải polling của `sh`, mà là jar của
    controller tải qua kênh remoting. Tìm ra nhờ bám log từng pod từ lúc nó tồn tại, tách từng giai đoạn,
    và **chụp thread dump đúng lúc kẹt**. Chứng minh bằng A/B trước khi sửa.

## 16. Giới hạn và những gì chưa kiểm chứng

- **UI Jenkins tắt khoảng 40–60 s khi tiếp quản**: giới hạn của thiết kế "một người ghi". Phần của netCI là
  khoảng 3 s; còn lại là storage và Jenkins khởi động. Log vẫn đọc được qua Loki, trigger vẫn vào hàng đợi.
- **PostgreSQL của queue chỉ có một bản trên lab**: nếu nó nằm trên đúng máy bị mất, queue **từ chối** run (503
  hoặc không kết nối được) trong lúc PostgreSQL chuyển máy, đo được khoảng 45–70 s; không mất run nào đã nhận,
  nhưng bên gửi phải gửi lại. `[Prod]` PostgreSQL phải HA.
- **Chưa fence một server vật lý thật** qua iDRAC/iLO; hành vi BMC mới được mô phỏng theo tài liệu và báo
  cáo lỗi của hãng.
- **Quy mô với storage thật**: kwok đo phần control plane; storage chuyển nhiều volume mới đo tới 8.
- **Fabric khi chạy lần lượt** vẫn chậm hơn Kubernetes plugin khoảng 0,7 s (trung vị); khi dồn dập thì
  nhanh hơn. Mẫu còn nhỏ, và số của Kubernetes plugin dao động giữa các lượt đo.
- **Jar cache** mới phủ Pipeline có bước `sh`; plugin của các bước khác (git, credentials…) vẫn tải jar như
  cũ. Pool Kata chưa có jar cache.
- **Co giãn số máy theo dự báo** và **tái chế máy**: chưa làm.
- Mọi số liệu đều từ lab 3 VM, nơi mọi máy đồng thời là control plane. Production nên tách riêng.


---

# Bước 4 — Các quyết định kiến trúc (ADR, tiếng Anh)

> **Tuần 3.** Mỗi ADR ghi một quyết định: bối cảnh, quyết định, bằng chứng, phương án bị loại. Thứ tự: 060 (quan trọng nhất), 063 (hàng đợi), 065 (netciOnce), 061 và 064 (fabric), rồi 066–072, mỗi cái một bài học cụ thể; rồi 074 và 076–080: active-active, từ cái gì hỏng khi dùng chung một home tới nhận nuôi build đang chạy và kho HA; cuối cùng 081–089: đưa lên production (hàng đợi, IDP, backup, fence bằng Nova, một chart, crumb, đổi tên và di chuyển job, view, drain không cắt request, IDP build–deploy thật trên lab HA, pipeline trên agent inbound của VM, adopt với pipeline thật của IDP và bản sao tại ranh giới bước).

## ADR-060: Jenkins cells, a global queue outside Jenkins, and fenced takeover

Status: Proposed (2026-10-01); decisions 1 and 5 revised by the spike the same day. Supersedes the product scope of ADR-001..059: netCI no longer

> Amended 2026-10-12 by ADR-073: netCI is the IDP again, and the HA layer is a part of it; the product-scope sentence below no longer holds.
deploys (the organisation deploys with Jenkins) and no longer ships a portal. Those ADRs stay
as history; the mechanisms reused from them are named below.

### Context

The organisation runs open-source Jenkins, on VM agents, and deploys with it. It wants the
guarantees CloudBees CI sells as High Availability (active/active): when a controller dies,
no build history, log, queued build or running build is lost, and people keep working.

What was verified before deciding (sources in docs/research/jenkins-ha-dr.md and the links
below):

- CloudBees HA runs several replicas of one controller on a shared file system and keeps
  their live state in Hazelcast; a surviving replica *adopts* the Pipeline builds of a dead
  one. Even CloudBees supports Pipeline only and reimplements `build` and `lock` for it.
  (docs.cloudbees.com: ha-fundamentals, pipeline-builds-ha-active-active)
- Open-source Jenkins has no equivalent. Controllers sharing one `JENKINS_HOME` corrupt it:
  state lives in memory and is serialised to XML without cross-process coordination.
- Adoption rests on a mechanism open-source Jenkins has: Pipeline durability. A `sh` step is
  a process on the agent that outlives the controller; the build resumes at the step it was
  in, provided the agent reconnects. Reconnection after a controller restart is buggy for
  WebSocket and Kubernetes agents (JENKINS-67062, kubernetes-operator#691) and must be proven
  and, where needed, fixed.
- A resuming build waits about five minutes for an ephemeral agent before giving up
  (workflow-durable-task-step). Takeover must finish well inside that.
- Jenkins' own queue is lost on a crash: `queue.xml` is written on orderly shutdown only
  (JENKINS-30909); the plugin that persisted it was withdrawn.
- Kubernetes >= 1.28 fences a dead node with the `node.kubernetes.io/out-of-service` taint:
  its pods are deleted and its volumes detached at once instead of after ~6 minutes.
- Pipeline logs can be written outside `JENKINS_HOME` and still shown in Jenkins
  (OpenTelemetry plugin, Elasticsearch-compatible backend).

True active/active on one `JENKINS_HOME` means rewriting how Jenkins holds its queue, builds,
build numbers and agent channels in memory, and it breaks plugins that keep state in memory.
It stays research (see "Rejected").

### Decision

**netCI becomes a high-availability layer around open-source Jenkins.** Jenkins is not
replaced or forked; people keep their Jenkinsfiles and the Jenkins UI.

1. **Cells.** A cell is one Jenkins controller: one JVM, its own `JENKINS_HOME` on a block
   volume replicated synchronously across nodes (Longhorn in the lab; Ceph RBD, vSAN or any
   CSI volume that survives a node in a company). Several cells run at once; each holds a set
   of teams or folders (its tenants). A fault, a bad plugin or an upgrade touches one cell.
   Velero backups remain for disaster recovery and corruption, not for failover.

   **Replication alone does not make a power loss safe** (spike, below): it replicates what
   reaches the block device, and Jenkins leaves most of its writes in the page cache. So:
   - page-cache writeback on controller nodes within ~1 s (`vm.dirty_expire_centisecs=100`,
     `vm.dirty_writeback_centisecs=100`) and the ext4 journal committed every second
     (`commit=1`) -- what a power loss can take back is bounded to about a second;
   - the WAR and the exploded plugins on the pod's local disk (`--webroot`, `--pluginroot`):
     they come from the image and are not state;
   - Pipeline durability `MAX_SURVIVABILITY` (the default) everywhere;
   - for the remaining second, a re-execution guard (decision 6) -- a step whose start was
     lost must not run twice where running twice is harmful.

2. **Every cell is built from the same sources.** Controller image and all plugins from
   `toolchain/versions.yaml` (ADR-059), configuration from JCasC, jobs from Job DSL in git,
   credentials from an external secret store scoped per cell -- never by copying
   `credentials.xml` or `secrets/master.key` between cells. Drift blocks the cell (ADR-059).

3. **One intake and one durable queue, outside Jenkins.** Webhooks, cron schedules, API calls
   and manual runs are written to a queue in PostgreSQL (HA) before anything else happens.
   Each cell is handed only what it can start now (the admission budget of ADR-050), so a
   crashed cell loses nothing that was not already running. Every build carries a global run
   id; before a build is handed to another cell the dispatcher checks, under a fenced lease,
   that the first cell never took it -- a build runs once. Cron and SCM polling run in a
   leader-elected scheduler instead of in each controller: no double runs across cells, and
   a missed slot during a takeover is caught up according to the job's policy.

4. **Cell Supervisor: detect, fence, take over.** Each cell holds a lease renewed every few
   seconds. When it lapses the supervisor fences first (out-of-service taint, or power-off
   through the hypervisor API) and only then starts the controller elsewhere on the same
   replicated volume. A fencing token accompanies everything a cell writes to netCI, so a
   controller that comes back from a partition cannot act on stale authority. Budget, each
   phase measured: detect <= 10 s, fence <= 5 s, attach <= 10 s, Jenkins start <= 30 s,
   agents reconnected <= 10 s; p95 <= 60 s.

5. **Running builds continue.** Pipeline durability plus agents that reconnect to the cell's
   stable address; the agent fabric (ADR-061) never removes an agent holding a paused build.
   An agent cannot rely on its connection breaking: a machine without power sends no FIN or
   RST, and Jenkins' channel pinger needs 5 + 4 minutes while a resumed build waits ~5. Every
   agent therefore runs under a supervisor that watches the cell's `X-Jenkins-Session` (new on
   every start) and restarts the agent JVM when it changes; the build's processes are
   durable-task's and live outside that JVM.
   Freestyle builds cannot be resumed by any product; they are re-queued and reported as
   such, never as succeeded.

6. **Deploy stages are guarded on resume.** The organisation deploys from Jenkins. A stage
   declared as a deployment does not continue blindly after a takeover: it re-checks the
   target or waits for a person, and a deployment lease with a fencing token (ADR-016) stops
   a stale controller from deploying.

7. **The read path never goes down with a cell.** Build records are written to PostgreSQL as
   they happen and logs stream to OpenSearch; history, status and logs are served from there
   while a cell is taking over.

8. **Planned work loses nothing.** Draining a cell stops dispatch to it; running builds finish
   or resume after the restart. Upgrades roll cell by cell, a small cell first.

9. **Measured, and broken on purpose.** SLOs: no trigger lost, takeover p95, share of builds
   resumed, agent wait, logs lost = 0. A chaos suite kills the JVM, powers nodes off,
   partitions the network, stalls storage and fails PostgreSQL over; every run is recorded
   as evidence.

### Spike results (lab, 2026-10-01; evidence in lab/evidence/spike-*.json)

| Failure | Configuration | Outcome | Takeover |
|---|---|---|---|
| Controller JVM killed | any | resumed, 180/180 log lines, SUCCESS | Jenkins 12.5 s, build 14 s |
| Controller node powered off | plain volume, stock agent | agent never reconnected; FAILURE | Jenkins 99 s |
| Node powered off | plain volume + agent supervisor | agent back 3.6 s after Jenkins, **but state went back ~20 s: the `sh` step ran a second time in a new workspace while the first kept running** | Jenkins 87 s |
| Node powered off | `sync,dirsync` volume | step ran once, SUCCESS | Jenkins 92-98 s |
| Node powered off, 1 s into the step | writeback 1 s + `commit=1` | step ran once, all lines, SUCCESS; < 1 s of controller-side log lost | Jenkins 76-78 s |
| Queued item (quiet period), JVM killed | any | **queue lost** (JENKINS-30909) | -- |
| Same, graceful restart | any | queue kept | -- |

Cost per build (io-probe: 200 flow-node steps, 20,000 log lines; median of 5): plain 6.5 s /
0.98 s, `sync,dirsync` 34.4 s / 14.6 s (about 4x the whole build), writeback 1 s + `commit=1`
6.6 s / 0.91 s (no measurable cost).

Takeover after a power loss is dominated by detection: ~50 s for the node to be marked
NotReady. The Cell Supervisor's own lease (decision 4) is what brings it towards the 60 s p95.
Each figure is one run; the chaos suite repeats them with random failure points.

### Cell agent and Cell Supervisor (built 2026-10-01)

**Cell agent** (`cmd/cell-agent`, a native sidecar of the controller pod). It holds the cell's
Lease as `<pod>/<uid>` (15 s duration, renewed every second, 10 s renew deadline; 6 s / 4 s
was tried first, see "Unattended takeover" below). Expiry is
judged only on the observer's own monotonic clock (unchanged for the whole duration), never
from timestamps another machine wrote. The deadline counts from when a renewal *started*, so
a holder stops at least `duration - deadline` before anyone else may start, whatever the
latency of its calls. Three layers stop a controller that lost its Lease:
1. the agent SIGKILLs the JVM at the deadline (shared process namespace);
2. `cell-agent guard`, the controller container's command, kills it when the gate file's
   mtime -- set to the start of each renewal -- is older than the deadline. That covers the
   agent itself hanging or dying;
3. Jenkins starts only behind an open gate, so a restarted container is gated too.

On a graceful stop the Lease is released only after Jenkins has exited.

**Cell Supervisor** (`cmd/supervisor`, two replicas, leader-elected). It reads the API server
directly every second (never an informer cache) and forgets what it saw after a gap. The one
exception is when to ask for a machine's power state. A Lease whose version has not changed
across a gap was not renewed during it, because the reads are quorum reads, so it still
prompts the question. A machine is fenced on that basis only if it reports off. Expiry, and
anything done on a guess, still need an unbroken run of observations. When a lab machine that
was also an etcd member lost power, observations failed for a few seconds while etcd elected a
leader, and the reset added 4 s to the fencing. It acts
only on a Lease that names the current pod. Once that Lease has gone unrenewed for 3 s (three
missed renewals), it asks the machine's power controller at once. A machine that reports **off**
is fenced then, without waiting for the Lease to expire: nothing runs on a machine that is off,
and a holder alive elsewhere (a wrong mapping) would have renewed within those 3 s. Otherwise it
waits for the Lease to go unchanged for its whole duration, then asks the power controller:
- **off:** fence at once. Six seconds after a power loss the kubelet still looks alive.
- **running, kubelet silent for 20 s:** power off, unless that would cost the control plane
  its majority.
- **running, kubelet alive:** wait, then restart the pod after 30 s.
- **unknown:** alert. It never fences on a guess.

When most cells or nodes fail together, it powers nothing off. The order of a fencing is the
safety argument: confirm the machine off, then release the Lease conditionally on the version
the decision saw, then taint `out-of-service`, then delete the pod by UID. A conflict at the
release means something renewed a Lease whose machine is off, so the node-to-machine mapping
is wrong and fencing stops before anything lets a successor start. Startup refuses a mapping
that contradicts what it sees. Each of these rules has a test that turns red when the rule is
removed.

Lab evidence for the agent (cell-b, `lab/evidence/agent-*.json`, one run each):

| Probe | Result |
|---|---|
| Pod deleted | Lease released 0.07 s after "Jenkins stopped"; new pod held it at epoch+1 7.8 s after the delete; Ready at 22 s |
| Agent SIGKILLed (restarted at once) | Jenkins not interrupted; same holder and epoch |
| Agent SIGSTOPped (hung) | guard killed Jenkins 3.5 s after the stop (exit 137); liveness restarted the agent; Ready again |
| Pod cut off from the API server for 20 s (iptables in its network namespace) | Jenkins killed 3.2 s after the cut; no JVM of the pod while cut off; back after the cut healed |

### Unattended takeover (lab, 2026-10-01; lab/evidence/spike-resume-node-poweroff-supervised-*.json)

A VM was powered off under a 240 s build, with no operator action. The supervisor detected the
loss, confirmed the machine off through the power agent, released the Lease, tainted and
marked the node, deleted the pod, powered the machine back on and removed the taint. In every
run:
- the build resumed and finished SUCCESS;
- its `sh` step ran once;
- at most 1 of 240 log lines was lost.

What the runs found, and what changed:

| Run | Fenced after | Jenkins up after | What it showed | Change |
|---|---|---|---|---|
| 1 | 21 s | 67 s | The leader, on a healthy node, lost its lease: one HTTP/2 connection to the dead API server, every call failing. Restarted, it refused to start: the dead node still read Ready | HTTP/1.1 clients; the startup check retries and calls a mapping wrong only if the kubelet heartbeat is renewed |
| 2 | ~31 s | 95 s | The leader died with the cell's node. Longhorn moved the volume only once Kubernetes marked the node NotReady, 74 s after the power loss | The supervisor marks a fenced node NotReady itself; its pods prefer nodes without cells |
| 3 | 6.6 s | 109 s | Fast fencing, but the machine was powered on 30 s later, while the volume was still moving; Longhorn deleted the almost-started pod | Power on only once every cell is held again |
| 4 | 30 s* | 61 s | Volume attached 13 s after fencing (was 27-45 s) | — |
| 5 | 16 s* | 75 s | **A healthy cell on another node lost its Lease and restarted Jenkins:** etcd on the surviving members took 1-3 s per read, and the 4 s renew deadline did not outlast that | Cell Lease 15 s / 10 s, as Kubernetes' own controllers use; renewals retried with short attempts; the supervisor re-enters the election instead of exiting |

| 6 | 16.5 s | 44.5 s (build resumed 42.5 s) | With 15 s / 10 s: the healthy cell on another machine kept its Lease and its Jenkins; the supervisor kept its leadership. Timeline from the build's ticks and the pod's conditions: fenced 16.5 s, pod scheduled 18.5 s, volume attached and Lease held 31.5 s, build resumed 42.5 s | The probe's kubectl now fails over between API servers (pinned to the dead one, it recorded 58 s for all of these) |

\* The supervisor's leader lost its own lease in the same API stall and had to regain it.

Run 6 is the design working as intended: a power loss is taken over in ~45 s, with no person
and no lost work.

**Repeated (lab/spike/chaos_poweroff.py, random failure tick 5-60, evidence chaos-poweroff-*.json).**

| Series | Runs | Builds | Fenced | Lease held | Build resumed | Other cell |
|---|---|---|---|---|---|---|
| Before the hot standby | 4 | 4/4 SUCCESS, 1 line lost once | 27.5-37.9 s | 61.5-75.1 s | 81.2-93.9 s | Never lost its Lease when on another machine; taken over when on the same one |
| Hot standby (every replica observes) | 4 | 4/4 SUCCESS, 1 line lost once | 14.5-23.1 s | 52.4-63.1 s | 71.1-83.7 s | The same |
| Supervisor deletes every pod on the fenced node | 4 | 4/4 SUCCESS | 15.9-23.7 s | 57.6-65.9 s | 74.7-83.5 s | The same |
| One CSI attacher per node; attacher health check 2 s + 2 s | 4 | 4/4 SUCCESS, 1 line lost once | 20.5-22.9 s | 31.4-75.9 s | 49.6-93.9 s | The same |
| Longhorn managers' health check 2 s + 2 s; a machine found off is fenced 3 s after its last renewal | 2 (host shut down) | 2/2 SUCCESS | 17.4 s | 29.5-34.1 s | 47.3-55.7 s | The same |
| netCI's API clients bound each attempt (ADR-066) | 1 | 1/1 SUCCESS | 2.7 s | 19.8 s | 33.5 s | Run 2 found a deadlock: the cell had no node with room, and the supervisor would not power the fenced machine on while the cell awaited its takeover. Fixed, with priority classes |
| `netciOnce` blocks; probe evidence includes the supervisor's counters | 3 | 3/3 SUCCESS, one `netciOnce` marker each | 10.1-20.3 s | 23.8-46.4 s | 42.8-66.5 s | The leader lost observations to the dead API server (fixed: direct API server dialling), or died with the machine (fixed: leader hand-over) |
| Direct dialling, hand-over | 2 | 2/2 SUCCESS | 10.3-19.0 s | 37.5-41.8 s | 55.8-60.5 s | A gap in observations restarted the count (fixed: LeaseQuiet); the replicas had drifted beside the cells (fixed: rebalancing) |
| LeaseQuiet, rebalancing | 3 | 3/3 SUCCESS | 3.6-8.2 s | 30.2-46.9 s | 48.0-77.7 s | A renewal just before the stall was counted from its first sight (fixed: renewTime within the gap) |
| **Every fix** (`chaos-poweroff-20261001T182441Z.json`) | 6 | **6/6 SUCCESS**, one `netciOnce` marker each, no line lost | **2.9-9.3 s** | 30.2-49.3 s | **49.0-68.7 s** | In 4 of 6 runs both cells were on the lost machine; both were taken over |
| Longhorn tuning as admission policies (it had been undone mid-series) | 6 | 6/6 SUCCESS, one marker each | 2.6-9.6 s | 28.2-86.2 s | 43.3-105.0 s | One attach took 70 s (unexplained; logs lost) |
| Attacher leader lease 10 s; probe records the leaders | 6 | 6/6 SUCCESS, one marker each | 3.1-7.4 s | 25.6-64.4 s | 42.5-83.4 s | Every run lost a leader of kube-controller-manager, kube-scheduler or the attacher |
| **Control-plane timings at upstream values** (`lab/k3s-timings.sh upstream`; `chaos-poweroff-20261001T201806Z.json`) | 6 | **6/6 SUCCESS**, one marker each, 1 line lost once | **2.5-3.5 s** | 19.1-47.4 s (median 23.2) | **37.4-64.8 s (median 40.9)** | Slowest: both cells and all three leaders on the lost machine |
| Every cell on a fenced machine released at once; attacher retries ≤ 2 s | 6 | 6/6 SUCCESS, one marker each, 1 line lost once | 2.9-3.6 s | 13.4-64.8 s | 32.9-82.6 s | Longhorn deleted the replacement after it took its Lease in 2 runs (ADR-067) |
| **netCI restarts a failed JENKINS_HOME; Longhorn leaves StatefulSets alone** (`chaos-poweroff-20261001T213158Z.json`) | 6 | **6/6 SUCCESS**, one marker each, no line lost | **2.8-3.6 s** | 14.3-43.5 s (median 19.9) | **32.6-63.3 s (median 40.5)** | Jenkins started in 11-16 s every time |
| **Longhorn tuning without template annotations** (`chaos-poweroff-20261001T220424Z.json`) | 6 | **6/6 SUCCESS**, one marker each, 1 line lost once | **2.7-3.8 s** | 18.0-31.9 s (median 24.6) | **40.8-50.3 s (median 42.9)** | Every run lost two or three of the controllers' and the attacher's leaders with the machine; no run past 51 s |
| **Everything together**: Redfish fencing, log shippers, plugins from the image only, honest headroom, sandbox NetworkPolicy, Kata on one machine, Loki on the host (`chaos-poweroff-20261002T105339Z.json`) | 6 | **6/6 SUCCESS**, one marker each, no line lost | **2.8-4.1 s** | 19.7-37.6 s (median 25.4) | **40.0-66.7 s (median 51.2)** | Both cells on the lost machine in 3 runs; both taken over |
| **Installed with the Helm charts** (`chaos-poweroff-20261002T060641Z.json`) | 6 | **6/6 SUCCESS**, one marker each, 1 line lost twice | **3.1-3.6 s** | 19.8-44.3 s (median 25.0) | 53.7-73.4 s (median 57.7) | Jenkins took 25-31 s to start (11-16 s before): the host had come back from a reboot in its power-saver profile, cores at 1.6-1.9 of 4.5 GHz |

**Hung machines, fenced through Redfish.** In the series above a machine loses its power, so
the power controller already reports it off. In these the machine is suspended
(`virsh suspend`): running, answering nothing. The supervisor itself must power it off, once
its kubelet has been silent for 20 s, through sushy-tools' Redfish emulator in front of the VMs
(`lab/redfish.sh`; TLS pinned by SHA-256, basic auth, only the three lab machines visible).

| Series | Runs | Builds | Fenced (machine off) | Lease held | Build resumed | Other cell |
|---|---|---|---|---|---|---|
| First (`chaos-poweroff-20261002T063919Z.json`) | 2 | 1 SUCCESS, then **FAIL** | 20.0 s | 38.4 s | 67.1 s | Run 2: both cells were on the hung machine (the preferred anti-affinity lost to the scheduler's other scores). The supervisor read "2 of 2 cells stopped renewing at once" as a shared cause and fenced nothing; both controllers stayed down. Fixed: the mass-failure guard counts machines, not cells, as Kubernetes' node controller and NHC do |
| Both cells on the hung machine, after the fix (`chaos-poweroff-20261002T065849Z.json`) | 1 | SUCCESS, one marker | 24.1 s | 36.8 s | 73.6 s | Both taken over |
| **After the fix** (`chaos-poweroff-20261002T070357Z.json`) | 6 | **6/6 SUCCESS**, one marker each, no line lost; every machine powered off by the supervisor and back on | **18.6-22.3 s** | 33.1-51.9 s (median 39.8) | **69.1-82.5 s (median 71.2)** | Both cells on the hung machine in 3 runs; both taken over each time |

A hung machine costs ~17 s more than a power loss: the 20 s the supervisor waits for a silent
kubelet before it switches a running machine off. That wait is what separates a hung machine
from a slow one; it is a setting (`NodeStale`), not a limit of the design.

With the lab's control plane at upstream timings, standing in for a control plane on machines
without cells, a power loss is fenced in about 3 s. The build continues after 41-50 s even when
the machine also took the leaders of kube-controller-manager, kube-scheduler and the CSI
attacher (the latest series), and after 32.6 s at best. With k3s's own timings, fencing takes ~3 s, or 8-9 s when the lost machine held etcd's
leadership. The
lab's k3s sets etcd's election timeout to 5 s, and until a new leader is elected no API server
answers anyone, so the supervisor has nothing to act through. A control plane on machines that
run no cells, or etcd with its 1 s default, removes that difference.

What remains is the storage moving the volume (21-40 s from fencing to the Lease held, longer
when two volumes move at once) and Jenkins starting (~10 s, longer when two start on one
machine). The traces (`lab/spike/trace_takeover.sh`) found and removed two blind spots on the
storage's side (ADR-066). The volume move itself is the next thing to measure.

### What this reuses from netCI

The Jenkins REST client and controller router (ADR-009), admission budget and supersession
(ADR-050), plugin and toolchain drift gates (ADR-056/059), leases and fencing tokens
(ADR-016/041), the reconciler (ADR-020), webhook signature checks and de-duplication
(ADR-019), workload identity for callbacks (ADR-015). They are rewritten in Go (ADR-062).

### Rejected

- **Active/active replicas on one `JENKINS_HOME`.** The only complete implementation is
  proprietary and constrained even there; building one means maintaining a distributed
  rewrite of Jenkins internals against every plugin release. Research after this ADR is in
  production, not before.
- **Restore from backup as the failover path** (ADR-055). RPO of a backup interval and a
  restore on every failure; replication makes RPO 0 and backups stay for DR.
- **One large controller.** A single queue lock and a single blast radius for the whole
  organisation; the Jenkins project's own guidance is to scale by adding controllers.

### Consequences

- Jobs must be defined as code (Job DSL) for a cell to be rebuilt or a job to spill to a
  sibling cell; hand-made jobs are migrated once.
- Webhooks point at netCI's intake, not at a controller.
- Builds on a cell that fails pause for up to the takeover time; CloudBees adopts them almost
  immediately. Everything else in the table of guarantees holds.
- Nothing in this ADR is verified yet. The first work is a spike on the lab (3 KVM VMs, k3s,
  Longhorn) that proves or disproves resume, agent reconnection, queue loss on crash,
  controller start time and replicated storage.

## ADR-063: A durable run queue outside Jenkins, and dispatch that cannot start a run twice

- Status: accepted
- Date: 2026-10-01
- Refines: ADR-060 decision 3

### Context

Jenkins keeps its queue in memory and writes it to `queue.xml` only on a graceful shutdown. A
controller that crashes loses every queued item (JENKINS-30909, confirmed in the lab: a quiet-
period item vanished when the JVM was killed). A takeover of a cell is a crash, so every
trigger waiting in that cell's queue is lost. ADR-060 promises that no trigger is lost.

A run identity has to survive into Jenkins for anything outside it to tell whether a run
started. The lab showed that the REST API cannot carry one for every job:
- `buildWithParameters` drops parameters that the job does not declare (SECURITY-170);
- it refuses jobs without parameters (HTTP 400).

### Decision

1. **Accepted means durable.** A trigger (webhook, schedule, API call, person) becomes a row in
   PostgreSQL before anything acknowledges it. The row carries a run id (UUID), the job, its
   parameters, the cell that owns the job, and the client that asked (decided by the server
   from the client's credential, never taken from the request). An optional idempotency key
   (a webhook's delivery id) makes a retried delivery the same run.

2. **The netCI Jenkins plugin dispatches idempotently.** `POST /netci/dispatch` with
   `(job, runId, parameters)`:
   - holds Jenkins' queue lock;
   - looks for the run in the queue (pending items included), in the work units of executors
     that have taken an item but not yet created its build, and in the job's recent builds;
   - returns what it finds, or schedules the build with a `NetciRunAction(runId)` that is
     copied onto the build and saved with it.

   Under the queue lock no item can move between those places, so a run is never found
   nowhere while it is in fact starting. The same call repeated, by a retry, a second
   dispatcher or after a crash, returns the existing run instead of scheduling another.
   Parameters the job does not declare are refused (HTTP 400), never dropped silently. The
   caller needs `Item.BUILD` on the job, as the UI would require.

3. **The dispatcher re-sends until Jenkins has started the run.** For every run that is
   accepted or dispatched but not yet started, it calls dispatch again whenever it has not
   seen this controller session (`X-Jenkins-Session`, new on every start) acknowledge the run.
   After a crash the new controller's lookup finds nothing, and the run is scheduled again;
   after a graceful restart the restored queue item is found and nothing is duplicated. Runs
   never move to another cell: a job belongs to one cell, and the cell's lease (ADR-060)
   guarantees that only one controller of it is alive.

4. **Admission.** Each cell is given at most a configured number of dispatched-but-not-started
   runs. Jenkins' queue stays short, and the queue that matters is the durable one.

5. **What this does not cover.** A build whose start reached an agent but whose record was lost
   in the last second before a power loss (ADR-060's bounded writeback) can be scheduled again.
   The re-execution guard (ADR-060 decision 1) is what covers that window, not this ADR.

### Lab result (cell-b, 2026-10-01; lab/evidence/queue-crash-*.json, one run)

Five runs were submitted through netci-queue and one trigger went straight to Jenkins. With
all six in Jenkins' queue (20 s quiet period), the controller's JVM was SIGKILLed:
- all five netCI runs were found missing from the restarted controller's queue and dispatched
  again;
- each finished SUCCESS with exactly one build carrying its run id;
- the direct trigger was lost and never ran;
- 34 s passed from the kill to the last build finishing.

Webhooks (lab GitLab 19.4.1, lab/evidence/webhook-gitlab-*.json): a push to `main` was
accepted as a run 1.2 s after the push, and its Jenkins build (with the pushed SHA as a
parameter) finished at 8.9 s. GitLab was then asked to resend the delivery. Both deliveries
reached netCI with the same Idempotency-Key and were answered with the same run, and one run
exists. Every build of the probe job maps to one real push, including the README commit GitLab
made when it created the project.

### Rejected

- **A required `NETCI_RUN_ID` job parameter:** every job would have to change, and jobs without
  parameters could not be triggered.
- **`hudson.model.ParametersAction.safeParameters`:** it keeps an undeclared parameter but
  still refuses jobs without parameters, and it changes a security default.
- **Matching builds by start time:** ambiguous whenever two runs of one job start together.
- **Persisting Jenkins' queue on every change:** that patches core behaviour of every
  controller and would still lose the item scheduled in the last second.

## ADR-065: `netciOnce` -- a step that must not run twice is guarded outside JENKINS_HOME

- Status: accepted
- Date: 2026-10-01
- Implements: ADR-060 decisions 1 ("re-execution guard") and 6 ("deploy stages are guarded on resume")

### Context

A controller that loses power loses at most about a second of `JENKINS_HOME` (bounded writeback,
ADR-060). If a step started in that second, the record of its start is gone, and when the
Pipeline resumes on the new controller it starts the step again. The lab saw exactly this when
writeback was unbounded: an `sh` step ran a second time, in a new workspace, while the first
copy was still running.

Re-running a compile is harmless. Re-running a deployment, a database migration, a release
publication or a payment is not, and nothing inside `JENKINS_HOME` can tell, because what was
lost is precisely the record that would.

### Decision

1. **A block step, `netciOnce(key) { ... }`.** The author marks what must not run twice. Before
   the body starts, the plugin records `(build, key)` in netCI's PostgreSQL through netci-queue:
   `POST /v1/once {scope, key, nonce}`. That record lives outside `JENKINS_HOME` and does not
   roll back with it.
   - First record of this build and key: the body runs.
   - Record already there with **another nonce**: the block started before, in a state the
     controller no longer has. The step fails with that explanation, and a person decides,
     e.g. by re-running the block with `netciOnce(key, retry: true)` after checking the target.
   - Record already there with **the same nonce**: a retried call of the same attempt (its
     reply was lost). The body runs.
2. **The nonce** is generated when the step starts and kept with the step execution. After a
   normal resume the step is resumed, not started, so no record is asked for. After a rollback
   the step is started anew, with a new nonce.
3. **The scope is the build:** the controller's instance identity, the job's full name and the
   build number. It is decided by the plugin, and the server records which client (cell) asked.
   Each controller has its own netci-queue client with `"once": true` and no `jobs`: the
   credential on every controller records markers and can start no run, and a webhook or API
   client cannot record markers (403).
4. **Fail closed.** If netci-queue cannot be reached, the body does not run. A guarded step
   that might run twice is worse than one that waits; the call is retried for up to a minute,
   then the step fails.

### Rejected

- **Guarding every step:** a re-run compile or test is harmless, and recording every step
  would put the database on every build's critical path.
- **Detecting the orphaned process on the agent** (durable-task's control directory): it shows
  that something ran, not whether running it again is safe, and the agent may be gone too.
- **Synchronous writes for `JENKINS_HOME`:** the spike measured about 4x the build time.

## ADR-061: Agent fabric -- isolated build sandboxes on VM hosts, provisioned ahead of demand

Status: Proposed (2026-10-01).

### Context

The organisation builds on long-lived VM agents: state leaks from one build into the next,
idle VMs cost capacity, and peaks queue. One fresh VM per build fixes the first problem and
wastes the machine: most builds need a fraction of a VM, and a VM takes minutes to boot.

The practice elsewhere (verified, sources in the links at the end): GitLab runs several jobs
per autoscaled instance (`capacity_per_instance`) and warns that jobs sharing an instance
must be trusted, recommending a job-per-instance or a nested VM for untrusted ones; Nodepool
and GitLab keep ready capacity (`min-ready`, `idle_count`) and retire instances after N uses
(`max_use_count`); microVM runners (Firecracker) boot a fresh guest per job in under a second;
Jenkins' default provisioning deliberately waits before adding capacity, which the EC2 Fleet
plugin's no-delay strategy removes.

### Decision

1. **A VM is a host; a build runs in a sandbox on it.** The VM fleet is a Kubernetes cluster
   (k3s/RKE2 on the organisation's hypervisor). A sandbox is a pod holding one Jenkins agent
   with one executor, created for one build and destroyed after it. Kubernetes supplies
   scheduling by resources, cgroup limits and networking; netCI supplies the policy.

2. **Isolation follows trust (RuntimeClass).**

   | Class | For | Runtime | Isolation |
   |---|---|---|---|
   | `standard` | the organisation's own branches | runc with a user namespace (`hostUsers: false`), seccomp, no privilege | shared kernel; container root is unprivileged on the host |
   | `docker` | builds that run `docker build` / compose | Sysbox (Docker inside without `--privileged`) | wider kernel surface (+~267 % reachable syscalls): trusted code only |
   | `untrusted` | fork pull requests, outside code | Kata Containers (needs nested virtualisation), else gVisor, else a dedicated host per build | separate kernel |

   Untrusted sandboxes also run on their own node pool (taints), so a kernel escape from a
   trusted-class sandbox and an untrusted build never share a host.

3. **Neighbours cannot starve each other.** Every sandbox has CPU, memory and PID limits,
   an ephemeral-storage limit, IO weight, and a network policy that allows egress only to what
   builds need (git, registry, artifact proxy, the cell). Workspaces are ephemeral. Secrets
   arrive at step time through Jenkins credentials, never in an image.

4. **Hosts are immutable and short-lived.** A host is drained -- no new sandboxes, running ones
   finish -- and replaced when it reaches N builds, M hours, an outdated image (image drift is
   gated like plugin drift, ADR-059) or fails health checks. Images are built with Packer
   from `toolchain/versions.yaml`.

5. **Caches are shared without being poisoned.** Image layers and dependencies come through
   read-only mirrors/proxies on or near each host; writable caches are per tenant, so one
   team's build cannot plant content another team's build will execute.

6. **Two-level scaling, ahead of demand.**
   - *Sandboxes (seconds):* warm sandboxes are started in advance with the toolchain loaded
     and an agent that is not yet bound to any controller. A claim binds one to a cell and a
     build (late binding), so the build starts in about a second instead of paying image pull
     and JVM start.
   - *Hosts (minutes):* the fleet grows when projected use crosses a threshold. The projection
     reads the global queue (ADR-060) -- which knows about builds before any controller does --
     plus the time-of-day history; it shrinks when hosts are idle beyond a hold time, never
     below a floor.
   Both levels have per-class and per-label limits and respect hypervisor quota.

7. **Lifecycle is a state machine in PostgreSQL**, atomic and audited like everything else:
   sandbox `warm -> claimed -> connected -> busy -> released -> deleted`, with `failed` and
   `orphaned`; host `provisioning -> ready -> draining -> retired`, with `unhealthy`.
   Rules that are never broken: a sandbox holding a build is never deleted, including while
   its cell is taking over (ADR-060) -- it is kept until the build resumes or the durable-task
   timeout plus a grace period passes; orphans (a sandbox with no Jenkins node, a node with no
   sandbox) are reaped only after that grace.

8. **Jenkins sees an ordinary cloud.** A Java plugin implements a Jenkins `Cloud` that claims
   sandboxes from the fabric with no provisioning delay, so builds started inside Jenkins --
   by people, cron or other jobs -- are served too, not only those netCI dispatches. One
   fabric serves every cell.

9. **Hosts come from a driver.** libvirt/KVM in the lab first; vSphere, OpenStack or Proxmox
   for the organisation (its hypervisor is not known yet). Cluster API providers are an
   option behind the same interface.

10. **Measured.** Agent wait p50/p95, sandbox start, host boot, utilisation, idle
    sandbox-minutes and host-minutes wasted, failures by class; every policy change is judged
    against these numbers, before and after.

### Rejected

- **A fresh VM per build.** Minutes of boot and a whole VM for a fraction of its use; kept
  only as the last fallback for untrusted builds where nested virtualisation is unavailable.
- **Long-lived shared VM agents** (today). Leaks between builds, no isolation between teams.
- **A scheduler written for this.** Kubernetes already schedules by resources, enforces
  cgroups and networking and offers RuntimeClass; the value to add is the policy on top.
- **The Kubernetes plugin's provisioning alone.** It creates a pod after the build is queued
  (image pull and JVM start on the critical path), has no warm capacity bound late to a
  controller, no trust classes and no fleet scaling ahead of demand.

### Consequences

- The organisation runs Kubernetes on its build VMs; people still see "a VM fleet".
- `untrusted` at full strength needs nested virtualisation on the hypervisor (vSphere and KVM
  offer it). ~~Where it is absent the fabric falls back and reports which fallback it used.~~
  Superseded (2026-10-02): there is no fallback. A pool's RuntimeClass is checked when
  netci-fabric starts, and a missing one stops it with the pool named. A fallback would run code
  the organisation does not trust on the shared kernel while the pool still said `untrusted`.
  An organisation without nested virtualisation gives such builds dedicated hosts or does
  without the pool -- a choice made in the open, not by the fabric.

### Verified on the lab (2026-10-02)

- `standard`: container root maps to an unprivileged host uid (`fabric-agents-*.json`).
- `untrusted`: Kata Containers 4.2.0, QEMU on the Rust runtime, on one machine with nested KVM
  (`lab/kata.sh`). Four builds ran in four VMs: guest kernel 6.18.35 against the machines'
  6.8.0-142, CPU flag `hypervisor`. Each VM was discarded after its build. A build took ~10 s
  with a warm sandbox, as on `standard` (`lab/evidence/untrusted-sandboxes-20261002.json`).
- A Kata VM is sized by the pod's memory limit, and its kernel keeps 70-95 MiB of it: a pool
  limited to 1 GiB gives builds 947 MiB. Kata's `default_memory` does not add to it.
- The NetworkPolicy holds from inside a Kata VM as from a `standard` sandbox: fabric,
  controller and internet reachable; netci-queue, the API servers, a kubelet and Prometheus not.
- A pool naming a RuntimeClass the cluster lacks: the new fabric replica stopped with the pool
  named, and the running replicas kept serving until the change was rolled back.
- Not done: `docker` (Sysbox), a separate node pool with taints for `untrusted`, image builds
  inside an untrusted VM.

Sources: docs.gitlab.com (runner advanced configuration, docker autoscaler, instance
executor), zuul-ci.org nodepool configuration, javadoc ec2-fleet NoDelayProvisionStrategy,
actuated.com, northflank.com and edera.dev isolation comparisons, github.com/nestybox/sysbox,
kubernetes.web.cern.ch (rootless builds, user namespaces default in 1.33).

## ADR-064: Agent fabric v1 -- warm sandboxes bound late to a controller

- Status: accepted
- Date: 2026-10-01
- Implements: ADR-061 decisions 1, 2 (`standard` class), 3 (limits, no API credentials), 6
  (sandbox level), 7 and 8. Host scaling (6, hosts), host recycling (4) and the `docker` and
  `untrusted` classes come later.

### Decision

**Pieces.**
- `netci-fabric` (Go) keeps a pool of warm sandbox pods per *pool* (image, resources,
  RuntimeClass, labels) in its own namespace and serves claims.
- The netCI plugin adds a Jenkins `Cloud`.
- `netci-sandbox` (Go) is the sandbox's entrypoint.

**Late binding.**
1. Jenkins needs an executor for a label one of its pools serves. The cloud provisions with no
   delay, and its launcher asks the fabric for a sandbox:
   `POST /v1/claims {cell, pool, agent, secret, controller}`.
2. The fabric takes a warm sandbox atomically, or creates one when none is warm, and keeps the
   binding (agent name, its inbound secret, the controller's URL) **in memory only**. The
   secret is a credential and is never written to the database.
3. `netci-sandbox` in the pod has been long-polling `GET /v1/binding`. It receives the binding
   and runs `agent.jar -webSocket` towards the controller. The build starts as soon as the agent
   connects; the image pull and the pod start happened before anyone was waiting.
4. After the build, the agent's retention terminates the node. The plugin calls
   `DELETE /v1/claims/{id}` and the fabric deletes the pod. A sandbox serves one build.
   (Since ADR-071, `GET /v1/claims/{id}` tells the claiming cell what became of its claim.)

**A sandbox proves who it is with its own pod.** The pod gets a projected ServiceAccount token
with audience `netci-fabric` and a 10-minute lifetime, and no other API credential
(`automountServiceAccountToken: false`). The fabric checks it with a TokenReview and takes the
pod's name and UID from the review, never from the request. A pod can only receive its own
binding.

**The plugin authenticates to the fabric** with a per-cell bearer token, of which the fabric
stores only the SHA-256. The token decides the cell; a claim naming another cell is refused.

**State.** Sandboxes are rows in PostgreSQL:
`creating -> warm -> claimed -> bound -> released -> deleted`, with `failed`. Transitions are
compare-and-set, and each one is an audited event. The reconciler compares rows with pods
every second:
- a pod with no row is deleted;
- a row whose pod vanished becomes `failed`;
- a claim not bound within 2 minutes is released and its pod deleted;
- the pool is refilled up to its warm count, never above its maximum.

**The sandbox survives a takeover of its controller** (ADR-060). `netci-sandbox` restarts the
agent JVM when the agent exits or when the controller's `X-Jenkins-Session` changes (a
controller lost to power never sends a FIN). It stops only when the fabric says the claim was
released.

**Isolation (`standard`).**
- runc with a user namespace (`hostUsers: false`) where the node supports it;
- non-root, all capabilities dropped, no privilege escalation, RuntimeDefault seccomp;
- CPU, memory and ephemeral-storage limits;
- an `emptyDir` workspace;
- no API credentials.

**One active fabric replica.** Bindings live in memory, so the API and the reconciler run only
in the replica holding the fabric's leader lease. The Service has no selector; the leader writes
the Service's EndpointSlice to point at itself (RBAC allows updating that one slice only).
A standby replica is an ordinary ready pod, so rollouts complete. A failover loses unbound
claims, and Jenkins provisions again.

**Controllers provision at once.** By default Jenkins provisions no cloud agent for 100 s after
it starts (`NodeProvisioner.initialDelay`) and then only every 10 s. After a takeover, that
would make new builds wait over a minute and a half, whatever is warm. Cells run with
`-Dhudson.slaves.NodeProvisioner.initialDelay=0` and a 2 s `recurrencePeriod`. In the plugin's
tests a build on a sandbox went from about 115 s to about 7 s, Jenkins' own start included.

### Lab result (cell-b, 2026-10-01; lab/evidence/fabric-agents-*.json)

Works end to end:
- Pipeline `node('netci-standard')` builds ran in fabric sandboxes;
- every claim was released and the pool returned to two warm sandboxes;
- the sandbox ran in a user namespace (`uid_map` maps uid 0 to host uid 813957120, not 0).

From the sandbox's own log:
- the binding arrives 0.00-0.01 s after the claim;
- the agent's WebSocket is connected 0.5-0.6 s after the binding;
- the rest of a trivial build is the `sh` step and the node's removal.

Before a QueueListener woke the provisioner, 2.6 s passed between a build starting and its
claim (the provisioner's period).

**Not faster than the Kubernetes plugin on this lab, yet.**

| Trivial build, trigger to done | Fabric sandbox | Kubernetes plugin pod |
|---|---|---|
| one at a time, median of 5 | 5.9 s | 4.4 s |
| 4 at once, median | 7.0 s | 3.3 s |

Each side had outliers of 17-20 s right after the controller restarted (a warm-up, not yet
explained). On an idle cluster whose nodes already hold the agent image, a new pod starts in
about a second, which leaves warm capacity little to win. The fabric's value shown here is:
- isolation (user namespace, no API credential, limits);
- sandboxes that survive a takeover;
- one pool shared by every cell.

Its latency case — large toolchain images, busy nodes, heavy pod start — is still to be
measured. Until it is, the fabric is not claimed to be faster.

### Rejected

- **Putting the binding in a Secret mounted into the pod:** a Secret update takes up to a
  minute to reach a running pod, and it would put the agent's credential in etcd.
- **Exec into the pod to hand over the binding:** that needs `pods/exec`, which is far more
  power than a fabric should hold.
- **Using the Kubernetes plugin as it is:** the pod is created only after the build is queued,
  with no warm capacity and no binding late (ADR-061).

## ADR-066: API clients keep working when an API server dies with its machine

- Status: accepted
- Date: 2026-10-01
- Refines: ADR-060 (unattended takeover)

### Context

A cell's machine can also run an API server: in the lab every machine is a control-plane
node, and small production clusters are often built the same way. When that machine loses
power, its API server's TCP connections neither answer nor close. Behind the `kubernetes`
Service, each client connection was balanced to one API server, so some of every client's
connections now lead nowhere.

Traces of unattended power-offs (`lab/spike/trace_takeover.sh`, probe evidence with the
supervisor's counters) found this in four places:

| Client | What it did | Cost |
|---|---|---|
| Cell Supervisor, pooled HTTP/1.1 connections | Two observations in a row waited out the 2 s timeout; the supervisor forgot what it had seen and started over | fenced at 9.7 s instead of ~4 s |
| Supervisor and cell agent before (HTTP/2) | One pinned connection carried every call | 7 s of failed calls; a healthy cell's renewals at risk |
| Longhorn CSI attacher (HTTP/2) | The leader lost its lease; no attacher left to move the volume | until a pod restarted |
| Longhorn manager (HTTP/2) | Its watches stayed on the dead connection for client-go's 30 s + 15 s; only it could stop a replica of the cell's volume | 44 s of the takeover |

### Decision

1. **netCI's own clients go through `internal/kubeclient`.** HTTP/1.1. Each attempt is bounded
   until its response headers arrive: 0.7 s for the supervisor, 0.5 s for its leader election,
   0.3 s for the cell agent and 2 s for the fabric. When an attempt fails, every connection of
   that client is closed, and a read is sent again on a new connection. The kubelet does the same
   after a failed heartbeat. Writes are not repeated by the transport, because only the caller
   knows whether its write may be repeated. Watches are refused rather than cut off.
   The supervisor goes further: it dials the API servers directly, from the endpoints of the
   `kubernetes` Service, and stays away for 30 s from one that failed an attempt. Bounding the
   attempts was not enough. Through the Service, each new connection still had a one-in-three
   chance of reaching the dead machine's API server until its endpoint was removed, 15-30 s
   later. Chaos series 7 then recorded the live leader failing three observations and starting
   over, so it fenced at 11.9 s. The request still names the Service, so TLS still verifies the
   Service's name, which every API server's certificate carries.
2. **Third-party clients on the takeover path get client-go's HTTP/2 health check at 2 s + 2 s**
   (`HTTP2_READ_IDLE_TIMEOUT_SECONDS`, `HTTP2_PING_TIMEOUT_SECONDS`): Longhorn's managers and
   CSI attachers. Attachers run one per node, spread per revision, and retry at most 5 s apart.
   These settings are applied as MutatingAdmissionPolicies (`deploy/longhorn/tuning-policy.yaml`),
   not as patches. Longhorn's driver deployer rewrites the attacher's Deployment every time it
   starts, so a patch was undone by the next takeover of its machine, and chaos series 10 ran
   mostly untuned. With the policies, the deployer was restarted and the attacher stayed tuned.
   The policies are shaped around a fault of the API server (k3s 1.36.4, cel-go 0.26.1). A JSON
   patch whose value is a list of typed objects panics the API server on every request the
   policy matches. The first version did exactly that to the attacher's Deployment and blocked
   updates to it until it was removed. Every variant has since been tried on a throwaway object
   first, while counting panics.
3. **A machine found off is fenced 3 s after its last renewal**, not at Lease expiry (ADR-060).
   That only helps if the observations around a power loss succeed, which decisions 1 and 2
   make so.

### Consequences

- In the lab, with all three decisions in place, the cell's machine was fenced 2.7 s after the
  power loss, and the build continued at 33.5 s. Before, those figures were 14.5-23 s and
  71-84 s. ADR-060 lists the runs.
- A cell machine that serves no API server does not meet this failure. Keeping the control
  plane on its own machines is still the better layout. These decisions keep a takeover fast
  when that layout is not possible.
- An API server that is slow to start answering the supervisor's lists makes it blind, not
  wrong: it acts on nothing it has not observed. `NETCI_API_ATTEMPT_TIMEOUT` raises the bound,
  and the `NetciSupervisorBlind` alert fires.
- The Longhorn settings are admission policies, so they survive the driver deployer's restarts,
  Longhorn's upgrades and a re-applied manifest. They are tied to Longhorn's object names, so a
  release that renames the attacher or the managers needs them updated. `lab/longhorn-tune.sh`
  fails if the attacher it finds is not tuned.

### What the traces found beyond netCI's reach

The traces after the last series broke the volume move into its waits. Each one comes from the
machine that lost power also running part of the control plane, or the storage's services:
- **kube-controller-manager and kube-scheduler** hold 15 s leader leases in k3s. When their
  leader was on the lost machine, the replacement pod was created 8-11 s late and scheduled up
  to 9.5 s late.
- **etcd's leader** waits 5 s before elections in k3s. Until a new one is elected, no API server
  answers anyone, so fencing itself was delayed to 8-12 s.
- **Longhorn's admission webhooks** are served by its managers, with a 10 s timeout and
  `failurePolicy: Fail`. Until the lost machine's manager was gone from the Service, an update to
  the cell's volume could wait the whole 10 s on it, and the volume controller lost a 10 s round.
  Fencing removes that endpoint: when fencing is late, the webhook calls are slow.

A control plane on machines without cells has none of the first two. `lab/k3s-timings.sh
upstream` sets etcd's elections and the two controllers' leases to upstream values, so the lab
can measure roughly what that layout gives.

### Rejected

- **Waiting for the dead API server's endpoint to leave the Service**: that is up to 15 s plus
  a reconcile period, and connections that already exist are not affected by it.
- **Disabling keep-alive**: every request would need a new TLS handshake, on every client,
  forever, to save seconds on a rare event. Closing connections only on failure costs nothing
  when nothing fails.
- **Retrying writes in the transport**: a lease renewal or a fence step that is repeated after
  a reply was lost may act twice. Their callers already retry them with conditions such as
  resourceVersion, which keeps a repeat safe.

## ADR-067: netCI restarts a cell whose JENKINS_HOME fails; the storage does not

- Status: accepted
- Date: 2026-10-02
- Refines: ADR-060 (cells, takeover), ADR-066 (storage tuning)

### Context

A volume can fail under a pod that keeps running. In the lab, Longhorn's engine for the cell's
volume died and Longhorn attached the volume again. The pod's mount then failed every write.
Kubernetes restarts no pod for that, so the controller would have run on, broken.

Longhorn has its own answer, `auto-delete-pod-when-volume-detached-unexpectedly`. After it
asks for a volume to be remounted, it deletes every pod that uses the volume and started
before that request. A takeover broke this rule. Once fencing had become fast (ADR-060), the
replacement controller often started before Longhorn recorded the remount it was requesting for
the lost machine's attachment. Longhorn then deleted the replacement, with a 30 s grace,
2 s after it had taken its Lease. That happened twice in six power-offs (chaos series 14), and
each time cost 35-55 s. The faster the takeover, the more often it happens.

### Decision

1. **The cell agent probes JENKINS_HOME.**
   - Every 5 s it writes a file under `.netci`, fsyncs it, renames it and fsyncs the directory.
   - A write that has not returned after 10 s counts as a failure, and no second write is
     started while it hangs.
   - After three failures in a row, it reports `"<identity> <time> <error>"` on the cell's Lease
     (`netci.io/volume-failed`, written with its renewals). That needs no permission beyond the
     Lease, and the report is cleared when the volume works again or by the next pod.
2. **The supervisor restarts the pod that reports it.**
   - It acts only on a report from the Lease's current holder, never a predecessor's.
   - The cell's 2-minute cooldown applies.
   - When more than half the cells report at once, it alerts instead: that is a storage
     problem, and restarting every controller would not fix it.
3. **Longhorn leaves StatefulSets alone.** Its auto-delete blacklist gets `apps/StatefulSet`
   (`lab/longhorn-tune.sh`, `deploy/helm/README.md`).

### Consequences

- **Lab:** with JENKINS_HOME made unwritable under a running controller, the failure was
  reported 12 s later. The supervisor restarted the pod 1 s after that. The new pod probed
  cleanly and cleared the report.
- **Any storage.** The probe sees the failure from inside the pod, so it works on Longhorn,
  Ceph and NFS alike. A hung NFS mount is a failure too.
- **The setting is cluster-wide.** Longhorn's blacklist applies to every StatefulSet in the
  cluster, including those netCI does not run, which lose Longhorn's remount after an engine
  failure.
- **Slow disks.** A disk where three writes in a row take more than 10 s each restarts the
  controller once, and then raises alerts within the cooldown. Such a disk cannot hold a
  controller's `JENKINS_HOME` anyway (ADR-060's writeback bound).

### Rejected

- **Keep Longhorn's deletion and delay the replacement** until Longhorn has recorded its remount
  request. That would add seconds to every takeover, and it would depend on the timing inside a
  storage system that netCI does not control.
- **Restart the pod from inside**, by having the agent exit. A container restart keeps the dead
  mount; only a new pod mounts the volume again.

## ADR-068: Build logs readable during a takeover, shipped beside the controller

- Status: accepted
- Date: 2026-10-02
- Refines: ADR-060 (cells, takeover)

### Context

While a cell is taken over -- about a minute in the lab -- its controller does not answer and its
`JENKINS_HOME` volume is moving to another machine. Nobody can read a build's log, including the
log of the build that was running, which is the one people want to read. CloudBees' active/active
HA does not have this gap: another replica serves the UI. netCI's README listed "history and
logs readable during a takeover" as not built.

Two ways to get the log out of the cell were tried on the lab.

**The Jenkins OpenTelemetry plugin** (3.1603.ve3fa_cc8a_b_f5e) can store Pipeline logs in Loki
through an OpenTelemetry collector, "mirrored" so Jenkins keeps its own copy. Measured:
- every fresh build agent waited the plugin's 10 s pre-online timeout for its SDK to be
  configured over remoting, also when the plugin's exporters were `none`. Builds on warm
  sandboxes went from ~10 s to 19-27 s;
- the output of `sh` steps never reached Loki. Agents send their lines themselves, through the
  SDK whose configuration had just been cancelled at the timeout. Only the controller's own
  lines ("Running on ...") arrived;
- its SDK settings take effect at the controller's start only, not on a JCasC reload.

**A log shipper beside the controller.** Jenkins writes every build's log -- the agents' output
included -- to `jobs/.../builds/N/log` on the volume. Fluent Bit in the controller's pod reads
those files as they grow and pushes the lines to Loki.

### Decision

1. **netci-cell `logShipping`**: Fluent Bit (5.1.3, pinned by digest) as a regular container in
   the controller's pod.
   - Reads jobs, jobs in folders, multibranch branches and those in folders.
   - Strips Jenkins' console notes (`ESC[8mha:...ESC[0m`).
   - Loki labels `cell` and `job`; the build number is structured metadata, not a label (a
     label per build would make a stream per build).
   - Its offsets are in `JENKINS_HOME/.netci-logship.db`, on the volume: the next controller's
     shipper goes on from where this one stopped.
2. **At least once, never lost.** A power loss can take the volume's last write-back, which
   holds both the end of the log and the shipper's offset. Jenkins writes those lines again on
   resume, and the next shipper sends them again: a line of the last second before a crash can
   be in Loki twice. Exactly once would need an identity per line, which Jenkins' log does not
   have.
3. **Loki must take pushes of ~5.3 MB.** One push is one Fluent Bit chunk (~2 MB), larger as
   Loki's protobuf. Over Loki's default 4 MB gRPC limit a backlog -- a first install, or the
   lines kept while Loki was down -- was refused with 500 and retried forever. `INSTALL.md`
   lists the settings (`grpc_server_max_recv_msg_size` 16 MB, a burst to match).
4. **The OpenTelemetry plugin is not installed.** Reverted from the controller image. Rolling
   back showed a second problem, now fixed: the Jenkins image adds its plugins to `JENKINS_HOME`
   and never removes one, so the plugin stayed, configured, and agents still waited 10 s. The
   cell agent's guard now removes, under the Lease, the plugins the image does not carry
   (`pluginsFromImageOnly`, on by default; ADR-059's drift).

### Verified on the lab

`lab/spike/logs_during_takeover.py`: a 240 s build, its controller's machine powered off, and
Loki and Jenkins asked every second. Evidence: `lab/evidence/logs-during-takeover-*.json`.

Every run, in order; the failures are what changed the design.

| Crash at tick | Verdict | What it showed | Change |
|---|---|---|---|
| 40 | FAIL | During the ~65 s outage Loki returned ticks 1-41. Afterwards all 240; tick 41 twice (Jenkins' own log: once) | Point 2: a line at the crash may be there twice |
| 25 | PASS | Ticks 1-26 readable during the outage; all 240 afterwards, 26 twice | -- |
| 70 | FAIL | The machine lost also ran Loki: nothing readable during the outage; 5 lines arrived minutes after the build (Fluent Bit's backoff had grown while Loki was down) | Retries capped at 30 s; the test waits for Loki to catch up |
| 40 | FAIL | Loki on the lost machine again: nothing readable | Loki moved to its own failure domain (the host, in the lab) |
| 70 | PASS | Ticks 1-71 readable during the outage | -- |
| 100 | FAIL | The build failed: the new controller preempted the agent running it. Not a log fault | Headroom no longer counts a running build's room (RUNBOOK, `NetciCellWithoutHeadroom`) |
| **30, 70, 110** (Loki on the host) | **3/3 PASS** | Builds SUCCESS; Jenkins down 50-95 s after the power-off; throughout, Loki returned every tick up to the crash; all 240 in Loki as the build finished; 1-2 lines twice, at the crash | -- |

### Rejected

- **The OpenTelemetry plugin's log storage**: measured above.
- **A Jenkins log storage plugin that writes only elsewhere** (JEP-210, e.g. CloudWatch): the
  build log would leave the volume, and Pipeline's resume and Jenkins' own UI would then depend
  on that service being up. Shipping a copy changes nothing in Jenkins.
- **A read-only replica of `JENKINS_HOME`**: the volume is ReadWriteOnce and moving during the
  very window it would be needed.

### Consequences

- During a takeover, a build's log is read in Grafana (Explore, Loki, `{cell="...", job="..."}`,
  then `| build="N"`), not in Jenkins.
- Run history beyond the logs: runs submitted through netci-queue are in PostgreSQL and
  readable through its API; builds started inside Jenkins are visible as their logs' streams.
- One more container per cell (~30 MB). A shipper that fails costs the Loki copy, never the
  build: Jenkins' own log is untouched.

## ADR-069: Fencing with real BMCs, at scale, without costing a running build

- Status: accepted
- Date: 2026-10-09
- Refines: ADR-060 (cells, takeover)

### Context

Three things the lab could not show, because it has three machines, VMs, and a Redfish
emulator that answers at once and always succeeds:

1. **Real BMCs.** They are slow (0.5-3 s a request), busy at times (503), and late: PowerState
   still says On seconds after a ForceOff. iDRAC answers 409 "Server is already powered OFF" when
   the machine lost its power between a state read and the ForceOff (Red Hat bug 1873305), and
   refuses with 409 for a while after a power change (openshift/openstack-ironic#502). OpenBMC
   names its allowed reset types through `@Redfish.ActionInfo`, not inline.
2. **Scale.** An organisation runs tens to hundreds of cells, on as many machines.
3. **A full cluster.** A controller that needs room after a takeover preempts pods of lower
   priority -- by design, or a takeover could be stuck -- and in the lab one preempted the agent
   of the very build it was resuming.

### Decision

1. **A failed power-off is not the end of fencing.** `fence.EnsureOff` keeps asking for the
   state until its deadline and sends the ForceOff again every 2 s; only a refusal
   (400/401/403/404/405) ends it at once. As before, nothing counts as off until the BMC says
   Off. The timeouts are settings: `supervisor.power.queryTimeout` (3 s), `offTimeout` (60 s,
   as Ironic's `power_state_change_timeout`; it was 20 s), and Redfish's `requestTimeout` in
   `fence.json`. A longer `offTimeout` costs nothing when a BMC is quick.
2. **An observation costs the same for any number of cells.** The supervisor lists every
   cell's Lease and pod by name across namespaces, one request each, instead of one GET of each
   per cell. `netci_supervisor_observation_seconds` and `NetciSupervisorSlowObservations` watch it.
3. **Running builds carry a disruption budget.** netci-fabric labels a claimed sandbox
   `netci.io/busy`; the charts put a budget on that label and on the Kubernetes plugin's agents.
   kube-scheduler prefers victims whose budget preemption would not break, and still preempts
   when nothing else frees room. The budget is an integer `minAvailable` above any pod count:
   Kubernetes defines budgets on pods without a controller only in that form (with
   `maxUnavailable` the disruption controller warns "undefined behavior").

4. **A changed fence configuration is loaded and checked.** The supervisor reads it once; it
   now fingerprints its directory (a Secret volume's `..data` link) and starts again when it
   changes, so a rotated BMC password or a new machine goes through the start-up mapping check.

### Verified

| What | How | Result |
|---|---|---|
| BMC behaviour, unit | `internal/fence/bmc_quirks_test.go`: 409 already off, a late answer, 503, 409 while settling, a refusal, OpenBMC's ActionInfo | The old `EnsureOff` failed 4 of them; all pass |
| BMC behaviour, live | `lab/redfish.sh quirks` in front of the emulator: 1.5 s a request, first ForceOff 503, PowerState 8 s late; 3 hung machines (`chaos-poweroff-20261009T020225Z.json`) | 3/3 SUCCESS. Confirmed off 22 s after fencing began, past the old 20 s timeout: hence 60 s |
| Scale | `lab/scale/run.sh` on kwok (real kube-apiserver, etcd, scheduler, controller manager v1.36.1); the real lease code for each cell agent; the real supervisor | Before: an observation took 4.0 s at 100 cells and 11.9 s at 300, and fencing 6-8 s and 12-17 s. After: 0.05-0.1 s mean at 100-600 cells, fencing 2.6-3.0 s, every cell held again within 5.7 s; 0.16 cores and 53 MiB at 600 cells |
| Preemption | `lab/scale/preemption.py`, real kube-scheduler | Without the budget a running build was preempted 5/5; with it 0/5, the idle sandbox taken instead; with builds on every machine the controller was still placed 2/2 |
| The lab after the change | 3 power losses through the same slow BMC (`chaos-poweroff-20261009T040127Z.json`) | 3/3 SUCCESS, one netciOnce marker each. Observations 17 ms p50, 84 ms p99 when quiet. Fencing took 5.5-6.9 s against 2.8-4.1 s with the emulator: the BMC's 1.5 s twice, once for the state read that prompts the fencing and once for the confirmation before the Lease is released -- the confirmation is kept |
| A rotated BMC password | The emulator's password changed, then the fence Secret | Before: the replicas kept the old configuration (found when re-pointing them). After: each stopped when its volume was updated, 18 s apart, and checked the mapping with the new password; both within 66 s of the Secret. A power loss then: fenced 3.0 s, SUCCESS (`chaos-poweroff-20261009T042237Z.json`) |
| Budget on the lab | a claimed sandbox | Labelled at once; its eviction refused by the budget, an idle one's allowed |
| The alert | `lab/spike/headroom_probe.py` | Metric 0 after 26 s, alert firing at the rule's 10 minutes, cleared 30 s after the build ended |

### What is still not verified

- **Real BMCs.** The behaviours above are reproduced from vendors' documented and reported
  behaviour; nothing has fenced a physical server. Before production, run the drill in
  `docs/INSTALL.md` against your BMCs.
- **Scale with real storage and Jenkins.** The scale test measures the control plane's share
  (detection, fencing, the Lease); a takeover in the lab spends ~22 s moving the volume and ~17 s
  starting Jenkins, out of ~51 s (chaos series 19). Neither depends on the number of cells, but
  a storage system moving many volumes at once is untested. *Since measured (ADR-070): eight
  Longhorn volumes moved as fast as one; the first pod deleted paid ~10 s, now avoided.*

### Rejected

- **Informers instead of lists.** A watch on a dead API server is a stale view that looks
  fresh; ADR-066's clients bound each request instead. Two lists a second cost the API server
  little: they are served from its watch cache (ConsistentListFromCache, on by default since
  1.31, GA in 1.34; KEP-2340). On older clusters each is a read of etcd.
- **Retrying ForceOff without a deadline.** A machine that never reports Off must reach a person.
- **Never preempting builds.** A takeover with no other room would wait for builds to finish,
  and every build of that cell with it.

### Consequences

- Draining a node waits for the builds on it (`kubectl drain --disable-eviction` overrides).
- The supervisor needs `list` on pods and leases cluster-wide (it already had it).

## ADR-070: A cell's pod goes after the storage has settled, and never while it renews

- Status: accepted
- Date: 2026-10-09
- Refines: ADR-060 (takeover), ADR-069

### Context

ADR-069 left one thing unverified: storage moving many volumes at once. `lab/spike/many_volumes_probe.py`
measures it on the lab: N cells of the real netci-cell chart (cell agent, guard, Lease,
`longhorn-sync` volume) on one machine, Jenkins replaced by a stand-in that appends an fsynced
sequence number five times a second, the machine powered off, each cell timed from the power loss,
and every volume read back.

Eight volumes moved as fast as one. What was slow was something else: **the cell whose pod went
first**. In every 8-cell run, the pod the supervisor deleted with the fencing (within 0.3 s of
marking the node not ready) was the last to attach, and the single cell of every 1-cell run paid the
same: about 10 s, 5 of 5. Longhorn's logs say why: a detach asked for while Longhorn is still taking
in that the node is gone either times out in its CSI plugin (10 s), or is accepted while Longhorn
tries to stop the engine on the machine that is off and gives up 10 s later. The other seven,
deleted ~2.5 s later (after the fencing's 2 s quiet check of their Leases), detached in 2 s.

Two more things came out on the way:

- **A safety defect.** When fencing stopped because another cell on the machine still renewed its
  Lease (a wrong node-to-machine mapping), the recovery of the fenced node deleted that cell's pod
  one second later anyway: it deleted every cell pod bound to a fenced node whose machine read off.
  `TestAnotherCellWhoseHolderStillRenewsIsLeftAlone` checked only the moment of fencing.
- **Kubernetes deletes the pods too.** The out-of-service taint is `NoExecute`: the taint-eviction
  controller deletes every pod that does not tolerate it at once, and the pod garbage collector
  force-deletes it on its next pass (k3s log: `taint_eviction.go "Deleting pod"`, then
  `gc_controller.go "PodGC is force deleting Pod"`, 0.4 s apart). In one 8-cell run it came first for
  3 pods.

And one waste: every cell on the lost machine got its own FenceNode; the first released every
Lease, the other seven asked the power controller again, in a row (8 cells: 2.5 s; with a BMC
answering in 1.5 s, 12 s).

### Decision

1. **The cells' pods on a fenced node are deleted by its recovery, `supervisor.storageSettle` (3 s)
   after the taint**, not with the fencing. The Leases are still released at fencing, so the
   replacements wait for nothing else. `0` deletes at the next observation.
2. **Only a pod that no longer renews is deleted**: its Lease was released for it, or has expired.
   One that still renews is left, and an alert names it. Each deletion carries the UID observed.
3. **The controller pod tolerates `node.kubernetes.io/out-of-service` for
   `outOfServiceTolerationSeconds` (30)**, so taint eviction does not delete it before the
   supervisor; past that Kubernetes still does, so without a supervisor a takeover is slower, never
   stuck.
4. **One fencing per machine per decision.**

### Verified

| What | Result |
|---|---|
| 8 volumes at once (probe, 2 runs, before) | Ready median 21.2 s, max 30.6 s; every write before the power loss present (last one 0.32-0.49 s before it), no gap, no torn line |
| The first pod's penalty (before) | 5 of 5 pods deleted within 0.3 s of NotReady: ~10 s more; 11 of 14 deleted ≥ 2.2 s after: none |
| After, 1 cell (7 runs) | Every detach in ~2 s but one (1 of 7 still timed out); attached on average 16.0 s after the power loss, 18.9 s before (3 runs) |
| After, 8 cells (3 runs, all four changes) | All 8 pods go together; attached median 15.1 s (14.5 before), Ready median 23.5 s (21.2): no gain with many cells -- they were already deleted late |
| Taint eviction (before the toleration) | 3 of 8 pods deleted by Kubernetes 1.1 s after the taint, two of their volumes late; none after |
| The renewing pod | Kept for 10 s of recovery in the test; failed at 1 s before |
| Real Jenkins, 6 takeovers (chaos 23, 24) | 6/6 SUCCESS, one netciOnce marker each. Fencing to the new pod holding the Lease: median 21.0 s (14.9-41.1), against 22.9 s (13.5-34.5) in the 10 runs before |

**What this does not show**: a shorter takeover end to end. The real-Jenkins difference is inside
the spread, which the lab's other variance dominates -- the controller manager's, scheduler's and
csi-attacher's leaders dying with the machine (the two slowest after, 34.0 and 41.1 s, lost all
three), and Longhorn's own attach: its CSI plugin waits for the engine's endpoint, which the engine
monitor fills on its first pass, every 5 s (`EnginePollInterval`), polled every 2 s; an attach takes
~4 s or ~10 s with nothing else different. The mechanism the settle removes is shown; its weight in
a whole takeover is not.

### Rejected

- **Waiting on a sign from Longhorn** that it has taken in the node loss: storage-specific, and its
  internal states are not an interface.
- **Deleting the pods at once and accepting the 10 s**: it is the commonest failure, one machine with
  one cell, that always paid it.

### Consequences

- With storage that does not need it, a takeover's pod deletion is 3 s later; set
  `supervisor.storageSettle: "0"`.
- A manually added out-of-service taint without a supervisor deletes a controller pod 30 s later
  than before.
- The Longhorn volumes in the lab: 26.4 GiB schedulable per disk; the probe uses 128 MiB volumes and
  refuses when they would not fit.

## ADR-071: What production asks beyond a power loss -- upgrades, failing dependencies, cluster DNS, start time, a fabric that heals

- Status: accepted
- Date: 2026-10-11
- Refines: ADR-060, ADR-064, ADR-069, ADR-070

### Context

The earlier series measured one fault at a time, a machine lost. An organisation also upgrades
netCI while people build, loses its database or its BMCs' network, runs whatever DNS its
distribution ships, and restarts machines in ways that leave clocks and containers in odd states.
This round put each of those on the lab, and every one found something.

### Decisions

1. **Upgrades are a planned restart, not a failure** -- nothing changed; it is now measured.
   `lab/spike/upgrade_probe.py` rolls every component with the lab's install script while a
   netciOnce build runs, a run is submitted every 15 s and both UIs are polled.
2. **A failing dependency is refused, never pretended** -- nothing changed; now measured
   (`lab/spike/dependency_probe.py`).
3. **Cluster DNS on one machine is reported** (`NetciClusterDNSOnOneMachine`,
   `supervisor.clusterDNSService`). k3s ships one CoreDNS; the lab lost it with a cell's machine,
   and for the whole takeover nothing resolved a name: the volume moved in 60 s instead of ~15.
4. **A takeover's agents reach the controller as soon as it is up**: the jenkins Service publishes
   the pod before it is ready (Jenkins answers 503 without a session until fully up; every client
   retries), and the startupProbe asks every second.
5. **A controller no longer unzips its plugins at every start**: the image unzips them when it is
   built; the cell agent links the pod's plugin root to them and gives identical archives in
   `JENKINS_HOME` the image's times (`cellagent.LinkPluginRoot`, `AlignPluginTimes`).
6. **A broken warm sandbox does not cost a build its wait.**
   - A warm sandbox whose pod is not ready is failed and replaced.
   - A warm claim not bound within 15 s fails its sandbox; a cold one keeps 2 minutes.
   - `GET /v1/claims/{id}` tells the claiming cell what became of its claim, and the plugin's
     launcher stops as soon as it was given up.
   - A claim bound 20 s with no agent online is given up by the plugin.
7. **The fabric times claims on the database's clock**, not its own: claimed_at is the database's,
   and a machine that resumed 197 s behind made the fabric give up every claim.
   netci-queue's accept-to-start metric is now the difference of the database's own two
   timestamps too (`runqueue.Store.AcceptToStart`).
8. **CI checks the charts and the alerts** on every push (`ci/charts.sh`,
   `deploy/helm/netci/ci/test-rules.sh`).

### Verified (lab)

| What | Result | Evidence |
|---|---|---|
| Rollback, then upgrade, of every component during a 240 s netciOnce build | Build SUCCESS, no tick lost, one marker; 9/9 and 6/6 runs exactly once, no intake failure; no supervisor action, no taint; UIs down 19-42 s | `upgrade-*.json` |
| PostgreSQL restarted (20 s) | netciOnce waited and ran, one marker; every accepted run once; refusals were 503 or no ready replica | `dependency-pg-restart-*.json` |
| PostgreSQL down 90 s | netciOnce failed closed ("cannot tell whether this block ran before ... not running it"), no marker | `dependency-pg-outage-*.json` |
| BMC unreachable when a machine dies | 60 s with nothing done to the cell and an alert; fenced 0.1 s after the BMC returned; build SUCCESS | `dependency-bmc-outage-*.json` |
| Cluster DNS on one machine | Metric 1 in 40 s, alert pending at 50 s, firing at 650 s, cleared 30 s after two replicas | `cluster-dns-alert-*.json` |
| Agent after the controller is up (takeovers) | 1.5-2.6 s (6 runs), against 6.25 s before (one run) | `takeover-timelines-20261011*.json` |
| Jenkins start, gate open to fully up | Planned restart 6.96 s against 9.3-9.4 s; takeovers 8.5-10.3 s against 9.2-13.0 s | the same |
| A build past two broken warm sandboxes (frozen) | SUCCESS in 52.8 s; before the changes, each could cost 300 s (one build waited 353 s) | `fabric-broken-sandbox-*.json` |
| Soak: 3 h of random faults back to back, seed 20261012 (power loss 15, power loss + supervisor leader 7, hung machine 6, JVM killed 4, PostgreSQL restarted 4, pod deleted 2) | 38/38 rounds PASS: every netciOnce build SUCCESS with no tick lost and one marker; after each round every Lease held by its own pod, no taint left, every node Ready, every volume healthy, a supervisor leader. Fenced 2.3-3.9 s after a power loss, 16.5-20.5 s after a hang (the Lease's 15 s), ~25 s when the supervisor's leader died too; build resumed 34-67 s (median 43) | `soak-20261011T125323Z.json` and each round's own |
| The image with both clock fixes on the lab (netci@sha256:134e2db8…, rolled by `lab/helm-install.sh`) | fabric builds SUCCESS; claim to bind 0.27 s mean; accept to start 22.7 s mean for 5 runs of queue-probe-short, whose quiet period is 20 s | `fabric-timeline-20261012T023941Z.json`, Prometheus |
| Every fix | Tests that failed before and pass now: Go (supervisor, fabric on PostgreSQL, cell agent) and the plugin (JenkinsRule) | the commits |

### Found on the way

- **The host's reboots** left the machines' clocks ~7 h ahead at start (a dual-boot host keeps local
  time in its RTC): k3s issued agent certificates valid from the future and every node went
  NotReady when NTP corrected the clock. Pods then stuck in CreateContainerError, and Longhorn's
  engine-image and manager pods unready, until deleted. A lab matter, recorded in the lab notes.
- **A paused machine resumes with a stale clock** that its NTP client does not notice until its
  next poll (up to 34 min): timesyncd reported -12 ms while the machine was 197 s behind. netCI
  must not depend on clocks agreeing across machines; the supervisor never did (it times a Lease on
  its own clock), the fabric did and does not any more.
- **The host's disk filled twice** (a controller image push writes a tar first) and QEMU paused
  every machine. Nothing was fenced: paused machines report power on, and the supervisor holds
  back when every node stops at once.
- A flaky cell agent test, real fsyncs against 50 ms (CI run 37883975435), rewritten.

### Not shown

- The fabric is not faster than the Kubernetes plugin per build: median 4.7 s against 4.1 s one at
  a time, 5.6 s against 4.8 s four at a time (`fabric-timeline-20261012T022726Z.json`, cells on two
  machines, images cached). Under bursts its tail was shorter: 1 of 17 builds over 15 s against 6 of
  16; a small sample. Those slow builds of both lost ~15 s either before the agent connected or
  before the first `sh` step ran: found since (ADR-072), the controller's jars fetched by each new agent.
- Measured with both controllers on one machine (lab-3, 84% memory, after takeovers), every build
  of both took ~9 s and some 20-48 s; latency figures need the cells apart (RUNBOOK
  NetciCellsShareAMachine).
- The soak never drew the BMC-unreachable fault (weight 1 of 12); that case is measured on its own
  (`dependency-bmc-outage-*.json`). A first soak stopped at round 2 on a defect of the harness, not of
  netCI (the timestamper's prefix hid the build's ticks from the probe): `soak-20261011T113218Z.json`.

### Rejected

- **Hot reload of the plugin root instead of a link**: the chart would have to know the image; a
  link made at start works with images that do not have it.
- **A fixed grace for not-ready warm pods**: a sandbox is ready once the fabric answered it and stays
  so; not ready means its container is gone.
- **Lowering connectTimeoutSeconds**: a cold claim may take minutes to start its pod; the claim's
  state says when waiting is pointless.

## ADR-072: Sandboxes start with the controller's jars in their agent's cache

- Status: accepted
- Date: 2026-10-12
- Refines: ADR-064 (agent fabric), ADR-071

### Context

ADR-071 left a question open: some builds, on the fabric and on the Kubernetes plugin alike, lost
~15 s before their agent was online or before their first `sh` step printed anything, and nobody
could say why, because the agents' pods were gone by the time anyone looked.

`lab/spike/agent_delay_probe.py` follows every agent pod's log from the moment it exists, the
controller's log, and when the controller first calls each agent online. It found:

- An agent that had said "Connected" was online on the controller 15-16 s later in some builds of
  both (2 of 16 fabric, 2 of 16 Kubernetes plugin).
- A thread dump of the controller taken while one was stuck: `SlaveComputer.setChannel` waited on
  a call to the agent, and another thread was writing a jar to the agent (`JarLoaderImpl.writeJarTo`),
  blocked in `PipeWindow.get`, i.e. waiting for the agent to take more of it.
- A fresh agent fetches ~19 MB of the controller's jars over its channel: 15 jars, jenkins-core
  alone 11.9 MB, the rest core libraries and the plugins of the steps (durable-task for `sh`). Its
  jar cache is empty in every new pod, so every sandbox fetches them again; four at once share the
  controller's channels, and the window fills.
- The `sh` step (durable-task) polls for output (`USE_WATCHING` is off by default), and its first
  check waits for those classes too.

### Decision

1. **An image holds the agent's jar cache for one controller image** (`jenkins/Dockerfile.agent-jars`
   from `jenkins/agent-jars/agent-jars.txt`): the listed jars, laid out under the names remoting asks
   its cache for, computed by remoting's own `Checksum` and `FileSystemJarCache` against the
   controller's remoting. A line that does not match exactly one file stops the build.
2. **A fabric pool's `jarCache`** names that image, pinned by digest. It is mounted read-only, and
   `netci-sandbox` copies it into the agent's own cache before the sandbox is warm, so no claim waits
   for it. Only entries named as remoting names them are copied, never a link, never over a file;
   a missing cache only means the agent fetches what it needs, as before.
3. Trust: the agent loads a cached jar by its name and never checks its content
   (`FileSystemJarCache.lookInCache`). The cache therefore comes only from an image the platform
   built, pinned by digest and read-only. Each sandbox gets its own copy: a build that alters it
   alters only its own agent, which it already controls. Nothing a sandbox wrote is ever given to
   another.

### Verified (lab)

| What | Result | Evidence |
|---|---|---|
| Where the time went | As above: 15-16 s from "Connected" to online, the controller writing a jar to the agent meanwhile | `agent-delay-20261012T030531Z.json` and its directory (logs, thread dump) |
| Warm sandboxes seeded by hand against none, alternating, 16 builds each | 3.38 s median, 4.04 s at worst, none stalled; against 4.66 s, 18.59 s, 2 stalled 15 s | `agent-jar-cache-ab-20261012.json` |
| The image's cache against what a real agent fetched | The same 15 names and contents | the build, compared by hand |
| On the lab with the feature (netci@sha256:ab2d7a77…, jenkins-agent-jars@sha256:c4685316…) | Seeded 15 jars, 19.5 MB, in 9 ms, in user-namespaced pods. Fabric: 3.88 s one at a time (2.31-4.79), 4.82 s four at a time (3.63-5.97), no build over 6 s of 23; before, 4.73 s and 5.6 s with one at 20.5 s. The Kubernetes plugin in the same run: 3.17 s (one at 21.1 s) and 6.17 s | `fabric-timeline-20261012T034419Z.json` |
| Tests | Go: the pool's image mounted read-only and named to the bootstrap, refused by tag; seeded before warm, odd names, links and existing files left alone; a missing cache still runs. Each fails without its code | the commit |

### Not shown

- The list is what one agent fetched for a Pipeline with `sh` steps. Steps of other plugins (git,
  credentials, artifacts) fetch their own jars, which are not in it: they are fetched as before.
- The Kata pool has no jar cache: image volumes were not tried with Kata.
- One at a time, the fabric is still ~0.7 s behind the Kubernetes plugin's median, which itself
  moved between runs (4.06 s, 3.17 s); small samples.
- The probes authenticate with a password, which Jenkins checks with bcrypt on every request: the
  controller does that work during the measurements, for both kinds of agent alike.
- The Kubernetes plugin could use the same image as a volume in its pod templates; not done.

### Rejected

- **A cache shared between sandboxes, filled by them**: a build could put a jar under another's
  name and have the next agents load it.
- **Mounting the image as the agent's cache directly**: the agent writes the jars it misses into
  its cache, and a read-only one would fail them.
- **Every jar of the controller** (~200 MB): ten times the copy for jars no agent was seen fetching.
- **Turning durable-task's watching on**: it changes how every step's output reaches Jenkins, for a
  wait whose cause was the jars, not the polling.

## ADR-074: Active-active Jenkins on one JENKINS_HOME -- what breaks, and the plan

- Status: superseded in its storage decision by ADR-076 (the mentor's answers: Pipeline only, inbound
  agents, storage mostly local-path). Phases 0 and 1 stand as evidence.
- Date: 2026-10-13
- Refines: ADR-060 (one writer per cell), ADR-071 (UI unavailable during a takeover)

### Context

The project's mentor asks that Jenkins' UI and API never go down, with as few of CloudBees'
active/active limits as possible, on Viettel's infrastructure. What is known of it:
- controllers on Kubernetes;
- agents on one large OpenStack VM with many executors and labels;
- builds never on a controller;
- 3 VMs or nodes on separate hosts may be had;
- our own load balancer, plugin and PostgreSQL are allowed.

Still open: whether jobs are Pipeline or Freestyle, whether agents connect over SSH or inbound,
and what shared file system there is. The lab is to cover each case.
(Answered 2026-10-13: Pipeline only; inbound JNLP agents; mostly local-path, Longhorn present. See ADR-076.)

CloudBees runs several replicas of a controller on one `JENKINS_HOME` on a shared file system,
keeps live state in Hazelcast, needs sticky sessions, and has a replica adopt a dead one's
builds. Its documented limits:
- non-Pipeline builds are aborted when their replica ends;
- one executor per agent;
- `build` and `lock` restricted;
- REST answers from one replica.

What open-source Jenkins does with a shared home and nothing coordinating it had to be seen
first.

### Phase 0: two controllers, one home, no coordination

`lab/aa/phase0.yaml` and `lab/aa/phase0_probe.py`: namespace `aa-lab`, a Longhorn RWX volume
(NFS), replicas r1 and r2 of the lab's controller image on different machines, JCasC-defined jobs.

| # | Experiment | Result | Evidence |
|---|---|---|---|
| F1 | Both started together, plugins unzipped into `JENKINS_HOME/plugins` (Jenkins' default) | Each deleted the other's unzipped files: r1 55 SEVERE, 13 plugins failed; r2 failed to load configuration-as-code, so JCasC never ran and neither had a job | `aa-phase0-startup-20261013T020719Z.json` |
| E1 | Both started, plugins and WAR unzipped on each pod's own disk | Both up in 51 s, both see the jobs. Nothing stops a second controller on a home | `aa-phase0-20261013T021729Z.json` |
| E2 | A build run on r1 | r2 does not know it (404) although it is on disk; r2's job shows no build | same |
| E3 | The same job triggered on both at once | Numbers 2 and 3, no collision in two runs; each replica sees only its own builds | same |
| E4 | A description set on r1, then the job disabled on r2 | Lost update: `config.xml` holds r2's save with an empty description; r1 still shows its own in memory | same |
| E5 | r1 killed in a build's 8th second | r2 does not see the build. After r2 restarted, it loaded the build from disk and resumed it ("Resuming build ... after Jenkins restart"). The step's process had run on r1's built-in node and died with it, so the build waits for it | same |

What it shows:
- **Jenkins keeps everything in one JVM's memory** (jobs, builds, numbers, configuration) and
  treats disk as its own. A shared home without coordination gives every kind of divergence:
  invisible builds, lost updates, and nothing stopping two writers.
- **Adoption exists.** A controller that loads a running Pipeline from disk resumes it. This is the
  mechanism to use. It runs only at start, it needs the build's previous owner provably stopped
  (or the build runs twice), and it needs the step's process on an agent the adopter can reach.

### Phase 1: the shared home's cost, and what it does when its machine dies

`lab/aa/phase1_storage_probe.py`: on the replica that is *not* on the share-manager's machine, 4 KiB
writes with `fsync` every 0.2 s, on the home and on the pod's own disk; then a writer loop on the home
and the replica's `/login` asked every 0.5 s while the machine running Longhorn's share-manager (the
NFS server of the RWX volume) loses power (`virsh destroy`). One run per setting.

| Setting | Write latency, home vs local (median / p99 ms) | Writes stalled after power loss | `/login` failures | Evidence |
|---|---|---|---|---|
| Longhorn default | 8.0 / 15.0 vs 4.5 / 15.1 | **133.7 s** | 0 of 496 | `aa-phase1-storage-20261013T023735Z.json` |
| `rwx-volume-fast-failover=true` | 8.9 / 17.1 vs 4.9 / 18.4 | **61.0 s** | 0 of 497 | `aa-phase1-storage-20261013T024510Z.json` |

What it shows:
- **Steady state is not the problem.** A synced write to the home costs about twice the local disk,
  a few milliseconds; Jenkins' own saves are not on a hot path where that matters.
- **The failover is.** Every writer on the home blocks until the share-manager is up on another
  machine: about a minute even with fast failover. `/login` kept answering only because it never
  touches the home. Any request that reads or saves a build, a job or a log would hang as long, on
  *every* replica at once, since they share the one NFS server. That is an outage of the UI in all
  but name. So Longhorn RWX alone does not meet "the UI never goes down".
- So point 8 below: the controllers must not block on the home. What the home holds stays there, but
  a replica must keep serving while the home is away, and the storage choice must be measured the
  same way for each option the mentor's site may have (an HA NAS over NFS, CephFS with a standby
  MDS), with the probe above.
- Kept: `rwx-volume-fast-failover=true` on the lab's Longhorn. It halves the stall and costs nothing
  measured.

### Decision: the design the next phases build

1. **Nothing that is not state on the shared home.** WAR, unzipped plugins and caches are on each
   replica's own disk (F1).
2. **PostgreSQL coordinates, as Hazelcast does for CloudBees** (netCI already runs one):
   - replica membership with leases and epochs (as cells hold theirs);
   - the owner of each running build;
   - build numbers allocated atomically per job;
   - a per-item lock for configuration writes;
   - change notifications (`LISTEN`/`NOTIFY`), so replicas reload an item another replica changed.
3. **One owner per build.** netci-queue dispatches each run to one replica. Requests for a build's
   pages go to its owner (the load balancer asks), so its live state is never read from a replica
   that does not hold it.
4. **Adoption after fencing.** When a replica stops renewing, its machine or pod is fenced first
   (the supervisor and fence code, ADR-060 and ADR-069). Only then does a surviving replica take
   its builds and resume them from the home, without restarting itself.
5. **Agents outside the replicas.** The large VM's executors become single-executor agents with the
   same labels: same capacity, one connection per build. Each agent reconnects to whichever replica
   owns its build; SSH and inbound agents are both to be covered.
6. **Our own load balancer** (HAProxy): sticky sessions, health checks, and build URLs routed to
   their owner.
7. **Every CloudBees limit is to be removed or measured**:
   - one executor per agent: point 5, with no loss of capacity;
   - `build` and `lock`: tracked in PostgreSQL;
   - REST answers from one replica: merged by the proxy;
   - Freestyle: cannot resume in Jenkins at all, so it is re-run from the start and that is said
     plainly.

8. **Requests do not wait on the shared home** (phase 1). Reads a replica can answer from memory
   are answered from memory; a request that needs the home while it is away gets a bounded wait and
   an honest error, never a hang; the load balancer's health check tells "this replica is stuck on
   storage" from "this replica is down", so a storage stall does not look like every replica dying.
   Which store the mentor's site has decides how long the home is away; the probe measures it.

### Phases

| Phase | What | Pass |
|---|---|---|
| 0 | Uncoordinated replicas | Done: the table above |
| 1 | The shared file system's latency, and its failover when the machine holding it dies (Longhorn RWX now; NFS and CephFS options) | Done for Longhorn RWX: it does not, alone (61 s stall); NFS and CephFS still to measure |
| 2 | Load balancer + replicas serving the UI; a machine loses power | UI and API probed every second, none failing (or within the mentor's bound) |
| 3 | PostgreSQL coordination: ownership, numbers, configuration; adoption without restart | A running build on a dead replica carries on, every step once |
| 4 | Agents (single-executor, SSH and inbound), cron and polling once per cluster, `build`, `lock` | Each correct with a replica lost |
| 5 | Chaos and soak, as for the cells | Evidence in `lab/evidence/` |

### Rejected

- **Two `JENKINS_HOME` copies synchronised file by file** (rsync, lsyncd, DRBD dual-primary). Two
  JVMs writing the same XML files diverge, as E4 shows on one home. Copying the files does not order
  the writes.
- **Restarting the surviving replica to adopt** (what E5 did). Its users and its own builds would see
  it go down, which is what active-active is for.

## ADR-076: Active-active on replicated homes, not on one shared home

- Status: accepted (design; phases 2-6 below build and test it -- none of it is established yet)
- Date: 2026-10-13
- Supersedes: ADR-074's storage decision ("one JENKINS_HOME on a shared file system").
  ADR-074's phase 0 and phase 1 results stand and are the reason for this one.

### Context

The mentor answered the three open questions (2026-10-13):
- **Jobs are Pipeline only.** No Freestyle; every build is a durable Pipeline that can resume.
- **Agents are inbound (JNLP).** They connect to the controller, not the controller to them.
- **Storage is mostly `local-path`; Longhorn exists.** A node-local disk is the normal case;
  a ReadWriteMany volume would have to come from Longhorn's share-manager.

What ADR-074 measured on a shared home (Longhorn RWX):
- **Phase 0:** nothing coordinates two Jenkins on one home (invisible builds, lost config updates).
- **Phase 1:** when the machine running the share-manager (the volume's NFS server) loses power,
  every write to the home blocks for **61 s even with fast failover** (134 s by default). That
  blocks *every* replica at once, because they share the one NFS server.

So with a shared home, one machine's death becomes a disk stall on all replicas, and the store
it needs is not the one the site mostly uses.

### Decision

**Each replica has its own `JENKINS_HOME` on its own node's disk (`local-path`). The homes are
kept the same by replicating Jenkins' objects through PostgreSQL, not by copying files.**

A machine's death then takes one replica and its disk; the other replicas' disks are untouched
and they keep answering. This is also the problem as first posed: two homes that are the same,
not necessarily one home.

ADR-074 rejected "two homes synchronised file by file" (rsync, lsyncd, DRBD), because copying
files does not order two writers' saves. This is not that. PostgreSQL orders every change:
- **Configuration** (jobs, folders, views): an item's `config.xml` is written to PostgreSQL first,
  under a per-item version (compare-and-set). The losing writer of a concurrent edit is refused
  and reloads; it is never silently overwritten (ADR-074 E4 was that silent overwrite). Other
  replicas reload the item on `LISTEN/NOTIFY`. Our own plugin does this (allowed by the mentor).
- **Global configuration and secrets** come from JCasC plus one `secrets/` set given to every
  replica. They must be byte-identical: an inbound agent's secret is an HMAC under them.
- **Build numbers** come from a PostgreSQL sequence per job, so two replicas never take one number.
- **Builds have one owner** (the replica netci-queue dispatched to). A finished build's record
  (`build.xml`, log, flow nodes) is published to PostgreSQL; the others materialise it, so every
  replica shows the full history.
- **A running build** is replicated as it runs (Pipeline writes `program.dat` and flow nodes at
  each step). When its owner stops renewing its lease, the owner is fenced first (supervisor and
  fence, ADR-060/069), then a survivor materialises the latest state and resumes the build.
  Pipeline only (the mentor's answer) is what makes this possible at all.
- **Inbound agents use WebSocket**: the same agent jar with `-webSocket`, through the load
  balancer. Checked in the running Jenkins 2.555.3 core: the agent sends `Node-Name` and
  `Secret-Key` on the `/wsagents/` upgrade, so HAProxy routes each agent to the replica that owns
  it. Over TCP JNLP (port 50000) the name is inside the protocol and the balancer cannot route it;
  that mode is to be measured as the alternative, not assumed.
  The `Secret-Key` header must never be logged, and must travel over TLS outside the lab.
- **The UI and API**: two HAProxy instances behind a VIP (kube-vip in the lab; on OpenStack an
  Octavia balancer or a VIP on allowed-address-pairs -- a question for the site). Sticky
  sessions; any replica answers lists and finished builds; a running build's pages go to its owner.

### What it costs, said plainly

- A finished build appears on the other replicas after it is published: a lag to measure, not zero.
- A running build on a dead replica pauses from the death until fencing and adoption complete.
  The UI stays up meanwhile; that build's own page says it is being adopted.
- Our plugin hooks Jenkins' save and build lifecycle. Jenkins upgrades must be tested against it,
  as with any plugin that does this (CloudBees' does too).

### Phase 2: the UI and API through an HA balancer (done)

`lab/aa/phase2.yaml`, `lab/aa/phase2_probe.py`: two replicas on `local-path`, an HAProxy per machine,
the VIP 192.168.122.240 held by kube-vip. Three probes every 0.5 s through the VIP, 2 s timeout:
`/login`, the API with basic auth, and a session signed in through the form. One power loss
(`virsh destroy`) per scenario, observed for 150 s. **One sample each**: these are what happened,
not a distribution.

| Run | Scenario | Failures (login / API / session) | Longest outage |
|---|---|---|---|
| v1: `externalTrafficPolicy: Cluster`, 2 HAProxy | a replica's machine | 5 / 5 / 4 | 4-6 s |
| v1 | the VIP's machine | 5 / 5 / 5 | 8-10 s |
| v2: `Local` | a replica's machine | 1 / 0 / 1 | 2 s |
| v2 | the VIP's machine | 7 / 7 / 7 | 14 s |
| v2 | the VIP's and the session's replica's machine | 15 / 16 / 15 | 30-32 s |
| **v3: `Local`, an HAProxy per machine, kube-vip on its own API server** | a replica's machine | 1 / 0 / 1 | **2 s** |
| v3 | the VIP's machine (with a replica on it) | 3 / 3 / 3 | **6 s** |
| v3 | the VIP's and the session's replica's machine | 1 / 2 / 1 | **2-4 s** |
| **netci15** (users replicated, shared sign-in, ADR-077) | a replica's machine, the session's replica on it | 0 / 1 / 1 | **0-2 s; the session stayed signed in as admin on r1** |
| netci15 | the VIP's machine | 3 / 3 / 3 | 6 s; session kept |

What each change fixed, from the evidence:
- `Cluster` let kube-proxy on the VIP's machine send requests to a balancer on a machine that had
  just died, until its endpoint was removed. `Local` keeps traffic on the VIP's machine.
- kube-vip renewed its lease through the `kubernetes` Service, which still listed the dead
  machine's API server: each call landing there hung 10 s (`error retrieving resource lock`), so the
  VIP took 14-30 s to move instead of the lease's 3 s. Pointing kube-vip at its own machine's API
  server (every lab machine is a control-plane node) removed that.
- An HAProxy on every machine leaves every surviving machine able to take the VIP under `Local`.

What it does not fix yet, said plainly:
- ~~A signed-in session is lost when its replica dies~~: fixed by ADR-077 (users replicated, a shared
  signed cookie); measured with netci15 above (`aa-phase2-20261013T151003Z.json`).
- The few seconds that remain are the 2 s client timeout of requests already sent to the dead
  machine, plus the 3 s VIP lease. Shorter timings are possible and were not tried.
- On OpenStack the VIP needs the port's allowed-address-pairs (or an Octavia balancer instead):
  a question for the site.

### Phase 3: the homes kept the same (done)

`lab/aa/phase3.yaml`, `lab/aa/phase3_probe.py`: netci-homesync (two, on the lab's PostgreSQL) and the
plugin on both replicas (controller `2.555.3-netci15`). Each client pinned to one replica through the
VIP. Evidence `aa-phase3-20261013T150720Z.json`.

| # | Experiment | Result |
|---|---|---|
| T1 | A job created on r0: until r1 has it (10 samples) | median **231 ms**, max 545 ms, none missing |
| T2 | ADR-074 E4 again: description on r0, then disabled on r1 | **No lost update**: both replicas have both changes |
| T3 | 40 description edits at once, half on each replica | 25 accepted, 15 refused with "being changed on replica ..." (409), none lost silently; both end on the same accepted edit |
| T4 | A job deleted on r1 | gone from r0 in 0.16 s |
| T5 | netci-homesync scaled to 0 | edits refused (503, with the reason), reads served (200); after it returned, an edit in 0.3 s and on r1 in 1.4 s |
| T6 | r1 down while r0 creates 3 jobs and edits one | r1 back in 20 s with every change |
| T7 | An API token minted on r0 (users replicated, ADR-077) | accepted on r1 after 0.05 s; revoked on r0, refused on r1 after 0.8 s |
| T8 | Signed in through the form on r0, r0's pod killed | 59 of 60 polls answered by r1, **all signed in as admin**, no failure |

The first run (`aa-phase3-20261013T101804Z.json`) found a real gap: a job created from XML
(POST createItem with a config.xml) is written and loaded without a save, so it was never published
-- ten jobs on r0, none on r1. Fixed (f52639b); the unit test now goes through that API. On the next
start, r0's bootstrap published those jobs and both replicas listed the same 18.

### Phase 4: builds with two replicas (done; ADR-078)

`lab/aa/phase4_probe.py` on netci17. Which replica ran a build is the store's record of who
published it. Evidence `aa-phase4-20261013T161858Z.json`.

| # | Experiment | Result |
|---|---|---|
| P1 | 3 builds on the owner r0 | #1, #2, #3, all by jenkins-0; listed on r1 after 104-229 ms, console included |
| P2 | "Build now" on r1 for r0's job, with r1's crumb | 201; built on jenkins-0 as #4, and not also on r1 |
| P3 | A job triggered every minute, on both replicas, for 3 minutes | 3 builds, #1-#3, one owner built them all, both replicas list the same |
| P4 | r0's pod killed, "Build now" on r1 | refused with the owner's name (409) until r0's lease expired; at 13.8 s r1 built #5 -- numbers continued |

The first two runs found what unit tests could not: HAProxy removed the routing cookie the replica
set (`preserve` fixes it), and a redirected POST was refused by the owner because its CSRF crumb
belonged to the first replica's session -- so build requests are now made on the owner by the first
replica (ADR-078 point 4). Both runs are kept as evidence.

### Phases

| Phase | What | Pass |
|---|---|---|
| 2 | HAProxy + VIP before two replicas on `local-path`; the VIP's machine, a replica's machine, both, lose power | Done: 2-6 s, one sample per scenario (above); sessions are lost with their replica |
| 3 | Configuration (and users) through PostgreSQL: concurrent edits on two replicas | Done (above) |
| 4 | Build numbers and finished builds replicated | Done (above) |
| 5 | Running builds replicated; adoption after fencing; WebSocket agents routed by name | Done (ADR-079): pod killed 4.3 s, machine lost 49.5 s, SUCCESS, every tick once |
| 6 | Chaos and soak | Evidence in `lab/evidence/` |

### Rejected

- **One shared home on Longhorn RWX** (ADR-074's plan): phase 1's 61 s stall hits all replicas at
  once, and RWX is not what the site mostly runs. Kept as the measured alternative for a site with
  an HA NAS, where the same probe would decide.
- **Copying homes file by file**: see ADR-074.
- **A home per replica on Longhorn RWO, moved on failure**: that is the cell takeover of ADR-060;
  it keeps builds but not the UI, which is what active-active is for.

## ADR-077: A sign-in that survives its replica

- Status: accepted; measured in the lab (ADR-076 phase 3, T7 and T8: a token minted on r0 accepted by r1 in
  0.05 s; a person signed in on r0 still signed in on r1 after r0's pod was killed, 59 of 60 polls)
- Date: 2026-10-13
- Refines: ADR-076 (replicated homes)

### Context

ADR-076 phase 2 measured the UI through the balancer staying up when a machine dies, but the
person signed in on the dead replica was **anonymous** on the other one: Jenkins keeps a session in
one JVM's memory. The balancer's sticky cookie only names a replica; it cannot carry a session that
replica no longer has.

Two more things were missing for the replicas to look like one Jenkins to their users:
- **Users were not replicated.** An API token minted on r0 was refused by r1, and a script going
  through the VIP reaches either at random. A password changed on one was unknown to the other.
- Each replica had its own **user seed** (Jenkins' per-user secret behind "Invalidate sessions"),
  so nothing could be bound to it on both.

### Decision

1. **Users are replicated through netci-homesync** like items, under `@users/<id>`: a user's
   `config.xml` (password hash, API token hashes, seed, properties) is published on save and
   applied on the other replicas. Posts to a user's pages take that user's lock, so two replicas
   minting tokens for one user at once cannot drop one of them.
2. **`NETCI_SESSION`, a cookie every replica can verify.** A signed-in browser session (one with an
   HTTP session -- API-token calls have none and get no cookie) is given a cookie holding the user's
   id, issue and expiry times, and the SHA-256 of the user's seed, signed with HMAC-SHA256 under a key
   every replica is given (`NETCI_SESSION_KEY_FILE`, at least 32 bytes, from a Secret).
   - A replica with no session for the request but a valid cookie signs the user in again, with
     **the authorities its security realm gives that user now** (`User.impersonate2()`): the cookie
     names a user and carries no authority.
   - Refused: a bad signature, another key, expired, issued in the future, a user that no longer
     exists, a seed that changed (Jenkins' "Invalidate sessions", replicated with the user). A
     cookie that does not verify is cleared rather than re-checked on every request.
   - **Idle lifetime 1 hour by default** (`NETCI_SESSION_TTL_SECONDS`), renewed once half of it has
     passed while the person is active: it ends like a session, not like a days-long "remember me".
   - Sign-out clears it on the `/logout` request itself, before Jenkins handles it, and never
     re-issues it there. (The first version cleared it from Jenkins' logout listener; the unit test
     showed the next page signed the person straight back in.)
   - `HttpOnly`, `SameSite=Lax`, `Secure` under HTTPS. Off unless the key is configured.

### What it costs, said plainly

- A **POST** sent with a page loaded from the dead replica carries a CSRF crumb bound to the old
  session and is refused once (403); reloading the page fixes it. GETs are transparent.
- The key is a shared secret: whoever holds it can sign a session for any user. It lives in a
  Kubernetes Secret mounted only into the controllers, as `secrets/` already does.
- A realm that cannot look a user up by name (some SSO set-ups) cannot restore from the cookie;
  the person is sent to sign in again, as before.

### Rejected

- **Replicating the servlet session** (Jetty's session data stores): Jenkins' sessions hold
  objects that are not designed to be serialised across JVMs, and it would put every session write
  on the network path of every request.
- **Forcing Jenkins' own "remember me"**: lasts days and is off unless the person ticks the box;
  the property needed is "survives a failover", not "survives closing the browser".
- **Sticky sessions alone**: they pin a person to a replica, which is exactly what fails here.

## ADR-078: One owner per job; build numbers reserved before a build starts

- Status: accepted for the numbering (store and plugin built and tested); finished-build replication,
  routing and adoption are still design
- Date: 2026-10-13
- Refines: ADR-076

### Context

After ADR-076 phase 3 the replicas hold the same jobs and users, but each still builds on its own:
- each home keeps its own `nextBuildNumber`, so r0 and r1 would both run build #5 of a job;
- a finished build exists only in the home that ran it, so the other replica's job page lacks it;
- a running build's live state (its execution, its console) is in one JVM.

The mentor's constraints that matter here: Pipeline only (durable), inbound agents, no builds on a
controller.

### Decision

1. **One owner per job at a time**, holding a lease in netci-homesync (PostgreSQL), with an epoch
   that grows on every change of hands (the fencing token). Only the owner starts builds of the job.
   A job is claimed by the replica that is asked to build it and has no live owner; the owner
   renews while it is alive. Numbers then stay what people expect: 1, 2, 3 per job.
2. **A number is reserved before the build takes it.** Jenkins assigns a build its number when the
   executor creates the run, after any plugin could intervene, so the reservation happens one step
   earlier, when the queue decides the item may run (`QueueTaskDispatcher.canRun`): the owner
   records `(job, next number)` in the store under its lease epoch, and only then lets the item
   start. Not `canTake`: a Pipeline run is a flyweight task, started without any dispatcher's
   `canTake` being asked -- the unit test showed no number reserved through it. While a reservation is not yet consumed by a started run, other items of that job
   wait ("waiting for a build number"), so the run takes exactly the reserved number. A replica
   that lost its lease cannot reserve, so it cannot start a build -- a partitioned old owner stops
   by itself. **Every number is used once**, including across a change of owner, which starts from
   the store's highest reservation.
3. **A finished build is published**: its directory (`build.xml`, the log, the flow nodes) to the
   store; the other replicas write it into their home and call
   `LazyBuildMixIn._getRuns().recognizeNumber(n)` (public in 2.555), so every replica lists every
   build without reloading the job.
4. **A running build's pages go to its owner**: a replica that does not have the build answers with
   a redirect that sets the balancer's cookie to the owner (the sign-in follows, ADR-077; HAProxy's
   cookie needs `preserve`, or it removes the cookie the replica sets). **A "Build now" on a
   non-owner is made on the owner by that replica**, for the same person: redirecting the browser's
   POST failed in the lab, because its CSRF crumb is bound to the first replica's session and the
   owner answered 403. The first replica has checked the crumb; it calls the owner with the same
   credential (an API token is valid everywhere, users being replicated) or a shared session it
   signs for that person, takes the owner's own crumb, and relays the answer, with the owner's
   Location rewritten to a path on the entry point.
5. Logs of finished builds are kept in PostgreSQL in the lab, with a size limit that is enforced and
   said; a site would put them in an object store (the corp lab has one).

### Phase 5, built on this

When a job's owner stops renewing, its machine or pod is fenced first (supervisor and fence,
ADR-060/069); then a survivor takes the lease with a new epoch, writes the running build's
replicated state into its home and resumes it. The agent running the step reconnects through the
VIP and is routed by `Node-Name` to the new owner. That is where "a build is not lost when a
machine dies" is decided, and it is measured there, not claimed here.

### Phase 5 by hand: a running build continued by the surviving replica (done)

`lab/aa/phase5_manual.py`, before any code automates it. `long` runs 90 one-second ticks in an `sh`
step on agent-0 (an inbound agent, two executors, WebSocket through the VIP), started on r0. At
tick 15 r0's copy of the running build is copied into r1's home; r0 is killed and its machine
cordoned (dead, fenced). Evidence `aa-phase5-manual-20261013T164325Z.json`:

- agent-0 reconnected through the VIP; r0 being down, the balancer gave it to r1 **after 4.5 s**;
- on r1, the build's number made known (`recognizeNumber`) and the run loaded: it resumed itself
  ("Resuming build ... Waiting for reconnection of agent-0 ... Ready to run") -- **r1 was not
  restarted**;
- it **finished SUCCESS** on r1: **90 ticks, none missing, none twice**, all on agent-0. The `sh`
  process had kept running on the agent; r1 read its output on from the offset the copy held.

What the first try taught (`aa-phase5-manual-20261013T164147Z.json`): loading the run is the whole
adoption. Calling `onResume()` on its steps as well resumed them a second time ("Already
resumed"), the `sh` step interrupted its process, and the build failed.

What automation must add, from this: the running build replicated while it runs (here: one copy by
hand); adoption only once the old owner is fenced (here: killed and cordoned by hand); and a
returning replica must not resume, from its stale copy, a build that was adopted elsewhere (here:
its copy was replaced by hand before it came back).

### Phase 5 automated (ADR-079)

What was done by hand above is done by the replicas: a killed pod's build adopted in 4.3 s, a dead
machine's in 49.5 s, both SUCCESS with every tick once, neither resumed again by the replica that
came back (`aa-phase5-20261013T192242Z.json`, ADR-079 has the table).

### Rejected

- **Numbers striped by replica** (r0 odd, r1 even): no coordination needed, but numbers that jump
  and change meaning when a replica is added.
- **Reserving the number after the build has it** (`RunListener.onInitialize`): too late -- if the
  record failed, a new owner could take the same number.
- **Any replica builds any job, numbers from a database sequence**: Jenkins gives a plugin no hook
  between "this item starts" and "this run has number N" other than the queue's dispatch step,
  which this design uses; without an owner, two replicas' dispatches would still race for one
  job's next number.

## ADR-079: Adopting a running build when its replica dies

- Status: accepted; measured in the lab (below)
- Date: 2026-10-13
- Refines: ADR-078

### Context

By hand (ADR-078 phase 5): a Pipeline running on r0, copied into r1's home, r0 killed: loading the
run on r1 resumed it, the agent reconnected to r1 through the VIP, and the build finished with every
line once. Three things were done by hand that a replica must do itself:
1. the copy of the running build;
2. the decision that r0 is dead, so that continuing its build cannot run it twice;
3. on r0's return, not resuming its stale copy of a build r1 has taken.

### Decision

1. **A running build is replicated while it runs**: its directory (program, flow nodes, log) is
   published as `@running/<job>/<n>` every 3 s when it changed, and deleted when the build is
   finalised (the finished build is then `@builds/...`). The other replicas only **record** that it
   exists; they never write it into their home, because loading it there would resume it.
2. **Adoption needs the runner gone and a claim.** The runner is the replica that last wrote the
   build's copy. A replica adopts it when the runner is itself (it restarted: the old process is
   gone) or the fence says the runner is gone since that copy; it claims the build by writing the
   copy again under its own name, compare-and-set (of two replicas trying, one wins), writes it into
   its home, makes the number known and loads the run, which resumes itself. Lab fence: the
   Kubernetes API says the runner's pod is gone, being deleted, not running, its container started
   after the copy (a new process), or its node not Ready.
   **Not the job's lease.** The first design required it. The lease decides who starts builds and
   reserves numbers, and it moved to the other replica for a new build (#8) while #7 waited for
   adoption with its agent on the first replica: #7 was stuck between the two for good.
2b. **A build follows its agent.** It is adopted only where the agent it is using (the last
   `Running on X` of its log, if that node block has not closed) is online. The name is read with
   the console notes removed: in the log file it is a hyperlink note, and read raw it named no agent
   any replica had -- nothing was adopted in the second lab run.
2c. **An idle agent visiting another replica goes home** when its home answers, so the next build of
   a job owned by the home has its agents (both agents had ended on one replica once). The first lab run had
   the dead replica come back in 9 s and take its own build back while its agent had reconnected to
   the other replica; the build waited for that agent for good.
3. **A replica that starts finds its own running builds suspect**: before Jenkins loads its jobs,
   each build directory whose build.xml says `<completed>false</completed>` is moved aside. Before
   loading, not after: a job lists its numbers as it loads, and a number listed while its directory
   was away stayed unloadable (`recognizeNumber` keeps an entry it already has). And not "no
   `<result>`": a running Pipeline's build.xml already says `SUCCESS`, its initial value. Those builds come back only through
   adoption (2) -- by this replica or another, whichever takes the lease -- so a build is resumed in
   one place, from the latest replicated copy.

### Measured (the lab, netci22, `lab/aa/phase5_probe.py`, `aa-phase5-20261013T192242Z.json`)

`long`, 90 one-second ticks in an `sh` step on agent-0 (inbound, WebSocket through the VIP), owned by
r0. Nothing done by hand after the death; the adoption time is the adopting replica's own log line.

| Death of the owner | Adopted by | Adopted after | Result | Ticks | Returning replica |
|---|---|---|---|---|---|
| its pod killed (no grace) | jenkins-1 | **4.3 s** | SUCCESS, 74.5 s after the death | 90, none missing, none twice | lists it SUCCESS, did not resume it |
| its machine's power cut | jenkins-1 | **49.5 s** | SUCCESS, 76.2 s after the death | 90, none missing, none twice | lists it SUCCESS, did not resume it |

After a machine's death the build still finished when it would have: the `sh` process ran on the
agent throughout, and the adopter read its output on. The 49.5 s are Kubernetes taking the dead
node to NotReady, which is what the lab's fence waits for; a power fence (Redfish) is faster.

Found on the way, each kept as evidence and fixed: the agent's name read from the raw log (a
console note) matched nothing; adoption waited on the job's lease, which had moved for a new build
(#7 stuck while #8 ran); a fast-returning replica took its build back while its agent was on the
other; HAProxy kept an agent's tunnel to a dead machine open for an hour; the fence's pooled
connection kept landing on the dead API server; an agent was sent home from a build just adopted
for it. Once, the lab's PostgreSQL was on the lost machine: the store answered 503 for ~80 s and
adoption waited for it -- the store must be HA (done since, ADR-080).

### What it costs, said plainly

- **The fence decides correctness.** A pod that Kubernetes reports gone is gone on a live node; a
  machine that is partitioned but alive is not proven dead by the API. A site fences the machine
  (Redfish power-off, as the supervisor does for cells, ADR-060/069); the lab's Kubernetes check
  covers a killed pod and a dead machine, not a partition.
- An agent is connected to one replica at a time. A build adopted while its agent is still held by
  an old owner that is alive waits for the agent ("Waiting for reconnection"), it is not run twice
  on the agent.
- The build is paused from the owner's death to the adoption: the lease (15 s) plus the fence.
- A copy is at most 3 s old (a replica stopping on purpose publishes a last one); it is published
  only once the build has a build.xml -- the first copy, a bare log, could not be loaded by anyone.
  The `sh` step reads its output on from the offset the program holds.

### Rejected

- **Resuming on every replica and letting one win**: the losers would have run controller-side
  steps already.
- **Calling `onResume()` after loading the run**: a second resumption; it failed the build by hand.

## ADR-080: The store made HA; a rolling upgrade measured; a deadlock the failover run found

- Status: accepted; measured in the lab (below), soak included
- Date: 2026-10-14
- Refines: ADR-076, ADR-079

### Context

The active-active replicas (ADR-076..079) keep their homes the same through netci-homesync, and
netci-homesync was one PostgreSQL on a Longhorn volume. That was the last single machine whose
loss stopped edits. It had already happened:
- the lab's PostgreSQL was on the machine lost in ADR-079's run, and the store answered 503 for ~80 s;
- after the host restarted on 2026-10-14, every Longhorn volume came back faulted, and the store was
  down for ~10 minutes until Longhorn salvaged them.

The site mostly runs local-path, and so do the homes.

### Decision

1. **The store is three CloudNativePG instances, one per machine, each on its own machine's disk**
   (`local-path`). PostgreSQL replicates itself, as the homes are replicated through it. There is no
   shared volume, so no volume can come back faulted for all of them at once.
2. **Synchronous to ANY 1 of the two standbys, durability required.** A write is acknowledged only
   once a standby has it, so the standby promoted after the primary's death has every acknowledged
   write: a version compared-and-set, a build number reserved.
   - With one machine down, a standby remains and writes go on.
   - With two down, writes stop: the store refuses rather than forgets.
3. **The operator decides failover, so there are two of it, on two machines** (leader election,
   tolerations of 10 s for a dead node). One operator pod on the dead machine would have waited ~5
   minutes to be evicted before anyone promoted a standby.
4. **netci-homesync notices a dead peer in 10 s** (keepalives after 3 s, `TCP_USER_TIMEOUT` 10 s).
   pgx keeps the kernel's defaults, which retransmit for ~15 minutes. Under them, every pooled
   connection to the dead primary cost a request the plugin's 10 s timeout, and the LISTEN
   connection waited forever. It connects with `target_session_attrs=read-write`, so a connection
   never settles on an instance that is still a standby, or one again.
5. **Applying and publishing are ordered per item, not under one lock** (the plugin; see "Found").
   A publish can run inside Jenkins' locks. It waits for its item at most 10 s; after that it is
   deferred to the lease thread and redone from the home's content, read under the item's lock.
6. **The feed's order is its commit order.** Every change takes one advisory lock just before it
   is placed in the feed, and holds it until the commit (see "Found").
7. **The store's instances and the operator talk to their own machine's API server**, not the
   Service in front of all three (see "Found"). A machine's API server lives and dies with the pods
   on it.
8. **A running build's copy holds everything else first and its log last, flushed.** The log then
   holds all the output program.dat says was read. Watched output also carries the agents' marks,
   which the adopter puts back (see "Found").
9. **Agents notice a dead path in seconds.** Replicas ping every 5 s with a 10 s timeout, for both
   sides. The lab's agents use the balancer on their own machine and give up a connection attempt
   after 3 s; an agent outside the cluster uses the VIP.
10. **A replica leaves traffic before it stops.** The balancer checks the plugin's `/netci-ready/`.
    The pod's preStop makes it answer 503 and waits 6 s before Jenkins is told to stop. A 503 that
    still gets through is retried on the other replica.

`lab/aa/store.sh install` sets this up:
- applies the operator release by checksum, with its image by digest;
- applies `lab/aa/store.yaml`, with PostgreSQL 16.15 copied into Harbor because the nodes could not
  finish pulling it from ghcr.io;
- copies the data with homesync stopped, checking the row counts on both sides;
- points homesync at the new store.

### Found: a deadlock, before anything was killed

The first failover run (`aa-phase6-store-20261014T023528Z.json`) went like this:
- r1 stopped taking changes 6 s *before* the power-off, and never recovered.
- r1's thread dump showed the cause:
  - A job created on r1 held Jenkins' item-group lock and, publishing from `onCreated`, waited for
    homesync's single lock.
  - The follower held that lock while applying a job r0 had created, and waited for the item-group
    lock.
- From then on, every edit on r1 queued behind the two.

Nothing about failover is needed: two replicas creating jobs at once can hit it. Phase 3's
concurrent edits changed existing jobs; they never created any.

Two tests now cover it:
- The lab's interleaving, forced through the fake store. It fails on the old code with "the create
  here never finished".
- A contended publish, deferred within its bound and done later. Writing it caught the deferred
  publish reading the home before taking the item's lock, which undid an applied change in the store.

### Found: an acknowledged create skipped by one replica

A later run (`aa-phase6-store-20261014T043727Z.json`) checked every create answered 200 on both
replicas. One, `st-043727-r1-2`, was missing on r0, although it was in the store at seq 3205, 9 s
before the power-off. Nothing was logged. The cause:
- `home_changes.seq` comes from a sequence, handed out at insert time.
- Transactions commit in any order, so the one holding 3206 committed first.
- r0's follower read 3206, moved past it, and never read 3205.

Synchronous replication made each commit long enough to be overtaken. The single PostgreSQL before
had the same hole, only narrower. The test holds one change before its commit while another is
made: without the lock the reader sees only the second, with it both. A replica that missed a
change before the fix gets it back when it restarts: its bootstrap compares every item with the
store. r0 had `st-043727-r1-2` after the restart.

### Found: a standby's machine lost restarted the healthy primary

The first soak (`aa-phase6-soak-20261014T040915Z.json`) powered off the machine of a jenkins
replica, which also ran a standby. Three minutes later edits were refused for ~4 s: the
**primary's** postgres had been restarted by its own liveness probe.
- Its instance manager reached the API server through the Service, and its pooled connection sat on
  the dead machine's API server for ~40 s.
- CloudNativePG's isolation check then reached neither the API server nor the dead standby, and
  failed the primary's liveness.
- The kubelet restarted a primary that had nothing wrong with it.

With each instance on its own machine's API server, a standby's machine lost cost no edit:
`aa-phase6-store-20261014T043239Z.json` shows r1 refused nothing, and r0 lost two writes only
because jenkins-0 was on that machine.

### Found: an adopted build missing two lines of its log

A rerun of the soak's machine loss alone (`aa-phase6-soak-20261014T054113Z.json`) adopted
`long #30` with SUCCESS, but ticks 10 and 11 were not in its log; the `sh` step had printed them.
The lab's durable steps poll:
- the controller reads the agent's output file from `lastLocation`, writes it to the build log,
  and saves `lastLocation` in program.dat;
- the copy took `log` before `program.dat` (name order), so a read between the two left
  program.dat past the copy's log, and the adopter read on from there;
- and a Pipeline log is buffered (seconds) before it reaches the file.

A copy now holds everything else first, then the log, flushed just before it is copied. The log
then holds all output up to program.dat's position. At worst, a read landing between the two files
is shown twice: duplicated, never lost.

Where output is watched (durable-task's `USE_WATCHING`), the agent keeps the mark itself. A copy
records each running `sh` step's mark together with the log's length read just after it. The
adopter puts the agent's mark back to the copy's, and moves it on only past what was appended to
the log after the mark, when that is exactly the output following the mark. "Whatever the log ends
with" would have skipped a repeated line the log never got.

The tests:
- the log is the archive's last entry;
- only output provably appended counts;
- a mark is put back, never forward;
- a watched `sh` step's copy holds every tick up to its mark. This one failed ("lacks tick 11")
  until the log was flushed.

### Found: an agent connected to a dead machine for 9 minutes

In 1 of 2, then 1 of 3 machine-loss rounds the build was adopted only after ~8.7 minutes, yet
still SUCCESS with every tick. The agent's log showed why: agent-0 stayed "Connected" to the dead
jenkins-0 until its own remoting ping failed (Jenkins defaults: every 300 s, 240 s timeout).
- The lab's agents are pods. kube-proxy sends their connection to the VIP to the HAProxy of any
  machine, and in those rounds it was the HAProxy on the machine that lost power.
- A machine without power sends no reset, so the connection went silent.
- An agent outside the cluster, as at the site, meets the same thing when the machine holding the
  VIP dies.

The replicas now set a remoting ping every 5 s with a 10 s timeout. The controller sets the
agent's pinger too, so an agent on a dead path reconnects within ~15 s.

### Found: a replica stopping answers 503 for ~2 s

A second rolling upgrade (`aa-phase6-upgrade-20261014T061407Z.json`) failed 2 of 280 UI polls, one
at each replica's restart. HAProxy retried connection failures and empty responses, but not a
503. A stopping Jenkins answers 503 until its health check fails twice. HAProxy now retries a 503
on the other replica. That is safe: Jenkins says 503 before it acts on a request, and so does the
plugin when it refuses an edit.

### Measured (the lab: controller netci25, homesync with every fix above)

Each run below is kept in `lab/evidence/`. The load was the same throughout:
- each replica created a job every second through the VIP;
- the UI was polled through the VIP (every second, or twice a second in the upgrades);
- every create answered 200 was checked on both replicas afterwards.

**The store's machines.** These are `phase6_store_probe.py` runs on the feed fix.

| Machine powered off | Acknowledged creates | Missing on either replica | Edits refused | UI |
|---|---|---|---|---|
| The primary's, with jenkins-0 on it (`aa-phase6-store-20261014T045629Z.json`) | 268 | none; reserved numbers all on the promoted standby | 503 until 27.6 s (promoted at 22.6 s) | 148 of 149 |
| A standby's, with jenkins-0 on it (`aa-phase6-store-20261014T043239Z.json`) | 317 | none | none on r1; r0 lost 2 only because jenkins-0 died | 148 of 149 |

**The soak** (`phase6_soak.py`, `aa-phase6-soak-20261014T073506Z.json` plus 075230Z and 080719Z).
Each round started `long` (90 one-second ticks on an inbound agent) and injected one fault 10 ticks
in. There were 11 rounds:
- a jenkins pod killed, its replica's machine powered off (4 rounds);
- the store's primary pod killed, its machine powered off (3 rounds);
- a homesync pod killed;
- the HAProxy pod holding the VIP killed.

| | |
|---|---|
| Builds | 11 of 11 SUCCESS; every tick exactly once; each listed on both replicas; numbers distinct |
| Build finished after the fault | 83-85 s every round, as if nothing had happened (90 ticks, 10 already done) |
| Acknowledged creates | 3235, every one on both replicas |
| UI | 1742 of 1750 polls answered (each failure at the instant of a death) |

**Rolling restarts and upgrades** (`phase6_upgrade_probe.py`). Each one went one replica at a time,
with `minReadySeconds: 30`.

| Run | UI polls answered | Creates | Not accepted |
|---|---|---|---|
| netci22 to netci23 (`aa-phase6-upgrade-20261014T035213Z.json`) | 286 of 286 | 270, all on both | one 403 per restarted replica |
| netci23 to netci24, before the 503 retry (`...061407Z`) | 278 of 280 | 279, all on both | one 403 per restarted replica |
| restart, 503 retried, no drain (`...081554Z`) | 295 of 297 | 298, all on both | one 403 per restarted replica |
| **restart with the drain** (`...083045Z`) | **306 of 306** | 303, all on both | one 403 per restarted replica |

The 403 is Jenkins' CSRF crumb. The probe signs in with a password and keeps a crumb bound to a
session on the replica that restarted; it fetches a new one and goes on. A script using an API
token needs no crumb and does not see it.

**Nothing earlier regressed**, checked on netci23 with the HA store:
- Phase 3, all eight checks (`aa-phase3-20261014T040322Z.json`): a job on r0 reached r1 in 135 ms
  (median); 40 concurrent edits gave 30 accepted and 10 refused with the holder named, and both
  replicas converged; edits were refused with homesync down; the sign-in survived its replica, 60
  of 60 polls.
- Phase 5, pod killed (`aa-phase5-20261014T040446Z.json`): adopted in 6.4 s, SUCCESS, every tick
  once.

Superseded, kept for what they found: `aa-phase6-store-20261014T023528Z.json` (the deadlock),
`...035606Z` (on the feed bug, before it was known), `...043727Z` (the skipped create),
`aa-phase6-soak-20261014T040915Z.json`, `...050449Z` and `...054113Z` (the liveness restart, the
9-minute agent, the two lost lines), `...061712Z` (the agent again, 525 s), `...064157Z` (a SYN to
a dead balancer retried for 127 s, every round 145 s) and `...070341Z` (a "Build now" answered 302
by a replica just back; the probe now asks again).

### What it costs, said plainly

- **Edits are refused for ~25 s when the store's primary dies.** Most of that is CloudNativePG
  between noticing and promoting (noticing takes 4-20 s, longer when the operator's leader died
  too). Reads, the UI and running builds go on: a build needs the store only to start (its number)
  and to be adopted. Losing a standby's machine costs no edit.
- A job created in the second before its machine lost power can be left empty on that machine's
  disk (Jenkins writes it without a sync): it failed to load there ("Premature end of file"). The
  replica's bootstrap wrote it again from the store, and both replicas had it.
- Feed writes are ordered through one lock: the store takes one change at a time from the feed
  insert to the commit (a synchronous standby's acknowledgement included). That is a few hundred
  changes a second, far above what Jenkins edits and finished builds produce.
- The 403 after a replica's restart is Jenkins' CSRF crumb, bound to the replica's session. A
  person's browser fetches a new one when the page reloads; a script must retry once.
- Three PostgreSQL instances cost ~150 MB of memory each in the lab, plus two operator pods.
- A publish deferred because its item was being applied is done within 5 s by the lease thread.
  Until then the other replicas lack that change; it is logged as such.

### Rejected

- **PostgreSQL on Longhorn with one instance**, as before: one volume, and it came back faulted.
- **Patroni with etcd**: a second consensus system to run beside Kubernetes'. The operator uses the
  API server's leases.
- **Asynchronous replication**: a failover could forget a reserved build number or an accepted
  version, and the replicas would hand the same number to two builds.

## ADR-081: A run from netci-queue runs once across active-active replicas

- Status: accepted; unit- and store-tested, measured in the lab (below)
- Date: 2026-10-14
- Refines: ADR-063 (the durable run queue), ADR-078, ADR-079

### Context

The IDP is to submit builds through netci-queue (ADR-063, ADR-073), not to a Jenkins directly.
netci-queue dispatches a run to a controller's plugin (`/netci/dispatch`), follows it
(`/netci/run`), and decides from the answers. It was built for cells, one controller per cell, and
it reads three answers as final:
- a **409** means the run is refused for good;
- **absent** for a started run means it finished (result unknown);
- a **new controller session** with the run absent means the run was lost with that controller's
  queue, so it is dispatched again.

In front of active-active replicas these calls go through the VIP and land on either replica. Read
from the code, before any lab run, each of the three goes wrong without a fault:
- A dispatch on the replica that does not own the job is refused by Jenkins' queue (only the owner
  schedules, ADR-078). The plugin answered 409, so the run was refused for good.
- A lookup on that replica does not see a build running on the owner. It answered absent, so the
  run was marked finished while it ran.
- The worst case, when the owner dies with the run's build on it:
  1. the lease moves to the other replica;
  2. netci-queue sees a new session and dispatches again;
  3. the new owner has nothing yet (the build waits for adoption), and schedules a second build;
  4. the adoption resumes the first build, so the run runs twice.

### Decision

1. **Each call goes to the job's live owner** when that is another replica. The receiving replica
   makes the same call there with the caller's API token (users are replicated; API tokens need no
   CSRF crumb), marks it routed so it is never forwarded twice, and relays the owner's answer.
2. **The store records which build number a run was given.** The reservation made before a build
   takes its number (ADR-078) carries the run id.
   - A unique index refuses a run a second number, on any replica and for any job.
   - The item that asked for it is cancelled (by id: canRun's item is not the object the queue
     holds).
   - This is the guard that holds when everything else races.
3. **A replica that does not hold a run asks the store**, outside Jenkins' queue lock:
   - a number with a running copy, or with a finished build not yet listed here, is answered
     **started, building**;
   - a number and nothing else is answered **starting**, unless its replica is gone: this replica
     itself having restarted, or the fence saying so. Then the number is let go (the number stays
     used) and the run may be scheduled once more.
   - This replica never guesses a result.
4. **With replicas, a passing refusal is a 503, never a 409**: another replica owns the job, or the
   store cannot be reached. netci-queue retries a 503.

### Tests

Store (PostgreSQL):
- a run gets one number across replicas and jobs, and is told where it went;
- 20 rounds of two replicas reserving one run at once give exactly one winner. This fails without
  the unique index mapping;
- release, lookup and authentication over HTTP.

Plugin (fake store and fake owner):
- dispatch and lookup forwarded to the owner;
- a run with a build running elsewhere is reported started and never scheduled. With the store
  lookup disabled it was queued again (`"state":"queued","created":true`);
- a number this replica gave before restarting runs once, and the next number is used;
- a second item for a numbered run is cancelled;
- with the store down, 503 for both calls.

### Lab (`lab/aa/phase7_queue_probe.py`)

netci-queue in front of the replicas (`lab/aa/queue.sh`: two instances, its database on the HA
store, Jenkins reached through the machine-local balancer).

**First run, on netci26** (`aa-phase7-queue-20261014T095524Z.json`). The owner's machine (with the
store's primary) was powered off under `long`:
- `long` was SUCCESS, every tick once;
- six runs submitted while the machine was off were all SUCCESS, with one build each on both
  replicas;
- but 5 of 10 runs submitted as the machine came back were **refused for good**. The restarted
  replica, not yet caught up with the store, answered 409 like a lone Jenkins. Fixed in netci27:
  503 until caught up, and kept out of the balancer until then.

**On netci27** (`aa-phase7-queue-20261014T103155Z.json`): **17 of 17 runs SUCCESS, each with
exactly one build on each replica**:
- 10 steady;
- `long` with its owner's machine powered off at tick 10, every tick once;
- 6 runs submitted while the machine was off;
- `long`'s key submitted again: the same run.

While the store's primary failed over, the queue answered no submission (not ready). A client
retries with the same key, as the IDP's adapter does (ADR-082).

### What it costs, said plainly

- A dispatch or lookup on the non-owner costs one more hop.
- A replica that cannot reach the owner answers 503, and netci-queue retries until the lease moves
  (15 s).
- The fence decides "its replica is gone". Without one (the lab's tests), a number reserved by a
  dead replica that never started its build waits for that replica to come back.
- netci-queue still treats a new session as a lost queue. With replicas that is safe now (a
  dispatch again finds the run, or the store refuses a second number), but it costs re-dispatches
  when calls alternate between replicas.

### Rejected

- **Pointing netci-queue at one replica.** That replica would be the single point of failure the
  VIP removes.
- **Making netci-queue replica-aware** (two URLs, one session per replica). It would put homesync's
  ownership rules in a second program, while the plugin already knows them.

## ADR-082: The IDP submits its builds through netci-queue

- Status: accepted; unit-tested, lab smoke below
- Date: 2026-10-14
- Refines: ADR-063, ADR-073, ADR-081

### Context

The IDP triggered builds with `buildWithParameters` on a Jenkins it chose, and followed them by
Jenkins queue item and build number. Every Jenkins HA guarantee measured in the lab (ADR-060..081)
is for runs that come through netci-queue. A build the IDP triggered directly could be lost, because
Jenkins' queue is in memory: the lab lost exactly that kind of trigger.

Three behaviours of the launcher do not survive replicas or a durable queue:
- **Falling through to the next controller.** When a trigger failed, the launcher tried the next
  controller. If the first one had accepted the build and only its answer was lost, the run was
  built twice.
- **Queue item ids.** They belong to one replica, and so does `/stop` on a build. Behind the VIP,
  both land on either replica.
- **A new callback token on every launch.** A retried launch looked like a different run to an
  idempotent intake.

### Decision

1. With `NETCI_RUN_QUEUE_URL` (and its client token) set, the Jenkins adapter is
   `JenkinsQueueAdapter`.
   - A build is a `POST /v1/runs` keyed by the IDP's pipeline run id, carrying the same parameters
     as before (`_ci_parameters`, shared with the direct adapter).
   - It is read (`GET /v1/runs/{id}`) and cancelled (`POST /v1/runs/{id}/cancel`) by that run id.
   - The job is still created through Jenkins at the one entry point, where it is replicated
     (ADR-076). The log is read there too.
2. **One entry point only.** With a queue, `NETCI_JENKINS_CONTROLLERS` must name exactly one
   controller: the queue routes builds, not netCI. Startup is refused otherwise.
3. **A submission is retried with the same key while the queue is unavailable** (503 or no answer;
   its database failing over took ~30 s in the lab), for up to `NETCI_RUN_QUEUE_SUBMIT_RETRY_SECONDS`
   (120 s). Any other refusal fails the launch at once. Accepted means durable.
4. **The callback token is a secret parameter of the queue** (`secretParameters`).
   - The queue keeps it, because the run is dispatched from what was stored.
   - It never shows it back.
   - It is left out of what makes a retry the same run: a retry carries a newly minted token, and
     the run keeps its first one.
5. **Statuses map to the IDP's existing vocabulary.**
   - accepted and dispatched: queued; started: running;
   - finished by result: SUCCESS succeeded, FAILURE or UNSTABLE failed, ABORTED cancelled;
   - cancelled: cancelled; refused: failed;
   - a result the queue could not learn: unknown, which the IDP does not act on.
   - Runs with ids from before the queue are still read from Jenkins.

### Tests

`backend/tests/test_jenkins_queue_adapter.py`, against a real HTTP fake of the intake:
- one run per pipeline run, retried, with a newly minted token;
- the queue unavailable for three submissions, waited for with the same key;
- a 403 not retried, a queue down past the bound failing the launch, no answer at all;
- every state and result mapped;
- the log read through Jenkins under a folder, empty before the build exists;
- cancel by run id;
- an old run id read from Jenkins;
- the token required, one entry point required;
- no error carries the callback token.

The full gate: `pytest backend/tests tests/contract` 62 + 1507 passed, `pyflakes` clean,
`schema.sql` matches 35 migrations.

### Lab smoke (`lab/aa/idp_queue_smoke.py`, `aa-idp-queue-20261014T105149Z.json`)

The IDP's own adapter, against the replicas behind the VIP and netci-queue on the HA store
(controller netci28):
- the job the IDP generates was on both replicas 0.16 s after its creation;
- the build was submitted with every IDP parameter and read by run id (queued, then failed), and
  submitted again with a new token it was the same run;
- its 23 log lines were read through the VIP;
- the queue's answers never held the token;
- a second build was cancelled through the queue.

The build failed where it had to: `Could not find any definition of libraries
[netci-shared-library]`. The lab's replicas have no netCI shared library, so this proves the path,
not the pipeline. The rolling upgrade to netci28 under load answered 358 of 358 UI polls.

### What it costs, said plainly

- The IDP's admission budget (ADR-050) still counts the queue of the one Jenkins it reaches. With
  replicas that is one replica's queue. The queue's own budget per cell (ADR-063) is what bounds
  Jenkins now.
- The log of a build is read through the entry point. A replica that does not have the build sends
  the request to the one that does (owner routing, ADR-078). During a cell's takeover there is no
  log until it is back, except in Loki (ADR-068).

## ADR-083: The store in production: backups that are restored, a feed that is compacted

- Status: accepted; measured in the lab (below)
- Date: 2026-10-14
- Refines: ADR-076, ADR-080

### Context

The HA store (ADR-080) survives the loss of a machine, but two things would still have stopped it
in production:
- **No backups.** Replication copies a mistake to every instance at once: a job deleted by hand,
  or a bad migration. Nothing outside the cluster held the data.
- **No end to the feed.** `home_changes` keeps one row per change. A running build's copy is
  published every 3 s, so each build leaves dozens of rows, and a day of lab tests left 18,000.
  Nothing removed any.

### Decision

1. **Backups to S3-compatible storage**: Ceph RGW or MinIO at the site, the lab's SeaweedFS behind
   its CA.
   - WAL is archived continuously, and a base backup is taken daily (`ScheduledBackup`), kept 7 days.
   - CloudNativePG 1.30 still has the in-tree `barmanObjectStore`, deprecated and **removed in
     1.31.0**. The production chart uses the Barman Cloud plugin, which needs cert-manager; the
     settings carry over unchanged.
2. **A backup is only one when it has been restored.** `lab/aa/store.sh restore-test` recovers a
   new one-instance cluster from the object store and compares its rows with the store's. Run it
   after every change to backups, and on a schedule at the site.
3. **The feed is compacted, like a log.** Among changes older than a bound
   (`NETCI_HOMESYNC_COMPACT_AFTER`, 24 h), compaction deletes:
   - each change a later change of the same path has superseded;
   - everything of a running build's copy whose build has ended, its tombstone included.

   The latest change of every other path stays, so a replica reading from the start still learns
   every item, deletions included. Compaction takes the feed lock (ADR-080), so it decides on a
   prefix. Every homesync instance runs it hourly, taking turns on that lock.
4. **A replica behind the compaction reads everything again.** The feed's answer carries
   `compactedThrough`, the highest seq deleted.
   - A follower below it bootstraps again instead of going on: going on could apply a child before
     its parent, or never publish what only it has.
   - An edit catching up below it is refused until then.

### Measured / tested

- **Backup and restore** (lab, CloudNativePG 1.30.1, SeaweedFS):
  - continuous archiving and a base backup ran;
  - a cluster recovered from them in 384 s held 18,398 items and 126 build-number reservations,
    the same as the store.
  - On the way: the lab's key files end in a newline, which went into the S3 signature ("Invalid
    header value"), and the lab's S3 identity may write to one bucket only (AccessDenied). The keys
    are trimmed, and the backups use a prefix of that bucket.
- **Compaction** (Go, PostgreSQL): superseded old changes and an ended build's copy are deleted,
  recent ones kept, the feed afterwards is `a@3 b@1 c@1 c@2`, the mark is set, and a second pass
  deletes nothing.
- **Catch-up** (plugin): a home behind the compaction reads everything again. A job made while the
  store was down got published. With the re-bootstrap disabled it never was.

### What it costs, said plainly

- A replica down for longer than the compaction bound comes back by a full bootstrap, which it
  does on any start anyway.
- Backups need object storage that is outside the cluster's failure domain, and credentials for it
  in the cluster (a Secret). Restores are 6 minutes for this size in the lab.
- Finished builds' archives (`@builds/`) stay in the store until Jenkins deletes the build. Give
  every job a build discarder; the IDP's generated jobs carry one.

## ADR-084: Active-active replicas are fenced by power, on OpenStack through Nova

- Status: accepted; unit-tested, measured in the lab (below)
- Date: 2026-10-14
- Refines: ADR-060 (the supervisor fences cells), ADR-079 (adoption needs the runner gone)

### Context

A replica adopts another's running build only once its fence says the runner is gone (ADR-079).
The lab's fence read Kubernetes: a missing or deleted pod, or a node not Ready, meant gone. That
holds for a machine that lost power. It does not hold for one cut off from the network, which keeps
running its pod while the API shows the node NotReady and, later, the pod deleted. Adopting then
runs the build twice. The cells never had this problem: the supervisor powers a cell's machine off
and confirms it off before anything takes over. The replicas had no cell Lease for it to act on.

At the site the controllers are OpenStack VMs, and the owner answered that the cluster may hold an
application credential allowed to stop and start them.

### Decision

1. **A Nova fencer** (`internal/fence/nova.go`):
   - Keystone application-credential login, token reused until a minute before it expires;
   - the compute endpoint from the catalog (interface, region);
   - stop and start by `os-stop` / `os-start`.
   - Off is only SHUTOFF with power state SHUTDOWN and no task in progress. Anything else is
     Unknown, which never counts as off.
   - Keystone's 401 is a refusal. Nova's 401 means log in again. Nova's 409 means busy, so ask again.
   - The secret is never in an error.
   - Configured per node in the fence config:
     `{"nova": {"authURL", "region", "server", "credentials"}}`.
   - The controllers' image should carry `os_shutdown_timeout=0`, so that the stop is immediate.
2. **The supervisor fences a lost node that has no cell** when the node is labelled
   `netci.io/fence-on-loss=true` (`FenceLostNode`):
   - the trigger is a kubelet silent for `NETCI_NODE_FENCE_AFTER` (45 s) and the node not Ready;
   - the machine is powered off, or confirmed off, through `EnsureOff`, and only then the node gets
     the out-of-service taint;
   - the cells' guards apply: nothing is powered off while failures look shared, at most one
     running machine per decision, an unreadable power state is an alert;
   - `recovery()` removes the taint when the machine is back.
3. **The replicas' fence has a strict mode**, `NETCI_ADOPT_FENCE=kubernetes-power`, for production.
   A runner counts as gone only on proof that it cannot write:
   - its node carries the out-of-service taint;
   - or the kubelet of a node that is Ready reports the old process ended (container stopped, or a
     newer one).

   A missing or deleted pod and a NotReady node are not enough. `kubernetes` (lenient) stays for
   the lab's quick runs.

### Tests

- Nova, against a fake Keystone and Nova:
  - fenced end to end with one login;
  - Off only when the hypervisor has stopped the server (five in-between states are Unknown);
  - refusals vs retries;
  - a server id that is a path refused;
  - fence-config parsing, including two nodes on one server refused.
- Supervisor:
  - a labelled lost node is fenced, powered off or confirmed off;
  - an unreadable state is an alert;
  - not labelled, still Ready, silent briefly, or already fenced: left alone;
  - two lost of three: nothing powered off;
  - zero disables it.
- Plugin (`FenceTest`):
  - a partitioned machine is gone to the lenient fence and not to the strict one, until the taint;
  - a reporting kubelet proves a new or ended process in both modes;
  - a missing pod is gone only to the lenient fence.

### Lab: a partition (`lab/aa/phase8_partition_probe.py`, `aa-phase8-partition-20261014T171635Z.json`)

The setup:
- the replicas on netci29 with `NETCI_ADOPT_FENCE=kubernetes-power`;
- the supervisor on lost-node fencing, through the lab's Redfish emulator;
- `long` on agent-0, owned by r0.

At tick 10, r0's machine (netci-lab-2) lost its link (`virsh domif-setlink ... down`): the VM kept
running, and its Jenkins with it. Seconds after the link went down:
- 51.7: the node NotReady;
- 51.6: the supervisor's out-of-service taint, put on after its power controller confirmed the
  machine off (the probe, polling, saw the VM off at 54.0);
- **57.8: jenkins-1 adopted the build, after the fence and not before**;
- 81.9: the build ended SUCCESS, 90 of 90 ticks, none twice, resumed once.

The lenient fence would have adopted at the NotReady with the old replica still running. Here it
happened to come at the same second, which is why only the power-off may decide.

### What it costs, said plainly

- With the strict fence, a lost replica's builds are adopted after the supervisor's fence: 45 s of
  silence, then the power-off and its confirmation. That is longer than the lenient fence's ~50 s
  NotReady, but it is the one a partition cannot fool.
- A VM whose hypervisor host died cannot be stopped by Nova, which keeps reporting its last state.
  It stays unfenced, and its builds unadopted, until an operator forces the compute service down.
  That is a stall, not a double run.
- The application credential is a Secret in the cluster that can stop the controllers' VMs.
  Restrict it to them (an application credential with access rules, or a project holding only
  them).

## ADR-085: Active-active Jenkins is installed by one chart; backups go through the Barman Cloud plugin

- Status: accepted; the lab now runs from the chart (measured below)
- Date: 2026-10-15
- Refines: ADR-076..084

### Context

Until now the lab was a set of manifests applied by hand in order (`lab/aa/phase2.yaml`,
`phase3.yaml` with digests substituted, `phase5.yaml`, `store.yaml`, `queue.yaml`), plus patches from
`store.sh`. A site cannot install from that, and nothing checked that a change to one manifest still
matched the others. The site's answers decide the variants: the VIP may come from Octavia or from
kube-vip; backups go to S3-compatible storage (Ceph RGW or MinIO); cluster-scoped operators are
allowed.

The store's backups used CloudNativePG's in-tree `barmanObjectStore`. It is deprecated and is
removed in 1.31, so a site that upgrades the operator loses its backups.

### Decision

1. **One chart, `deploy/helm/netci-jenkins-ha`**, renders everything the lab ran:
   - the replicas;
   - homesync;
   - the balancer and its config;
   - the VIP;
   - the CNPG store and its backups;
   - netci-queue;
   - the fence's RBAC.

   The names stay the lab's (`jenkins`, `homesync`, `haproxy`, `ui`, `store`, `queue`), because the
   plugin's configuration and `haproxy.cfg` refer to them. Everything that depends on the number of
   replicas is generated from `replicas`: replica URLs, cookies, `server` lines and agent homes.
2. **It refuses rather than guesses:**
   - every image must be pinned by digest;
   - the store's image must also carry a tag, because CNPG reads the major version from it;
   - an agent's home must be a replica that exists;
   - every Secret the replicas need must be named;
   - the fence must be one of `kubernetes` or `kubernetes-power`.

   The chart creates no Secret. Each is named in values and made by the site.
3. **VIP in two modes.**
   - `kubevip`: an ARP VIP. On OpenStack the port needs allowed-address-pairs for that address.
   - `loadBalancer`: a `LoadBalancer` Service with the site's annotations, which is Octavia through
     cloud-provider-openstack.

   Both use `externalTrafficPolicy: Local`, so the client's address reaches HAProxy and no hop goes
   through another machine.
4. **Backups through the Barman Cloud plugin** (`backup.mode: plugin`):
   - an `ObjectStore` with the same S3 settings;
   - the Cluster's `plugins` entry as WAL archiver;
   - `method: plugin` on the ScheduledBackup.

   `inTree` remains for a CNPG older than the plugin.
5. **The plugin is on the store's failover path, so it runs like the operator:**
   - two pods on two machines, with the release's leader election;
   - the 10 s not-ready/unreachable tolerations;
   - each pod uses its own machine's API server;
   - the readiness check runs every 2 s instead of 10 s.

   It is updated by Recreate, as the release ships it, because only the leader serves: a rolling
   update would wait forever for the new pod to become Ready.

### Measured in the lab

- **The chart against the live lab.**
  - `kubectl diff` of the render showed only labels, two pull secrets, and the homesync/queue image
    meant to change.
  - Helm then adopted the lab's 22 objects (`lab/aa/chart.sh adopt`) and applied the chart under
    the upgrade probe (`aa-phase6-upgrade-20261014T175148Z.json`): 219 of 219 UI reads, 220 of 220
    creates, none missing on either replica.
- **Switching the live store to the plugin through the chart** (`aa-phase6-upgrade-20261014T175428Z.json`).
  - The instances restarted one at a time with the plugin's sidecar, the primary last by
    switchover.
  - The UI answered 602 of 602 reads. Of 584 creates, 573 were accepted and none of those is missing.
  - The 11 refused all fell in the ~11 s of the switchover: 503, or 403 from the crumb problem (below).
- **Backup and restore through the plugin.** `store.sh backup` took a base backup.
  `store.sh restore-test` recovered a new cluster from it in 211 s: 19,192 items and 127 build
  numbers, identical to the store. The WAL it replayed was written partly in-tree, partly by the
  plugin.
- **The plugin found on the failover path.**
  - With its one pod stopped, the operator reconciled nothing for the cluster ("cannot proceed to
    reconciliation due to an error while interacting with plugins"). A deleted standby was not
    recreated, and the deleted primary was not replaced for 189 s. Started again, there was a
    primary 16 s later.
  - The primary's machine powered off (`phase6_store_probe.py --kill primary`), nothing lost in any
    run. Edits accepted again after:

| Plugin | Edits accepted again after | Evidence |
|---|---|---|
| none (in-tree, ADR-080) | 27.7 s, 42.1 s | `aa-phase6-store-20261014T045629Z`, `…043727Z` |
| two pods, leader on another machine | 38.7 s | `aa-phase6-store-20261014T182757Z` |
| two pods, leader on the primary's machine, readiness every 10 s | 73.8 s | `aa-phase6-store-20261014T182323Z` |
| the same, readiness every 2 s | 44.5 s | `aa-phase6-store-20261014T183829Z` |

  With the leader on the primary's machine, the operator kept calling the dead pod through the
  Service until the other pod took the lease (15 s) and passed its readiness check. Each row is one
  run.

### Rejected

- **Keeping the in-tree backups.** Rejected because CNPG 1.31 removes them.
- **A `pg_dump` CronJob instead of WAL archiving.** It needs no plugin, but it restores only to the
  last dump. A day of build numbers and edits would be gone.
- **One plugin pod.** That makes one machine's loss a store with no failover until Kubernetes moves
  the pod (minutes).
- **Each manifest kept beside the chart.** Rejected because two sources of one install drift apart.
  The phase manifests stay as the record of how the lab was built; `lab/aa/chart.sh` is how it is
  run now.

### What it costs, said plainly

- **cert-manager and the plugin are two more cluster-wide components.** While the plugin cannot be
  reached, the store does not fail over.
- **Placing the plugin away from the store.** Where the cluster has machines that run no store
  instance, the plugin belongs there. Then no store machine's loss takes its leader with it.
- **Upgrading the plugin** (Recreate) leaves a few seconds with no failover. Upgrade it when no
  machine is being worked on.
- **Crumbs on failover (ADR-077's open item).** A replica that refused an edit with 503 had HAProxy
  retry it on the other replica, where a browser session's crumb was not valid: 403 instead of "try
  again". Fixed in ADR-086 (controller netci31). Clients using an API token (the IDP, netci-queue)
  send no crumb.

## ADR-086: A CSRF crumb every replica accepts, bound to the shared sign-in

- Status: accepted; unit-tested, measured in the lab (below)
- Date: 2026-10-15
- Refines: ADR-077 (a sign-in that survives its replica)

### Context

Jenkins binds a CSRF crumb to the user and to the HTTP session. A session belongs to one JVM.
ADR-077 made the *sign-in* survive the replica, but not the crumb. So a form served by jenkins-0
and posted to jenkins-1 was refused with 403 "No valid crumb", although the person was signed in
on both. That happens in three cases:
- HAProxy retries a 503 on the other replica;
- the next click after jenkins-0 died;
- each step of a rolling restart (3 refused creates in every upgrade probe so far).

ADR-085 found it again on a store failover: a "try again" (503) became a "forbidden" (403).

The salt is not the problem: it is the `jenkins.model.Jenkins.crumbSalt` secret, already the same on
every replica. Only the session id differs.

### Decision

1. **Each shared sign-in has an id** (`sid`, 18 random bytes) inside the signed `NETCI_SESSION`
   cookie:
   - kept when the cookie is renewed, so a crumb in an open page stays valid;
   - new at the next sign-in;
   - gone at sign-out.
2. **`SharedCrumbIssuer`** binds the crumb to user id + sid, under Jenkins' own salt. A request with
   no shared sign-in gets Jenkins' own crumb, unchanged:
   - an API token (which needs no crumb);
   - a script sending its password, whose first crumb comes before any cookie.

   Either form is accepted where it is valid. The bound form carries a prefix, so it can never equal
   a Jenkins crumb for some session.
3. **A POST that is the first request on a replica is checked against the cookie's sign-in.** The
   filter that restores a sign-in from the cookie may run after Jenkins' CSRF filter. A cookie
   naming a user other than the request's own binds nothing.
4. **Installed at start-up over Jenkins' default issuer only**, when shared sign-ins are on. A
   site's own choice is kept. JCasC can name it: `crumbIssuer: netciSharedSession`.
5. **Read back from config.xml, it is made anew** (`readResolve`). XStream builds objects without
   their constructor, and `DefaultCrumbIssuer`'s own `readResolve` is private, so it is not run for a
   subclass. That left its digest null.

### Why it is as safe as Jenkins' own

A crumb defends against a page on another site making the browser post. Such a page cannot read
the crumb or compute it:
- the sid lives in an HttpOnly cookie, and the crumb salt is secret;
- the cookie is signed, so a sid cannot be chosen;
- signing in again, signing out, and "Invalidate sessions" (the user's seed) each end the sid.

### Tests (`SharedCrumbTest`)

1. **The main test.** A crumb served on one replica is accepted from a browser that has only the
   shared cookie and no session. With Jenkins' default issuer this test fails with 403, the failure
   seen in the lab.
2. A crumb is refused when:
   - it was served to alice and is posted as bob;
   - it is missing, or made up;
   - it belongs to the sign-in before a sign-out.
3. A renewed cookie keeps its sid, and the open page's crumb still works.
4. After a save and reload of the configuration, crumbs are still issued. This test fails without
   `readResolve`.
5. Only Jenkins' default issuer is replaced.

The plugin's 80 tests pass; SpotBugs reports 0 bugs.

### Lab (controller netci31)

- **The first build of this, netci30, shipped the bug in decision 5.** After its rolling start both
  replicas answered `"crumb": null`, and every form was refused. That is how it was found; netci31
  fixes it.
- **Rolling restart under load** (`aa-phase6-upgrade-20261014T193437Z.json`). Two people signed in
  through the form, each creating a job every second with the crumb their page gave them:
  - none of their 239 creates was refused;
  - person r1 held one crumb for 149 creates, answered 60 times by r0 and 89 times by r1.
- The two scripts that send a password were still refused twice (403). Their crumb came before any
  shared sign-in, so it is Jenkins' own, bound to one JVM.
- The UI answered 331 of 331 reads.

### What it costs, said plainly

- **Scripts that sign in with a password and keep a crumb** still get 403 after their replica
  restarts. The answer is Jenkins' own advice: an API token, which needs no crumb.
- **Removing the plugin** leaves config.xml naming a class that is gone. Jenkins then falls back to
  no crumb issuer configured, so set the issuer again (or through JCasC) when removing the plugin.

## ADR-087: Renames and moves are one change of the store

- Status: accepted; unit-tested, measured in the lab (below)
- Date: 2026-10-15
- Refines: ADR-076 (replicated homes), ADR-078 (one owner per job)

### Context

A rename or a move ("Move" from cloudbees-folder) changes an item's full name. Jenkins moves the
item's directory, its builds with it, and then tells its listeners (`onLocationChanged`). It does not
save. The replicas' store knew items only by full name, so the other replica saw only what happened
next:
- the first save under the new name, which arrived as a new job with no builds;
- the old name, which stayed.

After a failover a person found two jobs, and the one with the new name had no history. A rename
also has to be ordered against builds. Jenkins refuses to rename a job while it builds, but it
checks only its own JVM. A build of the job on the other replica would have its directory moved
from under it.

### Decision

1. **The store moves an item in one transaction** (`POST /v1/moves`, `Store.Move`). The old name, its
   children and their finished builds (`@builds/<old>/…`) become tombstones. The same content appears
   under the new names. The jobs' leases and reserved numbers move too, so the owner goes on
   building, numbering from where it was. The feed carries:
   1. the old name's tombstone, with `movedTo` (new column `home_changes.moved_to`);
   2. the new names, parents before children;
   3. the builds;
   4. the other tombstones.
2. **Refused, with the reason, when:**
   - the item is not there, or the new name is (`NO_ITEM`, `EXISTS`);
   - the item or one of its children is locked by another replica (`LOCKED`);
   - a build under it is running, or about to (`BUSY`), on any replica.

   "Running" covers four cases:
   - its running copy is live;
   - its number was reserved less than 2 minutes ago and it has no finished record;
   - its copy was deleted less than 2 minutes ago and it has no finished record (the copy's deletion
     and the finished record are two listeners of one event, in no set order);
   - a running copy with no reservation at all.
3. **The plugin asks before Jenkins acts.** `EditOrder` recognises `confirmRename` and `move/move`. It:
   1. takes both names' locks;
   2. catches up;
   3. asks the store with a dry run.

   It refuses with the store's reason (409) before Jenkins moves the directory, because Jenkins cannot
   be made to take a move back. The move itself is published from `onLocationChanged`, under the same
   locks.
   - A folder's children also get `onLocationChanged`. They are recognised and not published: the
     store moved them with the folder.
   - Saves inside the request, under either name, are part of the move, not changes of their own. A
     save published first would make the new name exist, and the store would refuse the move onto it.
4. **A replica that holds the item renames its own copy** (`Job.renameTo` / `AbstractFolder.renameTo`,
   `Items.move`). Its builds go with the directory, and their archives are not fetched again. A
   feed entry it already holds is recorded without being applied:
   - a build that is there already;
   - a deleted build that is not there;
   - content equal to its own.

   If it cannot rename its copy, it falls back to the plain entries: delete, then fetch the new names.
5. **No build can overtake a move.**
   - `ReserveRun` refuses (`LOCKED`) while the job or one of its folders is locked by another
     replica, so a number is not reserved between the dry run and the move.
   - `ClaimJob` refuses a job whose store entry is deleted (`GONE`, 410). It waits on the item's row,
     which the move holds, so a replica that has not applied the move yet cannot take the old name and
     build there.
6. **A move refused after Jenkins made it** can only come from a script that bypassed `EditOrder`, or
   from a lost lock. The local copy is then moved back to the store's name. A move the store could
   not be reached for is deferred and retried by the lease thread, like any other publish.

### Tests

- **Go (PostgreSQL):** a folder moved with its job, two builds, lease and numbers (and `fx`, which
  only starts with the same letter, left alone). Refused for:
  - a build just given its number, a running copy, a copy just deleted;
  - onto an item, from nothing, into itself;
  - a child locked elsewhere, a stale token.

  A reservation waits while the folder is locked. A claim and a reservation racing the move, held
  in `beforeCommit`, each wait for it and are then refused. The HTTP codes are tested too.
- **Plugin (JenkinsRule, fake store):**
  - a rename here is one move: the new name is never published alone, both locks are let go, and
    the next build is #2 under the new name;
  - a rename is refused while the store says a build runs elsewhere, and Jenkins' copy keeps its name;
  - another replica's rename renames the job here, its build listed, no archive fetched;
  - a move into a folder, then out of it and the folder renamed by another replica;
  - a folder's rename is one move, its child not published again.
- **The plugin's 86 tests pass; SpotBugs reports 0 bugs.**

### Lab (controller netci32, homesync `cab7d33`)

`lab/aa/phase9_rename_probe.py`, evidence `aa-phase9-rename-20261015T014229Z.json`, each client pinned
to one replica through the VIP:

| Step | Result |
|---|---|
| T1: a job with 2 builds made on r0, renamed on r1 | on both replicas 1.1 s later with builds 1 and 2; old name gone on both; next build #3 |
| T2: renamed on r1 while it builds on r0 | 409: "#1 is running on jenkins-0; an item is not renamed or moved while it builds"; name unchanged on both; renamed at the first try once the build finished |
| T3: moved into a folder on r1 | on both 0.6 s later, builds 1-3 |
| T4: the folder renamed on r0, the job inside | on both 0.5 s later; build #4 started from r1 |

The rollout to netci32 itself was a Helm upgrade of the chart. The upgrade probe watching it died on
its own 120 s kubectl timeout (fixed in `phase0_probe.k`), so that run has no evidence. A
`--restart` roll measured afterwards is the evidence for netci32 (`aa-phase6-upgrade-20261015T014337Z.json`):
- the UI answered 356 of 356 reads;
- 624 creates were acknowledged, and none is missing on either replica;
- the two people signed in through the form made 270 creates, none refused;
- the two scripts sending a password were each refused once (403, ADR-086's known limit).

### What it costs, said plainly

- **A move rewrites every build of the item in the store.** Each archive is copied inside
  PostgreSQL, not over the network, but a job with thousands of builds makes one large transaction.
- **A build waits while its job or a folder above it is being edited on another replica.** An edit
  holds its lock for well under a second, so the build waits that long. A lock left by a dead replica
  holds it at most 30 s, until the lock expires.
- **Renames by script are checked after the fact.** A Groovy rename does not pass through
  `EditOrder`. If the store refuses it, the item is moved back.
- **netci-queue** dispatches by job name. A run queued for the old name is answered 404 by Jenkins
  after the rename. The IDP does not rename the jobs it creates.
- **Views were still not replicated** (done since, ADR-088).

## ADR-088: Root views are replicated; a job's pages reached through a view are ordered too

- Status: accepted; unit-tested, measured in the lab (below)
- Date: 2026-10-15
- Refines: ADR-076 (replicated homes), ADR-078 (owner routing)

### Context

A view at the top of Jenkins lives in Jenkins' own `config.xml`, together with the global settings
each replica gets from JCasC. That file is not replicated, so a view made on one replica never
reached the other: after a failover a person's views were gone. Other views were already
replicated:
- a folder's views are in the folder's configuration;
- a user's "My Views" are in the user's configuration.

A second defect was found while doing this. A job's pages are reached through the view a person is
looking at as well, `/view/team/job/app/configSubmit`; these are the links a view's own page gives.
`EditOrder` and `OwnerRouting` read only paths that start with `/job/`, so:
- an edit made there was not ordered at all;
- "Build now" there, on a replica that does not own the job, was declined without a word.

### Decision

1. **Each root view is an entry of the store, `@views/<name>`**, holding the view's own XML
   (`View.writeXml`).
   - A replica applies another's change with `View.updateByXml`, or with
     `View.createViewFromXML` + `Jenkins.addView`, or with `deleteView`.
   - The primary view is never deleted this way.
   - At start, a home publishes the views the store does not have, like its items. Views equal to
     the store's are held as applied, so they are not published again.
2. **A view request is ordered like an edit.** `EditOrder.editedView` maps the request to the view:
   - `/createView` (name);
   - `/view/<name>/` + `configSubmit`, `config.xml`, `submitDescription`, `doDelete`, `addJobToView`
     or `removeJobFromView`;
   - a view inside a view: the outer one.

   The view is locked, and the home catches up. After Jenkins has acted, still under the lock, every
   root view is compared with what this home last published or applied. What differs is published,
   and what is gone is published as deleted. This runs on the request's thread, not in a save
   listener: Jenkins saves holding its monitor, and applying another replica's view needs that
   monitor too.
3. **Other saves of Jenkins' configuration** (a list view following a renamed job, a script) compare
   the views a second later, on a timer, outside that monitor.
4. **`/view/<v>[/view/<w>…]` is skipped before a job's path is read**, in `EditOrder` and
   `OwnerRouting` alike.

### Tests

- **Plugin (JenkinsRule, fake store):**
  - a view made here is published;
  - its description set through the web is published under its lock, and its deletion is published;
  - another replica's view is made here, changed, deleted, and nothing is published back;
  - an edit through `/view/all/job/<j>/` is made under the job's lock.
- **`EditOrderTest`:** the view a request changes, nested views, `/view/<v>/job/<j>/…` for edits,
  moves and owner routing.

### Lab (controller netci34)

`lab/aa/phase10_views_probe.py`, `aa-phase10-views-20261015T024853Z.json`:

| Step | Seen on the other replica after |
|---|---|
| V1: a list view made on r0 (XML, as the API does) | 0.12 s |
| V2: a job added to it and its description set on r1 | 0.10 s |
| V3: a job's description set through `/view/<v>/job/<j>/` on r0 | 0.09 s |
| V4: the view deleted on r0 | 0.12 s |

### What it costs, said plainly

- **Which view is the primary one** stays each replica's own; JCasC sets it.
- **Every save of Jenkins' configuration** compares the root views a second later. It is a
  serialisation of each view, cheap for tens of views.
- **Two people editing the same view at once**, on two replicas, are ordered by its lock: the second
  is refused for a moment (409), as for jobs.

## ADR-089: A drain lets what is in flight finish; the balancers reload their configuration in place

- Status: accepted; measured in the lab (below)
- Date: 2026-10-15
- Refines: ADR-080 (drained rolling restart), ADR-085 (the chart)

### Context

The rolling upgrade to netci33 failed one UI read of 360. The lab holds 22,677 jobs, and its
dashboard takes 17 s to render. HAProxy's log showed what happened:
1. The draining replica's health check answered 503 (ADR-080).
2. The balancer marked it down.
3. `on-marked-down shutdown-sessions` closed the 17 s request in flight on it (`DCVN`).

`shutdown-sessions` is there for a machine that lost power: without it, an agent's WebSocket stays
open for the tunnel's hour, and the build it runs is not adopted. A drain is not that case, but the
balancer could not tell them apart.

Fixing it showed two defects in how the balancers are run:
- **The DaemonSet did not carry its configuration's digest.** A changed `haproxy.cfg` sat unread
  in the ConfigMap until someone restarted the balancers by hand.
- **Restarting a balancer takes the VIP down.** kube-vip elects the VIP's machine among the
  Service's endpoints, and restarts that election whenever an endpoint goes away. In the upgrade
  to netci34, replacing the balancers with a surge (a new pod ready before the old one stops) still
  refused connections for about 2 s: "existing endpoint has been removed, restarting
  leaderElection … Deleting VIP".

### Decision

1. **The plugin answers HAProxy's agent check** (`DrainAgent`, `NETCI_DRAIN_AGENT_PORT`, 8098). It
   says `maint` while `/tmp/netci-draining` exists and `ready` otherwise. In maintenance the balancer
   sends nothing new to the replica, cookie-pinned requests included, and lets what is in flight
   finish. With the agent, `/netci-ready/` stays 200 while the replica drains. Only a replica that
   is gone, or still starting, is marked down, and only then are its sessions closed.
2. **The preStop waits for what is in flight.** It:
   1. creates the drain file;
   2. gives the balancers 2 s (agent checks run every 1 s);
   3. polls `/netci-ready/busy` until no request is in flight, for at most `drainSeconds`.

   The count leaves out the drain's own polls and WebSockets (an agent's lasts hours). The chart
   refuses a grace period shorter than `drainSeconds` + 20.
3. **The balancers reload a changed configuration in place.** HAProxy runs master-worker under a
   loop that:
   - checks a changed file (the kubelet updates the mount within a minute) with `haproxy -c`;
   - reloads it with SIGUSR2, the old worker finishing its connections;
   - keeps a configuration HAProxy refuses out, and says so.

   The pod, its endpoint and the VIP stay. The image's stop signal (SIGUSR1, a soft stop) is passed
   on. A new balancer image still replaces the pods. For that the DaemonSet now surges (one new pod
   beside the old one on each machine) and the old pod leaves the endpoints 3 s before it stops
   listening.

### Measured in the lab

`phase6_upgrade_probe.py`: UI reads twice a second, two writers pinned by cookie, two people signed
in through the form.

| Run | Reads | Creates | Evidence |
|---|---|---|---|
| upgrade to netci33 (before) | 359 of 360: one cut in flight | 676, none missing | `…20261015T015907Z` |
| upgrade to netci34, balancers replaced (the digest) | 368 of 369: refused while the VIP moved | 523, none missing | `…20261015T023008Z` |
| the balancers' new command applied (replaced once more) | 223 of 223 | 374, none missing | `…20261015T023647Z` |
| a change of `haproxy.cfg` only: all 3 balancers reloaded in ~36 s, no pod replaced | 242 of 242 | 357, none missing | `…20261015T024008Z` |
| rolling restart, both replicas netci34 (the agent check in use) | 324 of 324 | 630, none missing; 308 form creates, none refused | `…20261015T024350Z` |

In the last run HAProxy logged `Server jenkins/r1 is going DOWN for maintenance … 0 sessions
active`, then `leaving forced maintenance` once the new pod's agent answered. Replacing the
balancers once more (third row) failed nothing, but replacing a balancer remains exposed to the VIP
moving; only the in-place reload avoids it.

The two scripts that send a password were still refused once each per roll (403, ADR-086's known
limit). The upgrade probe's r1 writer died in the netci34 run on a crumb it could not fetch (fixed:
the failure is now that request's answer), so that run has 20 of its writes.

### What it costs, said plainly

- **A stop waits for the slowest request in flight**, up to `drainSeconds` (30 by default). A rolling
  restart is that much longer per replica when someone is rendering a very large page.
- **The agent port is unauthenticated.** It answers one word and accepts nothing.
- **A new HAProxy image still replaces the balancers**, and with kube-vip's per-endpoint election
  the VIP can move once per machine. With an Octavia LoadBalancer the endpoints are health-checked
  by the load balancer instead.
- **A dashboard of 22,000 jobs takes 17 s**, and a person's first page after a replica restarts
  (cold) took 100 s in one run. That is Jenkins with that many top-level items, replicated or not;
  folders and views keep a page small.

## ADR-090: The Portal shows delivery as it runs: build once, promote the digest

- Status: accepted; unit-tested, browser-tested against the API (below)
- Date: 2026-10-15
- Refines: ADR-043 (CI and CD are separate acts), ADR-038/039 (production by request)

### Context

The server has delivered in the production way since ADR-043:
- a repository event builds a commit into one signed digest;
- a module's rules decide whether that build deploys, and to where;
- moving the digest onward is a promotion, which builds nothing, and is allowed once the
  environment before it is healthy (and has soaked);
- production is reached only through a production request that a second person approves.

The Portal still showed the model from before that. A module had one "pipeline" per environment
(Dev, Staging, Prod), each with a "Run Pipeline" button that built the commit again and deployed it
there. Production was offered as a build target "after approval". A person reading the module page
could not tell what was running where, which digest it was, or what stood between staging and
production. The user called the CI/CD "like a demo, not production". The page was why.

Reviewing it showed three more places where the Portal said something that was not so:
- **"Success rate 100%, 1 successful"** for a single run that was still queued. The dashboard
  counted every run that had not failed as a success.
- **A system marked "degraded"** because a build was queued, before anything had been deployed.
  A build says nothing about a service's runtime.
- **The demo data registered versions with a CI report** (42 tests passed, 94.5% coverage,
  "created by netCI Pipeline") that no build had produced. The Portal then showed that report
  beside "not deployed" everywhere.

### Decision

1. **The module page is organised the way delivery runs.** Tabs: Delivery, Builds, Releases,
   Configuration, Metrics.
   - **Delivery:** the promotion path, Development → Staging → Production. For each environment:
     what runs there (version or digest, commit, when, strategy, approver) and the gate that admits
     the next one.
     - "Promote" moves the digest that runs in development: the run recorded on that environment,
       `POST /modules/{id}/promotions`.
     - Production offers only "Request release", which opens production requests.
     - Below: the evidence of the latest artifact (signature, SBOM, vulnerabilities, tests), the
       recent builds and deployments, and the rules in force, written as "when … then …".
   - **Builds:** every run in one list, with its trigger (push, pull request, tag, manual), commit,
     artifact, and where it went. Filters: deploys, build only, PR verify.
   - **"Run build":** picks a branch and a commit, then either deploys to the first
     non-production environment or only builds. It never offers production. A module with only
     production configured gets "build only".
2. **The gate shown is a hint; the server decides.** `promotionGate` reads the module's promotion
   rule:
   - nothing deployed before → blocked;
   - not healthy → blocked;
   - soaking → minutes left;
   - otherwise ready.

   The server checks again when the promotion is asked for, and its refusal is shown as it was
   given.
3. **Run rates are over finished runs.** `/portal/dashboard` reports succeeded, failed,
   in-progress and cancelled runs. The success and failure rates are over succeeded + failed
   only. The schema is now in OpenAPI (`PortalDashboardKpis`). The Portal shows "—" until a run has
   finished.
4. **A system's health is its deployments'.** Builds no longer move it. With nothing deployed it is
   `unknown`.
5. **Demo versions say what they are.** They are still seeded with `NETCI_DEMO_DATA`, so a demo can
   walk a production request and its second approver without a CI server. Their metadata is
   `createdBy: demo seed`, `demo: true`, with no digest and no CI report, so the Portal shows their
   evidence as not reported. A re-seed adds the versions an earlier seed did not write.
6. **One design system** (`theme.css` tokens, `ui.css` components) with a light and a dark theme,
   following the system's or the person's choice:
   - 233 hard-coded colours became tokens;
   - text is no smaller than 11 px (it went down to 8 px);
   - every text token meets WCAG AA on its surfaces in both themes.

   The streamlined navigation of fca849d is kept: Release Plan, Scorecards, Stage Catalog and CI
   Cost are reached from search.

### Tests

- **Backend:**
  - a queued run is neither a success nor degraded health;
  - a cancelled run is counted as cancelled;
  - the SoD test registers the version it names instead of relying on the seed.
- **Unit (vitest, 124):**
  - the rules panel, including `cancelInProgress` and path filters;
  - `promotionGate`: blocked, not healthy, soaking (20 of 30 minutes), ready;
  - the Builds list for admission and supersession (ADR-050);
  - "Run build" never offering production.
- **Browser (Playwright against the API on its test database):** 41 passed. 6 skip, as before,
  because they need Keycloak. The specs now drive Builds → Run build, and they pick a system by
  name rather than by its place in a sorted list.
- **Full gate:**
  - `pytest backend/tests tests/contract`: 1509 + 62 passed;
  - `pyflakes` clean;
  - `migrate.py --check-schema`;
  - `npm test && npm run build`.

### What it costs, said plainly

- **The promotion path reads the module overview's environments.** An environment the module
  does not configure still shows as a lane, "not deployed".
- **"Request release" opens production requests;** it does not fill the request in.
- **Old screenshots and the defense deck show the per-environment pipeline cards.** They describe
  the Portal before this ADR.

## ADR-091: The IDP delivers on the active-active replicas, end to end

- Status: accepted; run live on the HA lab (below)
- Date: 2026-10-15
- Refines: ADR-082 (IDP through netci-queue), ADR-043 (build once, promote), ADR-085 (the chart)

### Context

The IDP's last full live delivery (rc20, September) ran on the systemd lab, against two single
Jenkins controllers. That stack is stopped, and the host has no memory to run it beside the HA lab.
ADR-082 had sent a build from the IDP to the active-active replicas, but the build failed when it
loaded the pipeline, because the replicas had no netCI shared library. Nothing had built, signed,
deployed or released an application on them.

The user asked whether the CI/CD "still works". The honest answer needed a run on what exists now.

### Decision

1. **The replicas get what the pipeline needs** (`lab/aa/casc-ci.yaml`, merged by
   `lab/aa/idp.sh casc`):
   - the shared library, from the corp GitLab;
   - credentials, read from files (`${readFile:...}`) mounted by the chart's new
     `jenkins.extraSecretMounts`, never written into the ConfigMap;
   - a Kubernetes cloud whose build pods run in `aa-build`. The chart's new `buildPods` gives the
     replicas a Role there, and the pods' own account no token.

   Each replica's pods dial back to that replica (`${POD_NAME}`): a pod is an agent of the
   controller that created it, not of the balancer.
2. **The build container runs under its own AppArmor profile** (`deploy/apparmor/netci-builder`).
   Rootless Buildah unpacks layers in a user namespace it creates. On Ubuntu 24.04 nodes the
   runtime's default profile denies mount, and `kernel.apparmor_restrict_unprivileged_userns=1`
   strips the namespace of capabilities; the first build failed so ("remount /: permission denied").
   - The profile grants `userns` and `mount`. The kernel bounds both to the build's own namespace.
   - The node's guard stays on for every other process.
   - It must be loaded on each build node.
3. **The API reads private repositories** with `NETCI_GIT_CREDENTIALS_FILE`: a git credential-store
   file holding a read token. Any helper of the API's user is reset first, and the token is never
   an argument or part of a URL. Before this, the API read repositories anonymously, so for a
   private repository the run dialog listed no branch and no commit. A file that is set but
   unreadable is refused, not ignored.
4. **The Kubernetes playbooks use the kubeconfig netCI names, with its own current context.** They
   forced `kind-netci-local`, the first lab's cluster, so they could not reach any other cluster.
   The worker reaches the cluster as `netci-deployer`: `admin` in `dev`, `staging` and `prod`,
   nothing else (`lab/aa/idp.sh deploy-prepare`).
5. **The defects the live run found are fixed where they were:**
   - **The sample chart enabled KEDA autoscaling by default**, so any cluster without KEDA refused
     the whole release. It is now off by default.
   - **The worker's cosign did not trust the registry's CA.** The TLS attempt failed and
     go-containerregistry fell back to plain HTTP on an RFC 1918 address. The worker is now given
     the CA (`SSL_CERT_DIR`), as the build pods are.
   - **netCI's own Jenkins plugin was undeclared in the toolchain**, so the controller netCI ships
     failed its own gate under `NETCI_TOOLCHAIN_ENFORCE=enforce`. It is now declared as
     `jenkins.ownPlugins` (checked, never sent to the update site) and kept equal to the plugin's
     `pom.xml` revision by a test.
   - **The module views omitted what a run was.** A build-only run read "deploys to dev", and a
     production request's release run, which builds nothing, read "manual build". All routes now
     answer with one run projection (`projections/runs.py`), which says `promotedFrom` and
     `productionRequestId`.
6. **The scan's Trivy DB is mirrored on a timer** (`netci-trivy-db-mirror`, now enabled). The lab's
   IDP runs with `NETCI_TOOLCHAIN_ENFORCE=enforce`.

### Measured on the HA lab

`lab/aa/idp_e2e.py` drives the IDP's public API as people do, signed in through Keycloak: dana is a
developer, rae a reviewer. The evidence is in `lab/evidence/aa-idp-e2e-*.json`.

| Step | Result |
|---|---|
| Build `payments-api` 545d56a (build only), through netci-queue on the replicas | checkout, unit tests, build, SBOM, scan, sign and publish all succeeded; digest `sha256:f28ea2ce…`; signature and provenance verified by the IDP (`…055522Z`) |
| Promote that digest to dev, then to staging | both healthy; no rebuild; the pods run `apps/payments-api@sha256:f28ea2ce…` |
| Register v1.0.0; dana requests production; dana approves her own request | refused, 403 |
| rae approves | production deployment healthy (`…055944Z`) |
| A push to GitLab main (b0920c6) | the GitLab webhook made a run: event `push`, rule 0, deploy to dev |
| The same build under `enforce` | refused at first: netCI's own plugin was undeclared (decision 5) |
| Retried after the fix | all 9 stages, deploy and health check included; dev healthy on `sha256:f770e5bc…`, while staging and prod stay on v1.0.0 (`…061405Z`) |

Later the same day the IDP's commit statuses reached GitLab: `netci/pipeline=success` on 355cfd6, linking to
the Jenkins build. Before that, all 22 statuses had been dead-lettered, each with its reason, because no
token was configured. GitLab sets a status on a protected branch only for a user who may push or merge
to it, so a Developer token was refused 403. The lab now uses a Maintainer project access token for
payments-api only.

The rolling upgrades that applied the chart changes failed nothing: 329 of 329 reads,
`aa-phase6-upgrade-20261015T053255Z.json`.

### What it costs, said plainly

- **The pipeline needed Kubernetes build pods.** `netciPipeline` declared `agent { kubernetes { … } }`,
  so it could not run on inbound agents on a VM, which is the mentor's layout. ADR-092 added that.
- **A build pod is an agent of one replica.** If that replica dies mid-build, the build is not
  adopted (ADR-079 adopts builds on inbound agents, which reconnect). It fails and is retried.
- **The builder's AppArmor profile is node configuration.** A node without it cannot run a build.
- **The lab's environments are three namespaces of one cluster**, and its production approval was
  given within a minute. Real separation of clusters and real change windows are a site's
  configuration.
- **Build isolation is `none` on the lab.** Every build shares `aa-build`; per-project namespaces
  (ADR-030) were not exercised.

## ADR-092: The pipeline runs on a VM's inbound agents too

- Status: accepted; run live on the HA lab (below)
- Date: 2026-10-15
- Refines: ADR-091 (which listed this as not done), ADR-007 (one pod per build), ADR-079 (adoption)

### Context

The mentor's layout puts the controllers on Kubernetes and the build agents on one large OpenStack
VM, connected inbound. `netciPipeline` declared `agent { kubernetes { … } }` and nothing else, so on
that site the IDP's pipeline could not run at all. ADR-091 said so.

Declarative Pipeline fixes its `agent` when the script is parsed, so one pipeline cannot pick
between a pod and a label at run time. A second copy kept by hand would be 500 lines that drift
from the first. Every past fix to a stage (evidence, signing, callbacks) would have had to be made
twice.

### Decision

1. **The operator chooses where builds run, with `NETCI_AGENT_KIND`:**
   - `kubernetes` (the default) gives each build its own pod;
   - `static` runs builds on inbound agents labelled `NETCI_AGENT_LABEL` (the VM's).

   The value is written into the job netCI generates (`agentKind: '…'`), never into a build
   parameter, so no run, browser or webhook can change it. Any other value stops startup. `static`
   together with `NETCI_BUILD_ISOLATION=kubernetes` also stops startup: a static agent is in no
   per-project namespace, so the pair would claim an isolation no build had. The pipeline refuses
   the same combination again if a namespace parameter reaches it.
2. **The second pipeline is generated, not written.** `vars/netciPipelineOnAgents.groovy` is
   `vars/netciPipeline.groovy` with two changes:
   - the `netci:agent` block is replaced by `agent { label agentTemplate }`;
   - the `netci:dispatch` block is removed.

   `scripts/sync_shared_library.py` writes the copy and its `--check` fails CI when it is stale. A
   test asserts that both files have the same stages.
3. **On a long-lived agent each build gets its own image storage**, under the build's temporary
   directory, and removes it when it ends (`netciInBuilder`, `netciInBuilder.dropStorage()`).
   Rootless Buildah's default storage is in the agent's home. After the first build on the lab it
   held 233 MB, and every later build would have added to it until the VM's disk filled. It also
   let a later build of another module find this build's images already there, its base-image tag
   included. Pods never had this problem, because their storage dies with them.
   - The storage is set with `XDG_CONFIG_HOME`, because rootless Buildah 1.39 ignores
     `CONTAINERS_STORAGE_CONF` (measured).
   - The storage keeps the toolbox's `ignore_chown_errors`. Without it the first base image failed
     where `newuidmap` is denied ("insufficient UIDs or GIDs … requested 0:42").
   - The workspace is wiped too, by the `cleanWs` the pipeline already ran on agents that are not
     disposable.
4. **What the VM needs** is what the toolbox image (`jenkins/agent-toolbox/Dockerfile`) has:
   - an inbound agent with buildah, syft, trivy, cosign, git and python3;
   - the registry's CA in `/etc/containers/certs.d/<registry>/` and in `SSL_CERT_DIR`;
   - on Ubuntu 24.04, the builder's AppArmor profile (ADR-091), or user namespaces allowed for
     the agent's user.

   An active-active pair needs one agent per replica, each sent home by HAProxy (`agents.homes`):
   an inbound agent belongs to one controller.

### Measured on the HA lab

The lab's two inbound agents (`agent-0` and `agent-1`, which stand for the VM) were given the
toolbox image (`lab/aa/idp.sh vm-agents`) and the label `netci-vm`. The library on the lab's GitLab
was taken to `netci-0.5.3`.

| Run | Result |
|---|---|
| GitLab push faf3e7b, `NETCI_AGENT_KIND=static` | "Running on agent-0"; no pod in `aa-build`; all 9 stages; dev healthy (`aa-idp-e2e-20261015T062914Z.json`) |
| A second push (760cbe0) | Failed in Build: the first per-build storage lacked `ignore_chown_errors` (decision 3) |
| Retried after the fix | All 9 stages; dev healthy; afterwards the workspace was empty and the build's storage gone (`…063911Z`) |
| `NETCI_AGENT_KIND=kubernetes`, the same library | "Created Pod: netci-builds aa-build/…", build succeeded: the pod path still works (`…064106Z`) |

### What it costs, said plainly

- **A static agent is shared.** Builds on it are not isolated from each other the way pods are:
  separate workspaces and image storage are what keeps them apart, and a build that escapes them
  could affect the next one. A site that needs per-project isolation uses `kubernetes`.
- **Adoption is not yet shown with this pipeline.** ADR-079 proved that a build on an inbound agent
  survives its replica's death, but it proved it with a shell loop, not with the IDP's pipeline.
- **Two agents on one VM is not two machines.** If the VM is lost, every build on it is lost, and
  nothing in netCI can change that; it is the site's choice of one large VM.

## ADR-093: Adoption tested with the IDP's own pipeline -- what holds and what does not

- Status: accepted (the fence fix); the copy problem is solved by ADR-094 (4 of 4 adopted IDP builds succeeded)
- Date: 2026-10-15
- Refines: ADR-079 (adoption), ADR-084 (strict fence), ADR-092 (static agents)

### Context

ADR-079 measured adoption with `long`, a pipeline of 90 one-second `sh` ticks. When its replica's
pod was killed, the surviving replica took the build over in 4.3 s. The mentor's requirement is
"no lost builds", and the build that matters is the IDP's: about 9 stages, each wrapped in
callbacks that bind a credential and run a short `sh`. `lab/aa/idp_adoption_probe.py` starts a
real build of payments-api through the IDP on the lab's inbound agents and kills the replica that
owns it.

### Decision: the fence no longer misses a renewal written in its successor's first second

Kubernetes reports a container's `startedAt` in whole seconds. After a force-delete, the old
process lived on for a second or two and wrote one last copy of its running build. When that copy
fell in the second its successor's container started, "started after the renewal" could never be
true. No survivor adopted the build; it waited 50-60 s for the restarted replica to boot and take
it back itself. This happened in 2 of 4 kills before the fix.

In that second, the renewal can only be the old process's: a new JVM has written nothing yet. The
fence now counts the old process as gone once it has been silent for two copy periods plus a second
(`SAME_SECOND_SILENCE_MS`, 7 s). The rule applies in both modes, and only while the node is Ready
and reporting. `FenceTest` covers it; it ships in controller `2.555.3-netci35`.

### Measured on the lab (controller netci35, pod of the owner killed, no grace)

| Build | Adopted by the survivor | End |
|---|---|---|
| `long` #67 | after 9.6 s | SUCCESS, all 90 ticks once |
| `long` #68 | after 4.6 s | SUCCESS, all 90 ticks once |
| `long` #69 | after 4.2 s | **ABORTED**: 30 s after adoption the agent's channel stopped (the replica's ping failed). The agent's writer stayed blocked, so it noticed only at TCP's broken pipe, 5 min later, which is the build's wait for its agent |
| IDP payments-api #9 (before the fix) | no: the restarted owner took it back after 61 s | succeeded, signed, policy allow |
| IDP #12, killed during Build | after 17.9 s | **hung**: resumed from a copy taken before the old process's last steps, in an `sh` whose control directory that process had already removed. Cancelled through the IDP after 15 min |
| IDP #13, killed during the scan | after 6.3 s | **failed**: `SynchronousResumeNotSupportedException: withCredentials cannot be resumed after a controller restart`. The IDP run ended failed, honestly |

Evidence: `lab/evidence/aa-phase5-20261015T07*.json` and `aa-idp-adoption-20261015T0*.json`.

### What this establishes, said plainly

- **A build made of long steps survives its replica's death and is adopted in seconds.**
- **The IDP's pipeline does not, reliably.** In 2 of 3 kills it was adopted and then failed or hung.
  It is never reported as a success it did not have: the run ends failed, or stays running until
  cancelled, and a retry rebuilds it. But "no lost builds" is not established for it.

There are two causes.

1. **A copy is not taken at a point the pipeline can resume from.** Copies are taken every 3 s, and
   the old process runs on for a second or two after it is "dead". So the adopted state can be:
   - inside a step that cannot resume (`withCredentials` while it binds), or
   - behind steps the old process already finished and cleaned up (an `sh` whose control
     directory is gone).

   The IDP's pipeline runs dozens of such short steps per build, so it is far more exposed than
   `long`.
2. **An inbound agent can take minutes to notice its channel is dead**, when its writer blocks.

### Directions, not done

- Take a copy at step boundaries: a graph listener on each new flow node, alongside the timer.
  Never take one while a step that cannot resume is starting, so that the copy is always resumable.
- Bind credentials to fewer, longer blocks. The callback token, though, stays bound only around
  the callback: user code (unit tests) runs on the same agent and could forge results with it.
- Wrap the node in `retry(conditions: [nonresumable(), agent()])`. Its first attempt still reports
  "failed" to the IDP before the retry, so the IDP's run model has to learn "retrying" first.
- On the agent, a socket write timeout, so a blocked writer is found in seconds, not at TCP's
  give-up.

## ADR-094: A running build's copy is one it can be resumed from

- Status: accepted; measured on the lab (below)
- Date: 2026-10-15
- Refines: ADR-079 (adoption), ADR-093 (which found the IDP's pipeline not surviving)

### Context

ADR-093 killed the replica owning a real IDP build. The build was adopted, then it either failed
with `SynchronousResumeNotSupportedException` (`withCredentials`) or hung for good. Jenkins'
own sources show both causes:

- **`GeneralNonBlockingStepExecution` keeps its background thread's name in the saved program**
  (`threadName`), and `onResume` fails the step while it is set. `withCredentials` binds and unbinds
  in such a thread, and the IDP's pipeline does that around every callback. A copy taken in that
  moment cannot be resumed. A restart could not resume there either: Jenkins does not restart then
  (`StepExecution.blocksRestart`).
- **durable-task's watcher on the agent removes a step's control directory** right after it has
  given the result to its controller (`handler.exited(...)`, then `controller.cleanup(...)`). A
  watcher started on a directory that is gone gives up without a word ("giving up on watching
  nonexistent"). The copy was taken every 3 s, and an IDP build runs a dozen short `sh` steps in
  that time. Adopted from a copy taken before a step ended, the build waited for that step forever.

### Decision

1. **No copy while the run blocks a restart.** This is Jenkins' own test:
   `Executor.getAsynchronousExecution().blocksRestart()`, which is true while the CPS VM is busy or
   a step is running background code. It is asked before and after the directory is read. The
   previous copy stands, and the next comes at the next step boundary.
2. **A copy at every step boundary.** A `GraphListener` asks for a copy on each new flow node. The
   copy is made 100 ms later on a timer thread, never on the CPS VM thread, and once per burst of
   nodes; the 3 s timer stays. A copy is at most a fraction of a second behind the build, not up to
   3 s. Copies of one run are made one at a time.
3. **A step that ended after the last copy fails with its reason; it is not waited for and not
   guessed.** When the copy's step has no control directory on its agent any more, the adopter
   writes the directory back before the run is loaded:
   - a log line saying what happened;
   - exit code 75 (EX_TEMPFAIL).

   The step fails now, and the build can be run again. Its real result is lost with the replica
   that had it, and nothing invents one.

### Tests, written with it (`CopyPointsTest`)

- **A build stopped while a step runs background code is adopted from the copy before it and
  succeeds.** With the guard switched off, the same test fails with the lab's
  `SynchronousResumeNotSupportedException`.
- **Each of four step boundaries reaches the store's copy within a second.** With the listener off,
  the first is 2.6 s late.
- **A step whose control directory is gone gets exit 75 and its reason.**

### Measured on the lab (controller `2.555.3-netci36`)

`lab/aa/idp_adoption_probe.py --kill pod` was run four times, each killing the replica that owned
a real build of payments-api on the lab's inbound agents at a different stage, with no grace.

| Killed during | Adopted by the survivor | Run in the IDP | Artifact |
|---|---|---|---|
| unit-test (#14) | after 24.2 s | succeeded | signed, policy allow |
| build (#15) | after 15.1 s | succeeded | signed, policy allow |
| vulnerability-scan (#16) | after 11.0 s | succeeded | signed, policy allow |
| sign (#17) | after 5.2 s | succeeded | signed, policy allow |

Each build was resumed once, and `build.sh` and `scan.sh` each ran once. In #15, decision 3
applied, to the storage-setup `sh` inside a stage callback. The callback is best effort, so it
logged "stage report skipped: exit code 75" and the build went on; before this ADR that step was
waited for forever. Evidence: `lab/evidence/aa-idp-adoption-20261015T095*.json`. ADR-093's three
kills had given one hang and one failure.

### What it does not solve

- **The window is narrowed, not closed.** A step can still end after the last boundary copy, in the
  fraction of a second before its replica dies. That case now fails at once with its reason
  (decision 3). It no longer hangs.
- **An inbound agent whose channel writer is blocked** still takes minutes to notice (ADR-093,
  `long` #69).
- **A step adopted from a copy before it ended runs again**, from its start or from its agent's
  mark. An `sh` with side effects must be safe to repeat; netCI's callbacks and build scripts are.


---

# Bước 5 — Đọc code (mở trong trình soạn thảo, đọc kèm test)

> **Tuần 3.** Không cần đọc hết repo. Bốn chỗ này chứa phần cốt lõi; đọc test trước để biết code phải làm gì.

## Bốn chỗ cần đọc trong code

| File | Đọc để hiểu | Tìm gì |
|---|---|---|
| `internal/supervisor/decide_test.go`, rồi `internal/supervisor/decide.go` | Toàn bộ chính sách tiếp quản: khi nào nghi, khi nào fence, khi nào không làm gì | Tên từng test là một quy tắc (ví dụ panic guard đếm theo máy). `Decide` là hàm thuần: vào là bức tranh, ra là danh sách hành động |
| `internal/lease/holder.go` (+ `lease_test.go`) | Cell agent giữ Lease thế nào, và tự dừng trước hạn ra sao | `renewDeadline`, cập nhật có điều kiện theo `resourceVersion` |
| `jenkins/plugin/src/main/java/io/netci/jenkins/NetciEndpoint.java` (+ `RunLookup.java`) | Dispatch không trùng | Mọi việc làm dưới `Queue.withLock`; tìm run ở hàng đợi, executor và build gần đây trước khi xếp lịch |
| `jenkins/plugin/src/main/java/io/netci/jenkins/once/NetciOnceStep.java` | `netciOnce` | Ghi `(build, key, nonce)` trước khi chạy thân khối; nonce khác thì từ chối; không gọi được queue thì không chạy |

Chạy test của supervisor để xem tên các quy tắc:

```bash
go test ./internal/supervisor -v -run . 2>&1 | grep -E "^(=== RUN|--- )" | head -40
```


---

# Bước 6 — Chuẩn bị bảo vệ

> **Tuần 4.** Thuộc bảng con số (mục BV2), tập dàn bài (mục BV3), luyện trả lời câu hỏi (mục BV5). Số trong ngoặc như (8.4) là mục của tài liệu trình bày, ở Bước 1–3 phía trên.

## BV1. Thông điệp cốt lõi

**Một câu**: netCI làm cho Jenkins mã nguồn mở sống sót khi mất máy (build đang chạy chạy tiếp, hàng đợi không
mất, bước nguy hiểm không chạy hai lần, không bao giờ có hai controller cùng ghi) mà không phải mua CloudBees,
và chứng minh điều đó bằng hơn 80 lần rút điện thật cùng một soak 3 giờ.

**30 giây**: Jenkins mã nguồn mở có một controller duy nhất, giữ hàng đợi trong RAM và ghi đĩa qua page cache.
Mất máy là mất build, mất hàng đợi, có khi một bước deploy chạy hai lần. netCI bọc Jenkins bằng bốn thứ:
- controller trên Kubernetes với đĩa nhân bản;
- supervisor xác nhận máy đã tắt qua BMC rồi mới chuyển controller, trong khoảng 3 giây;
- hàng đợi bền trong PostgreSQL;
- bước `netciOnce` từ chối thay vì chạy lại khi không chắc.

Sandbox build được cấp bởi một agent fabric riêng. Tất cả chạy được trên lab 3 VM và có bằng chứng cho từng
khẳng định.

**2 phút**: 30 giây ở trên, thêm:
- **Vì sao "một người ghi, chuyển nhanh"**: Jenkins không chia sẻ được trạng thái giữa hai JVM, nên netCI không
  cố chạy hai cái. Nó đảm bảo đúng một cái chạy và chuyển cái đó thật nhanh, an toàn.
- **Ba lớp chống split-brain**: Lease có hạn chót tự dừng; fence bằng tắt nguồn trước khi chuyển; volume RWO.
- **Kết quả**: fence 2,3–3,9 s; build chạy tiếp sau trung vị 43 s; 0 split-brain; mỗi `netciOnce` đúng một lần.
- **Giới hạn**: UI tắt 40–60 s khi chuyển; chưa thử trên server và BMC thật.

---

## BV2. Những con số phải thuộc

| Nội dung | Con số | Nguồn |
|---|---|---|
| Lần mất điện không người can thiệp | Hơn 80, mọi build SUCCESS | ADR-060, `chaos-poweroff-*.json` |
| Soak 3 giờ, lỗi ngẫu nhiên liên tiếp | 38/38 PASS | `soak-20261011T125323Z.json` |
| Fence sau mất điện | 2,3–3,9 s | soak |
| Fence sau máy treo | 16,5–20,5 s (chờ Lease 15 s hết hạn, rồi ForceOff) | soak |
| Build chạy tiếp sau mất điện | 34–67 s, trung vị 43 s | soak |
| Split-brain | 0 | mọi bằng chứng chaos |
| `netciOnce` giữa lúc mất điện | 51 lần, mỗi lần đúng 1 marker | ADR-065 |
| Quy mô (kwok) | 600 cell / 200 máy: quan sát 0,1 s, fence 2,7–2,9 s, 0,16 core, 53 MiB | `scale-*.json` |
| Thời gian một build qua fabric (lần lượt / 4 cùng lúc) | 3,88 s / 4,82 s (Kubernetes plugin: 3,17 s / 6,17 s) | ADR-072 |
| Jar cache | 19 MB, 15 jar; A/B 3,38 s vs 4,66 s, 0 vs 2 lần kẹt 15 s | ADR-072 |
| UI tắt khi tiếp quản / khi nâng cấp | 40–60 s / 19–42 s | demo, ADR-071 |
| Cảnh báo | 16, mỗi cái có runbook | `monitoring.yaml` |
| Quy mô mã | Go 9 015 dòng + 6 298 dòng test (193 test); Java 1 283 + 758 dòng test (24 test); 16 probe lab; 323 file bằng chứng | repo |
| ADR | 72 (060–072 cho bản HA hiện tại) | `docs/decisions/` |
| Demo tập dượt (cell-b) | fence 3,2–3,6 s, Lease mới 20–30 s, Jenkins lên 31–41 s, build chạy tiếp 32–43 s, SUCCESS 114 s | `demo-power-loss-2026101*.json` |

---

## BV3. Dàn bài thuyết trình 15–20 phút

Mỗi slide một ý. Câu in nghiêng là câu nên nói.

| # | Slide | Nội dung, hình | Thời lượng |
|---|---|---|---|
| 1 | Tên đề tài | netCI: HA và agent fabric cho Jenkins mã nguồn mở. Tên, người hướng dẫn | 0:20 |
| 2 | Một đêm Jenkins sập | Bảng 1.1 của TB rút còn 5 dòng: không vào được, build mất, hàng đợi mất, deploy chạy hai lần, khôi phục hàng giờ. *"Đây không phải giả định: JENKINS-30909 là lỗi mở của Jenkins."* | 1:30 |
| 3 | Vì sao không mua CloudBees | Bản quyền, plugin bị giới hạn khi HA, muốn giữ Jenkins mã nguồn mở | 0:45 |
| 4 | Yêu cầu và tiêu chí | R1–R6 và ngưỡng: tiếp quản dưới 5 phút (mục tiêu 1 phút), 0 split-brain, 0 trigger mất, `netciOnce` 0 lần chạy hai lần | 1:30 |
| 5 | Quyết định gốc | Active/active bị loại (Jenkins giữ trạng thái trong RAM) → **một người ghi, chuyển nhanh**. *"Đừng bắt Jenkins chia sẻ thứ nó không chia sẻ được."* | 1:30 |
| 6 | Kiến trúc | Hình 8.1 của TB: cell (Jenkins + cell agent + volume nhân bản), supervisor, queue, fabric | 2:00 |
| 7 | Ba lớp chống split-brain | Lease tự dừng trước hạn; fence bằng tắt nguồn rồi mới chuyển; RWO. *"Muốn split-brain thì cả ba phải cùng sai."* | 1:30 |
| 8 | Một lần mất điện, từng giây | Bảng 9.2 của TB, hoặc chuyển ngay sang demo | 1:30 |
| 9 | **Demo trực tiếp** | Mục BV4 | 3:00 |
| 10 | Không mất hàng đợi, không chạy hai lần | netci-queue (nhận là bền, dispatch idempotent), `netciOnce` (từ chối khi không chắc) | 1:30 |
| 11 | Agent fabric | Pod ấm, bind muộn, Kata cho code không tin cậy, jar cache: 15 s → 0 | 1:15 |
| 12 | Kiểm chứng | Các tầng test; hơn 80 lần mất điện; soak 38/38; quy mô 600 cell. *"Mỗi test phải đỏ trên code cũ."* | 1:30 |
| 13 | Bài học | Chọn 3: page cache (nhân bản ≠ an toàn), hai đồng hồ trong một phép trừ, 15 s bí ẩn (thread dump) | 1:30 |
| 14 | Giới hạn và hướng phát triển | Mục BV6 | 1:00 |
| 15 | Kết luận | Câu ở mục BV1 | 0:20 |

Mẹo:
- Slide 2 kể bằng câu chuyện, không đọc bảng.
- Slide 6 chỉ vẽ những gì sẽ nhắc lại về sau.
- Demo đặt giữa bài, lúc hội đồng còn tập trung; nếu demo hỏng thì chuyển ngay sang phương án dự phòng (BV4.5).

---

## BV4. Kịch bản demo trực tiếp

### BV4.1 Ý tưởng

Rút điện thật máy đang chạy Jenkins controller, giữa lúc một build đang chạy bên trong `netciOnce`. Màn hình in
từng mốc. Build chạy tiếp và kết thúc SUCCESS, không mất dòng log nào, đúng 1 marker. Không ai chạm tay vào gì
sau khi rút điện.

### BV4.2 Chuẩn bị

**Trước một ngày**:
1. Làm Bước 10 của TB (sau khởi động lại máy chủ): VM running, đồng hồ đúng, Harbor (`curl -k
   https://172.17.0.1:8930/v2/` trả 401), `netci-queue-pg`, Redfish (`lab/redfish.sh start`), Loki.
2. Đĩa máy chủ còn trên 20 GB: `df -h /`.
3. Chạy tập một lần: `python3 lab/demo/power_loss_demo.py --cell cell-b --yes`. Phải ra SUCCESS, không thiếu tick,
   1 marker.
4. Quay màn hình lần tập đó (OBS hoặc `asciinema rec`) làm phương án dự phòng.

**Trước 30 phút**:
1. `kubectl get nodes` đủ 3 Ready; volume Longhorn healthy.
2. Hai cell ở **hai máy khác nhau**. Script tự kiểm và tự từ chối nếu chưa; cách sửa:
   `kubectl -n cell-a delete pod jenkins-0`, chờ Ready.
3. Mở sẵn ba cửa sổ:
   - A: terminal chạy demo;
   - B: `watch -n1 'kubectl get pods -A -o wide | grep -E "jenkins-0|NAME"; kubectl get nodes'`;
   - C: trình duyệt, Jenkins của cell-b (`http://192.168.122.21x:30081`), sẵn sàng mở console của build.

**Dùng cell-b**, không dùng cell-a. Cell-a mount `sync,dirsync` (cấu hình đối chứng): mỗi bước Pipeline mất
khoảng 4 s, và build demo kết thúc muộn 100–150 s sau khi tiếp quản (đã đo; INSTALL khuyến nghị `commit=1`).

> **Hướng dẫn demo thủ công từng bước** (gồm cả hàng đợi, fabric, Loki, cảnh báo): `docs/HUONG-DAN-DEMO-THU-CONG.md`.
> Trước giờ demo ít nhất 30 phút: `python3 lab/demo/power_loss_demo.py --arrange-only`.

### BV4.3 Chạy

```bash
export KUBECONFIG=$PWD/.netci-gate/lab/kubeconfig
python3 lab/demo/power_loss_demo.py --cell cell-b --duration 120
# Script kiểm điều kiện, khởi động build once-probe, chờ tới tick 8, in link console rồi dừng chờ Enter.
# Mở link console ở cửa sổ C cho hội đồng thấy các dòng "tick N". Nhấn Enter.
```

### BV4.4 Lời dẫn theo từng dòng (số thật của lần tập 2026-10-12)

| Màn hình in | Nói |
|---|---|
| `+0.0s netci-lab-1 mất điện` | *"Em vừa rút điện máy đang chạy Jenkins. Trang Jenkins sẽ không trả lời nữa."* |
| `+3.2s Supervisor: ... máy Off → nhả Lease` | *"Sau 3 giây Lease không được gia hạn, supervisor không đoán: nó hỏi bộ điều khiển nguồn (Redfish), và chỉ khi được báo máy đã Off mới nhả quyền."* |
| `+3.2s ... taint out-of-service` | *"Taint này cho Kubernetes biết máy đã tắt hẳn, nên được phép gỡ đĩa mà không chờ máy chết trả lời. Không có netCI, pod sẽ kẹt Terminating mãi."* |
| `+8.6s ... jenkins-0 mới được đặt lên netci-lab-2` | *"Pod controller mới ở máy khác, ổ đĩa nhân bản đi theo."* |
| `+30.2s Cell agent mới giữ Lease (epoch 230 → 231)` | *"Chỉ ai giữ Lease mới được chạy Jenkins; số epoch tăng mỗi lần đổi chủ."* |
| `+41.1s Jenkins mới đã lên` | *"Jenkins đọc lại trạng thái Pipeline từ đĩa."* |
| `+43.3s Build CHẠY TIẾP ... tick 52` | *"Bước sh vẫn chạy trên agent suốt lúc controller chết; controller mới chỉ nối lại với nó."* (Chỉ console ở C) |
| `+113.9s SUCCESS; tick thiếu: không; marker: 1` | *"Không mất dòng nào; khối netciOnce chạy đúng một lần."* |

Nếu còn thời gian: máy bị rút điện sẽ được supervisor tự bật lại (`PowerOnAfter`), cho hội đồng thấy ở cửa sổ B.

### BV4.5 Phương án dự phòng

| Sự cố | Làm |
|---|---|
| Script từ chối ở bước kiểm tra | Đọc dòng ✗ nó in, làm đúng hướng dẫn (thường là tách cell), chạy lại |
| Mạng hội trường không vào được lab | Chiếu video quay hôm trước, rồi mở file `lab/evidence/demo-power-loss-20261012T044008Z.json` |
| Build FAILURE hoặc thiếu tick | Nói thật: "đây là điều netCI sinh ra để bắt". Chuyển sang slide kiểm chứng; sau buổi bảo vệ phân tích bằng chứng |
| Hội đồng muốn thử máy treo | `python3 lab/spike/chaos_poweroff.py --runs 1 --job once-probe --failure node-hang-supervised` (khoảng 5 phút) |

---

## BV5. Ngân hàng câu hỏi hội đồng

Cột "Gợi ý" là ý chính; tự diễn đạt bằng lời của mình, và mở TB ở mục ghi trong ngoặc để học kỹ.

### BV5.1 Động cơ và so sánh

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Vì sao không dùng CloudBees CI? | Bản quyền theo người dùng, buộc vào một nhà cung cấp, và chính CloudBees ghi nhiều plugin bị giới hạn trong HA. Công ty muốn giữ Jenkins mã nguồn mở (1.2) |
| Sao không bỏ Jenkins, chuyển sang GitLab CI hay GitHub Actions? | Hàng trăm Jenkinsfile, plugin và thói quen; chuyển là một dự án nhiều năm. netCI giữ nguyên tất cả (R12) |
| Kubernetes đã tự chạy lại pod, cần netCI làm gì? | Với StatefulSet, pod trên node chết kẹt Terminating mãi, và volume RWO vẫn gắn vào node chết. Force-delete bằng tay thì có nguy cơ split-brain nếu node chỉ mất mạng. netCI làm việc đó an toàn trong 3 s (TB 3.7) |
| Khác gì Medik8s (Node Healthcheck + Self Node Remediation)? | Cùng ý tưởng fence. netCI phát hiện theo Lease của chính ứng dụng (3 s, nhanh hơn chờ node NotReady), xác nhận Off qua BMC, có panic guard theo máy, và lo cả phần Jenkins (hàng đợi, `netciOnce`, fabric). Xem `docs/research/HA-LANDSCAPE.md` |
| Sao không chạy hai controller active/active? | Jenkins giữ queue, số build, kênh agent trong RAM và ghi XML không phối hợp; hai JVM cùng ghi là hỏng dữ liệu. Muốn làm phải viết lại lõi Jenkins (7.1) |
| Backup/restore không đủ sao? | Tính bằng phút đến giờ và mất dữ liệu từ bản backup gần nhất; đó là khôi phục thảm hoạ, không phải failover |

### BV5.2 Thiết kế an toàn

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Làm sao chắc không bao giờ có hai controller cùng ghi? | Ba lớp độc lập: Lease tự dừng trước hạn (cell agent + guard), supervisor chỉ nhả Lease sau khi BMC báo Off và nhả có điều kiện theo `resourceVersion`, volume RWO (8.4). Thử cả máy treo và ánh xạ node–máy sai |
| Nếu mạng chỉ chập chờn chứ máy không chết? | Cell agent không gia hạn được trong 10 s thì tự giết Jenkins, *trước khi* Lease 15 s hết hạn. Supervisor chỉ chuyển sau khi máy đã bị tắt nguồn. Máy chỉ mất mạng thì bị ForceOff trước |
| Nếu chính cell agent treo? | Guard bọc Jenkins thấy gate file cũ quá hạn thì tự giết JVM |
| Nếu BMC trả lời sai, hoặc ánh xạ node–máy bị nhầm? | Supervisor kiểm ánh xạ lúc khởi động; nhả Lease có điều kiện nên nếu Lease vừa được gia hạn (máy "đã tắt" vẫn sống) thì dừng và cảnh báo `NetciFenceLeaseMoved` |
| Nếu BMC không trả lời? | Không làm gì với cell đó (không biết máy có thật tắt không), cảnh báo cần người. Đã đo: BMC trả lời lại thì fence sau 0,1 s |
| Nếu nhiều máy hỏng cùng lúc? | Panic guard: hơn nửa số **máy** có cell hỏng thì không tắt máy nào, vì nguyên nhân thường là chung (mạng, API server) |
| Supervisor chết thì sao? | 2 bản sao, leader election. Leader chết cùng máy thì bản kia lên sau khoảng 25 s (soak: 7 lần, đều PASS). Không có supervisor nào thì cảnh báo `NetciSupervisorNotLeading` |
| Vì sao nghi sau 3 s mà không quyết luôn? | Nghi thì rẻ: chỉ để đi hỏi. Quyết dựa vào sự thật (máy Off). Phát hiện càng nhanh càng hay nghi nhầm, nên tách hai việc (TB 4.3) |
| Lease 15 s, renew deadline 10 s chọn thế nào? | Lần đầu thử 6 s / 4 s: một lần bầu leader etcd làm cell khoẻ bị restart. 15/10 chịu được mà vẫn đủ nhanh (bài học 6) |

### BV5.3 Dữ liệu và "đúng một lần"

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Đĩa nhân bản 3 bản sao rồi, sao còn mất dữ liệu? | Nhân bản chỉ chép thứ đã xuống thiết bị khối; Jenkins để phần lớn dữ liệu ghi trong page cache. Spike đầu: trạng thái lùi 20 s, một bước chạy hai lần. Sửa bằng writeback 1 s + ext4 `commit=1`; phần 1 s còn lại do `netciOnce` lo (10.1) |
| Sao không mount `sync` cho chắc? | Đã đo: chậm khoảng 4 lần mỗi build; trên lab mỗi bước Pipeline mất khoảng 4 s. Writeback 1 s + `commit=1` không tốn gì đo được |
| "Exactly-once" có thật không? | Không có trong hệ phân tán. netCI làm at-least-once (gửi lại tới khi chắc) cộng khử trùng lặp (idempotency key, dispatch dưới khoá hàng đợi). Với bước có tác dụng phụ ra bên ngoài thì dùng `netciOnce`: khi không chắc thì **từ chối**, không chạy lại |
| `netciOnce` từ chối thì build fail, có tệ không? | Tệ hơn nhiều là deploy production hai lần. Người quyết định bằng `retry: true`. Đã đo: 51 lần mất điện giữa khối, không lần nào bị từ chối nhầm |
| PostgreSQL chết thì sao? | Queue trả 503, client gửi lại; khối `netciOnce` fail closed. Đã đo: restart 20 s thì mọi run vẫn đúng một lần; chết 90 s thì khối không chạy, không marker |
| PostgreSQL có là điểm chết mới không? | Có, nó phải HA (bản sao, failover) ở production. Lab chạy một bản. Thiết kế đã cho nó fail closed: hỏng là từ chối, không phải làm sai |

### BV5.4 Kubernetes, Jenkins, storage

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Taint out-of-service là gì, có an toàn không? | Tính năng non-graceful node shutdown (GA 1.28): khẳng định node đã tắt hẳn để Kubernetes gỡ volume không chờ kubelet. Tài liệu đòi node phải thật sự tắt, nên netCI xác nhận Off trước khi gắn |
| Vì sao chọn Longhorn? | Dễ cài trên lab, nhân bản đồng bộ, CSI. Production dùng Ceph RBD, vSAN hay SAN cũng được, miễn block RWO nhân bản và có CSI |
| Vì sao k3s? | Kubernetes đầy đủ, một binary, dựng 3 node nhanh. Mọi cơ chế netCI dùng đều là API chuẩn, không riêng k3s |
| Build đang chạy sống sót thế nào khi controller chết? | Bước `sh` là tiến trình của agent (durable-task), ghi output và exit code ra file; controller mới nối lại và đọc tiếp. Điều kiện: agent nối lại trong khoảng 5 phút, và đĩa còn đúng trạng thái (TB 2.3) |
| Agent nối lại controller mới bằng cách nào? | Trình giám sát quanh agent theo dõi `X-Jenkins-Session`; khi thấy phiên mới thì khởi động lại JVM agent để nối vào controller mới. Service publish pod chưa Ready (Jenkins trả 503 tới khi sẵn sàng) nên agent nối được trong khoảng 2 s |
| Vì sao viết bằng Go mà không Python như bản cũ? | Các controller hạ tầng (client-go, Cluster API) đều bằng Go; binary tĩnh, ảnh `FROM scratch`, test với `-race` |

### BV5.5 Fabric và hiệu năng

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Fabric có nhanh hơn Kubernetes plugin không? | Khi dồn dập thì có: 4,82 s so với 6,17 s trung vị, không build nào quá 6 s. Khi chạy lần lượt thì **chưa**: 3,88 s so với 3,17 s. Mẫu còn nhỏ |
| Cái 15 s là gì, tìm ra thế nào? | Mỗi agent mới tải 19 MB jar của controller qua kênh remoting. Tìm bằng cách bám log từng pod từ lúc nó tồn tại, tách từng giai đoạn, chụp thread dump controller đúng lúc kẹt; chứng minh bằng A/B trước khi sửa (ADR-072) |
| Jar cache có rủi ro bảo mật không? | Remoting dùng jar trong cache theo tên, không kiểm nội dung. Vì vậy cache chỉ đến từ ảnh do platform build, ghim theo digest, mount chỉ đọc; mỗi sandbox nhận một bản chép riêng; không gì sandbox ghi ra được chuyển cho sandbox khác |
| Code không tin cậy (PR từ fork) chạy ở đâu? | Pool `untrusted` dùng Kata: mỗi build một VM có kernel riêng. Pool `standard` dùng runc + user namespace, bỏ mọi capability, NetworkPolicy |
| Secret của agent nằm ở đâu? | Chỉ trong RAM của fabric, không xuống đĩa hay CSDL; sandbox chứng minh danh tính bằng projected ServiceAccount token |

### BV5.6 Phương pháp và kiểm chứng

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Sao biết test có ý nghĩa? | Mỗi test viết cùng code và phải đỏ trên code cũ (bỏ phần sửa đi thì test fail). Test viết từ cùng một hiểu lầm với code thì pass mà chẳng chứng minh gì |
| Lab 3 VM có đại diện cho production không? | Không hoàn toàn: mọi máy vừa là control plane vừa chạy cell, BMC là giả lập. Quy mô đo bằng kwok (control plane thật, node giả). Đó là lý do có phần giới hạn |
| Có số liệu nào sai đã phải bỏ không? | Có, và đã ghi lại: lần soak đầu hỏng vì công cụ đo (timestamper đổi định dạng console); một lần đo burst sai vì 4 luồng đọc cùng một số build; số đo lúc hai controller dồn một máy. Đều bị loại, đều được ghi lý do (TB 15.18–15.20) |
| Soak chọn lỗi thế nào? | Ngẫu nhiên có trọng số, seed cố định để chạy lại được; sau mỗi lượt kiểm bất biến: Lease đúng pod, không còn taint, node Ready, volume healthy, có supervisor leader |
| ADR là gì, sao nhiều thế? | Mỗi quyết định kiến trúc ghi bối cảnh, quyết định, phương án bị loại và bằng chứng. 060–072 là của bản HA; 001–059 là lịch sử bản IDP |

### BV5.7 Production, sản phẩm, cá nhân

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Đưa lên production cần gì thêm? | Diễn tập trên server và BMC thật; control plane tách khỏi máy chạy cell; PostgreSQL HA; storage của tổ chức; registry của tổ chức (INSTALL.md) |
| Chi phí vận hành? | Supervisor đo được 0,16 core và 53 MiB khi theo dõi 600 cell. Chart yêu cầu cho supervisor, queue, fabric mỗi bản 50m CPU và 64 MiB (giới hạn 192–256 MiB), 2 bản mỗi loại; mỗi cell thêm một sidecar nhỏ |
| Có bán được không, khác biệt là gì? | HA cho Jenkins mã nguồn mở mà không đổi Jenkinsfile, có bằng chứng chaos, fail closed. Thiếu: diễn tập phần cứng thật, co giãn máy, tài liệu hỗ trợ khách hàng |
| IDP và Jenkins HA liên quan thế nào? | netCI là IDP (portal, build, ký ảnh, SBOM, deploy có phê duyệt). Mọi build của nó chạy trên Jenkins, nên Jenkins sập là IDP đứng: HA cho Jenkins là bài toán con, đi sâu nhất. Nối hai phần: build của IDP đi qua hàng đợi bền tới các cell (ADR-073) |
| Em tự làm phần nào, có dùng AI không? | **Trả lời trung thực.** Nói rõ mình dùng công cụ AI thế nào (viết code, chạy thử nghiệm, soạn tài liệu), mình đã quyết định những gì, kiểm chứng ra sao. Quan trọng nhất là chứng minh được mình hiểu và giải thích được từng phần: đó là mục đích của mục BV7 |

---

## BV6. Điểm yếu nên chủ động nói

Nói trước thì hội đồng thấy mình hiểu giới hạn; để hội đồng tự tìm ra thì mất điểm.

1. **UI Jenkins tắt 40–60 s khi tiếp quản**: giới hạn của thiết kế "một người ghi". Bù lại: log vẫn đọc được
   qua Loki, trigger vẫn vào hàng đợi bền (trừ khi PostgreSQL của queue chết cùng máy: lab chỉ có một bản, và
   queue **từ chối** chứ không làm mất run trong khoảng 45–70 s đó), build vẫn chạy tiếp.
2. **Chưa thử trên server và BMC thật**: hành vi BMC được mô phỏng theo tài liệu và báo cáo lỗi của hãng
   (chậm, 503, báo trạng thái trễ). Việc đầu tiên khi có phần cứng là diễn tập.
3. **Lab nhỏ**: 3 VM, mọi máy là control plane. Quy mô 600 cell đo bằng kwok, không có storage thật.
4. **Fabric chưa nhanh hơn Kubernetes plugin khi chạy lần lượt** (chậm hơn khoảng 0,7 s).
5. **Chưa làm** co giãn số máy theo dự báo và tái chế máy.

---

## BV7. Kế hoạch học trước ngày bảo vệ

Bốn tuần, mỗi tuần một mục tiêu và một bài tự kiểm. Có thể nén hoặc giãn theo ngày bảo vệ.

| Tuần | Học (TB) | Làm | Tự kiểm |
|---|---|---|---|
| 1 | Phần 2, 3, 4: Jenkins, container, Kubernetes, storage, hệ phân tán | Bài tập 17.3 số 1, 2, 3 (Jenkins trong Docker; k3s; tắt node xem bảng 3.7) | Trả lời câu 1–4 ở TB phần 18 không nhìn tài liệu |
| 2 | Phần 1, 5–9: câu chuyện, yêu cầu, lựa chọn, kiến trúc, các luồng | Vẽ lại hình 8.1 bằng tay; tự kể bảng 9.2 từng giây | Thuyết trình slide 2–8 trong 8 phút cho một người nghe |
| 3 | Phần 10, 13, 15: thành phần, kiểm chứng, bài học | Đọc `internal/supervisor/decide.go` và test của nó; chạy `go test ./internal/supervisor -v`; chạy demo trên lab | Trả lời mục BV5.2 và BV5.3 không nhìn |
| 4 | Phần 12, 14, 16 và tài liệu này | Tập toàn bài 2 lần có bấm giờ; tập demo 2 lần; quay video dự phòng | Nhờ người khác hỏi ngẫu nhiên 15 câu ở mục BV5 |

Mỗi ngày đọc lại bảng ở mục BV2 một lần: hội đồng hay hỏi con số.


---

# Bước 7 — Demo thủ công

> **Tuần 4.** Đọc, rồi tập trên lab ít nhất hai lần và quay một video dự phòng.

## Luồng demo (khoảng 12 phút)

| Phần | Cho hội đồng thấy | Thời lượng |
|---|---|---|
| 1. Hệ thống đang chạy | 3 máy, các thành phần netCI, hai cell, Jenkins | 1,5 phút |
| 2. Hàng đợi bền, không trùng | Gửi một run hai lần, chỉ có một build | 1,5 phút |
| 3. Sandbox của fabric | Build chạy trong một pod mới, root không phải root | 1,5 phút |
| 4. **Rút điện** | Controller chết giữa build; hệ thống tự tiếp quản; trong lúc đó vẫn nhận run và vẫn đọc được log | 4 phút |
| 5. Sau sự cố | Build SUCCESS, đúng một lần; sự kiện của supervisor; cảnh báo; máy tự bật lại | 2 phút |
| (6) Rút gọn | Nếu chỉ có 3 phút: chạy script tự động | — |

---

## DM0. Chuẩn bị

### DM0.1 Hôm trước

```bash
cd ~/netci-delivery-platform
export KUBECONFIG=$PWD/.netci-gate/lab/kubeconfig
virsh -c qemu:///system list                     # 3 VM running (không "paused")
df -h /                                          # còn trên 20 GB
kubectl get nodes                                # 3 Ready
curl -sk -o /dev/null -w '%{http_code}\n' https://172.17.0.1:8930/v2/   # Harbor: 401 là đã lên
```

Nếu máy chủ vừa khởi động lại, làm Bước 10 của tài liệu trình bày (Harbor theo thứ tự, `docker start
netci-queue-pg`, `lab/redfish.sh start`, kiểm Loki).

Tập toàn bộ luồng ít nhất một lần, và **quay màn hình** lần tập đó làm phương án dự phòng.

### DM0.2 Trước giờ demo **ít nhất 30 phút**

```bash
python3 lab/demo/power_loss_demo.py --arrange-only
```

Lệnh này sắp xếp lab cho demo, bằng các lần restart có kế hoạch (khoảng 30 s mỗi cái):
- máy chạy Kata (lab-3) không có cell nào, để pool `untrusted` có chỗ;
- cell-a ở lab-1, cell-b ở lab-2;
- PostgreSQL của queue không nằm cùng máy với controller của cell-b.

Nó in `Lab đã sẵn sàng` khi xong.

Vì sao phải trước 30 phút: cảnh báo `NetciFabricSandboxesFailing` đếm số sandbox hỏng trong 30 phút gần nhất.
Nếu trước đó pool Kata đã thiếu chỗ, cảnh báo còn bắn cho tới khi hết khoảng đó.

Kiểm lại sắp xếp:

```bash
kubectl get pods -A -o wide | grep -E "jenkins-0|netci-pg-0"
# cell-a jenkins-0 ... netci-lab-1
# cell-b jenkins-0 ... netci-lab-2
# netci-system netci-pg-0 ... netci-lab-1   (miễn là KHÁC máy của cell-b)
```

### DM0.3 Mật khẩu: đăng nhập trước, đừng chiếu lên màn hình

| Ở đâu | User | Mật khẩu nằm trong file |
|---|---|---|
| Jenkins cell-b `http://192.168.122.212:30081` | `admin` | `.netci-gate/lab/cell-admin-password` |
| Grafana `http://192.168.122.211:30300` | `admin` | `.netci-gate/lab/monitoring/grafana-password` |
| Token API của queue | — | `.netci-gate/lab/queue/client-token` |

Mở các file này ở một terminal **không chiếu**, đăng nhập Jenkins và Grafana trên trình duyệt trước giờ demo.
Trong terminal chiếu, chỉ dùng `$(cat ...)` như các lệnh bên dưới, không bao giờ `cat` thẳng ra màn hình.

> Jenkins trả lời ở cả ba IP (`.211`, `.212`, `.213`, cổng 30081), nên vẫn mở được khi một máy chết. Dùng
> `.211` trong trình duyệt nếu demo rút điện lab-2.

### DM0.4 Các cửa sổ

| Cửa sổ | Mở gì |
|---|---|
| **A** (terminal chính) | Nơi gõ lệnh. Chạy trước: `cd ~/netci-delivery-platform; export KUBECONFIG=$PWD/.netci-gate/lab/kubeconfig; Q=http://192.168.122.211:30090; TOKEN=$(cat .netci-gate/lab/queue/client-token)` |
| **B** (terminal theo dõi) | `export KUBECONFIG=...; watch -n1 'kubectl get nodes; echo; kubectl get pods -A -o wide \| grep -E "NAME\|jenkins-0\|netci-pg"; echo; kubectl -n cell-b get lease jenkins -o custom-columns=HOLDER:.spec.holderIdentity,EPOCH:.spec.leaseTransitions'` |
| **C** (trình duyệt) | Jenkins cell-b: `http://192.168.122.211:30081` |
| **D** (trình duyệt) | Grafana → Explore → datasource **Loki**; và dashboard `netCI` (`/d/netci-overview/netci`) |

---

## DM1. Hệ thống đang chạy (1,5 phút)

Cửa sổ A:

```bash
kubectl get nodes -o wide
kubectl -n netci-system get pods -o wide
kubectl get pods -A -o wide | grep jenkins-0
```

**Nói**:
- *"Ba máy, mỗi máy là một node Kubernetes."*
- *"netci-system có supervisor (2 bản), queue (2 bản), fabric (2 bản) và PostgreSQL của queue."*
- *"Mỗi cell là một Jenkins controller với ổ đĩa nhân bản 3 bản sao; cell-a ở lab-1, cell-b ở lab-2."*

Cửa sổ C: mở Jenkins, cho thấy các job (`once-probe`, `fabric-probe`, `queue-probe-short`).

Có thể gặp: trong `kubectl get pods -A` có một pod `sbx-…` ở trạng thái Pending của pool `untrusted`. Đó là
pool Kata, chỉ chạy trên lab-3, đang chờ chỗ. Không liên quan tới luồng demo; nếu bị hỏi, xem mục DM7.

## DM2. Hàng đợi bền và không trùng (1,5 phút)

Cửa sổ A, gửi **cùng một khoá idempotency hai lần**:

```bash
KEY="hoidong-$(date +%s)"
curl -s -X POST $Q/v1/runs -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"job\":\"queue-probe-short\",\"idempotencyKey\":\"$KEY\"}" -w "  HTTP %{http_code}\n"
curl -s -X POST $Q/v1/runs -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"job\":\"queue-probe-short\",\"idempotencyKey\":\"$KEY\"}" -w "  HTTP %{http_code}\n"
```

Kết quả (đã chạy):

```
{"id":"7d72ce5d-…","job":"queue-probe-short","cell":"cell-b",…,"state":"accepted",…}  HTTP 202
{"id":"7d72ce5d-…","job":"queue-probe-short","cell":"cell-b",…,"state":"accepted",…}  HTTP 200
```

**Nói**: *"Lần đầu 202: đã ghi vào PostgreSQL rồi mới trả lời. Lần hai cùng khoá: 200 với **cùng id**, không
tạo run thứ hai. Đây là cách một webhook gửi lại không gây build trùng."*

Theo dõi run (thay `<id>`):

```bash
curl -s $Q/v1/runs/<id> -H "Authorization: Bearer $TOKEN"; echo
```

Trạng thái đi từ `accepted` sang `dispatched`, rồi `finished` với `buildNumber` và `result: SUCCESS`, sau khoảng
25 s. Job này cố ý có quiet period 20 s. Cửa sổ C: build đó hiện trong `queue-probe-short`.

## DM3. Sandbox của fabric (1,5 phút)

Cửa sổ A:

```bash
kubectl -n netci-agents get pods -L netci.io/pool -o wide
```

**Nói**: *"Đây là các sandbox ấm: pod đã khởi động sẵn, chưa thuộc về controller nào."*

Cửa sổ C: job `fabric-probe` → **Build Now**. Mở console. Build xong trong khoảng 4 s, với:

```
Running on netci-standard-8964357c …
on netci-standard-8964357c: 1000 6.8.0-142-generic
         0 1165557760      65536
```

**Nói**:
- *"Build được gắn vào một sandbox ấm ngay lúc cần (bind muộn)."*
- *"Dòng cuối là `uid_map`: root trong sandbox (uid 0) là uid 1165557760 trên máy thật, tức là không phải root. Đó là user namespace."*
- *"Xong build, sandbox bị xoá; mỗi build một sandbox mới."*

Xem lại vòng đời của nó:

```bash
kubectl -n netci-system exec netci-pg-0 -- psql -U netci -d netci -c \
  "SELECT pod, state, cell, agent, reason FROM sandboxes WHERE claimed_at > now() - interval '5 minutes' ORDER BY claimed_at"
```

## DM4. Rút điện (4 phút)

### DM4.1 Khởi động build dài

Cửa sổ C: job `once-probe` → **Build with Parameters** → `DURATION` = `180` → Build. Mở console: mỗi giây một
dòng `tick N`.

**Nói**: *"Build này chạy một bước sh 180 giây bên trong `netciOnce`, giống một bước deploy không được phép
chạy hai lần."*

### DM4.2 Tìm máy của controller

```bash
kubectl -n cell-b get pod jenkins-0 -o wide        # cột NODE, ví dụ netci-lab-2
```

### DM4.3 Rút điện

Đợi console tới khoảng tick 15, rồi ở cửa sổ A:

```bash
virsh -c qemu:///system destroy netci-lab-2
```

Theo dõi cửa sổ B. Mốc thời gian dưới đây là của lần chạy thật (#160):

| Khoảng giây | Cửa sổ B thấy | Nói |
|---|---|---|
| 0–3 | Mọi thứ còn như cũ; trang Jenkins ở C không tải được | *"Máy vừa mất điện; Jenkins chết cùng nó."* |
| ~3–6 | `netci-lab-2` có taint out-of-service; HOLDER của Lease thành rỗng | *"Supervisor thấy Lease không được gia hạn 3 giây, hỏi BMC qua Redfish, được báo máy đã Off, nhả Lease và gắn taint."* |
| ~7 | `jenkins-0` mới, `Pending`, ở `netci-lab-3` | *"Kubernetes tạo controller mới ở máy khác; ổ đĩa đang được gỡ khỏi máy chết và gắn sang."* |
| ~30 | HOLDER là pod mới, EPOCH tăng 1; pod `Running` | *"Controller mới giữ Lease; chỉ nó được chạy Jenkins."* |
| ~40 | Trang Jenkins ở C mở lại được | *"Jenkins mới lên, đọc lại Pipeline từ đĩa."* |

Mốc thời gian dao động theo việc máy chết còn mang theo gì: trong các lần tập, Lease mới ở giây 20–52, Jenkins
lên ở giây 30–62. Đừng hứa trước một con số chính xác.

### DM4.4 Trong lúc Jenkins đang chết (giây 8–30): hai điều Jenkins thường không làm được

**a) Vẫn nhận run** (cửa sổ A):

```bash
curl -s -X POST $Q/v1/runs -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"job\":\"queue-probe-short\",\"idempotencyKey\":\"luc-sap-$(date +%s)\"}" -w "  HTTP %{http_code}\n"
```

Kết quả thật: `HTTP 202`, `state: accepted`. Run này sẽ chạy khi controller mới lên.

**Nói**: *"Jenkins đang chết, nhưng hàng đợi bền vẫn nhận việc."*

> Chỉ đúng khi PostgreSQL không chết cùng máy (bước DM0.2 lo việc đó). Nếu nó chết cùng máy, lệnh trên trả 503
> hoặc lỗi kết nối trong khoảng 45–70 s: queue từ chối chứ không làm mất. Lab chỉ có một PostgreSQL; production
> phải chạy nó HA.

**b) Vẫn đọc được log build** (cửa sổ D, Grafana → Explore → Loki), truy vấn:

```
{cell="cell-b", job="once-probe"} |= "tick "
```

Kết quả thật: các dòng tới `tick 14`, đúng lúc mất điện.

**Nói**: *"Log được đẩy ra Loki ngay khi Jenkins ghi, nên đọc được kể cả khi controller đang được chuyển."*

### DM4.5 Build chạy tiếp

Khi trang Jenkins mở lại, vào lại console của build (cửa sổ C). Sẽ thấy:

```
Resuming build at … after Jenkins restart
Waiting for reconnection of spike-ws-… before proceeding with build
Ready to run at …
tick 16 …
…
```

và các tick **không đứt quãng**.

**Nói**: *"Bước sh vẫn chạy trên agent suốt lúc controller chết. Controller mới chỉ nối lại với nó, không chạy
lại."*

## DM5. Sau sự cố (2 phút)

### DM5.1 Build xong, đúng một lần

Console kết thúc bằng `Finished: SUCCESS`. Kiểm marker của `netciOnce` (thay số build):

```bash
kubectl -n netci-system exec netci-pg-0 -- psql -U netci -d netci -tAc \
  "SELECT count(*) FROM once_markers WHERE client='cell-b' AND key='deploy' AND scope LIKE '%/once-probe#160'"
# 1
```

**Nói**: *"Một marker: khối netciOnce bắt đầu đúng một lần."*

Run gửi lúc Jenkins đang chết (thay `<id>`):

```bash
curl -s $Q/v1/runs/<id> -H "Authorization: Bearer $TOKEN"; echo
# "state":"finished","buildNumber":131,…,"result":"SUCCESS"
```

### DM5.2 Supervisor đã làm gì

```bash
kubectl get events -A --field-selector source=netci-supervisor --sort-by=.lastTimestamp \
  -o custom-columns=TIME:.lastTimestamp,REASON:.reason,MSG:.message | tail -6
```

Kết quả thật (rút gọn):

```
Fenced             machine netci-lab-2 confirmed off in 176ms; lease released, node netci-lab-2 out of service …
TakeoverComplete   jenkins-0/… holds the lease (epoch 236) 30.0s after the last renewal by jenkins-0/…
PoweredOn          nothing of a cell is left on the fenced node: powering it back on
Unfenced           the machine is running again and its node is Ready
```

**Nói**:
- *"Supervisor xác nhận máy tắt trong 176 ms rồi mới chuyển."*
- *"Sau khi mọi thứ của cell đã rời máy chết, nó tự bật máy lại và gỡ taint khi máy Ready."*

### DM5.3 Cảnh báo

Grafana dashboard `netCI`, hoặc:

```bash
curl -s http://192.168.122.211:30909/api/v1/alerts | python3 -c 'import json,sys; print(sorted({a["labels"]["alertname"]+": "+a["state"] for a in json.load(sys.stdin)["data"]["alerts"] if a["labels"]["alertname"].startswith("Netci")}))'
```

Trong lúc lab-2 tắt, lần chạy thật có `NetciCellWithoutHeadroom: pending`.

**Nói**: *"Khi một máy đang tắt, nếu thêm một máy nữa chết thì không còn chỗ cho controller; netCI báo trước
điều đó."*

Có thể thấy thêm `NetciCellsShareAMachine` nếu sau tiếp quản hai cell ở chung một máy. Đó là cảnh báo đúng:
chúng sẽ chết cùng nhau. Cách xử lý là bước DM0.2.

### DM5.4 Máy tự bật lại

Cửa sổ B: `netci-lab-2` trở lại `Ready`, taint biến mất, sau khoảng 2 phút kể từ lúc rút điện.

---

## DM6. Rút gọn: nếu chỉ có 3 phút

```bash
python3 lab/demo/power_loss_demo.py --cell cell-b --duration 120
```

Script kiểm điều kiện, chạy build, dừng chờ Enter (mở link console nó in ra cho hội đồng xem), rồi rút điện và
kể từng mốc bằng tiếng Việt. Kết thúc bằng `SUCCESS; tick thiếu: không; marker netciOnce: 1`.

Có thể thêm `--arrange` để nó tự sắp xếp lab trước, nhưng mất thêm vài phút.

---

## DM7. Khi có trục trặc

| Hiện tượng | Nguyên nhân | Làm |
|---|---|---|
| Lệnh `curl` có `?tree=...[...]` không trả gì | `curl` hiểu `[ ]` là mẫu glob | Thêm `-g` (`curl -sg …`), hoặc dùng giao diện web |
| Chờ "tick" mãi bằng `grep "^tick"` | Console của cell-b có tiền tố giờ `[2026-…Z] tick 15` | Bỏ `^`: `grep "tick 15 "` |
| Queue trả 503 / lỗi kết nối lúc tiếp quản | PostgreSQL chết cùng máy | Bước DM0.2; hoặc giải thích: từ chối chứ không mất, production chạy PostgreSQL HA |
| Pod `sbx-…` của pool `untrusted` Pending; cảnh báo `NetciFabric…` bắn | Pool Kata chỉ chạy trên lab-3 và lab-3 hết chỗ (thường do một cell nằm đó) | Bước DM0.2, và chờ 30 phút cho cảnh báo tự hết |
| Jenkins chậm lên hơn mọi lần (60 s) | Máy chết mang theo cả leader của control plane hay storage, hoặc nhiều volume cùng chuyển | Bình thường trong biên độ đã đo (34–67 s); nói thẳng như vậy |
| Build trên **cell-a** xong muộn 100–150 s | cell-a mount `sync,dirsync` (cấu hình đối chứng): mỗi bước Pipeline mất khoảng 4 s | Luôn demo trên **cell-b** |
| Sau demo hai cell ở chung một máy | Tiếp quản đặt controller vào máy còn chỗ | Chạy lại `--arrange-only` trước lần demo sau |
| Máy bị rút điện không lên lại sau 5 phút | Supervisor chưa bật lại | `kubectl get events -A --field-selector source=netci-supervisor`; bật tay: `virsh -c qemu:///system start netci-lab-2` |
| Mạng hội trường không vào được lab | — | Chiếu video quay lần tập, mở file bằng chứng trong `lab/evidence/` |

## DM8. Sau buổi demo

```bash
python3 lab/demo/power_loss_demo.py --arrange-only     # đưa lab về sắp xếp chuẩn cho lần sau
```


---

# Bước 8 — Tự kiểm tra

> **Tuần 4.** Trả lời không nhìn tài liệu. Câu nào chưa trả lời được thì đọc lại mục ghi trong ngoặc.

## 18. Câu hỏi tự kiểm tra

1. Vì sao không chạy hai Jenkins controller trên cùng một `JENKINS_HOME`? *(2.7, 7.1)*
2. Vì sao một bước `sh` đang chạy có thể "sống sót" khi controller chết? Điều kiện để nó chạy tiếp là gì? *(2.3)*
3. Nếu chỉ dùng Kubernetes, vì sao pod `jenkins-0` của một node chết không bao giờ được tạo lại? *(3.7)*
4. Taint out-of-service làm gì, và vì sao netCI phải xác nhận máy Off trước khi gắn nó? *(3.6, 9.2)*
5. Kể ba lớp chống split-brain. Lớp nào vẫn bảo vệ được nếu chính supervisor có lỗi? *(8.4)*
6. Vì sao supervisor "nghi" sau 3 s nhưng không "quyết" sau 3 s? *(4.3, 9.2)*
7. Vì sao panic guard phải đếm theo máy chứ không theo cell? *(15.5)*
8. "Nhận nghĩa là bền" nghĩa là gì trong netci-queue? Điều gì ngăn một webhook gửi lại tạo build thứ hai? *(9.1, 10.6)*
9. Khi nào `netciOnce` từ chối chạy, và vì sao từ chối tốt hơn chạy lại? *(9.5)*
10. Vì sao fabric giữ secret của agent chỉ trong RAM? *(10.8)*
11. Vì sao không được trừ hai mốc thời gian từ hai máy? Ví dụ thật trong đề tài? *(15.17)*
12. Vì sao jar cache phải được ghim theo digest, và vì sao mỗi sandbox nhận một bản chép riêng? *(10.9)*
13. Một test pass thì đã chứng minh được gì chưa? Cần thêm điều kiện gì? *(13.1)*


---

# Tham khảo — dựng lại, vận hành, lộ trình kỹ năng, thuật ngữ, câu lệnh, bản đồ mã nguồn

> **Khi cần.** Không phải đọc từ đầu đến cuối: tra khi dựng lab, khi xử lý sự cố, hoặc khi gặp từ lạ.

## 12. Dựng lại từ đầu, theo đúng thứ tự

> Mọi lệnh chạy từ gốc repo trên **máy chủ lab** (host). Secret sinh vào `.netci-gate/` và **không bao giờ
> commit**. `[Prod]` là chỗ khác biệt khi lên server thật. Thứ tự quan trọng: mỗi bước dựa vào bước trước.

### Bước 0 — Chuẩn bị máy chủ

Yêu cầu: Linux có `/dev/kvm` và nested virtualisation, libvirt (mạng `default`), Docker, Go 1.27, Helm,
kubectl, Python 3 (có `pyyaml`), khoảng 30 GB RAM, **đĩa trống > 30 GB** (đĩa đầy làm QEMU pause mọi VM),
ảnh cloud Ubuntu 24.04 ở `~/iso/noble-server-cloudimg-amd64.img`.

```bash
cat /sys/module/kvm_intel/parameters/nested      # phải là Y
virsh -c qemu:///system net-list --all           # mạng default active
df -h /                                          # còn chỗ
go version; helm version; kubectl version --client; docker version
```

### Bước 1 — Hạ tầng phụ trợ: CA riêng, Harbor

```bash
scripts/corp/lab_ca.sh ca                                          # CA riêng của lab (một lần)
scripts/corp/lab_ca.sh issue harbor IP:172.17.0.1 DNS:localhost IP:127.0.0.1
# Cài Harbor bằng offline installer chính thức, https với cert vừa cấp, cổng 8930.
curl -fsS --cacert .netci-gate/corp/pki/ca.crt https://172.17.0.1:8930/api/v2.0/ping
```

Tạo project `netci` và `mirror`, một robot account đẩy/kéo (credential trong `.netci-gate/corp/`), **bật tag
bất biến**. Mirror các ảnh ngoài vào `mirror/` (ví dụ `jenkins/inbound-agent`).

`[Prod]` Dùng registry của tổ chức; chart từ chối ảnh không có digest.

### Bước 2 — Ba VM

```bash
lab/vms.sh up        # 3 guest: 4 vCPU, 5 GiB RAM, đĩa thin 40 GiB, IP cố định 192.168.122.211-213
lab/vms.sh status
lab/vms.sh ssh 1
```

`vms.sh` dùng cloud-init đặt user, SSH key, sysctl writeback 1 s, và gán IP bằng DHCP reservation (đổi IP
từng làm etcd mất quorum).

### Bước 3 — k3s và Longhorn

```bash
lab/k3s.sh up                      # k3s v1.36.4+k3s1: 3 server, etcd nhúng; Longhorn v1.13.0; CoreDNS ×2
export KUBECONFIG=$PWD/.netci-gate/lab/kubeconfig
kubectl get nodes -o wide
kubectl -n longhorn-system get pods
lab/longhorn-tune.sh               # admission policy: attacher mỗi node một bản, retry ≤ 5 s, HTTP/2 health check
```

Script làm: tin CA của Harbor trong containerd (`/etc/rancher/k3s/registries.yaml`); server đầu
`--cluster-init`, hai server sau `--server https://<ip1>:6443`; tắt traefik, servicelb; `--tls-san` cả ba IP;
token trong file (không lộ trên dòng lệnh); **2 bản CoreDNS**.

> Chỉ thử MutatingAdmissionPolicy bằng `lab/longhorn/try-policy.sh`: policy có phạm vi toàn cụm, từng có
> lần thay nhầm policy thật.
> `[Prod]` Control plane trên máy không chạy cell; StorageClass RWO nhân bản qua nhiều máy.

### Bước 4 — Build và đẩy ảnh

```bash
make check                                     # vet, gofmt, test Go với -race
make plugin                                    # plugin Java: jenkins/plugin/target/netci.hpi (Maven trong container)

# 4a. Ảnh netCI: LUÔN từ một worktree sạch của HEAD
git worktree add --detach /tmp/wt HEAD
( cd /tmp/wt && mkdir -p bin/linux-amd64 \
  && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o bin/linux-amd64/ ./cmd/... \
  && docker build --network host -t netci/netci:<ver> . )
git worktree remove --force /tmp/wt
scripts/corp/push_image.sh netci/netci:<ver> 172.17.0.1:8930/netci/netci:<ver>      # in ra digest

# 4b. Ảnh controller Jenkins (Jenkins 2.555.3 LTS + plugins.txt + netci.hpi; plugin giải nén sẵn)
docker build --network host -f jenkins/Dockerfile.controller -t netci/jenkins-controller:2.555.3-netciN jenkins/
scripts/corp/push_image.sh netci/jenkins-controller:2.555.3-netciN 172.17.0.1:8930/netci/jenkins-controller:2.555.3-netciN

# 4c. Ảnh jar cache của agent, từ đúng ảnh controller ở 4b (ADR-072)
docker build --network host -f jenkins/Dockerfile.agent-jars \
  --build-arg CONTROLLER_IMAGE=netci/jenkins-controller:2.555.3-netciN -t netci/jenkins-agent-jars:2.555.3-N jenkins/
scripts/corp/push_image.sh netci/jenkins-agent-jars:2.555.3-N 172.17.0.1:8930/netci/jenkins-agent-jars:2.555.3-N
```

- Dùng `push_image.sh` (buildah trong toolbox, chỉ tin CA của lab), **không** dùng `docker push`: Docker 29
  lỗi x509 khi lấy token.
- `push_image.sh` ghi file tar của ảnh vào `/tmp` trước: giữ đĩa trống trên 15 GB khi đẩy ảnh controller.
- Docker trên máy chủ lab không có mạng bridge: luôn `--network host`.

### Bước 5 — PriorityClass, StorageClass, cell đầu tiên

```bash
kubectl apply -f lab/priorities.yaml
kubectl apply -f lab/spike/storageclass-sync.yaml -f lab/spike/storageclass-commit1.yaml
CELL=cell-a STORAGE_CLASS=longhorn-sync    NODE_PORT=30080 NETCI_IMAGE=172.17.0.1:8930/netci/netci@sha256:<digest> lab/spike/deploy.sh
CELL=cell-b STORAGE_CLASS=longhorn-commit1 NODE_PORT=30081 NETCI_IMAGE=172.17.0.1:8930/netci/netci@sha256:<digest> lab/spike/deploy.sh
```

Một cell cần: ConfigMap JCasC (`jenkins.yaml`; ví dụ `lab/spike/cell.yaml`), Secret (mật khẩu admin,
`fabric-token`, `once-token`, `casc-reload-token`), pull secret Harbor.

### Bước 6 — Hàng đợi, fabric, supervisor, BMC

```bash
NETCI_IMAGE=...@sha256:<digest> lab/queue.sh        # PostgreSQL (lab) + 2 netci-queue, NodePort 30090
NETCI_IMAGE=...@sha256:<digest> lab/fabric.sh       # netci-fabric + token cho cell-b
lab/redfish.sh start            # BMC giả lập ở 192.168.122.1:8000, TLS + basic auth, chỉ thấy netci-lab-1..3
lab/redfish.sh secret           # Secret netci-fence-redfish + values Helm
```

### Bước 7 — Giám sát và Kata

```bash
lab/monitoring.sh install       # kube-prometheus-stack (Prometheus :30909, Grafana :30300) + Loki trên host :3100
lab/kata.sh install netci-lab-3 # kata-deploy 4.2.0, RuntimeClass kata-qemu-runtime-rs
lab/kata.sh check               # kernel trong pod phải khác kernel của node
```

### Bước 8 — Cài toàn bộ bằng Helm (như một tổ chức sẽ cài)

```bash
MONITORING=on FENCE=redfish \
  NETCI_IMAGE=172.17.0.1:8930/netci/netci@sha256:<digest> \
  AGENT_JARS=172.17.0.1:8930/netci/jenkins-agent-jars@sha256:<digest> \
  lab/helm-install.sh
helm list -A
```

Script "nhận nuôi" (adopt) các đối tượng các bước trước đã tạo, rồi `helm upgrade --install` chart `netci`
và từng cell (`netci-cell`). Volume của cell được giữ nguyên.

> **Luôn truyền đủ biến.** Bỏ `MONITORING`/`FENCE` ở lần chạy lại là lặng lẽ quay về SSH fencing và mất
> PodMonitor; bỏ `AGENT_JARS` là pool mất jar cache.

Cài thủ công cho production:

```bash
helm upgrade --install netci deploy/helm/netci -n netci-system --create-namespace -f my-values.yaml \
  --set image.repository=REGISTRY/netci/netci --set image.digest=sha256:...
helm upgrade --install cell-payments deploy/helm/netci-cell -n cell-payments --create-namespace -f cell-values.yaml
```

Các giá trị quan trọng:
- **`netci`**: `supervisor.fence.*`, `supervisor.power.queryTimeout` (3s), `supervisor.power.offTimeout`
  (60s), `supervisor.storageSettle` (3s), `supervisor.autoPowerOn` (false ở prod),
  `supervisor.clusterDNSService`, `queue.database.secretName`, `fabric.config.pools` (kể cả `jarCache`),
  `fabric.networkPolicy.clusterCIDRs` (bắt buộc).
- **`netci-cell`**: `image.controller`, `netci.digest`, `storage.className`, `casc.configMapName`,
  `secrets.secretName`, `outOfServiceTolerationSeconds` (30), `logShipping.*`, `kubernetesPlugin.enabled`.

### Bước 9 — Kiểm tra trước khi ai đó phụ thuộc vào nó

```bash
kubectl -n netci-system get lease netci-supervisor                 # có leader
kubectl -n netci-system logs deploy/netci-supervisor | grep "mapping checked"
kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide        # 2 CoreDNS trên 2 máy
kubectl -n netci-agents logs -l netci.io/pool=standard -c agent | grep "jar cache seeded"
deploy/helm/netci/ci/test-rules.sh                                 # promtool unit test cho cảnh báo
python3 lab/spike/chaos_poweroff.py --runs 3 --job once-probe      # diễn tập mất điện
python3 lab/spike/chaos_poweroff.py --runs 3 --job once-probe --failure node-hang-supervised   # máy treo
python3 lab/spike/fabric_timeline.py --runs 5 --burst 4            # thời gian build fabric vs Kubernetes plugin
```

`[Prod]` Diễn tập trên **phần cứng và BMC thật của mình** trước khi chạy production.

### Bước 10 — Sau mỗi lần máy chủ lab khởi động lại

1. `virsh list`: 3 VM `running` (không `paused`).
2. Đồng hồ VM khớp máy chủ: `lab/vms.sh ssh 1 'date -u'`. Lệch thì xem 14.1.
3. Harbor: khởi động container theo thứ tự `redis, harbor-db, registry, registryctl, harbor-portal,
   harbor-core, harbor-jobservice, nginx`. Kiểm: `curl -k https://172.17.0.1:8930/v2/` trả **401** là lên
   (http thường trả 400, không phải lỗi).
4. `docker start netci-queue-pg` (DB của test Go); `lab/redfish.sh start`; `docker ps` có `netci-lab-loki`.
5. `kubectl get nodes`, pod kẹt, volume Longhorn `healthy`.
6. Hai cell ở hai máy khác nhau (nếu dồn một máy: `kubectl -n cell-a delete pod jenkins-0` lúc vắng việc).

## 14. Vận hành và xử lý sự cố

### 14.1 Những tình huống thường gặp trên lab

| Triệu chứng | Nguyên nhân | Xử lý |
|---|---|---|
| `virsh list` báo VM `paused` | Đĩa máy chủ đầy | `docker builder prune -a`, `go clean -cache`, xoá ảnh cũ; `virsh resume`; trong guest `sudo k3s crictl rmi --prune` và `sudo fstrim -v /` |
| Không kéo được ảnh | Harbor chưa lên sau khởi động lại | Bước 10 |
| Supervisor không fence qua Redfish | Emulator chưa chạy | `lab/redfish.sh start` |
| Test Go của queue/fabric bị skip hay lỗi | `netci-queue-pg` chưa chạy | `docker start netci-queue-pg` |
| Không có log trong Grafana | Loki trên host chưa chạy | `docker ps`, `lab/monitoring.sh install` |
| `pkill -f <mẫu>` giết luôn shell của mình | Mẫu có trong chính dòng lệnh | `pgrep -f "[c]haos..."` rồi kill theo PID |
| Cả 3 node NotReady, log k3s `x509: certificate ... is before <giờ>` | Máy chủ dual-boot Windows (RTC giữ giờ địa phương): VM khởi động nhanh khoảng 7 giờ, k3s cấp chứng chỉ "từ tương lai", rồi NTP chỉnh lại | Từng node: dừng k3s, dời `/var/lib/rancher/k3s/agent/*.crt` (+`.key`) có `notBefore` ở tương lai, khởi động k3s; xoá pod kẹt `CreateContainerError`, pod engine-image/manager Longhorn không ready (manager từng cái một) |
| Một VM chậm vài phút sau khi resume | VM bị pause thì đồng hồ dừng; timesyncd chưa nhận ra | `sudo systemctl restart systemd-timesyncd` trong VM |
| Đẩy ảnh lỗi 500, VM bị pause | `push_image.sh` ghi tar vào `/tmp` | Giữ đĩa trên 15 GB; `crictl rmi --prune` + `fstrim` trong VM |
| `docker build` lỗi `network bridge not found` | Docker trên máy chủ không có bridge | `--network host` |
| Mọi build chậm gấp đôi, pool Kata không lên | Hai controller dồn chung một máy sau các lần tiếp quản | `kubectl -n cell-a delete pod jenkins-0` lúc vắng việc (runbook `NetciCellsShareAMachine`) |

### 14.2 Lệnh chẩn đoán

```bash
kubectl get nodes -o custom-columns=N:.metadata.name,T:.spec.taints[*].key
kubectl get lease -A --field-selector metadata.name=jenkins -o custom-columns=NS:.metadata.namespace,H:.spec.holderIdentity
kubectl -n netci-system logs deploy/netci-supervisor | grep -E "confirmed off|cell fenced|force-deleted|SupervisorAlert"
kubectl -n longhorn-system get volumes.longhorn.io -o custom-columns=N:.metadata.name,S:.status.state,R:.status.robustness,NODE:.status.currentNodeID
kubectl -n longhorn-system logs -l app=csi-attacher --since=10m | grep -E "Detach|Attach|Error"
kubectl -n netci-system exec netci-pg-0 -- psql -U netci -d netci -c "SELECT pool, state, count(*) FROM sandboxes GROUP BY 1,2"
kubectl -n cell-b exec jenkins-0 -c jenkins -- jcmd 1 Thread.print     # thread dump controller (đổi 1 thành PID của java)
```

### 14.3 Runbook

Mỗi cảnh báo có một mục trong `docs/RUNBOOK.md`: nghĩa là gì, kiểm gì, làm gì. Ví dụ `NetciFenceLeaseMoved`:
máy được báo Off nhưng Lease lại vừa được gia hạn, tức ánh xạ node–máy sai. Việc cần làm: kiểm `fence.json`
ngay, **không** tự sửa bằng cách xoá pod.

## 17. Bản đồ kỹ năng, lộ trình tự học và bài tập

### 17.1 Bản đồ kỹ năng

| Mảng | Cần nắm | Tự luyện bằng |
|---|---|---|
| Linux | systemd, journalctl, sysctl, page cache và writeback, fsync, ext4 mount options, namespace, cgroup | Đo `dd conv=fsync`; chỉnh `dirty_expire_centisecs` rồi rút điện VM |
| Ảo hoá | KVM, libvirt, cloud-init, qcow2, nested virt | Tự viết lại `lab/vms.sh` |
| Mạng và TLS | TCP (vì sao kết nối chết không đóng), HTTP/1.1 và HTTP/2, WebSocket, TLS, CA, pin SHA-256 | `openssl`, `curl --cacert`, đọc `internal/kubeclient` |
| Jenkins | Controller/agent, executor, queue, Pipeline durability, durable-task, remoting, JCasC, Cloud, NodeProvisioner, plugin Java | Bài tập 17.3 |
| Kubernetes | Mục 3.4–3.9; client-go; leader election; RBAC; scheduler (preemption, PDB); CSI; taint | Bài tập 17.3 |
| Storage | Block và file, RWO, nhân bản đồng bộ, Longhorn engine/replica | Rút điện node đang giữ volume, đọc log csi-attacher |
| Hệ phân tán | Lease, fencing, epoch, split-brain, failure detector, quorum, idempotency, đồng hồ | Viết `Decide` thuần rồi test từng quy tắc |
| Go | goroutine, context, net/http, testing, `-race` | Đọc `internal/supervisor` cùng test |
| Java | Maven, JenkinsRule, extension point của Jenkins | `jenkins/plugin` |
| PostgreSQL | Giao dịch, compare-and-set, `FOR UPDATE SKIP LOCKED`, migration | `internal/runqueue`, `internal/fabric/store.go` |
| Phần cứng | BMC, Redfish, IPMI | sushy-tools; tài liệu Redfish của DMTF |
| Quan sát | PromQL, alert `for`, promtool test, LogQL, thread dump JVM | `deploy/helm/netci/ci/rules-test.yaml` |
| Helm | template, values, `required`, adoption | `helm template` hai chart |
| Phương pháp | Viết ADR; thí nghiệm có giả thuyết; A/B; test phải fail trên code cũ; không nói quá bằng chứng | Viết lại ADR-072 theo cách hiểu của mình |

### 17.2 Lộ trình gợi ý (khoảng 10–12 tuần)

1. Linux, KVM, dựng 3 VM.
2. Jenkins một mình (2.8): job, Pipeline, agent, thử restart và mất hàng đợi.
3. Kubernetes cơ bản (3.10), k3s 3 node, Longhorn.
4. Jenkins trên StatefulSet và PVC; rút điện, quan sát bảng 3.7.
5. Lease và cell agent (Go); guard.
6. Supervisor tối thiểu: quan sát, hỏi `virsh domstate`, taint out-of-service. Đo lại.
7. Script chaos; sửa từng lỗi tìm được (Phần 15).
8. Hàng đợi bền và plugin dispatch.
9. Fabric, Kata, jar cache.
10. Giám sát, Helm, tài liệu, diễn tập, soak.

### 17.3 Bài tập (làm theo thứ tự)

1. **Jenkins durability**: job `sh` đếm tới 120; giữa chừng `docker restart` Jenkins; giải thích vì sao build
   chạy tiếp. Rồi `docker kill` thay vì restart: còn chạy tiếp không, vì sao?
2. **Hàng đợi trong RAM**: xếp 5 build vào hàng đợi (quiet period 60 s), `docker kill` Jenkins, khởi động
   lại: còn mấy build?
3. **StatefulSet và node chết**: trên k3s 3 node, chạy Jenkins StatefulSet + PVC Longhorn, tắt hẳn máy của nó,
   đo thời gian từng mốc ở 3.7 trong 10 phút. Rồi gắn taint out-of-service bằng tay
   (`kubectl taint node <n> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute`) và đo lại.
4. **Lease**: viết chương trình Go dùng `k8s.io/client-go/tools/leaderelection` giữ một Lease; chạy hai bản;
   giết bản đang giữ; đo thời gian bản kia nhận.
5. **Split-brain**: dùng `virsh suspend` (máy treo) thay vì `destroy`, rồi `resume` sau 60 s. Nếu đã
   force-delete pod và pod mới đã chạy, chuyện gì xảy ra với hai Jenkins? (Chỉ làm trên lab.)
6. **Redfish**: dựng sushy-tools, dùng `curl` đọc `PowerState`, gửi `ForceOff`, đo thời gian tới khi báo Off.
7. **Idempotency**: viết một endpoint nhận webhook ghi vào PostgreSQL với `UNIQUE(client, delivery_id)`; gửi
   lại 10 lần cùng delivery id; chỉ có một dòng.
8. **Jar cache**: chạy một agent mới, đo `du -sh <workDir>/remoting/jarCache` sau một build; chép thư mục đó
   vào một agent khác trước khi nó kết nối; so thời gian online.

## Phụ lục A — Thuật ngữ A–Z

| Thuật ngữ | Nghĩa |
|---|---|
| **ADR** | Architecture Decision Record: tài liệu ghi một quyết định, lý do, phương án bị loại |
| **Admission (netci-queue)** | Giới hạn số run đã gửi vào một cell mà chưa chạy |
| **Agent (Jenkins)** | Tiến trình `agent.jar` chạy build, nối về controller |
| **Agent fabric** | Lớp cung cấp sandbox build của netCI (`netci-fabric` + `netci-sandbox`) |
| **Anti-affinity** | Quy tắc "không đặt pod này cạnh pod kia" |
| **API server** | Cổng vào duy nhất của Kubernetes |
| **Attach / detach** | Gắn / gỡ volume khối vào một node |
| **BMC** | Bộ điều khiển quản trị trên bo mạch, bật tắt nguồn từ xa |
| **Bind muộn (late binding)** | Pod sandbox khởi động trước, chỉ gắn với controller và build khi có claim |
| **Build** | Một lần chạy job |
| **Cell** | Một Jenkins controller + `JENKINS_HOME` + cell agent, phục vụ một nhóm đội |
| **Cell agent** | Sidecar Go giữ Lease, mở gate, probe `JENKINS_HOME`, reload JCasC |
| **Checksum (remoting)** | Cách remoting đặt tên jar trong cache |
| **Claim** | Yêu cầu một sandbox từ fabric |
| **Cloud (Jenkins)** | API để Jenkins tự tạo agent |
| **Cold claim** | Claim khi hết pod ấm: tạo pod mới cho claim đó |
| **Control loop / reconcile** | Vòng lặp so thực tế với mong muốn rồi sửa cho khớp |
| **Control plane** | API server, etcd, scheduler, controller-manager |
| **Controller (Jenkins)** | JVM Jenkins giữ cấu hình, job, hàng đợi, UI |
| **Cooldown** | Thời gian không hành động lại với một cell sau một hành động |
| **CoreDNS** | DNS trong cụm Kubernetes |
| **CSI** | Container Storage Interface, chuẩn plugin storage |
| **Digest** | Mã băm sha256 định danh bất biến một ảnh |
| **Dispatch idempotent** | Gửi một run vào Jenkins; gửi lại không tạo build thứ hai |
| **Durability (Pipeline)** | Khả năng Pipeline chạy tiếp sau khi controller khởi động lại |
| **durable-task** | Plugin chạy bước `sh` thành tiến trình sống độc lập trên agent |
| **Epoch** | Số lần Lease đổi chủ (`leaseTransitions`), dùng như fencing token |
| **etcd** | CSDL key-value của Kubernetes; cần quorum |
| **Executor** | Một khe chạy build trên agent |
| **Fail closed** | Không chắc thì từ chối |
| **Fence / fencing** | Cô lập chắc chắn một máy (tắt nguồn, xác nhận Off) trước khi giao tài nguyên của nó |
| **ForceOff** | Lệnh Redfish tắt nguồn cứng |
| **fsync** | Lời gọi đẩy dữ liệu file xuống đĩa và chờ |
| **Gate file** | File trong RAM mà cell agent ghi để guard biết Lease còn được giữ |
| **Guard** | Lớp bọc tiến trình Jenkins; giết JVM khi gate cũ quá hạn |
| **Harbor** | Registry ảnh container |
| **Headroom** | Chỗ trống trên máy khác đủ nhận controller nếu máy hiện tại chết |
| **Helm, chart, values, release** | Trình quản lý gói Kubernetes; bộ template; tham số; một lần cài |
| **Holder identity** | Định danh người giữ Lease: `<pod>/<uid>` |
| **Idempotency key** | Khoá giúp yêu cầu gửi lại được coi là cùng một yêu cầu |
| **Image volume** | Volume Kubernetes lấy nội dung từ một ảnh container, chỉ đọc |
| **Inbound agent** | Agent tự gọi về controller |
| **Jar cache** | Thư mục agent cất jar đã tải từ controller |
| **JCasC** | Jenkins Configuration as Code |
| **`JENKINS_HOME`** | Thư mục trạng thái của Jenkins |
| **Jenkinsfile, Pipeline** | File mô tả quy trình build bằng code; kiểu job tương ứng |
| **Kata Containers** | Chạy pod trong VM nhẹ có kernel riêng |
| **kubelet** | Tác nhân trên mỗi node |
| **kwok** | Giả lập node để thử quy mô |
| **Label (Jenkins)** | Nhãn trên agent mà build xin |
| **Lease** | Đối tượng Kubernetes thể hiện quyền có thời hạn |
| **Leader election** | Chọn một bản sao làm việc nhờ Lease |
| **Longhorn** | Storage khối phân tán cho Kubernetes |
| **`MaxGap`** | Khoảng trống quan sát tối đa (3 s); vượt thì supervisor quên và quan sát lại |
| **Namespace** | Nhóm đối tượng Kubernetes có quyền riêng |
| **`netciOnce`** | Bước Pipeline đảm bảo một khối chạy không quá một lần |
| **NodePort** | Cổng mở trên mọi node tới một Service |
| **NodeProvisioner** | Bộ quyết định khi nào Jenkins tạo thêm agent |
| **NodeStale** | kubelet im lặng 20 s |
| **Nonce** | Số ngẫu nhiên dùng một lần, phân biệt các lần thử |
| **Out-of-service taint** | Taint báo node đã tắt hẳn; Kubernetes xoá pod và gỡ volume không chờ kubelet |
| **Page cache, writeback** | Bộ đệm ghi trong RAM; việc kernel đẩy nó xuống đĩa |
| **Panic guard** | Không hành động khi quá nhiều máy hỏng cùng lúc |
| **PDB** | PodDisruptionBudget |
| **Pod** | Đơn vị chạy nhỏ nhất của Kubernetes |
| **PodGC** | Vòng điều khiển dọn pod |
| **Pool (fabric)** | Một loại sandbox: ảnh, tài nguyên, runtime, số pod ấm |
| **Preemption** | Đẩy pod ưu tiên thấp để lấy chỗ cho pod ưu tiên cao |
| **PriorityClass** | Mức ưu tiên của pod |
| **Queue (Jenkins)** | Hàng đợi build, trong RAM |
| **Quorum** | Đa số thành viên cần để một cụm (etcd) hoạt động |
| **Quiet check** | Chờ Lease của các cell khác trên máy im lặng 2 s trước khi nhả |
| **Redfish** | Chuẩn REST quản trị phần cứng |
| **Remoting** | Thư viện kênh nối agent–controller của Jenkins |
| **resourceVersion** | Phiên bản đối tượng, dùng cho cập nhật có điều kiện |
| **RetentionStrategy** | Khi nào Jenkins gỡ một agent |
| **Runbook** | Hướng dẫn xử lý một cảnh báo |
| **RuntimeClass** | Chọn runtime container cho pod |
| **RWO** | ReadWriteOnce: volume gắn vào một node mỗi lúc |
| **Sandbox** | Pod chạy một build, bị huỷ sau build |
| **Settle (storage)** | Khoảng chờ để storage xử lý xong việc node chết trước khi xoá pod |
| **Sidecar (native)** | Container chạy kèm suốt đời pod, khởi động trước container chính |
| **Soak** | Chạy lỗi ngẫu nhiên liên tiếp nhiều giờ |
| **Split-brain** | Hai bên cùng tin mình là chủ và cùng ghi |
| **StatefulSet** | Pod có tên và volume cố định |
| **STONITH** | Shoot The Other Node In The Head: fencing bằng cắt điện |
| **Supervisor** | Dịch vụ Go quan sát, fence và tiếp quản cell |
| **SuspectAfter** | 3 s Lease không đổi thì hỏi BMC |
| **sushy-tools** | Trình giả lập Redfish cho VM libvirt |
| **Taint / toleration** | Cơ chế đẩy pod khỏi node / cho pod chịu được |
| **Thread dump** | Ảnh chụp mọi luồng của một JVM tại một thời điểm (`jcmd <pid> Thread.print`) |
| **TokenReview** | API xác minh token ServiceAccount |
| **Warm pod** | Sandbox đã khởi động sẵn, chờ claim |
| **WebSocket** | Kênh hai chiều qua HTTP, agent dùng để nối về controller |
| **`X-Jenkins-Session`** | Header đổi giá trị mỗi lần controller khởi động |

## Phụ lục B — Sổ tay câu lệnh

```bash
# --- Lab: vòng đời ---
lab/vms.sh up|status|ssh N|down|reclaim
lab/k3s.sh up ; export KUBECONFIG=$PWD/.netci-gate/lab/kubeconfig
lab/longhorn-tune.sh ; lab/k3s-timings.sh upstream|k3s
lab/redfish.sh start|stop|secret ; lab/redfish.sh quirks start|stop     # BMC "khó tính" ở cổng 8001
lab/monitoring.sh install ; lab/kata.sh install netci-lab-3|check
MONITORING=on FENCE=redfish NETCI_IMAGE=...@sha256:... AGENT_JARS=...@sha256:... lab/helm-install.sh

# --- Build ---
make check ; make plugin
scripts/corp/push_image.sh <local> 172.17.0.1:8930/netci/<name>:<tag>      # in ra digest
docker build --network host -f jenkins/Dockerfile.controller -t netci/jenkins-controller:<tag> jenkins/
docker build --network host -f jenkins/Dockerfile.agent-jars --build-arg CONTROLLER_IMAGE=<controller> -t netci/jenkins-agent-jars:<tag> jenkins/

# --- Kiểm chứng ---
python3 lab/spike/chaos_poweroff.py --runs 3 --job once-probe [--failure node-hang-supervised]
python3 lab/spike/soak.py --hours 3 --seed 20261012
python3 lab/spike/fabric_timeline.py --runs 5 --burst 4
python3 lab/spike/agent_delay_probe.py --bursts 5 --size 4
python3 lab/spike/many_volumes_probe.py --cells 8 --runs 2
KWOK_DIR=<kwokctl+kwok 0.8.0> lab/scale/run.sh 100 300 5
deploy/helm/netci/ci/test-rules.sh ; ci/charts.sh

# --- Tắt/bật máy bằng tay (thí nghiệm) ---
virsh -c qemu:///system destroy netci-lab-2      # mất điện
virsh -c qemu:///system suspend netci-lab-2      # máy treo
virsh -c qemu:///system start netci-lab-2

# --- Quan sát ---
curl -s http://192.168.122.211:30909/api/v1/query --data-urlencode \
  'query=histogram_quantile(0.99, sum(rate(netci_supervisor_observation_seconds_bucket[10m])) by (le))'
```

## Phụ lục C — Bản đồ mã nguồn và ADR

```
cmd/cell-agent        sidecar: Lease, gate, guard, probe JENKINS_HOME, reload JCasC, plugin
cmd/supervisor        phát hiện, fence, tiếp quản, headroom, DNS
cmd/netci-queue       hàng đợi bền, webhook, netciOnce
cmd/netci-fabric      pool sandbox, claim, reconcile
cmd/netci-sandbox     entrypoint của sandbox: jar cache, long-poll binding, agent.jar
internal/lease        Holder (giữ Lease), Observer
internal/supervisor   collect.go, decide.go (chính sách), execute.go, headroom.go, validate.go
internal/fence        EnsureOff, redfish.go, ssh.go, config.go
internal/kubeclient   client sống sót qua API server chết
internal/runqueue     queue, dispatch, once
internal/fabric       API, store PostgreSQL, reconcile, pods.go (pod spec, jarCache)
internal/sandbox      bootstrap của sandbox
jenkins/plugin        NetciEndpoint, RunLookup, NetciOnceStep, NetciCloud/Launcher/Agent, NoDelayProvisioning, ProvisionOnEnqueue
jenkins/Dockerfile.controller, jenkins/Dockerfile.agent-jars, jenkins/agent-jars/
deploy/helm/netci     chart nền tảng (+ monitoring.yaml, ci/rules-test.yaml)
deploy/helm/netci-cell chart cell
lab/                  dựng lab, probe, chaos, soak, scale; bằng chứng ở lab/evidence/
```

ADR nên đọc theo thứ tự: **060** (cell, tiếp quản), **061/064** (fabric), **063** (queue), **065**
(`netciOnce`), **066** (client sống sót qua API server chết), **067** (`JENKINS_HOME` hỏng), **068** (log khi
tiếp quản), **069** (BMC thật, quy mô), **070** (settle storage), **071** (nâng cấp, phụ thuộc hỏng, DNS,
fabric tự lành, đồng hồ), **072** (jar cache). 001–059 là lịch sử của bản IDP. So sánh với CloudBees và
Medik8s: `docs/research/HA-LANDSCAPE.md`.


---
