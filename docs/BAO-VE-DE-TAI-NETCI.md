# netCI — Bộ chuẩn bị bảo vệ đề tài

> Viết ngày 2026-10-12 (commit `7c05e26`). Đi kèm `docs/TRINH-BAY-TOAN-DIEN-NETCI.md` (gọi tắt là **TB**):
> TB để **học**, tài liệu này để **bảo vệ**. Mọi con số dưới đây đều có trong TB và trong `lab/evidence/`.
> Hội đồng sẽ hỏi vặn từng con số. Không nói quá những gì bằng chứng cho phép.

Nội dung:
1. Thông điệp cốt lõi (1 câu, 30 giây, 2 phút)
2. Những con số phải thuộc
3. Dàn bài thuyết trình 15–20 phút, theo từng slide
4. Kịch bản demo trực tiếp (kèm chuẩn bị và phương án dự phòng)
5. Ngân hàng câu hỏi hội đồng và gợi ý trả lời
6. Điểm yếu nên chủ động nói
7. Kế hoạch học trước ngày bảo vệ

---

## 1. Thông điệp cốt lõi

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

## 2. Những con số phải thuộc

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

## 3. Dàn bài thuyết trình 15–20 phút

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
| 9 | **Demo trực tiếp** | Phần 4 | 3:00 |
| 10 | Không mất hàng đợi, không chạy hai lần | netci-queue (nhận là bền, dispatch idempotent), `netciOnce` (từ chối khi không chắc) | 1:30 |
| 11 | Agent fabric | Pod ấm, bind muộn, Kata cho code không tin cậy, jar cache: 15 s → 0 | 1:15 |
| 12 | Kiểm chứng | Các tầng test; hơn 80 lần mất điện; soak 38/38; quy mô 600 cell. *"Mỗi test phải đỏ trên code cũ."* | 1:30 |
| 13 | Bài học | Chọn 3: page cache (nhân bản ≠ an toàn), hai đồng hồ trong một phép trừ, 15 s bí ẩn (thread dump) | 1:30 |
| 14 | Giới hạn và hướng phát triển | Phần 6 | 1:00 |
| 15 | Kết luận | Câu ở mục 1 | 0:20 |

Mẹo:
- Slide 2 kể bằng câu chuyện, không đọc bảng.
- Slide 6 chỉ vẽ những gì sẽ nhắc lại về sau.
- Demo đặt giữa bài, lúc hội đồng còn tập trung; nếu demo hỏng thì chuyển ngay sang phương án dự phòng (4.5).

---

## 4. Kịch bản demo trực tiếp

### 4.1 Ý tưởng

Rút điện thật máy đang chạy Jenkins controller, giữa lúc một build đang chạy bên trong `netciOnce`. Màn hình in
từng mốc. Build chạy tiếp và kết thúc SUCCESS, không mất dòng log nào, đúng 1 marker. Không ai chạm tay vào gì
sau khi rút điện.

### 4.2 Chuẩn bị

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

### 4.3 Chạy

```bash
export KUBECONFIG=$PWD/.netci-gate/lab/kubeconfig
python3 lab/demo/power_loss_demo.py --cell cell-b --duration 120
# Script kiểm điều kiện, khởi động build once-probe, chờ tới tick 8, in link console rồi dừng chờ Enter.
# Mở link console ở cửa sổ C cho hội đồng thấy các dòng "tick N". Nhấn Enter.
```

### 4.4 Lời dẫn theo từng dòng (số thật của lần tập 2026-10-12)

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

### 4.5 Phương án dự phòng

| Sự cố | Làm |
|---|---|
| Script từ chối ở bước kiểm tra | Đọc dòng ✗ nó in, làm đúng hướng dẫn (thường là tách cell), chạy lại |
| Mạng hội trường không vào được lab | Chiếu video quay hôm trước, rồi mở file `lab/evidence/demo-power-loss-20261012T044008Z.json` |
| Build FAILURE hoặc thiếu tick | Nói thật: "đây là điều netCI sinh ra để bắt". Chuyển sang slide kiểm chứng; sau buổi bảo vệ phân tích bằng chứng |
| Hội đồng muốn thử máy treo | `python3 lab/spike/chaos_poweroff.py --runs 1 --job once-probe --failure node-hang-supervised` (khoảng 5 phút) |

