# netCI — Bộ chuẩn bị bảo vệ đề tài

> Bản 3, viết ngày 2026-10-15, cho nhánh `rearch/jenkins-ha-fabric`. **Thay thế bản 12/10.** Bản cũ mô tả
> thế hệ HA đầu tiên (cell, "một người ghi, chuyển nhanh"), là kiến trúc hệ thống không còn chạy nữa.
> Mọi con số dưới đây có nguồn: một ADR trong `docs/decisions/` hoặc một file trong `lab/evidence/`.
> Hội đồng sẽ hỏi vặn từng con số. Đừng nói quá những gì bằng chứng cho phép.
>
> Tài liệu học nền (Jenkins, container, Kubernetes, storage, hệ phân tán) vẫn là
> `docs/TRINH-BAY-TOAN-DIEN-NETCI.md` (TB), phần 2–4. Phần kiến trúc HA trong TB là của thế hệ cell; phần
> active-active và IDP hiện tại nằm ở tài liệu này và ở ADR-074..096.

Nội dung:
0. Đề tài đã đi qua ba giai đoạn (phải kể được vì sao kiến trúc đổi)
1. Thông điệp cốt lõi
2. Những con số phải thuộc
3. Dàn bài thuyết trình 15–20 phút
4. Hai kịch bản demo trực tiếp
5. Ngân hàng câu hỏi hội đồng
6. Điểm yếu nên chủ động nói
7. Kế hoạch học trước ngày bảo vệ

---

## BV0. Ba giai đoạn của đề tài

Hội đồng có thể hỏi: "sao em có tới 96 ADR, và ADR sau lại bác ADR trước?". Câu trả lời là lịch sử dưới đây,
và nó là một điểm mạnh: mỗi lần đổi hướng đều có lý do được ghi lại, kèm phương án bị loại.

| Giai đoạn | ADR | Nội dung | Vì sao chuyển sang giai đoạn sau |
|---|---|---|---|
| 1. IDP | 001–059 | Portal, API, CD qua Temporal, kiểm chuỗi cung ứng (SBOM, quét, ký), phê duyệt production. Build trên một Jenkins thường | Mất Jenkins là cả IDP đứng: mọi build của nó chạy trên Jenkins |
| 2. Jenkins HA, thế hệ cell | 060–072 | Mỗi lúc một controller ghi; mất máy thì supervisor xác nhận máy đã tắt qua BMC rồi mới chuyển controller sang máy khác. Đo hơn 80 lần rút điện, soak 38/38 | UI tắt 40–60 s mỗi lần chuyển. Mentor yêu cầu **UI và API không bao giờ sập**, chỉ dùng Pipeline, agent inbound, storage chủ yếu `local-path` (ADR-074, 076) |
| 3. Active-active + IDP chạy thật trên nó | 073–096 | Hai replica Jenkins cùng phục vụ, mỗi replica có `JENKINS_HOME` riêng được giữ giống nhau qua một kho có thứ tự (netci-homesync, PostgreSQL HA). Build đang chạy được replica còn sống nhận tiếp (adopt). IDP build–ký–deploy thật qua chính hệ này | — |

Câu nên nói: *"Thế hệ 2 đúng về an toàn nhưng không đạt yêu cầu 'UI không sập'. Em không vá nó mà đổi
kiến trúc, và giữ lại những gì đã chứng minh được: fence bằng tắt nguồn, hàng đợi bền, đo bằng rút điện thật."*

---

## BV1. Thông điệp cốt lõi

**Một câu**: netCI là một nền tảng cho lập trình viên, đưa code từ commit tới production đúng cách production
làm: build một lần, ký và chứng minh nguồn gốc, promote đúng digest đó qua các môi trường, và lên production
phải có người thứ hai duyệt. Nó chạy trên Jenkins mã nguồn mở active-active: UI không sập, và build đang chạy
vẫn sống khi mất một replica. Tất cả được đo trên lab với GitLab, Harbor, Keycloak và Kubernetes thật.

**30 giây**: Có hai nỗi đau.
- **Thứ nhất, CI/CD tuỳ tiện**: mỗi môi trường build lại một lần; không ai chứng minh được image đang chạy
  được build từ đâu; một người vừa xin vừa tự duyệt lên production.
- **Thứ hai, Jenkins là điểm chết duy nhất**: một controller, hàng đợi trong RAM, mất máy là mất build.

