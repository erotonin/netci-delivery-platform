# netCI — Hiểu toàn bộ project từ đầu

> Tài liệu này viết cho người **chưa nắm chắc kiến trúc**, không giả định bạn đã biết
> gì. Mỗi thuật ngữ khó đều được giải thích ngay tại chỗ dùng nó lần đầu.
> Đọc theo thứ tự sẽ dễ hơn là nhảy cóc.
>
> Cập nhật: 2026-09-04 — tương ứng commit `6b38fc3` (Phase 7 xong, Phase 8 đang dở).
>
> Bổ sung 2026-09-25 — tương ứng commit `9f114a1`: §2.5 (thuật ngữ mới), §3.4 (hành trình
> đầy đủ của một thay đổi), §3.5 (AI agent là người dùng netCI), §3.6 (đã kiểm chứng live
> tới đâu). Các phần này giải thích ADR-043 → ADR-052. Nếu thấy luồng CI/CD khó hiểu,
> đọc §3.4 trước.
>
> Bổ sung 2026-09-29: có thêm ADR-053 → ADR-059 (bảng ở §5). Những thay đổi lớn:
> - **Pipeline dùng chung** (ADR-058, amended): một script, không qua bước duyệt, chỉ bắt buộc
>   build + publish. Trang Pipelines thay cho designer theo module.
> - **netCI khai báo đủ 85 plugin Jenkins và kiểm drift trước mỗi build** (ADR-059).
> - **Lab mô phỏng công ty** `netci-corp`: xem `infra/corp/README.md`.
> - **Portal chuyển sang tiếng Anh**; đã bỏ tab Golden Path Templates và trang Vulnerabilities.
>
> Tổng quan cập nhật nhất và đủ để vẽ sơ đồ: `docs/diagrams/NETCI-CONTEXT-FOR-DIAGRAMS.md`.

---

## Mục lục