---

## 5. Ngân hàng câu hỏi hội đồng

Cột "Gợi ý" là ý chính; tự diễn đạt bằng lời của mình, và mở TB ở mục ghi trong ngoặc để học kỹ.

### 5.1 Động cơ và so sánh

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Vì sao không dùng CloudBees CI? | Bản quyền theo người dùng, buộc vào một nhà cung cấp, và chính CloudBees ghi nhiều plugin bị giới hạn trong HA. Công ty muốn giữ Jenkins mã nguồn mở (1.2) |
| Sao không bỏ Jenkins, chuyển sang GitLab CI hay GitHub Actions? | Hàng trăm Jenkinsfile, plugin và thói quen; chuyển là một dự án nhiều năm. netCI giữ nguyên tất cả (R12) |
| Kubernetes đã tự chạy lại pod, cần netCI làm gì? | Với StatefulSet, pod trên node chết kẹt Terminating mãi, và volume RWO vẫn gắn vào node chết. Force-delete bằng tay thì có nguy cơ split-brain nếu node chỉ mất mạng. netCI làm việc đó an toàn trong 3 s (TB 3.7) |
| Khác gì Medik8s (Node Healthcheck + Self Node Remediation)? | Cùng ý tưởng fence. netCI phát hiện theo Lease của chính ứng dụng (3 s, nhanh hơn chờ node NotReady), xác nhận Off qua BMC, có panic guard theo máy, và lo cả phần Jenkins (hàng đợi, `netciOnce`, fabric). Xem `docs/research/HA-LANDSCAPE.md` |
| Sao không chạy hai controller active/active? | Jenkins giữ queue, số build, kênh agent trong RAM và ghi XML không phối hợp; hai JVM cùng ghi là hỏng dữ liệu. Muốn làm phải viết lại lõi Jenkins (7.1) |
| Backup/restore không đủ sao? | Tính bằng phút đến giờ và mất dữ liệu từ bản backup gần nhất; đó là khôi phục thảm hoạ, không phải failover |

### 5.2 Thiết kế an toàn

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

### 5.3 Dữ liệu và "đúng một lần"

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Đĩa nhân bản 3 bản sao rồi, sao còn mất dữ liệu? | Nhân bản chỉ chép thứ đã xuống thiết bị khối; Jenkins để phần lớn dữ liệu ghi trong page cache. Spike đầu: trạng thái lùi 20 s, một bước chạy hai lần. Sửa bằng writeback 1 s + ext4 `commit=1`; phần 1 s còn lại do `netciOnce` lo (10.1) |
| Sao không mount `sync` cho chắc? | Đã đo: chậm khoảng 4 lần mỗi build; trên lab mỗi bước Pipeline mất khoảng 4 s. Writeback 1 s + `commit=1` không tốn gì đo được |
| "Exactly-once" có thật không? | Không có trong hệ phân tán. netCI làm at-least-once (gửi lại tới khi chắc) cộng khử trùng lặp (idempotency key, dispatch dưới khoá hàng đợi). Với bước có tác dụng phụ ra bên ngoài thì dùng `netciOnce`: khi không chắc thì **từ chối**, không chạy lại |
| `netciOnce` từ chối thì build fail, có tệ không? | Tệ hơn nhiều là deploy production hai lần. Người quyết định bằng `retry: true`. Đã đo: 51 lần mất điện giữa khối, không lần nào bị từ chối nhầm |
| PostgreSQL chết thì sao? | Queue trả 503, client gửi lại; khối `netciOnce` fail closed. Đã đo: restart 20 s thì mọi run vẫn đúng một lần; chết 90 s thì khối không chạy, không marker |
| PostgreSQL có là điểm chết mới không? | Có, nó phải HA (bản sao, failover) ở production. Lab chạy một bản. Thiết kế đã cho nó fail closed: hỏng là từ chối, không phải làm sai |

