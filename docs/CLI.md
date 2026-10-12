# netci CLI — Hướng dẫn sử dụng

Công cụ dòng lệnh `netci` giao tiếp với netCI Delivery API.  
Không cần cài đặt thêm thư viện — chỉ cần Python 3.9+.

---

## Cài đặt

Không cần bước cài đặt nào thêm. Chỉ cần Python 3 tiêu chuẩn.

```bash
# Chạy trực tiếp
python3 scripts/netci_cli.py --help

# Hoặc thêm quyền thực thi
chmod +x scripts/netci_cli.py
./scripts/netci_cli.py --help
```

---

## Cấu hình

| Tham số / Biến môi trường | Mặc định | Ý nghĩa |
|---|---|---|
| `--api URL` hoặc `NETCI_API_URL` | `http://127.0.0.1:8000` | URL gốc của API |
| `--token-file PATH` hoặc `NETCI_TOKEN_FILE` | _(không có)_ | Đường dẫn tệp chứa bearer token |
| `NETCI_TOKEN` | _(không có)_ | Bearer token (nếu không dùng tệp) |
| `--json` | _(không có)_ | In JSON thô thay vì bảng |

> **Lưu ý bảo mật:** Token không bao giờ được in ra stdout/stderr và không được truyền qua đối số dòng lệnh.

---

## Các lệnh

### `netci me` — Xem danh tính hiện tại

```bash
netci me
netci me --json
```

---

### `netci modules [--system S]` — Liệt kê module

```bash
# Liệt kê tất cả module trong mọi system
netci modules

# Liệt kê module trong một system cụ thể
netci modules --system payment-system
```

---

### `netci runs MODULE [--limit N]` — Liệt kê lịch sử pipeline

```bash
# Hiển thị 20 run gần nhất của module orders-api
netci runs orders-api --limit 20
```

---

### `netci run MODULE --commit SHA [--branch B] [--env ENV] [--build-only]` — Khởi chạy pipeline

```bash
# Build và deploy lên dev
netci run orders-api --commit abc1234 --branch main --env dev

# Chỉ build, không deploy
netci run orders-api --commit abc1234 --build-only
```

---

### `netci watch RUN_ID [--interval 10]` — Theo dõi tiến trình run

Trả về exit code 0 khi trạng thái là `succeeded`, 1 khi `failed/cancelled/rolled_back`.

```bash
netci watch 550e8400-e29b-41d4-a716-446655440000 --interval 5
```

---

### `netci logs RUN_ID [--tail N]` — Xem log của run

```bash
# Xem 50 dòng cuối
netci logs 550e8400-e29b-41d4-a716-446655440000 --tail 50
```

---

### `netci promote MODULE RUN_ID --env dev|staging` — Promote artifact

Triển khai artifact đã build sang môi trường khác mà không build lại.

```bash
netci promote orders-api 550e8400-e29b-41d4-a716-446655440000 --env staging
```

---

### `netci rules MODULE` — Xem quy tắc delivery

```bash
netci rules orders-api
# Ví dụ kết quả:
# on push main -> deployTo=dev
# on tag v* -> build only
```

---

### `netci versions MODULE` — Liệt kê các phiên bản đã xuất bản

```bash
netci versions orders-api
```

---

### `netci cve ID` — Kiểm tra mức độ ảnh hưởng của lỗ hổng bảo mật

In danh sách module bị ảnh hưởng và khối coverage (bao gồm mọi mục `notCovered`).

```bash
netci cve CVE-2024-12345
```

---

### `netci freezes [--past]` — Liệt kê change freeze

```bash
# Chỉ hiện freeze đang hoạt động
netci freezes

# Bao gồm cả freeze đã hết hạn
netci freezes --past
```

---

### `netci freeze create` — Tạo change freeze

```bash
netci freeze create \
  --name "Đóng băng cuối năm" \
  --start "2026-12-24T00:00:00+07:00" \
  --end   "2026-12-27T08:00:00+07:00" \
  --env prod \
  --env staging \
  --reason "Kỳ nghỉ lễ cuối năm"
```

---

### `netci freeze cancel ID` — Huỷ change freeze

```bash
netci freeze cancel 550e8400-e29b-41d4-a716-446655440000
```
