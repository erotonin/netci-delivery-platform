# Hướng dẫn demo thủ công netCI

> Viết ngày 2026-10-12, sau khi chạy trọn luồng này trên lab (build `once-probe` #160, run qua queue #131,
> bằng chứng `demo-power-loss-20261012T054100Z.json`). Mọi lệnh dưới đây đã chạy được đúng như ghi. Đi kèm
> `docs/BAO-VE-DE-TAI-NETCI.md` (dàn bài, câu hỏi) và `docs/TRINH-BAY-TOAN-DIEN-NETCI.md` (kiến thức).

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

## 0. Chuẩn bị

### 0.1 Hôm trước

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

### 0.2 Trước giờ demo **ít nhất 30 phút**

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

### 0.3 Mật khẩu: đăng nhập trước, đừng chiếu lên màn hình

| Ở đâu | User | Mật khẩu nằm trong file |
|---|---|---|
| Jenkins cell-b `http://192.168.122.212:30081` | `admin` | `.netci-gate/lab/cell-admin-password` |
| Grafana `http://192.168.122.211:30300` | `admin` | `.netci-gate/lab/monitoring/grafana-password` |
| Token API của queue | — | `.netci-gate/lab/queue/client-token` |

Mở các file này ở một terminal **không chiếu**, đăng nhập Jenkins và Grafana trên trình duyệt trước giờ demo.
Trong terminal chiếu, chỉ dùng `$(cat ...)` như các lệnh bên dưới, không bao giờ `cat` thẳng ra màn hình.

> Jenkins trả lời ở cả ba IP (`.211`, `.212`, `.213`, cổng 30081), nên vẫn mở được khi một máy chết. Dùng
> `.211` trong trình duyệt nếu demo rút điện lab-2.

### 0.4 Các cửa sổ

| Cửa sổ | Mở gì |
|---|---|
| **A** (terminal chính) | Nơi gõ lệnh. Chạy trước: `cd ~/netci-delivery-platform; export KUBECONFIG=$PWD/.netci-gate/lab/kubeconfig; Q=http://192.168.122.211:30090; TOKEN=$(cat .netci-gate/lab/queue/client-token)` |
| **B** (terminal theo dõi) | `export KUBECONFIG=...; watch -n1 'kubectl get nodes; echo; kubectl get pods -A -o wide \| grep -E "NAME\|jenkins-0\|netci-pg"; echo; kubectl -n cell-b get lease jenkins -o custom-columns=HOLDER:.spec.holderIdentity,EPOCH:.spec.leaseTransitions'` |
| **C** (trình duyệt) | Jenkins cell-b: `http://192.168.122.211:30081` |
| **D** (trình duyệt) | Grafana → Explore → datasource **Loki**; và dashboard `netCI` (`/d/netci-overview/netci`) |

---

## 1. Hệ thống đang chạy (1,5 phút)

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
pool Kata, chỉ chạy trên lab-3, đang chờ chỗ. Không liên quan tới luồng demo; nếu bị hỏi, xem mục 7.

## 2. Hàng đợi bền và không trùng (1,5 phút)

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

## 3. Sandbox của fabric (1,5 phút)

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

## 4. Rút điện (4 phút)

### 4.1 Khởi động build dài

Cửa sổ C: job `once-probe` → **Build with Parameters** → `DURATION` = `180` → Build. Mở console: mỗi giây một
dòng `tick N`.

**Nói**: *"Build này chạy một bước sh 180 giây bên trong `netciOnce`, giống một bước deploy không được phép
chạy hai lần."*

### 4.2 Tìm máy của controller

```bash
kubectl -n cell-b get pod jenkins-0 -o wide        # cột NODE, ví dụ netci-lab-2
```

### 4.3 Rút điện

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

### 4.4 Trong lúc Jenkins đang chết (giây 8–30): hai điều Jenkins thường không làm được

**a) Vẫn nhận run** (cửa sổ A):

```bash
curl -s -X POST $Q/v1/runs -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"job\":\"queue-probe-short\",\"idempotencyKey\":\"luc-sap-$(date +%s)\"}" -w "  HTTP %{http_code}\n"
```

Kết quả thật: `HTTP 202`, `state: accepted`. Run này sẽ chạy khi controller mới lên.

**Nói**: *"Jenkins đang chết, nhưng hàng đợi bền vẫn nhận việc."*

> Chỉ đúng khi PostgreSQL không chết cùng máy (bước 0.2 lo việc đó). Nếu nó chết cùng máy, lệnh trên trả 503
> hoặc lỗi kết nối trong khoảng 45–70 s: queue từ chối chứ không làm mất. Lab chỉ có một PostgreSQL; production
> phải chạy nó HA.