### 5.4 Kubernetes, Jenkins, storage

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Taint out-of-service là gì, có an toàn không? | Tính năng non-graceful node shutdown (GA 1.28): khẳng định node đã tắt hẳn để Kubernetes gỡ volume không chờ kubelet. Tài liệu đòi node phải thật sự tắt, nên netCI xác nhận Off trước khi gắn |
| Vì sao chọn Longhorn? | Dễ cài trên lab, nhân bản đồng bộ, CSI. Production dùng Ceph RBD, vSAN hay SAN cũng được, miễn block RWO nhân bản và có CSI |
| Vì sao k3s? | Kubernetes đầy đủ, một binary, dựng 3 node nhanh. Mọi cơ chế netCI dùng đều là API chuẩn, không riêng k3s |
| Build đang chạy sống sót thế nào khi controller chết? | Bước `sh` là tiến trình của agent (durable-task), ghi output và exit code ra file; controller mới nối lại và đọc tiếp. Điều kiện: agent nối lại trong khoảng 5 phút, và đĩa còn đúng trạng thái (TB 2.3) |
| Agent nối lại controller mới bằng cách nào? | Trình giám sát quanh agent theo dõi `X-Jenkins-Session`; khi thấy phiên mới thì khởi động lại JVM agent để nối vào controller mới. Service publish pod chưa Ready (Jenkins trả 503 tới khi sẵn sàng) nên agent nối được trong khoảng 2 s |
| Vì sao viết bằng Go mà không Python như bản cũ? | Các controller hạ tầng (client-go, Cluster API) đều bằng Go; binary tĩnh, ảnh `FROM scratch`, test với `-race` |

### 5.5 Fabric và hiệu năng

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Fabric có nhanh hơn Kubernetes plugin không? | Khi dồn dập thì có: 4,82 s so với 6,17 s trung vị, không build nào quá 6 s. Khi chạy lần lượt thì **chưa**: 3,88 s so với 3,17 s. Mẫu còn nhỏ |
| Cái 15 s là gì, tìm ra thế nào? | Mỗi agent mới tải 19 MB jar của controller qua kênh remoting. Tìm bằng cách bám log từng pod từ lúc nó tồn tại, tách từng giai đoạn, chụp thread dump controller đúng lúc kẹt; chứng minh bằng A/B trước khi sửa (ADR-072) |
| Jar cache có rủi ro bảo mật không? | Remoting dùng jar trong cache theo tên, không kiểm nội dung. Vì vậy cache chỉ đến từ ảnh do platform build, ghim theo digest, mount chỉ đọc; mỗi sandbox nhận một bản chép riêng; không gì sandbox ghi ra được chuyển cho sandbox khác |
| Code không tin cậy (PR từ fork) chạy ở đâu? | Pool `untrusted` dùng Kata: mỗi build một VM có kernel riêng. Pool `standard` dùng runc + user namespace, bỏ mọi capability, NetworkPolicy |
| Secret của agent nằm ở đâu? | Chỉ trong RAM của fabric, không xuống đĩa hay CSDL; sandbox chứng minh danh tính bằng projected ServiceAccount token |

### 5.6 Phương pháp và kiểm chứng

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Sao biết test có ý nghĩa? | Mỗi test viết cùng code và phải đỏ trên code cũ (bỏ phần sửa đi thì test fail). Test viết từ cùng một hiểu lầm với code thì pass mà chẳng chứng minh gì |
| Lab 3 VM có đại diện cho production không? | Không hoàn toàn: mọi máy vừa là control plane vừa chạy cell, BMC là giả lập. Quy mô đo bằng kwok (control plane thật, node giả). Đó là lý do có phần giới hạn |
| Có số liệu nào sai đã phải bỏ không? | Có, và đã ghi lại: lần soak đầu hỏng vì công cụ đo (timestamper đổi định dạng console); một lần đo burst sai vì 4 luồng đọc cùng một số build; số đo lúc hai controller dồn một máy. Đều bị loại, đều được ghi lý do (TB 15.18–15.20) |
| Soak chọn lỗi thế nào? | Ngẫu nhiên có trọng số, seed cố định để chạy lại được; sau mỗi lượt kiểm bất biến: Lease đúng pod, không còn taint, node Ready, volume healthy, có supervisor leader |
| ADR là gì, sao nhiều thế? | Mỗi quyết định kiến trúc ghi bối cảnh, quyết định, phương án bị loại và bằng chứng. 060–072 là của bản HA; 001–059 là lịch sử bản IDP |

