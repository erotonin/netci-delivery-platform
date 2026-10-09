#!/usr/bin/env python3
"""Does NetciCellWithoutHeadroom say when a takeover would cost a running build?

The supervisor's headroom check (ADR-060) counts as room the pods of lower priority that run no
build. This probe arranges the lab so that, for one cell, the only room on any other machine is
the room a running build holds, and checks end to end -- headroom watch, metric, Prometheus rule:
  1. the other machines are filled, leaving one with room only under a pod labelled as a
     Kubernetes-plugin agent (jenkins/label): the metric must go to 0 and the alert pend, then fire;
  2. the label is taken off (the same pod, now an idle one of low priority): the metric must
     return to 1.
The pods are pause containers bound to their node (spec.nodeName) and sized inside what is free,
so nothing real is preempted. Everything it creates is removed at the end, also on failure.

    python3 lab/spike/headroom_probe.py [--cell cell-a] [--wait-firing]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("KUBECONFIG", str(ROOT / ".netci-gate" / "lab" / "kubeconfig"))
PROM = "http://192.168.122.212:30909"
NS = "netci-system"
PAUSE = "docker.io/rancher/mirrored-pause:3.10.2"
MI = 1024 * 1024


def kubectl(*args: str, stdin: str | None = None) -> str:
    out = subprocess.run(["kubectl", *args], input=stdin, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def mem(q: str) -> int:
    units = {"Ki": 1024, "Mi": MI, "Gi": 1024 * MI, "k": 1000, "M": 10**6, "G": 10**9}
    for u, m in units.items():
        if q.endswith(u):
            return int(float(q[: -len(u)]) * m)
    return int(q)


def cpu(q: str) -> int:
    return int(q[:-1]) if q.endswith("m") else int(float(q) * 1000)


def requests(pod: dict) -> tuple[int, int]:
    """What the scheduler counts: containers, plus restartable init containers (sidecars)."""
    c = m = 0
    for ct in pod["spec"]["containers"] + [i for i in pod["spec"].get("initContainers", []) if i.get("restartPolicy") == "Always"]:
        r = (ct.get("resources") or {}).get("requests") or {}
        c, m = c + cpu(r.get("cpu", "0")), m + mem(r.get("memory", "0"))
    o = pod["spec"].get("overhead") or {}
    return c + cpu(o.get("cpu", "0")), m + mem(o.get("memory", "0"))


CELL_PRIORITY = 900000


def runs_a_build(p: dict) -> bool:
    """As internal/supervisor/headroom.go: a Kubernetes-plugin agent, or a pod not to be disrupted."""
    a = p["metadata"].get("annotations") or {}
    return ("jenkins/label" in (p["metadata"].get("labels") or {})
            or a.get("cluster-autoscaler.kubernetes.io/safe-to-evict") == "false" or a.get("karpenter.sh/do-not-disrupt") == "true")


def free() -> dict[str, dict[str, int]]:
    """Per machine: what the kubelet would still admit (physical), and the memory of pods the
    headroom check counts as room (evictable: lower priority than a cell, running no build)."""
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    pods = json.loads(kubectl("get", "pods", "-A", "-o", "json"))["items"]
    out = {}
    for n in nodes:
        a = n["status"]["allocatable"]
        c, m, ev = cpu(a["cpu"]), mem(a["memory"]), 0
        for p in pods:
            if p["spec"].get("nodeName") == n["metadata"]["name"] and p["status"].get("phase") not in ("Succeeded", "Failed"):
                pc, pm = requests(p)
                c, m = c - pc, m - pm
                if (p["spec"].get("priority") or 0) < CELL_PRIORITY and not runs_a_build(p):
                    ev += pm
        out[n["metadata"]["name"]] = {"cpu": c, "mem": m, "evictable": ev}
    return out


def pod(name: str, node: str, mem_bytes: int, priority_class: str | None, labels: dict[str, str]) -> str:
    spec = {"nodeName": node, "terminationGracePeriodSeconds": 0,
            "containers": [{"name": "pause", "image": PAUSE, "imagePullPolicy": "IfNotPresent",
                            "resources": {"requests": {"cpu": "10m", "memory": f"{mem_bytes // MI}Mi"},
                                          "limits": {"memory": f"{mem_bytes // MI}Mi"}}}]}
    if priority_class:
        spec["priorityClassName"] = priority_class
    return json.dumps({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NS,
                       "labels": {"netci.io/headroom-probe": "true", **labels}}, "spec": spec})


def headroom(cell: str) -> float | None:
    q = urllib.parse.urlencode({"query": f'min(netci_supervisor_cell_headroom{{cell="{cell}/jenkins"}})'})
    r = json.load(urllib.request.urlopen(f"{PROM}/api/v1/query?{q}", timeout=10))["data"]["result"]
    return float(r[0]["value"][1]) if r else None


def alert(cell: str) -> str:
    alerts = json.load(urllib.request.urlopen(f"{PROM}/api/v1/alerts", timeout=10))["data"]["alerts"]
    for a in alerts:
        if a["labels"].get("alertname") == "NetciCellWithoutHeadroom" and a["labels"].get("cell") == f"{cell}/jenkins":
            return a["state"]
    return "inactive"


def wait(what: str, ok, timeout: float) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if ok():
            return round(time.monotonic() - start, 1)
        time.sleep(2)
    raise TimeoutError(f"timed out waiting for {what}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", default="cell-a")
    ap.add_argument("--wait-firing", action="store_true", help="also wait out the alert's 10 minutes")
    args = ap.parse_args()
    cell_pod = json.loads(kubectl("-n", args.cell, "get", "pod", "jenkins-0", "-o", "json"))
    home = cell_pod["spec"]["nodeName"]
    need_cpu, need_mem = requests(cell_pod)
    facts: dict = {"scenario": "headroom-running-build", "cell": args.cell, "cellNode": home,
                   "controllerNeeds": {"cpuMilli": need_cpu, "memoryMiB": need_mem // MI}}
    if headroom(args.cell) != 1:
        print("the cell has no headroom before the probe; nothing to show", file=sys.stderr)
        return 2
    f = free()
    others = sorted((n for n in f if n != home), key=lambda n: f[n]["mem"])
    room_node, full_nodes = others[-1], others[:-1]
    extra = 128 * MI
    try:
        facts["freeBefore"] = {n: {"cpuMilli": v["cpu"], "memoryMiB": v["mem"] // MI, "evictableMiB": v["evictable"] // MI} for n, v in f.items()}
        # Machines that must take nothing: filled, with a pod as important as a platform service.
        # The headroom check still counts the evictable pods there as room: they must be fewer
        # than the controller needs.
        fills = {}
        for n in full_nodes:
            fills[n] = f[n]["mem"] - 64 * MI
            if 64 * MI + f[n]["evictable"] >= need_mem:
                raise RuntimeError(f"{n}: its evictable pods alone give a controller room; the probe cannot fill it")
        # The one with room: what the check sees as room is the controller and 128 Mi more; the
        # build takes the rest of what is free, so that without it the evictable pods alone are
        # less than the controller needs.
        fills[room_node] = f[room_node]["mem"] + f[room_node]["evictable"] - need_mem - extra
        build = f[room_node]["mem"] - fills[room_node]
        if fills[room_node] < 0 or build <= extra:
            raise RuntimeError(f"{room_node}: cannot leave room only under a build (free {f[room_node]})")
        facts["buildMiB"] = build // MI
        for n in others:
            if f[n]["cpu"] < need_cpu + 20:
                raise RuntimeError(f"{n} is short of CPU already; the probe shows memory only")
        for i, n in enumerate(full_nodes):
            kubectl("apply", "-f", "-", stdin=pod(f"headroom-fill-{i}", n, fills[n], "netci-platform", {}))
        kubectl("apply", "-f", "-", stdin=pod("headroom-fill-room", room_node, fills[room_node], "netci-platform", {}))
        kubectl("apply", "-f", "-", stdin=pod("headroom-build", room_node, build, None, {"jenkins/label": "headroom-probe"}))
        kubectl("-n", NS, "wait", "pod", "-l", "netci.io/headroom-probe=true", "--for=condition=Ready", "--timeout=120s")
        facts["arranged"] = {"full": full_nodes, "roomOnlyUnderABuild": room_node}
        started = time.monotonic()
        facts["headroomZeroAfterSeconds"] = wait("headroom 0", lambda: headroom(args.cell) == 0, 180)
        facts["alertPendingAfterSeconds"] = wait("alert pending", lambda: alert(args.cell) in ("pending", "firing"), 120) + facts["headroomZeroAfterSeconds"]
        events = json.loads(kubectl("get", "events", "-A", "--field-selector", "reason=NoTakeoverHeadroom", "-o", "json"))["items"]
        facts["event"] = max(events, key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")["message"] if events else None
        if args.wait_firing:
            wait("alert firing", lambda: alert(args.cell) == "firing", 13 * 60)
            facts["alertFiringAfterSeconds"] = round(time.monotonic() - started, 1)
        # The same pod, no longer a build: an idle pod of low priority, which gives way.
        kubectl("-n", NS, "label", "pod", "headroom-build", "jenkins/label-")
        facts["headroomBackAfterSeconds"] = wait("headroom 1", lambda: headroom(args.cell) == 1, 180)
        facts["verdict"] = "PASS"
    except Exception as e:  # noqa: BLE001 - recorded, and the cleanup below still runs
        facts["verdict"], facts["error"] = "FAIL", str(e)
    finally:
        kubectl("-n", NS, "delete", "pod", "-l", "netci.io/headroom-probe=true", "--wait=false")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "lab" / "evidence" / f"headroom-running-build-{stamp}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps(facts, indent=1))
    print("evidence:", out.name)
    return 0 if facts["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
