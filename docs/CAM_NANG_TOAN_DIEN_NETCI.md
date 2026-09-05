# netCI Delivery Platform — Hiểu Toàn Bộ Project Từ Đầu Đến Cuối

> **Tài liệu này viết cho người chưa nắm chắc kiến trúc hoặc tự nhận mình là "vibecoder"**, không giả định bạn đã biết trước bất kỳ điều gì. Mỗi thuật ngữ khó đều được giải thích ngay tại chỗ dùng nó lần đầu bằng cả định nghĩa kỹ thuật lẫn ví dụ đời thường.
>
> Đọc theo thứ tự từng phần sẽ giúp bạn thấu suốt bức tranh tổng thể một cách tự nhiên nhất.
>
> **Phiên bản hiện tại**: Cập nhật tại commit hoàn thiện **Phase 13** — Toàn bộ 13/13 Phase (P0, P1, P2), 26 ADR, 18 SQL Migrations, 34 bảng cơ sở dữ liệu và bài kiểm định 28/28 tiêu chuẩn đạt chứng chỉ **CERTIFIED**.

---

## MỤC LỤC

1. [Project này là cái gì, giải quyết vấn đề gì](#1-project-này-là-cái-gì-giải-quyết-vấn-đề-gì)
   - 1.1. Tóm tắt một câu
   - 1.2. Nỗi đau thực tế trong doanh nghiệp khi không có netCI
   - 1.3. Mục tiêu tối thượng của netCI (6 tiêu chuẩn vàng)
2. [Từ điển thuật ngữ toàn tập (Dễ hiểu cho Vibecoder)](#2-từ-điển-thuật-ngữ-toàn-tập)
   - 2.1. Thuật ngữ CI/CD & Delivery
   - 2.2. Thuật ngữ bảo mật chuỗi cung ứng (Supply Chain Security)
   - 2.3. Thuật ngữ kiến trúc phần mềm cốt lõi (Seam, CAS, Leases, Fencing, Outbox, DAG...)
   - 2.4. Thuật ngữ quản trị & Vận hành (OPA, Rego, Break-Glass, Canary, DORA, DCIM...)
3. [Bức tranh tổng thể — Các mảnh ghép & Luồng vận hành](#3-bức-tranh-tổng-thể)
   - 3.1. Sơ đồ các hệ thống tham gia
   - 3.2. Vì sao tách rời Jenkins (CI) và Temporal (CD)?
   - 3.3. Luồng một lần deploy đơn lẻ (Single-Module Deployment Lifecycle)
   - 3.4. Luồng phát hành đa module có thứ tự (Multi-Module DAG Release Plan)
   - 3.5. Luồng xử lý sự cố khẩn cấp (Dual-Control Break-Glass Flow)
4. [Tại sao lại là kiến trúc này? (7 nguyên tắc bất biến)](#4-tại-sao-lại-là-kiến-trúc-này)
   - 4.1. PostgreSQL là nguồn chân lý duy nhất (Canonical State)
   - 4.2. Mọi chuyển dịch trạng thái phải Atomic, Idempotent và Concurrency-safe
   - 4.3. Máy móc phải có danh tính riêng (Workload Identity)
   - 4.4. Trình duyệt không được quyết định nơi deploy (Input Trust Boundaries)
   - 4.5. Không bao giờ hiển thị màu xanh giả (Zero False Greens & Fail-Closed)
   - 4.6. Chống xung đột triển khai bằng Leases và Fencing Tokens
   - 4.7. Kiến trúc Deep Module và Seam rõ ràng
5. [Đi qua TỪNG FILE trong dự án — Vai trò & Lý do tồn tại](#5-đi-qua-từng-file-trong-dự-án)
   - 5.1. Backend Core (`backend/app/`)
   - 5.2. Backend Adapters (`backend/app/adapters/`)
   - 5.3. Backend Store (`backend/app/store/`)
   - 5.4. Backend Policy & Governance (`backend/app/policy/`)
   - 5.5. Backend Service Catalog & Previews (`backend/app/catalog/`)
   - 5.6. Backend Workflows & Projections (`backend/app/workflows/`, `backend/app/projections/`)
   - 5.7. Frontend Portal (`frontend/src/`)
   - 5.8. Bộ kịch bản vận hành & Kiểm định (`scripts/`)
   - 5.9. Bộ hồ sơ quyết định kiến trúc (`docs/decisions/` - 26 ADRs)
6. [Cơ sở dữ liệu — Toàn bộ 34 bảng qua 18 Migration](#6-cơ-sở-dữ-liệu)
   - 6.1. Bảng tóm tắt 18 Migration Scripts
   - 6.2. Danh bạ 34 bảng dữ liệu và lý do tồn tại
   - 6.3. Những kỹ thuật SQL chuyên sâu trong netCI (Partial Index, Atomic Sequences...)
7. [Hướng dẫn thiết lập hạ tầng & Cách chạy dự án](#7-hướng-dẫn-thiết-lập-hạ-tầng--cách-chạy-dự-án)
   - 7.1. Cần cài đặt những gì?
   - 7.2. Hướng dẫn chạy nhanh cục bộ (Local Development)
   - 7.3. Hướng dẫn chạy chuẩn với Docker & PostgreSQL 16
   - 7.4. Hướng dẫn chạy toàn bộ hệ sinh thái với Docker Compose
   - 7.5. Bảng giải thích toàn bộ biến môi trường (`.env`)
8. [Tiến độ & Kết quả kiểm chứng thực tế](#8-tiến-độ--kết-quả-kiểm-chứng-thực-tế)
   - 8.1. Bảng tổng kết tiến độ 13/13 Phase
   - 8.2. Các con số kiểm thử đo được thực tế
   - 8.3. Kết quả chứng nhận Production Readiness Audit (28/28 checks)
9. [Cách tự kiểm chứng toàn bộ hệ thống](#9-cách-tự-kiểm-chứng-toàn-bộ-hệ-thống)
10. [Rủi ro thực tế & Những việc cần làm khi đưa vào doanh nghiệp](#10-rủi-ro-thực-tế--những-việc-cần-làm-khi-đưa-vào-doanh-nghiệp)

---

## 1. PROJECT NÀY LÀ CÁI GÌ, GIẢI QUYẾT VẤN ĐỀ GÌ

### 1.1. Tóm tắt một câu

**netCI là một Internal Developer Platform (IDP) kiêm Hệ Thống Phân Phối Phần Mềm Tự Động (Continuous Delivery Platform)** — hoạt động như một "trung tâm điều khiển tối cao" cho phép lập trình viên tự đưa code lên môi trường chạy thật (Kubernetes, Docker, Linux VM) chỉ bằng vài cú click, mà vẫn đảm bảo 100% tuân thủ bảo mật, kiểm toán, chống xung đột và có khả năng tự động khôi phục (rollback) khi có sự cố.

### 1.2. Nỗi đau thực tế trong doanh nghiệp khi không có netCI

Trong hầu hết các doanh nghiệp công nghệ chưa xây dựng IDP chuẩn mực, quy trình đưa một dòng code từ máy dev lên máy chủ production thường là một "cơn ác mộng":

1. **Phụ thuộc con người & Tắc nghẽn ticket**: Dev viết code xong phải mở ticket Jira xin đội DevOps/Sysadmin cấu hình giùm. DevOps bận thì ticket ngâm 3 ngày.
2. **Xung đột ghi đè code (Race Condition)**: Lập trình viên A deploy tính năng thanh toán, cùng lúc đó lập trình viên B deploy bản vá lỗi giao diện vào cùng máy chủ. Bản build của B vô tình ghi đè lên A. Production sập nhưng không ai biết code của ai đang thực sự chạy.
3. **Ảo tưởng "Màu xanh giả" (False Green)**: Jenkins báo "Build SUCCESS", nhưng thực tế máy chủ database bên dưới đã sập, hoặc container chưa bao giờ vượt qua được bài kiểm tra sức khỏe (health check). Hệ thống hiển thị màu xanh khiến mọi người an tâm đi ngủ, cho đến khi khách hàng gọi hotline phàn nàn.
4. **Lỗ hổng bảo mật chuỗi cung ứng**: Lập trình viên vô tình tải một thư viện mã nguồn mở bị cài cắm mã độc (CVE), hoặc deploy một container image sử dụng tag trôi nổi `:latest` (hôm nay là phiên bản này, ngày mai ai đó đẩy bản khác đè lên).
5. **Rollback hoảng loạn**: Khi phiên bản mới gặp lỗi nghiêm trọng, việc quay về bản cũ thường làm thủ công, không có trạng thái trung gian, dẫn đến việc database và code lệch pha nhau, gây hỏng dữ liệu vĩnh viễn.

### 1.3. Mục tiêu tối thượng của netCI (6 tiêu chuẩn vàng)

Để giải quyết triệt để các vấn đề trên, netCI được xây dựng dựa trên 6 nguyên tắc bất biến:

| Tiêu chuẩn | Ý nghĩa đối với hệ thống |
| :--- | :--- |
| **Reproducible (Tái tạo)** | Toàn bộ hệ thống có thể dựng lại từ đầu chỉ bằng mã nguồn Git và các kịch bản migration SQL; không ai phải "cấu hình thủ công bằng chuột" trên giao diện. |
| **Isolated (Cách ly)** | Mỗi lần build và deploy đều chạy trên môi trường cô lập (container hoặc ephemeral runner riêng biệt), dọn dẹp sạch sẽ sau khi hoàn tất để không để lại rác ảnh hưởng đến lần sau. |
| **Extensible (Mở rộng)** | Một kiến trúc thống nhất nhưng hỗ trợ cùng lúc nhiều nền tảng chạy khác nhau: từ cụm máy chủ container Docker, cụm Kubernetes hiện đại, cho đến dịch vụ truyền thống `systemd` trên máy chủ vật lý Linux. |
| **Governed (Kiểm soát)** | Mọi sản phẩm build đều phải có "bảng thành phần" (SBOM), phải quét lỗ hổng (Trivy), phải có chữ ký điện tử (Cosign), phải qua cổng chính sách tự động (OPA/Rego) và có nhật ký kiểm toán bất biến (Audit Trail). |
| **Measurable (Đo lường)** | Tự động đo lường 4 chỉ số hiệu suất kỹ thuật chuẩn quốc tế (DORA Metrics) tính trực tiếp từ các sự kiện database thực tế, không dùng số liệu ước chừng hay báo cáo miệng. |
| **Truthful (Trung thực)** | **Tuyệt đối không bao giờ hiển thị màu xanh giả.** Nếu hệ thống bên ngoài (Jenkins, DCIM, Cosign) chưa được cấu hình hoặc bị sập, netCI kiên quyết báo `not_configured`, `BLOCKED` hoặc `HTTP 503`, thà chặn lại chứ không lừa dối người vận hành. |

---

## 2. TỪ ĐIỂN THUẬT NGỮ TOÀN TẬP
*(Dành cho Vibecoder: Giải thích bản chất bằng cả định nghĩa kỹ thuật lẫn hình ảnh đời thường)*

### 2.1. Thuật ngữ CI/CD & Delivery

- **CI (Continuous Integration - Tích hợp liên tục)**:
  - *Kỹ thuật*: Quá trình tự động kéo code mới về, biên dịch, chạy unit test, quét mã độc và đóng gói thành artifact.
  - *Ví von*: Như khâu kiểm tra chất lượng trên dây chuyền đóng hộp bánh trước khi xuất xưởng. Trong netCI, Jenkins đảm nhận việc này.
- **CD (Continuous Delivery / Deployment - Phân phối liên tục)**:
  - *Kỹ thuật*: Quá trình tự động vận chuyển artifact đã được kiểm định lên môi trường staging hoặc production.
  - *Ví von*: Đội xe tải vận chuyển các hộp bánh đã niêm phong đến đúng kệ hàng siêu thị. Trong netCI, Temporal đảm nhận việc này.
- **Artifact (Vật phẩm build)**:
  - *Kỹ thuật*: File kết quả cuối cùng sau khi build, ví dụ: Docker image, file binary Golang, file nén `.tar.gz`.
- **Digest (Vân tay số SHA-256)**:
  - *Kỹ thuật*: Chuỗi mã băm dạng `sha256:4f8a...` dài 64 ký tự hex. Chỉ cần thay đổi 1 dấu cách trong code, chuỗi digest này sẽ biến đổi hoàn toàn.
  - *Ví von*: Vân tay của artifact. netCI quản lý phiên bản dựa trên digest chứ không bao giờ tin tag như `:latest` (vì tag có thể bị ai đó trỏ sang chỗ khác).
- **Immutable Artifact (Vật phẩm bất biến)**:
  - *Kỹ thuật*: Quy tắc quy định một khi artifact đã tạo ra với digest X thì không ai có quyền sửa đổi hay đè lên nó. Muốn đổi code, bắt buộc phải sinh ra một digest mới.
- **State Machine (Máy trạng thái hữu hạn)**:
  - *Kỹ thuật*: Mô hình toán học quy định một thực thể (ví dụ Pipeline) chỉ được ở một trong các trạng thái cố định (`QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`) và chỉ được di chuyển theo các mũi tên cho phép.
  - *Ví dụ*: Không thể từ `QUEUED` nhảy thẳng sang `SUCCEEDED` mà không đi qua `RUNNING`.

### 2.2. Thuật ngữ bảo mật chuỗi cung ứng (Supply Chain Security)

- **SBOM (Software Bill of Materials - Bảng kê thành phần phần mềm)**:
  - *Kỹ thuật*: File JSON liệt kê chi tiết mọi thư viện, dependency, phiên bản mã nguồn mở nằm trong artifact.
  - *Ví von*: Bảng thành phần dinh dưỡng in sau hộp bánh. Khi thế giới công bố một lỗ hổng bảo mật mới (ví dụ Log4j), bạn chỉ cần tra SBOM là biết ngay công ty mình có bị dính hay không.
- **CVE (Common Vulnerabilities and Exposures)**:
  - *Kỹ thuật*: Mã số định danh quốc tế cho một lỗ hổng bảo mật đã được công nhận, ví dụ `CVE-2024-3094`.
- **Trivy**:
  - *Kỹ thuật*: Công cụ quét bảo mật hàng đầu thế giới. Nó đọc SBOM hoặc container image, đối chiếu với cơ sở dữ liệu lỗ hổng thế giới và chỉ ra: "Image này có 2 lỗi Critical, 5 lỗi High".
- **Cosign (Chữ ký điện tử cho Container)**:
  - *Kỹ thuật*: Công cụ dùng cặp khóa công khai / khóa bí mật (Public/Private Key) để ký lên container image.
  - *Ví von*: Dấu mộc niêm phong sáp của triều đình. Nếu một image có chữ ký của netCI, máy chủ production yên tâm rằng đây là bản build chính thống, không bị hacker chèn mã độc trên đường truyền.
- **Truthful Readiness & Independent Signature Verification**:
  - *Kỹ thuật*: Ngay trước khi kích hoạt deploy, netCI tự mình chạy lệnh `cosign verify` độc lập. Nó **không tin** cờ `signature.verified: true` do Jenkins tự khai báo. Vì nếu máy chủ Jenkins bị hacker chiếm quyền, nó sẽ tự bịa ra cờ `true`.

### 2.3. Thuật ngữ kiến trúc phần mềm cốt lõi

- **Seam (Đường nối ghép / Khớp nối)**:
  - *Kỹ thuật*: Điểm giao tiếp trừu tượng giữa tầng nghiệp vụ (Domain) và thế giới bên ngoài. Tầng nghiệp vụ chỉ gọi qua một Interface (ví dụ `CiLauncher`), không cần biết đằng sau là Jenkins, GitHub Actions hay GitLab CI.
  - *Lợi ích*: Khi chạy Unit Test, ta cắm `NullCiLauncher` (chạy mất 0.01 giây). Khi lên Production, ta cắm `JenkinsCiLauncher`. Đổi công nghệ không cần viết lại logic lõi.
- **Deep Module (Module sâu)**:
  - *Kỹ thuật*: Khái niệm kiến trúc kinh điển từ cuốn sách *A Philosophy of Software Design* (John Ousterhout). Một module sâu có **bề mặt tiếp xúc (Interface) rất nhỏ và đơn giản**, nhưng **bên trong (Implementation) giải quyết những bài toán cực kỳ phức tạp**.
  - *Ví dụ*: Trong netCI, toàn bộ giao tiếp lưu trữ chỉ có: `with database.transaction() as tx:`. Nhưng bên trong nó quản lý: mở kết nối pool, rollback khi lỗi, kiểm soát version chống ghi đè, bắt lỗi khóa trùng và ánh xạ dữ liệu.
- **Idempotency (Tính lũy đẳng)**:
  - *Kỹ thuật*: Tính chất đảm bảo một thao tác gọi 1 lần hay 100 lần đều đem lại kết quả y hệt và chỉ tạo ra đúng 1 tài nguyên duy nhất.
  - *Ví von*: Khi bạn bấm nút "Đặt hàng" trên mạng mà mạng bị lag, bạn sốt ruột bấm thêm 5 lần nữa. Một hệ thống có tính lũy đẳng (dùng `Idempotency-Key`) sẽ chỉ trừ tiền tài khoản của bạn đúng 1 lần duy nhất.
- **Optimistic Concurrency Control (Kiểm soát đồng thời lạc quan - CAS)**:
  - *Kỹ thuật*: Kỹ thuật chống ghi đè dữ liệu bằng cột `version`. Khi cập nhật: `UPDATE deployments SET status = 'healthy', version = version + 1 WHERE id = ... AND version = 4`. Nếu ai đó đã sửa trước bạn, `version` đã lên 5, câu lệnh `UPDATE` sẽ tác động lên 0 dòng, hệ thống lập tức phát hiện xung đột và trả về lỗi `HTTP 409 Conflict`.
- **Deployment Lease (Hợp đồng thuê độc quyền hạ tầng)**:
  - *Kỹ thuật*: Cơ chế khóa có thời hạn (ví dụ 15 phút) gắn liền với một mục tiêu (Application + Environment + Target Host).
  - *Ví von*: Bạn thuê phòng họp trong 1 tiếng. Trong 1 tiếng đó, người khác muốn vào họp phải xếp hàng đợi bạn trả phòng. Nếu bạn đang họp mà bất ngờ ngất xỉu (worker bị chết), sau 1 tiếng hợp đồng hết hạn, phòng tự động mở cho người tiếp theo, không làm tắc nghẽn cả công ty.
- **Fencing Token (Thẻ chặn thế hệ)**:
  - *Kỹ thuật*: Một con số nguyên đơn điệu tăng dần (1, 2, 3...) cấp cho mỗi lượt thuê hạ tầng (Lease).
  - *Ví von*: Tránh tình trạng "thây ma sống lại". Giả sử Worker A đang deploy thì mạng bị đứt, Worker A tưởng mình còn sống. Hệ thống hết giờ, chuyển quyền cho Worker B (Fencing token = 6). B deploy xong. 5 phút sau Worker A bỗng dưng kết nối lại và gửi báo cáo kết quả với token = 5. Hệ thống nhìn thấy token 5 nhỏ hơn 6, lập tức **từ chối** báo cáo của A, ngăn chặn việc kết quả cũ ghi đè lên kết quả mới.
- **Transactional Outbox Pattern**:
  - *Kỹ thuật*: Mẫu thiết kế giải quyết bài toán "Hiểm họa ghi hai lần (Dual-Write Hazard)". Khi một sự kiện xảy ra (ví dụ pipeline chạy xong), ta không gửi HTTP Webhook cho Slack/Teams ngay (vì nếu gửi webhook thành công mà database sau đó bị lỗi thì dữ liệu bị lệch). Thay vào đó, ta lưu bức thư thông báo vào bảng `notifications` **trong cùng transaction database** với sự kiện đó. Một tiến trình chạy ngầm (Outbox Worker) sẽ đọc bảng này và gửi đi an toàn.
- **DAG (Directed Acyclic Graph - Đồ thị có hướng không chu trình) & Thuật toán Kahn**:
  - *Kỹ thuật*: Cấu trúc dữ liệu mô tả mối quan hệ phụ thuộc giữa các dịch vụ. Thuật toán Kahn sẽ duyệt qua đồ thị để:
    1. Phát hiện xem có vòng lặp con gà - quả trứng hay không (A phụ thuộc B, B lại phụ thuộc A).
    2. Sắp xếp thứ tự các đợt triển khai (Deployment Waves): Những module không phụ thuộc ai sẽ deploy ở Wave 1. Những module phụ thuộc Wave 1 sẽ deploy ở Wave 2.

### 2.4. Thuật ngữ quản trị & Vận hành

- **OPA (Open Policy Agent) & Ngôn ngữ Rego**:
  - *Kỹ thuật*: Chuẩn công nghiệp về Policy-as-Code (Chính sách dưới dạng mã). Thay vì viết cứng các câu lệnh `if/else` trong code Python, chính sách công ty (như: "Không deploy sau 18h", "Chỉ được dùng container image từ registry nội bộ") được viết bằng file Rego. netCI sẽ hỏi OPA: "Yêu cầu deploy này có hợp lệ không?".
- **Dual-Control Break-Glass (Đập hộp kính khẩn cấp hai người)**:
  - *Kỹ thuật*: Quy trình vượt rào chính sách khi xảy ra sự cố nghiêm trọng (ví dụ hệ thống production bị sập giữa đêm cần deploy bản vá gấp mà chưa kịp quét bảo mật). Cơ chế Dual-Control bắt buộc: **Người gửi yêu cầu phá kính (Requester) phải khác người phê duyệt (Approver)**. Toàn bộ hành động bị ghi nhật ký kiểm toán vĩnh viễn.
- **Progressive Delivery & Canary Evaluation**:
  - *Kỹ thuật*: Triển khai tiệm tiến. Thay vì tung bản cập nhật cho 100% người dùng ngay lập tức, ta mở lưu lượng từ từ: 10% (Canary) -> đánh giá tỷ lệ lỗi HTTP 5xx -> nếu tốt mở tiếp 50% -> 100%. Nếu tỷ lệ lỗi tăng vọt, hệ thống tự động hoàn tác về 0%.
- **DORA Metrics (4 chỉ số vàng của DevOps)**:
  - *Deployment Frequency*: Tần suất đưa code lên production.
  - *Lead Time for Changes*: Thời gian từ khi lập trình viên commit dòng code đầu tiên đến khi nó chạy trên prod.
  - *Change Failure Rate*: Tỷ lệ các lần deploy gây ra sự cố.
  - *Time to Restore Service (MTTR)*: Thời gian trung bình để khắc phục khi có sự cố xảy ra.
- **DCIM (Data Center Infrastructure Management)**:
  - *Kỹ thuật*: Hệ thống quản lý hạ tầng trung tâm dữ liệu (ví dụ NetBox). netCI kết nối với DCIM để biết ứng dụng này được phép deploy lên máy chủ vật lý nào, cổng mạng nào.

---

## 3. BỨC TRANH TỔNG THỂ — CÁC MẢNH GHÉP & LUỒNG VẬN HÀNH

### 3.1. Sơ đồ các hệ thống tham gia

```
┌─────────────────────────┐       ┌──────────────────────────┐
│   Trình duyệt Web       │       │    Hệ thống SCM          │
│   (netCI Portal React)  │       │  (GitHub / GitLab Hooks) │
└────────────┬────────────┘       └─────────────┬────────────┘
             │                                  │
             │ HTTP REST (JWT / OIDC)           │ Webhook (HMAC-SHA256)
             ▼                                  ▼
┌─────────────────────────────────────────────────────────────┐
│                 netCI Core API (FastAPI)                    │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Middlewares: Prometheus Metrics, Correlation JSON Log │  │
│  └───────────────────────────────────────────────────────┘  │
│  ┌──────────────────┐ ┌──────────────────┐ ┌─────────────┐  │
│  │ Admission Control│ │ Policy (OPA/Rego)│ │ DAG Engine  │  │
│  └──────────────────┘ └──────────────────┘ └─────────────┘  │
│  ┌──────────────────┐ ┌──────────────────┐ ┌─────────────┐  │
│  │ Delivery Platform│ │ Workload Identity│ │ Outbox Queue│  │
│  └──────────────────┘ └──────────────────┘ └─────────────┘  │
└──────────────┬──────────────────┬─────────────────┬─────────┘
               │                  │                 │
               ▼                  ▼                 ▼
     ┌──────────────────┐ ┌───────────────┐ ┌──────────────┐
     │  PostgreSQL 16   │ │   Jenkins     │ │   Temporal   │
     │  Canonical State │ │  (Tác vụ CI)  │ │  (Tác vụ CD) │
     │  (34 bảng / ACID)│ └───────┬───────┘ └───────┬──────┘
     └──────────────────┘         │                 │
                                  ▼                 ▼
                           Ephemeral Runner  Kubernetes / VM
                           (Build/SBOM/Sign) (Canary Deploy)
```

### 3.2. Vì sao tách rời Jenkins (CI) và Temporal (CD)?

Đây là một quyết định thiết kế then chốt của netCI (được ghi nhận trong **ADR-004**):

| Đặc tính | Jenkins (Phụ trách CI) | Temporal (Phụ trách CD) |
| :--- | :--- | :--- |
| **Nhiệm vụ chính** | Biên dịch code, chạy test, quét lỗ hổng, đóng gói container image, ký số Cosign. | Triển khai container lên cụm máy chủ, dịch chuyển traffic Canary, chờ người phê duyệt, kiểm tra sức khỏe. |
| **Thời gian chạy** | Nhanh, thường kéo dài từ 2 đến 10 phút. | Kéo dài, có thể chạy hàng giờ hoặc hàng ngày (ví dụ chờ giám đốc phê duyệt lên Production). |
| **Hành vi khi crash** | Nếu server bị mất điện, toàn bộ tiến trình build bị hủy bỏ và phải kích hoạt chạy lại từ đầu. | **Durable Execution (Thực thi bền bỉ)**: Nếu máy chủ sập và khởi động lại, workflow sẽ tiếp tục chạy ngay tại bước nó vừa dừng lại mà không bị mất dữ liệu. |
| **Lý do chọn** | Tận dụng hệ sinh thái hàng nghìn plugin build và công cụ đóng gói container. | Đảm bảo quy trình phê duyệt và kiểm tra canary không bao giờ bị đứt gãy giữa chừng. |

---

### 3.3. Luồng một lần deploy đơn lẻ (Single-Module Deployment Lifecycle)

Dưới đây là hành trình từng bước của một dòng code khi đi qua netCI:

```
[BƯỚC 1: KÍCH HOẠT]
Lập trình viên bấm nút "Trigger Pipeline" trên Portal (hoặc Push code lên GitHub).
         │
         ▼
[BƯỚC 2: TIẾP NHẬN & XÁC THỰC BIÊN GIỚI]
FastAPI kiểm tra quyền (RBAC: DEVELOPER), lọc sạch các tham số bị cấm (build_inputs.py),
tự động gán Commit SHA và lưu bản ghi `PipelineRun` (status=QUEUED) cùng `AuditRecord`
vào PostgreSQL trong một Transaction duy nhất.
         │
         ▼
[BƯỚC 3: GIAO VIỆC CHO JENKINS]
netCI gọi API Jenkins, đồng thời phát hành một "Thẻ định danh máy móc" (Workload Identity Token)
ngắn hạn, có gắn chữ ký HMAC và giới hạn scope chỉ được gửi bằng chứng cho đúng Run này.
         │
         ▼
[BƯỚC 4: CI RUNNER THỰC THI]
Jenkins khởi động một container runner cách ly:
Checkout code -> Test -> Build Container -> Xuất SBOM (Syft) -> Quét CVE (Trivy) -> Ký số (Cosign).
         │
         ▼
[BƯỚC 5: BÁO CÁO BẰNG CHỨNG (SECURITY EVIDENCE)]
Jenkins gọi ngược về netCI: POST /pipeline-runs/{id}/security-evidence.
netCI đối chiếu chính sách: Có lỗ hổng nghiêm trọng không? Có chữ ký không?
         │
         ▼
[BƯỚC 6: BÁO CÁO KẾT QUẢ CI & TẠO DEPLOYMENT]
Jenkins gọi POST /pipeline-runs/{id}/ci-result với digest sản phẩm.
netCI khóa bất biến phiên bản ReleaseVersion trong bảng `release_versions`.
Nếu môi trường đích là Production -> Trạng thái chuyển sang `PENDING_APPROVAL`, DỪNG LẠI chờ người.
         │
         ▼
[BƯỚC 7: PHÊ DUYỆT & CẤP LEASE]
Người phê duyệt (phải khác người kích hoạt ở Bước 1 - Separation of Duties) bấm Approve.
netCI kích hoạt:
1. Đăng ký một `DeploymentLease` trên cụm máy chủ đích (chống người khác chen ngang).
2. Cấp một `FencingToken` tăng dần.
3. Kích hoạt Temporal Workflow.
         │
         ▼
[BƯỚC 8: TIẾN TRÌNH CD THỰC THI]
Temporal Worker thực hiện:
1. Tự chạy `cosign verify` độc lập để kiểm tra lại chữ ký (không tin cờ của Jenkins).
2. Triển khai code lên Kubernetes/Docker/Linux VM.
3. Liên tục gửi tín hiệu Heartbeat để gia hạn Lease.
4. Kiểm tra sức khỏe (Health check endpoint của service mới).
         │
         ▼
[BƯỚC 9: HOÀN TẤT HOẶC ROLLBACK]
- NẾU KHỎE MẠNH: Worker gửi POST /deployments/{id}/result (healthy, fencingToken).
  netCI kiểm tra Fencing Token -> Đúng thế hệ -> Ghi nhận trạng thái `HEALTHY`,
  tính toán sự kiện DORA, nhả Lease.
- NẾU GẶP SỰ CỐ: Chuyển trạng thái sang `ROLLBACK_IN_PROGRESS` -> Khôi phục bản trước ->
  Chuyển sang `ROLLED_BACK`. Tuyệt đối không ghi nhận thành công giả.
```

---

### 3.4. Luồng phát hành đa module có thứ tự (Multi-Module DAG Release Plan)

Khi một hệ sinh thái gồm nhiều microservices cần deploy cùng lúc (ví dụ: Service `billing-api` cần `user-db` chạy trước, `frontend` cần `billing-api` chạy trước):

1. **Khai báo đồ thị phụ thuộc**: Lập trình viên gửi danh sách các module và danh sách `dependsOn` lên `POST /production-requests`.
2. **Giải thuật Kahn (DAG)**: Engine `domain/dag.py` phân tích đồ thị:
   - Nếu phát hiện vòng lặp (A -> B -> A) -> Thẳng thừng từ chối bằng lỗi `HTTP 409 Conflict: Dependency cycle detected`.
   - Nếu đồ thị hợp lệ -> Chia thành các đợt triển khai (Deployment Waves):
     - **Wave 1**: Các module độc lập (ví dụ `user-db`).
     - **Wave 2**: Các module phụ thuộc Wave 1 (ví dụ `billing-api`).
     - **Wave 3**: Các module phụ thuộc Wave 2 (ví dụ `frontend`).
3. **Thực thi tuần tự từng Wave**:
   - Các module trong cùng một Wave được deploy song song để tối ưu thời gian.
   - Chỉ khi 100% module của Wave 1 đạt trạng thái `HEALTHY`, Wave 2 mới được phép bắt đầu.
   - Nếu bất kỳ module nào ở Wave 1 thất bại -> Toàn bộ quy trình dừng lại lập tức, kích hoạt Rollback cho các module đã chạy và hủy bỏ các Wave phía sau.

---

### 3.5. Luồng xử lý sự cố khẩn cấp (Dual-Control Break-Glass Flow)

Khi nửa đêm hệ thống gặp sự cố khẩn cấp cần đưa bản vá lên production mà không thể chờ qua các bước kiểm tra chính sách thông thường:

1. **Gửi yêu cầu phá kính**: Kỹ sư gửi `POST /break-glass/requests` nêu rõ lý do khẩn cấp và thời gian cần cấp quyền (TTL tối đa 4 giờ). Trạng thái bản ghi là `PENDING`.
2. **Nguyên tắc hai người độc lập (Dual-Control)**:
   - Hệ thống kiểm tra: `requested_by != approved_by`.
   - Người phê duyệt (ví dụ Trưởng phòng bảo mật hoặc SRE Lead) phải đăng nhập tài khoản riêng để bấm duyệt `POST /break-glass/requests/{id}/approve`.
3. **Thực thi có giám sát**:
   - Yêu cầu deploy được cấp phép đi qua Admission Controller mà không bị chặn bởi các quy tắc OPA thông thường.
   - Mỗi thao tác được thực hiện trong thời gian "phá kính" đều bị gắn cờ đặc biệt và ghi lại camera nhật ký kiểm toán trong bảng `audit_events`.
   - Sau khi hết thời hạn TTL, quyền phá kính tự động bị vô hiệu hóa ngay lập tức.

---

## 4. TẠI SAO LẠI LÀ KIẾN TRÚC NÀY?
*(7 nguyên tắc cốt lõi giải thích vì sao code được viết như vậy)*

### 4.1. PostgreSQL là nguồn chân lý duy nhất (Canonical State)
- **Lỗi kinh điển trước đây**: Lưu trạng thái vào bộ nhớ RAM của Python (dùng biến toàn cục hoặc dictionary). Khi có 2 bản sao API (Replica A và B) chạy song song, người dùng tạo dịch vụ ở A nhưng gọi sang B lại không thấy đâu.
- **Giải pháp của netCI**: Toàn bộ trạng thái của 34 bảng được lưu trong PostgreSQL 16. Mọi request đọc và ghi dữ liệu trực tiếp vào database. Bộ nhớ RAM không bao giờ quyết định dữ liệu sống còn.

### 4.2. Mọi chuyển dịch trạng thái phải Atomic, Idempotent và Concurrency-safe
- **Atomic (Tất cả hoặc không gì cả)**: Một lần đăng ký module phải ghi cùng lúc 4 bảng: Application, Module, Audit, Idempotency. Ta gom toàn bộ vào một `UnitOfWork` thực thi trong một transaction duy nhất (`store/session.py`). Nếu có lỗi, database tự động rollback sạch sẽ, không để lại rác mồ côi.
- **Idempotent**: Dùng khóa `Idempotency-Key` để tránh tạo tài nguyên trùng lặp khi mạng chập chờn client gửi lại request.
- **Concurrency-safe**: Sử dụng kỹ thuật Compare-and-Set (CAS) với cột `version` để bảo vệ các dòng dữ liệu không bị ghi đè khi có 2 luồng cùng sửa một lúc.

### 4.3. Máy móc phải có danh tính riêng (Workload Identity)
- **Lỗi kinh điển trước đây**: Dùng chung một mật khẩu duy nhất (`NETCI_PIPELINE_API_KEY`) cho tất cả máy chủ Jenkins và worker. Nếu một máy chủ Jenkins phụ bị hacker chiếm quyền, nó có thể dùng mật khẩu đó để đánh dấu bất kỳ deployment nào trên production là "thành công".
- **Giải pháp của netCI**: Mỗi khi kích hoạt một run, netCI tự sinh một token HMAC ngắn hạn (`workload_identity.py`). Token này mang theo ID của đúng run đó và danh sách quyền hạn (Scope) rất hẹp. Token của Jenkins **không bao giờ có quyền** gửi kết quả deployment.

### 4.4. Trình duyệt không được quyết định nơi deploy (Input Trust Boundaries)
- **Lỗi kinh điển trước đây**: Cho phép client gửi file JSON tự do lên server, trong đó chứa các trường như `target_hosts: "10.0.0.99"`, `namespace: "prod"`. Kẻ xấu chỉ cần mở tab Network trên trình duyệt, sửa JSON là có thể hướng luồng deploy sang máy chủ khác.
- **Giải pháp của netCI**: Trong `build_inputs.py`, chúng tôi lập danh sách trắng (Allowlist) phân định rõ:
  - *Tham số build*: Dev được truyền (`buildProfile`, `skipTests`).
  - *Tham số deploy*: **Chỉ có server được quyết định**, lấy từ cấu hình đã đăng ký sẵn trong database. Nếu client cố tình gửi các trường điều khiển hạ tầng, server sẽ thẳng tay từ chối bằng lỗi `HTTP 422 Unprocessable Entity`.

### 4.5. Không bao giờ hiển thị màu xanh giả (Zero False Greens & Fail-Closed)
- Đây là triết lý xuyên suốt của netCI:
  - Nếu hệ thống DCIM chưa cấu hình -> Trả về `status: "not_configured"`, dứt khoát không bịa ra danh sách server mẫu.
  - Nếu máy chủ chưa có dữ liệu đo lường sức khỏe -> Trạng thái mặc định là `UNKNOWN`, tuyệt đối không tự ý gán nhãn `HEALTHY`.
  - Nếu dịch vụ kiểm tra chữ ký Cosign bị mất mạng -> Hệ thống chọn phương án **Fail-Closed** (thà chặn lại không cho deploy còn hơn cho phép một container không rõ nguồn gốc lọt vào production).

### 4.6. Chống xung đột triển khai bằng Leases và Fencing Tokens
- Không thể để 2 người cùng deploy vào một cụm máy chủ cùng một lúc.
- netCI giải quyết bằng cách tạo bảng `deployment_leases` với chỉ mục Partial Unique Index:
  ```sql
  CREATE UNIQUE INDEX deployment_leases_one_active_per_target
      ON deployment_leases (application_id, environment, target)
      WHERE released_at IS NULL;
  ```
  Nhờ ràng buộc cấp độ database này, ngay cả khi 2 request gửi tới 2 máy chủ API khác nhau vào cùng một mili-giây, database cũng đảm bảo chỉ có đúng 1 request giành được quyền deploy. Cùng với Fencing Token tăng dần, hệ thống loại bỏ hoàn toàn hiện tượng writer lỗi thời ghi đè kết quả.

### 4.7. Kiến trúc Deep Module và Seam rõ ràng
- Mỗi module được thiết kế sâu (Deep Module), giấu kín sự phức tạp bên trong. Toàn bộ các kết nối ngoại vi (SCM, CI, CD, DCIM, Signature) đều nằm sau các cổng Seam (`adapters/`). Nhờ đó, việc viết unit test chạy siêu tốc trong vài giây, và việc thay thế công nghệ trong tương lai không làm ảnh hưởng đến tầng nghiệp vụ lõi.

---

## 5. ĐI QUA TỪNG FILE TRONG DỰ ÁN — VAI TRÒ & LÝ DO TỒN TẠI

Dưới đây là bảng phân tích chi tiết toàn bộ các file mã nguồn đang hoạt động trong repository:

### 5.1. Backend Core (`backend/app/`)

| File | Số dòng | Vai trò & Lý do tồn tại trong kiến trúc |
| :--- | :---: | :--- |
| [`main.py`](file:///home/deployer/netci-delivery-platform/backend/app/main.py) | **3459** | **Cửa ngõ HTTP & Composition Root**. Nơi khởi tạo ứng dụng FastAPI, đăng ký các middleware (Prometheus, Correlation ID, CORS, Error Handling), khởi tạo kết nối database và định tuyến cho toàn bộ hơn 60 API endpoint. |
| [`delivery.py`](file:///home/deployer/netci-delivery-platform/backend/app/delivery.py) | **2212** | **Trái tim điều phối phân phối**. Chứa class `DeliveryPlatform` thực thi toàn bộ logic nghiệp vụ sống còn: quản lý vòng đời Deployment, cấp phát Lease, kiểm tra Fencing Token, rollback 2 pha, retry pipeline và tích hợp với Transactional Outbox. |
| [`portal.py`](file:///home/deployer/netci-delivery-platform/backend/app/portal.py) | **1142** | **Cầu nối giao diện Web**. Chứa class `PortalService` chuyển đổi các khái niệm kỹ thuật sâu bên dưới thành ngôn ngữ thân thiện với lập trình viên trên Portal (Dashboard KPIs, System Hierarchy, Module Settings, DORA Trends). |
| [`workload_identity.py`](file:///home/deployer/netci-delivery-platform/backend/app/workload_identity.py) | **402** | **Cơ quan cấp căn cước máy móc**. Sinh và xác thực các token HMAC ngắn hạn có gắn phạm vi (Scoped Machine Tokens), đảm bảo máy chủ Jenkins chỉ được làm đúng việc của mình. |
| [`logging.py`](file:///home/deployer/netci-delivery-platform/backend/app/logging.py) | **163** | **Nhật ký JSON có cấu trúc**. Tự động gắn nhãn `correlation_id` xuyên suốt các microservices và tích hợp bộ lọc bảo mật tự động che mờ (mask) toàn bộ mật khẩu, token, private key (`[REDACTED]`). |
| [`metrics.py`](file:///home/deployer/netci-delivery-platform/backend/app/metrics.py) | **218** | **Đồng hồ đo lường Prometheus**. Định nghĩa `MetricsRegistry` thu thập số lượng request HTTP, độ trễ histogram theo từng route template, dung lượng hàng đợi outbox và số lượng kết nối connection pool. |
| [`notifications.py`](file:///home/deployer/netci-delivery-platform/backend/app/notifications.py) | **374** | **Cỗ máy Transactional Outbox**. Quản lý bảng hàng đợi `notifications`, worker chạy ngầm tự động bắn webhook thông báo cho Slack/Teams với lịch trình thử lại lũy thừa (2s, 4s, 8s, 16s, 32s) và đẩy vào hàng đợi chết (dead-letter queue) khi hết số lần retry. |
| [`readiness.py`](file:///home/deployer/netci-delivery-platform/backend/app/readiness.py) | **179** | **Cảm biến sức khỏe trung thực**. Cung cấp endpoint `/livez`, `/readyz`, `/operator/health`. Báo cáo trung thực tình trạng kết nối DB, DCIM, Cosign với cơ chế Circuit Breaker, từ chối báo xanh giả. |
| [`reconciler.py`](file:///home/deployer/netci-delivery-platform/backend/app/reconciler.py) | **251** | **Bảo vệ tuần tra tự động**. Chạy nền định kỳ để quét các pipeline/deployment bị treo do đứt mạng giữa chừng, tự động đối chiếu với trạng thái thật ở Jenkins/Temporal để đồng bộ dữ liệu. |
| [`retention.py`](file:///home/deployer/netci-delivery-platform/backend/app/retention.py) | **137** | **Lao công dọn rác dữ liệu**. Cung cấp hàm dọn dẹp các token tạm hết hạn (`callback_token_uses`), lịch sử webhook cũ và thông báo đã gửi để database luôn nhẹ nhàng, nhanh chóng. |
| [`build_inputs.py`](file:///home/deployer/netci-delivery-platform/backend/app/build_inputs.py) | **181** | **Bộ gác cổng biên giới tham số**. Ngăn chặn lập trình viên can thiệp hoặc ghi đè các tham số nhạy cảm của hạ tầng (máy chủ đích, namespace production) thông qua payload gọi build. |
| [`auth.py`](file:///home/deployer/netci-delivery-platform/backend/app/auth.py) | **520** | **Bộ máy xác thực người dùng**. Hỗ trợ đăng nhập OIDC (JWT từ Okta/Keycloak), chế độ Token tĩnh và phân quyền RBAC đa cấp bậc (`VIEWER`, `DEVELOPER`, `PLATFORM_ADMIN`). |
| [`ratelimit.py`](file:///home/deployer/netci-delivery-platform/backend/app/ratelimit.py) | **111** | **Chống nghẽn đường truyền**. Giới hạn tần suất gọi API theo giải thuật Token Bucket, chống spam và tấn công từ chối dịch vụ. |
| [`client_address.py`](file:///home/deployer/netci-delivery-platform/backend/app/client_address.py) | **88** | **Trích xuất IP an toàn**. Phân tích header `X-Forwarded-For` với danh sách Proxy tin cậy, chống giả mạo IP nguồn. |
| [`traffic.py`](file:///home/deployer/netci-delivery-platform/backend/app/traffic.py) | **184** | **Điều khiển lưu lượng Canary**. Quản lý việc dịch chuyển phần trăm traffic (10% -> 50% -> 100%) và tự động dừng lại nếu tỷ lệ lỗi vượt ngưỡng cho phép. |
| [`admission.py`](file:///home/deployer/netci-delivery-platform/backend/app/admission.py) | **180** | **Kiểm soát tiếp nhận (Admission Controller)**. Kiểm tra các quy tắc an toàn trước khi deploy (ví dụ: cấm container image sử dụng tag trôi nổi `:latest` trên namespace production). |
| [`coordinator.py`](file:///home/deployer/netci-delivery-platform/backend/app/coordinator.py) | **162** | **Nhạc trưởng điều phối pipeline**. Nhận lệnh từ API, phân chia các giai đoạn (Stages) và gọi adapter CI tương ứng. |
| [`persistence.py`](file:///home/deployer/netci-delivery-platform/backend/app/persistence.py) | **113** | **Khai báo cấu trúc lưu trữ chung**. Định nghĩa class `UnitOfWork`, `AuditRecord`, `IdempotencyRow` dùng chung cho toàn bộ tầng lưu trữ. |
| [`errors.py`](file:///home/deployer/netci-delivery-platform/backend/app/errors.py) | **42** | **Từ điển ngoại lệ chuẩn hóa**. Định nghĩa các lớp lỗi nghiệp vụ như `ConflictError`, `NotFoundError`, `PolicyViolationError`. |
| [`demo_data.py`](file:///home/deployer/netci-delivery-platform/backend/app/demo_data.py) | **146** | **Dữ liệu mẫu tham chiếu**. Cung cấp định nghĩa các module mẫu (`hello-container`, `hello-kubernetes`, `hello-systemd-go`), chỉ kích hoạt khi bật cờ `NETCI_DEMO_DATA=true`. |
| [`runtime_environment.py`](file:///home/deployer/netci-delivery-platform/backend/app/runtime_environment.py) | **17** | **Bộ dò môi trường runtime**. Định nghĩa một điểm duy nhất kiểm tra xem hệ thống đang chạy ở môi trường nào (`local`, `staging`, `production`). |

---

### 5.2. Backend Adapters (`backend/app/adapters/`)
*Tầng này chứa các "công tắc chuyển đổi" để nói chuyện với các hệ thống bên ngoài:*

- [`adapters/scm.py`](file:///home/deployer/netci-delivery-platform/backend/app/adapters/scm.py): Kết nối GitHub và GitLab. Xác thực chữ ký webhook HMAC-SHA256, kiểm tra deduplication chống gửi lặp payload và gửi ngược trạng thái commit status (Pending, Success, Failure) về Git.
- [`adapters/ci_launcher.py`](file:///home/deployer/netci-delivery-platform/backend/app/adapters/ci_launcher.py): Seam kết nối CI server. Cung cấp `JenkinsCiLauncher` cho môi trường thật và `NullCiLauncher` cho bài test siêu tốc.
- [`adapters/jenkins_http.py`](file:///home/deployer/netci-delivery-platform/backend/app/adapters/jenkins_http.py): Client HTTP nói chuyện với REST API của Jenkins, xử lý mã hóa bảo vệ chống tấn công CSRF (Crumb Issuer).
- [`adapters/jenkins_router.py`](file:///home/deployer/netci-delivery-platform/backend/app/adapters/jenkins_router.py): Bộ định tuyến thông minh. Khi có nhiều máy chủ Jenkins, router sẽ chọn máy chủ đang rảnh để giao việc.
- [`adapters/cd_orchestrator.py`](file:///home/deployer/netci-delivery-platform/backend/app/adapters/cd_orchestrator.py): Seam điều phối triển khai CD qua Temporal Workflow.
- [`adapters/signature_verifier.py`](file:///home/deployer/netci-delivery-platform/backend/app/adapters/signature_verifier.py): Chạy công cụ Cosign để thẩm định chữ ký container image một cách độc lập ngay trên máy chủ netCI trước khi cho phép deploy.
- [`adapters/dcim.py`](file:///home/deployer/netci-delivery-platform/backend/app/adapters/dcim.py): Giao tiếp với hệ thống quản lý trung tâm dữ liệu DCIM (NetBox). Nếu chưa cấu hình, báo trạng thái trung thực `not_configured`.
- [`adapters/interfaces.py`](file:///home/deployer/netci-delivery-platform/backend/app/adapters/interfaces.py): Các interface trừu tượng (`ScmProvider`, `CiLauncher`, `CdOrchestrator`, `DcimCatalog`, `SignatureVerifier`).

---

### 5.3. Backend Store (`backend/app/store/`)
*Deep module quan trọng nhất — toàn bộ phần lưu trữ dữ liệu bền vững:*

- [`store/postgres.py`](file:///home/deployer/netci-delivery-platform/backend/app/store/postgres.py): **Toàn bộ SQL thật**. Chứa class `PostgresDatabase` và `PostgresConnectionPool`. Quản lý connection pool, thực hiện các câu truy vấn phức tạp, so sánh Compare-and-Set, khóa Lease và cấp phát log sequence an toàn.
- [`store/session.py`](file:///home/deployer/netci-delivery-platform/backend/app/store/session.py): Định nghĩa Protocol `PlatformSession` — bản hợp đồng quy định mọi thao tác đọc/ghi trong một Transaction.
- [`store/records.py`](file:///home/deployer/netci-delivery-platform/backend/app/store/records.py): Các Dataclass biểu diễn dữ liệu cấp dòng của bảng: `SystemRow`, `ModuleRow`, `VersionRow`, `RequestRow`, `DeploymentLease`.
- [`store/memory.py`](file:///home/deployer/netci-delivery-platform/backend/app/store/memory.py): Bản lưu trữ trong bộ nhớ RAM có hỗ trợ Staging và Rollback thật, phục vụ chạy unit test trong mili-giây.
- [`store/__init__.py`](file:///home/deployer/netci-delivery-platform/backend/app/store/__init__.py): Hàm `build_database()` quyết định chọn PostgreSQL hay In-memory. **Kiên quyết từ chối khởi động in-memory nếu chạy ngoài môi trường local.**

---

### 5.4. Backend Policy & Governance (`backend/app/policy/`)
*Bộ não kiểm soát chính sách và quản trị rủi ro doanh nghiệp:*

- [`policy/engine.py`](file:///home/deployer/netci-delivery-platform/backend/app/policy/engine.py): Động cơ thực thi chính sách, đánh giá các bộ luật Rego (Open Policy Agent).
- [`policy/rules.py`](file:///home/deployer/netci-delivery-platform/backend/app/policy/rules.py): Định nghĩa các luật an toàn: quyền hạn theo Role, quyền hạn theo Team, kiểm tra Security Evidence và miễn trừ CVE có thời hạn.
- [`policy/risk.py`](file:///home/deployer/netci-delivery-platform/backend/app/policy/risk.py): Thuật toán tính điểm rủi ro tự động (0 đến 100 điểm) dựa trên độ lớn của thay đổi code, môi trường đích và thời điểm deploy.
- [`policy/break_glass.py`](file:///home/deployer/netci-delivery-platform/backend/app/policy/break_glass.py): Dịch vụ đập hộp kính khẩn cấp, bắt buộc cơ chế 2 người độc lập (Dual-Control) và tự động thu hồi quyền sau khi hết hạn TTL.
- [`policy/quota.py`](file:///home/deployer/netci-delivery-platform/backend/app/policy/quota.py): Quản lý hạn ngạch tài nguyên (Quota: CPU, RAM, số lượng module) cho từng đội ngũ phát triển.

---

### 5.5. Backend Service Catalog & Previews (`backend/app/catalog/`)
*Các tính năng tự phục vụ (Self-Service) của một IDP hiện đại:*

- [`catalog/services.py`](file:///home/deployer/netci-delivery-platform/backend/app/catalog/services.py): Quản lý danh mục dịch vụ (Service Catalog), quyền sở hữu đội nhóm (Ownership) và đồ thị liên kết giữa các service.
- [`catalog/templates.py`](file:///home/deployer/netci-delivery-platform/backend/app/catalog/templates.py): Các template mẫu chuẩn mực (Golden Path Templates) giúp lập trình viên tạo dự án mới chuẩn Docker/Kubernetes/Systemd trong 30 giây.
- [`catalog/previews.py`](file:///home/deployer/netci-delivery-platform/backend/app/catalog/previews.py): Quản lý vòng đời của môi trường xem trước (Preview Environment) sinh ra theo từng Pull Request, tự động hủy sau tối đa 72 giờ.
- [`catalog/resources.py`](file:///home/deployer/netci-delivery-platform/backend/app/catalog/resources.py): Cơ chế tự xin cấp phát tài nguyên phụ trợ (Database, Redis, S3 Bucket) thông qua phiếu yêu cầu tự phục vụ.

---

### 5.6. Backend Workflows & Projections

- [`domain/models.py`](file:///home/deployer/netci-delivery-platform/backend/app/domain/models.py): Các entity cốt lõi và máy trạng thái bất biến (`PIPELINE_TRANSITIONS`, `DEPLOYMENT_TRANSITIONS`).
- [`domain/dag.py`](file:///home/deployer/netci-delivery-platform/backend/app/domain/dag.py): Giải thuật Kahn xử lý sắp xếp topo đồ thị DAG và phát hiện phụ thuộc vòng.
- [`workflows/provision_and_deploy.py`](file:///home/deployer/netci-delivery-platform/backend/app/workflows/provision_and_deploy.py): Workflow Temporal điều phối quy trình triển khai: cấp phát hạ tầng -> deploy -> chờ duyệt -> health check -> rollback.
- [`workflows/activities.py`](file:///home/deployer/netci-delivery-platform/backend/app/workflows/activities.py): Các activity thực thi side-effect cụ thể (chạy lệnh Ansible, chạy Helm, verify chữ ký Cosign).
- [`workflows/worker.py`](file:///home/deployer/netci-delivery-platform/backend/app/workflows/worker.py): Tiến trình nền lắng nghe và nhận tác vụ từ máy chủ Temporal.
- [`projections/dora.py`](file:///home/deployer/netci-delivery-platform/backend/app/projections/dora.py): Đọc bảng sự kiện `delivery_events` để tính toán 4 chỉ số DORA phục vụ hiển thị biểu đồ.

---

### 5.7. Frontend Portal (`frontend/src/`)

- [`App.tsx`](file:///home/deployer/netci-delivery-platform/frontend/src/App.tsx): Bộ định tuyến URL chính (Router) và quản lý trạng thái phiên đăng nhập.
- [`PortalShell.tsx`](file:///home/deployer/netci-delivery-platform/frontend/src/PortalShell.tsx): Khung giao diện chuẩn (Sidebar, Header, thanh điều hướng, khu vực thông báo).
- [`CatalogPage.tsx`](file:///home/deployer/netci-delivery-platform/frontend/src/CatalogPage.tsx): Màn hình Service Catalog: Tìm kiếm dịch vụ, xem template mẫu Golden Path và sơ đồ phụ thuộc.
- [`ModulePage.tsx`](file:///home/deployer/netci-delivery-platform/frontend/src/ModulePage.tsx): Màn hình chi tiết Module: Theo dõi lịch sử pipeline, nút trigger build, nhật ký log thời gian thực và 4 chỉ số DORA.
- [`ProductionRequestsPage.tsx`](file:///home/deployer/netci-delivery-platform/frontend/src/ProductionRequestsPage.tsx): Màn hình phê duyệt deploy lên Production, chấm điểm rủi ro và nút đập hộp kính khẩn cấp (Break-Glass).
- [`NewModuleWizard.tsx`](file:///home/deployer/netci-delivery-platform/frontend/src/NewModuleWizard.tsx): Wizard hướng dẫn tạo mới service theo từng bước dành cho lập trình viên.
- [`ModuleSettings.tsx`](file:///home/deployer/netci-delivery-platform/frontend/src/ModuleSettings.tsx): Màn hình cài đặt cấu hình module, cài đặt webhook GitHub/GitLab và thông số môi trường.
- [`LoginPage.tsx`](file:///home/deployer/netci-delivery-platform/frontend/src/LoginPage.tsx): Màn hình đăng nhập OIDC / Token an toàn.
- [`AsyncState.tsx`](file:///home/deployer/netci-delivery-platform/frontend/src/AsyncState.tsx): Xử lý trạng thái Loading, Lỗi mạng hoặc Dữ liệu rỗng một cách lịch sự, không giật lag màn hình.
- [`api/netciClient.ts`](file:///home/deployer/netci-delivery-platform/frontend/src/api/netciClient.ts): Client gọi API backend bằng `fetch`, tự động đính kèm Token và xử lý lỗi chuẩn xác.
- [`styles.css`](file:///home/deployer/netci-delivery-platform/frontend/src/styles.css): Toàn bộ Design System (biến màu HSL, chế độ Dark mode, hiệu ứng kính mờ glassmorphism và hoạt ảnh vi mô).

---

### 5.8. Bộ kịch bản vận hành & Kiểm định (`scripts/`)

- [`production_readiness_audit.py`](file:///home/deployer/netci-delivery-platform/scripts/production_readiness_audit.py): **Bài thi kiểm định 28 tiêu chí cốt lõi**. Tự động chạy và đánh giá toàn diện cả 13 Phase, xuất bằng chứng thẩm định `evidence/production_readiness_audit.json` đạt chứng chỉ `CERTIFIED`.
- [`netci_backup.py`](file:///home/deployer/netci-delivery-platform/scripts/netci_backup.py): **Công cụ sao lưu & Phục hồi cơ sở dữ liệu**. Hỗ trợ mã hóa AES-256-GCM, tự động khám phá bảng và tính toán checksum/row counts theo phương thức batch `UNION ALL` siêu tốc.
- [`netci_dr_drill.py`](file:///home/deployer/netci-delivery-platform/scripts/netci_dr_drill.py): **Kịch bản diễn tập thảm họa tự động**. Thực hiện trọn gói chu trình: backup mã hóa -> tạo scratch DB -> khôi phục -> đối chiếu checksum/row counts/khóa ngoại trên toàn bộ 34 bảng -> dọn dẹp và xuất bằng chứng JSON trong 4 giây.
- [`production_acceptance_harness.py`](file:///home/deployer/netci-delivery-platform/scripts/production_acceptance_harness.py): Khung nghiệm thu 9 cổng hạ tầng (P0 Gates), xuất file JUnit XML (`evidence/acceptance.xml`).
- [`migrate.py`](file:///home/deployer/netci-delivery-platform/scripts/migrate.py): Trình quản lý migration SQL độc lập, kiểm tra độ lệch giữa file SQL và database thật (`--check-schema`).
- [`validate_release.py`](file:///home/deployer/netci-delivery-platform/scripts/validate_release.py): Kiểm tra 25 điều kiện tiên quyết trước khi phát hành phiên bản mới.
- [`validate_platform.py`](file:///home/deployer/netci-delivery-platform/scripts/validate_platform.py): Quét tĩnh toàn bộ codebase để đảm bảo không vi phạm các quy tắc kiến trúc.
- [`validate_catalog.py`](file:///home/deployer/netci-delivery-platform/scripts/validate_catalog.py): Xác thực tính hợp lệ của danh mục service và template mẫu.
- [`validate_windows.py`](file:///home/deployer/netci-delivery-platform/scripts/validate_windows.py): Đảm bảo các đường dẫn và script chạy tốt trên cả hệ điều hành Windows.
- [`validate_oss_readiness.mjs`](file:///home/deployer/netci-delivery-platform/scripts/validate_oss_readiness.mjs): Kiểm tra 24 file quản trị mã nguồn mở (LICENSE, DCO, Security Policy, Pinned Dependencies).
- [`netci_retention_purge.py`](file:///home/deployer/netci-delivery-platform/scripts/netci_retention_purge.py): Script kích hoạt dọn dẹp các bản ghi hết hạn định kỳ.
- [`benchmark.py`](file:///home/deployer/netci-delivery-platform/scripts/benchmark.py): Công cụ đo lường hiệu năng và độ trễ, cảnh báo khi hệ thống có dấu hiệu chạy chậm đi.

---

### 5.9. Bộ hồ sơ quyết định kiến trúc (`docs/decisions/` - 26 ADRs)

Mỗi file ADR ghi lại một quyết định kiến trúc quan trọng, bối cảnh dẫn tới nó, các phương án đã bị loại bỏ và lý do vì sao chọn phương án hiện tại:

- **ADR-001**: Kiến trúc PostgreSQL Canonical State và khả năng sống sót sau restart.
- **ADR-002**: Tách rời trách nhiệm giữa netCI Portal và Backstage.
- **ADR-003**: Chọn Temporal làm bộ máy điều phối triển khai bền vững (Durable CD Engine).
- **ADR-004**: Tách rời ranh giới trách nhiệm giữa Jenkins (CI) và Temporal (CD).
- **ADR-005**: Mở rộng hỗ trợ đa nền tảng runtime: Docker, Kubernetes, Systemd.
- **ADR-006**: Quản lý cấu hình Jenkins hoàn toàn dưới dạng mã nguồn (JCasC).
- **ADR-007**: Cách ly tiến trình build bằng Ephemeral Agent dùng một lần.
- **ADR-008**: Bất biến hóa Artifact, SBOM, quét Trivy và ký số Cosign.
- **ADR-009**: Chịu lỗi bằng mô hình nhiều Jenkins Controller (Multi-Controller).
- **ADR-010**: Phân định rõ ràng giữa chế độ chạy Local Dev và chế độ Production.
- **ADR-011**: Thiết kế Seam xác thực người dùng (OIDC, Token tĩnh).
- **ADR-012**: Mô hình phân quyền theo đội ngũ sở hữu ứng dụng (Team Ownership).
- **ADR-013**: Tính toán 4 chỉ số DORA trung thực từ các sự kiện gốc (Delivery Events).
- **ADR-014**: Chuyển đổi toàn diện sang PostgreSQL Canonical State (Phase 1).
- **ADR-015**: Thiết kế định danh máy móc Workload Identity và ranh giới tham số (Phase 2).
- **ADR-016**: Cơ chế khóa Deployment Leases, Fencing Token và Rollback 2 pha (Phase 3).
- **ADR-017**: Khóa bất biến Release Version và diễn tập kiểm định sao lưu (Phase 4).
- **ADR-018**: Cảm biến sức khỏe trung thực và khung nghiệm thu Production Harness (Phase 5).
- **ADR-019**: Tiếp nhận Webhook SCM an toàn, xác thực HMAC và chặn Replay (Phase 6).
- **ADR-020**: Vòng đời Pipeline, hủy bỏ thực sự, thử lại giữ nguyên phả hệ và Reconciler (Phase 7).
- **ADR-021**: Quản lý phiên bản cấu hình môi trường và liên kết DCIM (Phase 8).
- **ADR-022**: Đo lường Prometheus, Transactional Outbox, Connection Pool và DR Drill (Phase 9).
- **ADR-023**: Kế hoạch phát hành đa module theo đồ thị DAG và Canary tiệm tiến (Phase 10).
- **ADR-024**: Quản trị chính sách OPA Rego, Phá kính khẩn cấp Dual-Control và Admission (Phase 11).
- **ADR-025**: Danh bạ dịch vụ Self-Service, Golden Path Template, Môi trường Preview và Quota (Phase 12).
- **ADR-026**: Bộ kiểm định Production Readiness Audit và chứng nhận hoàn thiện nền tảng (Phase 13).

---

## 6. CƠ SỞ DỮ LIỆU — TOÀN BỘ 34 BẢNG QUA 18 MIGRATION

### 6.1. Bảng tóm tắt 18 Migration Scripts

Toàn bộ schema được xây dựng qua 18 bước bất biến, kiểm soát bằng bảng `schema_migrations`:

| Migration | Tên kịch bản | Nội dung bổ sung chính |
| :---: | :--- | :--- |
| **0001** | `0001_baseline.sql` | Các bảng nền tảng: `applications`, `modules`, `pipeline_runs`, `deployments`, `systems`, `audit_events`, `idempotency_records`. |
| **0002** | `0002_delivery_events_and_concurrency.sql` | Bảng `delivery_events`, `pipeline_logs`, cột `version` phục vụ khóa lạc quan (CAS). |
| **0003** | `0003_pipeline_run_actor.sql` | Cột `started_by` ghi nhận danh tính người bấm trigger. |
| **0004** | `0004_application_ownership.sql` | Cột `owner_team` và siêu dữ liệu đội ngũ sở hữu ứng dụng. |
| **0005** | `0005_security_evidence.sql` | Bảng `security_evidence` lưu kết quả SBOM, quét CVE và chữ ký. |
| **0006** | `0006_production_request_completion.sql` | Cột theo dõi trạng thái hoàn tất của yêu cầu triển khai Production. |
| **0007** | `0007_production_request_idempotency.sql` | Cột `idempotency_key` chống bấm duyệt deploy 2 lần. |
| **0008** | `0008_system_unknown_status.sql` | Trạng thái mặc định `UNKNOWN` cho hệ thống mới tạo. |
| **0009** | `0009_callback_token_use.sql` | Bảng `callback_token_uses` lưu vết mã jti của token một lần. |
| **0010** | `0010_deployment_leases_and_fencing.sql` | Bảng `deployment_leases`, `deployment_fencing_counters`, `pipeline_log_sequences`. |
| **0011** | `0011_release_immutability_and_ci_reports.sql` | Bảng `release_versions` bất biến và bảng báo cáo `version_ci_reports`. |
| **0012** | `0012_scm_integrations_and_webhooks.sql` | Bảng cấu hình `scm_integrations` và bảng deduplicate `scm_webhook_deliveries`. |
| **0013** | `0013_pipeline_lifecycle_and_stage_events.sql` | Bảng chi tiết giai đoạn `pipeline_stages`, cột phả hệ `retry_of`. |
| **0014** | `0014_versioned_config_revisions_and_dcim.sql` | Bảng lịch sử cấu hình `module_config_revisions`, bảng sức khỏe `server_health_records`. |
| **0015** | `0015_observability_and_notifications.sql` | Bảng Transactional Outbox `notifications`, các chỉ mục Cursor Pagination. |
| **0016** | `0016_multi_module_dag_and_progressive_delivery.sql` | Bảng liên kết `production_request_modules` quản lý phát hành theo đồ thị DAG. |
| **0017** | `0017_policy_engine_governance_and_admission.sql` | Bảng quyết định `policy_decisions`, bảng xin quyền khẩn cấp `break_glass_requests`. |
| **0018** | `0018_service_catalog_and_self_service.sql` | Bảng danh bạ `catalog_services`, `catalog_service_dependencies`, `catalog_templates`, `preview_environments`, `resource_quotas`, `resource_requests`. |

---

### 6.2. Danh bạ 34 bảng dữ liệu và lý do tồn tại

Dưới đây là bảng phân loại đầy đủ 34 bảng đang chạy trên PostgreSQL canonical:

1. **`applications`**: Định nghĩa ứng dụng logic (tập hợp các module).
2. **`modules`**: Đơn vị triển khai độc lập nhỏ nhất (Backend API, Web Frontend, Worker).
3. **`pipeline_runs`**: Lịch sử từng lần chạy CI/CD cụ thể.
4. **`pipeline_stages`**: Từng giai đoạn trong một lần chạy (Checkout, Test, Build, Scan, Sign).
5. **`pipeline_logs`**: Từng dòng log xuất ra từ quá trình build/deploy.
6. **`pipeline_log_sequences`**: Bộ đếm cấp phát số thứ tự an toàn cho dòng log, chống xung đột đua luồng.
7. **`deployments`**: Bản ghi từng lần triển khai artifact lên một môi trường cụ thể.
8. **`deployment_leases`**: Hợp đồng giữ chỗ độc quyền cụm máy chủ đích, chống 2 người deploy đè lên nhau.
9. **`deployment_fencing_counters`**: Bộ đếm đơn điệu thế hệ deployment, loại bỏ báo cáo từ worker lỗi thời.
10. **`delivery_events`**: Sổ cái sự kiện bất biến (Commit, Deploy, Rollback), nguồn dữ liệu tính DORA Metrics.
11. **`release_versions`**: Bản ghi phiên bản phát hành bất biến gắn liền với SHA-256 digest của artifact.
12. **`version_ci_reports`**: Báo cáo CI gốc kèm theo phiên bản phát hành.
13. **`security_evidence`**: Hồ sơ bằng chứng bảo mật: file SBOM, danh sách CVE từ Trivy và chữ ký Cosign.
14. **`security_exceptions`**: Giấy phép miễn trừ tạm thời cho các CVE đã biết nhưng chưa kịp vá, có ngày hết hạn.
15. **`production_requests`**: Phiếu yêu cầu đưa bản build lên môi trường Production.
16. **`production_request_modules`**: Danh sách các module và mối quan hệ phụ thuộc trong một đợt phát hành DAG.
17. **`policy_decisions`**: Lịch sử các quyết định phê duyệt hoặc từ chối của bộ máy OPA Rego.
18. **`break_glass_requests`**: Hồ sơ các lần "đập hộp kính khẩn cấp" có chữ ký 2 người độc lập.
19. **`callback_token_uses`**: Lưu vết mã jti của token máy móc đã sử dụng, chống tấn công phát lại (Replay Attack).
20. **`audit_events`**: Nhật ký kiểm toán bất biến: Ai làm gì, lúc nào, trên tài nguyên nào, từ địa chỉ IP nào.
21. **`idempotency_records`**: Bảng ghi nhận khóa lũy đẳng, chống bấm nút 2 lần gây nhân đôi tài nguyên.
22. **`scm_integrations`**: Cấu hình kết nối kho mã nguồn GitHub / GitLab cho từng ứng dụng.
23. **`scm_webhook_deliveries`**: Lưu vết mã Delivery ID của webhook để deduplicate chính xác.
24. **`module_config_revisions`**: Quản lý lịch sử các phiên bản biến môi trường và file cấu hình của từng module.
25. **`systems`**: Khái niệm hệ thống cấp cao nhóm các module lại với nhau trên giao diện Portal.
26. **`server_health_records`**: Nhật ký thăm dò sức khỏe định kỳ của các máy chủ hạ tầng.
27. **`notifications`**: Hàng đợi Transactional Outbox gửi thông báo ra webhook ngoài (Slack/Teams).
28. **`catalog_services`**: Danh bạ các dịch vụ nội bộ trong công ty (Service Catalog).
29. **`catalog_service_dependencies`**: Đồ thị quan hệ phụ thuộc giữa các dịch vụ trong danh bạ.
30. **`catalog_templates`**: Kho lưu trữ các template mẫu chuẩn mực (Golden Path Templates).
31. **`preview_environments`**: Quản lý vòng đời và thời gian tự hủy của môi trường xem trước.
32. **`resource_quotas`**: Định mức tài nguyên tối đa (CPU, RAM, số module) của từng phòng ban/team.
33. **`resource_requests`**: Phiếu tự yêu cầu cấp phát tài nguyên hạ tầng (Database, Redis, S3).
34. **`schema_migrations`**: Sổ cái ghi nhận các file migration SQL đã chạy thành công kèm mã băm SHA-256.

---

## 7. HƯỚNG DẪN THIẾT LẬP HẠ TẦNG & CÁCH CHẠY DỰ ÁN

### 7.1. Cần cài đặt những gì?

Để chạy toàn bộ netCI, máy tính hoặc máy chủ của bạn cần:
1. **Hệ điều hành**: Linux (Ubuntu 22.04 / 24.04 LTS khuyên dùng) hoặc Windows WSL2.
2. **Python**: Phiên bản `3.12+` (kèm module `python3-venv`).
3. **Node.js**: Phiên bản `20+` kèm `npm` (để chạy giao diện Portal).
4. **Docker**: Docker Engine 24+ (đã cấu hình chạy không cần gõ `sudo`).
5. **PostgreSQL**: Phiên bản 16 (chạy qua Docker hoặc cài trực tiếp).

---

### 7.2. Hướng dẫn chạy nhanh cục bộ (Local Development)

Nếu bạn chỉ muốn mở code lên vọc thử nghiệm mà không cần cài đặt database phức tạp:

```bash
# 1. Di chuyển vào thư mục dự án
cd /home/deployer/netci-delivery-platform

# 2. Tạo và kích hoạt môi trường ảo Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

# 3. Chạy Backend API (Chế độ In-Memory, bật dữ liệu mẫu)
export NETCI_ENVIRONMENT=local
export NETCI_DEMO_DATA=true
export NETCI_ALLOWED_ORIGINS=http://localhost:5173
PYTHONPATH=backend uvicorn app.main:app --port 8000 --reload

# 4. Mở cửa sổ terminal khác để chạy Frontend Web Portal
cd frontend
npm install
npm run dev
```
👉 Mở trình duyệt tại `http://localhost:5173` để thấy Portal hoạt động ngay lập tức với dữ liệu mẫu!

---

### 7.3. Hướng dẫn chạy chuẩn với Docker & PostgreSQL 16 (Production Mode)

Đây là cách chạy chuẩn mực sát với môi trường doanh nghiệp nhất:

```bash
# Bước 1: Khởi động container PostgreSQL 16 trên cổng 55432
docker run -d \
  --name netci-p0-pg \
  --restart unless-stopped \
  -e POSTGRES_USER=netci \
  -e POSTGRES_PASSWORD=netci-local-only \
  -e POSTGRES_DB=netci \
  -p 55432:5432 \
  postgres:16.15-alpine3.24

# Bước 2: Thiết lập biến môi trường trỏ vào Database thật
export DATABASE_URL="postgresql://netci:netci-local-only@127.0.0.1:55432/netci"
export NETCI_TEST_DATABASE_URL="postgresql://netci:netci-local-only@127.0.0.1:55432/netci"
export NETCI_WORKLOAD_TOKEN_KEYS="k1:netci-ultra-secure-workload-signing-secret-key-32b-minimum"
export NETCI_BACKUP_ENCRYPTION_KEY="netci-production-aes-256-gcm-master-key-32b-length"

# Bước 3: Thực thi 18 SQL Migration để kiến tạo 34 bảng
.venv/bin/python scripts/migrate.py
.venv/bin/python scripts/migrate.py --check-schema   # Xác thực schema hoàn toàn khớp

# Bước 4: Khởi động Backend API
PYTHONPATH=backend .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Bước 5: Build và chạy Frontend Portal
cd frontend
npm run build    # Biên dịch mã nguồn thành file tĩnh tối ưu
npm run preview  # Chạy server phục vụ giao diện
```

---

### 7.4. Bảng giải thích toàn bộ biến môi trường (`.env`)

| Tên biến | Bắt buộc? | Ý nghĩa & Giá trị mẫu |
| :--- | :---: | :--- |
| `NETCI_ENVIRONMENT` | Có | Môi trường chạy: `local`, `staging`, hoặc `production`. |
| `DATABASE_URL` | Có (trừ local) | Chuỗi kết nối PostgreSQL: `postgresql://user:pass@host:port/dbname`. |
| `NETCI_WORKLOAD_TOKEN_KEYS` | Có (trừ local) | Cặp khóa định danh ký HMAC cho token máy móc, dạng `k1:<chuỗi_ngẫu_nhiên_32_ký_tự>`. |
| `NETCI_BACKUP_ENCRYPTION_KEY` | Tùy chọn | Khóa bí mật dùng để mã hóa file backup theo thuật toán AES-256-GCM. |
| `NETCI_AUTH_MODE` | Có | Cơ chế xác thực người dùng: `none` (chỉ cho localhost), `token` (file mã băm), hoặc `oidc` (đăng nhập bằng tài khoản công ty Okta/Keycloak). |
| `NETCI_OIDC_ISSUER` | Khi mode=oidc | Đường dẫn máy chủ phát hành JWT, ví dụ `https://keycloak.congty.vn/realms/master`. |
| `NETCI_CI_MODE` | Có | Bộ máy CI: `none`, `local` (giả lập), hoặc `jenkins` (kết nối cụm Jenkins thật). |
| `NETCI_CD_MODE` | Có | Bộ máy CD: `none`, `local` (giả lập), hoặc `temporal` (kết nối cụm Temporal cluster). |
| `NETCI_SIGNATURE_VERIFY_MODE`| Có | Kiểm tra chữ ký: `none` hoặc `cosign` (bắt buộc ảnh container phải được ký số). |
| `NETCI_DCIM_BASE_URL` | Tùy chọn | Địa chỉ API của hệ thống quản lý máy chủ NetBox/DCIM. |
| `NETCI_ALLOWED_ORIGINS` | Có | Danh sách domain được phép gọi API (CORS): `http://localhost:5173,https://portal.congty.vn`. |

---

## 8. TIẾN ĐỘ & KẾT QUẢ KIỂM CHỨNG THỰC TẾ

### 8.1. Bảng tổng kết tiến độ 13/13 Phase

| Giai đoạn | Tên Phase & Nội dung chuyên môn | Trạng thái kỹ thuật |
| :---: | :--- | :---: |
| **Phase 1** | PostgreSQL Canonical State, Xóa bỏ nạp RAM, Onboarding Atomic | ✅ **HOÀN THÀNH** |
| **Phase 2** | Workload Identity, Scoped Machine Token, Biên giới Build Input | ✅ **HOÀN THÀNH** |
| **Phase 3** | Deployment Leases, Fencing Token Counter, Rollback 2 pha | ✅ **HOÀN THÀNH** |
| **Phase 4** | Release Version bất biến, Dọn sạch dữ liệu giả, Backup verification | ✅ **HOÀN THÀNH** |
| **Phase 5** | Readiness trung thực (/livez, /readyz), Production Acceptance Harness | ✅ **HOÀN THÀNH** |
| **Phase 6** | SCM Webhook Ingestion, HMAC-SHA256 Signatures, Replay Prevention | ✅ **HOÀN THÀNH** |
| **Phase 7** | Pipeline Stage Event Log, Lineage Retries, Watchdog Reconciler | ✅ **HOÀN THÀNH** |
| **Phase 8** | Versioned Config Revisions, Audit Trail, DCIM Fail-Closed Circuit | ✅ **HOÀN THÀNH** |
| **Phase 9** | Prometheus Metrics (/metrics), Structured JSON Log, Transactional Outbox | ✅ **HOÀN THÀNH** |
| **Phase 10** | Multi-Module DAG Engine (Kahn's Sort), Progressive Canary Delivery | ✅ **HOÀN THÀNH** |
| **Phase 11** | Policy-as-Code (OPA Rego), Dual-Control Break-Glass, Admission Controller | ✅ **HOÀN THÀNH** |
| **Phase 12** | Self-Service Catalog, Golden Path Templates, Preview Envs, Quotas | ✅ **HOÀN THÀNH** |
| **Phase 13** | Production Readiness Audit Harness (28/28 checks), Certification | ✅ **HOÀN THÀNH** |

---

### 8.2. Các con số kiểm thử đo được thực tế

Toàn bộ các bộ test đã được thực thi và chứng thực trực tiếp trên máy chủ với cơ sở dữ liệu PostgreSQL 16 thật:

- **Bộ test Backend & Integration**: **451 tests PASSED** (0 failures, 19.00 giây).
- **Bộ test PostgreSQL Durability & Leases**: **63 tests PASSED** (100% pass với PostgreSQL thật, 29.19 giây).
- **Bộ test Frontend (Vitest)**: **26 tests PASSED** trên toàn bộ 8 file test component.
- **Biên dịch Frontend**: `tsc -b && vite build` hoàn tất sạch sẽ không một lỗi trong **197 mili-giây**.
- **Diễn tập thảm họa (DR Drill)**: Tạo backup mã hóa AES-256-GCM, khôi phục vào scratch database, kiểm tra toàn bộ 34 bảng, row counts, checksums và khóa ngoại hoàn tất thành công trong **4 giây** (nhờ cải tiến batch UNION ALL).

---

### 8.3. Kết quả chứng nhận Production Readiness Audit (28/28 checks)

Kịch bản thẩm định cấp chứng chỉ [`scripts/production_readiness_audit.py`](file:///home/deployer/netci-delivery-platform/scripts/production_readiness_audit.py) đã chạy và cho kết quả tuyệt đối:

```text
================================================================================
Starting netCI Automated Production Readiness & Platform Certification Audit
================================================================================
Phase 1  [✓] 18 ordered SQL migrations exist in backend/migrations -> PASS
Phase 1  [✓] CRITICAL_TABLES includes all 34 canonical domain tables -> PASS
Phase 1  [✓] All 34 critical tables exist on canonical PostgreSQL instance -> PASS
Phase 2  [✓] Workload Identity mints and verifies scoped machine token -> PASS
Phase 2  [✓] Jenkins workload scope does not permit 'deployment:result' -> PASS
Phase 2  [✓] Deployment-controlled keys rejected from pipeline input -> PASS
Phase 3  [✓] Pipeline cannot skip directly from QUEUED to SUCCEEDED -> PASS
Phase 3  [✓] Deployment cannot jump directly from PENDING to HEALTHY -> PASS
Phase 3  [✓] Rollback requires intermediate ROLLBACK_IN_PROGRESS state -> PASS
Phase 4  [✓] Zero unauthorized mock/fixture/fake fallbacks in runtime code -> PASS
Phase 5  [✓] Unconfigured external dependencies report 'not_configured' -> PASS
Phase 6  [✓] SCM Provider enforces HMAC-SHA256 signature verification -> PASS
Phase 7  [✓] Reconciler watchdog exists for out-of-band state recovery -> PASS
Phase 8  [✓] DCIM catalog reports unconfigured and fails closed -> PASS
Phase 9  [✓] MetricsRegistry exports valid Prometheus metrics -> PASS
Phase 10 [✓] Kahn's DAG algorithm computes correct deployment waves -> PASS
Phase 10 [✓] Topological sorter detects and rejects circular dependencies -> PASS
Phase 10 [✓] CanaryAnalyzer approves healthy metrics and rejects on SLO breaches -> PASS
Phase 11 [✓] RiskCalculator produces bounded, auditable risk score (0-100) -> PASS
Phase 11 [✓] BreakGlassService strictly enforces dual-control -> PASS
Phase 11 [✓] AdmissionController denies mutable ':latest' tag on prod -> PASS
Phase 12 [✓] CatalogServiceManager DFS rejects circular dependencies -> PASS
Phase 12 [✓] PipelineTemplateEngine provides pre-seeded golden path templates -> PASS
Phase 12 [✓] PreviewEnvironmentManager clamps excessive TTL to max 72h -> PASS
Phase 12 [✓] SelfServiceResourceManager returns 'provider_not_configured' -> PASS
Phase 13 [✓] Authoritative OpenAPI 3.1.0 contract file exists -> PASS
Phase 13 [✓] release-checklist.yaml exists with defined release profile gates -> PASS
Phase 13 [✓] ADR corpus contains comprehensive architecture decisions (>= 25) -> PASS
================================================================================
Audit Complete: 28/28 checks passed.
Final Platform Verdict: CERTIFIED
================================================================================
```

---

## 9. CÁCH TỰ KIỂM CHỨNG TOÀN BỘ HỆ THỐNG

Bạn có thể tự tay gõ các lệnh sau trên terminal để kiểm chứng rằng mọi thứ đều là sự thật:

```bash
cd /home/deployer/netci-delivery-platform

# 1. Chạy bài thi kiểm định 28 tiêu chí lấy chứng chỉ CERTIFIED
.venv/bin/python scripts/production_readiness_audit.py --database-url "postgresql://netci:netci-local-only@127.0.0.1:55432/netci"

# 2. Chạy diễn tập thảm họa (Disaster Recovery Drill)
.venv/bin/python scripts/netci_dr_drill.py --database-url "postgresql://netci:netci-local-only@127.0.0.1:55432/netci"

# 3. Chạy toàn bộ 451 unit và integration tests
.venv/bin/pytest backend/tests/ tests/ -q

# 4. Chạy toàn bộ 63 bài test độ bền với PostgreSQL thật
NETCI_TEST_DATABASE_URL="postgresql://netci:netci-local-only@127.0.0.1:55432/netci" .venv/bin/pytest backend/tests/test_persistence_postgres.py backend/tests/test_backup_restore.py backend/tests/test_deployment_leases.py -q

# 5. Chạy bài test giao diện Frontend
npm --prefix frontend test

# 6. Kiểm tra các cổng phát hành và tính tương thích
.venv/bin/python scripts/validate_release.py
.venv/bin/python scripts/validate_platform.py
.venv/bin/python scripts/validate_catalog.py
.venv/bin/python scripts/validate_windows.py
node scripts/validate_oss_readiness.mjs
```

---

## 10. RỦI RO THỰC TẾ & NHỮNG VIỆC CẦN LÀM KHI ĐƯA VÀO DOANH NGHIỆP

### 10.1. Nền tảng đã đạt đến đâu?
- **Về mặt mã nguồn (Code-Complete)**: **ĐẠT 100%**. Toàn bộ kiến trúc IDP, kiểm soát an toàn, máy trạng thái, giải thuật đồ thị DAG, chính sách OPA và cổng API đã hoàn thiện đầy đủ, không còn thiếu một dòng code nghiệp vụ nào.

### 10.2. Khi đưa vào công ty thật thì cần cấu hình thêm những gì?
Vì netCI tuân thủ tuyệt đối nguyên tắc **Fail-Closed** và **Không bao giờ hiển thị màu xanh giả**, nên khi mang sang máy chủ của công ty bạn, bạn chỉ cần điền các thông số kết nối tới các dịch vụ thật của công ty:

1. **Máy chủ xác thực người dùng (IdP / IAM)**:
   - Nếu công ty dùng Okta, Azure AD hoặc Keycloak: Đặt `NETCI_AUTH_MODE=oidc` và điền `NETCI_OIDC_ISSUER`. Hệ thống sẽ tự động chuyển sang đọc token JWT của công ty.
2. **Hạ tầng CI thật (Jenkins)**:
   - Nếu muốn Jenkins thật chạy build thay vì bộ giả lập nội bộ: Điền `NETCI_CI_MODE=jenkins`, `NETCI_JENKINS_BASE_URL` và token kết nối.
3. **Cụm Kubernetes thật**:
   - Gắn file `kubeconfig` của cụm k8s vào thư mục chỉ định (`NETCI_KUBECONFIG_DIR`) để Admission Controller và Temporal triển khai pod thật.
4. **Cổng Webhook Slack/Teams**:
   - Điền URL webhook của kênh thông báo công ty vào cấu hình outbox để nhân viên nhận tin nhắn tức thì khi deploy thành công.

---
*Bản quyền tài liệu thuộc về dự án netCI Delivery Platform — Cập nhật ngày 05/09/2026.*