1. [Project này là cái gì, giải quyết vấn đề gì](#1-project-này-là-cái-gì)
2. [Từ điển thuật ngữ](#2-từ-điển-thuật-ngữ)
3. [Bức tranh tổng thể — các mảnh ghép và luồng chạy](#3-bức-tranh-tổng-thể)
4. [Tại sao lại là kiến trúc này (7 nguyên tắc)](#4-tại-sao-lại-là-kiến-trúc-này)
5. [Đi qua từng file — vai trò và lý do tồn tại](#5-đi-qua-từng-file)
6. [Cơ sở dữ liệu — bảng nào, vì sao có](#6-cơ-sở-dữ-liệu)
7. [Hạ tầng — cần cài gì, cấu hình gì, chạy thế nào](#7-hạ-tầng)
8. [Tiến độ — đã xong gì, đang làm gì, còn gì](#8-tiến-độ)
9. [Cách tự kiểm chứng](#9-cách-tự-kiểm-chứng)
10. [Rủi ro và những chỗ chưa xong](#10-rủi-ro-và-những-chỗ-chưa-xong)

---

## 1. Project này là cái gì

### 1.1. Một câu

**netCI là một Internal Developer Platform (IDP)** — phần mềm nội bộ để lập trình viên
trong công ty tự đưa code của mình lên môi trường chạy thật, mà không cần xin ai
làm hộ, và không cần biết Jenkins/Kubernetes/Ansible hoạt động ra sao.

### 1.2. Vấn đề nó giải quyết

Trong một công ty không có IDP, để deploy một service lên production, dev thường phải:

- mở ticket cho team hạ tầng,
- chờ ai đó sửa file Jenkinsfile,
- chờ ai đó chạy tay lệnh `ansible-playbook`,
- và không ai biết chắc *bản build nào* đang thực sự chạy trên server.

netCI biến chuỗi đó thành: dev bấm nút trên web → hệ thống tự build, tự quét bảo mật,
tự xin phê duyệt, tự deploy, tự kiểm tra sức khoẻ, tự rollback nếu hỏng — **và ghi
lại bằng chứng cho mọi bước**.

### 1.3. Mục tiêu cuối cùng của project (theo yêu cầu đang thực hiện)

Biến netCI thành **production-grade** — nghĩa là chạy thật được trong doanh nghiệp,
không phải demo. Cụ thể có 6 tiêu chí, viết trong `README.md`:

| Tiêu chí | Nghĩa là gì |
|---|---|
| **Reproducible** | Dựng lại toàn bộ Jenkins từ Git, không ai "sửa tay trên UI" |
| **Isolated** | Mỗi lần build có máy ảo/container riêng, dọn sạch sau khi xong |
| **Extensible** | Cùng một luồng deploy được cho Docker, Kubernetes và Systemd |
| **Governed** | Artifact bất biến + SBOM + quét CVE + chữ ký + phê duyệt + audit + rollback |
| **Measurable** | Có số liệu DORA thật, tính từ sự kiện thật |
| **Truthful** | **Không bao giờ hiển thị màu xanh giả.** Chưa cấu hình thì nói chưa cấu hình |

Tiêu chí cuối cùng — **truthful** — là tinh thần xuyên suốt của project này. Rất nhiều
quyết định kiến trúc trong code chỉ tồn tại vì lý do đó.

---

## 2. Từ điển thuật ngữ

Đọc phần này trước, các phần sau sẽ dễ hơn nhiều.

### 2.1. Thuật ngữ CI/CD

| Thuật ngữ | Giải thích dễ hiểu |
|---|---|
| **CI** (Continuous Integration) | Tự động build + chạy test mỗi khi code thay đổi. Trong netCI, Jenkins làm việc này. |
| **CD** (Continuous Delivery/Deployment) | Tự động đưa bản build lên môi trường chạy. Trong netCI, Temporal điều phối việc này. |
| **Pipeline run** | Một lần chạy CI cụ thể: "build commit `abc123` của app X cho môi trường staging". |
| **Artifact** | Sản phẩm của build: một Docker image, một file binary, một file .tar.gz. |
| **Digest** | "Vân tay" SHA-256 của artifact, dạng `sha256:abc...`. Nội dung đổi 1 byte → digest đổi hoàn toàn. Đây là cách duy nhất để nói chắc chắn "bản đang chạy đúng là bản tôi đã duyệt". |
| **Immutable artifact** | Artifact không được sửa sau khi tạo. Vì thế netCI luôn nói tới artifact bằng digest, không bằng tag như `latest` (tag có thể trỏ sang bản khác lúc nào không biết). |
| **Deployment** | Một lần đưa **một** digest lên **một** môi trường. |
| **Environment** | `dev` / `staging` / `prod`. |
| **Rollback** | Quay lại bản trước khi bản mới hỏng. |
| **Runtime** | Kiểu nơi chạy: `docker`, `kubernetes`, hoặc `systemd` (service chạy trực tiếp trên máy Linux). |

### 2.2. Thuật ngữ bảo mật chuỗi cung ứng (supply chain)

| Thuật ngữ | Giải thích |
|---|---|
| **SBOM** (Software Bill of Materials) | "Bảng thành phần" của artifact — liệt kê mọi thư viện bên trong. Khi có CVE mới, bạn tra SBOM để biết mình có dính không. Công cụ: `syft`. |
| **CVE** | Mã định danh một lỗ hổng bảo mật đã công bố, ví dụ `CVE-2024-1234`. |
| **Vulnerability scan** | Quét artifact tìm CVE. Công cụ: `trivy`. |
| **Cosign** | Công cụ **ký số** artifact. Ký = chứng minh "bản này do hệ thống build của tôi tạo ra, chưa bị đổi". |
| **Signature verification** | Kiểm tra lại chữ ký **ngay trước khi deploy**. Quan trọng: netCI không tin cờ `signature.verified: true` mà CI tự khai — nó tự chạy `cosign verify` lại. Lý do có trong `signature_verifier.py`: nếu hệ thống CI bị chiếm quyền, nó sẽ tự khai là đã ký. |
| **Security evidence** | Gói bằng chứng gồm SBOM + kết quả scan + chữ ký, gắn với **một** pipeline run và **một** digest. |
| **Policy decision** | Kết luận `allow`/`deny` tính từ evidence. |
| **Provenance** | Nguồn gốc: artifact này ra đời từ commit nào, run nào, ai bấm. Từ ADR-044, netCI còn tạo **SLSA provenance** — bản khai nguồn gốc có chữ ký, gắn vào image trong registry (xem §2.5). |

### 2.3. Thuật ngữ kiến trúc phần mềm (phần khó nhất — đọc kỹ)

#### **Seam** (đường nối)

Một "seam" là **điểm mà bạn có thể tháo một thành phần ra và cắm cái khác vào**, mà
phần còn lại của code không biết.

Ví dụ trong netCI: domain (phần logic nghiệp vụ) chỉ biết `CiLauncher.launch(request)`.
Nó **không biết** Jenkins tồn tại. Nhờ vậy:
- Test có thể cắm `NullCiLauncher` (không làm gì) → test chạy trong 0.2 giây.
- Production cắm `JenkinsCiLauncher` (gọi HTTP thật tới Jenkins).
- Mai kia đổi sang GitLab CI → chỉ viết adapter mới, domain không đổi một dòng.

#### **Deep module** (module sâu)

Khái niệm từ sách *A Philosophy of Software Design*. Một module "sâu" có:
- **Interface nhỏ** — ít hàm, ít tham số, dễ dùng.
- **Implementation lớn** — chứa toàn bộ phần khó bên trong.

Ví dụ tốt nhất trong project này là `app/store`. Toàn bộ interface công khai chỉ có:

```python
with database.transaction() as tx:
    ...
```

Một hàm. Nhưng bên trong nó xử lý: mở kết nối PostgreSQL, quản lý transaction,
commit/rollback, ánh xạ dòng SQL thành object Python, kiểm tra version để chống ghi đè,
dịch lỗi trùng khoá thành lỗi 409. Người dùng module không cần biết gì trong số đó.

Ngược lại, **shallow module** (module nông) là loại có interface to bằng phần thân —
kiểu wrapper chỉ đổi tên hàm. Loại này chỉ thêm việc chứ không giấu được gì.

#### **Transaction** (giao dịch)

Một nhóm thao tác database chạy theo kiểu **"tất cả hoặc không gì cả"**.

Ví dụ thật trong netCI: khi tạo module mới, cần ghi 4 thứ — application, module,
audit record, idempotency record. Nếu ghi xong 2 cái rồi máy chết:
- **Không có transaction**: còn lại 2 dòng rác, application "mồ côi" không module nào trỏ tới.
- **Có transaction**: database tự xoá sạch, như chưa từng có gì xảy ra.

#### **Idempotency** (tính lũy đẳng)

"Gọi 1 lần hay 10 lần cũng ra cùng một kết quả, và chỉ tạo ra **một** tài nguyên."

Vì sao cần? Mạng có thể timeout. Client gửi request tạo module, mạng đứt trước khi
nhận response. Client không biết server đã tạo hay chưa nên gửi lại. Nếu không có
idempotency → tạo 2 module trùng.

Cách làm: client gửi kèm header `Idempotency-Key: abc-123`. Server ghi khoá này vào
bảng `idempotency_records` **trong cùng transaction** với tài nguyên. Lần sau thấy khoá
đó → trả lại đúng tài nguyên cũ.

Chi tiết tinh tế: nếu cùng khoá nhưng **nội dung request khác** → trả 409, vì đó là lỗi
của client chứ không phải retry.

#### **Optimistic concurrency / Compare-and-set (CAS)**

Vấn đề: hai người cùng sửa một bản ghi lúc 10:00:00. Ai thắng?

Cách của netCI: mỗi bản ghi có cột `version`.

```sql
UPDATE deployments SET status = 'healthy', version = 5
 WHERE id = ... AND version = 4;
```

- Nếu chưa ai sửa → `version` vẫn là 4 → update 1 dòng → thắng.
- Nếu người khác đã sửa → `version` thành 5 rồi → update **0 dòng** → biết mình thua →
  trả về HTTP 409 `CONCURRENT_MODIFICATION`.

"Optimistic" (lạc quan) vì nó không khoá trước, chỉ kiểm tra lúc ghi. Ngược lại là
"pessimistic" (khoá trước, chậm hơn).

#### **Lease** (hợp đồng thuê có hạn)

Là **khoá có thời hạn**. "Deployment này giữ độc quyền cụm máy `prod-a` trong 15 phút."

Vì sao phải có hạn? Nếu worker chết giữa chừng mà khoá vĩnh viễn → cụm máy đó bị kẹt
mãi mãi, không ai deploy được. Lease hết hạn thì tự nhả.

Worker còn sống thì phải **heartbeat** (gửi tín hiệu "tôi còn đây") để gia hạn.

#### **Fencing token** (thẻ chặn)

Đây là khái niệm khó nhất, nhưng cực kỳ quan trọng. Giải thích bằng kịch bản:

```
10:00  Workflow A nhận lease cho cụm prod-a, fencing_token = 5
10:01  Workflow A bị treo (GC pause / mạng đứt) — nhưng chưa chết
10:16  Lease của A hết hạn
10:17  Workflow B nhận lease cùng cụm đó, fencing_token = 6
10:18  Workflow B deploy xong, báo "healthy"
10:19  Workflow A TỈNH DẬY, báo "failed" cho cùng deployment đó
```

Nếu không có fencing token, báo cáo lỗi thời của A sẽ **ghi đè** kết quả đúng của B.
Hệ thống hiển thị "failed" trong khi thực tế đang chạy tốt.

Fencing token = **số tăng dần, không bao giờ lặp lại**, cấp mỗi lần có ai đó nhận lease
cho cùng một target. Mọi callback phải mang theo token của mình. Server thấy token 5
trong khi thế hệ hiện tại là 6 → từ chối.

Trong netCI, logic này nằm ở `DeliveryPlatform._reject_stale_writer` (file `delivery.py`).

#### **Unit of Work**

Một "gói" các thay đổi cần ghi cùng nhau. Trong netCI là class `UnitOfWork`
(`persistence.py`), chứa: applications, runs, deployments, events, audit, logs,
security evidence, idempotency. Toàn bộ gói ghi trong một transaction.

Vì sao gom lại? Để **không thể xảy ra** tình huống "trạng thái đã đổi nhưng sự kiện DORA
bị mất". Hai thứ đó phải cùng sống hoặc cùng chết.

#### **Outbox / Delivery event**

`delivery_events` là bảng ghi các **sự kiện gốc** không bao giờ sửa: "đã commit",
"đã deploy production", "đã khôi phục dịch vụ". Chỉ số DORA được **tính lại** từ bảng
này, không lưu sẵn. Nhờ vậy mọi con số trên dashboard đều truy ngược được về sự kiện
sinh ra nó.

#### **Projection / Read model**

"Chiếu" dữ liệu gốc thành hình dạng mà màn hình cần. Ví dụ `projections/dora.py` đọc
`delivery_events` rồi tính ra 4 chỉ số DORA.

#### **Saga**

Một giao dịch dài, gồm nhiều bước, **không thể** dùng transaction database (vì kéo dài
hàng phút và gọi ra hệ thống ngoài). Thay vào đó mỗi bước có một bước "đền bù"
(compensation) để hoàn tác. Deploy nhiều module theo thứ tự chính là một saga —
đây là **Phase 10, chưa làm**.

### 2.4. Thuật ngữ vận hành

| Thuật ngữ | Giải thích |
|---|---|
| **Replica** | Nhiều bản sao của cùng một API chạy song song để chịu tải và chịu lỗi. Vấn đề: chúng phải **thấy cùng một dữ liệu**. Đây là lý do tồn tại của Phase 1. |
| **Liveness (`/livez`)** | "Tiến trình còn sống không?" Nếu không → Kubernetes restart pod. |
| **Readiness (`/readyz`)** | "Có nhận việc được không?" Nếu không → load balancer ngừng gửi request, **nhưng không restart**. Phân biệt hai cái này quan trọng: mất kết nối DB thì restart cũng vô ích. |
| **Circuit breaker** | Sau N lần gọi hệ thống ngoài bị lỗi, ngừng gọi một lúc thay vì cứ gọi mãi và tự làm chậm chính mình. |
| **Fail closed** | Khi không chắc chắn → **từ chối**. Ngược lại là "fail open" (cứ cho qua) — nguy hiểm trong hệ thống bảo mật. |
| **Reconciler / watchdog** | Tiến trình chạy nền, so sánh "trạng thái tôi nghĩ" với "trạng thái thật ở Jenkins/Temporal" và sửa lệch. Cần vì callback có thể mất. |
| **Separation of duties** | Người xin deploy ≠ người duyệt deploy. |
| **Audit trail** | Sổ ghi bất biến: ai làm gì, lúc nào, trên tài nguyên nào. |
| **DORA metrics** | 4 chỉ số đo năng lực giao hàng phần mềm: tần suất deploy, thời gian từ commit đến production, tỉ lệ thay đổi gây lỗi, thời gian khôi phục. |
| **DCIM** | Data Center Infrastructure Management — hệ thống quản lý server/thiết bị của công ty. netCI hỏi nó "app này chạy trên máy nào". |
| **JCasC** | Jenkins Configuration as Code — cấu hình Jenkins bằng file YAML trong Git thay vì bấm chuột. |
| **Ephemeral agent** | Máy build dùng một lần rồi xoá. Chống việc build này để lại rác ảnh hưởng build sau. |

### 2.5. Thuật ngữ luồng giao hàng mới (ADR-043 → ADR-052)

> Lưu ý chữ **"agent"** có ba nghĩa khác nhau trong project này: *Jenkins agent* (máy
> build dùng một lần, §2.4), *edge agent* (tiến trình netCI chạy trên server đích), và
> *AI agent / coding agent* (chương trình AI tự viết code rồi push). Bảng dưới chỉ nói
> về nghĩa thứ ba khi viết **agent principal**.
>
> Chữ **"admission"** cũng có hai nghĩa: *Kubernetes admission* (Phase 11, cụm Kubernetes
> hỏi netCI có cho image chạy không) và *build admission* (ADR-050, dưới đây). Hai thứ
> không liên quan tới nhau.

| Thuật ngữ | Giải thích dễ hiểu |
|---|---|
| **Delivery rule** (luật giao hàng) | Luật của module nói một sự kiện Git gây ra gì: chỉ build, build rồi deploy dev/staging, hay build rồi đăng ký version. Nằm ở `pipelineConfig.delivery` của config revision. Luật đầu tiên khớp thì thắng, giống firewall. (ADR-043) |
| **Promote** (thăng cấp) | Đưa **đúng digest đã build** sang môi trường tiếp theo (dev → staging), **không build lại**. `POST /modules/{id}/promotions`. (ADR-043) |
| **Admission** (build admission) | Bước **cho một run đã ghi `queued` được gửi sang Jenkins**. Run chỉ được admit khi phạm vi quota của nó còn chỗ; nếu không thì nằm chờ trong PostgreSQL chứ không bị từ chối. Run đang chờ có `admittedAt` rỗng. (ADR-050) |
| **Concurrency group** (nhóm đồng thời) | Tập các run do SCM kích hoạt **trên cùng một ref** của cùng một application, khoá là `<application>:<ref>` — ví dụ một nhánh, hoặc `pr/12`. Run mới vào nhóm có thể thay thế run cũ. Build từ tag, run bấm tay và retry **không** thuộc nhóm nào. (ADR-050) |
| **Supersede / superseded** (thay thế / bị thay thế) | Khi một commit mới hơn tới cùng nhóm, run cũ đã lỗi thời bị huỷ và ghi `cancelled` kèm `supersededBy` = run thay nó. Có audit, commit status nói rõ lý do. (ADR-050) |
| **`cancelInProgress`** | Cờ quyết định có huỷ cả run **đang build** (không chỉ run đang chờ) khi commit mới tới không. Mặc định `true` cho pull request, `false` cho push. Chỉnh được theo từng luật trong `pipelineConfig.delivery`; tag không được đặt cờ này. (ADR-050) |
| **Path filter** (lọc theo đường dẫn) | `paths` / `pathsIgnore` trên một luật: bỏ qua build khi file thay đổi không liên quan (ví dụ chỉ sửa README). Chỉ áp dụng khi netCI **biết chắc** danh sách file thay đổi; không biết thì build đầy đủ. Không bao giờ lọc từng stage. (ADR-051) |
| **Agent principal** | Một danh tính đăng nhập netCI được **server** xếp loại `agent` (khác `human`). Agent tối đa chỉ có role developer/viewer, không duyệt được gì, và xếp sau con người trong hàng đợi admission. (ADR-052) |
| **SLSA provenance** | Bản khai nguồn gốc theo chuẩn SLSA v1: "image digest X được build từ repo Y, commit Z, bởi run R trên controller C". Được ký bằng cùng khoá cosign và lưu cạnh image trong registry. Worker kiểm tra lại ngay trước khi deploy. (ADR-044) |
| **Soak** (ngâm) | Khoảng thời gian một digest đã chạy **healthy** ở môi trường trước, trước khi được phép lên môi trường sau. Khai bằng `requireHealthyIn` (môi trường nào) + `minSoakMinutes` (bao nhiêu phút). Đo từ cột `deployments.healthy_at`. (ADR-043) |
| **Post-deploy verification** | Sau khi health check qua, worker đọc số liệu Prometheus (tỉ lệ lỗi, độ trễ p95) trong một khoảng thời gian; vượt ngưỡng hoặc **không có dữ liệu** thì rollback. (ADR-046) |
| **Change freeze** (đóng băng thay đổi) | Một khung thời gian `[bắt đầu, kết thúc)` cấm deploy vào các môi trường đã chọn, được netCI **thực thi** chứ không chỉ hiển thị. (ADR-047) |
| **Preview** (môi trường xem trước) | Một bản deploy tạm của pull request vào namespace Kubernetes riêng `preview-<module>-pr-<n>`, tự xoá khi PR đóng/merge hoặc hết TTL. Chỉ `active` khi worker thật sự đã deploy xong. (ADR-049) |
| **Rescan** (quét lại) | netCI tự quét lại SBOM đã lưu của những digest **đang chạy**, để phát hiện CVE được công bố sau ngày build. (ADR-045) |

---

## 3. Bức tranh tổng thể

### 3.1. Các hệ thống tham gia

```
┌─────────────┐     ┌──────────────┐
│  Trình duyệt│     │  Backstage   │   ← nơi dev bấm nút
│  (Portal)   │     │  (tuỳ chọn)  │
└──────┬──────┘     └──────┬───────┘
       │  HTTP/JSON        │
       └────────┬──────────┘
                ▼
       ┌────────────────────┐
       │   netCI API        │  ← FastAPI (Python). "Bộ não" - giữ luật lệ
       │   (nhiều replica)  │
       └─┬────┬────┬────┬───┘
         │    │    │    │
         │    │    │    └──────────────► DCIM (hỏi server nào)
         │    │    │
         │    │    └───────────────────► Jenkins A / Jenkins B (chạy CI)
         │    │                              │
         │    │                              ▼ agent dùng-một-lần
         │    │                          build → SBOM → scan → ký → push
         │    │
         │    └────────────────────────► Temporal (điều phối CD)
         │                                   │
         │                                   ▼ worker chạy Ansible/Helm
         │                              Docker | Kubernetes | Systemd
         ▼
   ┌───────────┐
   │PostgreSQL │  ← NGUỒN SỰ THẬT DUY NHẤT
   └───────────┘
```

### 3.2. Vì sao tách Jenkins và Temporal?

Đây là quyết định kiến trúc quan trọng (ADR-004):

| | Jenkins | Temporal |
|---|---|---|
| Việc | **CI**: build, test, quét, ký, đẩy artifact | **CD**: deploy, chờ duyệt, health check, rollback |
| Đặc điểm | Chạy vài phút, hỏng thì build lại từ đầu | Chạy hàng giờ/ngày (chờ người duyệt), **không được** làm lại từ đầu |
| Vì sao chọn | Hệ sinh thái plugin build khổng lồ | Workflow **durable** — máy chết, khởi động lại, workflow chạy tiếp đúng chỗ cũ |

"Durable workflow" nghĩa là: nếu worker chết lúc đang chờ phê duyệt 3 ngày, khi bật lại
nó vẫn nhớ đang chờ, không mất trạng thái. Jenkins không làm được điều đó.

### 3.2b. CI và CD là hai quyết định riêng (ADR-043)

Giống GitHub Actions: một sự kiện Git **khởi động build**; build xong **có deploy hay
không, deploy đi đâu** là luật của module; đưa bản đã build sang môi trường tiếp theo là
**promote** — không build lại, digest đã test chính là digest được deploy.

```
Sự kiện Git              Luật mặc định (module không khai báo gì)
───────────────────────  ────────────────────────────────────────────────────────────
push main           ──►  build → deploy dev (môi trường mặc định, nếu là dev/staging)
push nhánh khác     ──►  chỉ build (vẫn ký + publish, chưa deploy)
pull request        ──►  chỉ build; KHÔNG BAO GIỜ deploy
PR từ fork          ──►  bỏ qua; nếu module bật `forkPullRequests: verify` thì chỉ
                         verify: không ký, không publish, server từ chối digest
tag v1.2.3          ──►  build → tự đăng ký version v1.2.3 (cùng transaction)

Build đã publish ──► Promote (POST /modules/{id}/promotions) ──► dev / staging
                      └─ staging mặc định đòi digest đã healthy ở dev trước;
                         module có thể đòi soak N phút (`minSoakMinutes`)
Version ──► Production Request ──► người thứ hai duyệt ──► prod
             └─ có thể đòi đã healthy ở staging N phút (đổi luật này cần người duyệt)
```

Luật nằm ở `pipelineConfig.delivery` của config revision — có version, có audit, và
`GET /modules/{id}/delivery-rules` trả về luật đang áp dụng. Không luật nào được deploy
thẳng lên prod: khai báo `deployTo: prod` bị từ chối 422. Bấm "Run" trên Portal vẫn
build + deploy như trước, hoặc tích **Build only** để chỉ build.

### 3.3. Luồng một lần deploy đầy đủ

```
1.  Dev bấm "Trigger pipeline" trên Portal
       │
2.  POST /modules/{id}/pipeline-runs
       │  ├─ Kiểm tra quyền (role + team)
       │  ├─ Lọc parameters (chỉ cho phép build input, xem §4.4)
       │  ├─ Lấy target server từ CẤU HÌNH ĐÃ ĐĂNG KÝ, không lấy từ request
       │  └─ Ghi PipelineRun (status=queued) + audit + delivery_event(commit)
       │
3.  netCI gọi Jenkins (qua JenkinsRouter chọn controller còn chỗ)
       │  └─ Sinh callback token gắn với đúng run này
       │
4.  Jenkins chạy trên agent dùng-một-lần:
       checkout → test → build → SBOM(syft) → scan(trivy) → ký(cosign) → push
       │
5.  Jenkins gọi ngược: POST /pipeline-runs/{id}/security-evidence
       │  └─ netCI đánh giá policy: có chữ ký? có CVE nghiêm trọng? digest có khớp?
       │
6.  Jenkins gọi ngược: POST /pipeline-runs/{id}/ci-result {succeeded, digest}
       │  ├─ netCI đánh giá lại policy MỘT LẦN NỮA (không tin bước 5)
       │  ├─ Tạo Deployment
       │  └─ Nếu là prod → status=pending_approval, DỪNG chờ người
       │
7.  Reviewer bấm Approve (phải khác người bấm ở bước 1)
       │  ├─ NHẬN LEASE cho (app + môi trường + target) ← chống deploy trùng
       │  ├─ Cấp fencing token
       │  └─ Khởi động Temporal workflow
       │
8.  Temporal worker:
       │  ├─ Verify chữ ký LẠI bằng cosign (không tin cờ CI khai)
       │  ├─ Chạy Ansible / Helm / systemd
       │  ├─ Health check
       │  └─ Heartbeat gia hạn lease trong lúc chạy
       │
9.  Worker gọi ngược: POST /deployments/{id}/result {healthy, fencingToken}
       │  ├─ Kiểm tra fencing token — nếu cũ thì TỪ CHỐI
       │  ├─ Ghi delivery_event(deployment)
       │  └─ Nhả lease
       │
10. Nếu hỏng → rollback → POST /deployments/{id}/rollback-result
        └─ Thành công mới ghi delivery_event(recovery)
```

**Điểm đáng chú ý ở bước 6**: policy được đánh giá **hai lần**, và ở bước 8 chữ ký được
verify **lần thứ ba**. Đây không phải thừa. Mỗi lần kiểm tra ở một ranh giới tin cậy
khác nhau — nếu Jenkins bị chiếm quyền, nó có thể nói dối ở bước 5, nhưng không thể
làm giả chữ ký cosign mà worker verify độc lập ở bước 8.

> **Luồng trên là luồng gốc.** Từ ADR-043 và ADR-050 có hai chỗ khác: (a) giữa bước 2
> và bước 3 có thêm bước **admission** — run đã ghi `queued` chưa chắc được gửi sang
> Jenkins ngay; (b) ở bước 6, Deployment chỉ được tạo nếu luật giao hàng của module nói
> "deploy". Luồng đầy đủ hiện tại nằm ở §3.4.

### 3.4. Hành trình đầy đủ của một thay đổi (ADR-043 → ADR-052)

Phần này đi theo **một commit** từ lúc được push tới lúc chạy trên production và sau đó.
Mỗi bước có hai phần: **làm gì**, và **vì sao** — phần "vì sao" lấy từ mục Context/Decision
của ADR tương ứng, không phải suy đoán.

```
 Dev hoặc AI agent push / mở PR / đẩy tag
        │
 [1] Webhook có chữ ký ──► netCI xác định ai gửi (human | agent)
        │
 [2] Delivery rules ──► không luật nào khớp? ──► 200 "ignored" (kèm lý do)
        │   (path filter, fork, tag → version)
        ▼
 [3] Ghi PipelineRun status=queued, ý định cố định lúc này
        │   (deploy_after_build, publish_artifact, release_tag, trigger)
        ▼
 [4] Admission: quota còn chỗ? ── không ──► chờ trong PostgreSQL
        │ có                               (người trước, agent sau, cũ trước)
        │                                  hàng đợi đầy ──► 429 PIPELINE_QUEUE_FULL
 [5] Supersession: run cũ cùng ref ──► cancelled, supersededBy = run mới
        ▼                              (trừ run đã tới sign/publish/deploy)
 [6] Jenkins: checkout → unit-test → build → sbom → vulnerability-scan
        │         → sign (+ SLSA provenance) → publish
        ▼
 [7] Evidence + SBOM gửi về netCI ──► policy; provenance sai commit/repo = deny
        │
        ├── luật nói "build only" ──► run succeeded, có digest, KHÔNG có deployment
        │                              (PR cùng repo + module bật previews ──► [11] Preview)
        ▼
 [8] Deploy dev ──► Promote staging (cùng digest; requireHealthyIn dev + soak)
        │   mỗi deployment: worker verify chữ ký + provenance → deploy → health check
        │                   → post-deploy verification (Prometheus) → rollback nếu hỏng
        ▼
 [9] Version ──► Production Request ──► NGƯỜI THỨ HAI duyệt ──► prod
        │   change freeze chặn ở lúc tạo request và lúc duyệt; rollback không bao giờ bị chặn
        ▼
[10] Đang chạy: rescan SBOM định kỳ ──► "CVE này đang chạy ở đâu?"

[12] Suốt hành trình: mỗi lần đổi trạng thái ──► outbox ──► commit status / PR comment
```

#### Bước 1 — Push và webhook

**Làm gì.** Một người hoặc một AI agent push commit, mở pull request, hoặc đẩy tag. SCM
(GitHub/GitLab) gửi webhook có chữ ký tới netCI. netCI kiểm chữ ký, chống gửi lại (ADR-019)
và map repository → application **phía server**. Với webhook, người gửi (`sender`) nằm
trong `NETCI_AGENT_SCM_LOGINS` thì run được đánh dấu `trigger.actorKind = agent` (ADR-052).

Run từ webhook đi **cùng một đường** với nút "Run" trên Portal: target do server tính,
config revision đang active được ghim, build input lấy từ `pipelineConfig.buildInputs`
và vẫn bị ranh giới `build_inputs` kiểm soát (ADR-043, quyết định 9).

**Vì sao.** Trước ADR-043, run từ webhook chỉ mang `credentialsId`, không có target do
server quản lý, không ghim config revision, và module nằm trong thư mục con của monorepo
bị build nhầm thư mục gốc. Còn `actorKind` lấy từ `sender` của webhook có chữ ký vì đó là
"người SCM nói đã push" — **không** lấy từ tác giả commit, vì ai cũng tự ghi được tác giả.

#### Bước 2 — Delivery rules quyết định sự kiện gây ra gì

**Làm gì.** `domain/delivery_rules.py` là hàm thuần: luật vào, quyết định ra. Luật đầu
tiên khớp thì thắng. Module không khai báo gì thì nhận luật mặc định (bảng ở §3.2b):
push `main` → build + deploy môi trường mặc định (chỉ khi đó là dev/staging và có
target); push nhánh khác và pull request → chỉ build; tag `v*` → build, và nếu tag là
semantic version thì đăng ký thành version. Mẫu (pattern) theo ngữ nghĩa GitHub: `*`
dừng ở `/`, `**` thì không.

Các điểm cần nhớ:

- **Không luật nào đưa thẳng lên prod.** `deployTo: prod` bị từ chối 422. Pull request
  không deploy đi đâu cả.
- **PR từ fork mặc định bị bỏ qua.** Nếu module bật `forkPullRequests: verify` thì chỉ
  verify: `NETCI_PUBLISH=false`, không ký, không publish, khoá ký không bao giờ được đưa
  vào build đó; API từ chối digest của run như vậy (`UNPUBLISHED_RUN_HAS_NO_ARTIFACT`),
  và database cũng có CHECK constraint chặn.
- **Sự kiện không luật nào xử lý** được trả **200 `ignored`** kèm lý do.
- **Path filter (ADR-051).** Một luật có thể khai `paths` (chỉ khớp nếu có file thay đổi
  nằm trong danh sách) hoặc `pathsIgnore` (không khớp nếu **mọi** file thay đổi đều bị bỏ
  qua) — không được khai cả hai, và không dùng cho luật tag. Luật bị lọc thì rơi xuống luật
  kế tiếp. Danh sách file thay đổi lấy từ payload SCM, **chỉ khi payload đầy đủ**. Push
  GitHub bị coi là "không biết" khi: nhánh mới, force-push, `before` rỗng, danh sách rỗng
  hoặc sai định dạng, hoặc chạm giới hạn 2048 commit. Push GitLab chỉ đầy đủ khi
  `total_commits_count` bằng số commit được liệt kê. Pull request **luôn** là "không biết"
  vì payload không có danh sách file. "Không biết" nghĩa là **không áp dụng filter** — luật
  vẫn khớp, và lý do của run ghi rõ "changed files unknown, path filter not applied".

**Vì sao.**

- *Tách CI và CD* (ADR-043): trước đây mọi sự kiện đều deploy. Push `feature/x` thay luôn
  thứ `dev` đang chạy; một PR chưa ai review cũng vậy. CI chỉ nên xác lập một sự thật —
  "image này, cho component này, đã sẵn sàng"; đưa nó đi đâu là thao tác của platform, có
  kiểm soát quyền riêng.
- *Prod không qua trigger*: production phải đi qua production request và người thứ hai
  (ADR-038/039). PR không deploy vì đó là code chưa review.
- *Fork không được ký*: build của fork chạy code của fork, và code đó có thể đọc được khoá
  ký nếu stage Sign chạy.
- *200 thay vì 4xx*: trả 4xx khiến SCM đánh dấu webhook là lỗi và gửi lại.
- *Path filter chỉ lọc khi biết chắc* (ADR-051): nguy hiểm thật sự nằm ở chiều ngược lại —
  một filter bỏ build khi netCI không thực sự biết cái gì đã đổi sẽ đưa lên một thay đổi
  **chưa ai build**. "Không biết cái gì đổi" không phải bằng chứng "không có gì liên quan đổi".
- *Không lọc từng stage*: bỏ một stage bắt buộc (SBOM, scan, chữ ký) vì diff sẽ tạo ra
  artifact mà chính netCI từ chối deploy. Chỉ bỏ nguyên cả run.

#### Bước 3 — Run được ghi `queued`, ý định cố định

**Làm gì.** Mọi run được ghi `queued` **trong chính transaction chấp nhận nó**. Cùng lúc,
ý định của run được cố định (migration 0028): `deploy_after_build`, `publish_artifact`,
`release_tag`, `trigger`. Retry giữ nguyên ý định đó.

**Vì sao.** Trước ADR-043 không có cách nào build mà không deploy: mọi build thành công
đều tạo deployment. Giờ một build không deploy kết thúc `succeeded` với digest và **không
có** deployment. Với run verify-only của fork, API từ chối digest mà CI báo về, vì báo cáo
đó đến từ chính code của fork — nên "run này có publish không" phải được ghi **trước** khi
code đó chạy, chứ không đọc từ những gì nó báo lại (ADR-043, quyết định 3–4).

#### Bước 4 — Admission: chờ tới lượt, không bị từ chối

**Làm gì.** Gửi run sang Jenkins là một bước riêng gọi là **admission** (ADR-050). Run
chỉ được admit khi phạm vi quota của nó còn chỗ (`max_concurrent_pipelines`, đếm các run
đang giữ năng lực CI: đã admit mà chưa xong). Quá giới hạn thì run **chờ trong
PostgreSQL**. Admission chạy sau khi một run được chấp nhận, sau khi một run xong, và theo
timer; dưới một advisory lock; mỗi lần admit là một compare-and-set trên `version` của
run, nên hai replica không bao giờ gửi cùng một run hai lần.

Thứ tự: **người trước, agent sau, rồi cũ trước** (ADR-052; câu SQL trong `store/postgres.py`
sắp theo `actorKind = 'agent'` rồi `created_at`). Hàng đợi có giới hạn:
`max_queued_pipelines` (mặc định 50 mỗi phạm vi quota) — đầy thì trả
`429 PIPELINE_QUEUE_FULL`, đây là trường hợp **duy nhất** còn bị từ chối.

Admission còn hỏi chính Jenkins: mỗi controller khỏe chỉ nhận thêm build khi hàng đợi của
nó còn ít hơn `NETCI_ADMISSION_MAX_JENKINS_QUEUE` (mặc định 2). Controller đang chết, hoặc
không đọc được hàng đợi, thì không nhận gì: run tiếp tục chờ trong netCI chứ không bị đánh
`failed` lúc gửi đi. Con số executor cấu hình sẵn không được dùng làm giới hạn, vì agent
của Jenkins chạy dưới dạng pod nên số executor không phải giới hạn thật.

**Vì sao.** Coding agent push nhanh hơn người: một agent sửa một PR có thể push 20 commit
trong 2 phút. Trước đây mỗi commit được gửi Jenkins ngay và giữ một executor tới khi xong,
kể cả 19 cái đã lỗi thời trước khi bắt đầu. Quota cũ (migration 0017) chặn bằng
`429 PIPELINE_QUOTA_EXCEEDED` — bảo vệ được Jenkins nhưng **mất sự kiện**: GitHub không
gửi lại webhook bị trả 429, và commit quan trọng nhất (commit cuối) có thể chính là cái
bị từ chối. Hàng đợi phải có giới hạn vì hàng đợi vô hạn chỉ chuyển quá tải từ Jenkins
sang database. Người được ưu tiên vì một cơn bão push của agent không được bắt người đang
chờ đúng một build phải đợi.

> Run `queued` có `admittedAt` rỗng nghĩa là **đang chờ admission**, không phải bị kẹt.
> Lưu ý: "năng lực" ở đây là quota cấu hình, **không** phải số executor rảnh thật của
> Jenkins — đọc executor từ controller là bước tiếp theo, chưa làm (ADR-050).

#### Bước 5 — Supersession: huỷ run đã lỗi thời

**Làm gì.** Run do SCM kích hoạt trên cùng một ref tạo thành một **concurrency group**
(`<application>:<ref>`, ví dụ một nhánh hoặc `pr/12`). Khi run mới vào nhóm:

| Run cũ đang ở đâu | Điều gì xảy ra | Vì sao |
|---|---|---|
| Đang **chờ admission** | Huỷ ngay, `supersededBy` = run mới | Nó chưa bắt đầu, nên huỷ không tốn gì và không mất gì: commit mới đã chứa nó |
| **Đang build**, là pull request | Huỷ (`cancelInProgress` mặc định `true`) | Build cũ của PR trả lời một câu hỏi không còn ai hỏi |
| **Đang build**, là push nhánh | Để chạy xong (`cancelInProgress` mặc định `false`) | Build cũ của nhánh có thể chính là build sẽ deploy |
| Đã **bắt đầu sign hoặc publish**, đang **chờ duyệt**, hoặc **đã có deployment** | **Không bao giờ huỷ** | Dừng ở đó sẽ để lại artifact publish dở hoặc môi trường deploy dở — tốn hơn cái build tiết kiệm được |

Trong code, "điểm không quay lại" là các stage `sign`, `publish`, `deploy`, `health-check`
(`_POINT_OF_NO_RETURN_STAGES` trong `delivery.py`).

**Không bao giờ vào nhóm** (được admit như mọi run, nhưng không thay ai và không bị ai thay):

- **Build từ tag** — một bản release không bao giờ bị bỏ qua.
- **Run bấm tay** — một người đã yêu cầu **đúng** commit đó.
- **Retry.**

Run bị thay kết thúc `cancelled` kèm `supersededBy`, commit status nói rõ, và việc huỷ có
audit (`pipeline.superseded`).

> Về thứ tự thật: supersession xảy ra **ngay trong transaction ghi run mới** (bước 3), nên
> run cũ đang chờ bị huỷ trước khi admission kịp chọn nó. Build cũ đã được gửi Jenkins thì
> được abort **sau** khi transaction commit (`_supersede` / `_abort_superseded` trong
> `delivery.py`). Ở đây tách thành bước riêng cho dễ hiểu.

**Vì sao.** Cùng lý do ở bước 4: 20 push của agent chỉ nên tốn khoảng 2 build (một đang
chạy, một đang chờ), không phải 20. ADR-050 nói rõ con số "khoảng 2" là **tính chất của
luật**, chưa phải số đo.

#### Bước 6 — Jenkins chạy các stage

**Làm gì.** Được admit rồi, run mới được gửi sang Jenkins (qua `JenkinsRouter`, như §3.3).
Template container có các stage `checkout → unit-test → build → sbom → vulnerability-scan
→ sign → publish → deploy → health-check` (`TEMPLATES` trong `delivery.py`). Run chỉ-build
dừng sau publish — ví dụ live ở ADR-043 chạy 7 stage. Build verify-only của fork không có
`sign` và `publish` (Jenkins nhận `NETCI_STAGES` không chứa hai stage đó).

**Vì sao.** Tách build khỏi deploy (ADR-004, ADR-043) — xem §3.2 và §3.2b.

#### Bước 7 — SBOM, scan, chữ ký và SLSA provenance

**Làm gì.**

1. Stage Sign ký image bằng cosign (như trước), rồi **attest SLSA v1 provenance** bằng
   cùng khoá (`cosign attest --type slsaprovenance1`) và verify ngay tại chỗ
   (`cosign verify-attestation`). Nội dung provenance do `netci_callback.py provenance`
   tạo, **chỉ** từ những gì netCI bảo build làm và từ chính Jenkins: repository, commit
   chính xác, thư mục app, tên image, run id, controller, build URL. Credential trong URL
   repository bị xoá, vì attestation nằm trong registry — ai pull được là đọc được.
2. Khi CI gửi evidence, API **gắn provenance với run**: commit hoặc repository khác với
   cái netCI đã giao → `deny`. Kết luận deny được lưu cùng evidence và **ràng buộc mọi lần
   đánh giá sau**, nên không thể đọc lại thành allow. Provenance có mặt nhưng chưa verify
   bị từ chối ngay cả khi không bắt buộc provenance.
3. CI gửi tài liệu SBOM CycloneDX (`POST /pipeline-runs/{id}/sbom`, ADR-045). netCI lưu
   nó theo **digest có trong evidence của run đó**, không theo digest người gọi đưa, nên
   một build không thể gắn SBOM vào artifact khác. Tài liệu đầu tiên cho một digest được
   giữ (digest = nội dung bất biến). Kết quả scan của chính build cũng được giữ
   (`source = ci`).
4. `NETCI_REQUIRE_PROVENANCE` (chart: `supplyChain.requireProvenance`, mặc định `true`)
   từ chối container image không có provenance đã verify. Mặc định trong code là `false`
   để stack systemd trong lab (còn dùng library cũ) vẫn chạy. Binary systemd được ký dạng
   blob, không có attestation — ghi là `not_applicable`, **không bao giờ** ghi là pass.

**Vì sao.** Chữ ký chỉ chứng minh "người giữ khoá đã ký *một thứ gì đó*", không nói cái
gì đã được build. Một image build từ commit bất kỳ, repo bất kỳ, rồi ký bằng khoá thật vẫn
qua cả hai lần kiểm chữ ký. Trước ADR-044, bản ghi duy nhất nối digest với commit là dòng
`pipeline_runs` của netCI — thứ mà cụm Kubernetes hay một netCI khác không đọc được, và
một build bị chiếm quyền có thể báo bất kỳ digest nào. Còn SBOM (ADR-045): trước đây chỉ
lưu **đường dẫn trên máy build**, mà máy build bị xoá sau mỗi lần build.

> **Giới hạn phải biết**: đây là SLSA Build **L2**, chưa phải L3. Provenance được tạo trên
> chính agent đã chạy code của build, với khoá được đưa vào ở bước đó. Một build lấy trộm
> được khoá cũng làm giả được provenance. Cái ADR-044 đóng lại là lỗ hổng khác: image ký
> bằng khoá hợp lệ nhưng build từ nguồn khác.

Nếu có change freeze đang hiệu lực (ADR-047), **kết quả CI vẫn được ghi**: build thành
công dạng chỉ-build, có dòng log nêu tên freeze. Lý do: từ chối callback sẽ làm mất kết quả
build, còn artifact thì promote được sau khi freeze hết.

#### Bước 8 — Deploy dev, promote lên staging, và kiểm chứng sau deploy

**Làm gì.**

- Nếu luật nói `deployTo: dev`, run tạo deployment dev như luồng §3.3.
- Lên staging là **promote**: `POST /modules/{id}/promotions` deploy artifact của một run
  đã publish sang dev hoặc staging qua `redeploy_artifact` — **không build gì cả**, cùng
  lease, cùng máy trạng thái, cùng workflow như mọi deployment.
- Luật promotion có thể đòi digest đó đã **healthy ở môi trường trước N phút**:
  `requireHealthyIn` + `minSoakMinutes` (tối đa 10080 = một tuần; soak cần
  `requireHealthyIn` để biết đo ở đâu). Mặc định (module không khai luật): nếu module có
  target staging, promote lên staging đòi digest đã healthy ở dev (khi module có dev).
- Soak được đo từ `deployments.healthy_at` (migration 0029), ghi **một lần** khi chuyển
  sang healthy, và ngừng đếm khi deployment kế tiếp ở môi trường đó healthy. Deployment bị
  rollback hoặc failed không chứng minh gì. Deployment từ trước 0029 không có `healthy_at`
  nên không chứng minh soak nào.
- Promote lên prod bị từ chối `422 PRODUCTION_REQUIRES_REQUEST`. Có freeze thì promote /
  redeploy dev hoặc staging bị từ chối `409 CHANGE_FREEZE`.

Mỗi deployment (mọi môi trường), worker làm theo thứ tự:

1. Verify chữ ký cosign **và** SLSA provenance **từ registry**, ngay trước khi deploy
   (ADR-044). Nó đòi một statement cho đúng digest, ghi đúng commit của deployment (ở cả
   `externalParameters` và `resolvedDependencies`) và đúng repository của module — commit
   và repo lấy **từ server**, không từ evidence do CI viết. Ở chế độ
   `NETCI_SIGNATURE_VERIFY_MODE=none` không verify được provenance, nên nếu provenance bắt
   buộc thì **fail closed**.
2. Deploy (Ansible / Helm / systemd), rồi health check của runtime.
3. **Post-deploy verification** (ADR-046) nếu module khai `pipelineConfig.verification`:
   một hoặc hai truy vấn PromQL (`errorRate`, `p95LatencyMs`), ngưỡng, cửa sổ thời gian
   (1–60 phút), chu kỳ lấy mẫu, và các môi trường áp dụng. Activity `verify_release` lấy
   mẫu mỗi chu kỳ trong suốt cửa sổ, dừng ở lần vượt ngưỡng đầu tiên. **Vượt ngưỡng, hoặc
   truy vấn không trả dữ liệu suốt cả cửa sổ**, đi cùng đường với health check hỏng:
   rollback tự động (khi `rollbackStrategy` là `automatic` — mặc định), ngược lại ghi
   `failed` để người xử lý.

**Vì sao.**

- *Promote không build lại* (ADR-043): trước đây chỉ production có "deploy digest đã
  build". Lên staging nghĩa là run mới, tức là build lại commit — digest đã test không còn
  là digest được deploy.
- *Soak đo từ `healthy_at`*: `updated_at` không dùng được vì nó đổi ở mọi lần chuyển trạng
  thái sau đó. Delivery event cũng không dùng được: chúng chỉ ghi cho production vì DORA
  đọc chúng, thêm môi trường khác sẽ làm sai DORA.
- *Post-deploy verification* (ADR-046): health check chỉ chứng minh tiến trình đã chạy và
  trả lời URL health. Một bản trả 500 cho một phần ba request thật vẫn qua. "Không có dữ
  liệu" bị coi là hỏng vì module đã khai "hãy đánh giá tôi bằng các số này" mà không có số
  nào để đánh giá. Module không khai gì thì deploy như cũ, và thông báo deploy **không**
  nói "verified".
- *Canary* cũng đổi (ADR-046): trước đây số liệu canary lấy từ **body request** — nút
  "Advance" trên Portal không gửi gì, và `evaluate({})` đọc thành 0% lỗi, 0 ms độ trễ, nên
  mọi canary đều "qua phân tích" một bộ dữ liệu rỗng. Giờ server tự đọc Prometheus
  (`{track} = canary`); request mang số liệu bị từ chối 422; không phân tích được thì 409 —
  không advance cũng không abort, vì "không biết" không phải "xấu". Reviewer có thể
  advance kèm `overrideReason`, được audit là `canary.advanced_without_analysis` và trả
  `analysed: false`, không bao giờ là pass.
- *Placeholder trong PromQL* chỉ có `{release}`, `{environment}`, `{track}`, và giá trị
  thay vào phải là DNS label (kiểm bằng `fullmatch`), vì chúng được dán vào label matcher
  của PromQL.

#### Bước 9 — Production: version, request, người thứ hai duyệt, freeze

**Làm gì.**

- Artifact lên production qua **version** (ví dụ tag `v1.2.3` tự đăng ký version trong
  **cùng transaction** ghi build thành công) → **Production Request** → **một người khác
  người yêu cầu** duyệt (separation of duties, ADR-038/039).
- Production request tôn trọng luật promotion cho prod (ví dụ đòi đã healthy ở staging N
  phút). **Đổi luật promotion của prod cần người thứ hai duyệt**, như đổi target production.
- **Change freeze** (ADR-047) được thực thi ở nơi deployment bắt đầu di chuyển:
  - production request có `scheduledFor` rơi vào freeze của prod bị từ chối **ngay lúc
    tạo** — người lên kế hoạch biết ngay hôm nay, không phải lúc duyệt;
  - production deployment bị từ chối **lúc được duyệt**, thời điểm nó sắp bắt đầu;
  - **rollback không bao giờ bị từ chối**;
  - lối đi qua freeze là **break-glass cho đúng freeze đó** (`target_type = change_freeze`),
    dùng luồng break-glass sẵn có: người khác duyệt, có hạn.
  - Freeze là bản ghi (`change_freezes`, migration 0031); release manager và platform
    admin tạo và huỷ. Freeze hiện trên Release Calendar.
- Sau khi deploy prod, bước verify / rollback giống bước 8.

**Vì sao.**

- *Version trong cùng transaction*: nếu đăng ký sau, máy chết ở giữa sẽ để lại một tag mà
  không production request nào gọi tên được — và callback gửi lại cũng không sửa được vì
  run đã succeeded.
- *Đổi luật prod cần người thứ hai*: bỏ soak staging là làm yếu cổng production.
- *Freeze* (ADR-047): trước đây "maintenance" chỉ là cờ đúng/sai trên server, còn freeze
  ("không đổi production trong đợt sale") nằm trong tin nhắn chat — deploy lúc đó chỉ bị
  chặn nếu có người nhớ. Rollback không bị chặn vì freeze tồn tại để **bảo vệ** dịch vụ,
  không phải để ngăn **khôi phục** nó.

> **Lỗ hổng đã biết** (ADR-047): production deployment được duyệt **trước** freeze và lên
> lịch bắt đầu **trong** freeze (`notBefore`) thì **không** bị chặn — workflow ngủ tới giờ
> đó, và worker không đọc được bảng freeze. Việc chặn lúc tạo request che trường hợp phổ
> biến; kiểm tra lại trong workflow là cách sửa, **chưa làm**.

#### Bước 10 — Sau khi chạy: rescan SBOM và "CVE này đang chạy ở đâu?"

**Làm gì** (ADR-045).

- netCI tự quét lại SBOM đã lưu (`trivy sbom`, bật bằng `NETCI_SBOM_RESCAN=trivy`): theo
  lịch (`NETCI_SBOM_RESCAN_INTERVAL_SECONDS`, mặc định 6 giờ, mỗi lượt chỉ một replica
  chạy nhờ advisory lock) và theo yêu cầu (`POST /vulnerabilities/rescan`). **Chỉ quét
  digest đang chạy ở đâu đó.** Mỗi lần quét thay toàn bộ finding `rescan` của digest đó
  trong một transaction; finding vẫn còn thì giữ `first_seen_at`.
- **Quét thất bại được ghi là thất bại và không đổi finding nào.** Thiếu trivy, không tới
  được database của trivy, timeout, output không phải JSON — đều ném lỗi, không bao giờ
  ra danh sách rỗng.
- API exposure (`GET /vulnerabilities/{id}/exposure`,
  `GET /vulnerabilities/exposure?minSeverity=`) trả module + môi trường bị ảnh hưởng **và
  luôn nói độ phủ**: liệt kê mọi digest đang chạy mà nó không bảo đảm được — chưa có SBOM,
  chưa từng rescan, hoặc lần rescan gần nhất thất bại.

**Vì sao.** Câu đầu tiên đội bảo mật hỏi khi có CVE mới là *"mình đang chạy nó ở đâu?"*.
netCI đã biết digest nào đang chạy ở môi trường nào, nhưng dữ liệu lỗ hổng duy nhất nó
giữ là kết quả scan **ngày build** — một CVE công bố sau đó, theo định nghĩa, không có
trong đó. "Không quét được" tuyệt đối không được đọc thành "sạch".

> Câu trả lời chỉ mới bằng lần rescan gần nhất và database trivy nó dùng. Artifact build
> trước thay đổi này không có SBOM trong netCI — hiện là "không phủ" tới khi build lại.

#### Bước 11 — Preview cho pull request (ADR-049)

**Làm gì.**

- Một preview **thuộc về run PR đã build nó**. Nó được yêu cầu khi run PR **cùng
  repository** thành công với digest đã publish và module bật `pipelineConfig.previews`
  (hoặc yêu cầu tường minh cho run như vậy). PR từ fork là verify-only nên không có
  artifact và **không bao giờ** có preview.
- Worker deploy nó; **chỉ báo cáo của worker** (scope dùng một lần `preview:result`, gắn
  với run PR) mới làm nó thành `active`. Trước đó nó là `deploying`. URL là host của
  Ingress mà cụm **thật sự** tạo, do playbook đọc lại — netCI không tự ghép URL. Báo cáo
  không có URL thì URL để trống.
- Preview chạy bằng playbook riêng `preview-kubernetes.yml`, chỉ nhận namespace dạng
  `preview-<module>-pr-<n>`; đọc namespace trước khi tạo; tạo namespace có nhãn
  `netci.io/preview=true`, và chỉ xoá namespace mang nhãn đó. Playbook production vẫn từ
  chối mọi namespace ngoài dev, staging, prod.
- Preview kết thúc khi PR đóng hoặc merge (từ webhook), khi hết TTL (1–72 giờ, có reaper
  chạy dưới advisory lock), hoặc khi có yêu cầu. Việc xoá cũng là một thao tác của worker
  có báo cáo kết quả.
- Chỉ runtime Kubernetes, cần target dev có kubeconfig. Module Docker/systemd cấu hình
  preview bị từ chối. Các dòng preview từ trước thay đổi này thành `unverified`
  (migration 0032).

**Vì sao.** Trước ADR-049, "tạo preview" chỉ ghi một dòng `status = active` với URL
`https://prv-<app>-<pr>.preview.netci.internal` — không có gì được deploy, không có gì trả
lời URL đó. Reviewer bấm link không thấy gì, trong khi mọi màn hình ở giữa đều nói nó tồn
tại. Đó đúng là "màu xanh giả" mà §4.6 cấm. Nhãn namespace quan trọng vì kubeconfig của
worker phải được quyền tạo và xoá namespace — rộng hơn chỉ deploy vào namespace có sẵn;
nhãn là thứ giữ nó không chạm namespace khác (RBAC chặt hơn được khuyến nghị, chưa đi kèm).

#### Bước 12 — Commit status và PR comment qua outbox (ADR-048)

**Làm gì.** Suốt hành trình, mỗi lần run đổi trạng thái, `_notify_scm_status` **thêm một
dòng outbox** (`scm.commit_status`) trong cùng unit of work với transition. Khi run PR
thành công hoặc thất bại thì thêm dòng thứ hai (`scm.pr_comment`), nói build được phép làm
gì: đã publish kèm digest nhưng chưa deploy, hay chỉ verify (fork). Audit ghi
`scm.status_queued`. Outbox worker (`adapters/scm_reporter.py`) mới gọi API thật: GitHub
statuses + issue comments, GitLab statuses + merge-request notes. Token đọc từ file
(`NETCI_GITHUB_TOKEN_FILE`, `NETCI_GITLAB_TOKEN_FILE`). Thiếu token, repo/SHA/số PR sai,
hoặc phản hồi không phải 2xx → ném lỗi → outbox thử lại có backoff → rồi dead-letter, và
cảnh báo `NetciOutboxDeadLetters` (docs/SLO.md) làm nó hiện ra.

**Vì sao.** ADR-048 **sửa lại một tuyên bố sai** của ADR-019. ADR-019 nói netCI báo trạng
thái run về GitHub/GitLab — thực ra không: hàm `update_commit_status` chỉ đọc token, ghi
một dòng log rồi trả về, không có HTTP request nào; vậy mà audit vẫn ghi
`scm.status_updated` mỗi lần. Audit trail khẳng định SCM đã được báo một thứ nó chưa bao
giờ nhận. Ngoài ra lời gọi nằm **trong** transaction của transition, nên SCM chậm sẽ giữ
kết nối database. Giờ audit ghi `scm.status_queued` — đúng thứ thực sự biết lúc đó.

> Hệ quả: status tới **ngay sau** transition, không phải trong lúc đó. Mỗi provider chỉ hỗ
> trợ một token; `credentialReference` theo repository được mang trong payload nhưng
> **chưa dùng**.

### 3.5. AI agent là người dùng của netCI (ADR-052)

Coding agent giờ tự mở nhánh, push commit và chạy build qua netCI. Trước ADR-052, với
netCI chúng trông giống người lập trình viên có token mà chúng mang, hoặc giống một người
gửi SCM ẩn danh. Không có gì ngăn một agent giữ role reviewer rồi duyệt thay đổi — kể cả
thay đổi của chính nó — và separation of duties so sánh hai cái tên mà có thể cả hai đều là
agent. Cũng không có gì đưa build mà một người đang chờ lên trước một cơn bão build của agent.

**Server quyết định ai là agent** — không bao giờ từ tác giả commit (ai cũng ghi được):

| Nguồn | Cách nhận biết |
|---|---|
| OIDC | Thuộc nhóm trong `NETCI_OIDC_AGENT_GROUPS`, hoặc token cấp cho OAuth client trong `NETCI_OIDC_AGENT_CLIENTS` (đọc từ claim `azp`) |
| Token file | Trường `kind:` trong file token |
| Webhook | `sender` nằm trong `NETCI_AGENT_SCM_LOGINS` — **chỉ dùng để xếp hàng, không cấp quyền gì** |

**Agent được làm gì**

- Mọi việc của role **developer** và **viewer** (trừ ba việc bên dưới), ví dụ chạy build
  qua API và xem kết quả. Push commit là việc ở SCM; webhook của push đó khởi động build
  như mọi push khác.
- **Yêu cầu** production release. Nhưng một **người** phải duyệt, và separation of duties
  chặn người đó trùng với người yêu cầu.

**Agent không được làm gì**

- **Giữ role reviewer hoặc platform admin.** Agent mà role map sẽ biến thành reviewer/admin
  bị **từ chối**: `403 AGENT_ROLE_NOT_ALLOWED` với token OIDC, lỗi cấu hình với token file.
  Không âm thầm hạ xuống developer — vì một token nói "reviewer" mà hoạt động như
  "developer" sẽ che mất lỗi map role (đúng nguyên tắc "từ chối tốt hơn âm thầm bỏ qua").
- Vì vậy agent **không duyệt được gì**: deployment, production request, cấu hình,
  break-glass, VEX.
- **Ba việc của developer dành riêng cho người**:
  - yêu cầu break-glass (`403 AGENT_MAY_NOT_BREAK_GLASS`);
  - cấu hình SCM integration — repo nào được phép khởi động build và secret chứng minh
    điều đó (`403 AGENT_MAY_NOT_CONFIGURE_SCM`);
  - xoá system (`403 AGENT_MAY_NOT_DELETE_SYSTEMS`).

**Trong hàng đợi**: run ghi `trigger.actorKind` (loại principal với run qua API; `agent` với
webhook có `sender` trong `NETCI_AGENT_SCM_LOGINS`). Admission xếp **người trước agent**
(§3.4 bước 4), và supersession (bước 5) là cơ chế khiến 20 push của agent không chiếm 20
executor.

**Việc của SCM, không phải của netCI**: việc merge vào nhánh được bảo vệ do **SCM** thực
thi (branch protection). netCI chỉ kiểm soát những gì nó quyết định: build, deployment và
phê duyệt. Muốn chặn agent merge thẳng vào `main`, phải cấu hình branch protection ở
GitHub/GitLab.

> Audit record mang `subject` của actor, **không** mang loại (kind); loại hiện ở `/me` và
> trên run.

### 3.6. Đã kiểm chứng live tới đâu (ADR-043 → ADR-052)

"Live" ở đây nghĩa là chạy với hạ tầng thật của lab (Jenkins A, registry, Temporal worker
trong cụm, Prometheus lab, server `netci-prod-02`) qua bản cài Kubernetes của netCI. Bảng
dưới **chỉ** chép những gì mục "Live evidence" của từng ADR tự ghi. ADR nào không có mục đó
thì mới chỉ **có test tự động**, chưa kiểm chứng live.

| ADR | Đã chạy live (theo chính ADR) | Chưa kiểm chứng live |
|---|---|---|
| **043** CI/CD tách riêng | 2026-09-23, bản cài 0.2.0-rc6: hai pod API chạy migration 0028–0029 đồng thời; một run chỉ-build của `payments-api` trên Jenkins A (build #3) qua cả 7 stage, kết thúc `succeeded` với `sha256:7d39140d…`, **không** có deployment; promote run đó lên dev → deployment healthy, có `healthy_at`, container trên `netci-prod-02` báo đúng digest; promote lên prod bị từ chối 422; promote lên staging bị từ chối vì module không có target staging | SCM webhook (git server của lab là HTTP thường, không gửi webhook), pull request và fork, đăng ký version từ tag, luật soak |
| **044** SLSA provenance | 2026-09-24, rc10: build #5 attest provenance, evidence ghi `provenance.verified = true`, commit `fe28b3a2…`, repo `http://172.17.0.52/payments-api.git`, với `requireProvenance: true`; khi promote lên dev, worker trong cụm log *cosign verified …* và *SLSA provenance verified: built from … at fe28b3a2…* trước khi deploy; deployment healthy | ADR không ghi mục "chưa kiểm chứng". Nó nói rõ: kiểm tra attestation ở Kubernetes admission **chưa được xây**; builder id ghi `jenkins#netci-shared-library` vì container agent không có `JENKINS_URL` |
| **045** SBOM + rescan | 2026-09-24, rc10: build upload SBOM (938 component, `cyclonedx-json`); lượt rescan theo lịch đầu tiên và `POST /vulnerabilities/rescan` đều quét nó bằng trivy trong image API (mirror database của lab, `--insecure` vì mirror là HTTP); `GET /vulnerabilities/CVE-2025-8869/exposure` trả `payments-api` ở dev (pip 25.0.1, sửa ở 25.3, source `rescan`), coverage 1/1/1, 0 failed, 0 not covered. Scan của build (chỉ HIGH/CRITICAL) không thấy gì — các finding MEDIUM này có trong netCI **chỉ nhờ rescan** | ADR không ghi mục "chưa kiểm chứng" và không mô tả chạy live nhánh quét thất bại |
| **046** Post-deploy verification | 2026-09-24, rc11, Prometheus lab: `payments-api` chưa xuất metric request, nên lần **qua** dùng truy vấn thay thế trên metric của chính Prometheus (`prometheus_http_requests_total`) — 3 mẫu trong ngưỡng, deployment healthy. Trỏ truy vấn vào metric thật của service (không có series) → *deployment=failed … Prometheus returned no data for the whole window; automatic rollback completed* | **Canary analysis** chưa chạy live (lab không có module hỗ trợ canary có metric request). Lần "qua" chưa dùng metric của chính ứng dụng |
| **047** Change freeze | — (ADR không có mục live evidence) | **Chỉ có test**, chưa kiểm chứng live |
| **048** SCM status qua outbox | — | ADR nói rõ: **chưa kiểm chứng với GitHub/GitLab thật** (git server lab là HTTP thường, không có status API). Test dùng HTTP mock transport và vòng outbox |
| **049** Preview | — | ADR nói rõ: **chưa kiểm chứng live** cho tới khi một PR preview chạy trên cụm lab |
| **050** Admission + supersession | 2026-09-25, rc15, Jenkins A: 12 commit trên nhánh `agent/storm2` của `payments-api`, mỗi commit một webhook push GitHub có chữ ký, gửi trong 0,9 giây, quota 1 build đồng thời. Cả 12 được nhận (201). Chỉ 2 được gửi Jenkins: commit đầu (admit ngay) và commit thứ 12 (admit 44,9 giây sau khi được ghi, lúc cái đầu xong). Cả hai build thành công. 10 cái còn lại `cancelled` có `supersededBy`, **trước** admission — Jenkins không hề thấy. Tổng 101 giây | `cancelInProgress` (dừng build **đang chạy**) chưa chạy live — push mặc định là `false`. Đọc executor thật của Jenkins chưa làm |
| **051** Path filter | — | **Chỉ có test**, chưa kiểm chứng live. PR luôn build đầy đủ vì đọc danh sách file từ API SCM chưa làm |
| **052** Agent principal | — | **Chỉ có test**, chưa kiểm chứng live |

> Hai điểm dễ hiểu nhầm:
>
> - ADR-043 ghi "SCM webhook chưa kiểm chứng live". ADR-050 sau đó gửi 12 webhook push
>   **định dạng GitHub, có chữ ký** tới netCI trong lab — nghĩa là đường nhận webhook của
>   netCI đã chạy thật, nhưng webhook do bài test tự gửi, **không** phải do GitHub/GitLab
>   thật gửi. Pull request, fork và tag vẫn chưa chạy live.
> - Lần đầu thử "cơn bão" ở ADR-050 build một cây thư mục rỗng (bản clone của bài test về
>   rỗng qua HTTP), netCI báo đúng cả hai build hỏng ở `Build` vì thiếu Dockerfile. Đó là
>   lỗi dựng bài test, không phải của netCI; nhánh bị xoá và chạy lại.

---

## 4. Tại sao lại là kiến trúc này

Đây là phần "vì sao code như vậy". 7 nguyên tắc, mỗi cái đều xuất phát từ một lỗi thật.

### 4.1. PostgreSQL là nguồn sự thật, đọc lại ở mỗi request

**Lỗi cũ**: `DeliveryPlatform` và `PortalReadModel` nạp **toàn bộ** dữ liệu vào
dictionary Python lúc khởi động, rồi trả lời mọi request từ RAM.

**Hậu quả thật**:
- Replica B không bao giờ biết system mà replica A vừa tạo. Dictionary của B là ảnh
  chụp lúc B khởi động, không có gì làm nó cập nhật.
- Vì phân quyền tính từ `platform.list_applications()`, replica B **từ chối** cho team
  truy cập chính application của họ.
- Thời gian khởi động tăng theo lịch sử — mọi dòng log từng ghi đều phải nạp lên RAM.

**Cách sửa** (ADR-014): mỗi command đọc đúng dòng nó sắp sửa, **bên trong transaction
của chính nó**. Không cache gì giữa các request. Không nạp gì lúc khởi động.

**Đánh đổi**: mỗi request tốn ít nhất một lần đi-về database. Chấp nhận được — đổi lấy
việc API không bao giờ trả lời từ ảnh chụp cũ.

### 4.2. Mọi thay đổi trạng thái phải atomic

Cụ thể: `POST /systems/{id}/modules` trước đây tạo Application ở transaction 1, gắn
Module ở transaction 2. Máy chết ở giữa → application mồ côi. Client retry → nhận
`MODULE_EXISTS` trong khi chính nó đã tạo ra cái application đó.

Giờ cả hai nằm trong **một** transaction, cùng với audit và idempotency record.

### 4.3. Máy móc phải có danh tính, không dùng mật khẩu chung

**Lỗi cũ**: `NETCI_PIPELINE_API_KEY` — một khoá duy nhất cho mọi Jenkins và mọi worker.

Khoá đó chỉ chứng minh được "người gọi có mật khẩu của netCI". Nó **không** nói được:
- build nào đang gọi,
- thuộc application nào,
- là CI báo build hay worker báo deploy.

Nghĩa là: một Jenkins agent bị chiếm quyền có thể đánh dấu **bất kỳ** deployment nào là
"healthy".

**Cách sửa** (ADR-015): token ngắn hạn có chữ ký, mang theo:

```
iss  = ai cấp             aud = cho netCI nào
sub  = loại workload (jenkins/temporal)
application_id
pipeline_run_id HOẶC deployment_id   ← đúng một trong hai
scopes = danh sách thao tác được phép
iat/exp = thời hạn (tính bằng phút)
jti = mã duy nhất, dùng để chống phát lại
```

Xác thực chứng minh "token này thật". Một bước **riêng** (`_authorize_callback`) mới
chứng minh "token này dành cho *đúng* run này". Có bảng `WORKLOAD_SCOPES` khiến token
Jenkins **không thể được cấp** quyền `deployment:result` ngay từ lúc sinh.

### 4.4. Trình duyệt không được quyết định nơi deploy

**Lỗi cũ**: `parameters` là `dict[str, object]` tự do, đi thẳng vào CD adapter — nơi
code đọc các khoá `target_hosts`, `namespace`, `kubeconfig_ref`, `artifact_url`.

Nghĩa là: **trình duyệt chọn được máy nào bị deploy** chỉ bằng cách thêm một khoá JSON.

**Cách sửa**: file `build_inputs.py` viết rõ ranh giới thành **dữ liệu**:

| Loại | Ai quyết định | Ví dụ |
|---|---|---|
| **Build input** | Người gọi | `buildProfile`, `skipTests`, `logLevel` |
| **Application config** | Bất biến từ lúc đăng ký | runtime, template, repo |
| **Deployment parameter** | **Server**, tính từ cấu hình module | `target_hosts`, `namespace`, `kubeconfig_ref`, `artifact_url`, `playbook`, `health_command` |

Chi tiết quan trọng: khoá bị cấm sẽ bị **từ chối bằng 422**, *không* phải âm thầm bỏ qua.

> Vì sao? Bỏ qua âm thầm khiến người gọi tưởng override đã có tác dụng. Operator chỉ
> phát hiện lúc điều tra sự cố. Từ chối thì nói thẳng ngay.

### 4.5. Không hai deployment nào chạm cùng một target

Xem lại §2.3 (lease + fencing token). Điểm mấu chốt: **database quyết định**, không phải
code Python.

```sql
CREATE UNIQUE INDEX deployment_leases_one_active_per_target
    ON deployment_leases (application_id, environment, target)
    WHERE released_at IS NULL;
```

Vì sao phải ở database? Vì hai request có thể tới **hai replica khác nhau**. Code kiểu
"kiểm tra rồi ghi" trong một process không ngăn được race giữa hai process.

### 4.6. Không bao giờ hiển thị màu xanh giả

Đây là nguyên tắc mang tính triết lý của project.

Các biểu hiện cụ thể trong code:
- Cài đặt mới **trống rỗng**. Không có system/module mẫu. Dữ liệu demo chỉ xuất hiện khi
  bật `NETCI_DEMO_DATA=true`.
- DCIM chưa cấu hình → trả `status: "not_configured"` + danh sách rỗng, **không** trả
  dữ liệu bịa.
- Server không có nguồn health → trạng thái `unknown`, **không** mặc định `online`.
- `/readyz` trả 503 khi dependency bắt buộc hỏng.
- Rollback thất bại có state riêng `rollback_failed` — gọi nó là `failed` sẽ **mất**
  thông tin "đã thử khôi phục và không được".
- Rollback đi qua `rollback_in_progress` trước khi tới `rolled_back`. Ghi "đã khôi phục"
  trước khi thực sự khôi phục chính là màu xanh giả.

### 4.7. Deep module, seam rõ ràng

Xem §2.3. Cụ thể trong project:

| Seam | Interface | Production adapter | Test adapter |
|---|---|---|---|
| Lưu trữ | `PlatformDatabase.transaction()` | `PostgresDatabase` | `InMemoryDatabase` |
| Chạy CI | `CiLauncher.launch()` | `JenkinsCiLauncher` | `NullCiLauncher` |
| Điều phối CD | `CdOrchestrator.start()` | `TemporalCdOrchestrator` | `NullCdOrchestrator` |
| Xác thực người | `Authenticator.authenticate()` | OIDC / token file | anonymous (chỉ loopback) |
| Kiểm chữ ký | `SignatureVerifier.verify()` | `CosignVerifier` | none |
| Kho hạ tầng | `DcimCatalog` | HTTP DCIM | not_configured |
| SCM | `ScmProvider` | GitHub / GitLab | `MockScmProvider` |

Quy tắc bắt buộc: **adapter in-memory chỉ dùng cho test hoặc local mode**. Ngoài local,
thiếu cấu hình thì **không khởi động được** — chứ không âm thầm chạy chế độ giả.

---

## 5. Đi qua từng file

### 5.1. `backend/app/` — trái tim hệ thống

#### Tầng domain (luật nghiệp vụ — không biết gì về HTTP hay SQL)

| File | Dòng | Vai trò |
|---|---|---|
| `domain/models.py` | 272 | Định nghĩa các khái niệm gốc: `Application`, `PipelineRun`, `Deployment`, `DeliveryEvent`, và **máy trạng thái** (`PIPELINE_TRANSITIONS`, `DEPLOYMENT_TRANSITIONS`) — bảng nói rõ trạng thái nào được chuyển sang trạng thái nào. |
| `policy/rules.py` | 399 | Luật: role nào làm được gì, môi trường nào cần quyền gì, separation of duties, đánh giá security evidence. |
| `projections/dora.py` | 62 | Tính 4 chỉ số DORA từ `delivery_events`. Chỉ đọc, không ghi. |

> **Vì sao domain không biết SQL?** Để test được luật nghiệp vụ trong mili-giây, và để
> đổi database sau này mà không phải viết lại luật.

#### Tầng application (điều phối)

| File | Dòng | Vai trò |
|---|---|---|
| `delivery.py` | **2212** | **File lớn nhất và quan trọng nhất.** Class `DeliveryPlatform` — mọi thao tác trên application/run/deployment: tạo, chạy, nhận kết quả CI, phê duyệt, ghi kết quả deploy, rollback, lease, fencing, huỷ, retry. |
| `portal.py` | 1142 | Class `PortalService` — chuyển ngôn ngữ domain (application/run/deployment) sang ngôn ngữ Portal (System → Module → Version → Production Request). Trước đây tên là `PortalReadModel`, đã đổi vì nó **có** ghi dữ liệu, tên cũ nói dối. |
| `reconciler.py` | 251 | Watchdog: hỏi Jenkins/Temporal "run này thật ra thế nào rồi?", sửa các run bị kẹt do mất callback. |
| `demo_data.py` | 146 | Dữ liệu demo. Chỉ chạy khi `NETCI_DEMO_DATA=true`. |

#### Tầng lưu trữ — `app/store/` (deep module quan trọng nhất)

| File | Dòng | Vai trò |
|---|---|---|
| `store/__init__.py` | 107 | Interface `PlatformDatabase` + hàm `build_database()` chọn implementation. **Từ chối in-memory ngoài local mode.** |
| `store/session.py` | 237 | Protocol `PlatformSession` — liệt kê mọi phép đọc/ghi được phép trong một transaction. |
| `store/postgres.py` | **1531** | Toàn bộ SQL thật. Ánh xạ dòng ↔ object, compare-and-set, lease, fencing counter, log sequence an toàn. |
| `store/memory.py` | 669 | Bản in-memory cho test. Có **staging + rollback thật** — test tiêm lỗi giữa chừng sẽ thấy đúng hành vi như PostgreSQL. |
| `store/records.py` | 94 | Các dataclass mô tả dòng dữ liệu Portal: `SystemRow`, `ModuleRow`, `VersionRow`, `RequestRow`, `DeploymentLease`. |
| `persistence.py` | 113 | Kiểu dùng chung: `AuditRecord`, `IdempotencyRow`, `UnitOfWork`, các lớp lỗi. Tách ra để `postgres.py` và `memory.py` không phải import lẫn nhau. |

#### Tầng adapter — `app/adapters/` (chạm hệ thống ngoài)

| File | Dòng | Vai trò |
|---|---|---|
| `ci_launcher.py` | 189 | Seam CI. `NullCiLauncher` (test) và `JenkinsCiLauncher` (thật). |
| `jenkins_http.py` | 370 | Client HTTP tới Jenkins: tạo job, kích hoạt build, đọc trạng thái, abort. |
| `jenkins_router.py` | 50 | Chọn controller nào còn chỗ. Có nhiều Jenkins để chịu lỗi. |
| `cd_orchestrator.py` | 190 | Seam CD. `NullCdOrchestrator` và `TemporalCdOrchestrator`. |
| `signature_verifier.py` | 242 | Chạy `cosign verify` **độc lập** ngay trước deploy. |
| `dcim.py` | 258 | Hỏi hệ thống DCIM. Chưa cấu hình → `not_configured` + rỗng. |
| `scm.py` | 428 | GitHub/GitLab: kiểm chữ ký webhook, phân tích event, báo commit status ngược lại. |
| `interfaces.py` | 68 | Kiểu dùng chung giữa các adapter. |

#### Tầng transport & bảo mật

| File | Dòng | Vai trò |
|---|---|---|
| `main.py` | **2125** | Toàn bộ HTTP: 62 endpoint, các model request (Pydantic), xử lý lỗi, CORS, composition root (nơi lắp ráp mọi thành phần). |
| `auth.py` | 520 | Xác thực **người**: `none` (chỉ loopback) / `token` (file hash) / `oidc` (JWT thật). |
| `workload_identity.py` | 402 | Xác thực **máy**: sinh và kiểm token callback có scope (§4.3). |
| `build_inputs.py` | 181 | Ranh giới tin cậy của `parameters` (§4.4). |
| `readiness.py` | 179 | `/livez`, `/readyz`, `/operator/health` + circuit breaker. |
| `ratelimit.py` | 111 | Giới hạn số request. |
| `client_address.py` | 88 | Xác định IP thật khi đứng sau reverse proxy. Cần vì kiểm tra "loopback" bằng socket address là vô nghĩa khi có proxy. |
| `errors.py` | ~20 | Lớp `ApiError` chung cho `DeliveryError` và `PortalError`. |
| `runtime_environment.py` | 17 | Một chỗ duy nhất định nghĩa "khi nào được dùng chế độ dev". |

#### Workflow Temporal — `app/workflows/`

| File | Vai trò |
|---|---|
| `provision_and_deploy.py` | Định nghĩa workflow: deploy → chờ duyệt → health check → rollback nếu hỏng. |
| `activities.py` | Các "activity" — việc thật có side effect: chạy Ansible, Helm, verify chữ ký, gọi callback. |
| `worker.py` | Tiến trình worker nối tới Temporal và nhận việc. |

> **Workflow vs Activity**: workflow là logic điều phối, phải **deterministic** (chạy
> lại cho kết quả giống hệt) để Temporal replay được sau khi crash. Activity là phần
> "bẩn" — gọi mạng, ghi file. Temporal ghi lại kết quả activity để khi replay không
> chạy lại.

### 5.2. `backend/tests/` — 27 file, 419 test (backend + contract)

| File | Số test | Kiểm chứng điều gì |
|---|---|---|
| `test_api.py` | 30 | Hợp đồng HTTP: mã lỗi, idempotency, vòng đời deploy |
| `test_portal.py` | 28 | Luồng Portal, fail-closed khi mất DB |
| `test_integration_seams.py` | 27 | Seam CI/CD, outbox event, concurrency |
| `test_persistence_postgres.py` | 23 | **Chạy PostgreSQL thật**: sống sót restart, 2 replica, atomic onboarding |
| `test_authorization.py` | 20 | Role + team + separation of duties |
| `test_workload_identity.py` | 18 | Sinh/kiểm token, xoay khoá, hết hạn, sai audience |
| `test_security_exceptions.py` | 17 | Miễn trừ CVE có thời hạn |
| `test_auth_oidc.py` | 17 | Xác thực JWT thật |
| `test_deployment_leases.py` | 15 | **Lease, fencing, log append đồng thời** |
| `test_build_inputs.py` | 14 | Ranh giới tin cậy input |
| `test_callback_authorization.py` | 11 | Token run A không ghi được run B |
| `test_pipeline_lifecycle.py` | 11 | Cancel, retry, stage event, reconciler |
| `test_scm_webhooks.py` | 7 | Chữ ký webhook, chống replay |
| … | … | (còn 14 file khác) |

> **Vì sao có cả test in-memory và test PostgreSQL?** Test in-memory chạy trong ~12
> giây, dùng khi code. Test PostgreSQL chạy ~55 giây nhưng khẳng định được những điều
> in-memory không thể: hai process cùng ghi thì ai thắng, unique index có thật sự chặn
> không.

### 5.3. `frontend/src/` — giao diện web (React + TypeScript)

| File | Vai trò |
|---|---|
| `PortalShell.tsx` | Khung layout, thanh điều hướng, panel thông báo |
| `App.tsx` | Router, quản lý state phiên đăng nhập |
| `LoginPage.tsx` | Đăng nhập |
| `GeneralPages.tsx` | Dashboard, danh sách system, danh sách server |
| `ModulePage.tsx` | Chi tiết module: pipeline run, version, DORA |
| `ModuleSettings.tsx` | Cấu hình module |
| `NewModuleWizard.tsx` | Wizard đăng ký module mới |
| `ProductionRequestsPage.tsx` | Duyệt yêu cầu lên production |
| `AsyncState.tsx` | Component xử lý trạng thái loading/error/empty **một cách trung thực** |
| `api/netciClient.ts` | Client gọi API |
| `portalTypes.ts` | Kiểu TypeScript khớp OpenAPI |

### 5.4. `scripts/` — công cụ vận hành và gate nghiệm thu

| Nhóm | File | Việc |
|---|---|---|
| Schema | `migrate.py` | Chạy migration, sinh `schema.sql`, kiểm tra lệch |
| Backup | `netci_backup.py` | Backup + verify **19 bảng critical** |
| Nghiệm thu | `production_acceptance_harness.py` | 9 gate P0 với hạ tầng thật, ghi evidence JSON/JUnit |
| Gate | `gate_security.py`, `gate_e2e_container.py`, `gate_e2e_kubernetes.py`, `gate_e2e_systemd.py`, `gate_dora.py`, `gate_kind.py`, … | Mỗi gate chạy lệnh thật, ghi `evidence/<gate>.json` |
| Kiểm tra | `doctor.py`, `validate_platform.py`, `validate_release.py` | Kiểm tra môi trường và tính nhất quán |
| Tiện ích | `netci_token.py`, `netci_callback.py`, `trigger_pipeline.py` | Dùng trong CI template |

### 5.5. `docs/decisions/` — 20 ADR

**ADR = Architecture Decision Record**: ghi lại *một quyết định kiến trúc*, bối cảnh
dẫn tới nó, các phương án đã cân nhắc và **vì sao loại bỏ**, cùng hệ quả phải chấp nhận.

Vì sao quan trọng: 6 tháng sau, khi ai đó hỏi "sao không dùng mTLS?", câu trả lời nằm
sẵn trong ADR-015 chứ không phải trong trí nhớ của người đã nghỉ việc.

| ADR | Chủ đề |
|---|---|
| 001–013 | Ranh giới hệ thống, Portal vs Backstage, Temporal, Jenkins, runtime adapter, JCasC, agent cách ly, bảo mật artifact, multi-controller, local vs production, seam xác thực, ownership, projection trung thực |
| **014** | PostgreSQL canonical (Phase 1) |
| **015** | Workload identity + ranh giới input (Phase 2) |
| **016** | Lease, fencing token, log sequence (Phase 3) |
| **017** | Release bất biến + backup verification (Phase 4) |
| **018** | Readiness trung thực + acceptance harness (Phase 5) |
| **019** | SCM webhook + commit status (Phase 6) |
| **020** | Pipeline lifecycle + reconciler (Phase 7) |
| **023** | Multi-Module DAG Release Plan, SAGA Orchestration, Progressive Delivery (Phase 10) |
| **024** | Enterprise Policy Engine, Security Waivers, Break-Glass & Kubernetes Admission (Phase 11) |
| **025** | Service Catalog, Golden Path Templates, Previews & Self-Service Workflows (Phase 12) |
| **026** | Comprehensive Production Readiness Certification & Automated Platform Verification (Phase 13) |
| **043** | CI và CD là hai quyết định riêng: delivery rules, promotion, soak (§3.2b, §3.4) |
| **044** | SLSA provenance được kiểm tra ở nơi artifact chạy (§3.4 bước 7–8) |
| **045** | Lưu SBOM của từng artifact và rescan thứ đang chạy (§3.4 bước 10) |
| **046** | Release được kiểm chứng bằng metric netCI tự đọc; canary không nhận số liệu từ request (§3.4 bước 8) |
| **047** | Change freeze là khung thời gian được thực thi (§3.4 bước 9) |
| **048** | Commit status và PR comment đi qua outbox; sửa tuyên bố sai của ADR-019 (§3.4 bước 12) |
| **049** | Preview được deploy thật, hoặc không tồn tại (§3.4 bước 11) |
| **050** | Build được admit và supersede, không bị từ chối (§3.4 bước 4–5) |
| **051** | Path filter chỉ bỏ qua thứ biết chắc là không đổi (§3.4 bước 2) |
| **052** | Coding agent là principal thuộc loại riêng (§3.5) |
| **053** | Chi phí CI là thời gian run giữ CI, không phải tổng các stage |
| **054** | Build xác thực với git và registry; PR từ fork không bao giờ push |
| **055** | Một Jenkins controller tại một thời điểm; bản dự phòng được restore từ backup, không chạy song song |
| **056** | netCI quyết định phiên bản tool (syft, trivy, cosign, buildah) mà build dùng |
| **057** | Designer pipeline theo module, merge trong git (đã bị ADR-058 thay) |
| **058** | Pipeline dùng chung: một script cắt thành stage; amended 2026-09-29: không qua duyệt, chỉ bắt buộc build + publish |
| **059** | netCI quyết định controller Jenkins và mọi plugin; controller lệch không nhận build |

---

## 6. Cơ sở dữ liệu

### 6.1. Nguyên tắc quản lý schema

- **Migration là nguồn sự thật.** Mỗi file trong `backend/migrations/` chạy **đúng một
  lần**, theo thứ tự tên file, trong transaction riêng, và được ghi vào bảng
  `schema_migrations` kèm checksum.
- **Sửa migration đã chạy = bị từ chối.** Vì database sẽ không còn khớp lịch sử Git,
  nghĩa là không dựng lại được từ đầu.
- `backend/schema.sql` được **sinh tự động** bằng `python scripts/migrate.py --emit-schema`.
  Không sửa tay. Lệnh `--check-schema` báo lỗi nếu lệch.

### 6.2. 14 migration và lý do

| # | Tên | Thêm gì | Vì sao |
|---|---|---|---|
| 0001 | baseline | applications, pipeline_runs, deployments, audit_events, idempotency_records, systems, modules, release_versions, production_requests | Nền tảng |
| 0002 | delivery_events_and_concurrency | `delivery_events`, `pipeline_logs`, cột `version` | Nguồn cho DORA + compare-and-set |
| 0003 | pipeline_run_actor | `started_by` | Cần biết ai xin để so với ai duyệt |
| 0004 | application_ownership | `owner_team` | Role toàn cục là chưa đủ: cần biết được deploy app **nào** |
| 0005 | security_evidence | Bảng evidence | Policy phải đánh giá lại sau restart |
| 0006 | production_request_completion | Thêm state `succeeded` | Request phải theo dõi được kết quả cuối |
| 0007 | production_request_idempotency | `idempotency_key` | Chống tạo trùng qua restart |
| 0008 | system_unknown_status | Default `unknown` | System mới chưa có bằng chứng health, không được xanh |
| 0009 | callback_token_use | `callback_token_uses` | Chống phát lại token. **Chỉ lưu `jti`**, không lưu token |
| 0010 | deployment_leases_and_fencing | `deployment_leases`, `deployment_fencing_counters`, `pipeline_log_sequences`, state rollback mới | Chống deploy trùng + writer lỗi thời + đua log |
| 0011 | release_immutability_and_ci_reports | `version_ci_reports` | Release bất biến; CI report tách ra append-only |
| 0012 | scm_integrations_and_webhooks | `scm_integrations`, `scm_webhook_deliveries`, `console_url` | Webhook + chống replay + link Jenkins |
| 0013 | pipeline_lifecycle_and_stage_events | `pipeline_stages`, `retry_of` | Xem tiến độ từng stage; retry giữ lineage |
| 0014 | versioned_config_revisions_and_dcim | `module_config_revisions`, `server_health_records`, `config_revision_id` | **(đang làm)** Config có phiên bản, run "đóng băng" revision |

### 6.3. Vài chi tiết đáng chú ý

**Partial unique index cho lease** — chỉ áp dụng cho dòng chưa nhả:
```sql
CREATE UNIQUE INDEX deployment_leases_one_active_per_target
    ON deployment_leases (application_id, environment, target)
    WHERE released_at IS NULL;
```
Không có `WHERE` thì lease cũ đã nhả sẽ chặn lease mới.

**Bộ đếm fencing tách riêng** — `deployment_fencing_counters` tồn tại độc lập với
`deployment_leases`. Vì sao? Nếu lấy số từ lease, sau khi mọi lease nhả hết, lease mới
sẽ cấp lại số cũ — mà workflow lỗi thời có thể vẫn đang giữ số đó.

**Sequence log an toàn** — thay `SELECT max(sequence)+1` (đua nhau) bằng:
```sql
UPDATE pipeline_log_sequences SET next_sequence = next_sequence + N
 WHERE pipeline_run_id = ... RETURNING next_sequence - N;
```
`UPDATE` lấy row lock, nên mỗi số chỉ được cấp một lần.

---

## 7. Hạ tầng

### 7.1. Cần gì để chạy

| Thành phần | Bắt buộc? | Vai trò |
|---|---|---|
| **Python 3.12+** | ✅ | Chạy API |
| **PostgreSQL 16** | ✅ ngoài local | Nguồn sự thật |
| **Node 20+** | ✅ nếu cần UI | Build Portal |
| **Docker** | ⬜ | Chạy compose, registry, Jenkins |
| **Jenkins** | ⬜ | CI thật (`NETCI_CI_MODE=jenkins`) |
| **Temporal** | ⬜ | CD durable (`NETCI_CD_MODE=temporal`) |
| **syft / trivy / cosign** | ⬜ | SBOM, scan, ký |
| **kind / kubectl / helm** | ⬜ | Test runtime Kubernetes |
| **Ansible** | ⬜ | Deploy Docker/systemd |
| **DCIM** | ⬜ | Kho hạ tầng nội bộ |

### 7.2. Chạy nhanh nhất (local, không cần gì ngoài Python)

```bash
cd /home/deployer/netci-delivery-platform

# 1. Tạo môi trường Python
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

# 2. Chạy API — chế độ local, lưu trong RAM, KHÔNG có dữ liệu demo
export NETCI_ENVIRONMENT=local
export NETCI_ALLOWED_ORIGINS=http://localhost:5173
PYTHONPATH=backend .venv/bin/python -m uvicorn app.main:app --port 8000

# 3. Cửa sổ khác: chạy Portal
cd frontend && npm install && npm run dev     # http://localhost:5173
```

Mở `http://localhost:8000/healthz` sẽ thấy `"status": "ok"` và
`"deliveryPersistence": "in-memory"`.

> Giao diện sẽ **trống**. Đó là đúng — cài đặt mới không có dữ liệu giả. Muốn có dữ liệu
> mẫu thì thêm `export NETCI_DEMO_DATA=true`.

### 7.3. Chạy với PostgreSQL thật

```bash
# Khởi động PostgreSQL
docker run -d --name netci-pg --network host \
  -e POSTGRES_DB=netci -e POSTGRES_USER=netci \
  -e POSTGRES_PASSWORD=netci-local-only -e PGPORT=55432 \
  postgres:16.15-alpine3.24

export DATABASE_URL="postgresql://netci:netci-local-only@127.0.0.1:55432/netci"

# Chạy migration
.venv/bin/python scripts/migrate.py
.venv/bin/python scripts/migrate.py --check-schema   # kiểm tra không lệch

# Chạy API
PYTHONPATH=backend .venv/bin/python -m uvicorn app.main:app --port 8000
```

> **Lưu ý về Docker trong môi trường sandbox này**: `-p 55432:5432` không hoạt động
> (container không được cấp network endpoint). Phải dùng `--network host` + `PGPORT`.

### 7.4. Chạy toàn bộ stack bằng Docker Compose

`docker-compose.yml` định nghĩa 10 service:

| Service | Việc |
|---|---|
| `postgres` | Database |
| `registry` | Docker registry nội bộ (lưu image theo digest) |
| `minio` + `minio-init` | Lưu SBOM và evidence (giống S3) |
| `temporal` | Temporal server, lưu trạng thái workflow trong PostgreSQL (ADR-036) |
| `netci-api` | API |
| `portal` | UI (Nginx phục vụ bundle, proxy `/api`) |
| `temporal-worker` | Worker chạy deploy |
| `jenkins-a`, `jenkins-b` | Hai Jenkins controller |

```bash
cp .env.example .env      # rồi sửa .env
make compose-config       # kiểm tra cú pháp
make compose-up
```

### 7.5. Biến môi trường — nhóm theo mục đích

#### Bắt buộc ngoài local

```bash
NETCI_ENVIRONMENT=production
DATABASE_URL=postgresql://...            # hoặc DATABASE_URL_FILE
NETCI_WORKLOAD_TOKEN_KEYS=k1:<48+ ký tự ngẫu nhiên>
NETCI_ALLOWED_ORIGINS=https://portal.congty.vn
NETCI_AUTH_MODE=oidc                     # hoặc token
NETCI_REQUIRE_SECURITY_EVIDENCE=true
```

Nếu thiếu → **API từ chối khởi động**. Đó là cố ý (fail closed).

#### Xác thực người

```bash
NETCI_AUTH_MODE=none|token|oidc
NETCI_AUTH_TOKENS_FILE=/etc/netci/tokens.yaml    # mode=token, lưu HASH không lưu token
NETCI_OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0
NETCI_OIDC_AUDIENCE=api://netci
NETCI_OIDC_ROLES_CLAIM=roles
NETCI_OIDC_ROLE_MAP=netci-admins=platform-admin,release-managers=reviewer
NETCI_OIDC_TEAMS_CLAIM=groups
```

> `NETCI_AUTH_MODE=none` chỉ phục vụ loopback. Nếu request đến từ mạng, API trả 403 —
> để việc "quên cấu hình auth" không biến thành "công khai quyền duyệt production".

#### Xác thực máy

```bash
NETCI_WORKLOAD_TOKEN_KEYS=k1:<secret>,k0:<secret cũ>   # cái đầu để ký, tất cả để verify
NETCI_WORKLOAD_TOKEN_KEYS_FILE=/run/secrets/netci/workload-token-keys
NETCI_WORKLOAD_TOKEN_ISSUER=netci
NETCI_WORKLOAD_TOKEN_AUDIENCE=netci-api
# NETCI_ALLOW_LEGACY_PIPELINE_KEY=true   # chỉ trong thời gian chuyển đổi
```

Cách xoay khoá **không gián đoạn**: thêm khoá mới lên đầu → chờ hết một vòng đời token
→ bỏ khoá cũ.

#### Quản trị

```bash
NETCI_REQUIRE_APPLICATION_OWNER=false     # bật khi mọi app đã có chủ
NETCI_REQUIRE_SEPARATION_OF_DUTIES=true
NETCI_RATE_LIMIT=0                        # 0 = tắt
NETCI_TRUSTED_PROXY_HOPS=0                # số proxy tin cậy phía trước
```

#### Tích hợp ngoài

```bash
NETCI_CI_MODE=none|jenkins
NETCI_CD_MODE=none|temporal
NETCI_JENKINS_CONTROLLERS=A,B
NETCI_CALLBACK_URL=https://netci.congty.vn
NETCI_DCIM_BASE_URL=https://dcim.congty.vn/api/netci
NETCI_DCIM_API_TOKEN_FILE=/run/secrets/netci_dcim_token
NETCI_SIGNATURE_VERIFY_MODE=none|cosign
NETCI_COSIGN_PUBLIC_KEY_FILE=/run/secrets/netci/cosign.pub
NETCI_KUBECONFIG_DIR=/run/secrets/netci/kubeconfigs
NETCI_ANSIBLE_PRIVATE_KEY_FILE=/run/secrets/netci/ssh/id_ed25519
NETCI_DEPLOYMENT_LEASE_TTL_SECONDS=900
```

> Mọi secret đều có biến `*_FILE`. Nên dùng bản `_FILE` để secret nằm trong file mount
> (Docker/Kubernetes secret) thay vì trong biến môi trường — biến môi trường lộ ra khi
> ai đó đọc `/proc/<pid>/environ` hoặc khi process crash dump.

---

## 8. Tiến độ

### 8.1. Kế hoạch tổng thể: 13 phase, 3 nhóm

| Nhóm | Phase | Mục đích |
|---|---|---|
| **P0** | 1–5 | Sửa lỗi đúng đắn & bảo mật — **chặn** việc lên production |
| **P1** | 6–9 | Vận hành được lâu dài |
| **P2** | 10–13 | IDP trưởng thành |

### 8.2. Trạng thái hiện tại

| Phase | Nội dung | Trạng thái | Commit |
|---|---|---|---|
| **1** | PostgreSQL canonical + onboarding atomic | ✅ Xong | `6384706` |
| **2** | Workload identity + ranh giới build input | ✅ Xong | `4b33d5c` |
| **3** | Lease, fencing, log sequence an toàn | ✅ Xong | `89c4690` |
| **4** | Release bất biến, backup, xoá dữ liệu giả | ✅ Xong | `69871fe` |
| **5** | Readiness thật + acceptance harness | ✅ Xong | `1de68e3` |
| **6** | SCM webhook, private repo, commit status | ✅ Xong | `0b66569` |
| **7** | Pipeline lifecycle, reconciler, stage event | ✅ Xong | `6b38fc3` |
| **8** | Config có phiên bản + DCIM lifecycle | ✅ Xong | `5a26d03` |
| **9** | Observability, notification, pagination, DR | ✅ Xong | `f952f77` |
| **10** | Multi-module DAG + progressive delivery | ✅ Xong | `57c2243` |
| **11** | Policy engine, security exception, break-glass, quota, admission | ✅ Xong | *(sẵn sàng commit)* |
| **12** | Catalog, template, preview env, self-service | 🔨 **Tiếp theo** | |
| **13** | Chứng nhận production cuối cùng | ⬜ Chưa | |

**Đã xong: 11/13 phase** — toàn bộ **P0 (phase 1–5)**, **P1 (phase 6–9)**, và **P2.1, P2.2 (phase 10–11)** đã hoàn thành 100%.

**Trạng thái test hiện tại** (chạy với PostgreSQL 16 thật):

| Bộ test | Kết quả |
|---|---|
| Backend + contract | **423 pass, 50 skipped, 0 fail** |
| Durability PostgreSQL (17 migrations) | **24 pass, 0 fail** |
| Frontend unit (vitest) | **22 pass / 7 file** |
| Frontend build (`tsc -b && vite build`) | ✅ pass |

### 8.3. Từng phase đã làm được gì

<details>
<summary><b>Phase 1 — PostgreSQL canonical</b></summary>

- Xoá bỏ `load()` nạp toàn bộ dữ liệu vào RAM lúc khởi động.
- Tạo seam `app/store` với interface duy nhất `transaction()`.
- Onboarding module: application + module + audit + idempotency trong **một** transaction.
- Đổi tên `PortalReadModel` → `PortalService`.
- **Bằng chứng thật**: chạy 2 replica uvicorn trên cùng 1 PostgreSQL — tạo system ở A,
  đọc thấy ngay ở B không cần restart; retry onboarding gửi tới B trả về đúng
  applicationId của A; cùng một callback gửi đồng thời tới 2 replica → một cái 202,
  một cái 409, version tăng đúng 1.
</details>

<details>
<summary><b>Phase 2 — Danh tính máy móc</b></summary>

- Token callback có chữ ký, ngắn hạn, gắn với đúng một run/deployment.
- Bảng `WORKLOAD_SCOPES`: token Jenkins **không thể** được cấp quyền báo deployment.
- Token có scope terminal là **dùng một lần** (chống phát lại).
- Khoá chung `NETCI_PIPELINE_API_KEY` bị từ chối ngoài local trừ khi khai báo cửa sổ di trú.
- `build_inputs.py`: allowlist build input, chặn ~60 khoá điều khiển deployment.
- `commitSha` phải là hex; `branch` phải là refname hợp lệ (chặn `..`, `;`, `$()`).
- `/applications/{id}/pipeline-runs` giới hạn cho platform-admin/máy.
</details>

<details>
<summary><b>Phase 3 — Lease & fencing</b></summary>

- `deployment_leases` + partial unique index: 2 deploy đồng thời cùng target → đúng 1 thắng.
- Fencing token tăng đơn điệu; workflow lỗi thời bị từ chối bằng 409 `STALE_WORKFLOW`.
- Heartbeat gia hạn lease; lease hết hạn được thu hồi ngay khi có người cần.
- State mới: `rollback_in_progress`, `rollback_failed`, `cancelled`.
- Rollback thành hai pha — không ghi "đã khôi phục" trước khi thật sự khôi phục.
- Sequence log an toàn: 8 luồng × 25 dòng ghi đồng thời, không trùng, giữ đúng thứ tự.
</details>

<details>
<summary><b>Phase 4 — Bất biến & backup</b></summary>

- `release_versions` bất biến; đăng ký lại cùng payload = OK, khác payload = 409.
- `version_ci_reports` tách riêng, append-only.
- `netci_backup.py` verify **19 bảng critical**, tự khám phá bảng qua `information_schema`.
- Xoá thông báo giả "Backend API v2.4.1…" khỏi `PortalShell.tsx`.
</details>

<details>
<summary><b>Phase 5 — Readiness thật</b></summary>

- Tách `/livez` (tiến trình sống) / `/readyz` (nhận việc được) / `/operator/health` (chi tiết, cần auth).
- Circuit breaker + timeout cho từng dependency.
- Dependency chưa cấu hình báo `not_configured`, **không** giả xanh.
- `production_acceptance_harness.py`: 9 gate với hạ tầng thật, ghi evidence JSON/JUnit.
</details>

<details>
<summary><b>Phase 6 — SCM</b></summary>

- `ScmProvider` port + adapter GitHub (HMAC-SHA256) / GitLab (token so sánh hằng thời gian) / Mock.
- Dedup delivery ID bằng unique constraint (atomic giữa các replica).
- Giới hạn payload 1MB.
- Map repository → application **phía server** (không tin field trong payload).
- Báo commit status ngược: pending/running/success/failure/cancelled.
- Credential private repo lưu server-side, chỉ truyền `credentialsId` cho Jenkins.
- Persist `console_url` để nút "Open Jenkins" có link thật.
</details>

<details>
<summary><b>Phase 7 — Lifecycle</b></summary>

- `POST /pipeline-runs/{id}/cancel` → gọi Jenkins abort thật.
- `POST /deployments/{id}/cancel` → gọi Temporal cancel thật + nhả lease.
- `POST /pipeline-runs/{id}/retry` → tạo run **mới** với `retry_of`, run cũ bất biến.
- `pipeline_stages`: từng stage có attempt, thời gian, trạng thái, duration.
- `reconciler.py`: poll Jenkins/Temporal, phát hiện callback mất, sửa và audit.
</details>

<details>
<summary><b>Phase 8 — Versioned Config & DCIM Lifecycle</b></summary>

- Bảng `module_config_revisions` bất biến (migration 0014), tracking pipeline & deploy config.
- Con trỏ active dùng Compare-And-Set (CAS `config_version`) chống race condition (409 CONCURRENT_MODIFICATION).
- Pin `config_revision_id` vào pipeline run và deployment lúc trigger/dispatch.
- Phân quyền thay đổi: prod yêu cầu duyệt (`requiresApproval`), cấm tự duyệt (403 SEPARATION_OF_DUTIES).
- Diff viewer giữa các revision và 1-click forward rollback (tạo revision mới kế thừa settings cũ).
- DCIM target revalidation fail-closed: kiểm tra trạng thái máy chủ trước khi deploy (422 DCIM_TARGET_UNAVAILABLE).
- Drift detection giữa active config mong muốn và trạng thái deploy/DCIM thực tế.
</details>

<details>
<summary><b>Phase 9 — Observability, Outbox, Connection Pool, Retention & DR Drill</b></summary>

- PostgreSQL Connection Pooling (`PostgresConnectionPool`): quản lý kết nối an toàn đa luồng, timeout khi cạn pool, ping liveness `SELECT 1`.
- Structured JSON logging: format chuẩn JSON kèm correlation ID, service name, tự động redact token nhạy cảm (`[REDACTED]`).
- Metrics Prometheus: exposition chuẩn tại `/metrics` (request rates, latency histogram, pool stats, outbox queue depths).
- Transactional Outbox Pattern: bảng `notifications` (migration 0015), ghi notification trong cùng atomic transaction với business data.
- Background Outbox Worker: gửi notification kèm exponential backoff và routing sang dead-letter queue.
- Phân trang cursor-based cho mọi danh sách lớn (`/notifications`, `/pipeline-runs`, `/deployments`, `/audit-events`).
- Dọn dẹp dữ liệu theo TTL (retention manager & script `netci_retention_purge.py`).
- Sao lưu mã hóa AES-256-GCM và kịch bản DR Drill tự động (`scripts/netci_dr_drill.py`) kiểm toán phục hồi vào database scratch sạch, đối chiếu checksum, khóa ngoại và bằng chứng JSON.
</details>

<details>
<summary><b>Phase 10 — Multi-Module DAG, SAGA Orchestration & Progressive Delivery</b></summary>

- Thuật toán Topological Sort (Kahn's DAG) và phân tầng đợt triển khai (`compute_dag_waves`): nhóm các module độc lập vào Wave 1, giải quyết phụ thuộc tuần tự, phát hiện chu trình 422 `CYCLIC_DEPENDENCY`.
- Migration cơ sở dữ liệu `0016_multi_module_dag_and_progressive_delivery.sql`: bổ sung `release_plan`, `strategy`, `strategy_config` vào `production_requests`; `dependencies`, `status`, `deployment_id` vào `production_request_modules`; `traffic_weight`, `active_color`, `canary_step` vào `deployments`.
- Điều phối viên SAGA Release Plan (`ReleasePlanCoordinator`): tự động kích hoạt đợt kế tiếp khi các module đợt trước vượt qua kiểm tra sức khỏe; kích hoạt SAGA reverse rollback khi có sự cố, đưa hệ thống về trạng thái an toàn nhất quán.
- Động cơ Progressive Delivery (`TrafficRoutingAdapter`, `CanaryAnalyzer`): điều tiết lưu lượng Canary theo từng bước (10% -> 25% -> 50% -> 100%) và đánh giá ngưỡng lỗi/độ trễ (SLO), hỗ trợ Blue/Green cutover tức thì.
- Giao diện Release Portal (`ProductionRequestsPage.tsx`): hỗ trợ chọn nhiều module và liên kết phụ thuộc, chọn chiến lược triển khai (Rolling DAG, Canary, Blue/Green), hiển thị trực quan các Wave và bảng điều khiển Canary thời gian thực.
</details>

<details>
<summary><b>Phase 11 — Enterprise Policy Engine, Security Waivers, Break-Glass & Kubernetes Admission</b></summary>

- Migration cơ sở dữ liệu `0017_policy_engine_governance_and_admission.sql`: bảng lưu trữ bền vững quyết định chính sách `policy_decisions`, ngoại lệ bảo mật CVE `security_exceptions`, quy trình phá kính khẩn cấp `break_glass_requests`, và hạn ngạch tài nguyên `resource_quotas`.
- Động cơ chính sách thống nhất (`PolicyEngine` & `BuiltinPolicyEngine`): đánh giá và lưu vết kiểm toán vĩnh viễn cho kiểm định artifact, phê duyệt sản xuất và cổng triển khai.
- Máy tính điểm rủi ro minh bạch (`RiskCalculator`): tính điểm từ 0 đến 100 dựa trên môi trường đích, quy mô wave đa module, độ phủ kiểm thử tự động, số lượng ngoại lệ CVE hoạt động, chiến lược rollback và cờ phá kính.
- Kiểm soát hạn ngạch tài nguyên (`QuotaEnforcer`): phân cấp hạn ngạch (application -> team -> global) để giới hạn số pipeline và deployment đồng thời, chống cạn kiệt tài nguyên.
- Cơ chế phá kính hai người (`BreakGlassService`): bắt buộc dual-control (`requested_by != approved_by`), cấm tự phê duyệt (403 `SEPARATION_OF_DUTIES`), giới hạn thời gian sống (TTL tối đa 4h) và gắn với mã sự cố (incident ticket).
- Bộ điều khiển tiếp nhận động Kubernetes (`AdmissionController` & `/admission/validate`): webhook tiếp nhận AdmissionReview v1 từ cụm Kubernetes, từ chối tag có thể thay đổi (mutable tag như `:latest`) trên môi trường sản xuất, xác minh digest sha256 với bằng chứng SBOM, Trivy scan và chữ ký Cosign, cho phép tiếp nhận khẩn cấp khi có break-glass hợp lệ.
- Giao diện Release Portal (`ProductionRequestsPage.tsx`): hiển thị thẻ Enterprise Governance & Policy Verification với huy hiệu điểm rủi ro thời gian thực, trạng thái dual-control và chỉ báo phá kính.
</details>


<details>
<summary><b>Phase 12 — Service Catalog, Golden Path Templates, Ephemeral Preview Environments & Governed Self-Service</b></summary>

- Migration cơ sở dữ liệu `0018_service_catalog_and_self_service.sql`: các bảng quản lý danh mục dịch vụ `catalog_services`, quan hệ phụ thuộc dịch vụ `catalog_service_dependencies`, mẫu pipeline chuẩn `catalog_templates`, môi trường preview tạm thời `preview_environments`, và yêu cầu tài nguyên tự phục vụ `resource_requests`.
- Danh mục dịch vụ và phát hiện chu trình (`CatalogServiceManager`): phân cấp dịch vụ (`tier-1`, `tier-2`, `tier-3`), trạng thái vòng đời (`active`, `deprecated`, `decommissioned`), và thuật toán duyệt đồ thị theo chiều sâu (DFS cycle detection) để ngăn chặn chu trình phụ thuộc microservice.
- Mẫu Golden Path tham số hóa (`PipelineTemplateEngine`): quản lý phiên bản Semantic Versioning (`major.minor.patch`), xác thực JSON Schema cho tham số đầu vào, nội suy template `${parameters.KEY}`, và nạp sẵn các template sản xuất (`fastapi-service`, `go-microservice`, `react-spa`).
- Môi trường Preview tạm thời theo Pull Request (`PreviewEnvironmentManager`): tự động tạo namespace RFC 1123, URL truy cập nội bộ, áp dụng giới hạn thời gian sống (TTL tối thiểu 1h, mặc định 24h, tối đa 72h), và dọn dẹp môi trường hết hạn.
- Tự phục vụ tài nguyên hạ tầng có quản trị (`SelfServiceResourceManager`): hỗ trợ đăng ký tài nguyên (PostgreSQL, Redis, S3 bucket), áp dụng nghiêm ngặt nguyên tắc dual-control trên staging/production (`approved_by != requested_by`), và hợp đồng nhà cung cấp fail-closed (`provider_not_configured`) khi thiếu driver bên ngoài.
- Cổng nhà phát triển (`CatalogPage.tsx` & `netciClient.ts`): giao diện quản lý danh mục dịch vụ, khởi tạo 1-click từ Golden Path template, và theo dõi môi trường preview cùng yêu cầu tài nguyên.
</details>

<details>
<summary><b>Phase 13 — Platform Integrity, Clean-Room Standards & Automated Production Readiness Certification</b></summary>

- Bộ kiểm toán chứng nhận tự động (`scripts/production_readiness_audit.py`): kiểm thử toàn diện 28 tiêu chí bất biến nghiêm ngặt qua 13 phase, đạt tỷ lệ vượt qua 100% (28/28 checks PASS).
- Quy chuẩn Clean-Room: loại bỏ hoàn toàn mock/fake runtime trong các luồng vận hành sản xuất mặc định, đảm bảo nguyên tắc trung thực và fail-closed.
- Hợp đồng API chuẩn mực: đồng bộ 100% giữa OpenAPI 3.1.0 (`api/openapi.yaml`) và implementation REST API.
- Bộ lưu trữ kiến trúc đầy đủ: 26 tài liệu quyết định kiến trúc (ADR-001 đến ADR-026) ghi lại toàn bộ quyết định kỹ thuật từ khởi đầu đến trạng thái sẵn sàng sản xuất.
- Bằng chứng kiểm toán xuất ra dạng máy đọc (`evidence/production_readiness_audit.json`) phục vụ kiểm toán compliance tự động.
</details>

### 8.4. Trạng thái các phase

| Phase | Nội dung chính | Trạng thái |
|---|---|---|
| **10** | ProductionRequest nhiều module, release plan DAG, SAGA compensation, canary/blue-green, traffic adapter | **Đã hoàn thành** (ADR-023) |
| **11** | Policy engine bằng Python (`backend/app/policy/`, không phải OPA; policy bundle có chữ ký chưa làm), risk-based approval, security exception gắn CVE+digest+expiry, break-glass dual control, quota, admission gate trong API | **Có mã và kiểm thử** (ADR-024); OPA/bundle ký là hướng mở |
| **12** | Owning team first-class, service lifecycle, dependency graph, pipeline template có version, preview environment theo PR (namespace + TTL + DNS/TLS thật), ResourceRequest self-service | **Đã hoàn thành** (ADR-025) |
| **13** | Audit toàn bộ route, quét runtime tìm mock/fake còn sót, chạy mọi loại test, chạy harness 9 cổng trên hạ tầng thật, viết `LIVE-READINESS.md` | **Đã chạy thật ngày 2026-09-15** (ADR-026, ADR-028) — xem `docs/LIVE-READINESS.md`; không dùng chữ "certified" |

### 8.5. Kết luận trạng thái nền tảng

- ✅ **Toàn bộ 13 Phase kiến trúc đã hoàn thiện mã nguồn và kiểm thử tự động.**
- ✅ `scripts/production_readiness_audit.py` (28 self-check trong tiến trình, không chạm hạ tầng ngoài) đạt 28/28. **Đây không phải chứng nhận live**: bằng chứng chạy thật nằm ở `docs/LIVE-READINESS.md` và `evidence/production_acceptance_*.json` (harness 9 cổng chạy với Keycloak, Jenkins, Temporal, NetBox, registry, cosign thật).
- ✅ **Bộ kiểm thử tự động (pytest, openapi contract test, frontend build) hoạt động ổn định và vượt qua 100%.**

---

## 9. Cách tự kiểm chứng

### 9.1. Chạy test

```bash
cd /home/deployer/netci-delivery-platform

# Nhanh — không cần PostgreSQL, test durability bị skip
.venv/bin/python -m pytest backend/tests tests/contract -q

# Đầy đủ (~5-6 phút) — cần PostgreSQL đã migrate
export NETCI_TEST_DATABASE_URL="postgresql://netci:netci-local-only@127.0.0.1:55432/netci"
.venv/bin/python -m pytest backend/tests tests/contract -q

# Frontend
cd frontend && npm test && npm run build
```

> Nếu thấy `s` trong output nghĩa là **skipped**. Test PostgreSQL tự skip khi
> `NETCI_TEST_DATABASE_URL` chưa đặt — đó là cố ý, để không giả vờ đã kiểm chứng.

### 9.2. Kiểm tra schema không lệch

```bash
.venv/bin/python scripts/migrate.py --check-schema
.venv/bin/python scripts/migrate.py --status
```

### 9.3. Kiểm tra sức khoẻ khi API đang chạy

```bash
curl -s localhost:8000/livez           # tiến trình sống?
curl -s localhost:8000/readyz | jq     # nhận việc được? (503 nếu không)
curl -s localhost:8000/healthz | jq
```

### 9.4. Chạy các gate nghiệm thu (cần Docker/Linux)

```bash
make doctor              # kiểm tra môi trường trước
make security-test       # artifact ký/scan — 1 cho phép, 3 từ chối
make e2e-container       # build → sign → deploy → health → rollback
make e2e-kubernetes      # cùng digest lên kind
make e2e-systemd         # binary Go → unit systemd
make dora-dashboard      # tính lại DORA độc lập và đối chiếu
make test-durability     # PostgreSQL thật
make backup-verify       # backup + restore + verify
```

Mỗi gate ghi `evidence/<gate>.json` gồm: command, timestamp, exit code, output, từng
assertion kèm verdict.

### 9.5. Tự kiểm chứng "không còn dữ liệu giả"

```bash
# Không đặt NETCI_DEMO_DATA → giao diện phải trống
grep -rn "mock\|fixture\|fake\|sample" backend/app/ --include=*.py | grep -v test
```

---

## 10. Rủi ro và những chỗ chưa xong

### 10.1. Đã biết và đã ghi nhận

#### 🔴 Cần sửa trước tiên — một test đang đỏ trên `main`

**`tests/contract/test_state_machine.py::test_pipeline_does_not_skip_from_queued_to_success`
đang FAIL.**

Nguyên nhân: commit Phase 7 (`6b38fc3`) đã đổi bảng chuyển trạng thái:

```python
# trước Phase 7 (commit 0b66569)
PipelineStatus.QUEUED: frozenset({RUNNING, CANCELLED})

# sau Phase 7 (hiện tại)
PipelineStatus.QUEUED: frozenset({RUNNING, SUCCEEDED, FAILED, CANCELLED})
```

Nghĩa là hệ thống giờ **cho phép** một pipeline nhảy thẳng từ `queued` sang `succeeded`
mà chưa bao giờ chạy. Đó đúng là loại "thành công giả" mà nguyên tắc §4.6 cấm: báo
build thành công cho một build chưa từng chạy.

Có thể ý định là để reconciler đóng các run bị kẹt. Nếu vậy thì đúng đắn hơn là
`QUEUED → FAILED` hoặc `QUEUED → CANCELLED`, chứ không phải `SUCCEEDED`.

**Việc cần làm** — quyết định invariant này còn đúng không:
- Nếu **còn đúng** → bỏ `SUCCEEDED` khỏi tập chuyển từ `QUEUED` trong
  `domain/models.py`, rồi sửa chỗ nào trong reconciler đang dựa vào nó.
- Nếu **không còn đúng** → sửa test **và viết ADR giải thích vì sao**, chứ không im lặng.

> Bài học vận hành: `.github/workflows/ci.yml` hiện chỉ chạy một phần test. Nên chạy
> `pytest backend/tests tests/contract` **đầy đủ** trước mỗi commit phase.

#### Các vấn đề đã biết khác

| Vấn đề | Ảnh hưởng | Thuộc phase nào |
|---|---|---|
| `callback_token_uses` chưa có job dọn dòng hết hạn | Bảng phình dần | Phase 9 (retention) |
| Chưa có revoke token trước hạn | Token chỉ hết hiệu lực khi `exp` | Phase 11 |
| Chưa có connection pool | Mỗi request 1 kết nối; cạn kết nối → 503 | Phase 9 |
| `Idempotency-Key` là global theo scope, không theo application | Hai app dùng trùng khoá → 409 (an toàn, nhưng khó hiểu) | Ghi nhận, không sửa để giữ tương thích |
| Chưa từng chạy với Jenkins/Temporal/DCIM thật trong môi trường này | **Không được tuyên bố "live"** | Phase 13 |

### 10.2. Quyết định còn treo (cần người quyết, không phải code)

1. **Workload identity**: hiện dùng HMAC token tự ký. Mạnh hơn là **mTLS** hoặc
   **workload OIDC** (SPIFFE / Kubernetes projected token). Đã loại ở Phase 2 vì hạ tầng
   tham chiếu chạy agent như process thường, không có attestor. Seam chỉ là một hàm
   (`_workload_principal`) nên đổi được mà không sửa endpoint nào.

2. **Progressive delivery** (Phase 10): cần chọn cơ chế chuyển traffic — service mesh,
   Ingress annotation, hay Deployment strategy thuần.

3. **Policy engine** (Phase 11): OPA/Rego hay engine khác.

4. **Self-service resource provider & Service Catalog** (Phase 12 - Đã hoàn thành):
   - Đã áp dụng Migration 0018 và domain logic trong `backend/app/catalog/`.
   - Contract provider fail-closed vào trạng thái `provider_not_configured` khi chưa có driver bên ngoài, tuyệt đối không giả thành công.
   - Dual-control governance bắt buộc trên staging/production (`approved_by != requested_by`).
   - Service Catalog phát hiện chu trình (DFS cycle detection) và Golden Path templates hỗ trợ 1-click instantiation.
   - Preview environments tự động tính toán TTL và đối soát hết hạn.

### 10.3. Nguyên tắc khi làm tiếp

Từ chính prompt yêu cầu, đáng nhắc lại:

- Không mock/fixture/demo trong runtime mặc định.
- Production **fail closed** khi thiếu integration hoặc secret bắt buộc.
- PostgreSQL canonical; RAM không quyết định trạng thái bền vững.
- Mọi transition quan trọng: atomic + idempotent + audit + concurrency-safe.
- Artifact bất biến, định danh bằng digest.
- Không log token/secret/private key/bearer header.
- Migration là nguồn schema; `schema.sql` sinh bằng script.
- OpenAPI cập nhật **trước hoặc cùng lúc** với code.
- **Không tự tuyên bố live** nếu chưa có bằng chứng chạy với hạ tầng thật.

---

## Phụ lục A — Bản đồ đọc code theo mục đích

| Muốn hiểu… | Đọc file này | Bắt đầu từ |
|---|---|---|
| Luật nghiệp vụ | `domain/models.py` | `PIPELINE_TRANSITIONS` |
| Cách một pipeline chạy | `delivery.py` | `start_pipeline` → `record_ci_result` |
| Cách deploy được bảo vệ | `delivery.py` | `_acquire_lease`, `_reject_stale_writer` |
| Cách nói chuyện với DB | `store/postgres.py` | `PostgresDatabase.transaction` |
| Cách API lộ ra ngoài | `main.py` | dòng ~100 (composition root) |
| Cách xác thực người | `auth.py` | `build_authenticator` |
| Cách xác thực máy | `workload_identity.py` | `mint` và `verify` |
| Ranh giới input | `build_inputs.py` | `DEPLOYMENT_CONTROLLED_KEYS` |
| Portal hiển thị gì | `portal.py` | `PortalService._module` |

## Phụ lục B — Lỗi thường gặp

| Triệu chứng | Nguyên nhân | Cách sửa |
|---|---|---|
| `DATABASE_URL is required outside local mode` | `NETCI_ENVIRONMENT` ≠ local mà chưa có DB | Đặt `DATABASE_URL` hoặc `NETCI_ENVIRONMENT=local` |
| `NETCI_WORKLOAD_TOKEN_KEYS is required` | Chưa cấu hình khoá ký | Sinh: `python -c "from backend.app.workload_identity import generate_key; print(generate_key('k1'))"` |
| 403 `AUTH_NOT_CONFIGURED` | `NETCI_AUTH_MODE=none` nhưng gọi từ mạng | Đặt `NETCI_AUTH_MODE=token` hoặc `oidc` |
| 409 `CONCURRENT_MODIFICATION` | Hai writer cùng aggregate | Đọc lại rồi thử lại — đây là control hoạt động đúng |
| 409 `DEPLOYMENT_TARGET_BUSY` | Đã có deployment giữ target | Chờ, hoặc `POST /deployment-leases/recover` nếu worker đã chết |
| 409 `STALE_WORKFLOW` | Workflow lỗi thời báo cáo | Đúng như thiết kế — workflow đó đã bị thay thế |
| 422 `DEPLOYMENT_PARAMETER_NOT_ACCEPTED` | Gửi khoá server tự quyết | Bỏ khoá đó; cấu hình target lúc đăng ký module |
| 503 `PERSISTENCE_UNAVAILABLE` | Không tới được PostgreSQL | Kiểm tra `DATABASE_URL`, DB, giới hạn kết nối |
| `schema.sql is stale` | Sửa migration mà chưa sinh lại | `python scripts/migrate.py --emit-schema` |

---

*Tài liệu này mô tả trạng thái tại commit `6b38fc3`; riêng §2.5, §3.4, §3.5, §3.6 mô tả
commit `9f114a1` (ADR-043 → ADR-052). Khi code đổi, hãy cập nhật lại phần §8 (Tiến độ),
§10 (Rủi ro) và §3.6 (Đã kiểm chứng live tới đâu).*