netCI giải quyết nỗi đau thứ nhất bằng mô hình "build một lần, promote digest", có ký và tách quyền. Nó giải
quyết nỗi đau thứ hai bằng hai replica Jenkins, giữ giống nhau qua một kho có thứ tự, sau HAProxy và một IP
ảo; build đang chạy được replica còn lại nhận tiếp. Làm được mà không mua CloudBees.

**2 phút**: 30 giây ở trên, thêm:
- **Vì sao home nhân bản chứ không phải home dùng chung**: hai JVM ghi chung một thư mục là hỏng dữ liệu
  (ADR-074 đã thử và bác). Mỗi replica có home riêng; mọi thay đổi job, user, view đi qua một kho PostgreSQL
  có khoá theo item, compare-and-set theo version, và một feed mà mọi replica theo dõi (ADR-076).
- **Không chạy một build hai lần**: mỗi job có đúng một owner (lease có epoch); số build được giữ chỗ trong
  PostgreSQL trước khi dùng (ADR-078). Một replica chỉ adopt build khi fence xác nhận replica cũ không thể ghi
  nữa: máy đã tắt nguồn (ADR-084).
- **Kết quả**: mất điện máy chạy replica thì UI gián đoạn 0–2 s; build thật của IDP bị kill replica 4/4 lần,
  cắt điện máy 1/1 lần, đều chạy xong, đã ký, qua policy.
- **Giới hạn**: chưa thử trên OpenStack thật; dev/staging/prod là ba namespace của một cluster; API của IDP
  trên lab chưa có TLS.

---

## BV2. Những con số phải thuộc

### IDP: logic CI/CD chạy thật

| Nội dung | Con số / kết quả | Nguồn |
|---|---|---|
| Từ commit tới prod | payments-api build qua netci-queue, 7 stage; cosign và provenance được IDP xác minh; **cùng một digest** `sha256:f28ea2ce…` lên dev → staging → prod | ADR-091, `aa-idp-e2e-20261015T05*.json` |
| Tách quyền production | dana xin và tự duyệt: **403**; rae duyệt: prod healthy | ADR-091 |
| Webhook GitLab thật | push main → build và deploy dev; toolchain ở chế độ `enforce` | ADR-091 |
| Một commit một build | push nhánh và mở MR cùng một commit: **1 build**; trước khi sửa là 2 build, 2 digest | ADR-095 |
| MR từ fork | `ignore`: 0 build. `verify`: 1 run chỉ verify, không Sign, không Publish, **không có tag trong Harbor** | ADR-095 |
| Commit mới vào MR | build đang chạy bị huỷ (`supersededBy`, Jenkins ABORTED), commit mới build một lần | ADR-095 |
| Path filter | push chỉ sửa README: **0 build** (trước khi sửa: deploy dev) | ADR-095 |
| Namespace riêng mỗi project | NetworkPolicy chặn ingress (đối chứng: 200); cache: pull base 1 → 0, 6 layer lấy từ cache, stage build 10,0 s → 6,1 s | ADR-096 |
| Trạng thái commit trên GitLab | `netci/pipeline=success`, có link tới build trên Jenkins | ADR-091, 095 |

### Jenkins active-active

| Nội dung | Con số | Nguồn |
|---|---|---|
| UI qua mất điện máy | máy của replica: gián đoạn 0–2 s; máy giữ VIP: 6 s | ADR-076, `aa-phase2-*.json` |
| Đăng nhập khi replica chết | vẫn đăng nhập trên replica còn lại; pod bị kill: 59/60 lần poll vẫn signed in | ADR-077 |
| Đồng bộ job giữa hai replica | trung vị 231 ms; 40 edit đồng thời: 25 nhận, 15 bị từ chối kèm tên người giữ, hai bên hội tụ | ADR-076 |
| Số build | dùng đúng một lần; build hiện ở replica kia sau 104–229 ms | ADR-078 |
| Build thật của IDP bị kill replica | **4/4** (lúc unit-test, build, scan, sign), adopt sau 5,2–24,2 s, succeeded, đã ký | ADR-094 |
| Cắt điện máy giữa lúc build | adopt sau 59,4 s, succeeded sau 88,4 s | ADR-094 |
| Cắt cáp mạng (máy không chết) | chỉ adopt 6 s sau khi máy được xác nhận đã tắt; build chạy đúng một lần | ADR-084 |
| Kho mất máy primary | 268 lần ghi được xác nhận, không mất cái nào; ghi bị từ chối (503) trong 28 s | ADR-080 |
| Soak nhiều lỗi liên tiếp | 11/11 build SUCCESS, UI 1742/1750 lần poll | ADR-080 |
| Nâng cấp cuốn chiếu | UI 324/324 lần đọc, 630 lần tạo, không mất cái nào | ADR-089 |
| Hàng đợi qua mất máy | 17/17 run, mỗi run đúng một lần | ADR-081 |
| Backup / restore kho | restore trong 211 s, mọi item và số build giống hệt | ADR-083 |

