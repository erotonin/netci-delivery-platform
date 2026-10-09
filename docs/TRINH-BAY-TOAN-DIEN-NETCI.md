# netCI — Trình bày toàn diện: từ nỗi đau đến hệ thống chạy được

> Viết ngày 2026-10-09, cho nhánh `rearch/jenkins-ha-fabric` (commit `009c195`).
> Mục tiêu: đọc xong là đủ kiến thức, kỹ năng và các bước theo đúng thứ tự để **tự làm lại đề tài
> mà không cần AI**. Mọi con số đều có nguồn: một file bằng chứng trong `lab/evidence/`, một ADR
> trong `docs/decisions/`, hoặc mã nguồn. Chỗ nào chưa kiểm chứng thì được ghi rõ là chưa.

**Cách đọc.** Phần 1–4 kể câu chuyện: nỗi đau → yêu cầu → tiêu chí → chọn công nghệ. Phần 5 là
kiến thức nền từ con số 0 (đọc khi gặp từ lạ). Phần 6–8 là kiến trúc và cách nó chạy khi có sự cố.
Phần 9 là phần IDP cũ. Phần 10–12 là dựng lại từ đầu, kiểm chứng và vận hành. Phần 13–15 là bài
học, giới hạn và lộ trình kỹ năng. Phụ lục A là bảng thuật ngữ A–Z, phụ lục B là sổ tay câu lệnh.

---

## Mục lục

