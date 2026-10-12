# netCI — Trình bày toàn diện: từ nỗi đau đến hệ thống chạy được

> Bản 2, viết ngày 2026-10-12, cho nhánh `rearch/jenkins-ha-fabric` (commit `7706b02`).
> Viết cho người **chưa vững Jenkins và Kubernetes**: mọi khái niệm được giải thích từ đầu, trước khi
> được dùng. Đọc xong là đủ kiến thức, kỹ năng và các bước theo đúng thứ tự để **tự làm lại đề tài mà
> không cần AI**. Mọi con số đều có nguồn: một file bằng chứng trong `lab/evidence/`, một ADR trong
> `docs/decisions/`, hoặc mã nguồn. Chỗ nào chưa kiểm chứng thì được ghi rõ là chưa.

## Cách đọc

| Nếu bạn muốn... | Đọc |
|---|---|
| Hiểu *vì sao* có đề tài này | Phần 1 (câu chuyện), 5 (yêu cầu), 6 (tiêu chí), 7 (chọn công nghệ) |
| Học nền tảng trước (Jenkins, container, Kubernetes, storage, hệ phân tán, BMC) | Phần 2, 3, 4. **Đọc trước nếu chưa vững Jenkins/K8s** |
| Hiểu hệ thống làm việc ra sao | Phần 8 (kiến trúc), 9 (các luồng, từng giây một), 10 (từng thành phần) |
| Tự dựng lại | Phần 12 (các bước theo đúng thứ tự, kèm lệnh), Phụ lục B (sổ tay lệnh) |
| Biết hệ thống đã được chứng minh tới đâu | Phần 13 (kiểm chứng), 16 (giới hạn) |
| Vận hành, sửa sự cố | Phần 14, `docs/RUNBOOK.md` |
| Tránh lặp lại sai lầm | Phần 15 (bài học) |
| Tự học kỹ năng còn thiếu | Phần 17 (lộ trình + bài tập), 18 (câu hỏi tự kiểm tra) |
| Tra một từ lạ | Phụ lục A (thuật ngữ A–Z) |

Quy ước: **[Lab]** là cách làm trên lab 3 VM của đề tài; **[Prod]** là chỗ khác khi lên hạ tầng thật.

---

## Mục lục

1. Câu chuyện: một đêm Jenkins sập
2. Nền tảng 1 — Jenkins từ con số 0
3. Nền tảng 2 — Container và Kubernetes từ con số 0
4. Nền tảng 3 — Storage, hệ phân tán, phần cứng, quan sát
5. Từ nỗi đau thành yêu cầu
6. Tiêu chí: thế nào là "làm được"
7. Chọn giải pháp và công nghệ
8. Kiến trúc netCI
9. Các luồng chính, từng giây một
10. Các thành phần, đi sâu
11. Phần IDP của netCI (bài toán lớn)
12. Dựng lại từ đầu, theo đúng thứ tự
13. Kiểm chứng: test, chaos, soak, quy mô
14. Vận hành và xử lý sự cố
15. Những bài học đắt giá
16. Giới hạn và những gì chưa kiểm chứng
17. Bản đồ kỹ năng, lộ trình tự học và bài tập
18. Câu hỏi tự kiểm tra
- Phụ lục A — Thuật ngữ A–Z
- Phụ lục B — Sổ tay câu lệnh
- Phụ lục C — Bản đồ mã nguồn và ADR

---

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

---

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

---

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

---

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

---

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

---

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

---

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

---

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

---

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

---

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

---

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

---

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

---

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

---

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

---

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

---

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

---

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

---

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