### Quy mô

| Nội dung | Con số |
|---|---|
| Backend Python | 36 486 dòng; test 29 477 dòng; 1528 + 62 test pass |
| Plugin Jenkins (Java) | 5 416 dòng; 3 259 dòng test; 95 test |
| Dịch vụ Go (homesync, queue, …) | 11 140 dòng; 7 911 dòng test; 229 test |
| Portal (React/TypeScript) | 16 808 dòng; 124 test |
| ADR / file bằng chứng | 96 / 624 |

---

## BV3. Dàn bài thuyết trình 15–20 phút

Mỗi slide một ý. Câu in nghiêng là câu nên nói.

| # | Slide | Nội dung | Thời lượng |
|---|---|---|---|
| 1 | Tên đề tài | netCI: nền tảng giao phần mềm trên Jenkins active-active | 0:20 |
| 2 | Hai nỗi đau | CI/CD tuỳ tiện (build lại mỗi môi trường, tự duyệt prod, không biết image từ đâu ra); Jenkins là điểm chết duy nhất | 1:30 |
| 3 | Yêu cầu của mentor | UI/API không sập, không mất build, chỉ Pipeline, agent inbound trên một VM lớn, controller trên Kubernetes, storage `local-path`, không vướng giới hạn của CloudBees | 1:00 |
| 4 | Mô hình giao phần mềm | Commit → build một lần → ký, SBOM, quét → digest → promote dev → staging → prod (cần người thứ hai). *"Không môi trường nào được build lại."* | 1:30 |
| 5 | Kiến trúc tổng thể | Portal / API / worker (Temporal) → netci-queue → HAProxy + VIP → hai replica Jenkins ↔ netci-homesync (CNPG 3 bản) → agent / pod build → Harbor, k3s | 2:00 |
| 6 | Vì sao active-active kiểu này | Home dùng chung bị bác (hai JVM ghi một thư mục); cell bị bác (UI tắt 40–60 s). Chọn **home riêng, thay đổi có thứ tự** | 1:30 |
| 7 | Không ghi đè, không chạy hai lần | Khoá theo item, compare-and-set, feed; một owner mỗi job; số build giữ chỗ trước; fence bằng tắt nguồn | 1:30 |
| 8 | Build sống khi mất replica | Bản sao build đang chạy tại mỗi ranh giới bước; adopt khi fence xác nhận; agent nối lại qua VIP | 1:30 |
| 9 | **Demo trực tiếp** | Mục BV4 (chọn A hoặc B) | 3:00 |
| 10 | Logic delivery với GitLab thật | MR, fork, huỷ build cũ, path filter, một commit một build | 1:15 |
| 11 | An toàn | Ký và xác minh digest; tách quyền production; namespace riêng; quyền provisioner bị chặn bằng admission policy | 1:15 |
| 12 | Cách kiểm chứng | Test viết cùng code và phải đỏ khi bỏ phần sửa; probe chạy với hạ tầng thật; danh sách lỗi mà chạy thật mới tìm ra | 1:30 |
| 13 | Bài học | Chọn 3 trong mục BV5.6 | 1:30 |
| 14 | Giới hạn | Mục BV6 | 1:00 |
| 15 | Kết luận | Câu ở mục BV1 | 0:20 |

---

## BV4. Hai kịch bản demo trực tiếp

### BV4.0 Chuẩn bị (cho cả hai, trước 30 phút)

**Nếu máy chủ vừa khởi động lại**, một số thứ không tự lên (gặp thật sáng 2026-10-16):

