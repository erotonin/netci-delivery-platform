#!/usr/bin/env python3
"""Where do the ~15 s go that some builds lose, on the fabric and the Kubernetes plugin alike?

ADR-071 found builds of both that lost ~15 s either before their agent connected or before their
first `sh` step ran, and could not say why: the agents' pods were gone by the time anyone looked.
This follows every agent pod's log from the moment it exists (and the controller's), keeps the pod's
status as it changes, runs bursts of both jobs, and for each slow build keeps what its agent and the
controller said meanwhile.

    python3 lab/spike/agent_delay_probe.py [--bursts 5] [--size 4] [--slow 8]

Evidence: lab/evidence/agent-delay-<timestamp>.json (and the raw logs beside it, in
lab/evidence/agent-delay-<timestamp>/).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("NETCI_CELL", "cell-b")
os.environ.setdefault("NETCI_CELL_PORT", "30081")
sys.path.insert(0, str(Path(__file__).parent))
import fabric_timeline as ft  # noqa: E402
import probe  # noqa: E402

UTC = dt.timezone.utc
CELL = os.environ["NETCI_CELL"]
WATCH = [  # namespace, label selector, container
    (CELL, "jenkins=slave", "jnlp"),
    ("netci-agents", "netci.io/sandbox", "agent"),
]


class Follower(threading.Thread):
    """Follows the log of every agent pod as soon as its container has started, and keeps each
    pod's latest status (its conditions' and containers' times)."""

    def __init__(self, out: Path) -> None:
        super().__init__(daemon=True)
        self.out, self.stop = out, threading.Event()
        self.procs: dict[str, subprocess.Popen] = {}
        self.status: dict[str, dict] = {}

    def follow(self, ns: str, pod: str, container: str) -> None:
        f = open(self.out / f"{pod}.log", "w")
        self.procs[pod] = subprocess.Popen(["kubectl", "-n", ns, "logs", "-f", "--timestamps", pod, "-c", container],
                                           stdout=f, stderr=subprocess.STDOUT)

    def run(self) -> None:
        while not self.stop.is_set():
            for ns, sel, container in WATCH:
                try:
                    items = json.loads(probe.kubectl("-n", ns, "get", "pods", "-l", sel, "-o", "json"))["items"]
                except Exception:  # noqa: BLE001 - next poll
                    continue
                for p in items:
                    name = p["metadata"]["name"]
                    st = p.get("status", {})
                    self.status[name] = {
                        "node": p["spec"].get("nodeName"), "created": p["metadata"]["creationTimestamp"],
                        "conditions": {c["type"]: c.get("lastTransitionTime") for c in st.get("conditions", [])},
                        "containers": {c["name"]: c.get("state") for c in st.get("initContainerStatuses", []) + st.get("containerStatuses", [])},
                    }
                    started = any(c["name"] == container and ("running" in c.get("state", {}) or "terminated" in c.get("state", {}))
                                  for c in st.get("containerStatuses", []))
                    if started and name not in self.procs:
                        self.follow(ns, name, container)
            time.sleep(0.3)
        for p in self.procs.values():
            p.terminate()


