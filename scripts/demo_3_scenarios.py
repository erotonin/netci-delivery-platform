#!/usr/bin/env python3
"""Ba kịch bản demo chạy trên API netCI thật -- in ra đúng những gì API trả lời, không gì khác.

    NETCI_API_URL=http://127.0.0.1:8100 python3 scripts/demo_3_scenarios.py

1. Cổng chuỗi cung ứng: Kubernetes hỏi netCI có cho một pod chạy không (/admission/validate).
2. Phát hành nhiều module: thứ tự theo đợt (Kahn) và chu trình bị chặn (/release-plans/simulate).
3. Cấu hình có phiên bản: tạo revision, so sánh, rollback, hỏi drift (/modules/{id}/config-revisions).

Bản trước in "Observed: HEALTHY (Probe HTTP 200 OK)", "HTTP 503", "MTTR ~1.2s" và một mã băm cố
định mà không đo gì, gọi ba endpoint không tồn tại (nó chết ở bước đầu với HTTP 404), và in
"ALLOW"/"REJECT" khi API không trả quyết định. Ở đây mỗi bước gọi API, in mã HTTP và các trường của
câu trả lời, rồi so với điều kịch bản mong đợi. Điều gì máy này không có để chứng minh (một artifact
đã build và ký thật, một runtime để đo) thì nói là không có, kèm nơi nó đã được chứng minh trên lab.
Thoát mã 1 nếu có bước không như mong đợi.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

API_URL = os.getenv("NETCI_API_URL", "http://127.0.0.1:8100").rstrip("/")
RESULTS: list[tuple[str, bool]] = []


def call(method: str, path: str, body: object | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API_URL}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        raw = err.read().decode()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, {"raw": raw[:300]}


def header(title: str) -> None:
    print("\n" + "=" * 76 + f"\n  {title}\n" + "=" * 76)


def step(name: str, detail: str) -> None:
    print(f"\n▶ [{name}] {detail}")


def show(code: int, body: object, *fields: str) -> None:
    """The HTTP status and the named fields, exactly as the API sent them."""
    print(f"  HTTP {code}")
    if isinstance(body, dict):
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
        for key in fields or tuple(detail)[:6]:
            if key in detail:
                print(f"    {key}: {json.dumps(detail[key], ensure_ascii=False)[:220]}")


def expect(what: str, ok: bool) -> None:
    RESULTS.append((what, ok))
    print(f"  {'✓ đúng như mong đợi' if ok else '✗ KHÔNG như mong đợi'}: {what}")


def not_shown_here(what: str, where: str) -> None:
    print(f"  ○ Máy này không chứng minh được: {what}\n    Đã chứng minh trên lab: {where}")


def items_of(body: object) -> list:
    return body if isinstance(body, list) else (body.get("items", []) if isinstance(body, dict) else [])


def pod_review(namespace: str, image: str) -> dict:
    return {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview",
            "request": {"uid": secrets.token_hex(8), "namespace": namespace,
                        "object": {"metadata": {"name": "demo-pod"}, "spec": {"containers": [{"name": "app", "image": image}]}}}}


def admission(namespace: str, image: str) -> tuple[int, dict]:
    code, body = call("POST", "/admission/validate", pod_review(namespace, image))
    response = body.get("response", {}) if isinstance(body, dict) else {}
    print(f"  HTTP {code}  allowed={response.get('allowed')}\n    message: {response.get('status', {}).get('message')}")
    return code, response


# -----------------------------------------------------------------------------
def scenario_1() -> None:
    header("Kịch bản 1: Cổng chuỗi cung ứng -- pod nào được chạy ở production")
    print("Kubernetes gửi mỗi pod tới netCI trước khi chạy. netCI chỉ cho chạy ở prod image được")
    print("ghim bằng digest, do chính netCI build, có bằng chứng an ninh đạt chính sách.")

    step("1.1", "Pod ở namespace prod dùng tag có thể đổi (nginx:latest)")
    code, response = admission("prod", "nginx:latest")
    expect("tag có thể đổi bị từ chối ở prod", code == 200 and response.get("allowed") is False)

    step("1.2", "Pod ở prod, ghim digest, nhưng netCI chưa từng build digest này")
    code, response = admission("prod", "registry.example/app@sha256:" + secrets.token_hex(32))
    expect("digest không có bằng chứng bị từ chối ở prod", code == 200 and response.get("allowed") is False)

    step("1.3", "Một artifact netCI đã build: cho chạy nếu bằng chứng đạt")
    code, runs = call("GET", "/pipeline-runs")
    items = items_of(runs)
    built = [r for r in items if str(r.get("status")).lower() == "succeeded" and r.get("artifactDigest")]
    if not built:
        print(f"  HTTP {code}: {len(items)} pipeline run, không run nào thành công có artifact.")
        not_shown_here("cho phép một image thật đã build, ký và quét",
                       "payments-api build qua Jenkins A/B, cosign verify, attest/verify provenance (ADR-044), "
                       "SBOM + trivy (ADR-045)")
        return
    digest = built[0]["artifactDigest"]
    print(f"  digest {digest[:19]}… của run {str(built[0].get('id', ''))[:8]}")
    code, response = admission("prod", f"registry.example/app@{digest}")
    print("  (Cho phép hay từ chối đều là câu trả lời thật: nó tuỳ bằng chứng an ninh của run đó.)")
    expect("artifact đã build nhận được một quyết định", code == 200 and isinstance(response.get("allowed"), bool))


# -----------------------------------------------------------------------------
def wave_modules(wave: object) -> list[str]:
    if isinstance(wave, list):
        return [str(m if not isinstance(m, dict) else m.get("moduleId")) for m in wave]
    if isinstance(wave, dict):
        return wave_modules(wave.get("modules") or wave.get("moduleIds") or [])
    return [str(wave)]


def scenario_2() -> None:
    header("Kịch bản 2: Phát hành nhiều module -- thứ tự theo đợt và chu trình")
    print("Thuật toán chính production request dùng (Kahn): module chỉ phát hành sau các module nó")
    print("phụ thuộc. /release-plans/simulate chạy nó trên một kế hoạch và không lưu gì.")

    step("2.1", "web phụ thuộc api, api phụ thuộc db; worker phụ thuộc db")
    plan = {"modules": [{"moduleId": "db"}, {"moduleId": "api", "dependencies": ["db"]},
                        {"moduleId": "web", "dependencies": ["api"]}, {"moduleId": "worker", "dependencies": ["db"]}]}
    code, body = call("POST", "/release-plans/simulate", plan)
    show(code, body, "valid", "code", "message")
    waves = [sorted(wave_modules(w)) for w in (body.get("waves") or [])] if isinstance(body, dict) else []
    print("    đợt:", " → ".join("[" + ", ".join(w) + "]" for w in waves) or "(không có)")
    expect("3 đợt: db, rồi api+worker, rồi web", code == 200 and waves == [["db"], ["api", "worker"], ["web"]])

    step("2.2", "a phụ thuộc b, b phụ thuộc c, c phụ thuộc a")
    code, body = call("POST", "/release-plans/simulate", {"modules": [
        {"moduleId": "a", "dependencies": ["b"]}, {"moduleId": "b", "dependencies": ["c"]},
        {"moduleId": "c", "dependencies": ["a"]}]})
    show(code, body, "valid", "code", "cycle", "message")
    cycle = body.get("cycle") if isinstance(body, dict) else None
    expect("chu trình bị chặn và được gọi tên",
           isinstance(body, dict) and body.get("valid") is False and sorted(cycle or []) == ["a", "b", "c"])


# -----------------------------------------------------------------------------
def scenario_3() -> None:
    header("Kịch bản 3: Cấu hình có phiên bản -- revision, so sánh, rollback, drift")
    print("Mỗi thay đổi cấu hình là một revision bất biến; rollback tạo revision mới chép từ bản cũ,")
    print("không xoá lịch sử. Drift so cấu hình đang áp dụng với điều runtime báo về.")

    sys_id = f"demo-cfg-{secrets.token_hex(3)}"
    step("3.1", f"Tạo system {sys_id} và module payment-service (runtime docker, dev)")
    code, body = call("POST", "/systems", {"id": sys_id, "unit": "Demo", "description": "Kịch bản cấu hình"})
    show(code, body, "id", "code", "message")
    module = {"name": f"payment-{secrets.token_hex(2)}", "repositoryUrl": "https://git.example/payment-service.git",
              "pipelineTemplate": "container-ci-cd-v1", "runtime": "docker",
              "deploymentEnvironments": [{"displayName": "Dev", "environment": "dev", "runtime": "docker",
                                          "servers": ["localhost"]}]}
    code, body = call("POST", f"/systems/{sys_id}/modules", module)
    show(code, body, "id", "code", "message")
    mod_id = body.get("id") if isinstance(body, dict) else None
    expect("system và module được tạo", code in (200, 201) and bool(mod_id))
    try:
        if mod_id:
            config_story(mod_id)
    finally:
        if mod_id:
            call("DELETE", f"/modules/{mod_id}")
        call("DELETE", f"/systems/{sys_id}")


def print_revisions(mod_id: str, title: str) -> list:
    code, body = call("GET", f"/modules/{mod_id}/config-revisions")
    revs = items_of(body)
    print(f"\n  {title} (HTTP {code}, {len(revs)} revision):")
    for r in revs:
        print(f"    #{r.get('revisionNumber')} {r.get('status')} {str(r.get('changeSummary') or '')[:50]}")
    return revs


def config_story(mod_id: str) -> None:
    print_revisions(mod_id, "Module bắt đầu với")

    step("3.2", "Đề xuất revision mới: đổi biến môi trường PORT")
    code, body = call("POST", f"/modules/{mod_id}/config-revisions", {
        "changeSummary": "PORT 8080 -> 9999",
        "deploymentConfig": [{"environment": "dev", "displayName": "Dev", "runtime": "docker",
                              "servers": ["localhost"], "env": {"PORT": "9999"}}]})
    show(code, body, "revisionNumber", "status", "code", "message")
    new_rev = body.get("revisionNumber") if isinstance(body, dict) else None
    expect("revision mới được ghi", code in (200, 201) and isinstance(new_rev, int))
    if not isinstance(new_rev, int):
        return

    step("3.3", "So sánh revision 1 với revision mới")
    code, body = call("GET", f"/modules/{mod_id}/config-revisions/diff?fromRev=1&toRev={new_rev}")
    show(code, body)
    expect("so sánh trả về khác biệt giữa hai revision", code == 200)

    step("3.4", "Rollback về revision 1")
    code, body = call("POST", f"/modules/{mod_id}/config-revisions/1/rollback", {})
    show(code, body, "revisionNumber", "status", "code", "message")
    rb = body.get("revisionNumber") if isinstance(body, dict) else None
    expect("rollback tạo một revision mới (không ghi đè)", code in (200, 201) and isinstance(rb, int) and rb > new_rev)
    print_revisions(mod_id, "Lịch sử sau rollback, không bản nào bị xoá")

    step("3.5", "Hỏi drift: cấu hình đang áp dụng so với điều runtime báo về")
    code, body = call("GET", f"/modules/{mod_id}/drift")
    show(code, body, "hasDrift", "unobservedEnvironments", "deploymentDrift", "dcimDrift")
    # Nothing was deployed and no inventory was asked: the honest answer is "not verified".
    expect("chưa triển khai thì drift là 'chưa xác minh' (null), không phải 'đồng bộ'",
           code == 200 and isinstance(body, dict) and body.get("hasDrift") is None)
    not_shown_here("drift và rollback trên một runtime đang chạy",
                   "triển khai payments-api tới netci-prod-02, kiểm tra sau triển khai và rollback khi không có dữ liệu (ADR-046)")


def main() -> int:
    print("=" * 76 + f"\n  netCI -- 3 kịch bản trên API thật ({API_URL})\n" + "=" * 76)
    code, _ = call("GET", "/healthz")
    if code != 200:
        print(f"API không trả lời /healthz (HTTP {code}). Khởi động: scripts/e2e_stack.sh start")
        return 2
    started = time.time()
    for scenario in (scenario_1, scenario_2, scenario_3):
        scenario()
    bad = [what for what, ok in RESULTS if not ok]
    print("\n" + "=" * 76)
    print(f"  {len(RESULTS) - len(bad)}/{len(RESULTS)} bước đúng như mong đợi ({time.time() - started:.1f}s)")
    for what in bad:
        print(f"  ✗ {what}")
    print("=" * 76)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