```bash
# Harbor phải bật theo đúng thứ tự này (docker compose up không dùng được ở đây)
for c in redis harbor-db registry registryctl harbor-portal harbor-core harbor-jobservice nginx; do
  docker start $c; sleep 2; done
docker start netci-idp-test-pg netci-queue-pg     # database của IDP (55434) và của hàng đợi
lab/redfish.sh start                              # bộ điều khiển nguồn giả lập, cần cho fence
curl -sk -o /dev/null -w "%{http_code}\n" https://172.17.0.1:8930/v2/   # 401 là Harbor đã lên
```

Sau đó, cho mọi lần demo:

```bash
cd ~/netci-delivery-platform
export KUBECONFIG=$PWD/.netci-gate/lab/kubeconfig
df -h /                                   # còn trên 15 GB
virsh -c qemu:///system list              # netci-lab-1..3 running
kubectl get nodes                         # 3 Ready
kubectl -n aa-p2 get pods                 # jenkins-0, jenkins-1, agent-0, agent-1, haproxy-*, homesync-*, queue-* Running
lab/aa/idp.sh up                          # API và worker của IDP, chờ readyz
set -a; source .netci-gate/aa/idp.env; set +a
```

Nếu disk dưới 15 GB: trong từng VM chạy `sudo k3s crictl rmi --prune && sudo fstrim /` (lệnh `lab/vms.sh ssh N`).
Không xoá build cache Docker trước buổi bảo vệ (build lại controller mất khoảng 45 phút).

### BV4.1 Demo A — "Từ merge request tới production" (khoảng 4 phút)

Ý tưởng: cho hội đồng thấy logic CI/CD giống production, với GitLab thật.

1. Mở portal (cửa sổ thứ hai): `cd frontend && NETCI_API_URL=http://127.0.0.1:18100 npm run dev`, vào
   `http://localhost:5173`, đăng nhập bằng Keycloak với tài khoản `dana`.
2. Chạy `python3 lab/aa/idp_scm_probe.py mr`. Script tạo một nhánh, push, mở MR, rồi chờ.
   - *"Push nhánh tạo một build chỉ để kiểm. MR đến sau gộp vào chính build đó: một commit, một build,
     một digest."*
   - Mở tab Builds của module `payments-api` để chỉ build đó.
3. Trên GitLab (`http://172.17.0.1:8929`), mở MR: có dấu `netci/pipeline` xanh, bấm vào ra build Jenkins.
4. Bấm **Merge** trên GitLab. Commit merge vào `main` khớp rule "push main → deploy dev": portal hiện một build
   mới, rồi dev healthy. *"Chỉ code đã vào main mới được deploy."*
5. Promote và release đúng build đó (lấy RUN trong tab Builds):
   - `python3 lab/aa/idp_e2e.py promote <RUN> staging`. Không có build mới: *"staging nhận đúng digest của dev."*
   - `python3 lab/aa/idp_e2e.py release <RUN> v1.0.<n>`. Script cho dana tự duyệt (bị **403**), rồi rae
     duyệt.
   - *"Production không có đường tắt: người xin không thể tự duyệt."*
6. Tab Delivery: ba làn dev / staging / prod cùng một digest.

Đã tập trọn vẹn trên lab ngày 2026-10-16:
- MR !7 gộp vào build của lần push;
- merge tạo commit `5afc76a`, build và deploy dev;
- promote staging không build lại;
- `v1.1.0`: dana tự duyệt bị 403, rae duyệt;
- ba môi trường cùng chạy `sha256:3e05afeb717a`.

Evidence: `lab/evidence/aa-idp-scm-mr-20261016T062916Z.json`, `aa-idp-e2e-20261016T063113Z.json`,
`aa-idp-e2e-20261016T063128Z.json`.

### BV4.2 Demo B — "Mất một replica giữa build thật" (khoảng 3 phút)

```bash
python3 lab/aa/idp_adoption_probe.py --at build --kill pod
# Bản mạnh hơn, khoảng 2 phút: --kill machine (cắt điện cả VM, chờ fence qua nguồn điện)
```

Lời dẫn:
- `run … on <commit>`: *"Đây là build thật của payments-api, đang chạy trên agent inbound."*
- `[pod] r1 (jenkins-1 …) killed during build`: *"Em vừa giết replica đang sở hữu build. UI vẫn trả lời qua
  replica kia."* (Mở Jenkins qua VIP `http://192.168.122.240` để chứng minh.)
- `adoptedBy r0 after N s`: *"Replica còn lại thấy bản sao build trong kho, fence xác nhận replica cũ đã chết,
  và nó nhận build. Agent nối lại qua VIP."*
