#!/usr/bin/env python3
"""Builds docs/DOC-THEO-THU-TU.md: everything a reader needs, in the order to read it, in one file.

    python3 scripts/docs/reading_order.py

It is generated from the documents it gathers (the Vietnamese account, the ADRs, the defense pack and
the demo guide), so it never drifts from them: edit those, then run this again.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
TB = DOCS / "TRINH-BAY-TOAN-DIEN-NETCI.md"
OUT = DOCS / "DOC-THEO-THU-TU.md"


def sections(path: Path) -> dict[str, str]:
    """The document's "## " sections, keyed by their number ("1".."18", "11b") or their title."""
    out: dict[str, str] = {}
    parts = re.split(r"(?m)^(?=## )", path.read_text())
    for part in parts[1:]:
        title = part.splitlines()[0][3:]
        m = re.match(r"(\d+[a-z]?)\.", title)
        out[m.group(1) if m else title] = part.rstrip().removesuffix("---").rstrip() + "\n"
    return out


def body(path: Path) -> str:
    """A document without its title and the lines before its first section."""
    text = path.read_text()
    return text[text.index("\n## "):].strip() + "\n"


def demoted(path: Path) -> str:
    """An ADR with every heading one level down, so that it sits inside a step; a "#" line inside a
    code block is a comment, not a heading, and is left as it is."""
    lines, fenced = [], False
    for line in path.read_text().strip().splitlines():
        if line.startswith("```"):
            fenced = not fenced
        lines.append("#" + line if not fenced and re.match(r"#+ ", line) else line)
    return "\n".join(lines) + "\n"


def adr(n: int) -> Path:
    return next((DOCS / "decisions").glob(f"ADR-{n:03d}-*.md"))


def main() -> None:
    tb = sections(TB)
    pick = lambda *keys: "\n".join(tb[k] for k in keys)  # noqa: E731
    adrs = [60, 63, 65, 61, 64, 66, 67, 68, 69, 70, 71, 72, 74, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91]
    steps = [
        ("Bước 1 — Nền tảng", "Tuần 1",
         "Đọc câu chuyện để hiểu vì sao có đề tài, rồi học Jenkins, container, Kubernetes, storage, hệ phân tán, BMC. "
         "Vừa đọc vừa làm bài tập 17.3 số 1–3 (ở phần Tham khảo cuối file). Gặp từ lạ: Phụ lục A ở cuối file.",
         pick("1", "2", "3", "4")),
        ("Bước 2 — Hiểu netCI", "Tuần 2",
         "Yêu cầu, tiêu chí, lựa chọn công nghệ, kiến trúc, các luồng từng giây (phần 8 và 9 là trọng tâm), từng "
         "thành phần, phần IDP (bài toán lớn mà HA là một phần), và active-active: UI/API không bao giờ tắt.",
         pick("5", "6", "7", "8", "9", "10", "11", "11b")),
        ("Bước 3 — Kiểm chứng, bài học, giới hạn", "Tuần 3",
         "Hệ thống đã được chứng minh tới đâu, những lỗi đã tìm ra, và những gì chưa làm.",
         pick("13", "15", "16")),
        ("Bước 4 — Các quyết định kiến trúc (ADR, tiếng Anh)", "Tuần 3",
         "Mỗi ADR ghi một quyết định: bối cảnh, quyết định, bằng chứng, phương án bị loại. Thứ tự: 060 (quan trọng "
         "nhất), 063 (hàng đợi), 065 (netciOnce), 061 và 064 (fabric), rồi 066–072, mỗi cái một bài học cụ thể; "
         "rồi 074 và 076–080: active-active, từ cái gì hỏng khi dùng chung một home tới nhận nuôi build đang chạy và kho HA; "
         "cuối cùng 081–089: đưa lên production (hàng đợi, IDP, backup, fence bằng Nova, một chart, crumb, đổi tên và di chuyển job, view, drain không cắt request, IDP build–deploy thật trên lab HA).",
         "\n".join(demoted(adr(n)) for n in adrs)),
        ("Bước 5 — Đọc code (mở trong trình soạn thảo, đọc kèm test)", "Tuần 3",
         "Không cần đọc hết repo. Bốn chỗ này chứa phần cốt lõi; đọc test trước để biết code phải làm gì.",
         CODE),
        ("Bước 6 — Chuẩn bị bảo vệ", "Tuần 4",
         "Thuộc bảng con số (mục BV2), tập dàn bài (mục BV3), luyện trả lời câu hỏi (mục BV5). Số trong ngoặc như (8.4) là mục của tài liệu trình bày, ở Bước 1–3 phía trên.",
         body(DOCS / "BAO-VE-DE-TAI-NETCI.md")),
        ("Bước 7 — Demo thủ công", "Tuần 4",
         "Đọc, rồi tập trên lab ít nhất hai lần và quay một video dự phòng.",
         body(DOCS / "HUONG-DAN-DEMO-THU-CONG.md")),
        ("Bước 8 — Tự kiểm tra", "Tuần 4",
         "Trả lời không nhìn tài liệu. Câu nào chưa trả lời được thì đọc lại mục ghi trong ngoặc.",
         pick("18")),
        ("Tham khảo — dựng lại, vận hành, lộ trình kỹ năng, thuật ngữ, câu lệnh, bản đồ mã nguồn", "Khi cần",
         "Không phải đọc từ đầu đến cuối: tra khi dựng lab, khi xử lý sự cố, hoặc khi gặp từ lạ.",
         pick("12", "14", "17", "Phụ lục A — Thuật ngữ A–Z", "Phụ lục B — Sổ tay câu lệnh", "Phụ lục C — Bản đồ mã nguồn và ADR")),
    ]
    head = [
        "# netCI — Tài liệu đọc theo thứ tự",
        "",
        "> **File này được sinh tự động** bởi `scripts/docs/reading_order.py` từ: `docs/TRINH-BAY-TOAN-DIEN-NETCI.md`,",
        "> các ADR 060–072 trong `docs/decisions/`, `docs/BAO-VE-DE-TAI-NETCI.md` và `docs/HUONG-DAN-DEMO-THU-CONG.md`.",
        "> Đừng sửa trực tiếp file này: sửa các file gốc rồi chạy lại script.",
        "> Số phần (\"Phần 3\", \"mục 3.7\", \"TB 3.7\") giữ nguyên như trong tài liệu trình bày, nên các tham chiếu chéo",
        "> vẫn đúng. \"TB\" là tài liệu trình bày toàn diện.",
        "",
        "## Lộ trình",
        "",
        "| Bước | Đọc | Khi nào |",
        "|---|---|---|",
    ]
    head += [f"| {i} | {title.split(' — ', 1)[1]} | {when} |" for i, (title, when, _, _) in enumerate(steps[:-1], 1)]
    head += [f"| — | {steps[-1][0].split(' — ', 1)[1]} | {steps[-1][1]} |", "",
             "Nếu ít thời gian: Bước 1, Bước 2 (phần 8 và 9), Bước 6, Bước 7.", "", "---", ""]
    out = "\n".join(head)
    for title, when, intro, content in steps:
        out += f"\n# {title}\n\n> **{when}.** {intro}\n\n{content}\n\n---\n"
    OUT.write_text(out)
    print(f"{OUT.relative_to(ROOT)}: {len(out.splitlines())} dòng")


CODE = """## Bốn chỗ cần đọc trong code

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
"""

if __name__ == "__main__":
    main()
