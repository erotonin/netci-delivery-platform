# SLO và cảnh báo cho chính netCI

Bật bằng `monitoring.prometheusRule.enabled=true` trong chart. Cần Prometheus Operator, vì
chart render ra một `PrometheusRule`. File nguồn:
`deploy/helm/netci-platform/templates/prometheusrule.yaml`.

## Mục tiêu

| SLO | Mặc định | Đo bằng |
|---|---|---|
| API trả lời không lỗi 5xx | 99,5 % trong 30 ngày | `netci_http_requests_total{status_code}` |
| Độ trễ p95 của API | ≤ 1 s | `netci_http_request_duration_seconds` |

Các probe `/livez`, `/healthz`, `/readyz` và `/metrics` **không** được tính. Chúng được gọi
vài giây một lần và luôn thành công, nên nếu tính vào thì lỗi thật của API sẽ bị lấp đi.

99,5 % tương đương khoảng 3,6 giờ lỗi mỗi tháng. Con số này hợp với một nền tảng nội bộ,
nơi một lần API gián đoạn thường chỉ làm chậm việc deploy chứ không làm sập ứng dụng đang
chạy. Chỉnh `monitoring.slo.availabilityTarget` nếu tổ chức cần mức khác; mọi ngưỡng cảnh
báo đều được tính lại từ con số này.

## Cảnh báo

| Cảnh báo | Mức | Ý nghĩa |
|---|---|---|
| NetciApiAvailabilityBurnFast | page | Tốc độ đốt error budget gấp 14,4 lần, đo trên cả cửa sổ 1 giờ và 5 phút: cứ đà này thì hết budget tháng trong khoảng 2 ngày |
| NetciApiAvailabilityBurnSlow | ticket | Gấp 6 lần, đo trên cả 6 giờ và 30 phút: budget đang bị ăn dần |
| NetciApiLatencyHigh | ticket | p95 vượt ngưỡng liên tục 15 phút |
| NetciNotReady | page | Không replica nào báo ready |
| NetciNoCiController | page | Không Jenkins nào gọi được, nên không build nào chạy được |
| NetciNoCdPoller | page | Không worker nào nhận việc từ Temporal, nên không deploy nào chạy được |
| NetciOutboxBacklog | ticket | Hàng đợi thông báo và commit status dồn lại |
| NetciOutboxDeadLetters | ticket | Có thông báo bị bỏ sau khi đã hết số lần thử |
| NetciReconcilerCorrecting | ticket | Reconciler liên tục phải sửa trạng thái, tức là callback đang bị mất |

Mô tả của từng cảnh báo trong rule ghi rõ việc cần kiểm tra đầu tiên.

## Đã kiểm chứng tới đâu

- `promtool check rules` trên rule đã render: 15 rule hợp lệ (Prometheus v3.5.0 của lab).
- Contract test bảo đảm mỗi biểu thức chỉ dùng metric mà netCI thật sự export, và ngưỡng
  burn rate đi theo `availabilityTarget`.
- **Chưa có cảnh báo nào từng được quan sát bắn thật** trong lab: lab dùng Prometheus độc
  lập, không có Prometheus Operator để nạp `PrometheusRule`.