**b) Vẫn đọc được log build** (cửa sổ D, Grafana → Explore → Loki), truy vấn:

```
{cell="cell-b", job="once-probe"} |= "tick "
```

Kết quả thật: các dòng tới `tick 14`, đúng lúc mất điện.

**Nói**: *"Log được đẩy ra Loki ngay khi Jenkins ghi, nên đọc được kể cả khi controller đang được chuyển."*

### 4.5 Build chạy tiếp

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

## 5. Sau sự cố (2 phút)

### 5.1 Build xong, đúng một lần

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

### 5.2 Supervisor đã làm gì

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

### 5.3 Cảnh báo

Grafana dashboard `netCI`, hoặc:

```bash
curl -s http://192.168.122.211:30909/api/v1/alerts | python3 -c 'import json,sys; print(sorted({a["labels"]["alertname"]+": "+a["state"] for a in json.load(sys.stdin)["data"]["alerts"] if a["labels"]["alertname"].startswith("Netci")}))'
```

Trong lúc lab-2 tắt, lần chạy thật có `NetciCellWithoutHeadroom: pending`.

**Nói**: *"Khi một máy đang tắt, nếu thêm một máy nữa chết thì không còn chỗ cho controller; netCI báo trước
điều đó."*

Có thể thấy thêm `NetciCellsShareAMachine` nếu sau tiếp quản hai cell ở chung một máy. Đó là cảnh báo đúng:
chúng sẽ chết cùng nhau. Cách xử lý là bước 0.2.

### 5.4 Máy tự bật lại

Cửa sổ B: `netci-lab-2` trở lại `Ready`, taint biến mất, sau khoảng 2 phút kể từ lúc rút điện.

---

## 6. Rút gọn: nếu chỉ có 3 phút

```bash
python3 lab/demo/power_loss_demo.py --cell cell-b --duration 120
```

Script kiểm điều kiện, chạy build, dừng chờ Enter (mở link console nó in ra cho hội đồng xem), rồi rút điện và
kể từng mốc bằng tiếng Việt. Kết thúc bằng `SUCCESS; tick thiếu: không; marker netciOnce: 1`.

Có thể thêm `--arrange` để nó tự sắp xếp lab trước, nhưng mất thêm vài phút.

---

## 7. Khi có trục trặc

| Hiện tượng | Nguyên nhân | Làm |
|---|---|---|
| Lệnh `curl` có `?tree=...[...]` không trả gì | `curl` hiểu `[ ]` là mẫu glob | Thêm `-g` (`curl -sg …`), hoặc dùng giao diện web |
| Chờ "tick" mãi bằng `grep "^tick"` | Console của cell-b có tiền tố giờ `[2026-…Z] tick 15` | Bỏ `^`: `grep "tick 15 "` |
| Queue trả 503 / lỗi kết nối lúc tiếp quản | PostgreSQL chết cùng máy | Bước 0.2; hoặc giải thích: từ chối chứ không mất, production chạy PostgreSQL HA |
| Pod `sbx-…` của pool `untrusted` Pending; cảnh báo `NetciFabric…` bắn | Pool Kata chỉ chạy trên lab-3 và lab-3 hết chỗ (thường do một cell nằm đó) | Bước 0.2, và chờ 30 phút cho cảnh báo tự hết |
| Jenkins chậm lên hơn mọi lần (60 s) | Máy chết mang theo cả leader của control plane hay storage, hoặc nhiều volume cùng chuyển | Bình thường trong biên độ đã đo (34–67 s); nói thẳng như vậy |
| Build trên **cell-a** xong muộn 100–150 s | cell-a mount `sync,dirsync` (cấu hình đối chứng): mỗi bước Pipeline mất khoảng 4 s | Luôn demo trên **cell-b** |
| Sau demo hai cell ở chung một máy | Tiếp quản đặt controller vào máy còn chỗ | Chạy lại `--arrange-only` trước lần demo sau |
| Máy bị rút điện không lên lại sau 5 phút | Supervisor chưa bật lại | `kubectl get events -A --field-selector source=netci-supervisor`; bật tay: `virsh -c qemu:///system start netci-lab-2` |
| Mạng hội trường không vào được lab | — | Chiếu video quay lần tập, mở file bằng chứng trong `lab/evidence/` |

## 8. Sau buổi demo

```bash
python3 lab/demo/power_loss_demo.py --arrange-only     # đưa lab về sắp xếp chuẩn cho lần sau
```