class Computers(threading.Thread):
    """When the controller first lists each agent, and when it first calls it online: an agent's own
    "Connected" is not online yet (the controller still sets its channel up)."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.stop, self.seen, self.online = threading.Event(), {}, {}

    def run(self) -> None:
        j = probe.Jenkins()
        while not self.stop.is_set():
            try:
                now = time.time()
                for c in j.get_json("/computer/api/json?tree=computer[displayName,offline]")["computer"]:
                    self.seen.setdefault(c["displayName"], now)
                    if not c["offline"]:
                        self.online.setdefault(c["displayName"], now)
            except Exception:  # noqa: BLE001 - next poll
                pass
            time.sleep(0.2)


def lines_of(path: Path, start: float, end: float) -> list[str]:
    """Lines of a `kubectl logs --timestamps` file within [start, end], as 'seconds-from-start text'."""
    out = []
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+)Z (.*)", line)
        if not m:
            continue
        t = dt.datetime.fromisoformat(m.group(1)[:26]).replace(tzinfo=UTC).timestamp()
        if start - 2 <= t <= end + 2:
            out.append(f"{t - start:7.2f} {m.group(2)[:240]}")
    return out


def analyse(j: probe.Jenkins, job: str, number: int, slow: float) -> dict | None:
    lines = ft.stamped(j, job, number)
    t0 = lines[0][0]
    at = lambda pat: next((t for t, l in lines if re.search(pat, l)), None)  # noqa: E731
    node, running, sh = at(r"^\[Pipeline\] node"), at(r"^Running on "), at(r"^\[Pipeline\] sh")
    output = next((t for t, l in lines if sh and t > sh and not l.startswith("[Pipeline]")), None)
    if None in (node, running, sh, output):
        return {"build": number, "error": "console incomplete"}
    gaps = {"toRunningOn": round(running - node, 2), "shToOutput": round(output - sh, 2)}
    agent = next(l for t, l in lines if l.startswith("Running on ")).split()[2].strip("‘’'")
    return {"build": number, "job": job, "agent": agent, "gaps": gaps, "slow": max(gaps.values()) >= slow,
            "runningOn": round(running - t0, 2), "sh": round(sh - t0, 2), "consoleStart": t0, "consoleEnd": lines[-1][0],
            "console": [f"{t - t0:7.2f} {l[:200]}" for t, l in lines if not l.startswith(("  ", "apiVersion", "kind:", "metadata", "spec:"))][:40]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bursts", type=int, default=5)
    ap.add_argument("--size", type=int, default=4)
    ap.add_argument("--slow", type=float, default=8, help="a build is slow when a gap is at least this, in seconds")
    args = ap.parse_args()
    started = dt.datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    raw = probe.EVIDENCE / f"agent-delay-{stamp}"
    raw.mkdir()
    controller_log = open(raw / "controller.log", "w")
    ctl = subprocess.Popen(["kubectl", "-n", CELL, "logs", "-f", "--timestamps", "--since=1s", "jenkins-0", "-c", "jenkins"],
                           stdout=controller_log, stderr=subprocess.STDOUT)
    fol = Follower(raw)
    fol.start()
    comps = Computers()
    comps.start()
    builds: list[tuple[str, int]] = []
    try:
        for _ in range(args.bursts):
            for job in ("fabric-probe", "k8s-probe"):
                with ThreadPoolExecutor(args.size) as ex:
                    builds += [(job, r["build"]) for r in ex.map(lambda _: ft.run_once(probe.Jenkins(), job, True), range(args.size))]
                time.sleep(30)  # pools refill, pods go
    finally:
        time.sleep(3)
        fol.stop.set()
        comps.stop.set()
        fol.join(10)
        ctl.terminate()
    j = probe.Jenkins()
    every = []
    for job, n in sorted(set(builds)):
        a = analyse(j, job, n, args.slow)
        if not a:
            continue
        if "agent" in a:
            pod = a["agent"] if job == "k8s-probe" else probe.kubectl(
                "-n", "netci-system", "exec", "netci-pg-0", "--", "psql", "-U", "netci", "-d", "netci", "-At", "-c",
                f"SELECT pod FROM sandboxes WHERE agent = '{a['agent']}'")
            a["pod"], a["podStatus"] = pod, fol.status.get(pod)
            a["controllerListed"] = round(comps.seen[a["agent"]] - a["consoleStart"], 2) if a["agent"] in comps.seen else None
            a["controllerOnline"] = round(comps.online[a["agent"]] - a["consoleStart"], 2) if a["agent"] in comps.online else None
            connected = next((float(x.split()[0]) for x in lines_of(raw / f"{pod}.log", a["consoleStart"], a["consoleEnd"]) if "INFO: Connected" in x), None)
            a["agentConnected"] = connected
            a["agentLog"] = lines_of(raw / f"{pod}.log", a["consoleStart"], a["consoleEnd"])
            a["controllerLog"] = [l for l in lines_of(raw / "controller.log", a["consoleStart"], a["consoleEnd"])
                                  if a["agent"] in l or "WARN" in l or "SEVERE" in l][:60]
        if not a.get("slow"):
            a.pop("agentLog", None), a.pop("controllerLog", None), a.pop("console", None)
        every.append(a)
    slow = [a for a in every if a.get("slow") or "error" in a]
    facts = {"scenario": "agent-delay", "started": started.isoformat(timespec="seconds"), "bursts": args.bursts, "size": args.size,
             "slowSeconds": args.slow, "builds": len(set(builds)), "every": every, "slow": slow}
    out = probe.EVIDENCE / f"agent-delay-{stamp}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps({"builds": facts["builds"], "slow": [(s.get("job"), s["build"], s.get("gaps")) for s in slow]}, indent=1))
    print("evidence:", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