- `runStatus succeeded`, `securityEvidence decision allow`: *"Build chạy xong, image được ký và qua policy, như
  chưa có gì xảy ra."*

### BV4.3 Phương án dự phòng

| Sự cố | Làm |
|---|---|
| Lab không vào được từ hội trường | Mở evidence: `lab/evidence/aa-idp-adoption-20261015T095*.json` (4 lần kill) và `aa-idp-adoption-20261015T100001Z.json` (cắt điện); `aa-idp-scm-*.json` (MR, fork, huỷ, path) |
| Build fail hoặc treo | Nói thật: "đây là thứ netCI ghi lại chứ không giấu". Mở log run trên portal: lỗi có lý do. Kể ADR-093 → 094: chính những lần fail như vậy đã dẫn tới bản sửa |
| Script từ chối vì agent offline | `kubectl -n aa-p2 get pods`; chờ agent-0/1 Running, chạy lại |

Quay video một lần tập trước ngày bảo vệ để làm phương án cuối.

---

## BV5. Ngân hàng câu hỏi hội đồng

Cột "Gợi ý" là ý chính; tự diễn đạt bằng lời của mình.

### BV5.1 Động cơ và so sánh

| Câu hỏi | Gợi ý |
|---|---|
| Sao không mua CloudBees CI? | Bản quyền theo người dùng; CloudBees ghi rõ nhiều plugin bị giới hạn khi HA (ví dụ agent một executor). Mentor muốn Jenkins mã nguồn mở và không vướng các giới hạn đó |
| Sao không bỏ Jenkins, chuyển sang GitLab CI? | Hàng trăm Jenkinsfile và plugin; chuyển đổi là việc nhiều năm. netCI giữ Jenkins, thêm HA và mô hình delivery ở trên |
| IDP khác gì một Jenkins có plugin? | Jenkins chỉ chạy build. IDP sở hữu danh tính ứng dụng, rule delivery, digest, chính sách chuỗi cung ứng, phê duyệt, deploy và audit. Jenkins là "máy chạy build" của nó |
| Sao trước đây em bác active-active, giờ lại làm? | Thứ bị bác là **home dùng chung** (hai JVM ghi một thư mục, ADR-074). Thứ được chọn là **home riêng, thay đổi có thứ tự** (ADR-076). Đổi hướng vì yêu cầu "UI không sập" của mentor |

### BV5.2 Logic CI/CD

| Câu hỏi | Gợi ý |
|---|---|
| "Build một lần, promote digest" là gì, sao quan trọng? | Image được định danh bằng digest (hash nội dung), không bằng tag. Staging và prod chạy đúng byte đã test ở dev; build lại cho mỗi môi trường thì không chứng minh được điều đó |
| Ai được deploy lên prod? | Không ai deploy thẳng. Phải có production request; người duyệt phải khác người xin (đã đo: tự duyệt bị 403); server quyết định danh tính, không lấy từ trình duyệt |
| Push và MR cùng một commit thì sao? | Trước đây build hai lần, ra hai digest cho cùng một mã nguồn. Giờ sự kiện thứ hai gộp vào build thứ nhất, dưới một advisory lock trong một transaction; có test race trên PostgreSQL (ADR-095) |
| MR từ fork có nguy hiểm không? | Mặc định bị bỏ qua. Nếu cho phép thì chỉ verify: không Sign (khoá ký không bao giờ được bind cho mã của người ngoài), không Publish, không có tag trong registry. Đã đo live |
| Commit mới vào MR khi build cũ đang chạy? | Build cũ bị huỷ (nếu chưa tới bước ký/publish/deploy), Jenkins ABORTED; build mới chạy |
| Chỉ sửa tài liệu thì có build không? | Không, nếu rule có `pathsIgnore`. Live đã tìm ra lỗi `**/*.md` không khớp `README.md` và sửa theo ngữ nghĩa của GitHub |
| Ký và xác minh thế nào? | cosign ký digest trong pipeline; IDP tự xác minh chữ ký và provenance trước khi cho deploy; worker xác minh lại trước mỗi lần deploy |

### BV5.3 Active-active: nhất quán và không mất

