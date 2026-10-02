#!/usr/bin/env python3
"""Repeat the unattended power-off takeover (ADR-060) and summarise it.

    lab/spike/chaos_poweroff.py [--runs 3]

Each run waits until the cluster is whole (every node Ready and untainted, every Longhorn volume
healthy, both cells Ready),
then powers off the machine running cell-b's controller under a 240 s build, at a random tick
between 5 and 60, and lets the Cell Supervisor do everything else (probe.py
node-poweroff-supervised). It also records whether the other cell -- on a machine that kept its
power -- restarted its Jenkins or lost its Lease: a failure must never take down a healthy cell.
Evidence: lab/evidence/chaos-poweroff-<timestamp>.json, plus each run's own probe evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "lab" / "evidence"
os.environ.setdefault("NETCI_CELL", "cell-b")
os.environ.setdefault("NETCI_CELL_PORT", "30081")
sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402


def whole() -> bool:
    nodes = json.loads(probe.kubectl("get", "nodes", "-o", "json"))["items"]
    for n in nodes:
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"])
        tainted = any(t["key"] == "node.kubernetes.io/out-of-service" for t in n["spec"].get("taints") or [])
        if not ready or tainted:
            return False
    # A volume still rebuilding a replica has fewer copies than it will have: a power-off then
    # could take the last good one, which is a different experiment from this one.
    robustness = probe.kubectl("-n", "longhorn-system", "get", "volumes.longhorn.io", "-o",
                               "jsonpath={.items[*].status.robustness}", check=False).split()
    if not robustness or any(r != "healthy" for r in robustness):
        return False
    for cell in ("cell-a", "cell-b"):
        if "True" not in probe.kubectl("-n", cell, "get", "pod", "jenkins-0", "-o",
                                       "jsonpath={.status.conditions[?(@.type=='Ready')].status}", check=False):
            return False
    return True


def neighbour() -> tuple[int, str, str]:
    restarts = int(probe.kubectl("-n", "cell-a", "get", "pod", "jenkins-0", "-o",
                                 "jsonpath={.status.containerStatuses[?(@.name=='jenkins')].restartCount}") or 0)
    uid = probe.kubectl("-n", "cell-a", "get", "pod", "jenkins-0", "-o", "jsonpath={.metadata.uid}")
    node = probe.kubectl("-n", "cell-a", "get", "pod", "jenkins-0", "-o", "jsonpath={.spec.nodeName}")
    return restarts, uid, node


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--job", default="resume-probe", choices=["resume-probe", "once-probe"])
    # node-hang-supervised: the machine is suspended, not powered off -- the supervisor must
    # power it off itself, through the power controller.
    ap.add_argument("--failure", default="node-poweroff-supervised", choices=["node-poweroff-supervised", "node-hang-supervised"])
    args = ap.parse_args()
    started = dt.datetime.now(dt.timezone.utc)
    runs = []
    for i in range(args.runs):
        probe.wait("cluster whole", whole, 900, 5)
        time.sleep(30)  # let leases, heartbeats and the supervisor's observation settle
        before = neighbour()
        tick = random.randint(5, 60)
        t0 = dt.datetime.now(dt.timezone.utc)
        pattern = f"spike-resume-{args.failure}-*.json"
        before_files = set(EVIDENCE.glob(pattern))
        r = subprocess.run([sys.executable, str(Path(__file__).parent / "probe.py"), "resume", "--failure",
                            args.failure, "--seconds", "240", "--crash-at-tick", str(tick), "--job", args.job],
                           capture_output=True, text=True, timeout=3000)
        # Only this run's evidence: a probe that failed before writing any must not be read as
        # the previous run's result (series 6 counted run 1 twice that way).
        new = sorted(set(EVIDENCE.glob(pattern)) - before_files)
        if not new:
            runs.append({"run": i + 1, "crashAtTick": tick, "probeExit": r.returncode, "evidence": None,
                         "result": "NO EVIDENCE", "resumed": False, "timings_s": None, "once": None,
                         "probeOutputTail": (r.stdout + r.stderr).strip().splitlines()[-30:]})
            print(json.dumps(runs[-1]), flush=True)
            break  # the cluster is in a state this script did not make; a person looks first
        evidence = new[-1]
        d = json.loads(evidence.read_text())
        after = neighbour()
        losses = probe.kubectl("-n", "cell-a", "logs", "jenkins-0", "-c", "cell-agent",
                               f"--since-time={t0.strftime('%Y-%m-%dT%H:%M:%SZ')}", check=False).count("lease lost")
        runs.append({
            "run": i + 1, "crashAtTick": tick, "probeExit": r.returncode, "evidence": evidence.name,
            "node": d.get("controllerNodeBefore"), "result": d.get("result"), "resumed": d.get("resumed"),
            "ticksMissing": d.get("ticksMissing"), "timings_s": d.get("timings_s"), "job": args.job, "once": d.get("once"),
            "nodeRestoredBySupervisor": d.get("nodeRestoredBySupervisor"),
            "machineAfterFencing": d.get("machineAfterFencing"),
            # A neighbour on the machine that lost power is not a healthy neighbour: it must have
            # been taken over too. One on another machine must not have noticed anything.
            "neighbourNode": before[2],
            "neighbourColocated": before[2] == d.get("controllerNodeBefore"),
            "neighbourJenkinsRestarted": after[1] != before[1] or after[0] != before[0],
            "neighbourLeaseLosses": losses,
            "neighbourHeldAgain": after[1] != before[1] and "True" in probe.kubectl(
                "-n", "cell-a", "get", "pod", "jenkins-0", "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}", check=False),
        })
        print(json.dumps(runs[-1]), flush=True)
    def series(key):
        return [r["timings_s"][key] for r in runs if r["timings_s"] and key in r["timings_s"]]
    summary = {k: {"values": series(k), "median": statistics.median(series(k)) if series(k) else None,
                   "max": max(series(k)) if series(k) else None} for k in ("fenced", "leaseTaken", "jenkinsUp", "resumed")}
    def neighbour_ok(r):
        if r["evidence"] is None:
            return False
        if r["neighbourColocated"]:
            return r["neighbourHeldAgain"]
        return not r["neighbourJenkinsRestarted"] and not r["neighbourLeaseLosses"]
    def once_ok(r):  # once-probe: one marker, one start of the block, nothing refused
        return r["once"] is None or (r["once"]["markers"] == 1 and r["once"]["firstStarts"] == 1 and not r["once"]["refused"])
    ok = all(r["result"] == "SUCCESS" and r["resumed"] and neighbour_ok(r) and r["nodeRestoredBySupervisor"] and once_ok(r)
             for r in runs)
    out = {"scenario": "chaos-poweroff", "started": started.isoformat(), "runs": runs, "summary": summary,
           "verdict": "PASS" if ok else "FAIL"}
    path = EVIDENCE / f"chaos-poweroff-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({"summary": summary, "verdict": out["verdict"], "evidence": path.name}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
