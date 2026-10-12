#!/usr/bin/env python3
"""A power loss shown live, for an audience: the machine running a cell's controller loses power in
the middle of a build, and the screen narrates, second by second, what netCI and Kubernetes do until
the build carries on and finishes.

    python3 lab/demo/power_loss_demo.py [--cell cell-b] [--duration 120] [--yes] [--arrange | --arrange-only]

Before it starts it checks what a demo needs (every node Ready, a supervisor leader, the cells on
different machines, two cluster DNS servers) and stops if one is missing. Without --yes it waits for
Enter before cutting the power, so the presenter can point at the Jenkins console first. The build is
`once-probe`: a `sh` step inside `netciOnce`, printing "tick N" every second. Nothing here acts in
the supervisor's place: it only causes the fault (virsh destroy) and watches.
Evidence: lab/evidence/demo-power-loss-<timestamp>.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--cell", default="cell-b")
ap.add_argument("--duration", type=int, default=120, help="how long the build's sh step runs, in seconds")
ap.add_argument("--yes", action="store_true", help="cut the power without waiting for Enter")
ap.add_argument("--arrange-only", action="store_true", help="lay the lab out and check it, without cutting any power")
ap.add_argument("--arrange", action="store_true",
                help="first lay the lab out for the demo: the Kata machine free of cells, the two cells on the other two, "
                     "PostgreSQL away from the demo cell's controller (planned restarts, nodes cordoned meanwhile)")
args = ap.parse_args()
os.environ["NETCI_CELL"] = args.cell
os.environ.setdefault("NETCI_CELL_PORT", {"cell-a": "30080", "cell-b": "30081"}.get(args.cell, "30080"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "spike"))
import probe  # noqa: E402

VIRSH = ["virsh", "-c", "qemu:///system"]
k = probe.kubectl
T0 = 0.0


def say(text: str) -> None:
    stamp = f"+{time.monotonic() - T0:5.1f}s" if T0 else "      "
    print(f"{stamp}  {text}", flush=True)


def ready(node: str) -> bool:
    return k("get", "node", node, "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}", check=False) == "True"


def node_of(ns: str, pod: str) -> str:
    return k("-n", ns, "get", "pod", pod, "-o", "jsonpath={.spec.nodeName}", check=False)


def move(ns: str, pod: str, to: str, nodes: list[str]) -> None:
    """A planned restart of ns/pod onto `to`: every other node cordoned until it is Ready there."""
    if node_of(ns, pod) == to:
        return
    print(f"  {ns}/{pod}: → {to}")
    others = [n for n in nodes if n != to]
    old = k("-n", ns, "get", "pod", pod, "-o", "jsonpath={.metadata.uid}")
    try:
        for n in others:
            k("cordon", n)
        k("-n", ns, "delete", "pod", pod, "--wait=false")
        probe.wait(f"{pod} Ready on {to}", lambda: k("-n", ns, "get", "pod", pod, "-o", "jsonpath={.metadata.uid}", check=False) not in ("", old)
                   and "false" not in k("-n", ns, "get", "pod", pod, "-o", "jsonpath={.status.containerStatuses[*].ready}", check=False)
                   and k("-n", ns, "get", "pod", pod, "-o", "jsonpath={.status.containerStatuses[*].ready}", check=False) != "", 600, 2)
    finally:
        for n in others:
            k("uncordon", n, check=False)


def arrange() -> None:
    """The Kata machine free of cells (the untrusted pool runs only there), the cells on the other two, and
    PostgreSQL not on the demo cell's machine: the queue then takes runs throughout the takeover (it refuses
    them while its only database is down, as with any machine that carries it)."""
    nodes = k("get", "nodes", "-o", "jsonpath={.items[*].metadata.name}").split()
    kata = k("get", "nodes", "-l", "katacontainers.io/kata-runtime=true", "-o", "jsonpath={.items[*].metadata.name}").split()
    rest = [n for n in nodes if n not in kata] or nodes
    other = "cell-a" if args.cell == "cell-b" else "cell-b"
    print("== Sắp xếp lab cho demo")
    move(other, "jenkins-0", rest[0], nodes)
    move(args.cell, "jenkins-0", rest[-1], nodes)
    if node_of("netci-system", "netci-pg-0") == rest[-1]:
        move("netci-system", "netci-pg-0", rest[0], nodes)
    probe.wait("warm sandboxes", lambda: all(int(x or 0) >= 1 for x in k(
        "-n", "netci-system", "exec", "netci-pg-0", "--", "psql", "-U", "netci", "-d", "netci", "-tAc",
        "SELECT count(*) FROM sandboxes WHERE state = 'warm' GROUP BY pool ORDER BY pool", check=False).split()) and
        len(k("-n", "netci-system", "exec", "netci-pg-0", "--", "psql", "-U", "netci", "-d", "netci", "-tAc",
              "SELECT DISTINCT pool FROM sandboxes WHERE state = 'warm'", check=False).split()) >= 2, 600, 5)
    print("  ✓ sandbox ấm có ở mọi pool")


def preflight() -> str:
    nodes = k("get", "nodes", "-o", "jsonpath={.items[*].metadata.name}").split()
    problems = [f"{n} chưa Ready" for n in nodes if not ready(n)]
    if not k("-n", "netci-system", "get", "lease", "netci-supervisor", "-o", "jsonpath={.spec.holderIdentity}", check=False):
        problems.append("không có supervisor leader")
    where = {c: k("-n", c, "get", "pod", "jenkins-0", "-o", "jsonpath={.spec.nodeName}", check=False) for c in ("cell-a", "cell-b")}
    if len(set(where.values())) < len(where):
        problems.append(f"hai cell cùng một máy ({where}): kubectl -n cell-a delete pod jenkins-0 rồi chờ")
    dns = set(k("-n", "kube-system", "get", "pods", "-l", "k8s-app=kube-dns", "-o", "jsonpath={.items[*].spec.nodeName}").split())
    if len(dns) < 2:
        problems.append("DNS cụm chỉ trên một máy")
    pg = node_of("netci-system", "netci-pg-0")
    if pg == where[args.cell]:
        print(f"  ! PostgreSQL ở cùng máy {pg} với controller của {args.cell}: trong lúc tiếp quản queue sẽ từ chối run "
              "(nó chỉ có một CSDL). Chạy với --arrange để tách ra.")
    try:
        firing = sorted({a["labels"]["alertname"] for a in json.loads(subprocess.run(
            ["curl", "-s", "--max-time", "5", f"http://{list(probe.NODES.values())[0]}:30909/api/v1/alerts"],
            capture_output=True, text=True).stdout)["data"]["alerts"]
            if a["state"] == "firing" and a["labels"]["alertname"].startswith("Netci")})
    except Exception:  # noqa: BLE001 - Prometheus is for the audience, not a precondition
        firing = ["(không đọc được Prometheus)"]
    if firing:
        print(f"  ! Cảnh báo đang bắn trước demo: {', '.join(firing)} (xem RUNBOOK; --arrange gỡ phần lớn)")
    for p in problems:
        print("  ✗", p)
    if problems:
        sys.exit("Chưa sẵn sàng để demo.")
    print(f"  ✓ {len(nodes)} node Ready, có supervisor leader, cell ở {where}, DNS trên {len(dns)} máy")
    return where[args.cell]


def main() -> int:
    global T0
    if args.arrange or args.arrange_only:
        arrange()
    print("== Kiểm tra trước demo")
    node = preflight()
    if args.arrange_only:
        print("Lab đã sẵn sàng. Cảnh báo về sandbox hỏng tự hết sau 30 phút kể từ lần hỏng cuối.")
        return 0
    j = probe.Jenkins()
    n = j.trigger("once-probe", {"DURATION": str(args.duration)})
    print(f"== Build once-probe #{n} trên {args.cell} (controller ở {node}); chờ nó chạy ...")
    probe.wait("ticking", lambda: len(probe.ticks(j.console("once-probe", n))) >= 8, 300, 1)
    last = probe.ticks(j.console("once-probe", n))[-1]
    print(f"   build đang chạy, đã tới tick {last}: http://{probe.NODES[node]}:{probe.PORT}/job/once-probe/{n}/console")
    holder0 = k("-n", args.cell, "get", "lease", "jenkins", "-o", "jsonpath={.spec.holderIdentity}")
    epoch0 = k("-n", args.cell, "get", "lease", "jenkins", "-o", "jsonpath={.spec.leaseTransitions}")
    if not args.yes:
        input(f"== Nhấn Enter để RÚT ĐIỆN {node} ...")
    last = probe.ticks(j.console("once-probe", n))[-1]
    subprocess.run([*VIRSH, "destroy", node], check=True, capture_output=True)
    T0 = time.monotonic()
    started_at = dt.datetime.now(dt.timezone.utc)
    say(f"{node} mất điện (virsh destroy). Controller của {args.cell} chết cùng máy.")
    seen: dict[str, float] = {}
    marks: dict[str, float] = {}

    def once(key: str, cond: bool, text: str) -> None:
        if cond and key not in seen:
            seen[key] = time.monotonic()
            marks[key] = round(seen[key] - T0, 1)
            say(text)

    deadline = time.monotonic() + 600
    result = None
    while time.monotonic() < deadline:
        taints = k("get", "node", node, "-o", "jsonpath={.spec.taints[*].key}", check=False)
        holder = k("-n", args.cell, "get", "lease", "jenkins", "-o", "jsonpath={.spec.holderIdentity}", check=False)
        pod = k("-n", args.cell, "get", "pod", "jenkins-0", "-o", "jsonpath={.metadata.uid}|{.spec.nodeName}", check=False).split("|")
        once("released", holder != holder0 and not holder,
             "Supervisor: Lease không được gia hạn 3 s → hỏi BMC (Redfish) → máy Off → nhả Lease (có điều kiện)")
        once("fenced", "node.kubernetes.io/out-of-service" in taints,
             f"Supervisor: gắn taint out-of-service lên {node}: Kubernetes được phép gỡ volume mà không chờ máy chết")
        once("newpod", len(pod) == 2 and pod[0] and pod[0] != holder0.split("/")[-1],
             "Kubernetes: StatefulSet tạo jenkins-0 mới (scheduler đang chọn máy khác)")
        once("placed", "newpod" in seen and len(pod) == 2 and pod[1] not in ("", node),
             f"Kubernetes: jenkins-0 mới được đặt lên {pod[1] if len(pod) == 2 else '?'}; volume JENKINS_HOME được gỡ khỏi máy chết và gắn sang đó")
        once("lease", bool(holder) and holder != holder0,
             f"Cell agent mới giữ Lease (epoch {epoch0} → {k('-n', args.cell, 'get', 'lease', 'jenkins', '-o', 'jsonpath={.spec.leaseTransitions}', check=False)}): chỉ nó được chạy Jenkins")
        if "lease" in seen and "up" not in seen and j.up():
            j.after_restart()
            once("up", True, "Jenkins mới đã lên, đọc lại Pipeline từ JENKINS_HOME")
        if "up" in seen:
            try:
                t = probe.ticks(j.console("once-probe", n))
                once("resumed", bool(t) and t[-1] > last + 1,
                     f"Build #{n} CHẠY TIẾP: controller mới nối lại với bước sh, đã tới tick {t[-1]} (lúc mất điện: tick {last}). "
                     "Bước sh vẫn chạy trên agent suốt lúc controller chết; không tick nào mất")
                b = j.build("once-probe", n)
                if not b["building"] and b.get("result"):
                    result = b["result"]
                    break
            except Exception:  # noqa: BLE001 - Jenkins still coming up
                pass
        time.sleep(0.5)
    text = j.console("once-probe", n) if result else ""
    t = probe.ticks(text)
    missing = sorted(set(range(1, args.duration + 1)) - set(t)) if t else None
    markers = k("-n", "netci-system", "exec", "netci-pg-0", "--", "psql", "-U", "netci", "-d", "netci", "-tAc",
                f"SELECT count(*) FROM once_markers WHERE client = '{args.cell}' AND key = 'deploy' AND scope LIKE '%/once-probe#{n}'",
                check=False).strip()
    say(f"Build #{n}: {result}; tick thiếu: {missing if missing else 'không'}; marker netciOnce: {markers} (phải là 1)")
    facts = {"scenario": "demo-power-loss", "started": started_at.isoformat(timespec="seconds"), "cell": args.cell,
             "node": node, "build": n, "result": result, "secondsAfterPowerLoss": marks, "ticksMissing": missing,
             "onceMarkers": markers}
    out = probe.EVIDENCE / f"demo-power-loss-{started_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(f"evidence: {out.name}")
    print(f"(Máy {node} sẽ được supervisor bật lại; chờ nó Ready trước lần demo sau.)")
    return 0 if result == "SUCCESS" and not missing and markers == "1" else 1


if __name__ == "__main__":
    sys.exit(main())