| Câu hỏi | Gợi ý |
|---|---|
| Hai replica sửa cùng một job cùng lúc? | Khoá theo item và compare-and-set theo version trong kho; người đến sau bị từ chối kèm tên người đang giữ. Đã đo 40 edit đồng thời: 25 nhận, 15 từ chối, hội tụ |
| Số build có bị trùng không? | Không: mỗi job một owner (lease có epoch), số build giữ chỗ trong PostgreSQL trước khi dùng |
| Kho (PostgreSQL) chết thì sao? | Kho là CloudNativePG 3 bản, đồng bộ tới ít nhất một bản sao. Mất máy primary: ghi bị từ chối 28 s rồi chạy tiếp, không mất cái nào. Có backup lên S3, đã restore thử trong 211 s |
| Đăng nhập có mất khi replica chết? | Không: user được nhân bản, cookie ký bằng khoá chung (ADR-077); CSRF crumb gắn với phiên đăng nhập chung (ADR-086) |
| Nâng cấp có làm gián đoạn không? | Cuốn chiếu từng replica; replica sắp dừng được drain: HAProxy ngừng gửi việc mới, request đang chạy được làm xong. Đo: 324/324 lần đọc |

### BV5.4 Build sống khi mất replica

| Câu hỏi | Gợi ý |
|---|---|
| Build đang chạy sống sót bằng cách nào? | Replica chủ chụp bản sao thư mục build vào kho tại mỗi ranh giới bước. Replica kia, khi fence xác nhận chủ đã chết và agent đã nối vào nó, sẽ nhận bản sao, nạp lại và Pipeline resume. Bước `sh` vẫn chạy trên agent suốt thời gian đó |
| Sao biết replica cũ chết thật, không chỉ mất mạng? | Fence strict: chỉ tin khi máy đã bị tắt nguồn (Nova ở site thật, Redfish trên lab) rồi node mới được gắn taint out-of-service. Đã đo bằng rút cáp: chỉ adopt sau khi máy được xác nhận đã tắt |
| Lỗi "cùng một giây" là gì? | Kubernetes báo giờ khởi động container theo giây. Lần ghi cuối của tiến trình cũ rơi vào đúng giây container mới khởi động, nên fence không bao giờ chắc chắn, và build phải chờ replica cũ tự khởi động lại (50–60 s). Sửa: trong giây đó, bên ghi chỉ có thể là tiến trình cũ; coi nó đã chết khi im lặng quá 7 s (ADR-093) |
| Sao lúc đầu build thật của IDP adopt xong lại fail hoặc treo? | Hai nguyên nhân, đều tìm thấy trong mã nguồn Jenkins: bản sao chụp đúng lúc `withCredentials` đang chạy nền thì không resume được; agent xoá thư mục của bước `sh` ngay khi controller có kết quả, mà bản sao lại trễ tới 3 s. Sửa: không chụp khi Jenkins không cho restart, chụp tại mỗi ranh giới bước, và bước mất kết quả thì fail có lý do thay vì treo (ADR-094) |
| Có đảm bảo tuyệt đối không mất build? | Không nói "tuyệt đối". Khe hở đã thu hẹp còn một phần giây; nếu rơi vào đó thì bước fail ngay kèm lý do và build được chạy lại, không bao giờ báo thành công giả |

### BV5.5 An toàn và cách ly

| Câu hỏi | Gợi ý |
|---|---|
| Build của project này có thấy project khác không? | Mỗi project một namespace; NetworkPolicy chặn mọi ingress (đã đo); service account không có token; controller chỉ có quyền trên pod trong namespace đó |
| Account tạo namespace có quá quyền không? | RBAC không giới hạn được theo tên. Account không được đọc Secret, không được xoá gì; và một ValidatingAdmissionPolicy chặn mọi lần ghi của nó ngoài `netci-build-*`. Đã đo: bị từ chối ở `default` |
| Bí mật có bị lộ trong log không? | Không bao giờ in token hay khoá; lỗi của lệnh ghi Secret không lặp lại lời kubectl, vì lời đó có thể chứa dữ liệu (ADR-096) |
| Build trên VM dùng chung có an toàn không? | Kém hơn pod: mỗi build có workspace và image storage riêng, xoá khi xong. Site cần cách ly theo project thì dùng chế độ `kubernetes`; server từ chối khởi động nếu bật cả hai chế độ cùng lúc |

### BV5.6 Phương pháp, bài học

