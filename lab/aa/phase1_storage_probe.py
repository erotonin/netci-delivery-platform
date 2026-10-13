#!/usr/bin/env python3
"""Active-active, phase 1: what the shared JENKINS_HOME costs, and what it does when the machine
serving it dies (ADR-074).

    python3 lab/aa/phase1_storage_probe.py [--samples 100]

Needs lab/aa/phase0.yaml deployed (namespace aa-lab, a Longhorn RWX home: Longhorn serves it over
NFS from a share-manager pod on one machine).
1. Latency: 4 KiB writes with fsync, every 0.2 s, on the shared home and on the pod's own disk.
2. Failover: a replica on another machine writes to the home every 0.2 s and its UI is asked every
   0.5 s; the machine holding the share-manager loses power (virsh destroy). Measured: how long
   the replica's writes stall, and whether its UI answers meanwhile.
The machine is brought back by the supervisor (lab auto power-on) or by hand at the end.
Evidence: lab/evidence/aa-phase1-storage-<timestamp>.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase0_probe as p0  # noqa: E402

NS = p0.NS
VIRSH = ["virsh", "-c", "qemu:///system"]
WRITER = ("i=0; while true; do s=$(date +%s%N); dd if=/dev/zero of={dir}/.netci-probe bs=4k count=1 conv=fsync status=none "
          "&& echo \"$s $(date +%s%N)\" >> /local/{log}; i=$((i+1)); [ -n \"{n}\" ] && [ $i -ge {n} ] && break; sleep 0.2; done")


def pod(replica: str) -> str:
    return p0.k("-n", NS, "get", "pod", "-l", f"replica={replica}", "-o", "jsonpath={.items[0].metadata.name}")


def sh(podname: str, cmd: str, check: bool = False) -> str:
    return p0.k("-n", NS, "exec", podname, "-c", "jenkins", "--", "sh", "-c", cmd, check=check)


def latencies(podname: str, log: str) -> list[float]:
    out = sh(podname, f"cat /local/{log} 2>/dev/null")
    return [(int(b) - int(a)) / 1e6 for a, b in (l.split() for l in out.splitlines() if len(l.split()) == 2)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=100)
    args = ap.parse_args()
    started = dt.datetime.now(dt.timezone.utc)
    facts: dict = {"scenario": "aa-phase1-storage", "started": started.isoformat(timespec="seconds")}
    p0.k("-n", NS, "scale", "deployment/jenkins-r1", "deployment/jenkins-r2", "--replicas=1")
    r1 = p0.J("r1")
    p0.wait("r1 up", r1.up, 600, 3)
    vol = p0.k("-n", NS, "get", "pvc", "shared-home", "-o", "jsonpath={.spec.volumeName}")
    sm_node = p0.k("-n", "longhorn-system", "get", "pod", f"share-manager-{vol}", "-o", "jsonpath={.spec.nodeName}")
    where = {r: p0.k("-n", NS, "get", "pod", pod(r), "-o", "jsonpath={.spec.nodeName}") for r in ("r1", "r2")}
    # The replica that watches is the one NOT on the share-manager's machine.
    watcher = next((r for r, n in where.items() if n != sm_node), None)
    facts.update({"volume": vol, "shareManagerNode": sm_node, "replicas": where, "watcher": watcher})
    if not watcher:
        raise SystemExit(f"both replicas are on {sm_node}, the share-manager's machine")
    wp = pod(watcher)

    # 1. Latency, shared home against the pod's own disk.
    for where_, d, log in (("shared", "/var/jenkins_home", "lat-shared"), ("local", "/local", "lat-local")):
        sh(wp, f"rm -f /local/{log}; " + WRITER.format(dir=d, log=log, n=args.samples))
        ms = latencies(wp, log)
        facts[f"latency_{where_}_ms"] = {"n": len(ms), "median": round(statistics.median(ms), 2),
                                         "p99": round(sorted(ms)[int(len(ms) * 0.99) - 1], 2), "max": round(max(ms), 2)}
    print("latency", facts["latency_shared_ms"], facts["latency_local_ms"], flush=True)

    # 2. Failover: writes and the UI, while the share-manager's machine is lost.
    # nohup: the loop must outlive this kubectl exec session.
    sh(wp, "rm -f /local/fo; nohup sh -c '" + WRITER.format(dir="/var/jenkins_home", log="fo", n="") + "' >/dev/null 2>&1 &")
    ui: list[tuple[float, int]] = []
    stop = threading.Event()
    port = p0.PORTS[watcher]
    ips = [ip for n, ip in {"netci-lab-1": "192.168.122.211", "netci-lab-2": "192.168.122.212", "netci-lab-3": "192.168.122.213"}.items() if n != sm_node]

    def poll_ui() -> None:
        while not stop.is_set():
            t = time.time()
            try:
                with urllib.request.urlopen(f"http://{ips[0]}:{port}/login", timeout=2) as r:
                    code = r.status
            except Exception:  # noqa: BLE001 - any failure is "not answering"
                code = 0
            ui.append((t, code))
            time.sleep(0.5)

    th = threading.Thread(target=poll_ui, daemon=True)
    th.start()
    time.sleep(10)
    t_loss = time.time()
    subprocess.run([*VIRSH, "destroy", sm_node], check=True, capture_output=True)
    print("power lost:", sm_node, flush=True)
    time.sleep(240)
    stop.set()
    th.join(5)
    lines = sh(wp, "cat /local/fo").splitlines()
    ends = sorted(int(l.split()[1]) / 1e9 for l in lines if len(l.split()) == 2)
    before = [e for e in ends if e < t_loss]
    after = [e for e in ends if e >= t_loss]
    gaps = [b - a for a, b in zip(ends, ends[1:])]
    facts["failover"] = {
        "writesBefore": len(before), "writesAfter": len(after),
        "firstWriteAfterLossSeconds": round(after[0] - t_loss, 1) if after else None,
        "longestWriteGapSeconds": round(max(gaps), 1) if gaps else None,
        "uiPolls": len(ui), "uiFailures": sum(1 for _, c in ui if c != 200),
        "uiLongestOutageSeconds": None,
        "shareManagerNodeAfter": p0.k("-n", "longhorn-system", "get", "pod", f"share-manager-{vol}", "-o", "jsonpath={.spec.nodeName}", check=False),
    }
    longest, start = 0.0, None
    for t, c in ui:
        if c != 200:
            start = start or t
            longest = max(longest, t - start)
        else:
            start = None
    facts["failover"]["uiLongestOutageSeconds"] = round(longest, 1)
    print("failover", facts["failover"], flush=True)
    sh(wp, "pkill -f netci-probe; true")
    # The machine back (the supervisor powers fenced machines on in the lab; this makes sure).
    for _ in range(60):
        if "running" in subprocess.run([*VIRSH, "domstate", sm_node], capture_output=True, text=True).stdout:
            break
        time.sleep(5)
    else:
        subprocess.run([*VIRSH, "start", sm_node], capture_output=True)
    out = p0.EVIDENCE / f"aa-phase1-storage-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