### 5.7 Production, sản phẩm, cá nhân

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Đưa lên production cần gì thêm? | Diễn tập trên server và BMC thật; control plane tách khỏi máy chạy cell; PostgreSQL HA; storage của tổ chức; registry của tổ chức (INSTALL.md) |
| Chi phí vận hành? | Supervisor đo được 0,16 core và 53 MiB khi theo dõi 600 cell. Chart yêu cầu cho supervisor, queue, fabric mỗi bản 50m CPU và 64 MiB (giới hạn 192–256 MiB), 2 bản mỗi loại; mỗi cell thêm một sidecar nhỏ |
| Có bán được không, khác biệt là gì? | HA cho Jenkins mã nguồn mở mà không đổi Jenkinsfile, có bằng chứng chaos, fail closed. Thiếu: diễn tập phần cứng thật, co giãn máy, tài liệu hỗ trợ khách hàng |
| Sao bỏ bản IDP 0.3 đã chạy được? | Nó không chữa nỗi đau thật: công ty đã deploy bằng Jenkins, nỗi đau là Jenkins sập. Dám bỏ một sản phẩm chạy được khi nó không giải đúng vấn đề |
| Em tự làm phần nào, có dùng AI không? | **Trả lời trung thực.** Nói rõ mình dùng công cụ AI thế nào (viết code, chạy thử nghiệm, soạn tài liệu), mình đã quyết định những gì, kiểm chứng ra sao. Quan trọng nhất là chứng minh được mình hiểu và giải thích được từng phần: đó là mục đích của Phần 7 |

---

## 6. Điểm yếu nên chủ động nói

Nói trước thì hội đồng thấy mình hiểu giới hạn; để hội đồng tự tìm ra thì mất điểm.

1. **UI Jenkins tắt 40–60 s khi tiếp quản**: giới hạn của thiết kế "một người ghi". Bù lại: log vẫn đọc được
   qua Loki, trigger vẫn vào hàng đợi bền, build vẫn chạy tiếp.
2. **Chưa thử trên server và BMC thật**: hành vi BMC được mô phỏng theo tài liệu và báo cáo lỗi của hãng
   (chậm, 503, báo trạng thái trễ). Việc đầu tiên khi có phần cứng là diễn tập.
3. **Lab nhỏ**: 3 VM, mọi máy là control plane. Quy mô 600 cell đo bằng kwok, không có storage thật.
4. **Fabric chưa nhanh hơn Kubernetes plugin khi chạy lần lượt** (chậm hơn khoảng 0,7 s).
5. **Chưa làm** co giãn số máy theo dự báo và tái chế máy.

---

## 7. Kế hoạch học trước ngày bảo vệ

Bốn tuần, mỗi tuần một mục tiêu và một bài tự kiểm. Có thể nén hoặc giãn theo ngày bảo vệ.

| Tuần | Học (TB) | Làm | Tự kiểm |
|---|---|---|---|
| 1 | Phần 2, 3, 4: Jenkins, container, Kubernetes, storage, hệ phân tán | Bài tập 17.3 số 1, 2, 3 (Jenkins trong Docker; k3s; tắt node xem bảng 3.7) | Trả lời câu 1–4 ở TB phần 18 không nhìn tài liệu |
| 2 | Phần 1, 5–9: câu chuyện, yêu cầu, lựa chọn, kiến trúc, các luồng | Vẽ lại hình 8.1 bằng tay; tự kể bảng 9.2 từng giây | Thuyết trình slide 2–8 trong 8 phút cho một người nghe |
| 3 | Phần 10, 13, 15: thành phần, kiểm chứng, bài học | Đọc `internal/supervisor/decide.go` và test của nó; chạy `go test ./internal/supervisor -v`; chạy demo trên lab | Trả lời mục 5.2 và 5.3 không nhìn |
| 4 | Phần 12, 14, 16 và tài liệu này | Tập toàn bài 2 lần có bấm giờ; tập demo 2 lần; quay video dự phòng | Nhờ người khác hỏi ngẫu nhiên 15 câu ở Phần 5 |

Mỗi ngày đọc lại bảng ở mục 2 một lần: hội đồng hay hỏi con số.