| Câu hỏi | Gợi ý |
|---|---|
| Sao biết test có ý nghĩa? | Test viết cùng code và phải fail khi bỏ phần sửa. Ví dụ: tắt guard thì test restart fail đúng lỗi của lab; tắt logic gộp thì 6/7 test của ADR-095 fail |
| Chạy thật tìm ra những lỗi gì mà test không thấy? | Plugin của chính netCI chưa được khai báo nên gate toolchain chặn controller của netCI; chart mẫu bật KEDA mặc định; một commit build hai lần; `**/*.md`; AppArmor profile mất sau mất điện; layer cache không có tác dụng; fence cùng một giây; `withCredentials` không resume được |
| Ba bài học đáng nói nhất | (1) Một test pass không chứng minh gì nếu nó cũng pass khi chưa sửa. (2) Đọc mã nguồn của dependency (durable-task, step-api) trước khi đoán. (3) Trung thực về giới hạn: ADR-093 ghi "chưa đạt" trước khi ADR-094 sửa xong |

### BV5.7 Production và cá nhân

| Câu hỏi | Gợi ý |
|---|---|
| Đưa lên production cần gì? | Diễn tập trên OpenStack thật (Nova fence, Octavia hoặc kube-vip); cluster riêng cho prod; TLS cho API và portal; storage và registry của tổ chức. Hướng dẫn: `docs/JENKINS-HA-PRODUCTION.md` |
| Em tự làm phần nào, có dùng AI không? | **Trả lời trung thực**: dùng AI thế nào (viết code, chạy thử nghiệm, soạn tài liệu), mình quyết định những gì, kiểm chứng ra sao. Quan trọng nhất là giải thích được từng phần: đó là mục đích của BV7 |

---

## BV6. Điểm yếu nên chủ động nói

1. **Chưa chạy trên OpenStack thật**: fence Nova, Octavia và kube-vip trên hạ tầng site chưa được thử; lab dùng
   libvirt và Redfish giả lập.
2. **Môi trường lab chưa tách**: dev/staging/prod là ba namespace của một cluster k3s. API và portal của IDP
   trên lab chạy HTTP; Harbor, cosign và Keycloak mới dùng TLS.
3. **Khi kho đổi primary**, mọi thay đổi job/user bị từ chối (503) trong 28–45 s. Build và UI đọc vẫn chạy.
4. **Agent có thể mất vài phút mới biết kênh đã chết** khi luồng ghi của nó bị chặn (gặp 1 lần trong khoảng
   13 lần kill; 4 lần chạy lại ngày 15/10 đều không gặp). Build khi đó chờ agent rồi bị abort, không bao giờ báo thành công giả. Nguyên nhân gốc ở tầng
   mạng/remoting chưa xác định.
5. **Build trên VM dùng chung** cách ly kém hơn pod; script đăng nhập bằng mật khẩu vẫn bị CSRF crumb của
   Jenkins chặn (token API thì không).
6. **Fast-forward merge vào main build lại commit** thay vì promote digest của MR.

---

## BV7. Kế hoạch học trước ngày bảo vệ

| Tuần | Học | Làm | Tự kiểm |
|---|---|---|---|
| 1 | TB phần 2–4: Jenkins (controller, agent, Pipeline, durable task), container, Kubernetes (pod, StatefulSet, Service, RBAC, NetworkPolicy), PostgreSQL và hệ phân tán (lock, compare-and-set, lease) | Chạy Jenkins trong Docker, viết một Pipeline có `sh`; dựng k3s; tạo NetworkPolicy và thử chặn | Tự giải thích không nhìn: digest là gì, lease là gì, vì sao hai JVM không được ghi chung một thư mục |
| 2 | Mục BV0–BV3; ADR-043, 074, 076, 078, 079, 084 | Vẽ lại kiến trúc (slide 5) bằng tay; kể từng bước của một lần adopt | Thuyết trình slide 2–8 trong 8 phút cho một người nghe |
| 3 | ADR-091–096 và mã: `backend/app/delivery.py` (`start_pipeline`), `RunningBuilds.java`, `Fence.java` | Chạy demo A và B trên lab; chạy `pytest backend/tests/test_one_build_per_commit.py` và `CopyPointsTest` | Trả lời BV5.2–BV5.4 không nhìn |
| 4 | Tài liệu này | Tập toàn bài 2 lần có bấm giờ; tập mỗi demo 2 lần; quay video dự phòng | Nhờ người khác hỏi ngẫu nhiên 15 câu ở mục BV5 |

Mỗi ngày đọc lại bảng ở mục BV2 một lần: hội đồng hay hỏi con số.