1. [Câu chuyện: một đêm Jenkins sập](#1-câu-chuyện-một-đêm-jenkins-sập)
2. [Từ nỗi đau thành yêu cầu](#2-từ-nỗi-đau-thành-yêu-cầu)
3. [Tiêu chí: thế nào là "làm được"](#3-tiêu-chí-thế-nào-là-làm-được)
4. [Chọn giải pháp và công nghệ](#4-chọn-giải-pháp-và-công-nghệ)
5. [Kiến thức nền từ con số 0](#5-kiến-thức-nền-từ-con-số-0)
6. [Kiến trúc netCI](#6-kiến-trúc-netci)
7. [Một lần mất điện, từng giây một](#7-một-lần-mất-điện-từng-giây-một)
8. [Các thành phần, đi sâu](#8-các-thành-phần-đi-sâu)
9. [Phần IDP (netCI 0.3) và vì sao bỏ](#9-phần-idp-netci-03-và-vì-sao-bỏ)
10. [Dựng lại từ đầu, theo đúng thứ tự](#10-dựng-lại-từ-đầu-theo-đúng-thứ-tự)
11. [Kiểm chứng: test, chaos, quy mô](#11-kiểm-chứng-test-chaos-quy-mô)
12. [Vận hành và xử lý sự cố](#12-vận-hành-và-xử-lý-sự-cố)
13. [Những bài học đắt giá (lỗi đã tìm ra)](#13-những-bài-học-đắt-giá-lỗi-đã-tìm-ra)
14. [Giới hạn và những gì chưa kiểm chứng](#14-giới-hạn-và-những-gì-chưa-kiểm-chứng)
15. [Bản đồ kỹ năng và lộ trình tự học](#15-bản-đồ-kỹ-năng-và-lộ-trình-tự-học)
- [Phụ lục A — Thuật ngữ A–Z](#phụ-lục-a--thuật-ngữ-az)
- [Phụ lục B — Sổ tay câu lệnh](#phụ-lục-b--sổ-tay-câu-lệnh)
- [Phụ lục C — Bản đồ mã nguồn và ADR](#phụ-lục-c--bản-đồ-mã-nguồn-và-adr)

---

## 1. Câu chuyện: một đêm Jenkins sập

### 1.1 Bối cảnh (use case thực tế)

Một công ty phần mềm cỡ vừa: vài chục đội, vài trăm repository. Mọi thứ build và deploy bằng
**Jenkins mã nguồn mở**: Jenkinsfile nằm trong từng repo, plugin tự chọn, agent là các **VM sống
lâu** (long-lived). Jenkins không chỉ build: nó còn **deploy** lên production, chạy migration cơ
sở dữ liệu, phát hành bản release.

Một đêm, máy vật lý chạy Jenkins controller mất điện (nguồn hỏng, kernel treo, card mạng chết,
không quan trọng là gì). Sáng hôm sau, những gì đội vận hành thấy:

| Hiện tượng | Vì sao (bản chất) |
|---|---|
| Không ai vào được Jenkins suốt lúc máy chết, và cả lúc khởi động lại | Chỉ có **một** controller. Nó là điểm chết duy nhất (single point of failure) |
| Các build đang chạy dở bị đánh dấu thất bại, hoặc treo | Pipeline chỉ chạy tiếp được nếu controller quay lại kịp và agent nối lại được |
| Các build đang **xếp hàng** biến mất, không dấu vết | Jenkins giữ hàng đợi trong RAM; `queue.xml` chỉ được ghi khi tắt **có trật tự** (JENKINS-30909). Mất điện thì không có lúc tắt có trật tự |
| Webhook từ GitLab gửi trong lúc sập: mất, hoặc gửi lại thì build hai lần | Không có chỗ bền vững nào nhận và khử trùng lặp webhook |
| Một bước deploy **chạy hai lần** sau khi khôi phục | Bản ghi "bước đã bắt đầu" nằm trong page cache, chưa xuống đĩa, nên mất cùng máy. Khi chạy tiếp, Jenkins tưởng bước chưa chạy |
| Khôi phục bằng backup mất hàng giờ, thiếu dữ liệu của những giờ gần nhất | Backup là khôi phục thảm hoạ, không phải chuyển đổi dự phòng (failover) |
| Build hôm sau lỗi lạ vì file rác của build hôm trước trên agent | Agent sống lâu tích tụ trạng thái từ build này sang build khác |
| Giờ cao điểm build xếp hàng dài, giờ thấp điểm VM agent nằm không | Năng lực cố định, không co giãn theo nhu cầu |

### 1.2 Vì sao không "mua CloudBees là xong"

CloudBees CI (bản thương mại của Jenkins) có tính năng **High Availability active/active**: nhiều
bản sao (replica) của một controller cùng chạy trên một file system dùng chung (NFS), trạng thái
sống giữ trong Hazelcast, và khi một replica chết thì replica khác "nhận nuôi" các Pipeline build
đang chạy. Nhưng:

- Tốn tiền bản quyền theo người dùng, và buộc vào một nhà cung cấp.
- Chính CloudBees ghi nhiều plugin bị giới hạn hoặc không hỗ trợ trong HA (Docker, EC2, một số
  thiết lập của Kubernetes plugin, Blue Ocean, file parameter).
- Công ty muốn giữ Jenkins mã nguồn mở, giữ Jenkinsfile và UI quen thuộc.

**Đề bài của netCI**: xây một lớp HA và một "agent fabric" bao quanh Jenkins mã nguồn mở, ngang
CloudBees ở những gì quan trọng (không mất build, không mất hàng đợi, không chạy trùng), đủ chất
lượng production, có thể bán được. Phần lõi viết bằng Go, plugin Jenkins viết bằng Java.

---

## 2. Từ nỗi đau thành yêu cầu

Mỗi nỗi đau ở 1.1 được chuyển thành một yêu cầu kiểm chứng được:

| # | Nỗi đau | Yêu cầu |
|---|---|---|
| R1 | Controller là điểm chết duy nhất | Khi máy của controller chết, controller được **tự động** chạy lại trên máy khác, không cần người |
| R2 | Build đang chạy bị mất | Pipeline đang chạy **chạy tiếp** trên controller mới, mỗi bước (step) chạy **đúng một lần** |
| R3 | Hai controller có thể cùng ghi một `JENKINS_HOME` (split-brain) | **Không bao giờ** có hai controller cùng ghi một `JENKINS_HOME`, kể cả khi mạng chập chờn hoặc máy chỉ treo chứ chưa chết |
| R4 | Hàng đợi mất | Mọi yêu cầu chạy (trigger) đã được nhận thì **không mất**: nhận nghĩa là đã ghi bền |
| R5 | Webhook mất hoặc build trùng | Webhook được xác thực và khử trùng lặp theo delivery id |
| R6 | Bước nguy hiểm chạy hai lần | Có cách đánh dấu một khối lệnh "chỉ được chạy một lần", nếu nghi ngờ thì **từ chối** thay vì chạy lại |
| R7 | Lỗi một chỗ lan ra cả hệ thống | Sự cố của một controller không làm sập controller khoẻ mạnh khác |
| R8 | Không ai biết hệ thống đang hỏng | Mọi lỗi cần người xử lý đều thành cảnh báo, kèm hướng dẫn xử lý (runbook) |
| R9 | Build bẩn vì agent sống lâu | Mỗi build có một sandbox mới, cô lập, bị huỷ sau build |
| R10 | Code không tin cậy (PR từ fork) chạy chung kernel | Mức cô lập theo độ tin cậy: code ngoài chạy trong VM riêng |
| R11 | Không xem được log lúc controller đang chuyển | Log build đọc được ngay cả khi controller đang được tiếp quản |
| R12 | Phải đổi Jenkinsfile, plugin, thói quen | Không đổi Jenkinsfile, không fork Jenkins; người dùng vẫn dùng UI Jenkins |

Các yêu cầu phi chức năng (cách làm, không phải tính năng):

- **Fail closed**: thiếu một thứ bắt buộc thì từ chối, không lặng lẽ chạy kiểu "giả lập".
- **Không bao giờ nói "đã chạy thật" khi chưa có bằng chứng** chạy trên hạ tầng thật.
- Mọi chuyển trạng thái quan trọng phải nguyên tử, idempotent, có audit, an toàn khi chạy đồng thời.
- Ảnh container được định danh bằng **digest**, không bằng tag có thể đổi.
- Không bao giờ log token, secret, private key.

---

## 3. Tiêu chí: thế nào là "làm được"

Yêu cầu phải có con số đo được, nếu không sẽ không biết lúc nào dừng.

| Tiêu chí | Ngưỡng | Vì sao chọn ngưỡng này |
|---|---|---|
| Thời gian tiếp quản (từ lúc mất điện đến lúc build chạy tiếp) | Phải **dưới 5 phút**; mục tiêu khoảng 1 phút | Một build đang chạy chờ agent tạm thời khoảng 5 phút rồi bỏ cuộc (workflow-durable-task-step). Quá 5 phút là mất build |
| Fence (cô lập máy chết) | Vài giây sau khi máy mất điện | Mọi thứ phía sau chờ bước này |
| Split-brain | **0** lần, trong mọi kịch bản kể cả máy treo và ánh xạ node–máy sai | Một lần là hỏng `JENKINS_HOME` |
| Dữ liệu mất khi mất điện | Tối đa khoảng 1 giây ghi | Giới hạn writeback (mục 8.1) |
| Trigger mất | 0 | R4 |
| Bước `netciOnce` chạy hai lần | 0; nghi ngờ thì từ chối | R6 |
| Cell khoẻ bị ảnh hưởng | 0 lần restart, 0 lần mất Lease | R7 |
| Quy mô | Theo kịp hàng trăm cell, mỗi lần quan sát vẫn dưới 1 s | Nhịp quan sát của supervisor là 1 s |
| Bằng chứng | Mỗi khẳng định gắn với một file trong `lab/evidence/` | Không có bằng chứng thì không được nói "đã chạy" |

Kết quả hiện tại, tóm tắt (chi tiết ở Phần 11): **hơn 80 lần mất điện không người can thiệp** trên
lab (81 lần theo README, cộng 10 lần của các chuỗi 21–24 ngày 2026-10-09), mọi build chạy tiếp và kết
thúc SUCCESS. Fence mất 2,7–3,8 s. Build thường chạy tiếp sau 40–50 s; khi leader của control plane
chết cùng máy thì tới 65–70 s. Chưa có lần split-brain nào, và mỗi khối `netciOnce` có đúng một marker.

---

## 4. Chọn giải pháp và công nghệ

### 4.1 Quyết định gốc: active/active hay "một người ghi, chuyển nhanh"?

| Phương án | Ưu | Nhược | Kết luận |
|---|---|---|---|
| **Active/active trên một `JENKINS_HOME`** (như CloudBees) | UI không bao giờ tắt | Jenkins mã nguồn mở giữ queue, số build, kênh agent **trong RAM** và ghi XML không phối hợp giữa các tiến trình. Hai JVM cùng ghi sẽ hỏng dữ liệu. Muốn làm phải viết lại lõi Jenkins và làm hỏng plugin | **Loại**, để lại dạng nghiên cứu |
| **Nhiều Jenkins độc lập + chia việc** | Đơn giản | Không giải quyết được việc một controller chết | Dùng làm *một phần*: mỗi "cell" giữ một nhóm đội |
| **Backup/restore (Velero)** | Có sẵn, đã làm ở bản 0.3 (ADR-055) | Tính bằng phút đến giờ, mất dữ liệu kể từ bản backup gần nhất | Chỉ để khôi phục thảm hoạ |
| **Một người ghi tại một thời điểm, volume được nhân bản, chuyển nhanh và an toàn** | Jenkins vẫn là Jenkins, plugin không đổi | UI tắt khoảng 1 phút khi chuyển | **Chọn** (ADR-060) |

**Bản chất của lựa chọn**: Jenkins không chia sẻ được trạng thái, vậy đừng bắt nó chia sẻ. Thay
vào đó: (1) chỉ cho đúng một controller chạy tại một thời điểm, (2) giữ đĩa của nó an toàn qua
việc mất máy, (3) chuyển nó đi thật nhanh, và (4) chứng minh không bao giờ có hai cái cùng chạy.

### 4.2 Từng lựa chọn công nghệ

| Vấn đề | Đã xét | Chọn | Lý do |
|---|---|---|---|
| Nền chạy controller và sandbox | VM thuần + script; Nomad; Kubernetes | **Kubernetes** (lab: **k3s** v1.36.4) | Có sẵn lập lịch, volume (CSI), `Lease`, taint `out-of-service` cho node chết (GA từ 1.28), PriorityClass, PDB. k3s nhẹ, 3 server với etcd nhúng, dựng nhanh trên 3 VM |
| `JENKINS_HOME` sống qua việc mất máy | NFS dùng chung; Ceph RBD; Longhorn; vSAN | **Volume khối ReadWriteOnce nhân bản đồng bộ** (lab: **Longhorn** v1.13.0, 3 bản sao) | RWO chỉ gắn vào một node mỗi lúc, thành thêm một lớp chống split-brain. Nhân bản đồng bộ nghĩa là máy chết không mất gì đã xuống đĩa. Longhorn cài bằng một manifest. Production có thể dùng Ceph RBD hay vSAN, miễn là CSI |
| Ai được làm controller | ZooKeeper/etcd riêng; khoá trong PostgreSQL; **Kubernetes Lease** | **Lease** (coordination.k8s.io) | Có sẵn trong cụm, cập nhật có điều kiện theo `resourceVersion`, có `leaseTransitions` dùng làm "epoch" |
| Phát hiện máy chết | Chỉ dựa vào node NotReady (40–50 s); NHC (Medik8s) | **Lease của chính cell** (nghi sau 3 s) **+ hỏi trạng thái điện của máy** | Nhanh mà không đoán: nghi ngờ thì rẻ, còn quyết định dựa trên câu trả lời của bộ điều khiển nguồn |
| Fencing (cô lập máy) | Chỉ xoá pod; reboot qua agent; **tắt nguồn qua BMC** | **Tắt nguồn (STONITH) và xác nhận Off** qua **Redfish** (server thật), hoặc SSH agent tới libvirt (VM lab) | Như Pacemaker: một máy "hình như chết" có thể vẫn đang ghi. Chỉ máy đã tắt hẳn mới chắc chắn không ghi |
| Ngôn ngữ dịch vụ | Python (bản 0.3); Go | **Go** | Mọi thành phần là controller hạ tầng: client-go, Cluster API, autoscaler đều viết bằng Go. Mỗi dịch vụ là một binary tĩnh, ảnh `FROM scratch` |
| Plugin trong Jenkins | Groovy script; shared library; **plugin Java** | **Plugin Java** (`jenkins/plugin`) | Dispatch idempotent cần giữ khoá hàng đợi của Jenkins, chỉ làm được từ bên trong |
| Hàng đợi bền | Jenkins queue; Kafka; **PostgreSQL** | **PostgreSQL** (netci-queue) | Đủ bền, giao dịch ACID, đã có sẵn; không cần thêm hạ tầng |
| Sandbox build | Kubernetes plugin; VM mỗi build; **pod ấm + bind muộn** | **netci-fabric**: pod ấm, bind muộn, RuntimeClass theo độ tin cậy | Pod ấm đã kéo ảnh và khởi động sẵn; bind vào controller khi cần |
| Cô lập code không tin cậy | runc chung kernel; gVisor; Sysbox; **Kata Containers** | **Kata** (`kata-qemu-runtime-rs`) cho pool `untrusted`; **runc + user namespace** cho `standard` | Kata cho mỗi build một VM có kernel riêng |
| Giám sát | Tự viết; **Prometheus + Grafana + Loki** | kube-prometheus-stack 87.10.1, Loki 3, Fluent Bit 5.1.3 | Chuẩn ngành; PodMonitor và PrometheusRule kiểm được bằng `promtool` |
| Đóng gói, cài đặt | YAML thuần; Kustomize; **Helm** | 2 chart: `netci` (nền tảng), `netci-cell` (mỗi cell một release) | Tổ chức cài như cài một sản phẩm |
| Registry ảnh | Docker Hub; **Harbor** | Harbor (lab `172.17.0.1:8930`, TLS bằng CA riêng) | Registry riêng, có robot account, tag bất biến |
| Thử quy mô | Dựng 300 VM; **kwok** | **kwok** 0.8.0 + control plane thật v1.36.1 | Node giả nhưng API server, etcd và scheduler là thật; supervisor chạy bằng binary thật |
| BMC để thử | Server thật; **sushy-tools** (Redfish emulator) + proxy mô phỏng "tính xấu" | sushy-tools ở cổng 8000, `lab/redfish-quirks.py` ở cổng 8001 | Không có phần cứng nên mô phỏng đúng hành vi đã được hãng công bố |

---

## 5. Kiến thức nền từ con số 0

Phần này giải thích mọi khái niệm mà các phần sau dùng tới. Nếu đã biết thì bỏ qua.

### 5.1 Máy ảo và lab

- **KVM**: tính năng ảo hoá trong kernel Linux. **QEMU** giả lập phần cứng cho VM.
  **libvirt** (lệnh `virsh`) quản lý VM: `virsh list`, `virsh destroy <vm>` (rút điện ngay, như mất
  điện), `virsh suspend` (đóng băng, như máy treo), `virsh start`.
- **cloud-init**: cấu hình VM ở lần khởi động đầu (user, SSH key, sysctl). `lab/vms.sh` dùng nó.
- **Nested virtualisation**: VM chạy được VM bên trong. Kata trong lab cần tính năng này
  (`/sys/module/kvm_intel/parameters/nested` = `Y`).
- **qcow2 thin disk**: đĩa ảo chỉ chiếm chỗ thật khi được ghi. Nếu đĩa máy chủ đầy, QEMU **tạm dừng
  (pause)** VM (`virsh list` báo `paused`). Lab đã gặp chuyện này.

### 5.2 Container

- **Container**: tiến trình bị cô lập bằng *namespace* (pid, mount, network, user...) và giới hạn
  bằng *cgroup* (CPU, RAM).
- **runc**: chương trình tạo container (chuẩn OCI). **containerd**: daemon quản lý container mà
  k3s dùng.
- **User namespace** (`hostUsers: false`): root trong container được ánh xạ thành một uid không
  có quyền trên máy thật.
- **seccomp**: lọc system call. **capabilities**: chia nhỏ quyền root (`drop: [ALL]`).
- **Image digest** (`sha256:...`): định danh bất biến của ảnh; khác với tag (`:1.0`), thứ có thể bị
  ghi đè.
- **Kata Containers**: chạy mỗi pod trong một VM nhẹ có kernel riêng. **RuntimeClass** là cách pod
  chọn runtime (runc hay kata).

### 5.3 Kubernetes: những đối tượng cần biết

| Khái niệm | Là gì | Dùng trong netCI để |
|---|---|---|
| **Node** | Một máy (VM hay server) trong cụm, có `kubelet` chạy trên đó | Mỗi máy của lab là một node |
| **kubelet** | Tác nhân trên mỗi node, chạy pod, báo trạng thái, gia hạn **node lease** mỗi 10 s | Supervisor coi kubelet "chết" khi node lease không đổi 20 s (`NodeStale`) |
| **Control plane** | `kube-apiserver` (cổng vào duy nhất), **etcd** (CSDL), `kube-scheduler` (chọn node cho pod), `kube-controller-manager` (các vòng điều khiển) | Trong lab, cả 3 máy đều là control plane |
| **etcd quorum** | etcd cần đa số thành viên (2/3) còn sống mới ghi được | Không bao giờ tắt một máy nếu việc đó làm mất đa số |
| **Leader election** | Nhiều bản sao, một bản làm việc, nhờ một Lease | controller-manager, scheduler, csi-attacher, supervisor, fabric đều dùng. Leader chết cùng máy thì mất thêm vài chục giây |
| **Pod** | Đơn vị chạy: một hoặc nhiều container dùng chung mạng và volume | Controller Jenkins là một pod |
| **Init container, sidecar** | Container chạy trước container chính; *native sidecar* là init container có `restartPolicy: Always`, chạy suốt cùng pod | `cell-agent` là native sidecar |
| **StatefulSet** | Pod có tên cố định (`jenkins-0`) và volume riêng theo `volumeClaimTemplates` | Mỗi cell là một StatefulSet 1 bản sao. Tên cố định nghĩa là pod mới chỉ được tạo khi pod cũ đã bị xoá khỏi API |
| **Deployment, DaemonSet** | Deployment: N pod giống nhau. DaemonSet: mỗi node một pod | supervisor/queue/fabric là Deployment; longhorn-manager là DaemonSet |
| **Service, EndpointSlice** | Địa chỉ ổn định cho một nhóm pod; EndpointSlice liệt kê địa chỉ thật | Service `kubernetes` trỏ tới các API server |
| **PV, PVC, StorageClass** | PVC là "yêu cầu đĩa", PV là "đĩa thật", StorageClass là "loại đĩa" | `JENKINS_HOME` là một PVC Longhorn |
| **CSI** | Chuẩn plugin storage: `ControllerPublish`/`Unpublish` (gắn/gỡ volume vào node), `NodeStage`/`Publish` (mount) | Longhorn CSI |
| **VolumeAttachment** | Đối tượng ghi "volume X đang gắn vào node Y" | Supervisor đếm attachment trên node đã fence |
| **ReadWriteOnce (RWO)** | Volume chỉ gắn vào một node mỗi lúc | Thêm một lớp chống hai controller cùng ghi |
| **Lease** (coordination.k8s.io) | Đối tượng nhỏ: `holderIdentity`, `renewTime`, `leaseDurationSeconds`, `leaseTransitions` | Mỗi cell có Lease `jenkins` |
| **resourceVersion** | Phiên bản của một đối tượng; cập nhật kèm resourceVersion cũ sẽ bị từ chối nếu ai đó đã sửa trước (409 Conflict) | Nhả Lease "có điều kiện": chỉ nhả nếu Lease đúng như lúc quan sát |
| **Taint, toleration** | Taint trên node đẩy pod đi; toleration cho pod chịu được taint. Hiệu ứng `NoSchedule` (không xếp mới) hoặc `NoExecute` (đuổi pod đang chạy) | Taint `node.kubernetes.io/out-of-service` |
| **Non-graceful node shutdown** (KEP-2268) | Taint `node.kubernetes.io/out-of-service:NoExecute` báo rằng node đã tắt hẳn: pod trên đó bị xoá, volume được gỡ **không chờ kubelet** | Bước then chốt sau khi xác nhận máy Off. KEP đòi máy *thật sự* tắt, nên supervisor xác nhận Off trước |
| **PodGC, taint-eviction controller** | Hai vòng điều khiển trong controller-manager: một cái xoá pod trên node có taint NoExecute, một cái force-delete pod đang terminating trên node out-of-service | ADR-070: chúng từng "chạy đua" với supervisor |
| **PriorityClass, preemption** | Pod ưu tiên cao có thể đẩy (preempt) pod ưu tiên thấp khi thiếu chỗ | `netci-platform` 1 000 000, `netci-cell` 900 000, `netci-sandbox` -10 |
| **PodDisruptionBudget (PDB)** | Giới hạn số pod được "làm gián đoạn tự nguyện" (eviction, drain); scheduler cố tránh vi phạm PDB khi preempt | Build đang chạy có PDB để tiếp quản không giết nhầm build |
| **RBAC, ServiceAccount** | Quyền: Role/ClusterRole + Binding; danh tính của pod | Cell chỉ được get/update đúng Lease `jenkins` của nó |
| **TokenReview, projected token** | API kiểm tra token của một ServiceAccount; token gắn audience, có hạn | Sandbox chứng minh "tôi là pod X" với fabric |
| **Admission policy** | Quy tắc sửa hoặc chặn đối tượng khi được tạo (MutatingAdmissionPolicy, CEL) | Tuning Longhorn (`deploy/longhorn/tuning-policy.yaml`) |
| **Informer/watch và list** | Watch nhận thay đổi đẩy về; list là đọc trọn | Supervisor cố ý **list** mỗi giây, không dùng watch: một watch tới API server đã chết trông như "không có gì đổi" |
| **ConsistentListFromCache** (KEP-2340) | Từ 1.31 (GA 1.34), list nhất quán được phục vụ từ watch cache, không đọc etcd | Lý do 2 list/giây là rẻ |
| **Helm** | Trình quản lý gói: chart (template) + values = manifest | Chart `netci`, `netci-cell` |
| **kwok** | Giả lập node và pod (không có kubelet thật) trên control plane thật | Thử 600 cell / 200 máy |

### 5.4 Jenkins

- **Controller** (trước gọi là master): JVM Jenkins, giữ cấu hình, job, lịch sử build, hàng đợi,
  UI. **Agent**: máy hay tiến trình chạy build; nối tới controller qua TCP inbound hoặc **WebSocket**.
  **Executor**: một "khe" chạy build trên agent.
- **`JENKINS_HOME`**: thư mục chứa toàn bộ trạng thái (XML cấu hình, build, log). Đây chính là thứ
  phải sống sót khi mất máy.
- **Pipeline** (Jenkinsfile) và **durability**: một Pipeline lưu tiến trình xuống đĩa. Bước `sh` là
  một tiến trình trên agent và *sống lâu hơn controller*: khi controller khởi động lại, build
  **chạy tiếp** ở đúng bước đang dở, nếu agent nối lại được. Mức `MAX_SURVIVABILITY` (mặc định) là
  bền nhất.
- **Queue**: hàng đợi build, nằm trong RAM. `queue.xml` chỉ được ghi khi tắt có trật tự.
- **JCasC** (Configuration as Code): cấu hình Jenkins bằng một file YAML; đổi file là đổi cấu hình.
- **Job DSL / seed job**: tạo job bằng code. Bài học lab: job tạo bằng JCasC lúc khởi động có thể
  bị pha "Loaded all jobs" ghi đè.
- **Cloud** (trong Jenkins): API để Jenkins *tự tạo agent* khi cần (Kubernetes plugin, EC2...).
  netCI có cloud riêng tên `netci`.
- **`NodeProvisioner`**: bộ quyết định khi nào tạo agent. Mặc định chờ 100 s sau khởi động, rồi
  10 s một lần, nên netCI chỉnh để tạo agent ngay.
- **`X-Jenkins-Session`**: header đổi giá trị mỗi lần controller khởi động. netCI dùng nó để biết
  controller "đã là một phiên khác".

### 5.5 Hệ phân tán: những ý tưởng nền

- **Split-brain**: hai bên cùng tin mình là "chủ" và cùng ghi, dữ liệu hỏng. Đây là kẻ thù số một.
- **Lease** (Gray & Cheriton, 1989): quyền có thời hạn. Người giữ phải gia hạn; **không gia hạn được
  thì phải tự dừng *trước* khi lease hết hạn**, để người khác có thể nhận lease mà không chồng lấn.
- **Fencing, STONITH** ("Shoot The Other Node In The Head", Pacemaker): không tin vào việc bên kia
  "chắc đã dừng". Phải *làm cho nó dừng*, ví dụ cắt điện, trước khi giao tài nguyên cho người khác.
  Kleppmann (2016) chỉ ra rằng khoá thôi là không đủ, vì một tiến trình bị dừng tạm (GC pause) có thể
  tỉnh dậy và vẫn ghi.
- **Fencing token, epoch**: số tăng dần mỗi lần đổi chủ (ở đây là `leaseTransitions`). Người ghi cũ
  mang số cũ thì bị từ chối.
- **Failure detector** (Chen–Toueg–Aguilera; φ-accrual): phát hiện càng nhanh thì càng hay nghi nhầm.
  netCI tách hai việc: *nghi* thì nhanh và rẻ (3 s, chỉ để đi hỏi); *quyết* thì dựa vào sự thật (máy
  báo Off).
- **Panic guard** (như node controller của Kubernetes, `--unhealthy-zone-threshold` 0.55, hay
  `minHealthy` của Medik8s NHC): khi quá nhiều thứ hỏng cùng lúc, nguyên nhân thường là chung (mạng,
  API server), nên đừng hành động. Phải đếm theo **máy**, không theo cell.
- **Idempotent**: làm lại nhiều lần cho cùng kết quả như làm một lần. Dispatch, nhả Lease, xoá pod
  đều phải idempotent.
- **Fail closed**: khi không chắc thì từ chối, không đoán.

### 5.6 Phần cứng và điều khiển nguồn

- **BMC** (Baseboard Management Controller): máy tính nhỏ trên bo mạch server, chạy cả khi server
  tắt, cho phép bật/tắt nguồn từ xa. Hãng gọi là **iDRAC** (Dell), **iLO** (HPE), **OpenBMC**...
- **IPMI**: giao thức cũ. **Redfish** (DMTF): REST/JSON qua HTTPS, chuẩn hiện đại.
  - `GET /redfish/v1/Systems/<id>` cho `PowerState` (`On`/`Off`).
  - `POST .../Actions/ComputerSystem.Reset` với `{"ResetType":"ForceOff"}` để tắt cứng.
- **Hành vi thật của BMC** (ADR-069): trả lời chậm (0,5–3 s), có lúc bận (503), `PowerState` vẫn báo On
  vài giây sau ForceOff, iDRAC trả 409 "already powered OFF" nếu máy vừa tự mất điện, 409 khi đang
  "settle" sau một lần đổi nguồn.
- **sushy-tools**: trình giả lập Redfish đặt trước libvirt (ở lab).

### 5.7 Longhorn (đủ để hiểu các bài học ở Phần 13)

- Mỗi volume có một **engine** (chạy trên node đang dùng volume) và N **replica** (bản sao trên các
  node). Engine ghi đồng bộ vào mọi replica.
- **instance-manager**: pod trên mỗi node chạy engine và replica.
  **longhorn-manager**: DaemonSet điều khiển, có "chủ sở hữu" (owner) cho từng volume.
- **CSI plugin** của Longhorn: hàm `ControllerPublishVolume` chờ volume "published" (attachment được
  thoả, trạng thái `attached`, engine có endpoint), hỏi lại mỗi 2 s, tối đa 90 s. **Engine monitor**
  chỉ điền endpoint ở lần đồng bộ, **mỗi 5 s** (`EnginePollInterval`), nên attach có hai mức, khoảng
  4 s hoặc khoảng 10 s.
- Tham số quan trọng: `numberOfReplicas`, `staleReplicaTimeout`, `storage-over-provisioning-percentage`
  (lab 100%), `storage-minimal-available-percentage` (25%), `disable-scheduling-on-cordoned-node` (true).

### 5.8 Quan sát (observability)

- **Prometheus**: thu thập metric theo kiểu kéo (scrape). **PromQL**: ngôn ngữ truy vấn.
  **PrometheusRule**: luật cảnh báo (`expr`, `for`). **PodMonitor**: chỉ cho Prometheus biết scrape
  pod nào. **promtool**: kiểm luật và chạy unit test cho luật.
- **Grafana**: dashboard. **Loki**: lưu log, truy vấn bằng LogQL. **Fluent Bit**: tác nhân đọc file
  log rồi đẩy đi.
- **Histogram, p99**: phân bố thời gian; p99 là mức mà 99% số lần nhanh hơn.

---

## 6. Kiến trúc netCI

### 6.1 Hình tổng thể

```
                          ┌──────────── namespace netci-system ────────────┐
 GitLab/GitHub webhook ──►│ netci-queue (2 bản sao) ──► PostgreSQL           │
 API / người dùng ───────►│   nhận bền, khử trùng lặp, dispatch idempotent    │
                          │ netci-supervisor (2 bản sao, 1 leader)           │
                          │   quan sát Lease mỗi 1 s, hỏi BMC, fence, dọn     │──► BMC (Redfish) / SSH agent
                          │ netci-fabric (1 leader)                          │
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
        │ netci-sandbox → agent.jar -webSocket → controller         │
        │ RuntimeClass: runc+userns (standard) | kata (untrusted)   │
        └───────────────────────────────────────────────────────────┘
        Prometheus + Grafana (monitoring), 15 cảnh báo, 1 dashboard
```

### 6.2 Khái niệm "cell"

Một **cell** là một Jenkins controller: một JVM, một `JENKINS_HOME` riêng trên volume nhân bản,
phục vụ một nhóm đội (tenant). Lỗi, plugin hỏng hay một lần nâng cấp chỉ chạm tới một cell. Mọi cell
được dựng từ cùng nguồn: ảnh controller và plugin từ `toolchain/versions.yaml`, cấu hình từ JCasC,
job từ Job DSL trong git, credential từ kho secret theo cell. Không bao giờ chép `credentials.xml`
hay `secrets/master.key` giữa các cell.

### 6.3 Ba lớp chống split-brain (R3)

1. **Cell agent và guard**: không gia hạn được Lease trong 10 s (`renewDeadline`) thì agent giết
   Jenkins, trước khi Lease 15 s hết hạn. Nếu chính agent treo, **guard** (bọc tiến trình Jenkins)
   thấy "gate file" cũ quá hạn và tự giết JVM.
2. **Supervisor**: chỉ nhả Lease thay một holder sau khi **bộ điều khiển nguồn báo máy Off**. Lệnh
   nhả là **có điều kiện** theo đúng `resourceVersion` đã quan sát; nếu Lease vừa được gia hạn thì
   dừng ngay (`FenceAborted`).
3. **Storage**: volume RWO chỉ gắn vào một node mỗi lúc.

---

## 7. Một lần mất điện, từng giây một

Ví dụ thật (chuỗi chaos 19, trung vị của 6 lần). Máy `netci-lab-2` đang chạy controller của
`cell-b` và một build 240 giây.

| t (s) | Sự kiện | Ai làm |
|---|---|---|
| 0 | `virsh destroy netci-lab-2`: máy mất điện | (lỗi) |
| 0–1 | Cell agent không gia hạn được nữa (máy đã tắt) | — |
| ~3 | Supervisor thấy Lease không đổi đủ `SuspectAfter` = 3 s, nên **hỏi BMC**: `PowerState` = Off | supervisor |
| ~3,3 | **Fence**: (1) xác nhận Off; (2) nhả Lease có điều kiện, ghi annotation `netci.io/fenced`; (3) thêm taint `out-of-service`; (3b) ghi node NotReady (để storage phản ứng ngay, không chờ 40–50 s); (5) nhả Lease của các cell khác trên cùng máy (sau `QuietCheck` 2 s); (6) xoá mọi pod **không phải cell** trên node (kể cả longhorn-manager của node chết) | supervisor |
| ~6,5 | `StorageSettle` 3 s sau taint: recovery **force-delete pod cell** (chỉ pod không còn gia hạn) | supervisor |
| ~7 | StatefulSet tạo `jenkins-0` mới; scheduler đặt sang máy khác (priority `netci-cell` cao hơn build) | Kubernetes |
| 7–20 | Volume được gỡ khỏi node chết, gắn sang node mới (Longhorn), rồi mount | CSI, Longhorn |
| ~22 | Cell agent mới giữ Lease (epoch +1); guard mở cổng cho Jenkins | cell-agent |
| 22–40 | Jenkins khởi động (khoảng 17 s), đọc `JENKINS_HOME`, nạp lại Pipeline | Jenkins |
| ~44 | Agent của build nối lại (WebSocket), bước `sh` đang dở **chạy tiếp** | agent |
| ~51 | Build tiếp tục ghi log, sau đó kết thúc SUCCESS | — |

Phân rã chuỗi 19 (trung vị): fence 3,3 s, volume và pod 22,4 s, Jenkins khởi động 16,6 s, agent nối
lại 3,9 s, **tổng 51,2 s**. Phần của netCI là khoảng 3 s; phần còn lại là storage và Jenkins.

Trường hợp **máy treo** (không tắt, chỉ im lặng): supervisor chờ kubelet im lặng đủ 20 s
(`NodeStale`), chỉ khi việc tắt máy không làm mất đa số etcd, rồi **ForceOff qua Redfish** và chờ
Off (tối đa `offTimeout` 60 s), sau đó đi tiếp như trên. Lab: máy tắt sau 19–22 s, build chạy tiếp
sau 69–83 s.

---

## 8. Các thành phần, đi sâu

### 8.1 Dữ liệu sống sót qua mất điện (`JENKINS_HOME`)

Nhân bản không tự làm cho mất điện trở nên an toàn: Longhorn chỉ nhân bản thứ đã xuống thiết bị
khối, mà Jenkins để phần lớn dữ liệu ghi nằm trong page cache. Spike đầu tiên cho thấy trạng thái
Pipeline lùi khoảng 20 s và một bước chạy hai lần. Cách giải:

- **Giới hạn writeback** trên node: `vm.dirty_expire_centisecs=100`, `vm.dirty_writeback_centisecs=100`
  (`lab/vms.sh` ghi vào `/etc/sysctl.d/91-netci-writeback.conf`).
- **ext4 commit 1 s**: StorageClass `longhorn-commit1` với `mountOptions: [commit=1]`. Hoặc
  `longhorn-sync` với `[sync, dirsync]` (an toàn hơn, chậm hơn).
- WAR và plugin giải nén nằm trên đĩa cục bộ của pod (`--webroot`, `--pluginroot`), vì chúng không
  phải trạng thái.
- Phần một giây còn lại do `netciOnce` lo (8.6).

### 8.2 Cell agent (`cmd/cell-agent`, `internal/cellagent`)

- Giữ Lease `jenkins`: thời hạn 15 s, `renewDeadline` 10 s, gia hạn mỗi 1 s, mỗi lần thử tối đa
  0,3 s trên một kết nối mới (ADR-066).
- **Gate file** (`/run/netci/gate`, trong emptyDir bộ nhớ): agent ghi "tôi đang giữ Lease". **Guard**
  (`cell-agent guard -- jenkins.sh`) chỉ khởi động Jenkins khi gate mở, và giết JVM khi gate cũ quá
  `renewDeadline`.
- **Probe `JENKINS_HOME`** (ADR-067): cứ 5 s ghi một file, fsync, rename, fsync thư mục. Ba lần lỗi
  liên tiếp thì báo lên Lease (`netci.io/volume-failed`), supervisor restart pod đó.
- **JCasC reload nóng**: ConfigMap đổi thì agent gọi reload (nếu có `casc-reload-token`), không cần
  restart.
- **Prune plugin**: xoá khỏi `JENKINS_HOME` các plugin mà ảnh không mang theo.

### 8.3 Cell Supervisor (`cmd/supervisor`, `internal/supervisor`)

Ba phần tách bạch:
- **Collector** (`collect.go`): mỗi giây đọc trực tiếp, không dùng cache, gồm StatefulSet có nhãn
  `netci.io/cell`, Node, node lease, VolumeAttachment, Lease của mọi cell (1 list), pod của mọi cell
  (1 list), và trạng thái điện của máy khi cần. Nếu khoảng trống giữa hai lần quan sát thành công
  vượt `MaxGap` (3 s), nó **quên** hết và quan sát lại từ đầu.
- **Decide** (`decide.go`): hàm thuần, đầu vào là bức tranh, đầu ra là danh sách hành động
  (`FenceNode`, `DeletePod`, `ForceDeletePods`, `Unfence`, `PowerOn`, `Alert`). Toàn bộ chính sách
  nằm ở đây, nên test được hết.
- **Executor** (`execute.go`): thực hiện, theo thứ tự an toàn.

Các ngưỡng (`DefaultConfig`):

| Tham số | Giá trị | Ý nghĩa |
|---|---|---|
| `SuspectAfter` | 3 s | Lease không đổi chừng này thì hỏi BMC |
| `NodeStale` | 20 s | kubelet im lặng chừng này thì coi là không quản lý được pod |
| `StuckPodAfter` | 30 s | Lease không đổi mà kubelet sống: restart pod |
| `Cooldown` | 2 phút | Sau một hành động, không hành động lại với cell đó |
| `PanicFraction` | 0,5 | Hơn nửa số **máy** có cell hỏng cùng lúc: không tắt máy nào, chỉ cảnh báo |
| `PowerOnAfter` | 30 s | (lab) bật lại máy đã fence |
| `StorageSettle` | 3 s | Chờ storage "thấm" việc node chết rồi mới xoá pod cell (ADR-070) |
| `QuietCheck` | 2 s | Lease của các cell khác trên máy phải im lặng chừng này mới được nhả |
| `offTimeout` / `queryTimeout` | 60 s / 3 s | BMC phải báo Off trong 60 s; mỗi lần hỏi tối đa 3 s |

Các chốt an toàn khác:
- **Kiểm ánh xạ node–máy lúc khởi động** (`validate.go`): đối chiếu với heartbeat của kubelet; sai
  thì không hành động.
- **Đổi cấu hình fence** (đổi mật khẩu BMC) thì supervisor tự khởi động lại và kiểm ánh xạ lại
  (dấu vân tay symlink `..data` của Secret).
- **Không bao giờ xoá pod của cell còn gia hạn Lease** (ADR-070).
- Mỗi máy chỉ fence **một lần** cho mỗi quyết định.
- Kiểm **headroom**: báo trước nếu một cell mất máy thì không còn chỗ (`NetciCellWithoutHeadroom`).

### 8.4 Fence (`internal/fence`)

- Giao diện `Fencer`: `State(machine)`, `PowerOff`, `PowerOn`.
- **Redfish** (`redfish.go`): HTTPS với TLS ghim bằng SHA-256 (hoặc CA), basic auth, đọc thông điệp
  lỗi kiểu iDRAC.
- **`EnsureOff`**: gửi ForceOff, rồi hỏi trạng thái cho tới hạn chót, gửi lại ForceOff mỗi 2 s. Chỉ
  dừng sớm khi bị **từ chối** (400/401/403/404/405); 409 hay 503 không phải từ chối. Chỉ khi BMC báo
  Off mới tính là Off.
- **SSH agent** (lab, libvirt): một lệnh bắt buộc (forced command) chỉ đọc, tắt, bật được VM tên
  `netci-lab-<n>`.
- `fence.json`:

```json
{"nodes": {"netci-lab-1": {"redfish": {
   "endpoint": "https://192.168.122.1:8000",
   "system": "/redfish/v1/Systems/<uuid>",
   "credentials": "/etc/netci/fence/bmc",
   "requestTimeout": "5s"}}}}
```

`credentials` là thư mục chứa `username`, `password`, `tls-sha256` (mount từ Secret).

### 8.5 netci-queue và plugin Java (ADR-063)

- **Nhận nghĩa là bền**: `POST /v1/runs` (API) hoặc `POST /v1/hooks/{name}` (webhook) được ghi thành
  một dòng PostgreSQL *trước khi* trả lời. Client được xác định từ credential, không bao giờ lấy từ
  request. Delivery id của webhook là khoá idempotency.
- **Dispatch idempotent**: plugin cung cấp `POST /netci/dispatch {job, runId, params}`. Nó giữ **khoá
  hàng đợi** của Jenkins, tìm run trong hàng đợi, trong executor vừa nhận việc và trong các build gần
  đây. Thấy thì trả về, không thấy thì xếp lịch kèm `NetciRunAction(runId)`. Gọi lại bao nhiêu lần
  cũng không tạo build thứ hai.
- **Gửi lại đến khi chạy**: nếu chưa thấy phiên controller hiện tại (`X-Jenkins-Session`) xác nhận
  run, dispatcher gửi lại. Controller mới sau mất điện thì không có run trong hàng đợi, nên run được
  xếp lại.
- **Admission**: giới hạn số run đã dispatch mà chưa chạy trên mỗi cell.
- Lab: giết JVM khi có 6 mục trong hàng đợi. 5 run qua netci-queue đều chạy đúng một lần; trigger đi
  thẳng vào Jenkins thì mất (đúng như dự đoán).

### 8.6 `netciOnce` (ADR-065)

```groovy
netciOnce('deploy-prod') {
  sh './deploy.sh'
}
```

- Trước khi chạy thân khối, plugin ghi `(build, key, nonce)` vào PostgreSQL qua `POST /v1/once`.
  Bản ghi này nằm *ngoài* `JENKINS_HOME`, nên không bị cuốn theo khi đĩa lùi lại một giây.
- Lần đầu: chạy. Đã có với **nonce khác**: khối từng bắt đầu ở một trạng thái đã mất, nên **từ chối**
  và để người quyết định (`retry: true`). Cùng nonce: chỉ là gọi lại, cho chạy.
- Không gọi được queue thì không chạy (fail closed).
- Lab: 51 lần mất điện giữa khối `netciOnce`, mỗi lần đúng một marker, không lần nào bị từ chối nhầm.

### 8.7 Agent fabric (ADR-061, ADR-064)

- **Pool**: ảnh, tài nguyên, RuntimeClass, nhãn; giữ sẵn N pod ấm.
- **Bind muộn**: Jenkins cần executor → cloud `netci` gửi `POST /v1/claims {cell, pool, agent, secret,
  controller}` → fabric lấy một pod ấm (nguyên tử) và giữ binding **chỉ trong RAM** (secret không
  bao giờ xuống CSDL) → `netci-sandbox` trong pod đang long-poll `GET /v1/binding`, nhận binding,
  chạy `agent.jar -webSocket` → build chạy → `DELETE /v1/claims/{id}` và pod bị xoá. Mỗi sandbox
  phục vụ một build.
- Pod chứng minh danh tính bằng **projected ServiceAccount token** (audience `netci-fabric`, 10 phút)
  qua TokenReview.
- Trạng thái trong PostgreSQL: `creating → warm → claimed → bound → released → deleted` (+ `failed`).
  Mọi chuyển trạng thái là compare-and-set và có audit. Reconciler đối chiếu mỗi giây.
- Pod bận mang nhãn `netci.io/busy`, có PDB `minAvailable: 1000000`, nên preemption tránh build đang
  chạy.
- NetworkPolicy: sandbox chỉ ra được DNS, fabric, controller và địa chỉ ngoài cụm.

### 8.8 Ưu tiên và chỗ trống

- PriorityClass: platform 1 000 000 > cell 900 000 > sandbox -10 (`preemptionPolicy: Never`).
- Build đang chạy có PDB, nên scheduler preempt pod rảnh trước. Thử trên scheduler thật (kwok):
  không có PDB thì 5/5 lần build bị giết, có PDB thì 0/5; khi chỉ còn chỗ của build, controller vẫn
  được đặt (2/2).
- PDB cho pod không có controller **phải** dùng `minAvailable` dạng số nguyên (`maxUnavailable` cho
  ra "undefined behavior").

### 8.9 Quan sát và cảnh báo

15 cảnh báo (`deploy/helm/netci/templates/monitoring.yaml`), mỗi cái có mục trong `docs/RUNBOOK.md`:
`NetciCellDown`, `NetciCellWithoutHeadroom`, `NetciCellsShareAMachine`, `NetciCellConfigurationRefused`,
`NetciComponentDown`, `NetciControllerUnreachable`, `NetciFabricNoWarmSandbox`,
`NetciFabricSandboxesFailing`, `NetciFenceLeaseMoved`, `NetciRunsLostWithAController`,
`NetciSupervisorBlind`, `NetciSupervisorNeedsAPerson`, `NetciSupervisorNotLeading`,
`NetciSupervisorSlowObservations`, `NetciTakeoverSlow`.

Log build sang Loki qua Fluent Bit (ADR-068): đọc trong Grafana bằng
`{cell="cell-b", job="<job>"} | build="<n>"`, kể cả lúc cell đang bị tiếp quản.

---

## 9. Phần IDP (netCI 0.3) và vì sao bỏ

Trước khi chuyển hướng, netCI là một **Internal Developer Platform** (tag `netci-0.3-cd-portal`):

- **Backend**: Python (FastAPI 0.141, uvicorn, psycopg 3), PostgreSQL là nguồn sự thật lúc xử lý
  request (ADR-014), migration là nguồn schema.
- **Portal**: React 19 + Vite. SSO qua **Keycloak** (OIDC); trình duyệt đăng nhập ở identity provider
  (ADR-034).
- **CD**: **Temporal** (workflow bền) điều khiển triển khai: SSH tới host, canary sau ingress-nginx,
  blue/green, rollback khi không có dữ liệu metric (ADR-031/035/046).
- **Chuỗi cung ứng**: build trong Jenkins, đẩy lên **Harbor**, ký bằng **cosign**, SLSA provenance
  (ADR-044), SBOM bằng **syft** và quét bằng **trivy** (ADR-045).
- **Quản trị**: policy, change freeze, phê duyệt hai người (ADR-047/058), admission và supersession
  build (ADR-050), tính chi phí CI (ADR-053), mô phỏng kế hoạch release theo DAG.
- Đã kiểm chứng trên hạ tầng thật của lab: onboarding, build `payments-api` qua 9 stage, đẩy
  registry, `cosign verify`, deploy qua SSH, SSO trên trình duyệt...

**Vì sao bỏ (ADR-060, ADR-062)**: công ty *đã* deploy bằng Jenkins. Nỗi đau thật nằm ở chỗ Jenkins
sập thì mất việc, chứ không phải thiếu một portal. Backend một tiến trình Python với `main.py` khoảng
7 000 dòng trộn lẫn mọi mối quan tâm, gỡ từng phần sẽ để lại đường dở dang. Nên repo được dựng lại
bằng Go quanh bài toán HA. Phần giữ lại: `toolchain/versions.yaml`, ảnh controller, GitLab, Harbor,
SeaweedFS và Velero của lab, mọi ADR. **Bài học kỹ năng**: dám bỏ một sản phẩm chạy được khi nó
không giải đúng nỗi đau.

---

## 10. Dựng lại từ đầu, theo đúng thứ tự

> Mọi lệnh chạy từ gốc repo trên **máy chủ lab** (host). Secret sinh vào `.netci-gate/` và không bao
> giờ commit. Ghi chú **[Prod]** là chỗ khác biệt khi lên server thật.

### Bước 0 — Chuẩn bị máy chủ

Yêu cầu: Ubuntu/Linux có `/dev/kvm`, nested virtualisation, libvirt (mạng `default`), Docker, Go
1.27, Helm, kubectl, Python 3, khoảng 30 GB RAM, **đĩa trống > 30 GB** (lab từng bị pause VM vì
đĩa đầy), ảnh cloud Ubuntu 24.04 ở `~/iso/noble-server-cloudimg-amd64.img`.

```bash
cat /sys/module/kvm_intel/parameters/nested      # phải là Y
virsh -c qemu:///system net-list --all           # mạng default active
df -h /                                          # còn chỗ
go version; helm version; kubectl version --client
```

### Bước 1 — Hạ tầng phụ trợ: CA riêng, Harbor, (GitLab)

```bash
scripts/corp/lab_ca.sh ca                                         # CA riêng của lab (một lần)
scripts/corp/lab_ca.sh issue harbor IP:172.17.0.1 DNS:localhost IP:127.0.0.1
# Cài Harbor bằng offline installer chính thức, cấu hình https với cert vừa cấp, cổng 8930.
# (Script cũ infra/corp/harbor/install.sh nằm ở tag netci-0.3-cd-portal.)
curl -fsS --cacert .netci-gate/corp/pki/ca.crt https://172.17.0.1:8930/api/v2.0/ping
```

Tạo project `netci` và robot account đẩy/kéo; credential để trong `.netci-gate/corp/`. **Bật tag
bất biến**. GitLab (cổng 8929) chỉ cần nếu thử webhook.

> Sau khi máy chủ khởi động lại, Harbor **không tự lên**: khởi động container theo thứ tự redis,
> harbor-db, registry, registryctl, harbor-portal, harbor-core, harbor-jobservice, nginx.
> [Prod] Dùng registry của tổ chức; chart từ chối ảnh không có digest.

### Bước 2 — Ba VM

```bash
lab/vms.sh up        # 3 guest: 4 vCPU, 5 GiB RAM, đĩa thin 40 GiB, IP cố định 192.168.122.211-213
lab/vms.sh status
lab/vms.sh ssh 1     # vào máy 1
```

`vms.sh` dùng cloud-init để đặt user, SSH key, sysctl writeback 1 s, và gán IP bằng DHCP
reservation, nhờ vậy khởi động lại không đổi địa chỉ (lab cũ từng mất quorum etcd vì đổi IP).

### Bước 3 — k3s và Longhorn

```bash
lab/k3s.sh up                      # k3s v1.36.4+k3s1: 3 server, etcd nhúng; Longhorn v1.13.0
export KUBECONFIG=$(lab/k3s.sh kubeconfig)
kubectl get nodes -o wide
kubectl -n longhorn-system get pods
```

Script này làm các việc sau:
- tin CA của Harbor trong containerd (`/etc/rancher/k3s/registries.yaml`);
- cài server đầu với `--cluster-init`, hai server sau với `--server https://<ip1>:6443`;
- tắt traefik và servicelb;
- `--tls-san` cả ba IP;
- token nằm trong một file, không lộ trên dòng lệnh.

Tuning Longhorn và timing control plane (ADR-066, ADR-067):

```bash
lab/longhorn-tune.sh               # admission policy: attacher mỗi node một bản, retry ≤ 5 s,
                                   # HTTP/2 health check 2 s + 2 s; loại StatefulSet khỏi auto-delete
lab/k3s-timings.sh upstream        # (tuỳ thí nghiệm) etcd election 1 s như upstream
```

> Chỉ thử MutatingAdmissionPolicy bằng `lab/longhorn/try-policy.sh`. Policy có phạm vi toàn cụm;
> từng có lần thay nhầm policy thật.
> [Prod] Đặt control plane trên máy không chạy cell (leader chết cùng máy cộng thêm 10–40 s cho mỗi
> lần tiếp quản). Storage: một StorageClass RWO nhân bản qua nhiều máy.

### Bước 4 — Build ảnh

```bash
make check                                     # vet, gofmt, test Go với -race, kiểm drift toolchain
make plugin                                    # plugin Java: jenkins/plugin/target/netci.hpi
# Ảnh netCI: LUÔN từ một worktree sạch của HEAD (make image từ chối cây "dirty")
git worktree add --detach /tmp/wt HEAD && (cd /tmp/wt && make image VERSION=0.4.0-dev.$(git rev-parse --short HEAD))
git worktree remove --force /tmp/wt
scripts/corp/push_image.sh netci/netci:<ver> 172.17.0.1:8930/netci/netci:<ver>   # in ra digest
# Ảnh controller: jenkins/Dockerfile.controller (Jenkins 2.555.3 LTS + plugins.txt; netci.hpi chép vào thành netci.jpi.override, xem Dockerfile)
docker build -f jenkins/Dockerfile.controller -t netci/jenkins-controller:2.555.3-netciN jenkins/
scripts/corp/push_image.sh netci/jenkins-controller:2.555.3-netciN 172.17.0.1:8930/netci/jenkins-controller:2.555.3-netciN
```

Dùng `push_image.sh` (buildah, chỉ tin CA của lab), **không** dùng `docker push`: Docker 29 lỗi
x509 khi lấy token.

### Bước 5 — PriorityClass, StorageClass, cell đầu tiên

```bash
kubectl apply -f lab/priorities.yaml
kubectl apply -f lab/spike/storageclass-sync.yaml -f lab/spike/storageclass-commit1.yaml
CELL=cell-a STORAGE_CLASS=longhorn-sync NODE_PORT=30080 NETCI_IMAGE=172.17.0.1:8930/netci/netci@sha256:<digest> lab/spike/deploy.sh
CELL=cell-b STORAGE_CLASS=longhorn-commit1 NODE_PORT=30081 NETCI_IMAGE=... lab/spike/deploy.sh
```

Một cell cần: ConfigMap JCasC (`jenkins.yaml`), Secret (mật khẩu, `fabric-token`, `once-token`,
`casc-reload-token`), pull secret Harbor. Ví dụ JCasC: `lab/spike/cell.yaml`.

### Bước 6 — Hàng đợi, fabric, supervisor

```bash
NETCI_IMAGE=...@sha256:<digest> lab/queue.sh        # PostgreSQL (lab) + 2 netci-queue, NodePort 30090
NETCI_IMAGE=...@sha256:<digest> lab/fabric.sh       # netci-fabric + token cho cell-b
# Supervisor, cách 1: SSH agent tới libvirt (đổi ~/.ssh/authorized_keys của host, có giới hạn)
NETCI_IMAGE=...@sha256:<digest> lab/supervisor.sh
# Cách 2 (khuyến nghị, giống server thật): Redfish qua sushy-tools
lab/redfish.sh start            # emulator ở 192.168.122.1:8000, TLS + basic auth, chỉ thấy netci-lab-1..3
lab/redfish.sh secret           # tạo Secret netci-fence-redfish và in values Helm
```

### Bước 7 — Giám sát và Kata

```bash
lab/monitoring.sh install       # kube-prometheus-stack (Prometheus :30909, Grafana :30300) + Loki trên host :3100
lab/kata.sh install netci-lab-3 # kata-deploy 4.2.0, RuntimeClass kata-qemu-runtime-rs (khởi động lại k3s node đó)
lab/kata.sh check               # kernel trong pod phải khác kernel của node
```

### Bước 8 — Cài toàn bộ bằng Helm (như một tổ chức sẽ cài)

```bash
MONITORING=on FENCE=redfish NETCI_IMAGE=172.17.0.1:8930/netci/netci@sha256:<digest> lab/helm-install.sh
helm list -A
```

Script này "nhận nuôi" (adopt) các đối tượng mà các bước trước đã tạo, rồi chạy
`helm upgrade --install` cho chart `netci` và cho từng cell (`netci-cell`). Volume của cell được giữ
nguyên.

> Bỏ `MONITORING`/`FENCE` ở lần chạy lại là **lặng lẽ quay về SSH fencing và mất PodMonitor**. Luôn
> truyền đủ.

Cài thủ công cho production:

```bash
helm upgrade --install netci deploy/helm/netci -n netci-system --create-namespace -f my-values.yaml \
  --set image.repository=REGISTRY/netci/netci --set image.digest=sha256:...
helm upgrade --install cell-payments deploy/helm/netci-cell -n cell-payments --create-namespace -f cell-values.yaml
```

Các giá trị quan trọng:
- **`netci`**: `supervisor.fence.configSecretName`, `supervisor.fence.items`,
  `supervisor.power.queryTimeout` (3s), `supervisor.power.offTimeout` (60s),
  `supervisor.storageSettle` (3s), `supervisor.autoPowerOn` (false ở prod),
  `queue.database.secretName`, `fabric.networkPolicy.clusterCIDRs` (bắt buộc).
- **`netci-cell`**: `image.controller`, `netci.digest`, `storage.className`, `casc.configMapName`,
  `secrets.secretName`, `outOfServiceTolerationSeconds` (30), `logShipping.*`,
  `kubernetesPlugin.enabled`.

### Bước 9 — Kiểm tra trước khi ai đó phụ thuộc vào nó

```bash
kubectl -n netci-system get lease netci-supervisor                 # có leader
kubectl -n netci-system logs deploy/netci-supervisor | grep "mapping checked"
curl -s http://192.168.122.212:30909/api/v1/query --data-urlencode 'query=netci_supervisor_cell_headroom'   # = 1
kubectl -n netci-system scale deploy netci-supervisor --replicas=0 # NetciSupervisorNotLeading phải bắn trong khoảng 2 phút
kubectl -n netci-system scale deploy netci-supervisor --replicas=2
deploy/helm/netci/ci/test-rules.sh                                 # promtool unit test cho cảnh báo
python3 lab/spike/chaos_poweroff.py --runs 3 --job once-probe      # diễn tập mất điện
python3 lab/spike/chaos_poweroff.py --runs 3 --job once-probe --failure node-hang-supervised   # máy treo
```

[Prod] Diễn tập trên **phần cứng và BMC thật của mình** trước khi chạy production.

---

## 11. Kiểm chứng: test, chaos, quy mô

### 11.1 Các tầng kiểm chứng

| Tầng | Lệnh | Kiểm gì |
|---|---|---|
| Unit Go | `make check` (`go test -race ./...`) | Mọi quy tắc của Decide; fence với BMC "xấu tính" (`bmc_quirks_test.go`); số request không tăng theo số cell (`collect_scale_test.go`) |
| Tích hợp PostgreSQL | `NETCI_QUEUE_TEST_DATABASE_URL=... make check-pg` | Queue và fabric với CSDL thật |
| Plugin | `make plugin` (JenkinsRule, SpotBugs) | Dispatch, `netciOnce` |
| Luật cảnh báo | `deploy/helm/netci/ci/test-rules.sh` | promtool |
| Chaos trên lab | `lab/spike/chaos_poweroff.py` | Mất điện hay máy treo thật, Jenkins thật, build 240 s |
| Probe chuyên đề | `agent_probe.py` (handover, hang, partition), `queue_crash_probe.py`, `webhook_probe.py`, `headroom_probe.py`, `logs_during_takeover.py`, `many_volumes_probe.py` | Từng cơ chế |
| Quy mô | `KWOK_DIR=... lab/scale/run.sh 100 300 5` | Supervisor thật với 100–600 cell |
| Preemption | `lab/scale/preemption.py --rounds 5` | Scheduler thật |

**Quy tắc viết test**: test viết *cùng* code, và phải **thất bại trên code cũ** (ví dụ test BMC fail
4/7 trên `EnsureOff` cũ; test "pod còn gia hạn" fail ở giây thứ 1 trên recovery cũ). Một test viết
từ cùng một hiểu lầm với code thì sẽ pass và chẳng chứng minh gì.

### 11.2 Kết quả chính (có bằng chứng)

| Kịch bản | Kết quả | Bằng chứng |
|---|---|---|
| Mất điện, không người can thiệp | Hơn 80 lần, mọi build SUCCESS, mất tối đa 1/240 dòng log | ADR-060, `chaos-poweroff-*.json` |
| Máy treo, ForceOff qua Redfish | 6/6, máy tắt sau 19–22 s | `chaos-poweroff-20261002T070357Z.json` |
| BMC "giống thật" (chậm 1,5 s, 503, trễ 8 s) | 3/3; xác nhận Off sau 22 s, nên timeout cũ 20 s là quá ngắn | `chaos-poweroff-20261009T020225Z.json` |
| Đổi mật khẩu BMC | Hai replica tự khởi động lại, kiểm lại ánh xạ trong 66 s | `chaos-poweroff-20261009T042237Z.json` |
| Quy mô 600 cell / 200 máy | Mỗi lần quan sát 0,1 s, fence 2,7–2,9 s, 0,16 core, 53 MiB | `scale-*.json` |
| 8 volume cùng chuyển | Nhanh như 1 volume; mọi lần ghi có fsync còn nguyên | `many-volumes-*.json` |
| Preemption | 5/5 → 0/5 build bị giết khi có PDB | `preemption-budget-*.json` |
| Cell có `JENKINS_HOME` hỏng | Phát hiện sau 12 s, restart pod 1 s sau | ADR-067 |
| Log trong lúc tiếp quản | Loki trả đủ log tới thời điểm sập | ADR-068 |

### 11.3 Đọc một file bằng chứng chaos

`timings_s`: `fenced` (taint được thêm), `leaseTaken` (pod mới giữ Lease), `jenkinsUp`, `resumed`
(build chạy tiếp). `leadersBefore`: leader của controller-manager, scheduler và csi-attacher nằm ở
máy nào. Nếu leader nằm trên máy bị tắt thì lần đó chậm hơn, và đó không phải lỗi của netCI. `once`:
số marker `netciOnce` (phải bằng 1).

---

## 12. Vận hành và xử lý sự cố

### 12.1 Những tình huống thường gặp trên lab

| Triệu chứng | Nguyên nhân | Xử lý |
|---|---|---|
| `virsh list` báo VM `paused` | Đĩa máy chủ đầy | `docker builder prune -a`, `go clean -cache`, xoá ảnh cũ; `virsh resume`; trong guest `sudo k3s crictl rmi --prune` và `sudo fstrim -v /` |
| Không kéo được ảnh | Harbor chưa lên sau khi khởi động lại | Khởi động container Harbor theo thứ tự (Bước 1) |
| Supervisor không fence qua Redfish | Emulator chưa chạy | `lab/redfish.sh start` (không tự chạy khi boot) |
| Queue lỗi | Container `netci-queue-pg` chưa chạy | `docker start netci-queue-pg` |
| Không có log trong Grafana | Loki container trên host chưa chạy | `docker ps`, `lab/monitoring.sh install` |
| `pkill -f <mẫu>` giết luôn shell của mình | Mẫu có trong chính dòng lệnh | Dùng `pgrep -f "[c]haos..."` rồi kill theo PID |
| Supervisor dùng mật khẩu BMC cũ | (đã sửa) cấu hình chỉ đọc lúc khởi động | Bản mới tự khởi động lại khi Secret đổi |

### 12.2 Lệnh chẩn đoán

```bash
kubectl get nodes -o custom-columns=N:.metadata.name,T:.spec.taints[*].key
kubectl get lease -A --field-selector metadata.name=jenkins -o custom-columns=NS:.metadata.namespace,H:.spec.holderIdentity
kubectl -n netci-system logs deploy/netci-supervisor | grep -E "confirmed off|cell fenced|force-deleted|SupervisorAlert"
kubectl get events -A --field-selector reason=NoTakeoverHeadroom
kubectl -n longhorn-system get volumes.longhorn.io -o custom-columns=N:.metadata.name,S:.status.state,R:.status.robustness,NODE:.status.currentNodeID
kubectl -n longhorn-system logs -l app=csi-attacher --since=10m | grep -E "Detach|Attach|Error"
kubectl -n netci-agents create --raw /api/v1/namespaces/netci-agents/pods/<pod>/eviction -f - <<< \
 '{"apiVersion":"policy/v1","kind":"Eviction","metadata":{"name":"<pod>","namespace":"netci-agents"},"deleteOptions":{"dryRun":["All"]}}'
```

### 12.3 Runbook

Mỗi cảnh báo có một mục trong `docs/RUNBOOK.md` gồm: nghĩa là gì, kiểm gì, làm gì. Ví dụ
`NetciFenceLeaseMoved`: máy được báo Off nhưng Lease lại vừa được gia hạn, tức ánh xạ node–máy sai.
Việc cần làm: kiểm `fence.json` ngay, **không** tự sửa bằng cách xoá pod.

---

## 13. Những bài học đắt giá (lỗi đã tìm ra)

Mỗi bài học theo cùng một khuôn: hệ thống đúng trong điều kiện lab nhưng sai khi điều kiện thật khác
đi. Cách tìm cũng giống nhau: dựng lại đúng điều kiện thật, đo, và nghi ngờ cả công cụ đo.

1. **Nhân bản ≠ an toàn khi mất điện.** Page cache làm mất khoảng 20 s trạng thái và một bước chạy hai
   lần. Sửa bằng writeback 1 s, commit 1 s và `netciOnce`.
2. **Kết nối tới API server đã chết không đóng, cũng không trả lời** (ADR-066). HTTP/2 giữ một kết
   nối duy nhất; client-go chỉ phát hiện sau 30 + 15 s; Longhorn manager bị "mù" 44 s. Sửa bằng
   HTTP/1.1, giới hạn thời gian mỗi lần thử, đóng kết nối khi lỗi, dial thẳng endpoint, và health
   check HTTP/2 2 s + 2 s cho Longhorn.
3. **Patch Deployment của Longhorn bị driver-deployer ghi đè**, nên phải dùng admission policy. Một
   JSON patch có list typed object còn làm API server của k3s 1.36.4 panic.
4. **Longhorn xoá nhầm controller mới** (auto-delete khi volume bị gỡ bất ngờ): cứ 6 lần thì 2 lần mất
   35–55 s. Sửa bằng cách loại StatefulSet khỏi auto-delete; cell agent tự probe `JENKINS_HOME`.
5. **Đếm theo cell, không theo máy** (chuỗi 18): hai cell trên một máy treo bị đọc thành "2/2 cell
   hỏng", panic guard chặn, cả hai nằm chết. Sửa: đếm theo máy.
6. **Renew deadline 4 s ngắn hơn một lần bầu leader etcd**, nên một cell khoẻ bị restart. Sửa thành
   15 s / 10 s.
7. **BMC thật** (ADR-069): một lỗi của ForceOff không có nghĩa máy chưa tắt. Gửi lại tới hạn chót; chỉ
   tin `PowerState`; timeout 20 s → 60 s.
8. **O(N) trong vòng lặp có hạn chót** (ADR-069): 2N GET mỗi vòng, QPS 50, nên 4 s/vòng ở 100 cell, rồi
   `MaxGap` reset, rồi fence 12–17 s. Sửa: 2 LIST.
9. **Công cụ đo cũng có lỗi**: API server giả trong test bỏ qua namespace "" và field selector; kwok
   tự gia hạn node lease; bộ mô phỏng gia hạn Lease sau khi đã "mất điện". Lần nào cũng phải tìm ra
   trước khi tin số liệu.
10. **Scheduler không phân biệt pod rảnh với pod đang chạy build**: thông tin đó phải biểu diễn bằng
    thứ scheduler đọc được, tức PDB, và PDB cho pod không có controller phải dùng `minAvailable`
    nguyên.
11. **Cấu hình đổi trong lúc chạy** (xoay mật khẩu BMC): supervisor vẫn dùng mật khẩu cũ và sẽ im
    lặng mất khả năng fence. Sửa: theo dõi symlink `..data` của Secret, khởi động lại, kiểm ánh xạ lại.
12. **Xoá pod quá sớm làm storage chậm** (ADR-070): detach gửi trong vòng 0,3 s sau NotReady thì chờ
    10 s (5/5). Sửa bằng settle 3 s và toleration 30 s để taint-eviction không chạy trước. Trên đường
    sửa còn tìm ra **một lỗi an toàn**: recovery xoá pod của cell vẫn đang gia hạn Lease. Cũng ghi
    thẳng rằng cải thiện ở mức cả cuộc tiếp quản **chưa chứng minh được**, vì nằm trong độ dao động.

---

## 14. Giới hạn và những gì chưa kiểm chứng

- **UI Jenkins tắt khoảng 1 phút khi tiếp quản**: giới hạn của thiết kế "một người ghi". Phần của
  netCI là khoảng 3 s; còn lại là storage (khoảng 22 s) và Jenkins khởi động (khoảng 17 s). Log vẫn
  đọc được qua Loki, trigger vẫn vào hàng đợi bền.
- **Chưa fence một server vật lý thật** qua iDRAC/iLO. Hành vi BMC mới chỉ được mô phỏng theo tài
  liệu và báo cáo lỗi của hãng.
- **Quy mô với storage thật**: kwok đo phần control plane; storage chuyển *nhiều* volume mới đo tới 8.
- Fabric chưa nhanh hơn Kubernetes plugin trên lab nhàn rỗi (5,9 s so với 4,4 s); co giãn host theo
  dự báo và tái chế host **chưa làm**.
- Mọi số liệu đều từ lab 3 VM, nơi mọi máy đồng thời là control plane. Production nên tách riêng.

---

## 15. Bản đồ kỹ năng và lộ trình tự học

| Mảng | Cần nắm | Tự luyện bằng |
|---|---|---|
| Linux | systemd, journalctl, sysctl, page cache và writeback, fsync, ext4 mount options, iptables, namespace, cgroup | Đo thời gian `dd conv=fsync`; chỉnh `dirty_expire_centisecs` rồi rút điện VM |
| Ảo hoá | KVM, libvirt, cloud-init, qcow2, nested virt | Tự viết lại `lab/vms.sh` |
| Mạng và TLS | TCP (vì sao kết nối chết không đóng), HTTP/1.1 và HTTP/2, TLS, CA, pin SHA-256 | `openssl`, `curl --cacert`, đọc `internal/kubeclient` |
| Kubernetes | Mọi mục ở 5.3; client-go; leader election; RBAC; scheduler (preemption, PDB); CSI | Cài k3s 3 node; viết một controller nhỏ bằng Go với client-go |
| Storage | Block và file, RWO, nhân bản đồng bộ, Longhorn engine/replica | Rút điện node đang giữ volume, đọc log csi-attacher |
| Hệ phân tán | Lease, fencing, epoch, split-brain, failure detector, quorum, idempotency | Đọc các paper ở mục tham khảo (HA-LANDSCAPE.md); viết `Decide` thuần rồi test từng quy tắc |
| Jenkins | Controller/agent, Pipeline durability, JCasC, queue, plugin Java (Stapler, extension point) | `make plugin`; viết một `Step` |
| Go | goroutine, context, net/http, testing, `-race`, fake clock | Đọc `internal/supervisor` cùng test của nó |
| Java | Maven, JenkinsRule | `jenkins/plugin` |
| PostgreSQL | Giao dịch, compare-and-set, migration | `internal/runqueue`, `internal/fabric/store.go` |
| Phần cứng | BMC, Redfish, IPMI | sushy-tools; đọc tài liệu Redfish của DMTF |
| Quan sát | PromQL, alert `for`, promtool test, LogQL | `deploy/helm/netci/ci/rules-test.yaml` |
| Helm | template, values, `fail`/`required`, adoption | `helm template` hai chart |
| Phương pháp | Viết ADR (bối cảnh, quyết định, bị loại, hệ quả); thí nghiệm có giả thuyết; test phải fail trên code cũ; không nói quá bằng chứng | Viết lại ADR-070 theo cách hiểu của mình |

**Lộ trình gợi ý (khoảng 8–10 tuần)**:
1. Linux, KVM, dựng 3 VM.
2. Kubernetes cơ bản, k3s 3 node, Longhorn.
3. Jenkins controller trên StatefulSet và PVC; rút điện xem chuyện gì xảy ra (khoảng 6 phút pod mới
   chuyển).
4. Lease và cell agent (viết bằng Go); guard.
5. Supervisor tối thiểu: quan sát, hỏi `virsh domstate`, taint `out-of-service`. Đo lại.
6. Chaos script; sửa từng lỗi tìm được (Phần 13).
7. Hàng đợi bền và plugin dispatch.
8. Fabric, Kata.
9. Giám sát, Helm, tài liệu, diễn tập.

---

## Phụ lục A — Thuật ngữ A–Z

| Thuật ngữ | Nghĩa |
|---|---|
| **ADR** | Architecture Decision Record: tài liệu ghi một quyết định, lý do và phương án bị loại |
| **Admission (netci-queue)** | Giới hạn số run đã gửi vào một cell mà chưa chạy |
| **Agent (Jenkins)** | Nơi chạy build, nối về controller |
| **Agent fabric** | Lớp cung cấp sandbox build của netCI (`netci-fabric` + `netci-sandbox`) |
| **Attach / detach** | Gắn / gỡ volume khối vào một node (CSI ControllerPublish/Unpublish) |
| **BMC** | Bộ điều khiển quản trị trên bo mạch, bật tắt nguồn từ xa |
| **Bind muộn (late binding)** | Pod sandbox khởi động trước, chỉ gắn với controller và build khi có claim |
| **Cell** | Một Jenkins controller + `JENKINS_HOME` + cell agent, phục vụ một nhóm đội |
| **Cell agent** | Sidecar Go giữ Lease, mở gate, probe `JENKINS_HOME`, reload JCasC |
| **Claim** | Yêu cầu một sandbox từ fabric |
| **Cooldown** | Thời gian không hành động lại với một cell sau một hành động |
| **CSI** | Container Storage Interface, chuẩn plugin storage của Kubernetes |
| **Digest** | Mã băm sha256 định danh bất biến một ảnh |
| **Dispatch idempotent** | Gửi một run vào Jenkins; gửi lại không tạo build thứ hai |
| **Durability (Pipeline)** | Khả năng Pipeline chạy tiếp sau khi controller khởi động lại |
| **Epoch** | Số lần Lease đổi chủ (`leaseTransitions`), dùng như fencing token |
| **etcd** | CSDL key-value phân tán của Kubernetes; cần quorum |
| **Fail closed** | Không chắc thì từ chối |
| **Fence / fencing** | Cô lập chắc chắn một máy (ở đây: tắt nguồn và xác nhận Off) trước khi giao tài nguyên của nó cho máy khác |
| **ForceOff** | Lệnh Redfish tắt nguồn cứng |
| **Gate file** | File trong bộ nhớ mà cell agent ghi để guard biết Lease còn được giữ |
| **Guard** | Lớp bọc tiến trình Jenkins; giết JVM khi gate cũ quá hạn |
| **Headroom** | Chỗ trống trên máy khác đủ để nhận controller nếu máy hiện tại chết |
| **Holder identity** | Định danh người giữ Lease: `<pod>/<uid>` |
| **Idempotency key** | Khoá giúp một yêu cầu gửi lại được coi là cùng một yêu cầu (delivery id của webhook) |
| **JCasC** | Jenkins Configuration as Code |
| **`JENKINS_HOME`** | Thư mục trạng thái của Jenkins |
| **Kata Containers** | Chạy pod trong VM nhẹ có kernel riêng |
| **kwok** | Kubernetes WithOut Kubelet: giả lập node để thử quy mô |
| **Lease** | Đối tượng Kubernetes thể hiện quyền có thời hạn |
| **Leader election** | Chọn một bản sao làm việc nhờ Lease |
| **Longhorn** | Storage khối phân tán cho Kubernetes |
| **`MaxGap`** | Khoảng trống quan sát tối đa (3 s); vượt thì supervisor quên và quan sát lại |
| **`netciOnce`** | Bước Pipeline đảm bảo một khối chạy không quá một lần |
| **NodeStale** | kubelet im lặng 20 s |
| **Nonce** | Số ngẫu nhiên dùng một lần, phân biệt các lần thử |
| **Out-of-service taint** | Taint báo node đã tắt hẳn; Kubernetes xoá pod và gỡ volume không chờ kubelet |
| **Panic guard** | Không hành động khi quá nhiều máy hỏng cùng lúc |
| **PDB** | PodDisruptionBudget |
| **PodGC** | Vòng điều khiển dọn pod; force-delete pod terminating trên node out-of-service |
| **Preemption** | Đẩy pod ưu tiên thấp để lấy chỗ cho pod ưu tiên cao |
| **PriorityClass** | Mức ưu tiên của pod |
| **Quorum** | Đa số thành viên cần thiết để một cụm (etcd) hoạt động |
| **Quiet check** | Chờ Lease của các cell khác trên máy im lặng 2 s trước khi nhả |
| **Recovery (supervisor)** | Phần quyết định cho node đã fence: xoá pod còn sót, bỏ fence, bật máy |
| **Redfish** | Chuẩn REST quản trị phần cứng |
| **resourceVersion** | Phiên bản đối tượng, dùng cho cập nhật có điều kiện |
| **RuntimeClass** | Chọn runtime container cho pod |
| **RWO** | ReadWriteOnce: volume gắn vào một node mỗi lúc |
| **Sandbox** | Pod chạy một build, bị huỷ sau build |
| **Settle (storage)** | Khoảng chờ để storage xử lý xong việc node chết trước khi xoá pod |
| **Split-brain** | Hai bên cùng tin mình là chủ và cùng ghi |
| **StatefulSet** | Pod có tên và volume cố định |
| **STONITH** | Shoot The Other Node In The Head: fencing bằng cắt điện |
| **Supervisor** | Dịch vụ Go quan sát, fence và tiếp quản cell |
| **SuspectAfter** | 3 s Lease không đổi thì hỏi BMC |
| **sushy-tools** | Trình giả lập Redfish cho VM libvirt |
| **Taint / toleration** | Cơ chế đẩy pod khỏi node / cho pod chịu được |
| **TokenReview** | API xác minh token ServiceAccount |
| **Writeback** | Kernel đẩy dữ liệu từ page cache xuống đĩa |

---

## Phụ lục B — Sổ tay câu lệnh

```bash
# --- Lab: vòng đời ---
lab/vms.sh up|status|ssh N|down|reclaim
lab/k3s.sh up ; export KUBECONFIG=$(lab/k3s.sh kubeconfig)
lab/longhorn-tune.sh ; lab/k3s-timings.sh upstream|k3s
lab/redfish.sh start|stop|secret ; lab/redfish.sh quirks start|stop     # BMC "xấu tính" ở cổng 8001
BMC_PORT=8001 REQUEST_TIMEOUT=5s MONITORING=on FENCE=redfish NETCI_IMAGE=...@sha256:... lab/helm-install.sh
lab/monitoring.sh install ; lab/kata.sh install|check

# --- Build ---
make check ; make check-pg ; make plugin ; make image VERSION=...   # image: từ worktree sạch
scripts/corp/push_image.sh <local> 172.17.0.1:8930/netci/<name>:<tag>

# --- Kiểm chứng ---
python3 lab/spike/chaos_poweroff.py --runs 3 --job once-probe [--failure node-hang-supervised]
python3 lab/spike/many_volumes_probe.py --cells 8 --runs 2 [--size 128Mi] [--keep]
python3 lab/spike/headroom_probe.py --wait-firing
KWOK_DIR=<kwokctl+kwok 0.8.0> lab/scale/run.sh 100 300 5
KUBECONFIG=<kwok> python3 lab/scale/preemption.py --rounds 5
deploy/helm/netci/ci/test-rules.sh

# --- Tắt/bật máy bằng tay (thí nghiệm) ---
virsh -c qemu:///system destroy netci-lab-2      # mất điện
virsh -c qemu:///system suspend netci-lab-2      # máy treo
virsh -c qemu:///system start netci-lab-2

# --- Quan sát ---
curl -s http://192.168.122.212:30909/api/v1/query --data-urlencode \
  'query=histogram_quantile(0.99, sum(rate(netci_supervisor_observation_seconds_bucket[10m])) by (le))'
```

---

## Phụ lục C — Bản đồ mã nguồn và ADR

```
cmd/cell-agent        sidecar: Lease, gate, guard, probe JENKINS_HOME, reload JCasC, prune plugin
cmd/supervisor        phát hiện, fence, tiếp quản, headroom
cmd/netci-queue       hàng đợi bền, webhook, netciOnce
cmd/netci-fabric      pool sandbox, claim, reconcile
cmd/netci-sandbox     entrypoint của sandbox (long-poll binding, agent.jar)
internal/lease        Holder (giữ Lease), Observer (đo "không đổi bao lâu")
internal/supervisor   collect.go, decide.go (chính sách), execute.go, headroom.go, validate.go
internal/fence        fence.go (EnsureOff), redfish.go, ssh.go, config.go, fingerprint.go
internal/kubeclient   client chịu được API server chết (ADR-066)
internal/runqueue     queue, dispatch, once
internal/fabric       API, store PostgreSQL, reconcile, leader
jenkins/plugin        NetciEndpoint (dispatch), NetciOnceStep, NetciCloud/Launcher, NoDelayProvisioning
deploy/helm/netci     chart nền tảng (+ monitoring.yaml, ci/rules-test.yaml)
deploy/helm/netci-cell chart cell
deploy/longhorn       tuning-policy.yaml
lab/                  dựng lab, probe, chaos, scale; bằng chứng ở lab/evidence/
```

ADR nên đọc theo thứ tự: **060** (cell, tiếp quản), **061/064** (fabric), **063** (queue), **065**
(`netciOnce`), **066** (client sống sót qua API server chết), **067** (`JENKINS_HOME` hỏng), **068**
(log khi tiếp quản), **069** (BMC thật, quy mô, build đang chạy), **070** (settle storage). 001–059 là
lịch sử của bản IDP. So sánh với CloudBees và Medik8s: `docs/research/HA-LANDSCAPE.md`.
